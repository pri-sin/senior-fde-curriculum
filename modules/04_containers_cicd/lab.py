"""Module 04 lab: Dockerfile security linter, CI/CD promotion-gate engine and container/CI toolkit.

Policy-as-code: parses a Dockerfile into instructions, checks hardening rules (digest-pinned base,
multi-stage, non-root USER, no secrets in ENV/ARG, no ADD from URLs, HEALTHCHECK), then runs an async
pipeline of gates (with retries on flaky scanners) and decides whether an image may be promoted.
Run: python3 lab.py
"""
from __future__ import annotations

import ast
import asyncio
import fnmatch
import json
import logging
import math
import random
import re
import shlex
import sys
import time
from enum import Enum
from typing import Any, Awaitable, Callable, Literal, TypeVar

from pydantic import BaseModel, Field, model_validator


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


log = get_logger("lab04")


class PipelineError(Exception):
    """Base error for pipeline failures."""


class DockerfileParseError(PipelineError):
    pass


class ScannerUnavailableError(PipelineError):
    """Retryable: the vulnerability DB or scanner endpoint timed out."""


class GateFailedError(PipelineError):
    def __init__(self, gate: str, detail: str) -> None:
        super().__init__(f"{gate}: {detail}")
        self.gate, self.detail = gate, detail


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Instruction(BaseModel):
    line: int
    op: str
    arg: str


class Finding(BaseModel):
    rule: str
    severity: Severity
    line: int | None = None
    message: str


def parse_dockerfile(text: str) -> list[Instruction]:
    """Handles comments, line continuations and case-insensitive instructions."""
    out: list[Instruction] = []
    buf, start = "", 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not buf:
            start = n
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        parts = buf.split(None, 1)
        if not parts:
            raise DockerfileParseError(f"empty instruction at line {start}")
        out.append(Instruction(line=start, op=parts[0].upper(), arg=parts[1] if len(parts) > 1 else ""))
        buf = ""
    if buf:
        raise DockerfileParseError("dangling line continuation")
    if not any(i.op == "FROM" for i in out):
        raise DockerfileParseError("no FROM instruction")
    return out


SECRET_NAME = re.compile(r"(PASSWORD|SECRET|TOKEN|API_?KEY|PRIVATE_KEY)", re.I)


def lint_dockerfile(ins: list[Instruction]) -> list[Finding]:
    f: list[Finding] = []
    froms = [i for i in ins if i.op == "FROM"]
    for i in froms:
        image = i.arg.split()[0]
        if image == "scratch" or image in {s.arg.split()[-1] for s in froms if " AS " in s.arg.upper()}:
            continue
        if "@sha256:" not in image:
            sev = Severity.HIGH if image.endswith(":latest") or ":" not in image else Severity.MEDIUM
            f.append(Finding(rule="pin-base-digest", severity=sev, line=i.line, message=f"{image} is not pinned by digest"))
    if len(froms) < 2:
        f.append(Finding(rule="multi-stage", severity=Severity.MEDIUM, message="single-stage build ships build tooling"))
    users = [i for i in ins if i.op == "USER"]
    if not users or users[-1].arg.strip().split(":")[0] in {"root", "0"}:
        f.append(Finding(rule="non-root", severity=Severity.CRITICAL, line=users[-1].line if users else None,
                         message="final stage runs as root"))
    for i in ins:
        if i.op in {"ENV", "ARG"} and SECRET_NAME.search(i.arg.split("=")[0]) and "=" in i.arg and i.arg.split("=", 1)[1].strip():
            f.append(Finding(rule="no-secrets", severity=Severity.CRITICAL, line=i.line, message="secret value baked into image metadata"))
        if i.op == "ADD" and re.match(r"https?://", i.arg.split()[0]):
            f.append(Finding(rule="no-remote-add", severity=Severity.HIGH, line=i.line, message="ADD from URL is unverifiable; use curl with checksum"))
        if i.op == "RUN" and re.search(r"curl[^|]*\|\s*(sh|bash)", i.arg):
            f.append(Finding(rule="no-curl-pipe-sh", severity=Severity.HIGH, line=i.line, message="piping remote script to shell"))
        if i.op == "RUN" and "apt-get install" in i.arg and "--no-install-recommends" not in i.arg:
            f.append(Finding(rule="apt-minimal", severity=Severity.LOW, line=i.line, message="use --no-install-recommends and clean lists"))
    if not any(i.op == "HEALTHCHECK" for i in ins):
        f.append(Finding(rule="healthcheck", severity=Severity.LOW, message="no HEALTHCHECK (orchestrator probe may cover it)"))
    return f


class ImageScan(BaseModel):
    image: str
    vulns: dict[Severity, int]
    fixable_critical: int = 0


class PipelineContext(BaseModel):
    commit: str = Field(min_length=7)
    dockerfile: str
    image: str
    coverage_pct: float = Field(ge=0, le=100)
    findings: list[Finding] = Field(default_factory=list)
    scan: ImageScan | None = None
    signed: bool = False
    timings_ms: dict[str, float] = Field(default_factory=dict)


Gate = Callable[[PipelineContext], Awaitable[None]]


async def retry(fn: Callable[[], Awaitable[ImageScan]], attempts: int = 4, base: float = 0.02) -> ImageScan:
    for i in range(1, attempts + 1):
        try:
            return await fn()
        except ScannerUnavailableError:
            if i == attempts:
                raise
            await asyncio.sleep(random.uniform(0, base * 2 ** i))
    raise AssertionError("unreachable")


class FakeScanner:
    def __init__(self, vulns: dict[Severity, int], fixable_critical: int, fail_first: int = 2) -> None:
        self._vulns, self._fixable, self._fail_first, self.calls = vulns, fixable_critical, fail_first, 0

    async def scan(self, image: str) -> ImageScan:
        self.calls += 1
        await asyncio.sleep(0.01)
        if self.calls <= self._fail_first:
            raise ScannerUnavailableError("vuln DB download timed out")
        return ImageScan(image=image, vulns=self._vulns, fixable_critical=self._fixable)


async def gate_coverage(ctx: PipelineContext) -> None:
    if ctx.coverage_pct < 80:
        raise GateFailedError("coverage", f"{ctx.coverage_pct}% < 80%")


async def gate_dockerfile(ctx: PipelineContext) -> None:
    ctx.findings = lint_dockerfile(parse_dockerfile(ctx.dockerfile))
    blocking = [x for x in ctx.findings if x.severity in {Severity.CRITICAL, Severity.HIGH}]
    if blocking:
        raise GateFailedError("dockerfile-policy", "; ".join(f"{b.rule}@{b.line}" for b in blocking))


def make_scan_gate(scanner: FakeScanner) -> Gate:
    async def gate_scan(ctx: PipelineContext) -> None:
        ctx.scan = await retry(lambda: scanner.scan(ctx.image))
        if ctx.scan.fixable_critical > 0:
            raise GateFailedError("cve-scan", f"{ctx.scan.fixable_critical} fixable CRITICAL vulnerabilities")
    return gate_scan


async def gate_sign(ctx: PipelineContext) -> None:
    if "@sha256:" not in ctx.image:
        raise GateFailedError("sign", "refusing to sign a mutable tag; push by digest")
    ctx.signed = True


async def run_pipeline(ctx: PipelineContext, gates: list[tuple[str, Gate]]) -> bool:
    for name, gate in gates:
        t0 = time.perf_counter()
        try:
            await gate(ctx)
            ctx.timings_ms[name] = round((time.perf_counter() - t0) * 1000, 1)
            log.info("gate passed", extra={"ctx": {"gate": name, "commit": ctx.commit}})
        except GateFailedError as exc:
            log.error("gate failed", extra={"ctx": {"gate": exc.gate, "detail": exc.detail, "commit": ctx.commit}})
            return False
        except PipelineError as exc:
            log.error("infrastructure error", extra={"ctx": {"gate": name, "error": str(exc)}})
            return False
    return True


BAD = """FROM python:latest
ENV API_KEY=sk-live-123
ADD https://example.com/tool.tgz /opt/
RUN apt-get install -y gcc && curl https://x.sh | sh
COPY . /app
CMD ["python", "/app/main.py"]
"""

GOOD = """# builder
FROM python:3.12-slim@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa AS builder
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt
# runtime
FROM gcr.io/distroless/python3-debian12@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
COPY --from=builder /install /usr/local
COPY app /app
USER 65532:65532
HEALTHCHECK CMD ["python","/app/health.py"]
ENTRYPOINT ["python", "/app/main.py"]
"""


# ---- Syllabus extension: shared helpers ---
# Everything below runs offline with pydantic + stdlib. boto3 / fastapi appear only inside lazily imported
# adapters, IN ADDITION to the offline logic that extension_demo() executes.

T = TypeVar("T")
THROTTLE_CODES = {"Throttling", "ThrottlingException", "RequestLimitExceeded", "TooManyRequestsException", "ServerException"}


class OptionalDependencyError(PipelineError):
    """A lazily imported adapter library (boto3, fastapi) is not installed."""


class YamlError(PipelineError):
    pass


class ComposeError(PipelineError):
    pass


class DeploymentConfigError(PipelineError):
    pass


class TransientApiError(PipelineError):
    """Retryable provider/API failure (throttling, 5xx)."""


def has_blocking(findings: list[Finding]) -> bool:
    return any(f.severity in {Severity.CRITICAL, Severity.HIGH} for f in findings)


async def backoff_call(fn: Callable[[], Awaitable[T]], *, retry_on: tuple[type[Exception], ...] = (TransientApiError,),
                       attempts: int = 4, base: float = 0.02) -> T:
    """Exponential backoff with full jitter around any I/O callable."""
    for i in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on:
            if i == attempts:
                raise
            await asyncio.sleep(random.uniform(0, base * 2 ** i))
    raise AssertionError("unreachable")


def lazy_boto3_client(service: str, region: str = "us-east-1") -> Any:
    try:
        import boto3  # noqa: PLC0415
    except ImportError as exc:
        raise OptionalDependencyError("boto3 is not installed; run: pip install boto3") from exc
    return boto3.client(service, region_name=region)


async def call_aws(method: Callable[..., Any], **kwargs: Any) -> Any:
    """Blocking boto3 call in a thread; throttling codes become TransientApiError so backoff_call retries them."""
    def _run() -> Any:
        try:
            return method(**kwargs)
        except Exception as exc:  # botocore ClientError carries .response
            if getattr(exc, "response", {}).get("Error", {}).get("Code", "") in THROTTLE_CODES:
                raise TransientApiError(str(exc)) from exc
            raise
    return await backoff_call(lambda: asyncio.to_thread(_run))


# ---- Syllabus extension: writing optimized Dockerfiles ---

def copy_sources(i: Instruction) -> list[str]:
    """Source paths of a COPY/ADD (flags and destination removed; supports the JSON array form)."""
    arg = i.arg.strip()
    toks = json.loads(arg) if arg.startswith("[") else arg.split()
    toks = [t for t in toks if not t.startswith("--")]
    return toks[:-1]


DEP_INSTALL = re.compile(r"(pip install|pip3 install|npm ci|npm install|yarn install|poetry install|go mod download)")


def optimization_findings(ins: list[Instruction], has_dockerignore: bool) -> list[Finding]:
    """Layer-cache and image-size hygiene (complements lint_dockerfile, which covers security)."""
    f: list[Finding] = []
    ctx_copy = next((i for i in ins if i.op == "COPY" and "--from" not in i.arg and copy_sources(i) in (["."], ["./"])), None)
    install = next((i for i in ins if i.op == "RUN" and DEP_INSTALL.search(i.arg)), None)
    if ctx_copy and install and ctx_copy.line < install.line:
        f.append(Finding(rule="cache-order", severity=Severity.MEDIUM, line=ctx_copy.line,
                         message="COPY of the whole context precedes dependency install: every code edit re-installs dependencies"))
    for i in ins:
        if i.op == "RUN" and re.search(r"pip3? install", i.arg) and "--no-cache-dir" not in i.arg:
            f.append(Finding(rule="pip-cache", severity=Severity.LOW, line=i.line, message="pip install without --no-cache-dir bloats the layer"))
        if i.op == "RUN" and "apt-get install" in i.arg and "/var/lib/apt/lists" not in i.arg:
            f.append(Finding(rule="apt-lists", severity=Severity.LOW, line=i.line, message="remove /var/lib/apt/lists in the same RUN"))
    if not has_dockerignore:
        f.append(Finding(rule="dockerignore", severity=Severity.MEDIUM, message="no .dockerignore: .git, .env and node_modules enter the build context"))
    if not any(i.op == "WORKDIR" for i in ins):
        f.append(Finding(rule="workdir", severity=Severity.LOW, message="no WORKDIR: files land in / and relative paths are ambiguous"))
    return f


def _touches(src: str, changed: str) -> bool:
    s = src.rstrip("/")
    return s in (".", "") or changed == s or changed.startswith(s + "/") or fnmatch.fnmatchcase(changed, s)


def simulate_build_cache(ins: list[Instruction], changed_files: list[str]) -> list[str]:
    """Which instructions re-execute after `changed_files` changed? Docker invalidates a layer and every layer after it."""
    rebuilt: list[str] = []
    dirty = upstream = False
    for i in ins:
        if i.op == "FROM":
            upstream, dirty = upstream or dirty, False
            continue
        if i.op in {"COPY", "ADD"} and "--from" not in i.arg and any(_touches(s, c) for s in copy_sources(i) for c in changed_files):
            dirty = True
        if i.op == "COPY" and "--from" in i.arg and upstream:
            dirty = True
        if dirty:
            rebuilt.append("%d:%s" % (i.line, i.op))
    return rebuilt


# ---- Syllabus extension: minimal YAML subset (parser + emitter, no PyYAML) ---
# Supports block mappings/sequences, "- key: value" items, flow lists/maps, quoted scalars, comments and
# literal block scalars (| and |-). Enough for docker-compose and GitHub Actions files; anything else raises.

def _split_flow(s: str) -> list[str]:
    parts: list[str] = []
    buf, depth, quote = "", 0, ""
    for idx, ch in enumerate(s):
        if quote:
            buf += ch
            if ch == quote and s[idx - 1] != "\\":
                quote = ""
            continue
        if ch in "\"'" and (not buf.strip()):
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(buf.strip())
            buf = ""
            continue
        buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return parts


def _split_kv(text: str) -> tuple[str, str] | None:
    if not text or text[0] in "[{":
        return None
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'" and (i == 0 or text[i - 1] in " [{,"):
            quote = ch
        elif ch == ":" and (i + 1 == len(text) or text[i + 1] == " "):
            key = text[:i].strip()
            if not key:
                return None
            if key[0] in "\"'":
                key = str(_scalar(key))
            return key, text[i + 1:].strip()
    return None


def _strip_comment(s: str) -> str:
    quote = ""
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'" and (i == 0 or s[i - 1] in " [{,"):
            quote = ch
        elif ch == "#" and i > 0 and s[i - 1] == " ":
            return s[:i].rstrip()
    return s


def _scalar(tok: str) -> Any:
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return json.loads(tok)
    if len(tok) >= 2 and tok[0] == "'" and tok[-1] == "'":
        return tok[1:-1].replace("''", "'")
    if tok.startswith("[") and tok.endswith("]"):
        return [_scalar(p) for p in _split_flow(tok[1:-1])]
    if tok.startswith("{") and tok.endswith("}"):
        out: dict[str, Any] = {}
        for p in _split_flow(tok[1:-1]):
            kv = _split_kv(p)
            if kv is None:
                raise YamlError("bad flow mapping entry: " + p)
            out[kv[0]] = _scalar(kv[1])
        return out
    low = tok.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", tok):
        return int(tok)
    if re.fullmatch(r"-?\d+\.\d+", tok):
        return float(tok)
    return tok


class _YamlParser:
    def __init__(self, text: str) -> None:
        self.raw = text.splitlines()
        self.i = 0
        for n, ln in enumerate(self.raw, 1):
            if "\t" in ln[:len(ln) - len(ln.lstrip())]:
                raise YamlError("tab indentation at line %d" % n)

    @staticmethod
    def _indent(s: str) -> int:
        return len(s) - len(s.lstrip(" "))

    def _skip(self) -> None:
        while self.i < len(self.raw) and (not self.raw[self.i].strip() or self.raw[self.i].lstrip().startswith("#")):
            self.i += 1

    def parse(self) -> Any:
        self._skip()
        if self.i >= len(self.raw):
            return None
        value = self._block(self._indent(self.raw[self.i]))
        self._skip()
        if self.i < len(self.raw):
            raise YamlError("unexpected content at line %d" % (self.i + 1))
        return value

    def _block(self, indent: int) -> Any:
        self._skip()
        text = self.raw[self.i].strip()
        return self._seq(indent) if (text.startswith("- ") or text == "-") else self._map(indent)

    def _block_scalar(self, parent_indent: int, header: str) -> str:
        if header not in ("|", "|-"):
            raise YamlError("unsupported block scalar header " + header)
        lines: list[str] = []
        while self.i < len(self.raw) and (not self.raw[self.i].strip() or self._indent(self.raw[self.i]) > parent_indent):
            lines.append(self.raw[self.i])
            self.i += 1
        while lines and not lines[-1].strip():
            lines.pop()
        body = [ln for ln in lines if ln.strip()]
        if not body:
            return ""
        block_indent = min(self._indent(ln) for ln in body)
        text = "\n".join(ln[block_indent:] if ln.strip() else "" for ln in lines)
        return text + ("\n" if header == "|" else "")

    def _seq(self, indent: int) -> list[Any]:
        out: list[Any] = []
        while True:
            self._skip()
            if self.i >= len(self.raw):
                break
            line = self.raw[self.i]
            ind, text = self._indent(line), line.strip()
            if ind < indent or not (text.startswith("- ") or text == "-"):
                break
            if ind > indent:
                raise YamlError("bad indentation at line %d" % (self.i + 1))
            rest = _strip_comment(text[1:].strip())
            if rest == "":
                self.i += 1
                self._skip()
                if self.i >= len(self.raw) or self._indent(self.raw[self.i]) <= indent:
                    out.append(None)
                else:
                    out.append(self._block(self._indent(self.raw[self.i])))
            elif rest in ("|", "|-"):
                self.i += 1
                out.append(self._block_scalar(indent, rest))
            elif _split_kv(rest) is not None:
                col = line.index(rest, ind + 1)
                self.raw[self.i] = " " * col + rest  # treat "- k: v" as a mapping indented past the dash
                out.append(self._map(col))
            else:
                out.append(_scalar(rest))
                self.i += 1
        return out

    def _map(self, indent: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while True:
            self._skip()
            if self.i >= len(self.raw):
                break
            line = self.raw[self.i]
            ind, text = self._indent(line), line.strip()
            if ind < indent:
                break
            if ind > indent:
                raise YamlError("unexpected indentation at line %d" % (self.i + 1))
            kv = _split_kv(text)
            if kv is None:
                raise YamlError("expected 'key: value' at line %d: %s" % (self.i + 1, text))
            key, rest = kv[0], _strip_comment(kv[1])
            if key in out:
                raise YamlError("duplicate key %s at line %d" % (key, self.i + 1))
            self.i += 1
            if rest in ("|", "|-"):
                out[key] = self._block_scalar(indent, rest)
            elif rest == "":
                self._skip()
                nxt = self.raw[self.i] if self.i < len(self.raw) else ""
                if nxt and (self._indent(nxt) > indent or (self._indent(nxt) == indent and nxt.strip().startswith("- "))):
                    out[key] = self._block(self._indent(nxt))
                else:
                    out[key] = None
            else:
                out[key] = _scalar(rest)
        return out


def parse_yaml(text: str) -> Any:
    return _YamlParser(text).parse()


_BARE_KEY = re.compile(r"[A-Za-z_][\w.\-/]*")


def _needs_quotes(s: str) -> bool:
    if s == "" or s != s.strip() or s[0] in "-?:,[]{}#&*!|>'\"%@`" or "*" in s:
        return True
    if ": " in s or " #" in s or s.endswith(":"):
        return True
    return s.lower() in ("true", "false", "null", "~", "yes", "no", "on", "off") or bool(re.fullmatch(r"-?\d+(\.\d+)?", s))


def _emit_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        return "{}"
    if isinstance(v, list):
        return "[]"
    s = str(v)
    return json.dumps(s) if _needs_quotes(s) else s


def _block_lines(s: str, indent: int) -> list[str]:
    return [(" " * indent + ln) if ln else "" for ln in s.rstrip("\n").split("\n")]


def _emit_entry(prefix: str, val: Any, indent: int) -> list[str]:
    if isinstance(val, (dict, list)) and val:
        return [prefix] + _emit(val, indent + 2)
    if isinstance(val, str) and "\n" in val:
        return [prefix + (" |" if val.endswith("\n") else " |-")] + _block_lines(val, indent + 2)
    return [prefix + " " + _emit_scalar(val)]


def _emit(v: Any, indent: int) -> list[str]:
    pad = " " * indent
    out: list[str] = []
    if isinstance(v, dict) and v:
        for k, val in v.items():
            key = str(k) if _BARE_KEY.fullmatch(str(k)) else json.dumps(str(k))
            out += _emit_entry(pad + key + ":", val, indent)
        return out
    if isinstance(v, list) and v:
        for item in v:
            if isinstance(item, dict) and item:
                sub = _emit(item, indent + 2)
                sub[0] = pad + "- " + sub[0][indent + 2:]
                out += sub
            elif isinstance(item, list) and item:
                out += [pad + "-"] + _emit(item, indent + 2)
            elif isinstance(item, str) and "\n" in item:
                out += [pad + ("- |" if item.endswith("\n") else "- |-")] + _block_lines(item, indent + 2)
            else:
                out.append(pad + "- " + _emit_scalar(item))
        return out
    return [pad + _emit_scalar(v)]


def emit_yaml(value: Any) -> str:
    return "\n".join(_emit(value, 0)) + "\n"


# ---- Syllabus extension: multi-container environments, volume mounting and networking (docker compose) ---

class VolumeMount(BaseModel):
    kind: Literal["named", "bind", "anonymous"]
    source: str = ""
    target: str
    read_only: bool = False


def parse_volume(spec: str) -> VolumeMount:
    """Parses the short volume syntax: 'name:/path', './dir:/path:ro', '/path'."""
    parts = spec.split(":")
    if len(parts) == 1:
        src, target, mode = "", parts[0], ""
    else:
        src, target, mode = parts[0], parts[1], (parts[2] if len(parts) > 2 else "")
    if not target.startswith("/"):
        raise ComposeError("container path must be absolute in volume %r" % spec)
    kind: Literal["named", "bind", "anonymous"] = "anonymous" if not src else ("bind" if src.startswith((".", "/", "~")) else "named")
    return VolumeMount(kind=kind, source=src, target=target, read_only="ro" in mode.split(","))


def parse_duration(s: Any) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", str(s)):
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    if total == 0.0 and str(s) not in ("0", "0s"):
        raise ComposeError("bad duration %r" % (s,))
    return total


class ComposeHealthcheck(BaseModel):
    test: list[str]
    interval_s: float = 30.0
    timeout_s: float = 30.0
    retries: int = 3
    start_period_s: float = 0.0


class ComposeService(BaseModel):
    name: str
    image: str | None = None
    build: str | None = None
    ports: list[str] = Field(default_factory=list)
    volumes: list[str] = Field(default_factory=list)
    networks: list[str] = Field(default_factory=lambda: ["default"])
    depends_on: dict[str, str] = Field(default_factory=dict)  # service -> condition
    environment: dict[str, str] = Field(default_factory=dict)
    healthcheck: ComposeHealthcheck | None = None
    read_only: bool = False
    user: str | None = None

    @model_validator(mode="after")
    def _needs_image_or_build(self) -> "ComposeService":
        if not (self.image or self.build):
            raise ValueError("service %s needs image or build" % self.name)
        return self


class ComposeProject(BaseModel):
    services: dict[str, ComposeService]
    networks: list[str] = Field(default_factory=lambda: ["default"])
    volumes: list[str] = Field(default_factory=list)


def load_compose(text: str) -> ComposeProject:
    data = parse_yaml(text)
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        raise ComposeError("compose file needs a services mapping")
    services: dict[str, ComposeService] = {}
    for name, d in data["services"].items():
        d = d or {}
        dep = d.get("depends_on") or {}
        dep = {n: "service_started" for n in dep} if isinstance(dep, list) else {n: (v or {}).get("condition", "service_started") for n, v in dep.items()}
        env = d.get("environment") or {}
        if isinstance(env, list):
            env = {e.split("=", 1)[0]: (e.split("=", 1)[1] if "=" in e else "") for e in env}
        hc = d.get("healthcheck")
        if hc:
            test = hc.get("test", [])
            test = ["CMD-SHELL", test] if isinstance(test, str) else test
            hc = ComposeHealthcheck(test=[str(t) for t in test], interval_s=parse_duration(hc.get("interval", "30s")),
                                    timeout_s=parse_duration(hc.get("timeout", "30s")), retries=int(hc.get("retries", 3)),
                                    start_period_s=parse_duration(hc.get("start_period", "0s")))
        nets = d.get("networks") or ["default"]
        services[name] = ComposeService(name=name, image=d.get("image"), build=d.get("build"), ports=[str(p) for p in d.get("ports") or []],
                                        volumes=[str(v) for v in d.get("volumes") or []], networks=list(nets), depends_on=dep,
                                        environment={str(k): str(v) for k, v in env.items()}, healthcheck=hc or None,
                                        read_only=bool(d.get("read_only", False)), user=(str(d["user"]) if d.get("user") else None))
    return ComposeProject(services=services, networks=["default", *(data.get("networks") or {}).keys()],
                          volumes=list((data.get("volumes") or {}).keys()))


def startup_layers(p: ComposeProject) -> list[list[str]]:
    """Topological layers of depends_on (services in a layer can start together); raises on unknown deps and cycles."""
    for s in p.services.values():
        for dep in s.depends_on:
            if dep not in p.services:
                raise ComposeError("%s depends_on unknown service %s" % (s.name, dep))
    remaining = {n: set(s.depends_on) for n, s in p.services.items()}
    layers: list[list[str]] = []
    while remaining:
        ready = sorted(n for n, deps in remaining.items() if not deps)
        if not ready:
            raise ComposeError("depends_on cycle among " + ", ".join(sorted(remaining)))
        layers.append(ready)
        for n in ready:
            del remaining[n]
        for deps in remaining.values():
            deps.difference_update(ready)
    return layers


def parse_port(spec: str) -> tuple[str, int, int]:
    """'127.0.0.1:8080:80/tcp' -> (host_ip, host_port, container_port)."""
    parts = spec.split("/")[0].split(":")
    if len(parts) == 1:
        return "", 0, int(parts[0])
    if len(parts) == 2:
        return "0.0.0.0", int(parts[0]), int(parts[1])
    return parts[0], int(parts[1]), int(parts[2])


DATA_PORTS = {5432, 3306, 6379, 27017, 9200}


def validate_compose(p: ComposeProject) -> list[Finding]:
    """Raises ComposeError for files Docker would reject; returns Findings for risky-but-valid choices."""
    f: list[Finding] = []
    startup_layers(p)
    seen_ports: dict[int, str] = {}
    for s in p.services.values():
        for net in s.networks:
            if net not in p.networks:
                raise ComposeError("%s uses undeclared network %s" % (s.name, net))
        for raw in s.volumes:
            v = parse_volume(raw)
            if v.kind == "named" and v.source not in p.volumes:
                raise ComposeError("%s mounts undeclared named volume %s" % (s.name, v.source))
            if v.kind == "bind" and v.source in ("/", "/etc", "/var/run/docker.sock"):
                f.append(Finding(rule="dangerous-bind", severity=Severity.CRITICAL, message="%s binds %s: host takeover risk" % (s.name, v.source)))
        for raw in s.ports:
            ip, host_port, cport = parse_port(raw)
            if host_port and host_port in seen_ports:
                raise ComposeError("host port %d published by both %s and %s" % (host_port, seen_ports[host_port], s.name))
            if host_port:
                seen_ports[host_port] = s.name
            if cport in DATA_PORTS and ip not in ("127.0.0.1", "::1"):
                f.append(Finding(rule="published-data-port", severity=Severity.HIGH, message="%s publishes data port %d on all interfaces" % (s.name, cport)))
        for dep, cond in s.depends_on.items():
            target = p.services[dep]
            if cond == "service_healthy" and not target.healthcheck:
                raise ComposeError("%s waits for %s to be healthy but %s has no healthcheck" % (s.name, dep, dep))
            if cond == "service_started" and not target.healthcheck:
                f.append(Finding(rule="start-not-ready", severity=Severity.LOW, message="%s only waits for %s to start, not to be ready" % (s.name, dep)))
        hc = s.healthcheck
        if hc:
            if hc.retries < 1 or not hc.test or hc.test[0] not in ("CMD", "CMD-SHELL", "NONE"):
                raise ComposeError("%s has an invalid healthcheck" % s.name)
            if hc.timeout_s >= hc.interval_s:
                f.append(Finding(rule="hc-timeout", severity=Severity.MEDIUM, message="%s healthcheck timeout >= interval: checks overlap" % s.name))
        if s.image and (s.image.endswith(":latest") or ":" not in s.image.split("/")[-1]) and "@sha256:" not in s.image:
            f.append(Finding(rule="floating-tag", severity=Severity.MEDIUM, message="%s uses a floating image tag" % s.name))
        for k, v in s.environment.items():
            if SECRET_NAME.search(k) and v and not v.startswith("${") and not k.endswith("_FILE"):
                f.append(Finding(rule="plain-secret", severity=Severity.HIGH, message="%s sets %s in the compose file; use secrets or *_FILE" % (s.name, k)))
    return f


def can_reach(p: ComposeProject, a: str, b: str) -> bool:
    """Containers reach each other by service name only when they share a user-defined network."""
    return bool(set(p.services[a].networks) & set(p.services[b].networks))


def volume_report(p: ComposeProject) -> dict[str, dict[str, list[str]]]:
    """Which container paths survive container recreation (named volumes, bind mounts) and which are ephemeral."""
    out: dict[str, dict[str, list[str]]] = {}
    for n, s in p.services.items():
        mounts = [parse_volume(v) for v in s.volumes]
        out[n] = {"persistent_named": [m.target for m in mounts if m.kind == "named"],
                  "host_bind": [m.target for m in mounts if m.kind == "bind"],
                  "read_only": [m.target for m in mounts if m.read_only],
                  "ephemeral_writable_layer": ["everything else (lost when the container is removed)"] if not s.read_only else ["none: read_only root filesystem"]}
    return out


def simulate_startup(p: ComposeProject, boot_s: dict[str, float]) -> dict[str, tuple[float, float]]:
    """Approximate virtual timeline: name -> (started_at, healthy_at). First health probe fires one interval after boot."""
    t: dict[str, tuple[float, float]] = {}
    for layer in startup_layers(p):
        for n in layer:
            s = p.services[n]
            start = max([t[d][1] if c == "service_healthy" else t[d][0] for d, c in s.depends_on.items()] or [0.0])
            ready = start + boot_s.get(n, 1.0)
            healthy = max(ready, start + s.healthcheck.start_period_s) + s.healthcheck.interval_s if s.healthcheck else ready
            t[n] = (start, healthy)
    return t


def compose_to_docker_run(p: ComposeProject, name: str) -> list[str]:
    """Equivalent plain `docker` commands for one service (shows what compose automates)."""
    s = p.services[name]
    cmds = ["docker network create %s" % shlex.quote(n) for n in s.networks if n != "default"]
    run = ["docker", "run", "-d", "--name", name]
    if s.networks and s.networks[0] != "default":
        run += ["--network", s.networks[0]]
    if s.read_only:
        run.append("--read-only")
    if s.user:
        run += ["--user", s.user]
    for port in s.ports:
        run += ["-p", port]
    for v in s.volumes:
        run += ["-v", v]
    if s.healthcheck:
        hc = s.healthcheck
        run += ["--health-cmd", " ".join(hc.test[1:]) if hc.test[0] == "CMD-SHELL" else shlex.join(hc.test[1:]),
                "--health-interval", "%ds" % hc.interval_s, "--health-retries", str(hc.retries), "--health-timeout", "%ds" % hc.timeout_s]
    run.append(s.image or name)
    return cmds + [shlex.join(run)]


# ---- Syllabus extension: containerizing FastAPI backends ---

FASTAPI_APP_PY = '''from fastapi import FastAPI

app = FastAPI(title="fde-api")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up. Keep it dependency-free so a slow database never restarts the container."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    """Readiness: dependencies are reachable. The load balancer target group checks this path."""
    return {"status": "ready"}


@app.get("/v1/echo/{text}")
def echo(text: str) -> dict[str, str]:
    return {"echo": text}
'''

_FA_DIGEST = "@sha256:" + "a" * 64
FASTAPI_DOCKERFILE = (
    "# builder: resolves and installs dependencies into /install (cached until requirements.txt changes)\n"
    "FROM python:3.12-slim" + _FA_DIGEST + " AS builder\n"
    "WORKDIR /build\n"
    "COPY requirements.txt .\n"
    "RUN pip install --no-cache-dir --prefix=/install -r requirements.txt\n"
    "# runtime: slim image, non-root, only the installed packages and the app code\n"
    "FROM python:3.12-slim" + _FA_DIGEST + "\n"
    "ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1\n"
    "RUN useradd --system --uid 10001 --no-create-home appuser\n"
    "COPY --from=builder /install /usr/local\n"
    "WORKDIR /srv\n"
    "COPY app ./app\n"
    "USER 10001:10001\n"
    "EXPOSE 8000\n"
    'HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 CMD ["python", "-c", '
    '"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen(\'http://127.0.0.1:8000/healthz\', timeout=2).status == 200 else 1)"]\n'
    'CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]\n'
)

NAIVE_DOCKERFILE = "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt\nCMD [\"python\", \"app/main.py\"]\n"


def fastapi_contract_check(dockerfile: str, app_source: str) -> list[str]:
    """Offline proof that image and app agree: module path, port, EXPOSE, HEALTHCHECK URL, non-root user."""
    problems: list[str] = []
    try:
        ast.parse(app_source)
    except SyntaxError as exc:
        return ["app source does not parse: %s" % exc]
    routes = set(re.findall(r'@app\.(?:get|post|put|delete)\("([^"]+)"', app_source))
    ins = parse_dockerfile(dockerfile)
    cmd = next((i for i in reversed(ins) if i.op == "CMD"), None)
    if cmd is None:
        return ["no CMD"]
    argv = json.loads(cmd.arg)
    if "uvicorn" not in argv or "--port" not in argv:
        return ["CMD must run uvicorn with an explicit --port"]
    port = int(argv[argv.index("--port") + 1])
    module = argv[argv.index("uvicorn") + 1].split(":")[0]
    if not any(i.op == "COPY" and "--from" not in i.arg and module.split(".")[0] in copy_sources(i) for i in ins):
        problems.append("module package %s is never COPY'd into the image" % module.split(".")[0])
    expose = next((int(i.arg.split("/")[0]) for i in ins if i.op == "EXPOSE"), None)
    if expose != port:
        problems.append("EXPOSE %s does not match uvicorn port %d" % (expose, port))
    hc = next((i for i in ins if i.op == "HEALTHCHECK"), None)
    if hc is None:
        problems.append("no HEALTHCHECK")
    else:
        m = re.search(r"127\.0\.0\.1:(\d+)(/[\w/]*)", hc.arg)
        if not m or int(m.group(1)) != port:
            problems.append("HEALTHCHECK does not probe the serving port")
        elif m.group(2) not in routes:
            problems.append("HEALTHCHECK path %s is not a route in the app" % m.group(2))
    if "--host" in argv and argv[argv.index("--host") + 1] == "127.0.0.1":
        problems.append("binding 127.0.0.1 inside a container makes it unreachable from outside")
    if "/healthz" not in routes or "/readyz" not in routes:
        problems.append("app must expose /healthz and /readyz")
    return problems


def run_fastapi_smoke_test(app_source: str) -> dict[str, int]:
    """Real adapter: executes the app in-process with FastAPI's TestClient. Needs `pip install fastapi httpx`."""
    try:
        from fastapi.testclient import TestClient  # noqa: PLC0415
    except ImportError as exc:
        raise OptionalDependencyError("fastapi/httpx not installed; run: pip install fastapi httpx") from exc
    ns: dict[str, Any] = {}
    exec(compile(app_source, "app/main.py", "exec"), ns)  # our own constant source, not external input
    client = TestClient(ns["app"])
    return {path: client.get(path).status_code for path in ("/healthz", "/readyz")}


# ---- Syllabus extension: serverless container concepts and configuring task definitions (ECS / Fargate) ---

FARGATE_MEMORY_MIB: dict[int, list[int]] = {  # task CPU units -> allowed task memory (MiB)
    256: [512, 1024, 2048],
    512: list(range(1024, 4096 + 1, 1024)),
    1024: list(range(2048, 8192 + 1, 1024)),
    2048: list(range(4096, 16384 + 1, 1024)),
    4096: list(range(8192, 30720 + 1, 1024)),
    8192: list(range(16384, 61440 + 1, 4096)),
    16384: list(range(32768, 122880 + 1, 8192)),
}
SECRET_ARN = re.compile(r"^arn:aws[a-z-]*:(secretsmanager:[a-z0-9-]+:\d{12}:secret:[\w/+=.@-]+|ssm:[a-z0-9-]+:\d{12}:parameter/[\w/.-]+)$")


def smallest_fargate_size(vcpu: float, mem_mib: int) -> tuple[int, int]:
    """Smallest valid (cpu units, memory MiB) pair that fits the request."""
    for cpu in sorted(FARGATE_MEMORY_MIB):
        if cpu >= vcpu * 1024:
            for mem in FARGATE_MEMORY_MIB[cpu]:
                if mem >= mem_mib:
                    return cpu, mem
    raise PipelineError("no Fargate size fits %s vCPU / %d MiB" % (vcpu, mem_mib))


class WorkloadProfile(BaseModel):
    max_duration_s: int = Field(gt=0)
    vcpu: float = Field(gt=0)
    memory_mib: int = Field(gt=0)
    needs_gpu: bool = False
    needs_scale_to_zero: bool = False
    steady_24x7: bool = False
    long_lived_connections: bool = False


def choose_runtime(w: WorkloadProfile) -> tuple[str, list[str]]:
    """Serverless-container decision helper: function vs serverless containers vs self-managed hosts."""
    why: list[str] = []
    if w.needs_gpu:
        return "ECS or EKS on EC2", ["Fargate does not offer GPUs"]
    lambda_fits = w.max_duration_s <= 900 and w.memory_mib <= 10240 and not w.long_lived_connections
    if lambda_fits and w.needs_scale_to_zero:
        return "Lambda (zip or container image)", ["runs under 15 minutes and 10 GiB", "scales to zero between events"]
    if w.vcpu <= 16 and w.memory_mib <= 122880:
        cpu, mem = smallest_fargate_size(w.vcpu, w.memory_mib)
        why.append("no servers to patch; smallest valid task size %d CPU units / %d MiB" % (cpu, mem))
        if w.steady_24x7:
            why.append("steady 24x7 load: compare Fargate against EC2 capacity using your own price sheet")
        if not lambda_fits:
            why.append("exceeds Lambda limits or needs long-lived connections")
        return "ECS on Fargate", why
    return "ECS or EKS on EC2", ["task larger than the biggest Fargate size"]


class SecretRef(BaseModel):
    name: str
    value_from: str


class LogConfig(BaseModel):
    log_driver: Literal["awslogs", "awsfirelens"] = "awslogs"
    options: dict[str, str] = Field(default_factory=dict)


class ContainerHealth(BaseModel):
    command: list[str]
    interval_s: int = 30
    timeout_s: int = 5
    retries: int = 3
    start_period_s: int = 0


class ContainerDef(BaseModel):
    name: str
    image: str
    essential: bool = True
    cpu: int = 0
    memory_mib: int | None = None
    ports: list[int] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    secrets: list[SecretRef] = Field(default_factory=list)
    log: LogConfig | None = None
    health: ContainerHealth | None = None
    readonly_root_fs: bool = False
    user: str | None = None


class TaskDefinition(BaseModel):
    family: str
    cpu: int
    memory_mib: int
    network_mode: str = "awsvpc"
    execution_role_arn: str | None = None
    task_role_arn: str | None = None
    cpu_architecture: Literal["X86_64", "ARM64"] = "X86_64"
    containers: list[ContainerDef]


def validate_task_definition(td: TaskDefinition) -> list[Finding]:
    """HIGH/CRITICAL findings mean register-task-definition (or the first run) would fail or leak."""
    f: list[Finding] = []

    def add(rule: str, sev: Severity, msg: str) -> None:
        f.append(Finding(rule=rule, severity=sev, message=msg))

    if td.cpu not in FARGATE_MEMORY_MIB or td.memory_mib not in FARGATE_MEMORY_MIB[td.cpu]:
        add("fargate-size", Severity.CRITICAL, "cpu %d / memory %d is not a valid Fargate pair" % (td.cpu, td.memory_mib))
    if td.network_mode != "awsvpc":
        add("network-mode", Severity.CRITICAL, "Fargate requires awsvpc networking")
    if not any(c.essential for c in td.containers):
        add("essential", Severity.CRITICAL, "at least one container must be essential")
    if sum(c.cpu for c in td.containers) > td.cpu:
        add("cpu-sum", Severity.CRITICAL, "container cpu reservations exceed the task cpu")
    if sum(c.memory_mib or 0 for c in td.containers) > td.memory_mib:
        add("memory-sum", Severity.CRITICAL, "container memory limits exceed the task memory")
    needs_exec_role = False
    for c in td.containers:
        if c.secrets or (c.log and c.log.log_driver == "awslogs"):
            needs_exec_role = True
        for s in c.secrets:
            if not SECRET_ARN.match(s.value_from):
                add("secret-arn", Severity.CRITICAL, "%s: %s valueFrom is not a Secrets Manager or SSM ARN" % (c.name, s.name))
            if s.name in c.environment:
                add("secret-dup", Severity.HIGH, "%s: %s is both a secret and a plain environment variable" % (c.name, s.name))
        for k, v in c.environment.items():
            if SECRET_NAME.search(k) and v:
                add("plain-secret", Severity.HIGH, "%s: %s holds a secret-looking value in plain text; use secrets[]" % (c.name, k))
        if c.log is None:
            add("log-config", Severity.MEDIUM, "%s has no logConfiguration: output is lost when the task stops" % c.name)
        elif c.log.log_driver == "awslogs":
            for opt in ("awslogs-group", "awslogs-region", "awslogs-stream-prefix"):
                if opt not in c.log.options:
                    add("log-config", Severity.CRITICAL, "%s: awslogs option %s is required on Fargate" % (c.name, opt))
        if c.image.endswith(":latest") or ":" not in c.image.split("/")[-1]:
            add("image-tag", Severity.MEDIUM, "%s uses a mutable image tag; deploy by digest or commit SHA" % c.name)
        if not c.readonly_root_fs:
            add("readonly-fs", Severity.LOW, "%s root filesystem is writable" % c.name)
        if c.health and c.health.timeout_s >= c.health.interval_s:
            add("health", Severity.MEDIUM, "%s health check timeout >= interval" % c.name)
    if needs_exec_role and not td.execution_role_arn:
        add("exec-role", Severity.CRITICAL, "executionRoleArn is required to pull secrets and write awslogs")
    return f


def taskdef_json(td: TaskDefinition) -> dict[str, Any]:
    """Body for: aws ecs register-task-definition --cli-input-json file://taskdef.json"""
    containers: list[dict[str, Any]] = []
    for c in td.containers:
        d: dict[str, Any] = {"name": c.name, "image": c.image, "essential": c.essential}
        if c.cpu:
            d["cpu"] = c.cpu
        if c.memory_mib:
            d["memory"] = c.memory_mib
        if c.ports:
            d["portMappings"] = [{"containerPort": p, "protocol": "tcp"} for p in c.ports]
        if c.environment:
            d["environment"] = [{"name": k, "value": v} for k, v in sorted(c.environment.items())]
        if c.secrets:
            d["secrets"] = [{"name": s.name, "valueFrom": s.value_from} for s in c.secrets]
        if c.log:
            d["logConfiguration"] = {"logDriver": c.log.log_driver, "options": c.log.options}
        if c.health:
            h = c.health
            d["healthCheck"] = {"command": h.command, "interval": h.interval_s, "timeout": h.timeout_s, "retries": h.retries, "startPeriod": h.start_period_s}
        if c.readonly_root_fs:
            d["readonlyRootFilesystem"] = True
        if c.user:
            d["user"] = c.user
        containers.append(d)
    doc: dict[str, Any] = {"family": td.family, "networkMode": td.network_mode, "requiresCompatibilities": ["FARGATE"],
                           "cpu": str(td.cpu), "memory": str(td.memory_mib),
                           "runtimePlatform": {"cpuArchitecture": td.cpu_architecture, "operatingSystemFamily": "LINUX"},
                           "containerDefinitions": containers}
    if td.execution_role_arn:
        doc["executionRoleArn"] = td.execution_role_arn
    if td.task_role_arn:
        doc["taskRoleArn"] = td.task_role_arn
    return doc


TASKDEF_TOP_KEYS = {"family", "networkMode", "requiresCompatibilities", "cpu", "memory", "runtimePlatform", "containerDefinitions",
                    "executionRoleArn", "taskRoleArn"}


def validate_taskdef_json(doc: dict[str, Any]) -> list[str]:
    """Independent check of the emitted JSON (camelCase keys, string cpu/memory, valid pair, ARNs, awslogs options)."""
    p: list[str] = []
    for k in doc:
        if k not in TASKDEF_TOP_KEYS:
            p.append("unknown top-level key " + k)
    if doc.get("requiresCompatibilities") != ["FARGATE"] or doc.get("networkMode") != "awsvpc":
        p.append("Fargate needs requiresCompatibilities=[FARGATE] and networkMode=awsvpc")
    if not (isinstance(doc.get("cpu"), str) and isinstance(doc.get("memory"), str)):
        p.append("cpu and memory must be strings")
    else:
        if int(doc["memory"]) not in FARGATE_MEMORY_MIB.get(int(doc["cpu"]), []):
            p.append("invalid cpu/memory pair")
    for c in doc.get("containerDefinitions", []):
        for s in c.get("secrets", []):
            if not SECRET_ARN.match(s["valueFrom"]):
                p.append("secret %s has a bad valueFrom" % s["name"])
        lc = c.get("logConfiguration")
        if lc and lc["logDriver"] == "awslogs" and not {"awslogs-group", "awslogs-region", "awslogs-stream-prefix"} <= set(lc["options"]):
            p.append("awslogs options incomplete")
    if not doc.get("containerDefinitions"):
        p.append("containerDefinitions is empty")
    return p


# ---- Syllabus extension: load balancer integration (ALB target groups, health checks, routing rules) ---

class TargetGroup(BaseModel):
    name: str
    port: int = Field(ge=1, le=65535)
    target_type: Literal["ip", "instance"] = "ip"  # awsvpc/Fargate tasks register by IP
    health_path: str = "/"
    interval_s: int = 30
    timeout_s: int = 5
    healthy_threshold: int = 5
    unhealthy_threshold: int = 2
    matcher: str = "200"
    deregistration_delay_s: int = 300
    targets: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _limits(self) -> "TargetGroup":
        if not self.health_path.startswith("/"):
            raise ValueError("health path must start with /")
        if not (5 <= self.interval_s <= 300 and 2 <= self.timeout_s <= 120):
            raise ValueError("interval must be 5-300s and timeout 2-120s")
        if self.timeout_s >= self.interval_s:
            raise ValueError("timeout must be shorter than the interval")
        if not (2 <= self.healthy_threshold <= 10 and 2 <= self.unhealthy_threshold <= 10):
            raise ValueError("thresholds must be 2-10")
        if not re.fullmatch(r"\d{3}(-\d{3})?(,\d{3}(-\d{3})?)*", self.matcher):
            raise ValueError("bad success-code matcher")
        return self

    @property
    def seconds_to_mark_unhealthy(self) -> int:
        return self.interval_s * self.unhealthy_threshold

    @property
    def seconds_to_mark_healthy(self) -> int:
        return self.interval_s * self.healthy_threshold


def matcher_accepts(matcher: str, code: int) -> bool:
    for part in matcher.split(","):
        lo, _, hi = part.partition("-")
        if int(lo) <= code <= int(hi or lo):
            return True
    return False


class TargetHealth(BaseModel):
    state: Literal["initial", "healthy", "unhealthy"] = "initial"
    ok_streak: int = 0
    fail_streak: int = 0


class HealthTracker:
    """Consecutive-probe state machine: healthy after N passes, unhealthy after M failures (timeouts are failures)."""

    def __init__(self, tg: TargetGroup) -> None:
        self.tg = tg
        self.state: dict[str, TargetHealth] = {t: TargetHealth() for t in tg.targets}

    def probe(self, target: str, status_code: int | None) -> str:
        st = self.state[target]
        if status_code is not None and matcher_accepts(self.tg.matcher, status_code):
            st.ok_streak, st.fail_streak = st.ok_streak + 1, 0
            if st.ok_streak >= self.tg.healthy_threshold:
                st.state = "healthy"
        else:
            st.fail_streak, st.ok_streak = st.fail_streak + 1, 0
            if st.fail_streak >= self.tg.unhealthy_threshold:
                st.state = "unhealthy"
        return st.state

    def healthy(self) -> list[str]:
        return [t for t, s in self.state.items() if s.state == "healthy"]


class RuleAction(BaseModel):
    type: Literal["forward", "redirect", "fixed-response"] = "forward"
    target_groups: dict[str, int] = Field(default_factory=dict)  # name -> weight
    status_code: int | None = None
    location: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "RuleAction":
        if self.type == "forward" and not self.target_groups:
            raise ValueError("forward needs at least one target group")
        if self.type != "forward" and self.status_code is None:
            raise ValueError("%s needs a status code" % self.type)
        return self


class ListenerRule(BaseModel):
    priority: int = Field(ge=1, le=50000)
    path_patterns: list[str] = Field(default_factory=list)
    host_headers: list[str] = Field(default_factory=list)
    http_methods: list[str] = Field(default_factory=list)
    action: RuleAction


class HttpRequest(BaseModel):
    host: str
    path: str
    method: str = "GET"


class RouteDecision(BaseModel):
    kind: str
    rule: str
    target_group: str | None = None
    target: str | None = None
    status_code: int | None = None
    fail_open: bool = False


class LoadBalancerSim:
    """Application Load Balancer model: priority-ordered rules (AND across condition types, OR within), default
    action, weighted forwarding, round-robin over healthy targets, and fail-open when none are healthy."""

    def __init__(self, rules: list[ListenerRule], default: RuleAction, groups: dict[str, TargetGroup], seed: int = 7) -> None:
        if len({r.priority for r in rules}) != len(rules):
            raise PipelineError("rule priorities must be unique")
        self.rules = sorted(rules, key=lambda r: r.priority)
        self.default = default
        self.groups = groups
        self.trackers = {n: HealthTracker(g) for n, g in groups.items()}
        self._rr: dict[str, int] = {}
        self._rng = random.Random(seed)
        for r in [*self.rules, ListenerRule(priority=50000, action=default)]:
            for tgname in r.action.target_groups:
                if tgname not in groups:
                    raise PipelineError("rule %d references unknown target group %s" % (r.priority, tgname))

    @staticmethod
    def _matches(rule: ListenerRule, req: HttpRequest) -> bool:
        if rule.path_patterns and not any(fnmatch.fnmatchcase(req.path, p) for p in rule.path_patterns):
            return False
        if rule.host_headers and not any(fnmatch.fnmatchcase(req.host.lower(), h.lower()) for h in rule.host_headers):
            return False
        return not rule.http_methods or req.method.upper() in [m.upper() for m in rule.http_methods]

    def route(self, req: HttpRequest) -> RouteDecision:
        for r in self.rules:
            if self._matches(r, req):
                return self._apply(r.action, "priority %d" % r.priority)
        return self._apply(self.default, "default")

    def _apply(self, a: RuleAction, label: str) -> RouteDecision:
        if a.type != "forward":
            return RouteDecision(kind=a.type, rule=label, status_code=a.status_code)
        names = sorted(a.target_groups)
        pick = self._rng.choices(names, weights=[a.target_groups[n] for n in names])[0] if len(names) > 1 else names[0]
        pool = self.trackers[pick].healthy()
        fail_open = False
        if not pool:
            pool, fail_open = list(self.groups[pick].targets), True  # ALB sends to all targets when none are healthy
        if not pool:
            return RouteDecision(kind="fixed-response", rule=label, target_group=pick, status_code=503)
        i = self._rr.get(pick, 0)
        self._rr[pick] = i + 1
        return RouteDecision(kind="forward", rule=label, target_group=pick, target=pool[i % len(pool)], fail_open=fail_open)


class EcsServiceSpec(BaseModel):
    cluster: str
    name: str
    task_family: str
    desired_count: int = Field(ge=0)
    subnets: list[str] = Field(min_length=1)
    security_groups: list[str] = Field(min_length=1)
    assign_public_ip: bool = False
    lb_target_group_arn: str | None = None
    lb_container: str | None = None
    lb_port: int | None = None
    health_grace_s: int = 0
    max_percent: int = 200
    min_healthy_percent: int = 100


def check_service_wiring(td: TaskDefinition, tg: TargetGroup, svc: EcsServiceSpec) -> list[Finding]:
    """The wiring mistakes that cause 'service is running but the load balancer shows no healthy targets'."""
    f: list[Finding] = []
    box = next((c for c in td.containers if c.name == svc.lb_container), None)
    if box is None:
        f.append(Finding(rule="lb-container", severity=Severity.CRITICAL, message="load balancer container %s is not in the task definition" % svc.lb_container))
    elif svc.lb_port not in box.ports:
        f.append(Finding(rule="lb-port", severity=Severity.CRITICAL, message="container %s has no port mapping for %s" % (box.name, svc.lb_port)))
    if svc.lb_port != tg.port:
        f.append(Finding(rule="tg-port", severity=Severity.HIGH, message="service registers port %s but the target group port is %d" % (svc.lb_port, tg.port)))
    if td.network_mode == "awsvpc" and tg.target_type != "ip":
        f.append(Finding(rule="tg-type", severity=Severity.CRITICAL, message="awsvpc tasks need an ip target group"))
    if box and box.health:
        warm = box.health.start_period_s + tg.seconds_to_mark_healthy
        if svc.health_grace_s < warm:
            f.append(Finding(rule="grace", severity=Severity.MEDIUM, message="grace period %ds is shorter than start period + time to healthy (%ds): tasks may be killed while warming" % (svc.health_grace_s, warm)))
    if svc.assign_public_ip:
        f.append(Finding(rule="public-ip", severity=Severity.MEDIUM, message="tasks get public IPs; prefer private subnets behind the ALB"))
    return f


def ecs_service_cli(svc: EcsServiceSpec) -> str:
    net = "awsvpcConfiguration={subnets=[%s],securityGroups=[%s],assignPublicIp=%s}" % (
        ",".join(svc.subnets), ",".join(svc.security_groups), "ENABLED" if svc.assign_public_ip else "DISABLED")
    parts = ["aws", "ecs", "create-service", "--cluster", svc.cluster, "--service-name", svc.name, "--task-definition", svc.task_family,
             "--desired-count", str(svc.desired_count), "--launch-type", "FARGATE", "--network-configuration", net,
             "--deployment-configuration",
             "deploymentCircuitBreaker={enable=true,rollback=true},maximumPercent=%d,minimumHealthyPercent=%d" % (svc.max_percent, svc.min_healthy_percent)]
    if svc.lb_target_group_arn:
        parts += ["--load-balancers", "targetGroupArn=%s,containerName=%s,containerPort=%s" % (svc.lb_target_group_arn, svc.lb_container, svc.lb_port),
                  "--health-check-grace-period-seconds", str(svc.health_grace_s)]
    return shlex.join(parts)


def validate_service_cli(cmd: str, svc: EcsServiceSpec) -> list[str]:
    tok = shlex.split(cmd)
    flags = {t: tok[i + 1] for i, t in enumerate(tok[:-1]) if t.startswith("--")}
    p: list[str] = []
    if "rollback=true" not in flags.get("--deployment-configuration", ""):
        p.append("circuit breaker rollback missing")
    if svc.lb_target_group_arn and svc.lb_target_group_arn not in flags.get("--load-balancers", ""):
        p.append("target group missing from --load-balancers")
    if "assignPublicIp=DISABLED" not in flags.get("--network-configuration", "") and not svc.assign_public_ip:
        p.append("public IP unexpectedly enabled")
    return p


# ---- Syllabus extension: auto-scaling policy configuration ---

class ScalingPolicy(BaseModel):
    cluster: str
    service: str
    min_capacity: int = Field(ge=1)
    max_capacity: int = Field(ge=1)
    metric: Literal["ECSServiceAverageCPUUtilization", "ECSServiceAverageMemoryUtilization", "ALBRequestCountPerTarget"]
    target_value: float = Field(gt=0)
    scale_out_cooldown_s: int = Field(default=60, ge=0)
    scale_in_cooldown_s: int = Field(default=300, ge=0)
    disable_scale_in: bool = False
    resource_label: str | None = None  # required for ALBRequestCountPerTarget

    @model_validator(mode="after")
    def _consistent(self) -> "ScalingPolicy":
        if self.min_capacity > self.max_capacity:
            raise ValueError("min_capacity > max_capacity")
        if self.metric != "ALBRequestCountPerTarget" and self.target_value > 100:
            raise ValueError("utilization target cannot exceed 100")
        if self.metric == "ALBRequestCountPerTarget" and not self.resource_label:
            raise ValueError("ALBRequestCountPerTarget needs resource_label (app/<lb>/<id>/targetgroup/<tg>/<id>)")
        return self

    @property
    def resource_id(self) -> str:
        return "service/%s/%s" % (self.cluster, self.service)


def desired_capacity(current: int, metric_value: float, p: ScalingPolicy) -> int:
    """Target tracking in one line: capacity scales in proportion to metric/target, clamped to [min, max]."""
    want = max(1, math.ceil(current * metric_value / p.target_value))
    if p.disable_scale_in:
        want = max(want, current)
    return min(max(want, p.min_capacity), p.max_capacity)


class ScalingEvent(BaseModel):
    t_s: int
    from_tasks: int
    to_tasks: int
    metric: float


class ScalingRun(BaseModel):
    events: list[ScalingEvent]
    tasks_by_tick: list[int]
    saturated_ticks: int  # ticks where per-task load exceeded capacity


def simulate_autoscaling(p: ScalingPolicy, load: list[float], capacity_per_task: float, start_tasks: int = 2, step_s: int = 60) -> ScalingRun:
    """Simplified model (not AWS's exact algorithm): scale out is gated by the out-cooldown, scale in by the in-cooldown
    measured from the last scaling action of either kind. `load` is demand per tick in the same unit as capacity."""
    tasks = max(start_tasks, p.min_capacity)
    last_out = last_any = -10 ** 9
    events: list[ScalingEvent] = []
    series: list[int] = []
    saturated = 0
    for tick, demand in enumerate(load):
        t = tick * step_s
        if p.metric == "ALBRequestCountPerTarget":
            metric = demand / tasks
        else:
            metric = demand / (tasks * capacity_per_task) * 100
        if demand > tasks * capacity_per_task:
            saturated += 1
        want = desired_capacity(tasks, metric, p)
        if want > tasks and t - last_out >= p.scale_out_cooldown_s:
            events.append(ScalingEvent(t_s=t, from_tasks=tasks, to_tasks=want, metric=round(metric, 1)))
            tasks, last_out, last_any = want, t, t
        elif want < tasks and t - last_any >= p.scale_in_cooldown_s:
            events.append(ScalingEvent(t_s=t, from_tasks=tasks, to_tasks=want, metric=round(metric, 1)))
            tasks, last_any = want, t
        series.append(tasks)
    return ScalingRun(events=events, tasks_by_tick=series, saturated_ticks=saturated)


def scaling_artifacts(p: ScalingPolicy) -> tuple[list[str], dict[str, Any]]:
    """(CLI commands, target-tracking JSON) for application-autoscaling register-scalable-target / put-scaling-policy."""
    spec: dict[str, Any] = {"PredefinedMetricType": p.metric}
    if p.resource_label:
        spec["ResourceLabel"] = p.resource_label
    cfg = {"TargetValue": p.target_value, "PredefinedMetricSpecification": spec, "ScaleOutCooldown": p.scale_out_cooldown_s,
           "ScaleInCooldown": p.scale_in_cooldown_s, "DisableScaleIn": p.disable_scale_in}
    base = ["--service-namespace", "ecs", "--scalable-dimension", "ecs:service:DesiredCount", "--resource-id", p.resource_id]
    cmds = [shlex.join(["aws", "application-autoscaling", "register-scalable-target", *base, "--min-capacity", str(p.min_capacity), "--max-capacity", str(p.max_capacity)]),
            shlex.join(["aws", "application-autoscaling", "put-scaling-policy", "--policy-name", "%s-%s" % (p.service, "tt"), *base,
                        "--policy-type", "TargetTrackingScaling", "--target-tracking-scaling-policy-configuration", "file://policy.json"])]
    return cmds, cfg


def validate_scaling_artifacts(p: ScalingPolicy, cmds: list[str], cfg: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if cfg["TargetValue"] != p.target_value or cfg["PredefinedMetricSpecification"]["PredefinedMetricType"] != p.metric:
        problems.append("policy JSON does not match the model")
    if cfg["ScaleOutCooldown"] > cfg["ScaleInCooldown"]:
        problems.append("scale-out cooldown longer than scale-in: reacts slowly to load, flaps on the way down")
    if "--min-capacity" not in shlex.split(cmds[0]) or "ecs:service:DesiredCount" not in shlex.split(cmds[1]):
        problems.append("registration/policy commands incomplete")
    return problems


# ---- Syllabus extension: workflow YAML (GitHub Actions) as a typed model, emitted by our own YAML emitter ---

class WfStep(BaseModel):
    name: str
    id: str | None = None
    if_: str | None = None
    uses: str | None = None
    run: str | None = None
    with_: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _uses_xor_run(self) -> "WfStep":
        if (self.uses is None) == (self.run is None):
            raise ValueError("step %s needs exactly one of uses/run" % self.name)
        return self


class WfJob(BaseModel):
    runs_on: str = "ubuntu-latest"
    needs: list[str] = Field(default_factory=list)
    if_: str | None = None
    permissions: dict[str, str] | None = None
    environment: str | None = None
    timeout_minutes: int | None = 15
    outputs: dict[str, str] = Field(default_factory=dict)
    steps: list[WfStep]


class WfTriggers(BaseModel):
    push_branches: list[str] = Field(default_factory=list)
    push_tags: list[str] = Field(default_factory=list)
    push_paths: list[str] = Field(default_factory=list)
    pr_branches: list[str] = Field(default_factory=list)
    pr_paths: list[str] = Field(default_factory=list)
    manual: bool = False
    cron: list[str] = Field(default_factory=list)


class Workflow(BaseModel):
    name: str
    triggers: WfTriggers
    permissions: dict[str, str] = Field(default_factory=lambda: {"contents": "read"})
    env: dict[str, str] = Field(default_factory=dict)
    concurrency: dict[str, Any] | None = None
    jobs: dict[str, WfJob]

    def to_dict(self) -> dict[str, Any]:
        t, on = self.triggers, {}
        if t.push_branches or t.push_tags or t.push_paths:
            push: dict[str, Any] = {}
            for key, vals in (("branches", t.push_branches), ("tags", t.push_tags), ("paths", t.push_paths)):
                if vals:
                    push[key] = vals
            on["push"] = push
        if t.pr_branches or t.pr_paths:
            pr: dict[str, Any] = {}
            if t.pr_branches:
                pr["branches"] = t.pr_branches
            if t.pr_paths:
                pr["paths"] = t.pr_paths
            on["pull_request"] = pr
        if t.manual:
            on["workflow_dispatch"] = {}
        if t.cron:
            on["schedule"] = [{"cron": c} for c in t.cron]
        doc: dict[str, Any] = {"name": self.name, "on": on, "permissions": self.permissions}
        if self.env:
            doc["env"] = self.env
        if self.concurrency:
            doc["concurrency"] = self.concurrency
        jobs: dict[str, Any] = {}
        for jid, j in self.jobs.items():
            jd: dict[str, Any] = {"runs-on": j.runs_on}
            if j.needs:
                jd["needs"] = j.needs
            if j.if_:
                jd["if"] = j.if_
            if j.permissions is not None:
                jd["permissions"] = j.permissions
            if j.environment:
                jd["environment"] = j.environment
            if j.timeout_minutes:
                jd["timeout-minutes"] = j.timeout_minutes
            if j.outputs:
                jd["outputs"] = j.outputs
            steps: list[dict[str, Any]] = []
            for s in j.steps:
                sd: dict[str, Any] = {"name": s.name}
                for key, val in (("id", s.id), ("if", s.if_), ("uses", s.uses)):
                    if val:
                        sd[key] = val
                if s.with_:
                    sd["with"] = s.with_
                if s.run is not None:
                    sd["run"] = s.run
                if s.env:
                    sd["env"] = s.env
                steps.append(sd)
            jd["steps"] = steps
            jobs[jid] = jd
        doc["jobs"] = jobs
        return doc


TRUSTED_ACTION_OWNERS = {"actions", "aws-actions", "docker", "github"}
INJECTABLE = re.compile(r"\$\{\{\s*github\.(event\.(issue\.(title|body)|pull_request\.(title|body|head\.ref|head\.label)|comment\.body|review\.body|head_commit\.message)|head_ref)\s*\}\}")
SECRET_REF = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")


def _walk_strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _walk_strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _walk_strings(v)]
    return []


def validate_workflow(doc: dict[str, Any], require_sha_pins: bool = False) -> list[Finding]:
    """Security/hygiene audit of a parsed workflow (works on emitted YAML re-parsed by parse_yaml)."""
    f: list[Finding] = []

    def add(rule: str, sev: Severity, msg: str) -> None:
        f.append(Finding(rule=rule, severity=sev, message=msg))

    on = doc.get("on")
    if not isinstance(on, dict) or not on:
        add("trigger", Severity.HIGH, "workflow has no trigger")
        on = {}
    top_perms = doc.get("permissions")
    if top_perms is None:
        add("permissions", Severity.MEDIUM, "no top-level permissions: GITHUB_TOKEN gets the repository default")
    elif top_perms == "write-all" or (isinstance(top_perms, dict) and sum(v == "write" for v in top_perms.values()) > 2):
        add("permissions", Severity.HIGH, "broad write permissions at workflow level")
    for lit in re.findall(r"AKIA[0-9A-Z]{16}", " ".join(_walk_strings(doc))):
        add("hardcoded-key", Severity.CRITICAL, "AWS access key id literal in workflow: " + lit[:4] + "...")
    for jid, job in (doc.get("jobs") or {}).items():
        steps = job.get("steps") or []
        if "runs-on" not in job or not steps:
            add("job-shape", Severity.HIGH, "%s needs runs-on and steps" % jid)
        if not job.get("timeout-minutes"):
            add("timeout", Severity.LOW, "%s has no timeout-minutes (default is 360)" % jid)
        perms = job.get("permissions", top_perms)
        perms = perms if isinstance(perms, dict) else {}
        secret_env = {k for k, v in {**(doc.get("env") or {}), **{k: v for s in steps for k, v in (s.get("env") or {}).items()}}.items() if "secrets." in str(v)}
        uses_aws_creds = False
        for s in steps:
            label = "%s/%s" % (jid, s.get("name", s.get("uses", "run")))
            uses = s.get("uses")
            if uses and not uses.startswith(("./", "docker://")):
                owner = uses.split("/")[0]
                ref = uses.split("@", 1)[1] if "@" in uses else ""
                if not ref or ref in ("main", "master", "HEAD"):
                    add("action-pin", Severity.HIGH, "%s uses %s with no immutable ref" % (label, uses))
                elif not re.fullmatch(r"[0-9a-f]{40}", ref):
                    sev = Severity.HIGH if require_sha_pins else (Severity.LOW if owner in TRUSTED_ACTION_OWNERS else Severity.MEDIUM)
                    add("action-pin", sev, "%s pins %s to a mutable tag; pin the full commit SHA for strict supply-chain posture" % (label, uses))
            w = s.get("with") or {}
            if uses and uses.startswith("aws-actions/configure-aws-credentials"):
                uses_aws_creds = True
                if "aws-access-key-id" in w or "aws-secret-access-key" in w:
                    add("static-aws-keys", Severity.HIGH, "%s uses long-lived AWS keys; assume a role via OIDC instead" % label)
                if "role-to-assume" not in w or "aws-region" not in w:
                    add("oidc-inputs", Severity.HIGH, "%s needs role-to-assume and aws-region" % label)
                if perms.get("id-token") != "write":
                    add("oidc-permission", Severity.HIGH, "%s: job lacks permissions.id-token: write" % label)
                if perms and perms.get("contents") != "read":
                    add("checkout-permission", Severity.MEDIUM, "%s: job-level permissions omit contents: read, so checkout can fail" % label)
            script = s.get("run")
            if script:
                if "${{ secrets." in script or "${{secrets." in script:
                    add("secret-in-script", Severity.HIGH, "%s interpolates a secret into the shell script; pass it through env:" % label)
                if INJECTABLE.search(script):
                    add("script-injection", Severity.HIGH, "%s interpolates attacker-controlled event data into a script; pass it through env:" % label)
                scope = secret_env | {k for k, v in (s.get("env") or {}).items() if "secrets." in str(v)}
                for name in scope:
                    if re.search(r"\b(echo|printf)\b[^\n]*\$\{?%s\b" % re.escape(name), script):
                        add("secret-echo", Severity.HIGH, "%s prints secret-backed variable %s" % (label, name))
                if scope and re.search(r"\bset -[a-z]*x|\bprintenv\b|^env\s*$|\benv\s*\|", script, re.M):
                    add("secret-echo", Severity.HIGH, "%s enables shell tracing or dumps the environment while secrets are in scope" % label)
            if "pull_request_target" in on and uses and uses.startswith("actions/checkout") and "head" in str(w.get("ref", "")):
                add("pwn-request", Severity.HIGH, "%s checks out untrusted PR code under pull_request_target" % label)
        if perms.get("id-token") == "write" and not uses_aws_creds:
            add("id-token-unused", Severity.MEDIUM, "%s requests id-token: write but never assumes a role" % jid)
        if any("amazon-ecs-deploy-task-definition" in str(s.get("uses", "")) for s in steps):
            if not job.get("environment"):
                add("deploy-environment", Severity.MEDIUM, "%s deploys without a protected environment (approvals, scoped secrets)" % jid)
            if not job.get("needs"):
                add("deploy-needs", Severity.HIGH, "%s deploys without depending on test/build jobs" % jid)
    return f


# ---- Syllabus extension: managing GitHub secrets ---

SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_secret_name(name: str) -> None:
    """GitHub rules: alphanumerics and underscore only, no leading digit, must not start with GITHUB_."""
    if not SECRET_NAME_RE.match(name) or name.upper().startswith("GITHUB_"):
        raise PipelineError("invalid secret name %r" % name)


def collect_secret_refs(doc: dict[str, Any]) -> dict[str, list[str]]:
    """secret name -> where the workflow references it (GITHUB_TOKEN is built in and excluded)."""
    refs: dict[str, list[str]] = {}
    for jid, job in (doc.get("jobs") or {}).items():
        for idx, s in enumerate(job.get("steps") or []):
            for name in SECRET_REF.findall(" ".join(_walk_strings(s))):
                if name != "GITHUB_TOKEN":
                    refs.setdefault(name, []).append("%s/step%d" % (jid, idx))
    return refs


def reconcile_secrets(declared: set[str], refs: dict[str, list[str]]) -> list[Finding]:
    f: list[Finding] = []
    for name in sorted(refs):
        validate_secret_name(name)
        if name not in declared:
            f.append(Finding(rule="secret-missing", severity=Severity.HIGH, message="%s is referenced (%s) but not defined; the step would get an empty string" % (name, ", ".join(refs[name]))))
    for name in sorted(declared - set(refs)):
        f.append(Finding(rule="secret-unused", severity=Severity.LOW, message="%s is defined but never referenced; delete it" % name))
    return f


class LogMasker:
    """Models runner log masking: registered values are replaced with ***. Derived values (base64, substrings) are NOT
    masked unless also registered, which is why secrets must never be transformed and printed."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, value: str) -> None:
        if len(value) >= 4:
            self._values.add(value)

    def mask(self, text: str) -> str:
        for v in sorted(self._values, key=len, reverse=True):
            text = text.replace(v, "***")
        return text


def github_oidc_trust(account_id: str, org: str, repo: str, *, branch: str | None = None, environment: str | None = None) -> dict[str, Any]:
    """Role trust policy that lets one repo (and one branch or environment) assume an AWS role via GitHub OIDC."""
    if (branch is None) == (environment is None):
        raise PipelineError("pin the role to exactly one of branch or environment")
    sub = "repo:%s/%s:environment:%s" % (org, repo, environment) if environment else "repo:%s/%s:ref:refs/heads/%s" % (org, repo, branch)
    host = "token.actions.githubusercontent.com"
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Federated": "arn:aws:iam::%s:oidc-provider/%s" % (account_id, host)},
        "Action": "sts:AssumeRoleWithWebIdentity",
        "Condition": {"StringEquals": {host + ":aud": "sts.amazonaws.com", host + ":sub": sub}}}]}


def oidc_allows(trust: dict[str, Any], claims: dict[str, str]) -> tuple[bool, str]:
    """Evaluates the aud/sub conditions of a GitHub OIDC trust policy against a token's claims."""
    for st in trust["Statement"]:
        ok = True
        for op, conds in st.get("Condition", {}).items():
            for key, want in conds.items():
                claim = key.split(":", 1)[1]
                got = claims.get(claim, "")
                match = fnmatch.fnmatchcase(got, want) if op == "StringLike" else got == want
                if not match:
                    return False, "%s does not satisfy %s on %s" % (claim, op, key)
        if ok and st["Effect"] == "Allow":
            return True, "ok"
    return False, "no statement allows"


def audit_oidc_trust(trust: dict[str, Any]) -> list[Finding]:
    f: list[Finding] = []
    for st in trust["Statement"]:
        for op, conds in st.get("Condition", {}).items():
            for key, want in conds.items():
                if key.endswith(":sub") and (op == "StringLike" and want in ("*", "repo:*") or want.endswith(":*") or "/*" in want):
                    f.append(Finding(rule="oidc-sub", severity=Severity.HIGH, message="sub condition %s trusts many repositories" % want))
        if not any(k.endswith(":sub") for conds in st.get("Condition", {}).values() for k in conds):
            f.append(Finding(rule="oidc-sub", severity=Severity.CRITICAL, message="no sub condition: any GitHub repository can assume this role"))
    return f


# ---- Syllabus extension: triggering automated builds (push / PR / path filters) ---

class GitEvent(BaseModel):
    kind: Literal["push", "pull_request", "workflow_dispatch", "schedule"]
    branch: str | None = None  # push: the branch pushed; pull_request: the BASE branch
    tag: str | None = None
    changed_paths: list[str] = Field(default_factory=list)


def gh_glob(pattern: str) -> re.Pattern[str]:
    """GitHub filter globs: * stops at /, ** crosses /, ? is one non-/ char."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile("^" + out + "$")


def _any_glob(patterns: list[str], text: str) -> bool:
    return any(gh_glob(p).match(text) for p in patterns)


def _path_filter_ok(spec: dict[str, Any], changed: list[str]) -> bool:
    if "paths" in spec:
        pats: list[str] = spec["paths"]

        def included(path: str) -> bool:
            verdict = False
            for p in pats:  # later patterns win; "!" negates
                if p.startswith("!"):
                    if gh_glob(p[1:]).match(path):
                        verdict = False
                elif gh_glob(p).match(path):
                    verdict = True
            return verdict
        return any(included(c) for c in changed)
    if "paths-ignore" in spec:
        return any(not _any_glob(spec["paths-ignore"], c) for c in changed)
    return True


def workflow_triggers(on: dict[str, Any], ev: GitEvent) -> tuple[bool, str]:
    """Would this `on:` block start a run for this event?"""
    if ev.kind not in on:
        return False, "no %s trigger" % ev.kind
    spec = on[ev.kind] or {}
    if ev.kind in ("workflow_dispatch", "schedule"):
        return True, "manual or scheduled"
    if ev.tag:
        if "tags" not in spec and ("branches" in spec or "branches-ignore" in spec):
            return False, "tag push but only branch filters are defined"
        if "tags" in spec and not _any_glob(spec["tags"], ev.tag):
            return False, "tag %s does not match" % ev.tag
        return True, "tag matched"  # path filters are not evaluated for tag pushes
    if "tags" in spec and "branches" not in spec:
        return False, "only tag filters are defined"
    branch = ev.branch or ""
    if "branches" in spec and not _any_glob(spec["branches"], branch):
        return False, "branch %s does not match" % branch
    if "branches-ignore" in spec and _any_glob(spec["branches-ignore"], branch):
        return False, "branch %s is ignored" % branch
    if not _path_filter_ok(spec, ev.changed_paths):
        return False, "no changed path passes the path filters"
    return True, "matched"


def eval_condition(expr: str, ev: GitEvent) -> bool:
    """Tiny evaluator for `if:` expressions of the form  a == 'x' && b == 'y'. Anything else raises instead of guessing."""
    ctx = {"github.event_name": ev.kind, "github.ref": ("refs/heads/%s" % ev.branch) if ev.branch else ("refs/tags/%s" % ev.tag if ev.tag else "")}
    for part in expr.split("&&"):
        m = re.fullmatch(r"\s*([\w.]+)\s*==\s*'([^']*)'\s*", part)
        if not m or m.group(1) not in ctx:
            raise PipelineError("unsupported if-expression: " + expr)
        if ctx[m.group(1)] != m.group(2):
            return False
    return True


def jobs_to_run(doc: dict[str, Any], ev: GitEvent) -> tuple[list[str], str]:
    """Jobs that execute for `ev`, honoring triggers, `needs` ordering and job-level `if`."""
    fired, why = workflow_triggers(doc["on"], ev)
    if not fired:
        return [], why
    ran: list[str] = []
    pending = dict(doc["jobs"])
    while pending:
        progressed = False
        for jid, job in list(pending.items()):
            needs = job.get("needs") or []
            if any(n in pending for n in needs):
                continue
            del pending[jid]
            progressed = True
            if all(n in ran for n in needs) and (not job.get("if") or eval_condition(job["if"], ev)):
                ran.append(jid)
        if not progressed:
            raise PipelineError("needs cycle in workflow jobs")
    return ran, why


# ---- Syllabus extension: continuous deployment to AWS with rollback ---

def build_ci_cd_workflow() -> Workflow:
    """CI on every PR; build, push to ECR and deploy to ECS only on main. AWS access is OIDC role assumption, no stored keys."""
    watched = ["app/**", "requirements.txt", "Dockerfile", ".github/workflows/ci-cd.yml"]
    checkout = WfStep(name="Check out", uses="actions/checkout@v4")
    creds = WfStep(name="Assume AWS role via OIDC", uses="aws-actions/configure-aws-credentials@v4",
                   with_={"role-to-assume": "${{ secrets.AWS_DEPLOY_ROLE_ARN }}", "aws-region": "${{ env.AWS_REGION }}"})
    on_main = "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    oidc_perms = {"contents": "read", "id-token": "write"}
    return Workflow(
        name="ci-cd",
        triggers=WfTriggers(push_branches=["main"], push_paths=watched, pr_branches=["main"], pr_paths=watched, manual=True),
        env={"AWS_REGION": "us-east-1", "ECR_REPOSITORY": "fde-api", "ECS_CLUSTER": "fde", "ECS_SERVICE": "fde-api"},
        concurrency={"group": "ci-cd-${{ github.ref }}", "cancel-in-progress": True},
        jobs={
            "test": WfJob(steps=[
                checkout,
                WfStep(name="Set up Python", uses="actions/setup-python@v5", with_={"python-version": "3.12", "cache": "pip"}),
                WfStep(name="Install", run="pip install -r requirements.txt"),
                WfStep(name="Test", run="pytest -q", env={"API_TOKEN": "${{ secrets.INTEGRATION_API_TOKEN }}"}),
            ]),
            "build": WfJob(needs=["test"], if_=on_main, permissions=oidc_perms, outputs={"image": "${{ steps.meta.outputs.image }}"}, steps=[
                checkout, creds,
                WfStep(name="Log in to ECR", id="ecr", uses="aws-actions/amazon-ecr-login@v2"),
                WfStep(name="Compute image name", id="meta", env={"REGISTRY": "${{ steps.ecr.outputs.registry }}"},
                       run='echo "image=${REGISTRY}/${ECR_REPOSITORY}:${GITHUB_SHA}" >> "$GITHUB_OUTPUT"'),
                WfStep(name="Build and push", uses="docker/build-push-action@v6",
                       with_={"context": ".", "push": "true", "tags": "${{ steps.meta.outputs.image }}"}),
            ]),
            "deploy": WfJob(needs=["build"], if_=on_main, permissions=oidc_perms, environment="production", timeout_minutes=30, steps=[
                checkout, creds,
                WfStep(name="Render task definition", id="render", uses="aws-actions/amazon-ecs-render-task-definition@v1",
                       with_={"task-definition": "infra/taskdef.json", "container-name": "api", "image": "${{ needs.build.outputs.image }}"}),
                WfStep(name="Deploy to ECS", uses="aws-actions/amazon-ecs-deploy-task-definition@v2",
                       with_={"task-definition": "${{ steps.render.outputs.task-definition }}", "service": "${{ env.ECS_SERVICE }}",
                              "cluster": "${{ env.ECS_CLUSTER }}", "wait-for-service-stability": "true"}),
            ]),
        })


class RolloutResult(BaseModel):
    steps: list[str]
    old_tasks: int
    new_tasks: int
    rolled_back: bool


def plan_rolling_deploy(desired: int, max_percent: int, min_healthy_percent: int, new_is_healthy: bool, failure_threshold: int = 3) -> RolloutResult:
    """ECS-style rolling update. New tasks may exceed `desired` up to max_percent; healthy tasks never drop below
    min_healthy_percent. If new tasks keep failing, the circuit breaker stops the rollout and keeps the old version."""
    max_total = desired * max_percent // 100
    min_total = -(-desired * min_healthy_percent // 100)
    if max_total <= desired and min_total >= desired:
        raise DeploymentConfigError("no room to start new tasks: raise maximumPercent or lower minimumHealthyPercent")
    old, new, failed, steps = desired, 0, 0, []
    for _ in range(200):
        if new == desired and old == 0:
            return RolloutResult(steps=steps, old_tasks=0, new_tasks=new, rolled_back=False)
        launch = min(desired - new, max(max_total - old - new, 0))
        stop = 0
        if launch:
            if not new_is_healthy:
                failed += launch
                steps.append("start %d new, health check fails (%d/%d failures)" % (launch, failed, failure_threshold))
                if failed >= failure_threshold:
                    steps.append("circuit breaker trips: roll back, %d old tasks keep serving" % old)
                    return RolloutResult(steps=steps, old_tasks=old, new_tasks=0, rolled_back=True)
                continue
            new += launch
            steps.append("start %d new, healthy (old=%d new=%d)" % (launch, old, new))
        stop = min(old, max(old + new - min_total, 0))
        if stop:
            old -= stop
            steps.append("stop %d old (old=%d new=%d)" % (stop, old, new))
        if not launch and not stop:
            raise DeploymentConfigError("rollout stalled at old=%d new=%d" % (old, new))
    raise DeploymentConfigError("rollout did not converge")


class FakeEcsApi:
    """Offline stand-in for the ECS control plane: throttles the first calls, then records the active version."""

    def __init__(self, active: str, throttle_first: int = 1) -> None:
        self.active, self._throttle, self.calls = active, throttle_first, 0

    async def update_service(self, version: str) -> None:
        self.calls += 1
        await asyncio.sleep(0.001)
        if self.calls <= self._throttle:
            raise TransientApiError("ThrottlingException: rate exceeded")
        self.active = version


class DeployOutcome(BaseModel):
    candidate: str
    active: str
    status: Literal["deployed", "rolled-back"]
    steps: list[str]


async def deploy_with_rollback(api: FakeEcsApi, current: str, candidate: str, candidate_healthy: bool, desired: int = 4,
                               max_percent: int = 200, min_healthy_percent: int = 100) -> DeployOutcome:
    """Continuous deployment step: push the new version, watch the rollout, restore the previous version on failure."""
    plan = plan_rolling_deploy(desired, max_percent, min_healthy_percent, candidate_healthy)
    await backoff_call(lambda: api.update_service(candidate))
    if plan.rolled_back:
        await backoff_call(lambda: api.update_service(current))
        return DeployOutcome(candidate=candidate, active=api.active, status="rolled-back", steps=plan.steps)
    return DeployOutcome(candidate=candidate, active=api.active, status="deployed", steps=plan.steps)


async def ecs_deploy_boto3(cluster: str, service: str, task_definition: str, region: str = "us-east-1") -> None:
    """Real adapter: point the service at a task definition revision and wait until it is stable. Not run offline."""
    client = lazy_boto3_client("ecs", region)
    await call_aws(client.update_service, cluster=cluster, service=service, taskDefinition=task_definition)
    await call_aws(client.get_waiter("services_stable").wait, cluster=cluster, services=[service])


# ---- Syllabus extension: demo ---

async def extension_demo() -> None:
    """Runs every syllabus-extension section offline and logs a verdict per topic."""
    # 1. Optimized Dockerfiles
    naive, tuned = parse_dockerfile(NAIVE_DOCKERFILE), parse_dockerfile(FASTAPI_DOCKERFILE)
    edit = ["app/main.py"]
    rb_naive, rb_tuned = simulate_build_cache(naive, edit), simulate_build_cache(tuned, edit)
    log.info("dockerfile optimization", extra={"ctx": {
        "naive_findings": [x.rule for x in optimization_findings(naive, has_dockerignore=False)],
        "tuned_findings": [x.rule for x in optimization_findings(tuned, has_dockerignore=True)],
        "naive_rebuilds_on_code_edit": rb_naive, "tuned_rebuilds_on_code_edit": rb_tuned,
        "expensive_RUN_steps": {"naive": sum(":RUN" in r for r in rb_naive), "tuned": sum(":RUN" in r for r in rb_tuned)}}})

    # 2-3. Multi-container environments, volumes, networking
    compose_text = """services:
  api:
    image: fde-api:1.0.0
    ports:
      - "127.0.0.1:8000:8000"
    environment:
      DATABASE_URL: postgresql://app@db:5432/app
      REDIS_URL: redis://cache:6379/0
    depends_on:
      db:
        condition: service_healthy
      cache:
        condition: service_started
    networks: [frontend, backend]
    read_only: true
    volumes:
      - ./config:/etc/app:ro
      - uploads:/srv/uploads
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]
      interval: 10s
      timeout: 3s
      retries: 3
  db:
    image: postgres:16
    networks: [backend]
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app"]
      interval: 5s
      timeout: 3s
      retries: 5
      start_period: 10s
  cache:
    image: redis:7
    networks: [backend]
  web:
    image: nginx:1.27
    ports: ["8080:80"]
    networks: [frontend]
volumes:
  pgdata:
  uploads:
networks:
  frontend:
  backend:
    internal: true
"""
    proj = load_compose(compose_text)
    timeline = simulate_startup(proj, {"db": 4.0, "cache": 1.0, "api": 3.0, "web": 1.0})
    log.info("compose", extra={"ctx": {
        "layers": startup_layers(proj), "findings": [(x.rule, x.severity.value) for x in validate_compose(proj)],
        "web->api": can_reach(proj, "web", "api"), "web->db": can_reach(proj, "web", "db"), "api->db": can_reach(proj, "api", "db"),
        "volumes_api": volume_report(proj)["api"]["persistent_named"], "api_starts_at_s": timeline["api"][0], "api_healthy_at_s": timeline["api"][1],
        "docker_run_equivalent": compose_to_docker_run(proj, "db")}})
    bad_cases = {
        "cycle": "services:\n  a:\n    image: x:1\n    depends_on: [b]\n  b:\n    image: y:1\n    depends_on: [a]\n",
        "undeclared volume": "services:\n  a:\n    image: x:1\n    volumes:\n      - data:/d\n",
        "healthy without healthcheck": "services:\n  a:\n    image: x:1\n    depends_on:\n      b:\n        condition: service_healthy\n  b:\n    image: y:1\n",
        "port clash": "services:\n  a:\n    image: x:1\n    ports: ['80:80']\n  b:\n    image: y:1\n    ports: ['80:8080']\n",
    }
    caught: dict[str, str] = {}
    for label, text in bad_cases.items():
        try:
            validate_compose(load_compose(text))
            caught[label] = "NOT CAUGHT"
        except ComposeError as exc:
            caught[label] = str(exc)
    risky = load_compose("services:\n  db:\n    image: postgres:latest\n    ports: ['5432:5432']\n    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n"
                         "    environment:\n      POSTGRES_PASSWORD: hunter2\n")
    log.info("compose rejects", extra={"ctx": {"errors": caught, "risky_findings": sorted(x.rule for x in validate_compose(risky))}})

    # 4. FastAPI container
    lint = lint_dockerfile(tuned)
    log.info("fastapi container", extra={"ctx": {"lint": [(x.rule, x.severity.value) for x in lint], "blocking": has_blocking(lint),
                                                 "contract_problems": fastapi_contract_check(FASTAPI_DOCKERFILE, FASTAPI_APP_PY),
                                                 "broken_port_problems": fastapi_contract_check(FASTAPI_DOCKERFILE.replace('"--port", "8000"', '"--port", "9000"'), FASTAPI_APP_PY)}})
    try:
        log.info("fastapi smoke test", extra={"ctx": {"status_codes": run_fastapi_smoke_test(FASTAPI_APP_PY)}})
    except OptionalDependencyError as exc:
        log.info("fastapi smoke test skipped", extra={"ctx": {"hint": str(exc)}})

    # 5. Serverless container concepts
    for label, w in {"nightly report (10 min, scale to zero)": WorkloadProfile(max_duration_s=600, vcpu=1, memory_mib=2048, needs_scale_to_zero=True),
                     "FastAPI service (always on)": WorkloadProfile(max_duration_s=30, vcpu=0.5, memory_mib=1500, steady_24x7=True),
                     "model training (GPU)": WorkloadProfile(max_duration_s=7200, vcpu=8, memory_mib=32768, needs_gpu=True),
                     "websocket gateway": WorkloadProfile(max_duration_s=3600, vcpu=1, memory_mib=2048, long_lived_connections=True)}.items():
        choice, reasons = choose_runtime(w)
        log.info("runtime choice", extra={"ctx": {"workload": label, "runtime": choice, "why": reasons}})

    # 6. Task definition
    td = TaskDefinition(
        family="fde-api", cpu=512, memory_mib=1024, execution_role_arn="arn:aws:iam::111122223333:role/ecsTaskExecutionRole",
        task_role_arn="arn:aws:iam::111122223333:role/fde-api-task",
        containers=[ContainerDef(
            name="api", image="111122223333.dkr.ecr.us-east-1.amazonaws.com/fde-api:" + "a1b2c3d4", cpu=512, memory_mib=1024, ports=[8000],
            environment={"LOG_LEVEL": "info"}, readonly_root_fs=True, user="10001",
            secrets=[SecretRef(name="DATABASE_PASSWORD", value_from="arn:aws:secretsmanager:us-east-1:111122223333:secret:fde/db-AbCdEf")],
            log=LogConfig(options={"awslogs-group": "/ecs/fde-api", "awslogs-region": "us-east-1", "awslogs-stream-prefix": "api"}),
            health=ContainerHealth(command=["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')\""],
                                   interval_s=30, timeout_s=5, retries=3, start_period_s=10))])
    doc = taskdef_json(td)
    broken = td.model_copy(update={"cpu": 256, "memory_mib": 4096, "execution_role_arn": None})
    log.info("task definition", extra={"ctx": {"findings": [(x.rule, x.severity.value) for x in validate_task_definition(td)],
                                               "json_problems": validate_taskdef_json(doc), "json_roundtrip": json.loads(json.dumps(doc)) == doc,
                                               "broken": sorted(x.rule for x in validate_task_definition(broken)),
                                               "smallest_for_1vcpu_3gib": smallest_fargate_size(1, 3072)}})

    # 7. Load balancer integration
    tg = TargetGroup(name="api", port=8000, health_path="/readyz", interval_s=10, timeout_s=4, healthy_threshold=2, unhealthy_threshold=2,
                     matcher="200-299", deregistration_delay_s=30, targets=["10.0.1.11", "10.0.2.12"])
    admin = TargetGroup(name="admin", port=9000, targets=["10.0.1.21"], interval_s=10, timeout_s=4, healthy_threshold=2)
    lb = LoadBalancerSim(
        rules=[ListenerRule(priority=10, path_patterns=["/admin/*"], host_headers=["*.example.com"], action=RuleAction(target_groups={"admin": 1})),
               ListenerRule(priority=20, path_patterns=["/old/*"], action=RuleAction(type="redirect", status_code=301, location="/v1/"))],
        default=RuleAction(target_groups={"api": 1}), groups={"api": tg, "admin": admin})
    before = lb.route(HttpRequest(host="api.example.com", path="/v1/x"))
    for target in tg.targets:  # two passing probes each mark both api tasks healthy
        lb.trackers["api"].probe(target, 200)
        lb.trackers["api"].probe(target, 200)
    picks = [lb.route(HttpRequest(host="api.example.com", path="/v1/x")).target for _ in range(4)]
    lb.trackers["api"].probe("10.0.1.11", 500)
    lb.trackers["api"].probe("10.0.1.11", None)  # a timeout counts as a failure
    after_fail = [lb.route(HttpRequest(host="api.example.com", path="/v1/x")).target for _ in range(2)]
    svc = EcsServiceSpec(cluster="fde", name="fde-api", task_family="fde-api", desired_count=4, subnets=["subnet-0a1b2c3d", "subnet-1b2c3d4e"],
                         security_groups=["sg-0a1b2c3d"], lb_target_group_arn="arn:aws:elasticloadbalancing:us-east-1:111122223333:targetgroup/api/0123456789abcdef",
                         lb_container="api", lb_port=8000, health_grace_s=60)
    bad_svc = svc.model_copy(update={"lb_port": 8080, "health_grace_s": 0})
    cli = ecs_service_cli(svc)
    log.info("load balancer", extra={"ctx": {
        "fail_open_before_probes": (before.target, before.fail_open), "round_robin_healthy": picks, "after_one_target_fails": after_fail,
        "admin_rule": lb.route(HttpRequest(host="ops.example.com", path="/admin/ui")).rule, "redirect": lb.route(HttpRequest(host="a.example.com", path="/old/z")).status_code,
        "seconds_to_mark_unhealthy": tg.seconds_to_mark_unhealthy, "seconds_to_mark_healthy": tg.seconds_to_mark_healthy,
        "wiring_ok": [x.rule for x in check_service_wiring(td, tg, svc)], "wiring_bad": sorted(x.rule for x in check_service_wiring(td, tg, bad_svc)),
        "cli_problems": validate_service_cli(cli, svc)}})
    try:
        TargetGroup(name="bad", port=80, interval_s=10, timeout_s=10)
    except ValueError:
        log.info("target group rejected", extra={"ctx": {"why": "timeout must be shorter than interval"}})

    # 8. Auto-scaling
    spike = [100.0] * 2 + [250.0] * 3 + [550.0] * 4 + [100.0] * 14
    pol = ScalingPolicy(cluster="fde", service="fde-api", min_capacity=2, max_capacity=10, metric="ECSServiceAverageCPUUtilization",
                        target_value=60, scale_out_cooldown_s=60, scale_in_cooldown_s=300)
    sluggish = pol.model_copy(update={"scale_out_cooldown_s": 600})
    run, slow = simulate_autoscaling(pol, spike, capacity_per_task=100.0), simulate_autoscaling(sluggish, spike, capacity_per_task=100.0)
    cmds, cfg = scaling_artifacts(pol)
    log.info("autoscaling", extra={"ctx": {
        "desired_4tasks_at_90pct_target60": desired_capacity(4, 90, pol), "events": [(e.t_s, e.from_tasks, e.to_tasks, e.metric) for e in run.events],
        "saturated_ticks": {"cooldown60": run.saturated_ticks, "cooldown600": slow.saturated_ticks},
        "final_tasks": run.tasks_by_tick[-1], "artifact_problems": validate_scaling_artifacts(pol, cmds, cfg)}})

    # 9-10. Workflow YAML and automated build triggers
    wf = build_ci_cd_workflow()
    text = emit_yaml(wf.to_dict())
    reparsed = parse_yaml(text)
    wf_findings = validate_workflow(reparsed)
    log.info("workflow yaml", extra={"ctx": {"yaml_lines": len(text.splitlines()), "roundtrip_ok": reparsed == wf.to_dict(), "head": text.splitlines()[:6],
                                             "findings": [(x.rule, x.severity.value) for x in wf_findings], "strict_pins_blocking": has_blocking(validate_workflow(reparsed, require_sha_pins=True))}})
    events = {
        "PR touching app code": GitEvent(kind="pull_request", branch="main", changed_paths=["app/api.py"]),
        "PR touching docs only": GitEvent(kind="pull_request", branch="main", changed_paths=["docs/readme.md"]),
        "push to main (app change)": GitEvent(kind="push", branch="main", changed_paths=["app/api.py", "docs/a.md"]),
        "push to main (docs only)": GitEvent(kind="push", branch="main", changed_paths=["docs/a.md"]),
        "push to feature branch": GitEvent(kind="push", branch="feature/x", changed_paths=["app/api.py"]),
        "tag push v1.0": GitEvent(kind="push", tag="v1.0"),
        "manual dispatch on main": GitEvent(kind="workflow_dispatch", branch="main"),
    }
    log.info("build triggers", extra={"ctx": {label: {"jobs": jobs_to_run(reparsed, ev)[0], "why": jobs_to_run(reparsed, ev)[1]} for label, ev in events.items()}})

    # 11. Managing GitHub secrets + OIDC role assumption
    refs = collect_secret_refs(reparsed)
    masker = LogMasker()
    fake_secret = "demo-not-a-real-secret-123"
    masker.register(fake_secret)
    leaky_line = "deploying with token " + fake_secret
    derived = "".join(reversed(fake_secret))  # a transformed copy slips past masking
    trust = github_oidc_trust("111122223333", "acme", "fde-api", environment="production")
    wide = github_oidc_trust("111122223333", "acme", "fde-api", branch="main")
    wide["Statement"][0]["Condition"] = {"StringLike": {"token.actions.githubusercontent.com:sub": "repo:acme/*", "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"}}
    log.info("github secrets", extra={"ctx": {
        "referenced": {k: len(v) for k, v in refs.items()},
        "reconcile": [(x.rule, x.severity.value) for x in reconcile_secrets({"AWS_DEPLOY_ROLE_ARN", "OLD_UNUSED_KEY"}, refs)],
        "masked": masker.mask(leaky_line), "derived_value_still_visible": masker.mask(derived) == derived,
        "oidc_prod_env": oidc_allows(trust, {"aud": "sts.amazonaws.com", "sub": "repo:acme/fde-api:environment:production"}),
        "oidc_other_repo": oidc_allows(trust, {"aud": "sts.amazonaws.com", "sub": "repo:acme/other:environment:production"}),
        "oidc_wide_findings": [x.message for x in audit_oidc_trust(wide)]}})
    for bad in ("1BAD", "GITHUB_TOKEN2", "has-dash"):
        try:
            validate_secret_name(bad)
        except PipelineError as exc:
            log.info("secret name rejected", extra={"ctx": {"error": str(exc)}})
    fake_key = "AKIA" + "ABCDEFGHIJKLMNOP"
    risky_yaml = (
        "name: risky\non:\n  pull_request_target:\n    branches: [main]\njobs:\n  deploy:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@v4\n        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n"
        "      - uses: sketchy/deploy-action@main\n        with:\n          aws-access-key-id: " + fake_key + "\n"
        "      - uses: aws-actions/configure-aws-credentials@v4\n        with:\n          aws-access-key-id: x\n"
        "      - name: leak\n        env:\n          TOKEN: ${{ secrets.DEPLOY_TOKEN }}\n        run: |\n          set -x\n          echo \"token is $TOKEN\"\n"
        "      - run: echo \"${{ github.event.pull_request.title }}\"\n")
    log.info("risky workflow", extra={"ctx": {"rules": sorted({x.rule for x in validate_workflow(parse_yaml(risky_yaml))})}})

    # 12. Continuous deployment with rollback
    api = FakeEcsApi(active="v41", throttle_first=2)
    good = await deploy_with_rollback(api, "v41", "v42", candidate_healthy=True)
    bad = await deploy_with_rollback(api, "v42", "v43", candidate_healthy=False)
    log.info("continuous deployment", extra={"ctx": {"good": good.model_dump(), "bad": bad.model_dump(), "api_calls": api.calls,
                                                     "slow_rollout_125pct": plan_rolling_deploy(4, 125, 100, True).steps}})
    try:
        plan_rolling_deploy(4, 100, 100, True)
    except DeploymentConfigError as exc:
        log.info("deploy config rejected", extra={"ctx": {"error": str(exc)}})
    try:
        lazy_boto3_client("ecs")
        log.info("ecs adapter", extra={"ctx": {"boto3": "installed (adapter available, not invoked offline)"}})
    except OptionalDependencyError as exc:
        log.info("ecs adapter", extra={"ctx": {"boto3": "missing", "hint": str(exc)}})


async def main() -> None:
    digest = "@sha256:" + "c" * 64
    for label, df, image, fixable in (("bad", BAD, "reg/app:latest", 0), ("good-with-cve", GOOD, "reg/app" + digest, 2),
                                      ("good", GOOD, "reg/app" + digest, 0)):
        scanner = FakeScanner({Severity.CRITICAL: fixable, Severity.LOW: 14}, fixable)
        ctx = PipelineContext(commit="a1b2c3d4", dockerfile=df, image=image, coverage_pct=91.5)
        gates = [("coverage", gate_coverage), ("dockerfile", gate_dockerfile), ("scan", make_scan_gate(scanner)), ("sign", gate_sign)]
        ok = await run_pipeline(ctx, gates)
        log.info("pipeline result", extra={"ctx": {"case": label, "promoted": ok, "signed": ctx.signed, "scanner_calls": scanner.calls,
                                                    "findings": [f"{x.rule}:{x.severity.value}" for x in ctx.findings]}})
    await extension_demo()


if __name__ == "__main__":
    asyncio.run(main())
