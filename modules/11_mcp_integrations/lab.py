"""Module 11 lab: an MCP-style JSON-RPC 2.0 tool server fronting legacy systems.

Adapters: a real read-only SQLite "ERP" database (parameterized, allowlisted), a SOAP/XML service (envelope
build + parse + fault mapping), and a Jira-like REST API that rate-limits with Retry-After. The server adds
schema validation, scope-based authorization, per-backend circuit breakers, retries with jittered backoff and
structured, model-legible errors. Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from enum import Enum
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field, ValidationError


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


log = get_logger("lab11")


class ToolError(Exception):
    code = -32000
    retryable = False

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class BackendUnavailable(ToolError):
    retryable = True


class RateLimited(ToolError):
    retryable = True


class SoapFault(ToolError):
    pass


class Forbidden(ToolError):
    code = -32003


class InvalidParams(ToolError):
    code = -32602


class CircuitOpen(ToolError):
    retryable = True


class CircuitBreaker:
    class State(str, Enum):
        CLOSED = "closed"
        OPEN = "open"
        HALF_OPEN = "half_open"

    def __init__(self, threshold: int = 3, reset_s: float = 0.2) -> None:
        self.threshold, self.reset_s = threshold, reset_s
        self.failures, self.opened_at, self.state = 0, 0.0, self.State.CLOSED

    def before(self) -> None:
        if self.state is self.State.OPEN:
            if time.monotonic() - self.opened_at >= self.reset_s:
                self.state = self.State.HALF_OPEN
            else:
                raise CircuitOpen("backend circuit open", retry_after_s=round(self.reset_s - (time.monotonic() - self.opened_at), 3))

    def success(self) -> None:
        self.failures, self.state = 0, self.State.CLOSED

    def failure(self) -> None:
        self.failures += 1
        if self.state is self.State.HALF_OPEN or self.failures >= self.threshold:
            self.state, self.opened_at = self.State.OPEN, time.monotonic()


# ---------------- adapters -------------------------------------------------------------------------
class SqlAdapter:
    """Read-only access to an allowlisted view. Values are always bound parameters."""

    ALLOWED_COLUMNS = {"order_id", "supplier", "status", "amount_cents"}

    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:", check_same_thread=False)  # used via to_thread; serialized by the lock below
        self._lock = threading.Lock()
        self.db.execute("CREATE TABLE purchase_orders (order_id INTEGER PRIMARY KEY, supplier TEXT, status TEXT, amount_cents INTEGER)")
        self.db.executemany("INSERT INTO purchase_orders VALUES (?,?,?,?)",
                            [(1001, "Acme", "OPEN", 125000), (1002, "Borealis", "CLOSED", 9900), (1003, "Acme", "OPEN", 480000)])
        self.db.commit()
        self.db.execute("PRAGMA query_only = ON")  # belt and braces: connection cannot write

    async def query_orders(self, supplier: str | None, status: str | None, limit: int) -> list[dict[str, Any]]:
        clauses, params = [], []
        for col, val in (("supplier", supplier), ("status", status)):
            if val is not None:
                clauses.append(f"{col} = ?")  # column names come from code, never from the caller
                params.append(val)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT order_id, supplier, status, amount_cents FROM purchase_orders {where} ORDER BY order_id LIMIT ?"
        def run() -> list[tuple[Any, ...]]:
            with self._lock:
                return self.db.execute(sql, [*params, limit]).fetchall()

        rows = await asyncio.to_thread(run)
        return [dict(zip(("order_id", "supplier", "status", "amount_cents"), r)) for r in rows]


class SoapAdapter:
    NS = {"s": "http://schemas.xmlsoap.org/soap/envelope/", "e": "urn:erp:inventory"}

    def __init__(self) -> None:
        self.calls, self.fail_first = 0, 2

    @staticmethod
    def build_envelope(sku: str) -> str:
        ET.register_namespace("s", SoapAdapter.NS["s"])
        env = ET.Element(f"{{{SoapAdapter.NS['s']}}}Envelope")
        body = ET.SubElement(env, f"{{{SoapAdapter.NS['s']}}}Body")
        req = ET.SubElement(body, "{urn:erp:inventory}GetStockLevel")
        ET.SubElement(req, "{urn:erp:inventory}Sku").text = sku  # ElementTree escapes content: no XML injection
        return ET.tostring(env, encoding="unicode")

    async def _transport(self, xml: str) -> str:
        """Simulated endpoint: flaky first calls, fault for unknown SKU, Latin-1 style data normalized upstream."""
        self.calls += 1
        await asyncio.sleep(0.002)
        if self.calls <= self.fail_first:
            raise BackendUnavailable("ERP gateway timeout")
        sku = ET.fromstring(xml).find(".//e:Sku", self.NS).text  # type: ignore[union-attr]
        s = self.NS["s"]
        if sku != "WIDGET-X":
            return (f'<s:Envelope xmlns:s="{s}"><s:Body><s:Fault><faultcode>Client</faultcode>'
                    f"<faultstring>Unknown SKU</faultstring></s:Fault></s:Body></s:Envelope>")
        return (f'<s:Envelope xmlns:s="{s}"><s:Body><e:GetStockLevelResponse xmlns:e="urn:erp:inventory">'
                f"<e:OnHand>42</e:OnHand><e:AsOf>2025-03-04T23:30:00+01:00</e:AsOf></e:GetStockLevelResponse></s:Body></s:Envelope>")

    async def stock_level(self, sku: str) -> dict[str, Any]:
        root = ET.fromstring(await self._transport(self.build_envelope(sku)))
        fault = root.find(".//s:Fault", self.NS)
        if fault is not None:
            raise SoapFault(fault.findtext("faultstring") or "SOAP fault")
        return {"sku": sku, "on_hand": int(root.findtext(".//e:OnHand", namespaces=self.NS)),  # type: ignore[arg-type]
                "as_of": root.findtext(".//e:AsOf", namespaces=self.NS)}


class JiraAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.issues = {"OPS-1": {"key": "OPS-1", "summary": "Pump failure", "status": "Open"}}

    async def get_issue(self, key: str) -> dict[str, Any]:
        self.calls += 1
        await asyncio.sleep(0.001)
        if self.calls == 1:
            raise RateLimited("429 Too Many Requests", retry_after_s=0.01)
        if key not in self.issues:
            raise ToolError(f"issue {key} not found")
        return self.issues[key]


# ---------------- MCP server -----------------------------------------------------------------------
class Tool(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    scopes: set[str]
    backend: str


class QueryOrdersArgs(BaseModel):
    supplier: str | None = Field(None, max_length=64)
    status: str | None = Field(None, pattern="^(OPEN|CLOSED)$")
    limit: int = Field(10, ge=1, le=50)


class StockArgs(BaseModel):
    sku: str = Field(pattern=r"^[A-Z0-9\-]{3,20}$")


class IssueArgs(BaseModel):
    key: str = Field(pattern=r"^[A-Z]{2,10}-\d{1,6}$")


class McpServer:
    def __init__(self) -> None:
        self.sql, self.soap, self.jira = SqlAdapter(), SoapAdapter(), JiraAdapter()
        self.breakers = {b: CircuitBreaker() for b in ("sql", "soap", "jira")}
        self._handlers: dict[str, tuple[Tool, type[BaseModel], Callable[[Any], Awaitable[Any]]]] = {}
        self._register(Tool(name="query_purchase_orders", description="List purchase orders, optionally filtered by supplier and status.",
                            input_schema=QueryOrdersArgs.model_json_schema(), scopes={"erp:read"}, backend="sql"),
                       QueryOrdersArgs, lambda a: self.sql.query_orders(a.supplier, a.status, a.limit))
        self._register(Tool(name="get_stock_level", description="Current on-hand stock for a SKU from the inventory ERP.",
                            input_schema=StockArgs.model_json_schema(), scopes={"erp:read"}, backend="soap"),
                       StockArgs, lambda a: self.soap.stock_level(a.sku))
        self._register(Tool(name="get_jira_issue", description="Fetch a Jira issue by key.",
                            input_schema=IssueArgs.model_json_schema(), scopes={"jira:read"}, backend="jira"),
                       IssueArgs, lambda a: self.jira.get_issue(a.key))

    def _register(self, tool: Tool, model: type[BaseModel], fn: Callable[[Any], Awaitable[Any]]) -> None:
        self._handlers[tool.name] = (tool, model, fn)

    async def handle(self, request: dict[str, Any], caller_scopes: set[str]) -> dict[str, Any]:
        rid, method, params = request.get("id"), request.get("method"), request.get("params") or {}
        try:
            if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
                return self._err(rid, -32600, "invalid request")
            if method == "initialize":
                return self._ok(rid, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "lab11", "version": "1.0"}})
            if method == "tools/list":
                tools = [t.model_dump(mode="json", exclude={"scopes", "backend"}) for t, _, _ in self._handlers.values() if t.scopes <= caller_scopes]
                return self._ok(rid, {"tools": tools})
            if method == "tools/call":
                return self._ok(rid, await self._call(params, caller_scopes))
            return self._err(rid, -32601, f"method {method} not found")
        except ToolError as exc:  # protocol-level problems (authz, params)
            return self._err(rid, exc.code, str(exc))

    async def _call(self, params: dict[str, Any], scopes: set[str]) -> dict[str, Any]:
        name = params.get("name", "")
        if name not in self._handlers:
            raise InvalidParams(f"unknown tool {name!r}")
        tool, model, fn = self._handlers[name]
        if not tool.scopes <= scopes:
            raise Forbidden(f"missing scopes: {sorted(tool.scopes - scopes)}")
        try:
            args = model.model_validate(params.get("arguments") or {})
        except ValidationError as exc:
            raise InvalidParams("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())) from exc
        breaker = self.breakers[tool.backend]
        try:
            breaker.before()
            data = await self._with_retry(lambda: fn(args), breaker)
            return {"isError": False, "structuredContent": data, "content": [{"type": "text", "text": json.dumps(data)}]}
        except ToolError as exc:  # execution errors are returned to the model, not raised as protocol errors
            err = {"message": str(exc), "retryable": exc.retryable, "retry_after_s": exc.retry_after_s}
            return {"isError": True, "structuredContent": err, "content": [{"type": "text", "text": json.dumps(err)}]}

    async def _with_retry(self, fn: Callable[[], Awaitable[Any]], breaker: CircuitBreaker, attempts: int = 4) -> Any:
        for i in range(1, attempts + 1):
            try:
                breaker.before()  # stop retrying the moment the breaker opens mid-loop
                result = await asyncio.wait_for(fn(), 1.0)
                breaker.success()
                return result
            except (BackendUnavailable, RateLimited, asyncio.TimeoutError) as exc:
                breaker.failure() if isinstance(exc, (BackendUnavailable, asyncio.TimeoutError)) else None
                if i == attempts:
                    raise BackendUnavailable(f"backend failed after {attempts} attempts") from exc
                wait = exc.retry_after_s if isinstance(exc, RateLimited) and exc.retry_after_s else random.uniform(0, 0.004 * 2 ** i)
                await asyncio.sleep(wait or 0)

    @staticmethod
    def _ok(rid: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    @staticmethod
    def _err(rid: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


async def main() -> None:
    server = McpServer()
    scopes = {"erp:read", "jira:read"}
    rpc = lambda i, m, p=None, s=scopes: server.handle({"jsonrpc": "2.0", "id": i, "method": m, "params": p}, s)  # noqa: E731

    log.info("initialize", extra={"ctx": (await rpc(1, "initialize"))["result"]})
    listed = (await rpc(2, "tools/list"))["result"]["tools"]
    log.info("tools", extra={"ctx": {"names": [t["name"] for t in listed]}})
    log.info("restricted caller sees", extra={"ctx": {"names": [t["name"] for t in (await rpc(3, "tools/list", s={"jira:read"}))["result"]["tools"]]}})

    calls = [("query_purchase_orders", {"supplier": "Acme", "status": "OPEN"}),
             ("query_purchase_orders", {"supplier": "x' OR '1'='1"}),  # injection string is just a bound value
             ("query_purchase_orders", {"status": "OPEN; DROP TABLE purchase_orders"}),
             ("get_stock_level", {"sku": "WIDGET-X"}),
             ("get_stock_level", {"sku": "NOPE-1"}),
             ("get_jira_issue", {"key": "OPS-1"}),
             ("drop_everything", {})]
    for i, (name, args) in enumerate(calls, 10):
        resp = await rpc(i, "tools/call", {"name": name, "arguments": args})
        body = resp.get("result", {}).get("structuredContent") if "result" in resp else resp["error"]
        log.info("call", extra={"ctx": {"tool": name, "args": args, "isError": resp.get("result", {}).get("isError"), "body": body}})

    denied = await rpc(30, "tools/call", {"name": "get_jira_issue", "arguments": {"key": "OPS-1"}}, {"erp:read"})
    log.info("authz", extra={"ctx": denied["error"]})

    server.soap.fail_first, server.soap.calls = 10**6, 0  # backend outage: breaker should trip and shield it
    for i in range(4):
        r = await rpc(40 + i, "tools/call", {"name": "get_stock_level", "arguments": {"sku": "WIDGET-X"}})
        log.info("outage", extra={"ctx": {"attempt": i, "breaker": server.breakers["soap"].state, "backend_calls": server.soap.calls,
                                          "error": r["result"]["structuredContent"]["message"]}})


if __name__ == "__main__":
    asyncio.run(main())
