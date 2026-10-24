"""Module 04 lab: Dockerfile security linter and CI/CD promotion-gate engine.

Policy-as-code: parses a Dockerfile into instructions, checks hardening rules (digest-pinned base,
multi-stage, non-root USER, no secrets in ENV/ARG, no ADD from URLs, HEALTHCHECK), then runs an async
pipeline of gates (with retries on flaky scanners) and decides whether an image may be promoted.
Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import sys
import time
from enum import Enum
from typing import Awaitable, Callable

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


if __name__ == "__main__":
    asyncio.run(main())
