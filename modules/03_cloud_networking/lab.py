"""Module 03 lab: reachability analyzer, IAM policy evaluator and cloud-fundamentals toolkit.

Models VPC route tables (longest-prefix match), non-transitive peering, security groups and IAM
policy evaluation (explicit deny > allow > implicit deny) so network and permission questions can be
answered, and regression-tested, before touching a customer's cloud account. Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import calendar
import fnmatch
import ipaddress
import json
import logging
import random
import re
import shlex
import sys
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Awaitable, Callable, Literal, TypeVar

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


# ---- Syllabus extension: shared helpers ---
# Everything below runs offline with pydantic + stdlib. boto3 appears only inside lazily imported adapters
# (boto3_client) that are IN ADDITION to the offline logic exercised by extension_demo().

T = TypeVar("T")
THROTTLE_CODES = {"Throttling", "ThrottlingException", "RequestLimitExceeded", "TooManyRequestsException"}


class ProvisioningError(NetworkError):
    """A launch, database, lifecycle, policy or billing spec failed validation."""


class OptionalDependencyError(NetworkError):
    """A lazily imported adapter library (boto3) is not installed."""


class Finding(BaseModel):
    area: str
    severity: Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
    message: str
    subject: str = ""


def has_severity(findings: list[Finding], *levels: str) -> bool:
    return any(f.severity in levels for f in findings)


async def retry_async(fn: Callable[[], Awaitable[T]], *, retry_on: tuple[type[Exception], ...] = (CloudApiThrottled,),
                      attempts: int = 4, base: float = 0.02) -> T:
    """Exponential backoff with full jitter around any I/O callable; only `retry_on` errors are retried."""
    for i in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on:
            if i == attempts:
                raise
            await asyncio.sleep(random.uniform(0, base * 2 ** i))
    raise AssertionError("unreachable")


def boto3_client(service: str, region: str = "us-east-1") -> Any:
    """Lazy import: the offline code paths never touch boto3."""
    try:
        import boto3  # noqa: PLC0415
    except ImportError as exc:
        raise OptionalDependencyError("boto3 is not installed; run: pip install boto3") from exc
    return boto3.client(service, region_name=region)


async def call_aws(method: Callable[..., Any], **kwargs: Any) -> Any:
    """Run a blocking boto3 method in a thread; map throttling error codes so retry_async retries them."""
    def _run() -> Any:
        try:
            return method(**kwargs)
        except Exception as exc:  # botocore ClientError carries .response
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in THROTTLE_CODES:
                raise CloudApiThrottled(code) from exc
            raise
    return await retry_async(lambda: asyncio.to_thread(_run))


# ---- Syllabus extension: provisioning virtual machines ---

INSTANCE_CATALOG: dict[str, tuple[int, float]] = {  # instance type -> (vCPU, memory GiB)
    "t3.micro": (2, 1.0), "t3.medium": (2, 4.0), "m5.large": (2, 8.0),
    "m5.xlarge": (4, 16.0), "c5.xlarge": (4, 8.0), "r5.large": (2, 16.0),
}
REQUIRED_TAGS = ("owner", "cost-center")
HOURS_PER_MONTH = Decimal(730)


class VmLaunchSpec(BaseModel):
    """What `aws ec2 run-instances` (or a Terraform aws_instance) needs, with house rules enforced."""
    name: str = Field(min_length=1, max_length=63)
    ami_id: str = Field(pattern=r"^ami-[0-9a-f]{8}([0-9a-f]{9})?$")
    instance_type: str = Field(pattern=r"^[a-z][a-z0-9]*\.(nano|micro|small|medium|large|[0-9]*xlarge|metal)$")
    subnet_id: str = Field(pattern=r"^subnet-[0-9a-f]{8}([0-9a-f]{9})?$")
    security_group_ids: list[str] = Field(min_length=1)
    root_volume_gb: int = Field(ge=8, le=16384)
    volume_type: Literal["gp3", "gp2", "io2"] = "gp3"
    imdsv2_required: bool = True
    iam_instance_profile: str | None = None
    count: int = Field(default=1, ge=1, le=20)
    tags: dict[str, str] = Field(default_factory=dict)

    @field_validator("security_group_ids")
    @classmethod
    def _sg_ids(cls, v: list[str]) -> list[str]:
        for sg in v:
            if not re.fullmatch(r"sg-[0-9a-f]{8}([0-9a-f]{9})?", sg):
                raise ValueError(f"bad security group id {sg}")
        return v

    @model_validator(mode="after")
    def _house_rules(self) -> "VmLaunchSpec":
        missing = [t for t in REQUIRED_TAGS if t not in self.tags]
        if missing:
            raise ValueError(f"missing required tags {missing}")
        if not self.imdsv2_required:
            raise ValueError("IMDSv2 must be required (HttpTokens=required) to blunt SSRF credential theft")
        return self


class RateCard(BaseModel):
    """Caller-supplied rates. The demo uses obviously fake numbers; load real ones from the provider's price list."""
    per_vcpu_hour: Decimal
    per_gib_hour: Decimal
    ebs_gb_month: Decimal


def hourly_rate(vcpu: int, mem_gib: float, rates: RateCard) -> Decimal:
    return Decimal(vcpu) * rates.per_vcpu_hour + Decimal(str(mem_gib)) * rates.per_gib_hour


def monthly_cost(spec: VmLaunchSpec, rates: RateCard, duty_cycle: float = 1.0) -> Decimal:
    if spec.instance_type not in INSTANCE_CATALOG:
        raise ProvisioningError(f"{spec.instance_type} is not in INSTANCE_CATALOG; extend the catalog to price it")
    vcpu, mem = INSTANCE_CATALOG[spec.instance_type]
    compute = hourly_rate(vcpu, mem, rates) * HOURS_PER_MONTH * Decimal(str(duty_cycle))
    storage = rates.ebs_gb_month * spec.root_volume_gb
    return ((compute + storage) * spec.count).quantize(Decimal("0.01"))


def pick_instance(need_vcpu: int, need_mem_gib: float, rates: RateCard, headroom: float = 0.25) -> str:
    """Cheapest catalog type that fits the workload plus headroom (right-sizing)."""
    want_v, want_m = need_vcpu * (1 + headroom), need_mem_gib * (1 + headroom)
    fits = [(t, v, m) for t, (v, m) in INSTANCE_CATALOG.items() if v >= want_v and m >= want_m]
    if not fits:
        raise ProvisioningError(f"nothing fits {need_vcpu} vCPU / {need_mem_gib} GiB with {headroom:.0%} headroom")
    return min(fits, key=lambda x: hourly_rate(x[1], x[2], rates))[0]


def vm_cli(spec: VmLaunchSpec) -> str:
    """Console-free provisioning: the same launch as one reproducible AWS CLI command."""
    tags = ",".join("{Key=%s,Value=%s}" % (k, v) for k, v in {"Name": spec.name, **spec.tags}.items())
    bdm = [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": spec.root_volume_gb, "VolumeType": spec.volume_type,
                                              "Encrypted": True, "DeleteOnTermination": True}}]
    parts = ["aws", "ec2", "run-instances", "--image-id", spec.ami_id, "--instance-type", spec.instance_type,
             "--subnet-id", spec.subnet_id, "--security-group-ids", *spec.security_group_ids,
             "--count", str(spec.count), "--metadata-options", "HttpTokens=required,HttpEndpoint=enabled",
             "--block-device-mappings", json.dumps(bdm, separators=(",", ":")),
             "--tag-specifications", "ResourceType=instance,Tags=[%s]" % tags]
    if spec.iam_instance_profile:
        parts += ["--iam-instance-profile", "Name=" + spec.iam_instance_profile]
    return shlex.join(parts)


def vm_terraform(spec: VmLaunchSpec) -> str:
    tag_lines = "\n".join('    %s = "%s"' % (k, v) for k, v in {"Name": spec.name, **spec.tags}.items())
    profile = '  iam_instance_profile = "%s"\n' % spec.iam_instance_profile if spec.iam_instance_profile else ""
    return (
        'resource "aws_instance" "%s" {\n  ami                    = "%s"\n  instance_type          = "%s"\n'
        '  subnet_id              = "%s"\n  vpc_security_group_ids = [%s]\n  count                  = %d\n%s'
        '  metadata_options {\n    http_tokens   = "required"\n    http_endpoint = "enabled"\n  }\n'
        '  root_block_device {\n    volume_size = %d\n    volume_type = "%s"\n    encrypted   = true\n  }\n'
        '  tags = {\n%s\n  }\n}\n'
    ) % (re.sub(r"[^a-z0-9_]", "_", spec.name.lower()), spec.ami_id, spec.instance_type, spec.subnet_id,
         ", ".join('"%s"' % s for s in spec.security_group_ids), spec.count, profile, spec.root_volume_gb,
         spec.volume_type, tag_lines)


def validate_vm_artifacts(spec: VmLaunchSpec, cli: str, hcl: str) -> list[str]:
    """Re-parse the generated CLI/HCL and prove they say what the spec says. Returns a list of problems (empty = ok)."""
    problems: list[str] = []
    tok = shlex.split(cli)
    flags = {t: tok[i + 1] for i, t in enumerate(tok[:-1]) if t.startswith("--")}
    if flags.get("--instance-type") != spec.instance_type:
        problems.append("instance type mismatch")
    if "HttpTokens=required" not in flags.get("--metadata-options", ""):
        problems.append("IMDSv2 not enforced in CLI")
    try:
        bdm = json.loads(flags["--block-device-mappings"])
        if not bdm[0]["Ebs"]["Encrypted"]:
            problems.append("root volume not encrypted")
    except (KeyError, ValueError):
        problems.append("block-device-mappings is not valid JSON")
    for tag in REQUIRED_TAGS:
        if "Key=%s," % tag not in flags.get("--tag-specifications", ""):
            problems.append("tag %s missing in CLI" % tag)
    if hcl.count("{") != hcl.count("}"):
        problems.append("unbalanced braces in HCL")
    if 'http_tokens   = "required"' not in hcl:
        problems.append("IMDSv2 not enforced in HCL")
    return problems


# ---- Syllabus extension: object storage lifecycle policies ---

STORAGE_RANK = {"STANDARD": 0, "INTELLIGENT_TIERING": 1, "STANDARD_IA": 1, "GLACIER_IR": 2, "GLACIER": 3, "DEEP_ARCHIVE": 4}
MIN_STORAGE_DAYS = {"STANDARD_IA": 30, "GLACIER_IR": 90, "GLACIER": 90, "DEEP_ARCHIVE": 180}  # early-deletion windows
StorageClass = Literal["STANDARD_IA", "INTELLIGENT_TIERING", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE"]


class LifecycleTransition(BaseModel):
    days: int = Field(ge=0)
    storage_class: StorageClass


class LifecycleRule(BaseModel):
    id: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    prefix: str = ""
    transitions: list[LifecycleTransition] = Field(default_factory=list)
    expiration_days: int | None = Field(default=None, ge=1)
    noncurrent_expiration_days: int | None = Field(default=None, ge=1)
    abort_incomplete_multipart_days: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _consistent(self) -> "LifecycleRule":
        if not (self.transitions or self.expiration_days or self.noncurrent_expiration_days or self.abort_incomplete_multipart_days):
            raise ValueError("rule has no action")
        last_days, last_rank = -1, -1
        for t in self.transitions:
            if t.days <= last_days:
                raise ValueError("transition days must be strictly increasing")
            if STORAGE_RANK[t.storage_class] <= last_rank:
                raise ValueError("storage classes must move colder at each step")
            if t.storage_class == "STANDARD_IA" and t.days < 30:
                raise ValueError("STANDARD_IA needs at least 30 days in STANDARD first")
            last_days, last_rank = t.days, STORAGE_RANK[t.storage_class]
        if self.expiration_days is not None and self.transitions and self.expiration_days <= last_days:
            raise ValueError("expiration must be later than the last transition")
        return self


def lifecycle_findings(rules: list[LifecycleRule]) -> list[Finding]:
    out: list[Finding] = []
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        raise ProvisioningError("duplicate lifecycle rule ids")
    for r in rules:
        if r.expiration_days and r.transitions:
            last = r.transitions[-1]
            floor = MIN_STORAGE_DAYS.get(last.storage_class, 0)
            if r.expiration_days - last.days < floor:
                out.append(Finding(area="s3-lifecycle", severity="MEDIUM", subject=r.id,
                                   message="expires %d days after entering %s (minimum billed %d): early-deletion charge"
                                   % (r.expiration_days - last.days, last.storage_class, floor)))
    for i, a in enumerate(rules):
        for b in rules[i + 1:]:
            if a.enabled and b.enabled and (a.prefix.startswith(b.prefix) or b.prefix.startswith(a.prefix)):
                out.append(Finding(area="s3-lifecycle", severity="LOW", subject=a.id + "," + b.id,
                                   message="overlapping prefixes: both rules apply to shared objects"))
    return out


class ObjectRecord(BaseModel):
    key: str
    size_bytes: int = Field(ge=0)
    age_days: int = Field(ge=0)  # days since creation (or since it became noncurrent)
    noncurrent: bool = False


class ObjectOutcome(BaseModel):
    key: str
    final_class: str  # a storage class or "EXPIRED"
    applied: list[str]


def apply_lifecycle(rules: list[LifecycleRule], objects: list[ObjectRecord]) -> list[ObjectOutcome]:
    """Evaluates what the lifecycle engine would do to each object once it is `age_days` old."""
    out: list[ObjectOutcome] = []
    for o in objects:
        cls, applied, expired = "STANDARD", [], False
        for r in rules:
            if not r.enabled or not o.key.startswith(r.prefix):
                continue
            if o.noncurrent:
                if r.noncurrent_expiration_days and o.age_days >= r.noncurrent_expiration_days:
                    expired = True
                    applied.append("%s:noncurrent-expire@%dd" % (r.id, r.noncurrent_expiration_days))
                continue
            for t in r.transitions:
                if o.age_days >= t.days and STORAGE_RANK[t.storage_class] > STORAGE_RANK.get(cls, 0):
                    cls = t.storage_class
                    applied.append("%s:%s@%dd" % (r.id, t.storage_class, t.days))
            if r.expiration_days and o.age_days >= r.expiration_days:
                expired = True
                applied.append("%s:expire@%dd" % (r.id, r.expiration_days))
        out.append(ObjectOutcome(key=o.key, final_class="EXPIRED" if expired else cls, applied=applied))
    return out


def lifecycle_json(rules: list[LifecycleRule]) -> dict[str, Any]:
    """Body for: aws s3api put-bucket-lifecycle-configuration --bucket B --lifecycle-configuration file://lifecycle.json"""
    docs: list[dict[str, Any]] = []
    for r in rules:
        d: dict[str, Any] = {"ID": r.id, "Status": "Enabled" if r.enabled else "Disabled", "Filter": {"Prefix": r.prefix}}
        if r.transitions:
            d["Transitions"] = [{"Days": t.days, "StorageClass": t.storage_class} for t in r.transitions]
        if r.expiration_days:
            d["Expiration"] = {"Days": r.expiration_days}
        if r.noncurrent_expiration_days:
            d["NoncurrentVersionExpiration"] = {"NoncurrentDays": r.noncurrent_expiration_days}
        if r.abort_incomplete_multipart_days:
            d["AbortIncompleteMultipartUpload"] = {"DaysAfterInitiation": r.abort_incomplete_multipart_days}
        docs.append(d)
    return {"Rules": docs}


def lifecycle_from_json(doc: dict[str, Any]) -> list[LifecycleRule]:
    """Independent re-parse of the emitted JSON; a round trip proves the artifact means what the model means."""
    rules: list[LifecycleRule] = []
    for d in doc["Rules"]:
        if d["Status"] not in ("Enabled", "Disabled"):
            raise ProvisioningError("bad Status " + str(d["Status"]))
        rules.append(LifecycleRule(
            id=d["ID"], enabled=d["Status"] == "Enabled", prefix=d.get("Filter", {}).get("Prefix", ""),
            transitions=[LifecycleTransition(days=t["Days"], storage_class=t["StorageClass"]) for t in d.get("Transitions", [])],
            expiration_days=d.get("Expiration", {}).get("Days"),
            noncurrent_expiration_days=d.get("NoncurrentVersionExpiration", {}).get("NoncurrentDays"),
            abort_incomplete_multipart_days=d.get("AbortIncompleteMultipartUpload", {}).get("DaysAfterInitiation")))
    return rules


# ---- Syllabus extension: managed relational databases setup ---

class DbInstanceConfig(BaseModel):
    identifier: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    engine: Literal["postgres", "mysql"]
    instance_class: str = Field(pattern=r"^db\.[a-z0-9]+\.[a-z0-9]+$")
    allocated_storage_gb: int = Field(ge=20, le=65536)
    environment: Literal["dev", "prod"] = "dev"
    multi_az: bool = False
    storage_encrypted: bool = True
    backup_retention_days: int = Field(default=7, ge=0, le=35)
    publicly_accessible: bool = False
    deletion_protection: bool = False
    subnet_azs: list[str] = Field(min_length=1)  # AZ of each subnet in the DB subnet group
    vpc_security_group_ids: list[str] = Field(min_length=1)
    manage_master_password: bool = True  # let the service keep the admin password in Secrets Manager
    parameters: dict[str, str] = Field(default_factory=dict)
    expected_connections: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _subnet_group(self) -> "DbInstanceConfig":
        if len(set(self.subnet_azs)) < 2:
            raise ValueError("DB subnet group must span at least two availability zones")
        return self


def audit_db_config(cfg: DbInstanceConfig) -> list[Finding]:
    f: list[Finding] = []

    def add(sev: Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"], msg: str) -> None:
        f.append(Finding(area="managed-db", severity=sev, message=msg, subject=cfg.identifier))

    if cfg.publicly_accessible:
        add("CRITICAL", "database is publicly accessible; keep it in private subnets")
    if not cfg.storage_encrypted:
        add("HIGH", "storage encryption is off (cannot be enabled in place later)")
    if cfg.environment == "prod":
        if not cfg.multi_az:
            add("HIGH", "prod without Multi-AZ: a failure of one AZ is an outage")
        if cfg.backup_retention_days < 7:
            add("MEDIUM", "prod backup retention under 7 days (0 disables automated backups and PITR)")
        if not cfg.deletion_protection:
            add("MEDIUM", "prod without deletion protection")
    if not cfg.manage_master_password:
        add("LOW", "master password is self-managed; prefer a managed secret with rotation")
    required = {"postgres": ("rds.force_ssl", "1"), "mysql": ("require_secure_transport", "ON")}[cfg.engine]
    if cfg.parameters.get(required[0], "").upper() != required[1]:
        add("HIGH" if cfg.environment == "prod" else "MEDIUM", "set %s=%s to force TLS" % required)
    maxc = cfg.parameters.get("max_connections", "")
    if maxc.isdigit() and cfg.expected_connections and int(maxc) < cfg.expected_connections * 1.1:
        add("HIGH", "max_connections %s leaves under 10%% headroom for %d expected connections" % (maxc, cfg.expected_connections))
    return f


def db_cli(cfg: DbInstanceConfig, subnet_group: str, username: str = "app_admin") -> str:
    parts = ["aws", "rds", "create-db-instance", "--db-instance-identifier", cfg.identifier, "--engine", cfg.engine,
             "--db-instance-class", cfg.instance_class, "--allocated-storage", str(cfg.allocated_storage_gb),
             "--master-username", username, "--db-subnet-group-name", subnet_group,
             "--vpc-security-group-ids", *cfg.vpc_security_group_ids,
             "--backup-retention-period", str(cfg.backup_retention_days),
             "--multi-az" if cfg.multi_az else "--no-multi-az",
             "--storage-encrypted" if cfg.storage_encrypted else "--no-storage-encrypted",
             "--publicly-accessible" if cfg.publicly_accessible else "--no-publicly-accessible",
             "--deletion-protection" if cfg.deletion_protection else "--no-deletion-protection"]
    if cfg.manage_master_password:
        parts.append("--manage-master-user-password")
    return shlex.join(parts)


def db_terraform(cfg: DbInstanceConfig, subnet_group: str, username: str = "app_admin") -> str:
    b = lambda v: "true" if v else "false"  # noqa: E731
    return (
        'resource "aws_db_instance" "%s" {\n  identifier                  = "%s"\n  engine                      = "%s"\n'
        '  instance_class              = "%s"\n  allocated_storage           = %d\n  username                    = "%s"\n'
        '  manage_master_user_password = %s\n  db_subnet_group_name        = "%s"\n  vpc_security_group_ids      = [%s]\n'
        '  multi_az                    = %s\n  storage_encrypted           = %s\n  backup_retention_period     = %d\n'
        '  publicly_accessible         = %s\n  deletion_protection         = %s\n}\n'
    ) % (cfg.identifier.replace("-", "_"), cfg.identifier, cfg.engine, cfg.instance_class, cfg.allocated_storage_gb,
         username, b(cfg.manage_master_password), subnet_group, ", ".join('"%s"' % s for s in cfg.vpc_security_group_ids),
         b(cfg.multi_az), b(cfg.storage_encrypted), cfg.backup_retention_days, b(cfg.publicly_accessible),
         b(cfg.deletion_protection))


def validate_db_artifacts(cfg: DbInstanceConfig, cli: str, hcl: str) -> list[str]:
    problems: list[str] = []
    tok = shlex.split(cli)
    if ("--multi-az" in tok) != cfg.multi_az:
        problems.append("multi-az flag mismatch")
    if "--no-publicly-accessible" not in tok and not cfg.publicly_accessible:
        problems.append("CLI would make the DB public")
    if "--master-user-password" in tok:
        problems.append("plaintext password on the command line")
    if hcl.count("{") != hcl.count("}"):
        problems.append("unbalanced braces in HCL")
    if "manage_master_user_password = true" not in hcl and cfg.manage_master_password:
        problems.append("HCL missing managed master password")
    return problems


# ---- Syllabus extension: event-driven serverless function basics ---

class TransientHandlerError(NetworkError):
    """Retryable handler failure (dependency timeout, throttling)."""


class PoisonEventError(NetworkError):
    """Non-retryable: the event can never succeed; goes straight to the dead-letter queue."""


class CloudEvent(BaseModel):
    id: str
    source: str
    detail_type: str
    detail: dict[str, Any] = Field(default_factory=dict)


class EventPattern(BaseModel):
    """EventBridge-style: each listed field must match one of the allowed values; empty means any."""
    source: list[str] = Field(default_factory=list)
    detail_type: list[str] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)  # leaf = list of allowed values, nested dict recurses

    def matches(self, e: CloudEvent) -> bool:
        if self.source and e.source not in self.source:
            return False
        if self.detail_type and e.detail_type not in self.detail_type:
            return False
        return self._detail_ok(self.detail, e.detail)

    @classmethod
    def _detail_ok(cls, pat: dict[str, Any], det: dict[str, Any]) -> bool:
        for k, want in pat.items():
            if k not in det:
                return False
            if isinstance(want, dict):
                if not isinstance(det[k], dict) or not cls._detail_ok(want, det[k]):
                    return False
            elif det[k] not in want:
                return False
        return True


class FunctionConfig(BaseModel):
    name: str
    timeout_s: float = Field(default=1.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=2)  # async invocations retry at most twice


class DeadLetter(BaseModel):
    event: CloudEvent
    function: str
    attempts: int
    error: str


Handler = Callable[[CloudEvent], Awaitable[str]]


class EventDispatcher:
    """At-least-once delivery: retries with backoff, idempotent by event id, poison events to a DLQ."""

    def __init__(self, backoff_base: float = 0.005, concurrency: int = 4) -> None:
        self._routes: list[tuple[EventPattern, FunctionConfig, Handler]] = []
        self._done: dict[tuple[str, str], str] = {}
        self._sem = asyncio.Semaphore(concurrency)
        self._base = backoff_base
        self.dlq: list[DeadLetter] = []
        self.invocations: dict[str, int] = {}

    def register(self, pattern: EventPattern, cfg: FunctionConfig, handler: Handler) -> None:
        self._routes.append((pattern, cfg, handler))

    async def dispatch(self, event: CloudEvent) -> dict[str, str]:
        results: dict[str, str] = {}
        targets = [(c, h) for p, c, h in self._routes if p.matches(event)]
        if not targets:
            return {"-": "unrouted"}
        for cfg, handler in targets:
            key = (cfg.name, event.id)
            if key in self._done:
                results[cfg.name] = "duplicate-skipped"
                continue
            results[cfg.name] = await self._invoke(cfg, handler, event)
            if results[cfg.name].startswith("ok"):
                self._done[key] = results[cfg.name]
        return results

    async def _invoke(self, cfg: FunctionConfig, handler: Handler, event: CloudEvent) -> str:
        attempts = cfg.max_retries + 1
        last = ""
        for n in range(1, attempts + 1):
            self.invocations[cfg.name] = self.invocations.get(cfg.name, 0) + 1
            try:
                async with self._sem:
                    out = await asyncio.wait_for(handler(event), timeout=cfg.timeout_s)
                return "ok:" + out
            except PoisonEventError as exc:
                self.dlq.append(DeadLetter(event=event, function=cfg.name, attempts=n, error=str(exc)))
                return "dead-letter:poison"
            except (TransientHandlerError, asyncio.TimeoutError) as exc:
                last = str(exc) or exc.__class__.__name__
                if n < attempts:
                    await asyncio.sleep(random.uniform(0, self._base * 2 ** n))
        self.dlq.append(DeadLetter(event=event, function=cfg.name, attempts=attempts, error=last))
        return "dead-letter:exhausted"

    async def dispatch_many(self, events: list[CloudEvent]) -> list[dict[str, str]]:
        return await asyncio.gather(*(self.dispatch(e) for e in events))


async def invoke_lambda_boto3(function_name: str, payload: dict[str, Any], region: str = "us-east-1") -> int:
    """Real adapter: asynchronous (Event) invoke of a deployed function. Not executed by the offline demo."""
    client = boto3_client("lambda", region)
    resp = await call_aws(client.invoke, FunctionName=function_name, InvocationType="Event",
                          Payload=json.dumps(payload).encode())
    return int(resp["StatusCode"])


# ---- Syllabus extension: creating VPCs, public vs private subnet routing, outbound traffic via NAT ---

class SubnetClass(str, Enum):
    PUBLIC = "public"                    # default route -> internet gateway
    PRIVATE_EGRESS = "private-with-nat"  # default route -> NAT gateway (outbound only)
    ISOLATED = "isolated"                # no default route at all


class SubnetSpec(BaseModel):
    name: str
    cidr: str
    az: str
    route_table: str
    map_public_ip: bool = False  # auto-assign public IPv4 on launch


class VpcDesign(BaseModel):
    name: str
    cidr: str
    subnets: list[SubnetSpec]
    route_tables: dict[str, list[Route]]  # the implicit "local" route is added by longest_prefix_match
    nat_gateways: dict[str, str] = Field(default_factory=dict)  # nat id -> name of the (public) subnet hosting it
    igw_id: str | None = None

    def subnet(self, name: str) -> SubnetSpec:
        for s in self.subnets:
            if s.name == name:
                return s
        raise NetworkError("unknown subnet " + name)

    def routes_for(self, s: SubnetSpec) -> Vpc:
        if s.route_table not in self.route_tables:
            raise NetworkError("%s references unknown route table %s" % (s.name, s.route_table))
        return Vpc(name=s.name, cidr=self.cidr, routes=self.route_tables[s.route_table])


AWS_RESERVED_IPS_PER_SUBNET = 5  # network, router, DNS, future use, broadcast


def usable_hosts(cidr: str) -> int:
    return ipaddress.ip_network(cidr).num_addresses - AWS_RESERVED_IPS_PER_SUBNET


def carve_subnets(vpc_cidr: str, new_prefix: int, count: int, offset: int = 0) -> list[str]:
    """Slices `count` equal subnets out of a VPC CIDR, starting at index `offset`."""
    net = ipaddress.ip_network(vpc_cidr)
    pool = list(net.subnets(new_prefix=new_prefix))
    if offset + count > len(pool):
        raise CidrOverlapError("%s has only %d /%d blocks" % (vpc_cidr, len(pool), new_prefix))
    return [str(p) for p in pool[offset:offset + count]]


def classify_subnet(d: VpcDesign, s: SubnetSpec) -> tuple[SubnetClass, str]:
    """Public vs private is defined by the subnet's route table, not by its name."""
    default = [r for r in d.route_tables.get(s.route_table, []) if r.destination in ("0.0.0.0/0", "::/0")]
    if not default:
        return SubnetClass.ISOLATED, "no default route"
    target = default[0].target
    if target.startswith("igw-"):
        note = "route to internet gateway" + ("" if s.map_public_ip else " (but no auto-assigned public IP)")
        return SubnetClass.PUBLIC, note
    if target.startswith("nat-"):
        return SubnetClass.PRIVATE_EGRESS, "default route via " + target
    return SubnetClass.ISOLATED, "default route target %s is not usable (blackhole?)" % target


def validate_design(d: VpcDesign) -> list[Finding]:
    f: list[Finding] = []
    vnet = ipaddress.ip_network(d.cidr)
    nets = [(s, ipaddress.ip_network(s.cidr)) for s in d.subnets]
    for i, (s, n) in enumerate(nets):
        if not n.subnet_of(vnet):  # type: ignore[arg-type]
            raise CidrOverlapError("%s %s is outside VPC %s" % (s.name, s.cidr, d.cidr))
        for s2, n2 in nets[i + 1:]:
            if n.overlaps(n2):
                raise CidrOverlapError("%s overlaps %s" % (s.name, s2.name))
        if usable_hosts(s.cidr) < 11:
            f.append(Finding(area="vpc", severity="MEDIUM", subject=s.name, message="fewer than 11 usable IPs"))
        classify_subnet(d, s)
    uses_igw = any(r.target.startswith("igw-") for rts in d.route_tables.values() for r in rts)
    if uses_igw and not d.igw_id:
        raise NetworkError("route table points at an internet gateway but the design has none")
    for nat, host in d.nat_gateways.items():
        if classify_subnet(d, d.subnet(host))[0] is not SubnetClass.PUBLIC:
            raise NetworkError("%s sits in %s, which is not public: NAT cannot reach the internet" % (nat, host))
    if len({s.az for s in d.subnets}) < 2:
        f.append(Finding(area="vpc", severity="HIGH", message="single-AZ design"))
    nat_azs = {d.subnet(h).az for h in d.nat_gateways.values()}
    private_azs = {s.az for s in d.subnets if classify_subnet(d, s)[0] is SubnetClass.PRIVATE_EGRESS}
    if private_azs - nat_azs:
        f.append(Finding(area="vpc", severity="MEDIUM", message="private subnets in %s depend on a NAT in another AZ" % sorted(private_azs - nat_azs)))
    return f


def egress_path(d: VpcDesign, subnet_name: str, dest_ip: str) -> Verdict:
    """Outbound check: can an instance in `subnet_name` open a connection to `dest_ip` on the internet?"""
    s = d.subnet(subnet_name)
    path = [subnet_name, s.route_table]
    try:
        hit = longest_prefix_match(d.routes_for(s), dest_ip)
    except RouteNotFoundError as exc:
        return Verdict(reachable=False, path=path, reason=str(exc))
    if hit.target == "local":
        return Verdict(reachable=True, path=path + ["local"], reason="stays inside the VPC")
    if hit.target.startswith("igw-"):
        if not s.map_public_ip:
            return Verdict(reachable=False, path=path + [hit.target], reason="no public IP: IGW will not translate a private address")
        return Verdict(reachable=True, path=path + [hit.target, "internet"], reason="public IP via internet gateway")
    if hit.target.startswith("nat-"):
        host = d.nat_gateways.get(hit.target)
        if host is None:
            return Verdict(reachable=False, path=path, reason="unknown NAT gateway " + hit.target)
        path += ["%s (in %s)" % (hit.target, host)]
        if classify_subnet(d, d.subnet(host))[0] is not SubnetClass.PUBLIC:
            return Verdict(reachable=False, path=path, reason="NAT subnet has no route to an internet gateway")
        path += [d.subnet(host).route_table, d.igw_id or "?", "internet"]
        return Verdict(reachable=True, path=path, reason="source-translated by NAT; outbound-initiated only")
    return Verdict(reachable=False, path=path, reason="target %s does not reach the internet" % hit.target)


def internet_can_initiate(d: VpcDesign, subnet_name: str) -> bool:
    """Inbound exposure: only a public subnet with public IPs can be reached from the internet."""
    s = d.subnet(subnet_name)
    return classify_subnet(d, s)[0] is SubnetClass.PUBLIC and s.map_public_ip


def build_three_tier_vpc(name: str, cidr: str, azs: list[str]) -> VpcDesign:
    """public / private-app / data tiers per AZ, one NAT per AZ (survives an AZ failure; costs more than one shared NAT)."""
    pub = carve_subnets(cidr, 24, len(azs), 0)
    app = carve_subnets(cidr, 24, len(azs), 10)
    dat = carve_subnets(cidr, 24, len(azs), 20)
    subs: list[SubnetSpec] = []
    rts: dict[str, list[Route]] = {"rt-public": [Route(destination="0.0.0.0/0", target="igw-1")], "rt-data": []}
    nats: dict[str, str] = {}
    for i, az in enumerate(azs):
        L = az[-1]
        subs += [SubnetSpec(name="public-" + L, cidr=pub[i], az=az, route_table="rt-public", map_public_ip=True),
                 SubnetSpec(name="app-" + L, cidr=app[i], az=az, route_table="rt-app-" + L),
                 SubnetSpec(name="data-" + L, cidr=dat[i], az=az, route_table="rt-data")]
        rts["rt-app-" + L] = [Route(destination="0.0.0.0/0", target="nat-" + L)]
        nats["nat-" + L] = "public-" + L
    return VpcDesign(name=name, cidr=cidr, subnets=subs, route_tables=rts, nat_gateways=nats, igw_id="igw-1")


def vpc_cli(d: VpcDesign) -> list[str]:
    """Shell script lines that build the design with the AWS CLI. IDs are captured into shell variables."""
    var = lambda s: re.sub(r"[^A-Z0-9]", "_", s.upper())  # noqa: E731
    out = ['VPC_ID=$(aws ec2 create-vpc --cidr-block %s --query Vpc.VpcId --output text)' % d.cidr,
           'aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames "{\\"Value\\":true}"',
           'IGW_ID=$(aws ec2 create-internet-gateway --query InternetGateway.InternetGatewayId --output text)',
           'aws ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID"']
    for s in d.subnets:
        v = "SUBNET_" + var(s.name)
        out.append('%s=$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block %s --availability-zone %s --query Subnet.SubnetId --output text)'
                   % (v, s.cidr, s.az))
        if s.map_public_ip:
            out.append('aws ec2 modify-subnet-attribute --subnet-id "$%s" --map-public-ip-on-launch' % v)
    for nat, host in d.nat_gateways.items():
        out += ['EIP_%s=$(aws ec2 allocate-address --domain vpc --query AllocationId --output text)' % var(nat),
                '%s=$(aws ec2 create-nat-gateway --subnet-id "$SUBNET_%s" --allocation-id "$EIP_%s" --query NatGateway.NatGatewayId --output text)'
                % (var(nat), var(host), var(nat)),
                'aws ec2 wait nat-gateway-available --nat-gateway-ids "$%s"' % var(nat)]
    for rt, routes in d.route_tables.items():
        v = "RT_" + var(rt)
        out.append('%s=$(aws ec2 create-route-table --vpc-id "$VPC_ID" --query RouteTable.RouteTableId --output text)' % v)
        for r in routes:
            flag, ref = ("--gateway-id", "$IGW_ID") if r.target.startswith("igw-") else ("--nat-gateway-id", "$" + var(r.target))
            out.append('aws ec2 create-route --route-table-id "$%s" --destination-cidr-block %s %s "%s"' % (v, r.destination, flag, ref))
    for s in d.subnets:
        out.append('aws ec2 associate-route-table --route-table-id "$RT_%s" --subnet-id "$SUBNET_%s"' % (var(s.route_table), var(s.name)))
    return out


def validate_vpc_cli(lines: list[str], d: VpcDesign) -> list[str]:
    """Checks the generated script against the design: counts, and which route targets appear in which table."""
    problems: list[str] = []
    text = "\n".join(lines)
    if text.count("create-subnet") != len(d.subnets):
        problems.append("subnet count mismatch")
    if text.count("create-nat-gateway") != len(d.nat_gateways):
        problems.append("NAT count mismatch")
    if text.count("associate-route-table") != len(d.subnets):
        problems.append("every subnet needs a route table association")
    for ln in lines:
        if "--gateway-id" in ln:
            m = re.search(r'--route-table-id "\$RT_([A-Z0-9_]+)"', ln)
            if not m or not any(r.target.startswith("igw-") for r in d.route_tables[m.group(1).lower().replace("_", "-")]):
                problems.append("IGW route installed in a table that should not have one: " + ln)
        if "--nat-gateway-id" in ln and "RT_RT_PUBLIC" in ln:
            problems.append("NAT route in the public route table")
    return problems


# ---- Syllabus extension: configuring strict security groups ---

SENSITIVE_PORTS = {22: "ssh", 23: "telnet", 445: "smb", 1433: "mssql", 2375: "docker-api", 3306: "mysql", 3389: "rdp",
                   5432: "postgres", 6379: "redis", 9200: "elasticsearch", 27017: "mongodb"}
WORLD_CIDRS = {"0.0.0.0/0", "::/0"}


def audit_security_groups(groups: dict[str, list[SecurityRule]], public_ok_ports: frozenset[int] = frozenset({80, 443}),
                          public_groups: frozenset[str] = frozenset()) -> list[Finding]:
    """Flags world-open rules. `public_groups` (e.g. an ALB's SG) may expose public_ok_ports to the world."""
    out: list[Finding] = []
    for sg, rules in groups.items():
        for r in rules:
            if r.source_sg:
                continue  # SG-to-SG references are the preferred, least-exposed form
            assert r.source_cidr is not None
            net = ipaddress.ip_network(r.source_cidr, strict=False)
            span = range(r.port_from, r.port_to + 1)
            exposed = sorted(p for p in SENSITIVE_PORTS if p in span)
            subject = "%s %s/%d-%d from %s" % (sg, r.protocol, r.port_from, r.port_to, r.source_cidr)
            if r.source_cidr in WORLD_CIDRS:
                if r.protocol == "-1" or (r.port_from == 0 and r.port_to == 65535):
                    out.append(Finding(area="security-group", severity="CRITICAL", subject=subject, message="all ports open to the internet"))
                elif exposed:
                    names = ",".join(SENSITIVE_PORTS[p] for p in exposed)
                    out.append(Finding(area="security-group", severity="CRITICAL", subject=subject, message="sensitive service open to the internet: " + names))
                elif not (sg in public_groups and set(span) <= public_ok_ports):
                    out.append(Finding(area="security-group", severity="HIGH", subject=subject, message="non-web port open to the internet"))
            elif exposed and net.prefixlen <= 8:
                out.append(Finding(area="security-group", severity="MEDIUM", subject=subject, message="sensitive port open to a /%d" % net.prefixlen))
    return out


def tighten_world_rules(groups: dict[str, list[SecurityRule]], replacement_cidr: str,
                        public_groups: frozenset[str] = frozenset()) -> dict[str, list[SecurityRule]]:
    """Rewrites flagged world-open rules to a trusted CIDR (e.g. VPN range); leaves legitimate public web rules alone."""
    fixed: dict[str, list[SecurityRule]] = {}
    for sg, rules in groups.items():
        keep: list[SecurityRule] = []
        for r in rules:
            if audit_security_groups({sg: [r]}, public_groups=public_groups):
                keep.append(r.model_copy(update={"source_cidr": replacement_cidr}))
            else:
                keep.append(r)
        fixed[sg] = keep
    return fixed


def sg_revoke_cli(group_id: str, rule: SecurityRule) -> str:
    """The CLI command that removes a flagged rule (the console equivalent: Edit inbound rules -> Delete)."""
    return shlex.join(["aws", "ec2", "revoke-security-group-ingress", "--group-id", group_id, "--protocol", rule.protocol,
                       "--port", "%d-%d" % (rule.port_from, rule.port_to) if rule.port_from != rule.port_to else str(rule.port_from),
                       "--cidr", rule.source_cidr or ""])


# ---- Syllabus extension: principle of least privilege and creating identity policies ---

ACTION_CATALOG: dict[str, list[str]] = {
    "s3": ["GetObject", "PutObject", "DeleteObject", "ListBucket", "GetObjectVersion", "PutBucketPolicy", "DeleteBucket"],
    "sqs": ["SendMessage", "ReceiveMessage", "DeleteMessage", "CreateQueue", "PurgeQueue"],
    "kms": ["Decrypt", "Encrypt", "GenerateDataKey", "ScheduleKeyDeletion"],
    "dynamodb": ["GetItem", "PutItem", "Query", "Scan", "DeleteTable"],
    "logs": ["CreateLogStream", "PutLogEvents"],
    "iam": ["PassRole", "CreateUser", "AttachRolePolicy"],
}
ALL_ACTIONS = [svc + ":" + a for svc, acts in ACTION_CATALOG.items() for a in acts]


def expand_actions(patterns: list[str]) -> set[str]:
    """Expands wildcards against the catalog so 's3:*' becomes the concrete actions it grants."""
    out: set[str] = set()
    for p in patterns:
        hits = [a for a in ALL_ACTIONS if fnmatch.fnmatchcase(a, p)]
        out.update(hits or [p])
    return out


class UsageRecord(BaseModel):
    """One observed call, e.g. distilled from CloudTrail or IAM Access Analyzer."""
    action: str
    resource: str
    source_vpce: str | None = None


class LeastPrivilegeReport(BaseModel):
    granted: list[str]
    used: list[str]
    unused: list[str]
    used_but_not_granted: list[str]
    risky: list[Finding]
    tightened: PolicyDocument
    reduction_pct: float


def analyze_least_privilege(policy: PolicyDocument, usage: list[UsageRecord]) -> LeastPrivilegeReport:
    """Diffs granted vs used actions and proposes a tighter policy covering exactly the observed calls."""
    allows = [s for s in policy.statements if s.effect is Effect.ALLOW]
    granted = expand_actions([a for s in allows for a in s.actions])
    used = {u.action for u in usage}
    risky: list[Finding] = []
    for i, s in enumerate(allows):
        wide_res = any(r == "*" for r in s.resources)
        if any(a == "*" or a.endswith(":*") for a in s.actions) and wide_res:
            risky.append(Finding(area="iam", severity="HIGH", subject="statement %d" % i, message="wildcard action on wildcard resource"))
        if "iam:PassRole" in expand_actions(s.actions) and wide_res:
            risky.append(Finding(area="iam", severity="HIGH", subject="statement %d" % i, message="iam:PassRole on * enables privilege escalation"))
    by_svc: dict[str, tuple[set[str], set[str]]] = {}
    for u in usage:
        acts, res = by_svc.setdefault(u.action.split(":")[0], (set(), set()))
        acts.add(u.action)
        res.add(u.resource)
    vpces = {s.condition_source_vpce for s in allows}
    shared_vpce = vpces.pop() if len(vpces) == 1 else None
    new_stmts = [Statement(effect=Effect.ALLOW, actions=sorted(a), resources=sorted(r), condition_source_vpce=shared_vpce)
                 for _, (a, r) in sorted(by_svc.items())]
    new_stmts += [s for s in policy.statements if s.effect is Effect.DENY]  # keep guardrails
    tightened = PolicyDocument(statements=new_stmts)
    after = expand_actions([a for s in new_stmts if s.effect is Effect.ALLOW for a in s.actions])
    pct = round(100 * (1 - len(after) / len(granted)), 1) if granted else 0.0
    return LeastPrivilegeReport(granted=sorted(granted), used=sorted(used), unused=sorted(granted - used),
                                used_but_not_granted=sorted(used - granted), risky=risky, tightened=tightened, reduction_pct=pct)


def verify_tightened(old: PolicyDocument, new: PolicyDocument, usage: list[UsageRecord]) -> list[str]:
    """Regression: every call that worked before (and was observed) must still work; nothing new may open up."""
    problems: list[str] = []
    for u in usage:
        req = AccessRequest(action=u.action, resource=u.resource, source_vpce=u.source_vpce)
        was, _ = evaluate_iam(old, req)
        now, _ = evaluate_iam(new, req)
        if was and not now:
            problems.append("regression: %s on %s now denied" % (u.action, u.resource))
        if now and not was:
            problems.append("escalation: %s on %s newly allowed" % (u.action, u.resource))
    return problems


IAM_ACTION_RE = re.compile(r"^(\*|[a-z0-9-]+:[A-Za-z0-9*?]+)$")
IAM_MAX_MANAGED_POLICY_CHARS = 6144


def to_iam_json(policy: PolicyDocument) -> dict[str, Any]:
    """Body for: aws iam create-policy --policy-name N --policy-document file://policy.json"""
    stmts: list[dict[str, Any]] = []
    for i, s in enumerate(policy.statements):
        d: dict[str, Any] = {"Sid": "S%d" % i, "Effect": s.effect.value, "Action": s.actions, "Resource": s.resources}
        if s.condition_source_vpce:
            d["Condition"] = {"StringEquals": {"aws:SourceVpce": s.condition_source_vpce}}
        stmts.append(d)
    return {"Version": "2012-10-17", "Statement": stmts}


def validate_iam_json(doc: dict[str, Any]) -> None:
    if doc.get("Version") != "2012-10-17":
        raise ProvisioningError("Version must be 2012-10-17")
    stmts = doc.get("Statement")
    if not isinstance(stmts, list) or not stmts:
        raise ProvisioningError("Statement must be a non-empty list")
    for s in stmts:
        if s.get("Effect") not in ("Allow", "Deny"):
            raise ProvisioningError("bad Effect")
        for a in s.get("Action", []):
            if not IAM_ACTION_RE.match(a):
                raise ProvisioningError("bad action " + a)
        if not s.get("Resource"):
            raise ProvisioningError("identity policies need Resource")
    if len(json.dumps(doc, separators=(",", ":"))) > IAM_MAX_MANAGED_POLICY_CHARS:
        raise ProvisioningError("exceeds managed policy size limit")


def from_iam_json(doc: dict[str, Any]) -> PolicyDocument:
    validate_iam_json(doc)
    return PolicyDocument(statements=[
        Statement(effect=Effect(s["Effect"]), actions=s["Action"], resources=s["Resource"],
                  condition_source_vpce=s.get("Condition", {}).get("StringEquals", {}).get("aws:SourceVpce"))
        for s in doc["Statement"]])


# ---- Syllabus extension: assuming cross-account roles ---

class TrustStatement(BaseModel):
    effect: Effect = Effect.ALLOW
    principals: list[str] = Field(min_length=1)  # 12-digit account id, role/user ARN, or "*"
    external_id: str | None = None
    require_mfa: bool = False
    source_ip_cidrs: list[str] = Field(default_factory=list)
    principal_org_id: str | None = None


class TrustPolicy(BaseModel):
    role_name: str
    account_id: str = Field(pattern=r"^\d{12}$")
    statements: list[TrustStatement]
    max_session_seconds: int = Field(default=3600, ge=3600, le=43200)

    @property
    def role_arn(self) -> str:
        return "arn:aws:iam::%s:role/%s" % (self.account_id, self.role_name)


class AssumeRoleRequest(BaseModel):
    caller_arn: str
    caller_org_id: str | None = None
    external_id: str | None = None
    mfa_present: bool = False
    source_ip: str = "0.0.0.0"
    session_name: str = Field(pattern=r"^[\w+=,.@-]{2,64}$")
    duration_s: int = 3600
    role_chained: bool = False  # the caller is itself an assumed-role session


class AssumeDecision(BaseModel):
    allowed: bool
    reason: str
    session_arn: str | None = None
    expires_in_s: int | None = None


def caller_account(arn: str) -> str:
    return arn.split(":")[4]


def _principal_matches(principals: list[str], caller_arn: str) -> bool:
    acct = caller_account(caller_arn)
    for p in principals:
        if p == "*" or p == acct or p == "arn:aws:iam::%s:root" % acct or p == caller_arn:
            return True
    return False


def _trust_conditions_ok(st: TrustStatement, req: AssumeRoleRequest) -> tuple[bool, str]:
    if st.external_id is not None and req.external_id != st.external_id:
        return False, "sts:ExternalId missing or wrong"
    if st.require_mfa and not req.mfa_present:
        return False, "MFA required"
    if st.source_ip_cidrs and not any(ipaddress.ip_address(req.source_ip) in ipaddress.ip_network(c) for c in st.source_ip_cidrs):
        return False, "source IP not allowed"
    if st.principal_org_id is not None and req.caller_org_id != st.principal_org_id:
        return False, "caller not in the required AWS Organization"
    return True, "ok"


def can_assume(caller_policy: PolicyDocument, trust: TrustPolicy, req: AssumeRoleRequest) -> AssumeDecision:
    """Cross-account AssumeRole needs BOTH sides: the role's trust policy and the caller's identity policy."""
    if req.role_chained and req.duration_s > 3600:
        return AssumeDecision(allowed=False, reason="role chaining caps the session at 3600s")
    if req.duration_s > trust.max_session_seconds:
        return AssumeDecision(allowed=False, reason="duration exceeds the role's max session duration")
    why = "no trust statement matches the caller"
    trusted = False
    for st in trust.statements:
        if not _principal_matches(st.principals, req.caller_arn):
            continue
        ok, why = _trust_conditions_ok(st, req)
        if st.effect is Effect.DENY and ok:
            return AssumeDecision(allowed=False, reason="explicit deny in trust policy")
        if st.effect is Effect.ALLOW and ok:
            trusted = True
    if not trusted:
        return AssumeDecision(allowed=False, reason="trust policy: " + why)
    if caller_account(req.caller_arn) != trust.account_id:
        allowed, _ = evaluate_iam(caller_policy, AccessRequest(action="sts:AssumeRole", resource=trust.role_arn))
        if not allowed:
            return AssumeDecision(allowed=False, reason="caller's identity policy does not allow sts:AssumeRole on the role")
    arn = "arn:aws:sts::%s:assumed-role/%s/%s" % (trust.account_id, trust.role_name, req.session_name)
    return AssumeDecision(allowed=True, reason="ok", session_arn=arn, expires_in_s=req.duration_s)


def audit_trust_policy(trust: TrustPolicy) -> list[Finding]:
    f: list[Finding] = []
    for i, st in enumerate(trust.statements):
        if st.effect is not Effect.ALLOW:
            continue
        guarded = bool(st.external_id or st.principal_org_id or st.source_ip_cidrs)
        for p in st.principals:
            if p == "*" and not guarded:
                f.append(Finding(area="trust", severity="CRITICAL", subject="statement %d" % i, message="any AWS principal can assume this role"))
            elif p != "*" and caller_account(p if p.startswith("arn:") else "arn:aws:iam::%s:root" % p) != trust.account_id \
                    and not (st.external_id or st.principal_org_id):
                f.append(Finding(area="trust", severity="HIGH", subject="statement %d" % i,
                                 message="third-party account without ExternalId (confused-deputy risk)"))
    return f


def trust_json(trust: TrustPolicy) -> dict[str, Any]:
    """Body for: aws iam create-role --role-name N --assume-role-policy-document file://trust.json"""
    stmts: list[dict[str, Any]] = []
    for st in trust.statements:
        cond: dict[str, Any] = {}
        string_equals: dict[str, str] = {}
        if st.external_id:
            string_equals["sts:ExternalId"] = st.external_id
        if st.principal_org_id:
            string_equals["aws:PrincipalOrgID"] = st.principal_org_id
        if string_equals:
            cond["StringEquals"] = string_equals
        if st.require_mfa:
            cond["Bool"] = {"aws:MultiFactorAuthPresent": "true"}
        if st.source_ip_cidrs:
            cond["IpAddress"] = {"aws:SourceIp": st.source_ip_cidrs}
        arns = [p if (p == "*" or p.startswith("arn:")) else "arn:aws:iam::%s:root" % p for p in st.principals]
        d: dict[str, Any] = {"Effect": st.effect.value, "Principal": {"AWS": arns}, "Action": "sts:AssumeRole"}
        if cond:
            d["Condition"] = cond
        stmts.append(d)
    return {"Version": "2012-10-17", "Statement": stmts}


def validate_trust_json(doc: dict[str, Any]) -> None:
    validate_iam_json({"Version": doc.get("Version"), "Statement": [{**s, "Action": [s.get("Action", "")], "Resource": ["x"]} for s in doc.get("Statement", [])]})
    for s in doc["Statement"]:
        if "AWS" not in s.get("Principal", {}):
            raise ProvisioningError("trust statements need Principal.AWS")


def assume_role_cli(role_arn: str, session: str, external_id: str | None, duration: int) -> str:
    parts = ["aws", "sts", "assume-role", "--role-arn", role_arn, "--role-session-name", session, "--duration-seconds", str(duration)]
    if external_id:
        parts += ["--external-id", external_id]
    return shlex.join(parts)


async def assume_role_boto3(role_arn: str, session: str, external_id: str | None = None, duration: int = 3600) -> dict[str, str]:
    """Real adapter. Returns only non-secret session metadata; credentials never leave the SDK call."""
    client = boto3_client("sts")
    kwargs: dict[str, Any] = {"RoleArn": role_arn, "RoleSessionName": session, "DurationSeconds": duration}
    if external_id:
        kwargs["ExternalId"] = external_id
    resp = await call_aws(client.assume_role, **kwargs)
    return {"assumed_role_arn": resp["AssumedRoleUser"]["Arn"], "expiration": str(resp["Credentials"]["Expiration"])}


# ---- Syllabus extension: AWS Billing console, cost allocation by tag, automated budget thresholds ---

class CostRecord(BaseModel):
    day: date
    service: str
    usd: Decimal
    tags: dict[str, str] = Field(default_factory=dict)


class CostRollup(BaseModel):
    by_tag: dict[str, Decimal]
    total: Decimal
    untagged: Decimal
    tag_coverage_pct: float


def rollup_by_tag(records: list[CostRecord], tag_key: str) -> CostRollup:
    """Chargeback view: spend per tag value plus how much spend is invisible because it is untagged."""
    by: dict[str, Decimal] = {}
    for r in records:
        k = r.tags.get(tag_key, "(untagged)")
        by[k] = by.get(k, Decimal(0)) + r.usd
    total = sum(by.values(), Decimal(0))
    untagged = by.get("(untagged)", Decimal(0))
    cov = float((total - untagged) / total * 100) if total else 100.0
    return CostRollup(by_tag=dict(sorted(by.items())), total=total, untagged=untagged, tag_coverage_pct=round(cov, 1))


def rollup_by_service(records: list[CostRecord]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for r in records:
        out[r.service] = out.get(r.service, Decimal(0)) + r.usd
    return dict(sorted(out.items(), key=lambda kv: kv[1], reverse=True))


def forecast_month_end(spend_to_date: Decimal, day_of_month: int, year: int, month: int) -> Decimal:
    """Straight-line projection (a simplification of what Cost Explorer / Budgets forecast)."""
    dim = calendar.monthrange(year, month)[1]
    return (spend_to_date / day_of_month * dim).quantize(Decimal("0.01"))


class Budget(BaseModel):
    name: str
    limit_usd: Decimal = Field(gt=0)
    actual_thresholds_pct: list[int] = Field(default_factory=lambda: [50, 80, 100])
    forecast_threshold_pct: int | None = 100
    emails: list[str] = Field(min_length=1)


class BudgetAlert(BaseModel):
    threshold_pct: int
    basis: Literal["ACTUAL", "FORECASTED"]
    spend: Decimal
    limit: Decimal


class BudgetMonitor:
    """Fires each (threshold, basis) alert once per budget period, like an SNS/email notification would."""

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self._fired: set[tuple[int, str]] = set()

    def evaluate(self, actual: Decimal, forecast: Decimal) -> list[BudgetAlert]:
        out: list[BudgetAlert] = []
        lim = self.budget.limit_usd
        checks = [(t, "ACTUAL", actual) for t in sorted(self.budget.actual_thresholds_pct)]
        if self.budget.forecast_threshold_pct:
            checks.append((self.budget.forecast_threshold_pct, "FORECASTED", forecast))
        for pct, basis, value in checks:
            if value >= lim * pct / 100 and (pct, basis) not in self._fired:
                self._fired.add((pct, basis))
                out.append(BudgetAlert(threshold_pct=pct, basis=basis, spend=value, limit=lim))  # type: ignore[arg-type]
        return out


def budget_artifacts(b: Budget, account_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """(budget.json, notifications.json, CLI) for: aws budgets create-budget"""
    budget = {"BudgetName": b.name, "BudgetLimit": {"Amount": str(b.limit_usd), "Unit": "USD"}, "TimeUnit": "MONTHLY", "BudgetType": "COST"}
    subs = [{"SubscriptionType": "EMAIL", "Address": e} for e in b.emails]
    notes = [{"Notification": {"NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN", "Threshold": float(t),
                               "ThresholdType": "PERCENTAGE"}, "Subscribers": subs} for t in b.actual_thresholds_pct]
    if b.forecast_threshold_pct:
        notes.append({"Notification": {"NotificationType": "FORECASTED", "ComparisonOperator": "GREATER_THAN",
                                       "Threshold": float(b.forecast_threshold_pct), "ThresholdType": "PERCENTAGE"}, "Subscribers": subs})
    cli = shlex.join(["aws", "budgets", "create-budget", "--account-id", account_id, "--budget", "file://budget.json",
                      "--notifications-with-subscribers", "file://notifications.json"])
    return budget, notes, cli


def validate_budget_artifacts(b: Budget, budget: dict[str, Any], notes: list[dict[str, Any]], cli: str) -> list[str]:
    problems: list[str] = []
    if Decimal(budget["BudgetLimit"]["Amount"]) != b.limit_usd:
        problems.append("limit mismatch")
    if budget["TimeUnit"] != "MONTHLY":
        problems.append("expected a monthly budget")
    got = sorted(int(n["Notification"]["Threshold"]) for n in notes if n["Notification"]["NotificationType"] == "ACTUAL")
    if got != sorted(b.actual_thresholds_pct):
        problems.append("actual thresholds mismatch")
    if any(not n["Subscribers"] for n in notes):
        problems.append("notification without a subscriber")
    if "--notifications-with-subscribers" not in shlex.split(cli):
        problems.append("CLI missing notifications")
    return problems


def cost_explorer_cli(start: str, end: str, tag_key: str) -> str:
    return shlex.join(["aws", "ce", "get-cost-and-usage", "--time-period", "Start=%s,End=%s" % (start, end),
                       "--granularity", "MONTHLY", "--metrics", "UnblendedCost", "--group-by", "Type=TAG,Key=" + tag_key])


async def fetch_costs_boto3(start: str, end: str, tag_key: str) -> list[CostRecord]:
    """Real adapter for Cost Explorer (the data behind the console's Cost Explorer page). Not run offline."""
    client = boto3_client("ce")
    resp = await call_aws(client.get_cost_and_usage, TimePeriod={"Start": start, "End": end}, Granularity="DAILY",
                          Metrics=["UnblendedCost"], GroupBy=[{"Type": "TAG", "Key": tag_key}])
    out: list[CostRecord] = []
    for period in resp["ResultsByTime"]:
        for g in period["Groups"]:
            value = g["Keys"][0].split("$", 1)[1]  # Cost Explorer returns "tagkey$value"
            out.append(CostRecord(day=date.fromisoformat(period["TimePeriod"]["Start"]), service="(all)",
                                  usd=Decimal(g["Metrics"]["UnblendedCost"]["Amount"]), tags={tag_key: value} if value else {}))
    return out


# ---- Syllabus extension: demo ---

async def extension_demo() -> None:
    """Runs every syllabus-extension section offline and logs a one-line verdict per topic."""
    # 1. VM provisioning
    spec = VmLaunchSpec(name="api-1", ami_id="ami-0123456789abcdef0", instance_type="c5.xlarge", subnet_id="subnet-0a1b2c3d",
                        security_group_ids=["sg-0a1b2c3d"], root_volume_gb=30, iam_instance_profile="app-runtime",
                        tags={"owner": "platform", "cost-center": "cc-42"})
    rates = RateCard(per_vcpu_hour=Decimal("0.02"), per_gib_hour=Decimal("0.005"), ebs_gb_month=Decimal("0.08"))  # DEMO numbers, not real prices
    cli, hcl = vm_cli(spec), vm_terraform(spec)
    log.info("vm", extra={"ctx": {"pick_for_2vcpu_6gib": pick_instance(2, 6.0, rates), "monthly_demo_usd": monthly_cost(spec, rates),
                                  "artifact_problems": validate_vm_artifacts(spec, cli, hcl)}})
    try:
        VmLaunchSpec(**{**spec.model_dump(), "imdsv2_required": False})
    except ValueError as exc:
        log.info("vm rejected", extra={"ctx": {"why": "IMDSv2 off" if "IMDSv2" in str(exc) else str(exc)}})

    # 2. Object storage lifecycle
    rules = [LifecycleRule(id="logs", prefix="logs/", transitions=[LifecycleTransition(days=30, storage_class="STANDARD_IA"),
                                                                    LifecycleTransition(days=90, storage_class="GLACIER")],
                           expiration_days=365, noncurrent_expiration_days=30, abort_incomplete_multipart_days=7),
             LifecycleRule(id="tmp", prefix="tmp/", expiration_days=7)]
    objs = [ObjectRecord(key="logs/a.gz", size_bytes=10, age_days=10), ObjectRecord(key="logs/b.gz", size_bytes=10, age_days=45),
            ObjectRecord(key="logs/c.gz", size_bytes=10, age_days=120), ObjectRecord(key="logs/d.gz", size_bytes=10, age_days=400),
            ObjectRecord(key="tmp/x", size_bytes=1, age_days=8), ObjectRecord(key="logs/old-v", size_bytes=1, age_days=31, noncurrent=True)]
    doc = lifecycle_json(rules)
    log.info("lifecycle", extra={"ctx": {"outcomes": {o.key: o.final_class for o in apply_lifecycle(rules, objs)},
                                         "roundtrip_ok": lifecycle_from_json(doc) == rules,
                                         "findings": [x.message for x in lifecycle_findings(rules)]}})
    try:
        LifecycleRule(id="bad", transitions=[LifecycleTransition(days=10, storage_class="STANDARD_IA")])
    except ValueError:
        log.info("lifecycle rejected", extra={"ctx": {"why": "STANDARD_IA before 30 days"}})

    # 3. Managed database
    db = DbInstanceConfig(identifier="orders-prod", engine="postgres", instance_class="db.m5.large", allocated_storage_gb=100,
                          environment="prod", multi_az=True, backup_retention_days=14, deletion_protection=True,
                          subnet_azs=["us-east-1a", "us-east-1b"], vpc_security_group_ids=["sg-0a1b2c3d"],
                          parameters={"rds.force_ssl": "1", "max_connections": "200"}, expected_connections=150)
    weak = db.model_copy(update={"multi_az": False, "parameters": {"max_connections": "100"}, "storage_encrypted": False})
    dcli, dhcl = db_cli(db, "app-db-subnets"), db_terraform(db, "app-db-subnets")
    log.info("db", extra={"ctx": {"good_findings": [x.message for x in audit_db_config(db)], "weak_findings": len(audit_db_config(weak)),
                                  "artifact_problems": validate_db_artifacts(db, dcli, dhcl)}})

    # 4. Event-driven functions
    disp = EventDispatcher()
    flaky = {"n": 0}

    async def on_order(e: CloudEvent) -> str:
        return "order " + str(e.detail["order_id"])

    async def on_payment(e: CloudEvent) -> str:
        flaky["n"] += 1
        if flaky["n"] < 3:
            raise TransientHandlerError("ledger timeout")
        return "captured"

    async def on_audit(e: CloudEvent) -> str:
        if "amount" not in e.detail:
            raise PoisonEventError("malformed payload")
        return "audited"

    async def always_down(e: CloudEvent) -> str:
        raise TransientHandlerError("dependency down")

    disp.register(EventPattern(source=["shop"], detail_type=["order.created"]), FunctionConfig(name="order-fn"), on_order)
    disp.register(EventPattern(source=["pay"], detail_type=["payment.captured"], detail={"currency": ["USD"]}), FunctionConfig(name="payment-fn"), on_payment)
    disp.register(EventPattern(source=["pay"]), FunctionConfig(name="audit-fn", max_retries=0), on_audit)
    disp.register(EventPattern(source=["crm"]), FunctionConfig(name="crm-fn", max_retries=1), always_down)
    events = [CloudEvent(id="e1", source="shop", detail_type="order.created", detail={"order_id": 7}),
              CloudEvent(id="e1", source="shop", detail_type="order.created", detail={"order_id": 7}),
              CloudEvent(id="e2", source="pay", detail_type="payment.captured", detail={"currency": "USD", "amount": 5}),
              CloudEvent(id="e3", source="pay", detail_type="refund", detail={}),
              CloudEvent(id="e4", source="crm", detail_type="sync"),
              CloudEvent(id="e5", source="unknown", detail_type="x")]
    results = [await disp.dispatch(e) for e in events]
    log.info("events", extra={"ctx": {"results": results, "dlq": [(d.function, d.attempts, d.error) for d in disp.dlq],
                                      "invocations": disp.invocations}})
    try:
        boto3_client("lambda")
        log.info("lambda adapter", extra={"ctx": {"boto3": "installed (adapter available, not invoked offline)"}})
    except OptionalDependencyError as exc:
        log.info("lambda adapter", extra={"ctx": {"boto3": "missing", "hint": str(exc)}})

    # 5-7. VPC, public vs private routing, NAT egress
    vpc = build_three_tier_vpc("prod", "10.50.0.0/16", ["us-east-1a", "us-east-1b"])
    kinds = {s.name: classify_subnet(vpc, s)[0].value for s in vpc.subnets}
    script = vpc_cli(vpc)
    broken = vpc.model_copy(deep=True)
    broken.route_tables["rt-app-a"] = [Route(destination="0.0.0.0/0", target="blackhole")]
    nat_in_private = vpc.model_copy(update={"nat_gateways": {"nat-a": "app-a", "nat-b": "public-b"}})
    try:
        validate_design(nat_in_private)
        nat_msg = "accepted"
    except NetworkError as exc:
        nat_msg = str(exc)
    log.info("vpc", extra={"ctx": {"classes": kinds, "findings": [x.message for x in validate_design(vpc)], "cli_lines": len(script),
                                   "cli_problems": validate_vpc_cli(script, vpc), "hosts_per_24": usable_hosts("10.50.0.0/24"),
                                   "nat_in_private_subnet": nat_msg}})
    log.info("egress", extra={"ctx": {
        "app-a->internet": egress_path(vpc, "app-a", "93.184.216.34").model_dump(),
        "data-a->internet": egress_path(vpc, "data-a", "93.184.216.34").model_dump(),
        "app-a (blackhole)": egress_path(broken, "app-a", "93.184.216.34").reason,
        "internet can initiate": {n: internet_can_initiate(vpc, n) for n in ("public-a", "app-a", "data-a")}}})

    # 8. Strict security groups
    groups = {"sg-bastion": [SecurityRule(port_from=22, port_to=22, source_cidr="0.0.0.0/0")],
              "sg-alb": [SecurityRule(port_from=443, port_to=443, source_cidr="0.0.0.0/0")],
              "sg-db": [SecurityRule(port_from=5432, port_to=5432, source_sg="sg-app"), SecurityRule(port_from=3306, port_to=3306, source_cidr="10.0.0.0/8")],
              "sg-lab": [SecurityRule(protocol="-1", port_from=0, port_to=65535, source_cidr="0.0.0.0/0")]}
    pub = frozenset({"sg-alb"})
    before = audit_security_groups(groups, public_groups=pub)
    after = audit_security_groups(tighten_world_rules(groups, "10.20.0.0/16", pub), public_groups=pub)
    log.info("security groups", extra={"ctx": {"before": [(x.severity, x.subject) for x in before], "critical_after_fix": has_severity(after, "CRITICAL"),
                                               "revoke_cmd": sg_revoke_cli("sg-0a1b2c3d", groups["sg-bastion"][0])}})

    # 9-10. Least privilege + identity policies
    broad = PolicyDocument(statements=[
        Statement(effect=Effect.ALLOW, actions=["s3:*", "sqs:*", "iam:PassRole"], resources=["*"]),
        Statement(effect=Effect.DENY, actions=["s3:DeleteBucket"], resources=["*"])])
    usage = [UsageRecord(action="s3:GetObject", resource="arn:aws:s3:::acme-docs/a.pdf"),
             UsageRecord(action="s3:ListBucket", resource="arn:aws:s3:::acme-docs"),
             UsageRecord(action="sqs:SendMessage", resource="arn:aws:sqs:us-east-1:111122223333:jobs")]
    rep = analyze_least_privilege(broad, usage)
    iam_doc = to_iam_json(rep.tightened)
    validate_iam_json(iam_doc)
    log.info("least privilege", extra={"ctx": {"granted": len(rep.granted), "used": len(rep.used), "unused": len(rep.unused),
                                               "reduction_pct": rep.reduction_pct, "risky": [x.message for x in rep.risky],
                                               "verify": verify_tightened(broad, rep.tightened, usage),
                                               "roundtrip_ok": from_iam_json(iam_doc) == rep.tightened,
                                               "create_cmd": "aws iam create-policy --policy-name app-min --policy-document file://policy.json"}})

    # 11. Cross-account AssumeRole
    trust = TrustPolicy(role_name="audit-reader", account_id="111122223333", max_session_seconds=7200, statements=[
        TrustStatement(principals=["444455556666"], external_id="ext-demo-1", require_mfa=False, source_ip_cidrs=["203.0.113.0/24"])])
    caller_policy = PolicyDocument(statements=[Statement(effect=Effect.ALLOW, actions=["sts:AssumeRole"], resources=[trust.role_arn])])
    base = {"caller_arn": "arn:aws:iam::444455556666:role/vendor", "external_id": "ext-demo-1", "source_ip": "203.0.113.9", "session_name": "vendor-audit"}
    tcases = {
        "ok": can_assume(caller_policy, trust, AssumeRoleRequest(**base)),
        "wrong external id": can_assume(caller_policy, trust, AssumeRoleRequest(**{**base, "external_id": "guess"})),
        "bad ip": can_assume(caller_policy, trust, AssumeRoleRequest(**{**base, "source_ip": "198.51.100.7"})),
        "no identity policy": can_assume(PolicyDocument(statements=[Statement(effect=Effect.ALLOW, actions=["s3:GetObject"], resources=["*"])]),
                                         trust, AssumeRoleRequest(**base)),
        "chained 2h": can_assume(caller_policy, trust, AssumeRoleRequest(**{**base, "role_chained": True, "duration_s": 7200})),
        "stranger": can_assume(caller_policy, trust, AssumeRoleRequest(**{**base, "caller_arn": "arn:aws:iam::999900001111:role/x"})),
    }
    weak_trust = TrustPolicy(role_name="oops", account_id="111122223333", statements=[TrustStatement(principals=["*"]), TrustStatement(principals=["444455556666"])])
    tdoc = trust_json(trust)
    validate_trust_json(tdoc)
    log.info("assume role", extra={"ctx": {"decisions": {k: (v.allowed, v.reason) for k, v in tcases.items()}, "session": tcases["ok"].session_arn,
                                           "weak_trust_findings": [(x.severity, x.message) for x in audit_trust_policy(weak_trust)],
                                           "cli": assume_role_cli(trust.role_arn, "vendor-audit", "ext-demo-1", 3600)}})

    # 12-13. Billing: allocation by tag, budget thresholds
    recs = [CostRecord(day=date(2026, 10, d), service=svc, usd=Decimal(amt), tags=tags)
            for d in range(1, 14) for svc, amt, tags in (("AmazonEC2", "40.00", {"cost-center": "platform"}), ("AmazonRDS", "25.50", {"cost-center": "data"}),
                                                          ("AmazonS3", "6.25", {}), ("NATGateway", "9.00", {"cost-center": "platform"}))]
    roll = rollup_by_tag(recs, "cost-center")
    b = Budget(name="platform-monthly", limit_usd=Decimal("1000"), emails=["finops@example.com"])
    bj, bn, bcli = budget_artifacts(b, "111122223333")
    mon = BudgetMonitor(b)
    timeline: list[Any] = []
    for day in (4, 7, 10, 13):
        spend = sum((r.usd for r in recs if r.day.day <= day), Decimal(0))
        fc = forecast_month_end(spend, day, 2026, 10)
        timeline.append((day, str(spend), str(fc), [(a.basis, a.threshold_pct) for a in mon.evaluate(spend, fc)]))
    log.info("billing", extra={"ctx": {"by_tag": {k: str(v) for k, v in roll.by_tag.items()}, "coverage_pct": roll.tag_coverage_pct,
                                       "top_service": next(iter(rollup_by_service(recs))), "alerts": timeline,
                                       "artifact_problems": validate_budget_artifacts(b, bj, bn, bcli),
                                       "ce_cmd": cost_explorer_cli("2026-10-01", "2026-11-01", "cost-center")}})

    # Resilience: a throttled provider call recovers through retry_async
    state = {"n": 0}

    async def flaky() -> str:
        state["n"] += 1
        if state["n"] < 3:
            raise CloudApiThrottled("Throttling")
        return "ok"

    log.info("retry_async", extra={"ctx": {"result": await retry_async(flaky), "calls": state["n"]}})


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
    await extension_demo()


if __name__ == "__main__":
    asyncio.run(main())
