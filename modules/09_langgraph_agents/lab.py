"""Module 09 lab: a LangGraph-semantics state machine built from scratch.

Features: typed Pydantic state with per-field reducers, nodes as async functions returning partial updates,
conditional edges, cycles with a recursion limit, durable checkpoints after every node, resume after a crash,
human-approval interrupts, idempotent side-effecting tools, backoff retries. Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable

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


if __name__ == "__main__":
    asyncio.run(main())
