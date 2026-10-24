"""Module 12 lab: JWT verification with key rotation, RBAC, and tenant-isolated vector retrieval.

JWT: HS256 via stdlib hmac with kid-based key lookup and strict claim checks (production uses RS256/ES256 with
JWKS and a vetted library; the validation *logic* is identical). The vector store refuses queries without a
tenant filter, the filter is injected from the verified context, ACL groups are enforced inside the search
loop (pre-filter), and cross-tenant attempts are demonstrated and audited. Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import random
import sys
import time
from typing import Any

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


log = get_logger("lab12")


class SecurityError(Exception):
    http_status = 403


class AuthenticationError(SecurityError):
    http_status = 401


class AuthorizationError(SecurityError):
    http_status = 403


class IsolationViolation(SecurityError):
    """A query was built without a tenant filter: refuse (fail closed)."""


class JwksUnavailable(Exception):
    """Transient failure fetching signing keys; retried with backoff."""


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_jwt(claims: dict[str, Any], key: bytes, kid: str, alg: str = "HS256") -> str:
    header = {"alg": alg, "typ": "JWT", "kid": kid}
    signing_input = f"{b64u(json.dumps(header).encode())}.{b64u(json.dumps(claims).encode())}"
    sig = hmac.new(key, signing_input.encode(), hashlib.sha256).digest() if alg != "none" else b""
    return f"{signing_input}.{b64u(sig)}"


class Claims(BaseModel):
    iss: str
    aud: str
    sub: str
    tid: str = Field(min_length=1, description="tenant id claim")
    exp: int
    nbf: int = 0
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)


class RequestContext(BaseModel, frozen=True):
    tenant_id: str
    user: str
    roles: frozenset[str]
    groups: frozenset[str]


class KeySet:
    """Stand-in for a JWKS endpoint: kid -> secret, with rotation and a transient failure mode."""

    def __init__(self, keys: dict[str, bytes]) -> None:
        self._keys, self.fetches, self.fail_next = dict(keys), 0, 0

    async def fetch(self) -> dict[str, bytes]:
        self.fetches += 1
        await asyncio.sleep(0.001)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise JwksUnavailable("jwks endpoint timeout")
        return dict(self._keys)

    def rotate(self, kid: str, key: bytes) -> None:
        self._keys[kid] = key


class JwtVerifier:
    ALLOWED_ALGS = {"HS256"}  # prod: {"RS256", "ES256"}; never trust the token's own alg claim

    def __init__(self, jwks: KeySet, issuer: str, audience: str, leeway_s: int = 30, clock: Any = time.time) -> None:
        self.jwks, self.issuer, self.audience, self.leeway, self.clock = jwks, issuer, audience, leeway_s, clock
        self._cache: dict[str, bytes] = {}
        self._last_refresh = 0.0

    async def _key_for(self, kid: str) -> bytes:
        if kid in self._cache:
            return self._cache[kid]
        if self.clock() - self._last_refresh < 0.05 and self._cache:  # unknown-kid refresh rate limit (anti-DoS)
            raise AuthenticationError("unknown signing key")
        for attempt in range(1, 5):
            try:
                self._cache = await self.jwks.fetch()
                self._last_refresh = self.clock()
                break
            except JwksUnavailable:
                if attempt == 4:
                    raise AuthenticationError("cannot verify token right now") from None
                await asyncio.sleep(random.uniform(0, 0.004 * 2 ** attempt))
        if kid not in self._cache:
            raise AuthenticationError("unknown signing key")
        return self._cache[kid]

    async def verify(self, token: str) -> Claims:
        try:
            h_b64, p_b64, s_b64 = token.split(".")
            header = json.loads(b64u_dec(h_b64))
            payload = json.loads(b64u_dec(p_b64))
        except (ValueError, json.JSONDecodeError) as exc:
            raise AuthenticationError("malformed token") from exc
        if header.get("alg") not in self.ALLOWED_ALGS:
            raise AuthenticationError(f"algorithm {header.get('alg')!r} not allowed")
        key = await self._key_for(str(header.get("kid", "")))
        expected = hmac.new(key, f"{h_b64}.{p_b64}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, b64u_dec(s_b64)):
            raise AuthenticationError("bad signature")
        try:
            claims = Claims.model_validate(payload)
        except ValueError as exc:
            raise AuthenticationError("missing or invalid claims") from exc
        now = self.clock()
        if claims.iss != self.issuer or claims.aud != self.audience:
            raise AuthenticationError("wrong issuer or audience")
        if now > claims.exp + self.leeway or now < claims.nbf - self.leeway:
            raise AuthenticationError("token expired or not yet valid")
        return claims


ROLE_PERMISSIONS = {"viewer": {"docs:search"}, "editor": {"docs:search", "docs:write"}, "admin": {"docs:search", "docs:write", "tenant:admin"}}


def to_context(c: Claims) -> RequestContext:
    return RequestContext(tenant_id=c.tid, user=c.sub, roles=frozenset(c.roles), groups=frozenset(c.groups))


def authorize(ctx: RequestContext, permission: str) -> None:
    granted = set().union(*(ROLE_PERMISSIONS.get(r, set()) for r in ctx.roles)) if ctx.roles else set()
    if permission not in granted:
        raise AuthorizationError(f"{ctx.user} lacks {permission}")


class Doc(BaseModel):
    doc_id: str
    tenant_id: str
    acl_groups: frozenset[str]
    text: str


def embed(text: str, dim: int = 64) -> list[float]:
    v = [0.0] * dim
    for tok in text.lower().split():
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        v[h % dim] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class TenantVectorStore:
    """The only public search method takes a RequestContext; there is no code path to search without a tenant."""

    def __init__(self) -> None:
        self._rows: list[tuple[Doc, list[float]]] = []
        self.audit: list[dict[str, Any]] = []

    async def upsert(self, doc: Doc) -> None:
        self._rows.append((doc, embed(doc.text)))

    async def search(self, ctx: RequestContext, query: str, k: int = 3, *, raw_filter: dict[str, Any] | None = None) -> list[Doc]:
        if raw_filter and raw_filter.get("tenant_id") not in (None, ctx.tenant_id):
            self.audit.append({"user": ctx.user, "event": "cross_tenant_attempt", "requested": raw_filter.get("tenant_id")})
            raise IsolationViolation("request-supplied tenant filter does not match authenticated tenant")
        effective = {"tenant_id": ctx.tenant_id, "acl_any_of": sorted(ctx.groups)}
        q = embed(query)
        scored = []
        for doc, vec in self._rows:  # pre-filter inside the scan, before ranking: no cross-tenant top-k leakage
            if doc.tenant_id != effective["tenant_id"] or not (doc.acl_groups & ctx.groups):
                continue
            scored.append((sum(a * b for a, b in zip(q, vec)), doc))
        hits = [d for _, d in sorted(scored, key=lambda x: -x[0])[:k]]
        assert all(d.tenant_id == ctx.tenant_id for d in hits), "canary: cross-tenant row returned"
        self.audit.append({"user": ctx.user, "tenant": ctx.tenant_id, "filter": effective, "returned": [d.doc_id for d in hits]})
        return hits


async def handle_search(token: str, query: str, verifier: JwtVerifier, store: TenantVectorStore, raw_filter: dict[str, Any] | None = None) -> list[str]:
    ctx = to_context(await verifier.verify(token))
    authorize(ctx, "docs:search")
    return [d.doc_id for d in await store.search(ctx, query, raw_filter=raw_filter)]


async def main() -> None:
    now = int(time.time())
    key1, key2 = b"k1-secret-for-lab-only", b"k2-secret-for-lab-only"
    jwks = KeySet({"k1": key1})
    verifier = JwtVerifier(jwks, issuer="https://idp.example.com", audience="ai-gateway")
    store = TenantVectorStore()
    for d in (Doc(doc_id="a-1", tenant_id="acme", acl_groups=frozenset({"finance"}), text="acme quarterly revenue forecast confidential"),
              Doc(doc_id="a-2", tenant_id="acme", acl_groups=frozenset({"all"}), text="acme employee handbook vacation policy"),
              Doc(doc_id="b-1", tenant_id="globex", acl_groups=frozenset({"finance", "all"}), text="globex quarterly revenue forecast merger plans")):
        await store.upsert(d)

    def claims(**over: Any) -> dict[str, Any]:
        base = {"iss": "https://idp.example.com", "aud": "ai-gateway", "sub": "alice", "tid": "acme", "exp": now + 600,
                "roles": ["viewer"], "groups": ["finance", "all"]}
        return {**base, **over}

    good = sign_jwt(claims(), key1, "k1")
    log.info("acme finance user", extra={"ctx": {"hits": await handle_search(good, "quarterly revenue forecast", verifier, store)}})
    hr = sign_jwt(claims(sub="bob", groups=["all"]), key1, "k1")
    log.info("acme non-finance user (ACL)", extra={"ctx": {"hits": await handle_search(hr, "quarterly revenue forecast", verifier, store)}})
    globex = sign_jwt(claims(sub="carol", tid="globex"), key1, "k1")
    log.info("globex user", extra={"ctx": {"hits": await handle_search(globex, "quarterly revenue forecast", verifier, store)}})

    attacks: dict[str, tuple[str, dict[str, Any] | None]] = {
        "body-supplied tenant override": (good, {"tenant_id": "globex"}),
        "alg none": (sign_jwt(claims(), b"", "k1", alg="none"), None),
        "forged signature": (sign_jwt(claims(), b"attacker-key", "k1"), None),
        "expired": (sign_jwt(claims(exp=now - 3600), key1, "k1"), None),
        "wrong audience": (sign_jwt(claims(aud="other-app"), key1, "k1"), None),
        "missing tenant claim": (sign_jwt({k: v for k, v in claims().items() if k != "tid"}, key1, "k1"), None),
        "no permission": (sign_jwt(claims(roles=[]), key1, "k1"), None),
    }
    for name, (tok, raw) in attacks.items():
        try:
            await handle_search(tok, "revenue", verifier, store, raw)
            log.error("ATTACK SUCCEEDED", extra={"ctx": {"attack": name}})
        except SecurityError as exc:
            log.info("blocked", extra={"ctx": {"attack": name, "status": exc.http_status, "reason": str(exc)}})

    jwks.rotate("k2", key2)  # IdP rotates keys: unknown kid triggers one JWKS refresh (with transient failure + retry)
    jwks.fail_next = 1
    await asyncio.sleep(0.06)  # let the unknown-kid refresh rate limit (50 ms in the lab, ~30 s in prod) elapse
    rotated = sign_jwt(claims(), key2, "k2")
    log.info("after key rotation", extra={"ctx": {"hits": await handle_search(rotated, "employee handbook", verifier, store), "jwks_fetches": jwks.fetches}})
    log.info("audit tail", extra={"ctx": {"events": store.audit[-3:]}})


if __name__ == "__main__":
    asyncio.run(main())
