"""Module 03 lab: reachability analyzer and IAM policy evaluator.

Models VPC route tables (longest-prefix match), non-transitive peering, security groups and IAM
policy evaluation (explicit deny > allow > implicit deny) so network and permission questions can be
answered, and regression-tested, before touching a customer's cloud account. Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import json
import logging
import random
import sys
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Net = ipaddress.IPv4Network | ipaddress.IPv6Network


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


log = get_logger("lab03")


class NetworkError(Exception):
    """Base for topology/validation problems."""


class CidrOverlapError(NetworkError):
    pass


class RouteNotFoundError(NetworkError):
    pass


class CloudApiThrottled(NetworkError):
    """Simulated provider throttling; retryable."""


async def with_backoff(fn, *, attempts: int = 5, base: float = 0.02):  # type: ignore[no-untyped-def]
    for i in range(1, attempts + 1):
        try:
            return await fn()
        except CloudApiThrottled:
            if i == attempts:
                raise
            await asyncio.sleep(random.uniform(0, base * 2 ** i))


class Route(BaseModel):
    model_config = ConfigDict(frozen=True)
    destination: str
    target: str  # e.g. "local", "pcx-1", "tgw-1", "nat", "igw"

    @field_validator("destination")
    @classmethod
    def _valid_cidr(cls, v: str) -> str:
        ipaddress.ip_network(v, strict=True)
        return v


class SecurityRule(BaseModel):
    protocol: Literal["tcp", "udp", "icmp", "-1"] = "tcp"
    port_from: int = Field(ge=0, le=65535)
    port_to: int = Field(ge=0, le=65535)
    source_cidr: str | None = None
    source_sg: str | None = None

    @model_validator(mode="after")
    def _one_source(self) -> "SecurityRule":
        if (self.source_cidr is None) == (self.source_sg is None):
            raise ValueError("exactly one of source_cidr/source_sg is required")
        if self.port_from > self.port_to:
            raise ValueError("port_from > port_to")
        return self


class Vpc(BaseModel):
    name: str
    cidr: str
    routes: list[Route] = Field(default_factory=list)
    ingress: dict[str, list[SecurityRule]] = Field(default_factory=dict, description="sg name -> rules")

    @property
    def net(self) -> Net:
        return ipaddress.ip_network(self.cidr)


class Peering(BaseModel):
    pcx_id: str
    a: str
    b: str


class Topology(BaseModel):
    vpcs: dict[str, Vpc]
    peerings: list[Peering] = Field(default_factory=list)

    def validate_peerings(self) -> None:
        for p in self.peerings:
            va, vb = self.vpcs[p.a], self.vpcs[p.b]
            if va.net.overlaps(vb.net):
                raise CidrOverlapError(f"{p.pcx_id}: {va.name} {va.cidr} overlaps {vb.name} {vb.cidr}")

    def peer_of(self, pcx_id: str, from_vpc: str) -> str:
        for p in self.peerings:
            if p.pcx_id == pcx_id:
                if from_vpc == p.a:
                    return p.b
                if from_vpc == p.b:
                    return p.a
        raise RouteNotFoundError(f"{from_vpc} is not an endpoint of {pcx_id}")


def longest_prefix_match(vpc: Vpc, dest_ip: str) -> Route:
    ip = ipaddress.ip_address(dest_ip)
    local = Route(destination=vpc.cidr, target="local")
    candidates = [r for r in [local, *vpc.routes] if ip in ipaddress.ip_network(r.destination)]
    if not candidates:
        raise RouteNotFoundError(f"{vpc.name}: no route to {dest_ip}")
    return max(candidates, key=lambda r: ipaddress.ip_network(r.destination).prefixlen)


class Verdict(BaseModel):
    reachable: bool
    path: list[str]
    reason: str


def sg_allows(dst: Vpc, sg: str, src_ip: str, src_sg: str | None, proto: str, port: int) -> bool:
    for rule in dst.ingress.get(sg, []):
        if rule.protocol not in (proto, "-1") or not (rule.port_from <= port <= rule.port_to):
            continue
        if rule.source_cidr and ipaddress.ip_address(src_ip) in ipaddress.ip_network(rule.source_cidr):
            return True
        if rule.source_sg and rule.source_sg == src_sg:
            return True
    return False


def analyze(topo: Topology, src_vpc: str, src_ip: str, src_sg: str | None, dst_vpc: str, dst_ip: str,
            dst_sg: str, proto: str = "tcp", port: int = 443) -> Verdict:
    """Hop-by-hop reachability. Peering is non-transitive: only a direct 1:1 peering reaches the target."""
    path = [src_vpc]
    try:
        out = longest_prefix_match(topo.vpcs[src_vpc], dst_ip)
    except RouteNotFoundError as exc:
        return Verdict(reachable=False, path=path, reason=str(exc))
    if out.target == "local":
        next_vpc = src_vpc
    elif out.target.startswith("pcx-"):
        next_vpc = topo.peer_of(out.target, src_vpc)
        path.append(f"{out.target}->{next_vpc}")
    else:
        return Verdict(reachable=False, path=path, reason=f"route target {out.target} does not reach {dst_vpc}")
    if next_vpc != dst_vpc:
        return Verdict(reachable=False, path=path, reason=f"peering is non-transitive: landed in {next_vpc}, not {dst_vpc}")
    dst = topo.vpcs[dst_vpc]
    if ipaddress.ip_address(dst_ip) not in dst.net:
        return Verdict(reachable=False, path=path, reason="destination IP outside destination VPC CIDR")
    if next_vpc != src_vpc:  # return path must exist, or replies are dropped
        try:
            back = longest_prefix_match(dst, src_ip)
        except RouteNotFoundError:
            return Verdict(reachable=False, path=path, reason="no return route: replies dropped")
        if back.target == "local" or not back.target.startswith("pcx-"):
            return Verdict(reachable=False, path=path, reason=f"return route target {back.target} invalid")
    if not sg_allows(dst, dst_sg, src_ip, src_sg, proto, port):
        return Verdict(reachable=False, path=path, reason=f"security group {dst_sg} denies {proto}/{port}")
    return Verdict(reachable=True, path=path, reason="ok")


class Effect(str, Enum):
    ALLOW = "Allow"
    DENY = "Deny"


class Statement(BaseModel):
    effect: Effect
    actions: list[str] = Field(min_length=1)
    resources: list[str] = Field(min_length=1)
    condition_source_vpce: str | None = None


class PolicyDocument(BaseModel):
    statements: list[Statement]


class AccessRequest(BaseModel):
    action: str
    resource: str
    source_vpce: str | None = None


def _matches(patterns: list[str], value: str) -> bool:
    return any(fnmatch.fnmatchcase(value, p) for p in patterns)


def evaluate_iam(policy: PolicyDocument, req: AccessRequest) -> tuple[bool, str]:
    """AWS-style: explicit Deny wins, then any Allow, otherwise implicit deny."""
    allowed = False
    for i, st in enumerate(policy.statements):
        if not (_matches(st.actions, req.action) and _matches(st.resources, req.resource)):
            continue
        if st.condition_source_vpce and st.condition_source_vpce != req.source_vpce:
            continue
        if st.effect is Effect.DENY:
            return False, f"explicit deny by statement {i}"
        allowed = True
    return (True, "allowed") if allowed else (False, "implicit deny")


def build_topology() -> Topology:
    app = Vpc(name="app", cidr="10.30.0.0/16", routes=[Route(destination="10.40.0.0/16", target="pcx-1"),
                                                      Route(destination="172.16.0.0/16", target="pcx-2")])
    data = Vpc(name="data", cidr="10.40.0.0/16", routes=[Route(destination="10.30.0.0/16", target="pcx-1")],
               ingress={"sg-db": [SecurityRule(port_from=5432, port_to=5432, source_sg="sg-app")]})
    vendor = Vpc(name="vendor", cidr="172.16.0.0/16", routes=[Route(destination="10.30.0.0/16", target="pcx-2")],
                 ingress={"sg-model": [SecurityRule(port_from=443, port_to=443, source_cidr="10.30.0.0/16")]})
    return Topology(vpcs={"app": app, "data": data, "vendor": vendor},
                    peerings=[Peering(pcx_id="pcx-1", a="app", b="data"), Peering(pcx_id="pcx-2", a="app", b="vendor")])


async def main() -> None:
    topo = build_topology()
    topo.validate_peerings()
    cases = {
        "app->db 5432 via sg-app": analyze(topo, "app", "10.30.1.5", "sg-app", "data", "10.40.2.9", "sg-db", port=5432),
        "app->db 22 (blocked port)": analyze(topo, "app", "10.30.1.5", "sg-app", "data", "10.40.2.9", "sg-db", port=22),
        "vendor->db (non-transitive)": analyze(topo, "vendor", "172.16.0.4", None, "data", "10.40.2.9", "sg-db", port=5432),
        "app->vendor model 443": analyze(topo, "app", "10.30.1.5", "sg-app", "vendor", "172.16.3.3", "sg-model"),
    }
    for name, v in cases.items():
        log.info(name, extra={"ctx": v.model_dump()})

    try:
        bad = build_topology()
        bad.vpcs["vendor"].cidr = "10.30.128.0/17"
        bad.validate_peerings()
    except CidrOverlapError as exc:
        log.info("overlap detected", extra={"ctx": {"error": str(exc)}})

    policy = PolicyDocument(statements=[
        Statement(effect=Effect.ALLOW, actions=["s3:GetObject", "s3:ListBucket"], resources=["arn:aws:s3:::acme-docs", "arn:aws:s3:::acme-docs/*"],
                  condition_source_vpce="vpce-123"),
        Statement(effect=Effect.DENY, actions=["s3:*"], resources=["arn:aws:s3:::acme-docs/restricted/*"]),
    ])
    for req in (AccessRequest(action="s3:GetObject", resource="arn:aws:s3:::acme-docs/a.pdf", source_vpce="vpce-123"),
                AccessRequest(action="s3:GetObject", resource="arn:aws:s3:::acme-docs/restricted/x.pdf", source_vpce="vpce-123"),
                AccessRequest(action="s3:GetObject", resource="arn:aws:s3:::acme-docs/a.pdf", source_vpce="vpce-evil"),
                AccessRequest(action="s3:PutObject", resource="arn:aws:s3:::acme-docs/a.pdf", source_vpce="vpce-123")):
        ok, why = evaluate_iam(policy, req)
        log.info("iam", extra={"ctx": {"action": req.action, "resource": req.resource, "allowed": ok, "why": why}})

    calls = {"n": 0}

    async def flaky_describe() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise CloudApiThrottled("RequestLimitExceeded")
        return "ok"

    log.info("throttle recovered", extra={"ctx": {"result": await with_backoff(flaky_describe), "calls": calls["n"]}})


if __name__ == "__main__":
    asyncio.run(main())
