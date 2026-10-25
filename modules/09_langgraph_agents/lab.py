"""Module 09 lab: a LangGraph-semantics state machine built from scratch.

Features: typed Pydantic state with per-field reducers, nodes as async functions returning partial updates,
conditional edges, cycles with a recursion limit, durable checkpoints after every node, resume after a crash,
human-approval interrupts, idempotent side-effecting tools, backoff retries. Syllabus extension: agency, ReAct,
plan-and-execute, router/supervisor, concurrent fan-out, nested graphs, map-reduce, reducers, validation. Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import operator
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Protocol, TypedDict

from pydantic import BaseModel, Field


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "msg": record.getMessage()}
        payload.update(getattr(record, "ctx", {}))
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter())
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


log = get_logger("lab09")

END = "__end__"


class GraphError(Exception):
    pass


class GraphRecursionError(GraphError):
    pass


class NodeCrash(GraphError):
    """Simulated process death inside/between nodes."""


class ToolTransientError(GraphError):
    pass


class Interrupted(GraphError):
    def __init__(self, thread_id: str, reason: str) -> None:
        super().__init__(f"{thread_id} interrupted: {reason}")
        self.thread_id, self.reason = thread_id, reason


class AgentState(BaseModel):
    task: str
    plan: list[str] = Field(default_factory=list)
    draft: str = ""
    feedback: list[str] = Field(default_factory=list)  # reducer: append
    revisions: int = 0
    approved: bool | None = None
    tool_log: list[str] = Field(default_factory=list)  # reducer: append
    refunds_issued: int = 0
    tokens_used: int = 0  # reducer: add
    schema_version: int = 1


APPEND = {"feedback", "tool_log"}
ADD = {"tokens_used"}


def apply_update(state: AgentState, update: dict[str, Any]) -> AgentState:
    data = state.model_dump()
    for k, v in update.items():
        if k not in data:
            raise GraphError(f"node returned unknown state key {k!r}")
        if k in APPEND:
            data[k] = data[k] + list(v)
        elif k in ADD:
            data[k] = data[k] + v
        else:
            data[k] = v
    return AgentState.model_validate(data)


class Checkpoint(BaseModel):
    thread_id: str
    step: int
    next_node: str
    state: AgentState
    pending_interrupt: str | None = None


class FileCheckpointer:
    """Durable JSON-per-thread store; swap for Postgres/Redis. Atomic via write-temp-then-rename."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, thread_id: str) -> Path:
        return self.root / f"{thread_id}.json"

    async def put(self, cp: Checkpoint) -> None:
        tmp = self._path(cp.thread_id).with_suffix(".tmp")
        await asyncio.to_thread(tmp.write_text, cp.model_dump_json())
        await asyncio.to_thread(tmp.replace, self._path(cp.thread_id))

    async def get(self, thread_id: str) -> Checkpoint | None:
        p = self._path(thread_id)
        if not p.exists():
            return None
        return Checkpoint.model_validate_json(await asyncio.to_thread(p.read_text))


NodeFn = Callable[[AgentState], Awaitable[dict[str, Any]]]
Router = Callable[[AgentState], str]


class StateGraph:
    def __init__(self, recursion_limit: int = 25) -> None:
        self.nodes: dict[str, NodeFn] = {}
        self.edges: dict[str, str] = {}
        self.cond: dict[str, Router] = {}
        self.entry = ""
        self.interrupt_before: set[str] = set()
        self.recursion_limit = recursion_limit

    def add_node(self, name: str, fn: NodeFn) -> "StateGraph":
        self.nodes[name] = fn
        return self

    def add_edge(self, src: str, dst: str) -> "StateGraph":
        self.edges[src] = dst
        return self

    def add_conditional_edges(self, src: str, router: Router) -> "StateGraph":
        self.cond[src] = router
        return self

    def set_entry(self, name: str) -> "StateGraph":
        self.entry = name
        return self

    def compile(self, checkpointer: FileCheckpointer, crash_after: str | None = None) -> "CompiledGraph":
        for src, dst in self.edges.items():
            if src not in self.nodes or (dst not in self.nodes and dst != END):
                raise GraphError(f"bad edge {src}->{dst}")
        return CompiledGraph(self, checkpointer, crash_after)


class CompiledGraph:
    def __init__(self, g: StateGraph, cp: FileCheckpointer, crash_after: str | None) -> None:
        self.g, self.cp, self.crash_after = g, cp, crash_after

    def _next(self, node: str, state: AgentState) -> str:
        if node in self.g.cond:
            nxt = self.g.cond[node](state)
        elif node in self.g.edges:
            nxt = self.g.edges[node]
        else:
            raise GraphError(f"node {node!r} has no outgoing edge")
        if nxt != END and nxt not in self.g.nodes:
            raise GraphError(f"router returned unknown node {nxt!r}")
        return nxt

    async def invoke(self, thread_id: str, initial: AgentState | None = None, resume_update: dict[str, Any] | None = None) -> AgentState:
        cp = await self.cp.get(thread_id)
        if cp is None:
            if initial is None:
                raise GraphError("no checkpoint and no initial state")
            cp = Checkpoint(thread_id=thread_id, step=0, next_node=self.g.entry, state=initial)
        elif cp.pending_interrupt and resume_update is None:
            raise Interrupted(thread_id, cp.pending_interrupt)
        state, node, step = cp.state, cp.next_node, cp.step
        if resume_update:
            state = apply_update(state, resume_update)
            cp = cp.model_copy(update={"state": state, "pending_interrupt": None})
        interrupted_once = {n for n in self.g.interrupt_before if resume_update and n == node}
        while node != END:
            if step >= self.g.recursion_limit:
                raise GraphRecursionError(f"{thread_id}: exceeded {self.g.recursion_limit} steps")
            if node in self.g.interrupt_before and node not in interrupted_once:
                await self.cp.put(Checkpoint(thread_id=thread_id, step=step, next_node=node, state=state, pending_interrupt=f"approval needed before {node}"))
                raise Interrupted(thread_id, f"approval needed before {node}")
            update = await self.g.nodes[node](state)
            state = apply_update(state, update)
            step += 1
            nxt = self._next(node, state)
            await self.cp.put(Checkpoint(thread_id=thread_id, step=step, next_node=nxt, state=state))
            log.info("step", extra={"ctx": {"thread": thread_id, "step": step, "node": node, "next": nxt}})
            if self.crash_after == node:
                self.crash_after = None
                raise NodeCrash(f"process died after {node}")
            node = nxt
        return state


class FakeLLM:
    """Critic rejects the first draft; deterministic."""

    async def draft(self, task: str, feedback: list[str]) -> str:
        await asyncio.sleep(0.001)
        return f"Answer to '{task}'" + (" (revised: " + feedback[-1] + ")" if feedback else "")

    async def critique(self, draft: str) -> str | None:
        await asyncio.sleep(0.001)
        return None if "revised" in draft else "add concrete figures"


class RefundTool:
    """Side-effecting tool made idempotent by key; transient failures retried with backoff."""

    def __init__(self) -> None:
        self.done: dict[str, int] = {}
        self.fail_next = 1

    async def issue(self, key: str, amount: int) -> str:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ToolTransientError("payments gateway 503")
        self.done.setdefault(key, amount)
        return f"refund:{key}:{self.done[key]}"


def build_graph(llm: FakeLLM, tool: RefundTool, thread_id: str) -> StateGraph:
    async def plan(s: AgentState) -> dict[str, Any]:
        return {"plan": ["draft", "critique", "refund"], "tokens_used": 50}

    async def draft(s: AgentState) -> dict[str, Any]:
        text = await llm.draft(s.task, s.feedback)
        return {"draft": text, "revisions": s.revisions + 1, "tokens_used": 200}

    async def critic(s: AgentState) -> dict[str, Any]:
        fb = await llm.critique(s.draft)
        return {"feedback": [fb] if fb else [], "tokens_used": 80}

    async def refund(s: AgentState) -> dict[str, Any]:
        key = f"{thread_id}:refund"  # same key on re-run ⇒ at-most-once effect
        for attempt in range(1, 5):
            try:
                receipt = await tool.issue(key, 25)
                return {"tool_log": [receipt], "refunds_issued": len(tool.done)}
            except ToolTransientError:
                await asyncio.sleep(random.uniform(0, 0.003 * 2 ** attempt))
        raise GraphError("refund tool unavailable")

    def after_critic(s: AgentState) -> str:
        if s.revisions >= 3:
            return "refund"  # bounded loop: stop iterating, escalate in real systems
        return "draft" if s.feedback and s.feedback[-1] and "revised" not in s.draft else "refund"

    g = StateGraph(recursion_limit=12)
    g.add_node("plan", plan).add_node("draft", draft).add_node("critic", critic).add_node("refund", refund)
    g.set_entry("plan").add_edge("plan", "draft").add_edge("draft", "critic")
    g.add_conditional_edges("critic", after_critic).add_edge("refund", END)
    g.interrupt_before.add("refund")
    return g


# ---- Syllabus extension: typed errors and backoff helper ---
class NodeTimeoutError(GraphError):
    def __init__(self, node: str, timeout_s: float) -> None:
        super().__init__(f"node {node!r} exceeded {timeout_s}s")
        self.node, self.timeout_s = node, timeout_s


class StateConflictError(GraphError):
    """Two concurrent branches wrote the same field and no reducer says how to combine them."""

    def __init__(self, field: str, writers: list[str]) -> None:
        super().__init__(f"concurrent writes to non-reducer field {field!r} by {sorted(writers)}")
        self.field, self.writers = field, writers


class GraphValidationError(GraphError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class BranchFailedError(GraphError):
    def __init__(self, node: str, cause: BaseException) -> None:
        super().__init__(f"branch {node!r} failed: {cause!r}")
        self.node, self.cause = node, cause


class AgentLoopError(GraphError):
    """An agent or ReAct loop hit its step guard without terminating."""


class UnknownToolError(GraphError):
    pass


class ToolPermanentError(GraphError):
    def __init__(self, tool: str, detail: str) -> None:
        super().__init__(f"tool {tool!r} failed permanently: {detail}")
        self.tool = tool


class PlanFailedError(GraphError):
    pass


class LangGraphNotInstalledError(GraphError):
    pass


async def with_backoff(op: Callable[[], Awaitable[Any]], attempts: int = 4, base_s: float = 0.002,
                       retry_on: tuple[type[Exception], ...] = (ToolTransientError,)) -> Any:
    """Exponential backoff with full jitter around any I/O call; the last attempt is allowed to raise."""
    for attempt in range(1, attempts):
        try:
            return await op()
        except retry_on:
            await asyncio.sleep(random.uniform(0, base_s * 2 ** attempt))
    return await op()


# ---- Syllabus extension: defining agency (agent protocol vs fixed chain) ---
class AgentObservation(BaseModel):
    source: str
    content: str


class AgentAction(BaseModel):
    tool: str = ""
    value: str = ""
    finish: bool = False  # termination is an action the agent chooses, not a fixed length
    answer: str = ""


class AgentMemory(BaseModel):
    observations: list[AgentObservation] = Field(default_factory=list)
    actions: list[AgentAction] = Field(default_factory=list)


ToolFn = Callable[[str], Awaitable[str]]


class Agent(Protocol):
    """Agency = a goal + tools + memory + a loop where perceive/decide/act repeats until the agent says stop."""

    goal: str
    memory: AgentMemory
    tools: dict[str, ToolFn]

    def perceive(self, obs: AgentObservation) -> None: ...

    def decide(self) -> AgentAction: ...

    async def act(self, action: AgentAction) -> AgentObservation: ...


ORDERS_BY_EMAIL = {"ana@example.com": "1042"}
ETA_BY_ORDER = {"1042": "2 days"}


async def find_order(email: str) -> str:
    await asyncio.sleep(0.001)
    return ORDERS_BY_EMAIL.get(email, "not_found")


async def get_eta(order_id: str) -> str:
    await asyncio.sleep(0.001)
    return ETA_BY_ORDER.get(order_id, "unknown_order")


class OrderAgent:
    def __init__(self, goal: str, known: dict[str, str], tools: dict[str, ToolFn]) -> None:
        self.goal, self.known, self.tools = goal, known, tools
        self.memory = AgentMemory()

    def perceive(self, obs: AgentObservation) -> None:
        self.memory.observations.append(obs)

    def decide(self) -> AgentAction:
        facts = {o.source: o.content for o in self.memory.observations}
        if "get_eta" in facts:
            eta = facts["get_eta"]
            return AgentAction(finish=True, answer="could not determine ETA" if eta == "unknown_order" else f"ETA {eta}")
        found = facts.get("find_order")
        order_id = self.known.get("order_id") or (found if found not in (None, "not_found") else "")
        if order_id:
            return AgentAction(tool="get_eta", value=order_id)
        if found == "not_found":
            return AgentAction(finish=True, answer="cannot identify the order")
        return AgentAction(tool="find_order", value=self.known.get("email", ""))

    async def act(self, action: AgentAction) -> AgentObservation:
        self.memory.actions.append(action)
        return AgentObservation(source=action.tool, content=await self.tools[action.tool](action.value))


async def run_agent(agent: Agent, max_steps: int = 6) -> str:
    for _ in range(max_steps):
        action = agent.decide()
        if action.finish:
            return action.answer
        agent.perceive(await agent.act(action))
    raise AgentLoopError(f"agent did not finish within {max_steps} steps")


async def fixed_chain(tools: dict[str, ToolFn], known: dict[str, str]) -> str:
    """Contrast: the control flow is frozen at design time (always get_eta, then format)."""
    eta = await tools["get_eta"](known.get("order_id", ""))
    return "could not determine ETA" if eta == "unknown_order" else f"ETA {eta}"


# ---- Syllabus extension: ReAct loop (thought / action / observation) ---
THOUGHT_RE = re.compile(r"Thought:\s*(.+)")
ACTION_RE = re.compile(r"Action:\s*(\w+)\[(.*)\]")
FINAL_RE = re.compile(r"Final Answer:\s*(.+)", re.S)
CALC_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([+\-*/])\s*(\d+(?:\.\d+)?)\s*$")


class ScriptedLLM:
    """Deterministic stand-in for a chat model: replays canned completions and records every prompt it saw."""

    def __init__(self, script: list[str]) -> None:
        self.script: list[str] = list(script)
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        await asyncio.sleep(0.001)
        self.prompts.append(prompt)
        if not self.script:
            raise GraphError("scripted LLM exhausted")
        return self.script.pop(0)


async def tool_price(item: str) -> str:
    await asyncio.sleep(0.001)
    return {"widget": "12", "gadget": "30"}.get(item.strip(), "unknown_item")


async def tool_calc(expr: str) -> str:
    m = CALC_RE.match(expr)
    if not m:
        raise ToolPermanentError("calc", f"cannot parse {expr!r}")
    a, op, b = float(m.group(1)), m.group(2), float(m.group(3))
    result = {"+": a + b, "-": a - b, "*": a * b, "/": a / b if b else float("nan")}[op]
    return str(int(result)) if result == int(result) else str(round(result, 4))


class ReActStep(BaseModel):
    thought: str
    action: str
    action_input: str
    observation: str


async def react_loop(llm: ScriptedLLM, tools: dict[str, ToolFn], question: str, max_steps: int = 6) -> tuple[str, list[ReActStep]]:
    """Thought -> Action -> Observation, appended to the transcript, until 'Final Answer' or the step guard."""
    transcript = f"Question: {question}\n"
    steps: list[ReActStep] = []
    for _ in range(max_steps):
        out = await llm.complete(transcript)
        final = FINAL_RE.search(out)
        if final:
            return final.group(1).strip(), steps
        t = THOUGHT_RE.search(out)
        m = ACTION_RE.search(out)
        name, arg = (m.group(1), m.group(2)) if m else ("", "")
        if not m:
            obs = "error: reply must contain 'Action: tool[input]' or 'Final Answer: ...'"
        elif name not in tools:
            obs = f"error: unknown tool {name!r}; available: {sorted(tools)}"
        else:
            try:
                obs = await with_backoff(partial(tools[name], arg))
            except GraphError as exc:  # tool failure becomes an observation the model can react to
                obs = f"error: {exc}"
        steps.append(ReActStep(thought=t.group(1) if t else "", action=name, action_input=arg, observation=obs))
        transcript += f"{out}\nObservation: {obs}\n"
    raise AgentLoopError(f"ReAct did not finish within {max_steps} steps")


# ---- Syllabus extension: Plan-and-Execute (planner, executor, replanner) ---
class PlanStep(BaseModel):
    step_id: int
    tool: str
    arg: str  # "$prev" means: feed in the previous step's output
    purpose: str


class Plan(BaseModel):
    goal: str
    version: int
    steps: list[PlanStep]


Planner = Callable[[str, list[str], int], Plan]


def revenue_planner(goal: str, banned_tools: list[str], version: int) -> Plan:
    """The (re)planner: same goal, but it routes around tools the executor reported as broken."""
    source = "warehouse_export" if "primary_db" in banned_tools else "primary_db"
    return Plan(goal=goal, version=version, steps=[
        PlanStep(step_id=1, tool=source, arg="Q3", purpose="fetch Q3 revenue in USD"),
        PlanStep(step_id=2, tool="usd_to_eur", arg="$prev", purpose="convert currency"),
        PlanStep(step_id=3, tool="format_report", arg="$prev", purpose="render the answer"),
    ])


async def plan_and_execute(goal: str, planner: Planner, tools: dict[str, ToolFn], max_replans: int = 2) -> tuple[str, list[str]]:
    banned: list[str] = []
    trace: list[str] = []
    plan = planner(goal, banned, 1)
    while True:
        prev, current = "", plan.steps[0]
        try:
            for current in plan.steps:
                if current.tool not in tools:
                    raise PlanFailedError(f"planner produced unknown tool {current.tool!r}")
                arg = prev if current.arg == "$prev" else current.arg
                prev = await with_backoff(partial(tools[current.tool], arg), attempts=2)
                trace.append(f"v{plan.version} step{current.step_id} {current.tool} ok")
            return prev, trace
        except ToolPermanentError as exc:
            trace.append(f"v{plan.version} step{current.step_id} {current.tool} FAILED -> replan")
            banned.append(current.tool)
            if plan.version > max_replans:
                raise PlanFailedError(f"gave up after {plan.version} plans; banned={banned}") from exc
            plan = planner(goal, banned, plan.version + 1)


async def tool_primary_db(quarter: str) -> str:
    raise ToolPermanentError("primary_db", "replica lag, read refused")


async def tool_warehouse_export(quarter: str) -> str:
    await asyncio.sleep(0.001)
    return "1200000"


async def tool_usd_to_eur(usd: str) -> str:
    return str(int(int(usd) * 0.92))


async def tool_format_report(eur: str) -> str:
    return f"Q3 revenue: EUR {int(eur):,}"


# ---- Syllabus extension: reducers, conflict detection, concurrent graph engine ---
Reducer = Callable[[Any, Any], Any]
BranchFn = Callable[[Any], Awaitable[dict[str, Any]]]
MapFn = Callable[[Any, Any], Awaitable[dict[str, Any]]]


class Versioned(BaseModel):
    value: str = ""
    version: int = 0
    writer: str = ""


def reduce_append(old: list[Any], new: list[Any]) -> list[Any]:
    return list(old) + list(new)


def reduce_add(old: int, new: int) -> int:
    return old + new


def reduce_max(old: int, new: int) -> int:
    return max(old, new)


def reduce_lww(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Last-write-wins by (version, writer): a total order, so the outcome never depends on branch finish order."""
    return new if (new["version"], new["writer"]) >= (old["version"], old["writer"]) else old


def merge_updates(state: BaseModel, updates: list[tuple[str, dict[str, Any]]], reducers: dict[str, Reducer]) -> BaseModel:
    """Fold node updates into state. Two writers on one field need a reducer, otherwise StateConflictError."""
    data = state.model_dump()
    writers: dict[str, list[str]] = {}
    for node, upd in updates:
        for key in upd:
            if key not in data:
                raise GraphError(f"node {node!r} returned unknown state key {key!r}")
            writers.setdefault(key, []).append(node)
    for key, who in writers.items():
        if len(who) > 1 and key not in reducers:
            raise StateConflictError(key, who)
    for _, upd in updates:  # spawn order is deterministic because gather preserves it
        for key, val in upd.items():
            val = val.model_dump() if isinstance(val, BaseModel) else val
            data[key] = reducers[key](data[key], val) if key in reducers else val
    return type(state).model_validate(data)


@dataclass
class MapSpec:
    name: str
    items: Callable[[Any], list[Any]]
    worker: MapFn
    reduce_node: str
    max_concurrency: int


class ConcurrentGraph(StateGraph):
    """Extends StateGraph with typed per-field reducers, parallel fan-out, map-reduce, subgraphs and validation."""

    def __init__(self, state_cls: type[BaseModel], reducers: dict[str, Reducer] | None = None, recursion_limit: int = 25,
                 default_timeout_s: float = 1.0) -> None:
        super().__init__(recursion_limit)
        self.state_cls, self.reducers, self.default_timeout_s = state_cls, dict(reducers or {}), default_timeout_s
        self.timeouts: dict[str, float] = {}
        self.targets: dict[str, list[str]] = {}
        self.fanouts: dict[str, tuple[list[str], str]] = {}
        self.maps: dict[str, MapSpec] = {}

    def add_node(self, name: str, fn: Any, timeout_s: float | None = None) -> "ConcurrentGraph":
        super().add_node(name, fn)
        if timeout_s is not None:
            self.timeouts[name] = timeout_s
        return self

    def add_conditional_edges(self, src: str, router: Any, targets: list[str] | None = None) -> "ConcurrentGraph":
        super().add_conditional_edges(src, router)
        self.targets[src] = list(targets or [])  # declared so compile-time validation can see every route
        return self

    def add_fanout(self, src: str, branches: list[str], join: str) -> "ConcurrentGraph":
        self.fanouts[src] = (list(branches), join)
        return self

    def add_map(self, src: str, name: str, items: Callable[[Any], list[Any]], worker: MapFn, reduce_node: str,
                max_concurrency: int = 4, timeout_s: float | None = None) -> "ConcurrentGraph":
        self.maps[src] = MapSpec(name, items, worker, reduce_node, max_concurrency)
        if timeout_s is not None:
            self.timeouts[name] = timeout_s
        return self

    def add_subgraph(self, name: str, child: "CompiledConcurrent", input_map: Callable[[Any], BaseModel],
                     output_map: Callable[[Any], dict[str, Any]], timeout_s: float | None = None) -> "ConcurrentGraph":
        """A node that runs a whole compiled child graph with its own state schema, mapped in and out."""
        async def run_child(parent: Any) -> dict[str, Any]:
            return output_map(await child.ainvoke(input_map(parent)))

        return self.add_node(name, run_child, timeout_s)

    def _successors(self) -> dict[str, list[str]]:
        succ: dict[str, list[str]] = {n: [] for n in self.nodes}
        for src, dst in self.edges.items():
            succ.setdefault(src, []).append(dst)
        for src, tgts in self.targets.items():
            succ.setdefault(src, []).extend(tgts)
        for src, (branches, join) in self.fanouts.items():
            succ.setdefault(src, []).extend(branches)
            for b in branches:
                succ.setdefault(b, []).append(join)
        for src, spec in self.maps.items():
            succ.setdefault(src, []).append(spec.reduce_node)
        return succ

    def validate(self) -> list[str]:
        problems: list[str] = []
        known = set(self.nodes) | {END}
        if self.entry not in self.nodes:
            problems.append(f"entry {self.entry!r} is not a node")
        succ = self._successors()
        branch_nodes = {b for branches, _ in self.fanouts.values() for b in branches}
        for src, dsts in succ.items():
            if src not in self.nodes:
                problems.append(f"edge source {src!r} is not a node")
            for dst in dsts:
                if dst not in known:
                    problems.append(f"edge {src}->{dst} points at an unknown node")
        for src in self.cond:
            if not self.targets.get(src):
                problems.append(f"conditional edges on {src!r} declare no targets")
        for n in self.nodes:
            mechanisms = [n in self.edges, n in self.cond, n in self.fanouts, n in self.maps]
            if sum(mechanisms) > 1:
                problems.append(f"node {n!r} has more than one outgoing mechanism")
            if n in branch_nodes and (n in self.edges or n in self.cond):
                problems.append(f"fan-out branch {n!r} must not define its own edges")
        missing = sorted(n for n in self.nodes if not succ.get(n) and n not in branch_nodes)
        problems.extend(f"missing edge: node {n!r} has no outgoing edge" for n in missing)
        reachable: set[str] = set()
        stack = [self.entry] if self.entry in self.nodes else []
        while stack:
            cur = stack.pop()
            if cur in reachable or cur == END:
                continue
            reachable.add(cur)
            stack.extend(succ.get(cur, []))
        unreachable = sorted(set(self.nodes) - reachable)
        problems.extend(f"unreachable node {n!r}" for n in unreachable)
        can_end = {n for n in self.nodes if END in succ.get(n, [])}
        grew = True
        while grew:  # reverse reachability: who can eventually reach END
            grew = False
            for n, dsts in succ.items():
                if n in self.nodes and n not in can_end and any(d in can_end for d in dsts):
                    can_end.add(n)
                    grew = True
        trapped = sorted(n for n in reachable if n not in can_end and n not in missing)
        if trapped:
            problems.append(f"cycle without exit: nodes {trapped} can never reach END")
        return problems

    def compile_concurrent(self) -> "CompiledConcurrent":
        problems = self.validate()
        if problems:
            raise GraphValidationError(problems)
        return CompiledConcurrent(self)


class CompiledConcurrent:
    def __init__(self, g: ConcurrentGraph) -> None:
        self.g = g

    async def _call(self, name: str, op: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        timeout = self.g.timeouts.get(name, self.g.default_timeout_s)
        try:
            return await asyncio.wait_for(op(), timeout)
        except asyncio.TimeoutError as exc:
            raise NodeTimeoutError(name, timeout) from exc

    async def _bounded(self, sem: asyncio.Semaphore | None, name: str, op: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        if sem is None:
            return await self._call(name, op)
        async with sem:  # wait for a slot first so queueing time does not count against the node timeout
            return await self._call(name, op)

    async def _gather(self, jobs: list[tuple[str, Callable[[], Awaitable[dict[str, Any]]]]],
                      sem: asyncio.Semaphore | None = None) -> list[tuple[str, dict[str, Any]]]:
        results = await asyncio.gather(*(self._bounded(sem, n, op) for n, op in jobs), return_exceptions=True)
        for (name, _), res in zip(jobs, results):
            if isinstance(res, NodeTimeoutError):
                raise res
            if isinstance(res, BaseException):
                raise BranchFailedError(name, res) from res
        return [(name, res) for (name, _), res in zip(jobs, results) if isinstance(res, dict)]

    def _route(self, node: str, state: BaseModel) -> str:
        g = self.g
        nxt = g.cond[node](state) if node in g.cond else g.edges[node]
        if nxt != END and nxt not in g.nodes:
            raise GraphError(f"router returned unknown node {nxt!r}")
        return nxt

    async def ainvoke(self, initial: BaseModel, thread: str = "run") -> Any:
        g = self.g
        state, node, step = initial, g.entry, 0
        while node != END:
            if step >= g.recursion_limit:
                raise GraphRecursionError(f"{thread}: exceeded {g.recursion_limit} steps")
            step += 1
            state = merge_updates(state, [(node, await self._call(node, partial(g.nodes[node], state)))], g.reducers)
            width = 1
            if node in g.fanouts:
                branches, nxt = g.fanouts[node]
                done = await self._gather([(b, partial(g.nodes[b], state)) for b in branches])
                state, width = merge_updates(state, done, g.reducers), len(branches)
            elif node in g.maps:
                spec = g.maps[node]
                items = spec.items(state)
                sem = asyncio.Semaphore(spec.max_concurrency)
                done = await self._gather([(f"{spec.name}[{i}]", partial(spec.worker, state, item)) for i, item in enumerate(items)], sem)
                state, nxt, width = merge_updates(state, done, g.reducers), spec.reduce_node, len(items)
            else:
                nxt = self._route(node, state)
            log.info("cstep", extra={"ctx": {"thread": thread, "step": step, "node": node, "next": nxt, "parallel_width": width}})
            node = nxt
        return state


# ---- Syllabus extension: router vs supervisor patterns (specialist subgraphs) ---
class TicketState(BaseModel):
    question: str
    intent: str = ""
    answer: str = ""
    trail: list[str] = Field(default_factory=list)  # reducer: append


class SpecState(BaseModel):
    """Child schema: deliberately different from TicketState; the parent maps values in and out."""

    question: str
    answer: str = ""
    steps: list[str] = Field(default_factory=list)  # reducer: append


def make_specialist(domain: str, playbook: str) -> CompiledConcurrent:
    g = ConcurrentGraph(SpecState, {"steps": reduce_append})

    async def lookup(s: SpecState) -> dict[str, Any]:
        await asyncio.sleep(0.001)
        return {"steps": [f"{domain}:lookup"]}

    async def compose(s: SpecState) -> dict[str, Any]:
        return {"answer": f"[{domain}] {playbook} for '{s.question}'", "steps": [f"{domain}:compose"]}

    g.add_node("lookup", lookup).add_node("compose", compose).set_entry("lookup")
    g.add_edge("lookup", "compose").add_edge("compose", END)
    return g.compile_concurrent()


INTENT_KEYWORDS = {"billing": ("invoice", "refund", "charge"), "tech": ("error", "crash", "timeout")}


def build_router_graph() -> CompiledConcurrent:
    """Router pattern: ONE classification, ONE specialist, done. No loop, no re-decision."""
    g = ConcurrentGraph(TicketState, {"trail": reduce_append})

    async def classify(s: TicketState) -> dict[str, Any]:
        intent = next((k for k, words in INTENT_KEYWORDS.items() if any(w in s.question.lower() for w in words)), "general")
        return {"intent": intent, "trail": [f"classify->{intent}"]}

    def route(s: TicketState) -> str:
        return {"billing": "billing_agent", "tech": "tech_agent"}.get(s.intent, "general_agent")

    g.add_node("classify", classify).set_entry("classify")
    g.add_conditional_edges("classify", route, targets=["billing_agent", "tech_agent", "general_agent"])
    for name, domain, playbook in (("billing_agent", "billing", "check ledger and refund policy"),
                                   ("tech_agent", "tech", "collect logs and reproduce"),
                                   ("general_agent", "general", "route to a human triager")):
        g.add_subgraph(name, make_specialist(domain, playbook),
                       input_map=lambda p: SpecState(question=p.question),
                       output_map=lambda c: {"answer": c.answer, "trail": c.steps})
        g.add_edge(name, END)
    return g.compile_concurrent()


class SupState(BaseModel):
    task: str
    next_worker: str = ""
    notes: list[str] = Field(default_factory=list)  # reducer: append
    drafts: int = 0  # reducer: add
    reviews: int = 0  # reducer: add
    approved: bool = False


def build_supervisor_graph() -> CompiledConcurrent:
    """Supervisor pattern: after EVERY worker reply control returns to the supervisor, which decides again."""
    g = ConcurrentGraph(SupState, {"notes": reduce_append, "drafts": reduce_add, "reviews": reduce_add}, recursion_limit=15)

    async def supervisor(s: SupState) -> dict[str, Any]:
        if not any(n.startswith("research") for n in s.notes):
            nxt = "researcher"
        elif s.approved:
            nxt = "FINISH"
        elif s.drafts <= s.reviews:
            nxt = "writer"  # no fresh draft since the last review
        else:
            nxt = "reviewer"
        return {"next_worker": nxt, "notes": [f"supervisor->{nxt}"]}

    async def researcher(s: SupState) -> dict[str, Any]:
        return {"notes": ["research: 3 sources"]}

    async def writer(s: SupState) -> dict[str, Any]:
        return {"drafts": 1, "notes": [f"draft v{s.drafts + 1}"]}

    async def reviewer(s: SupState) -> dict[str, Any]:
        ok = s.drafts >= 2  # first draft is always sent back
        return {"reviews": 1, "approved": ok, "notes": ["review: approve" if ok else "review: revise"]}

    def decide(s: SupState) -> str:
        return END if s.next_worker == "FINISH" else s.next_worker

    for name, fn in (("supervisor", supervisor), ("researcher", researcher), ("writer", writer), ("reviewer", reviewer)):
        g.add_node(name, fn)
    g.set_entry("supervisor").add_conditional_edges("supervisor", decide, targets=["researcher", "writer", "reviewer", END])
    for w in ("researcher", "writer", "reviewer"):
        g.add_edge(w, "supervisor")
    return g.compile_concurrent()


# ---- Syllabus extension: concurrent fan-out, state conflicts, nested graphs, map-reduce ---
class ResearchState(BaseModel):
    topic: str
    findings: list[str] = Field(default_factory=list)  # reducer: append
    tokens: int = 0  # reducer: add
    confidence: int = 0  # reducer: max
    status: Versioned = Field(default_factory=Versioned)  # reducer: last-write-wins by version
    summary: str = ""  # single writer only


RESEARCH_REDUCERS: dict[str, Reducer] = {"findings": reduce_append, "tokens": reduce_add, "confidence": reduce_max, "status": reduce_lww}


def build_research_graph(crm_delay_s: float = 0.04, crm_timeout_s: float = 0.5, conflict: bool = False) -> CompiledConcurrent:
    g = ConcurrentGraph(ResearchState, RESEARCH_REDUCERS)

    async def plan(s: ResearchState) -> dict[str, Any]:
        return {"status": Versioned(value="planned", version=1, writer="plan")}

    def make_branch(name: str, delay: float, version: int, score: int) -> BranchFn:
        async def branch(s: ResearchState) -> dict[str, Any]:
            await asyncio.sleep(delay)
            out: dict[str, Any] = {"findings": [f"{name}: fact about {s.topic}"], "tokens": 100, "confidence": score,
                                   "status": Versioned(value=f"{name} done", version=version, writer=name)}
            if conflict and name in ("web", "docs"):
                out["summary"] = f"{name} wrote the summary"  # two writers, no reducer: must be rejected
            return out

        return branch

    async def synthesize(s: ResearchState) -> dict[str, Any]:
        return {"summary": " | ".join(sorted(s.findings))}

    g.add_node("plan", plan).add_node("synthesize", synthesize)
    g.add_node("web", make_branch("web", 0.04, 2, 70)).add_node("docs", make_branch("docs", 0.04, 3, 85))
    g.add_node("crm", make_branch("crm", crm_delay_s, 2, 60), timeout_s=crm_timeout_s)
    g.set_entry("plan").add_fanout("plan", ["web", "docs", "crm"], "synthesize").add_edge("synthesize", END)
    return g.compile_concurrent()


class ParentState(BaseModel):
    question: str
    research_note: str = ""
    report: str = ""


class ChildState(BaseModel):
    topic: str
    findings: list[str] = Field(default_factory=list)  # reducer: append
    tokens: int = 0  # reducer: add


def build_parent_child_graph() -> CompiledConcurrent:
    """Parent node 'research' = a compiled child graph (own schema, own fan-out); parent only sees mapped fields."""
    child = ConcurrentGraph(ChildState, {"findings": reduce_append, "tokens": reduce_add})

    async def seed(s: ChildState) -> dict[str, Any]:
        return {"tokens": 10}

    def make(name: str) -> BranchFn:
        async def probe(s: ChildState) -> dict[str, Any]:
            await asyncio.sleep(0.01)
            return {"findings": [f"{name}({s.topic})"], "tokens": 25}

        return probe

    child.add_node("seed", seed).add_node("a", make("a")).add_node("b", make("b")).add_node("merge", seed)
    child.set_entry("seed").add_fanout("seed", ["a", "b"], "merge").add_edge("merge", END)

    parent = ConcurrentGraph(ParentState)

    async def intake(s: ParentState) -> dict[str, Any]:
        return {}

    async def write(s: ParentState) -> dict[str, Any]:
        return {"report": f"REPORT[{s.research_note}]"}

    parent.add_node("intake", intake).add_node("write", write)
    parent.add_subgraph("research", child.compile_concurrent(), input_map=lambda p: ChildState(topic=p.question),
                        output_map=lambda c: {"research_note": f"{len(c.findings)} findings, {c.tokens} tokens"})
    parent.set_entry("intake").add_edge("intake", "research").add_edge("research", "write").add_edge("write", END)
    return parent.compile_concurrent()


class CorpusState(BaseModel):
    docs: list[str]
    summaries: list[str] = Field(default_factory=list)  # reducer: append
    words: int = 0  # reducer: add
    longest: int = 0  # reducer: max
    report: str = ""


class FlakySummariser:
    """Simulated I/O (LLM/API) that fails once for one document; retried by with_backoff inside the map worker."""

    def __init__(self) -> None:
        self.failed_once = False
        self.in_flight = 0
        self.peak = 0

    async def summarise(self, doc: str) -> str:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            if "flaky" in doc and not self.failed_once:
                self.failed_once = True
                raise ToolTransientError("summariser 503")
            return " ".join(doc.split()[:3]) + "..."
        finally:
            self.in_flight -= 1


def build_map_reduce_graph(svc: FlakySummariser, max_concurrency: int = 2) -> CompiledConcurrent:
    g = ConcurrentGraph(CorpusState, {"summaries": reduce_append, "words": reduce_add, "longest": reduce_max})

    async def split(s: CorpusState) -> dict[str, Any]:
        return {}

    async def summarise_one(s: CorpusState, doc: str) -> dict[str, Any]:
        text = await with_backoff(partial(svc.summarise, doc))
        n = len(doc.split())
        return {"summaries": [text], "words": n, "longest": n}

    async def reduce(s: CorpusState) -> dict[str, Any]:
        return {"report": f"{len(s.summaries)} docs, {s.words} words, longest {s.longest}: " + "; ".join(sorted(s.summaries))}

    g.add_node("split", split).add_node("reduce", reduce).set_entry("split")
    g.add_map("split", "summarise", items=lambda s: s.docs, worker=summarise_one, reduce_node="reduce", max_concurrency=max_concurrency)
    g.add_edge("reduce", END)
    return g.compile_concurrent()


def build_invalid_graph() -> ConcurrentGraph:
    g = ConcurrentGraph(TicketState)

    async def noop(s: TicketState) -> dict[str, Any]:
        return {}

    for n in ("a", "b", "c", "dangling", "orphan"):
        g.add_node(n, noop)
    g.set_entry("a").add_conditional_edges("a", lambda s: "b", targets=["b", "dangling"])
    g.add_edge("b", "c").add_edge("c", "b").add_edge("orphan", END)
    return g


# ---- Syllabus extension: lazy LangGraph adapter (in addition to the offline engine) ---
class ResearchLG(TypedDict):
    topic: str
    findings: Annotated[list[str], operator.add]  # LangGraph's reducer syntax: Annotated[type, reducer]
    summary: str


def build_langgraph_app(branch_findings: dict[str, str]) -> Any:
    """Same shape as build_research_graph, expressed with the real LangGraph API (imported lazily)."""
    try:
        from langgraph.graph import END as LG_END, START as LG_START, StateGraph as LGStateGraph
    except ImportError as exc:
        raise LangGraphNotInstalledError("langgraph is not installed; run: pip install langgraph") from exc

    def make_branch(name: str, finding: str) -> Callable[[ResearchLG], Awaitable[dict[str, Any]]]:
        async def branch(state: ResearchLG) -> dict[str, Any]:
            return {"findings": [f"{name}: {finding}"]}

        return branch

    async def plan(state: ResearchLG) -> dict[str, Any]:
        return {"topic": state["topic"]}

    async def synthesize(state: ResearchLG) -> dict[str, Any]:
        return {"summary": " | ".join(sorted(state["findings"]))}

    builder = LGStateGraph(ResearchLG)
    builder.add_node("plan", plan)
    builder.add_node("synthesize", synthesize)
    for name, finding in branch_findings.items():
        builder.add_node(name, make_branch(name, finding))
        builder.add_edge("plan", name)
    builder.add_edge(LG_START, "plan")
    builder.add_edge(list(branch_findings), "synthesize")  # list of sources = wait for ALL branches (join)
    builder.add_conditional_edges("synthesize", lambda state: "done", {"done": LG_END})
    return builder.compile()


async def run_langgraph_demo() -> None:
    try:
        app = build_langgraph_app({"web": "pricing page", "docs": "api limits"})
    except LangGraphNotInstalledError as exc:
        log.info("langgraph adapter skipped", extra={"ctx": {"reason": str(exc)}})
        return
    out = await app.ainvoke({"topic": "pricing", "findings": [], "summary": ""})
    log.info("langgraph adapter result", extra={"ctx": {"summary": out["summary"]}})


async def extension_demo() -> None:
    # 1 defining agency: same two inputs through a fixed chain and through an agent
    tools: dict[str, ToolFn] = {"find_order": find_order, "get_eta": get_eta}
    for label, known in (("has order id", {"order_id": "1042"}), ("email only", {"email": "ana@example.com"}),
                         ("unknown email", {"email": "zed@example.com"})):
        agent = OrderAgent("tell the customer the delivery ETA", known, tools)
        log.info("agency", extra={"ctx": {"case": label, "fixed_chain": await fixed_chain(tools, known),
                                          "agent": await run_agent(agent), "agent_tool_calls": [a.tool for a in agent.memory.actions if a.tool]}})

    # 2 ReAct: scripted model, tool registry, step guard
    react_tools: dict[str, ToolFn] = {"price": tool_price, "calc": tool_calc}
    llm = ScriptedLLM(["Thought: I need the unit price\nAction: price[widget]",
                       "Thought: now multiply by 3\nAction: calc[12*3]",
                       "Thought: I have the total\nFinal Answer: 36 dollars"])
    answer, steps = await react_loop(llm, react_tools, "What do 3 widgets cost?")
    log.info("react", extra={"ctx": {"answer": answer, "trace": [f"{s.action}[{s.action_input}]={s.observation}" for s in steps],
                                     "prompt_chars_last": len(llm.prompts[-1])}})
    bad = ScriptedLLM(["Thought: try a tool\nAction: teleport[home]", "just chatting", "Thought: again\nAction: calc[2+]"] * 3)
    try:
        await react_loop(bad, react_tools, "loop forever", max_steps=5)
    except AgentLoopError as exc:
        log.info("react guard", extra={"ctx": {"error": str(exc), "observations": [p.splitlines()[-1] for p in bad.prompts[1:4]]}})

    # 3 Plan-and-Execute with a replan after a permanent tool failure
    pe_tools: dict[str, ToolFn] = {"primary_db": tool_primary_db, "warehouse_export": tool_warehouse_export,
                                   "usd_to_eur": tool_usd_to_eur, "format_report": tool_format_report}
    result, trace = await plan_and_execute("Q3 revenue in EUR", revenue_planner, pe_tools)
    log.info("plan-and-execute", extra={"ctx": {"result": result, "trace": trace}})
    try:
        await plan_and_execute("Q3 revenue in EUR", revenue_planner, {**pe_tools, "warehouse_export": tool_primary_db}, max_replans=1)
    except PlanFailedError as exc:
        log.info("replan budget exhausted", extra={"ctx": {"error": str(exc)}})

    # 4 router vs supervisor
    router = build_router_graph()
    for q in ("I was charged twice on my invoice", "the export job hits a timeout", "do you have a partner program?"):
        out = await router.ainvoke(TicketState(question=q), thread="router")
        log.info("router", extra={"ctx": {"question": q, "intent": out.intent, "trail": out.trail}})
    sup = await build_supervisor_graph().ainvoke(SupState(task="write brief"), thread="supervisor")
    log.info("supervisor", extra={"ctx": {"notes": sup.notes, "drafts": sup.drafts, "reviews": sup.reviews}})

    # 5/9 concurrent fan-out with reducers, timing, and per-node timeout
    t0 = time.perf_counter()
    done = await build_research_graph().ainvoke(ResearchState(topic="churn"), thread="fanout")
    elapsed = time.perf_counter() - t0
    log.info("fan-out", extra={"ctx": {"elapsed_ms": round(elapsed * 1000), "serial_would_be_ms": 120, "faster_than_serial": elapsed < 0.12,
                                       "tokens": done.tokens, "confidence": done.confidence, "status": done.status.model_dump(),
                                       "findings": sorted(done.findings)}})
    try:
        await build_research_graph(crm_delay_s=0.5, crm_timeout_s=0.05).ainvoke(ResearchState(topic="churn"), thread="slow")
    except NodeTimeoutError as exc:
        log.info("node timeout", extra={"ctx": {"node": exc.node, "timeout_s": exc.timeout_s}})
    sem = asyncio.Semaphore(2)

    async def one_run(topic: str) -> str:
        async with sem:  # independent whole-graph runs are concurrent too, bounded by a semaphore
            return (await build_research_graph().ainvoke(ResearchState(topic=topic), thread=f"run-{topic}")).summary

    summaries = await asyncio.gather(*(one_run(t) for t in ("alpha", "beta", "gamma")))
    log.info("concurrent runs", extra={"ctx": {"count": len(summaries)}})

    # 12 state conflicts
    try:
        await build_research_graph(conflict=True).ainvoke(ResearchState(topic="churn"), thread="conflict")
    except StateConflictError as exc:
        log.info("state conflict", extra={"ctx": {"field": exc.field, "writers": sorted(exc.writers)}})
    log.info("lww order independent", extra={"ctx": {"same": reduce_lww({"version": 1, "writer": "a"}, {"version": 3, "writer": "b"})
                                                    == reduce_lww({"version": 3, "writer": "b"}, {"version": 1, "writer": "a"})}})

    # 10 nested parent-child graphs
    nested = await build_parent_child_graph().ainvoke(ParentState(question="pricing"), thread="nested")
    log.info("nested graphs", extra={"ctx": {"report": nested.report}})

    # 11 map-reduce
    svc = FlakySummariser()
    docs = ["alpha beta gamma delta", "flaky doc that fails once", "short one", "the fourth document is longest of all", "tiny"]
    mr = await build_map_reduce_graph(svc).ainvoke(CorpusState(docs=docs), thread="mapreduce")
    log.info("map-reduce", extra={"ctx": {"report": mr.report, "peak_concurrency": svc.peak, "limit": 2}})

    # 8 compile-time validation
    try:
        build_invalid_graph().compile_concurrent()
    except GraphValidationError as exc:
        log.info("compile validation", extra={"ctx": {"problems": exc.problems}})

    await run_langgraph_demo()


async def main() -> None:
    cp = FileCheckpointer(Path(tempfile.mkdtemp(prefix="lab09-")))
    llm, tool = FakeLLM(), RefundTool()

    thread = "t-100"
    graph = build_graph(llm, tool, thread).compile(cp, crash_after="critic")
    try:
        await graph.invoke(thread, AgentState(task="summarise Q3 churn"))
    except NodeCrash as exc:
        saved = await cp.get(thread)
        log.warning("crash", extra={"ctx": {"error": str(exc), "resume_at": saved.next_node if saved else None}})

    try:
        await graph.invoke(thread)  # new process would build the same graph and call invoke again
    except Interrupted as exc:
        log.info("paused for human", extra={"ctx": {"reason": exc.reason}})

    final = await graph.invoke(thread, resume_update={"approved": True})
    log.info("final", extra={"ctx": {"draft": final.draft, "revisions": final.revisions, "tokens": final.tokens_used,
                                      "tool_log": final.tool_log, "refunds_issued": final.refunds_issued}})

    # idempotency: replaying the refund node cannot double-pay
    again = await build_graph(llm, tool, thread).nodes["refund"](final)
    log.info("replayed refund", extra={"ctx": {"refunds_total": len(tool.done), "receipt": again["tool_log"]}})

    loop = StateGraph(recursion_limit=5)

    async def spin(s: AgentState) -> dict[str, Any]:
        return {"revisions": s.revisions + 1}

    loop.add_node("spin", spin).set_entry("spin").add_edge("spin", "spin")
    try:
        await loop.compile(cp).invoke("t-loop", AgentState(task="x"))
    except GraphRecursionError as exc:
        log.info("recursion guard", extra={"ctx": {"error": str(exc)}})

    await extension_demo()


if __name__ == "__main__":
    asyncio.run(main())
