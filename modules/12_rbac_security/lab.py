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
import os
import random
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

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


# ======================================================================================================
# Syllabus extension (Module 12). Everything below runs offline with pydantic + stdlib. Third-party
# libraries (python3-saml) appear ONLY inside lazily-imported adapters.
# ======================================================================================================


class MissingDependency(Exception):
    """An optional library is not installed. Only adapters raise this; the offline path never needs it."""

    def __init__(self, package: str, install: str) -> None:
        super().__init__(f"optional dependency {package!r} is missing; install it with: {install}")


class MissingCredential(Exception):
    """A credential was not provided through the environment (we never hard-code or default secrets)."""


class OAuthError(SecurityError):
    """RFC 6749 style error: a machine-readable code plus a human description."""

    http_status = 400

    def __init__(self, error: str, description: str = "") -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.error, self.description = error, description


class SamlError(AuthenticationError):
    pass


class GraphUnavailable(Exception):
    """Transient Graph failure (503/timeout), retried with backoff."""


class GraphThrottled(GraphUnavailable):
    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class LabClock:
    """Controllable clock so token lifetimes, polling intervals and assertion windows are testable without sleeping."""

    def __init__(self, start: float | None = None) -> None:
        self.t = time.time() if start is None else start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


async def retry_io(fn: Callable[[], Awaitable[Any]], *, attempts: int = 4, base_s: float = 0.004,
                   retry_on: tuple[type[BaseException], ...] = (GraphUnavailable,), label: str = "io") -> Any:
    """Exponential backoff with jitter around any I/O; a server-provided Retry-After wins over our own guess."""
    for i in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on as exc:
            if i == attempts:
                raise
            wait = getattr(exc, "retry_after_s", None) or random.uniform(0, base_s * 2 ** i)
            log.info("retry", extra={"ctx": {"op": label, "attempt": i, "wait_s": round(wait, 4), "error": str(exc)}})
            await asyncio.sleep(wait)


# ---- Syllabus extension: authentication vs authorization (401 vs 403) ------------------------------
class HttpOutcome(BaseModel):
    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)


async def guarded_endpoint(authorization: str | None, verifier: JwtVerifier, permission: str, resource_tenant: str | None = None) -> HttpOutcome:
    """Order matters: authenticate (who?) -> authorize (may they?) -> isolate (is it theirs?).

    401 = we do not know who you are (missing or bad credentials; send WWW-Authenticate).
    403 = we know who you are and the answer is no.
    404 = the object belongs to another tenant: do not even confirm it exists.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return HttpOutcome(status=401, headers={"WWW-Authenticate": 'Bearer realm="api"'}, body={"error": "authentication required"})
    try:
        ctx = to_context(await verifier.verify(authorization[7:]))
    except AuthenticationError as exc:
        return HttpOutcome(status=401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}, body={"error": str(exc)})
    try:
        authorize(ctx, permission)
    except AuthorizationError as exc:
        return HttpOutcome(status=403, body={"error": str(exc)})
    if resource_tenant is not None and resource_tenant != ctx.tenant_id:
        return HttpOutcome(status=404, body={"error": "not found"})
    return HttpOutcome(status=200, body={"user": ctx.user, "tenant": ctx.tenant_id})


# ---- Syllabus extension: JWT anatomy (decoding is NOT verifying) ----------------------------------
class JwtAnatomy(BaseModel):
    header: dict[str, Any]
    payload: dict[str, Any]
    signature_bytes: int
    verified: bool = False  # decode_unverified can never set this to True
    findings: list[str]


REGISTERED_CLAIMS = {"iss": "issuer", "sub": "subject (stable user id)", "aud": "audience (who the token is for)", "exp": "expiry",
                     "nbf": "not before", "iat": "issued at", "jti": "unique token id (revocation, replay detection)"}


def decode_unverified(token: str, now: float) -> JwtAnatomy:
    """Split a JWT into header.payload.signature and decode the first two. Anyone can do this, including an attacker,
    which is exactly why nothing in the payload may be trusted before the signature is verified."""
    if len(token) > 8192:
        raise AuthenticationError("token too large")
    try:
        h_b64, p_b64, s_b64 = token.split(".")
        header, payload = json.loads(b64u_dec(h_b64)), json.loads(b64u_dec(p_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthenticationError("not a JWT") from exc
    findings = []
    if header.get("alg") in ("none", "None", None):
        findings.append("alg is none: unsigned token, must be rejected")
    if "kid" not in header:
        findings.append("no kid: verifier cannot pick a key from the JWKS")
    for name in ("iss", "aud", "sub", "exp"):
        if name not in payload:
            findings.append(f"missing registered claim {name}")
    if isinstance(payload.get("exp"), (int, float)):
        delta = int(payload["exp"] - now)
        findings.append(f"expires in {delta}s" if delta >= 0 else f"expired {-delta}s ago")
    findings.append("payload is only base64url-encoded, not encrypted: never put secrets in a JWT")
    return JwtAnatomy(header=header, payload=payload, signature_bytes=len(b64u_dec(s_b64)), findings=findings)


# ---- Syllabus extension: OAuth 2.0 grant types as a state machine ----------------------------------
def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def new_code_verifier() -> str:
    return secrets.token_urlsafe(64)  # 86 URL-safe characters; the spec allows 43 to 128


def pkce_challenge(verifier: str) -> str:
    return b64u(hashlib.sha256(verifier.encode()).digest())  # code_challenge_method=S256


class OAuthClient(BaseModel):
    client_id: str
    secret_sha256: str | None = None  # None means a public client (SPA, mobile, CLI): PKCE is then mandatory
    redirect_uris: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    grants: frozenset[str] = frozenset()


GRANT_AUTH_CODE = "authorization_code"
GRANT_CLIENT_CREDS = "client_credentials"
GRANT_DEVICE = "urn:ietf:params:oauth:grant-type:device_code"
GRANT_REFRESH = "refresh_token"
DEPRECATED_GRANTS = {"password": "resource owner password grant exposes user credentials to the client; removed in OAuth 2.1",
                     "implicit": "implicit puts tokens in the URL fragment; replaced by authorization code + PKCE"}
USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"  # no vowels or look-alike characters: easier to type from a TV screen


class AuthorizationServer:
    """In-memory authorization server. Access tokens are real signed JWTs, so the Module 12 verifier can accept them."""

    CODE_TTL, ACCESS_TTL, REFRESH_TTL, DEVICE_TTL = 60, 600, 86_400, 600

    def __init__(self, clients: list[OAuthClient], clock: LabClock, key: bytes, *, kid: str, issuer: str, audience: str, tenant: str) -> None:
        self.clients = {c.client_id: c for c in clients}
        self.clock, self.key, self.kid, self.issuer, self.audience, self.tenant = clock, key, kid, issuer, audience, tenant
        self.codes: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, dict[str, Any]] = {}
        self.revoked_families: set[str] = set()
        self.devices: dict[str, dict[str, Any]] = {}
        self.user_codes: dict[str, str] = {}

    # -- helpers
    def _client(self, client_id: str, secret: str | None, *, grant: str) -> OAuthClient:
        client = self.clients.get(client_id)
        if client is None:
            raise OAuthError("invalid_client", "unknown client")
        if client.secret_sha256 is not None and not (secret and hmac.compare_digest(sha256_hex(secret), client.secret_sha256)):
            raise OAuthError("invalid_client", "client authentication failed")
        if grant not in client.grants:
            raise OAuthError("unauthorized_client", f"client may not use {grant}")
        return client

    @staticmethod
    def _scope(client: OAuthClient, requested: str) -> str:
        wanted = set(requested.split())
        if not wanted <= client.scopes:
            raise OAuthError("invalid_scope", f"not allowed: {sorted(wanted - client.scopes)}")
        return " ".join(sorted(wanted))

    def _access_token(self, sub: str, scope: str, client_id: str) -> str:
        now = int(self.clock())
        claims = {"iss": self.issuer, "aud": self.audience, "sub": sub, "tid": self.tenant, "iat": now, "nbf": now, "exp": now + self.ACCESS_TTL,
                  "scope": scope, "azp": client_id, "roles": ["viewer"] if "docs:read" in scope.split() else [], "groups": []}
        return sign_jwt(claims, self.key, self.kid)

    def _issue(self, sub: str, scope: str, client_id: str, *, family: str | None = None, with_refresh: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {"access_token": self._access_token(sub, scope, client_id), "token_type": "Bearer", "expires_in": self.ACCESS_TTL, "scope": scope}
        if with_refresh:
            family = family or secrets.token_urlsafe(8)
            refresh = secrets.token_urlsafe(32)
            self.refresh_tokens[refresh] = {"family": family, "client_id": client_id, "sub": sub, "scope": scope, "used": False, "exp": self.clock() + self.REFRESH_TTL}
            out["refresh_token"] = refresh
        return out

    # -- authorization endpoint (front channel)
    def authorize(self, *, response_type: str, client_id: str, redirect_uri: str, scope: str, state: str, code_challenge: str | None,
                  code_challenge_method: str | None, user: str) -> str:
        """`user` is who just logged in and consented at the IdP; returns the redirect URL the browser follows."""
        client = self.clients.get(client_id)
        if client is None or redirect_uri not in client.redirect_uris:  # exact string match; never redirect to an unregistered URI
            raise OAuthError("invalid_request", "unregistered redirect_uri (no redirect is performed)")
        if response_type != "code":
            raise OAuthError("unsupported_response_type", DEPRECATED_GRANTS["implicit"] if response_type == "token" else "only response_type=code")
        if GRANT_AUTH_CODE not in client.grants:
            raise OAuthError("unauthorized_client", "authorization code grant not enabled")
        if client.secret_sha256 is None and not code_challenge:
            raise OAuthError("invalid_request", "public clients must use PKCE")
        if code_challenge and code_challenge_method != "S256":
            raise OAuthError("invalid_request", "only code_challenge_method=S256 is accepted (plain defeats PKCE)")
        granted = self._scope(client, scope)
        code = secrets.token_urlsafe(24)
        self.codes[code] = {"client_id": client_id, "redirect_uri": redirect_uri, "challenge": code_challenge, "user": user, "scope": granted,
                            "exp": self.clock() + self.CODE_TTL, "used": False, "family": secrets.token_urlsafe(8)}
        return f"{redirect_uri}?{urllib.parse.urlencode({'code': code, 'state': state})}"

    # -- token endpoint (back channel)
    def token(self, **form: str) -> dict[str, Any]:
        grant = form.get("grant_type", "")
        if grant in DEPRECATED_GRANTS:
            raise OAuthError("unsupported_grant_type", DEPRECATED_GRANTS[grant])
        handler = {GRANT_AUTH_CODE: self._code_grant, GRANT_CLIENT_CREDS: self._client_credentials, GRANT_DEVICE: self._device_grant,
                   GRANT_REFRESH: self._refresh_grant}.get(grant)
        if handler is None:
            raise OAuthError("unsupported_grant_type", f"unknown grant {grant!r}")
        return handler(form)

    def _code_grant(self, f: dict[str, str]) -> dict[str, Any]:
        client = self._client(f.get("client_id", ""), f.get("client_secret"), grant=GRANT_AUTH_CODE)
        rec = self.codes.get(f.get("code", ""))
        if rec is None:
            raise OAuthError("invalid_grant", "unknown code")
        if rec["used"]:  # a replayed code means interception: revoke everything minted from it
            self.revoked_families.add(rec["family"])
            raise OAuthError("invalid_grant", "authorization code already used; tokens from it were revoked")
        rec["used"] = True  # single use even if the rest of validation fails
        if self.clock() > rec["exp"] or rec["client_id"] != client.client_id or rec["redirect_uri"] != f.get("redirect_uri"):
            raise OAuthError("invalid_grant", "code expired or bound to a different client or redirect_uri")
        if rec["challenge"]:
            verifier = f.get("code_verifier", "")
            if not re.fullmatch(r"[A-Za-z0-9\-._~]{43,128}", verifier) or not hmac.compare_digest(pkce_challenge(verifier), rec["challenge"]):
                raise OAuthError("invalid_grant", "PKCE verification failed")
        return self._issue(rec["user"], rec["scope"], client.client_id, family=rec["family"], with_refresh="offline_access" in rec["scope"].split())

    def _client_credentials(self, f: dict[str, str]) -> dict[str, Any]:
        client = self._client(f.get("client_id", ""), f.get("client_secret"), grant=GRANT_CLIENT_CREDS)
        if client.secret_sha256 is None:
            raise OAuthError("unauthorized_client", "client credentials requires a confidential client")
        return self._issue(client.client_id, self._scope(client, f.get("scope", "")), client.client_id)  # no user, no refresh token

    def device_authorize(self, client_id: str, scope: str) -> dict[str, Any]:
        client = self._client(client_id, None, grant=GRANT_DEVICE)
        device_code = secrets.token_urlsafe(24)
        user_code = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
        user_code = f"{user_code[:4]}-{user_code[4:]}"
        self.devices[device_code] = {"client_id": client_id, "scope": self._scope(client, scope), "user_code": user_code, "status": "pending",
                                     "interval": 5, "last_poll": None, "exp": self.clock() + self.DEVICE_TTL, "user": None}
        self.user_codes[user_code] = device_code
        return {"device_code": device_code, "user_code": user_code, "verification_uri": "https://idp.example.com/device", "expires_in": self.DEVICE_TTL, "interval": 5}

    def device_user_decision(self, user_code: str, user: str, approve: bool) -> None:
        """The human side: opens the verification URI on a phone, signs in, types the code, approves or denies."""
        device_code = self.user_codes.get(user_code)
        if device_code is None:
            raise OAuthError("invalid_request", "unknown user code")
        self.devices[device_code].update(status="approved" if approve else "denied", user=user)

    def _device_grant(self, f: dict[str, str]) -> dict[str, Any]:
        rec = self.devices.get(f.get("device_code", ""))
        if rec is None or rec["client_id"] != f.get("client_id"):
            raise OAuthError("invalid_grant", "unknown device_code")
        now = self.clock()
        if now > rec["exp"]:
            raise OAuthError("expired_token", "device code expired; start over")
        if rec["last_poll"] is not None and now - rec["last_poll"] < rec["interval"]:
            rec["interval"] += 5  # polling too fast: server raises the interval and the client MUST comply
            rec["last_poll"] = now
            raise OAuthError("slow_down", f"interval is now {rec['interval']}s")
        rec["last_poll"] = now
        if rec["status"] == "pending":
            raise OAuthError("authorization_pending", "user has not finished yet")
        if rec["status"] == "denied":
            raise OAuthError("access_denied", "user denied the request")
        del self.devices[f["device_code"]]  # single use
        return self._issue(rec["user"], rec["scope"], rec["client_id"], with_refresh="offline_access" in rec["scope"].split())

    def _refresh_grant(self, f: dict[str, str]) -> dict[str, Any]:
        client = self._client(f.get("client_id", ""), f.get("client_secret"), grant=GRANT_REFRESH)
        token = f.get("refresh_token", "")
        rec = self.refresh_tokens.get(token)
        if rec is None or rec["client_id"] != client.client_id or self.clock() > rec["exp"]:
            raise OAuthError("invalid_grant", "refresh token unknown or expired")
        if rec["family"] in self.revoked_families:
            raise OAuthError("invalid_grant", "token family revoked")
        if rec["used"]:  # rotation + reuse detection: a stolen token was used after the legitimate client rotated it
            self.revoked_families.add(rec["family"])
            raise OAuthError("invalid_grant", "refresh token reuse detected; whole family revoked")
        rec["used"] = True
        return self._issue(rec["sub"], rec["scope"], client.client_id, family=rec["family"], with_refresh=True)


class PkceClient:
    """The client half of authorization code + PKCE: secret verifier stays local, only its hash goes through the browser."""

    def __init__(self, client_id: str, redirect_uri: str) -> None:
        self.client_id, self.redirect_uri = client_id, redirect_uri
        self._verifier, self._state = "", ""

    def begin(self, scope: str) -> dict[str, Any]:
        self._verifier, self._state = new_code_verifier(), secrets.token_urlsafe(16)
        return {"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri, "scope": scope, "state": self._state,
                "code_challenge": pkce_challenge(self._verifier), "code_challenge_method": "S256"}

    def finish(self, redirect_url: str) -> dict[str, str]:
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(redirect_url).query).items()}
        if not hmac.compare_digest(q.get("state", ""), self._state):  # CSRF defense: the response must match what we started
            raise OAuthError("invalid_state", "state mismatch: possible CSRF, discard the response")
        return {"grant_type": GRANT_AUTH_CODE, "code": q["code"], "redirect_uri": self.redirect_uri, "client_id": self.client_id, "code_verifier": self._verifier}


async def poll_device_token(server: AuthorizationServer, clock: LabClock, client_id: str, grant: dict[str, Any], user_acts_at: float,
                            user: str, trace: list[str]) -> dict[str, Any]:
    """RFC 8628 client loop: honor interval, back off on slow_down, stop on terminal errors. The user approves at a set time."""
    interval, started, approved = float(grant["interval"]), clock(), False
    greedy_probe = True
    while True:
        if not approved and clock() - started >= user_acts_at:
            server.device_user_decision(grant["user_code"], user, True)
            approved = True
        try:
            return server.token(grant_type=GRANT_DEVICE, device_code=grant["device_code"], client_id=client_id)
        except OAuthError as exc:
            trace.append(f"t+{clock() - started:.0f}s {exc.error}")
            if exc.error not in ("authorization_pending", "slow_down"):
                raise
            if exc.error == "slow_down":
                interval += 5
        await asyncio.sleep(0)  # a real client sleeps `interval` seconds here
        clock.advance(1 if greedy_probe else interval)  # first retry is deliberately too fast to demonstrate slow_down
        greedy_probe = False


# ---- Syllabus extension: SAML 2.0 assertions ---------------------------------------------------------
NS_SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
NS_SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"
SAML_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
SAML_BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
GROUPS_CLAIM_URI = "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups"


def parse_xml_safely(text: str, max_bytes: int = 200_000) -> ET.Element:
    """SAML responses are attacker-supplied XML: refuse DTDs and entities (XXE, billion laughs) before parsing."""
    raw = text.encode("utf-8")
    if len(raw) > max_bytes or b"\x00" in raw:
        raise SamlError("response too large or not UTF-8")
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise SamlError("DTD/entity declarations are forbidden in SAML messages")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SamlError(f"malformed XML: {exc}") from exc


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_lab_saml_response(now: float, *, issuer: str = "https://sts.example.com/idp", audience: str = "https://app.example.com/sp",
                            recipient: str = "https://app.example.com/saml/acs", in_response_to: str = "_req123", assertion_id: str = "_a1b2c3",
                            not_before: float | None = None, not_on_or_after: float | None = None, groups: tuple[str, ...] = ("g-finance", "g-eng"),
                            extra_assertion: bool = False) -> str:
    """A lab-only SAML Response. The Signature element is a placeholder: NOTHING here is cryptographically signed."""
    nb, na = iso(now - 30 if not_before is None else not_before), iso(now + 300 if not_on_or_after is None else not_on_or_after)
    values = "".join(f"<saml:AttributeValue>{g}</saml:AttributeValue>" for g in groups)
    assertion = f"""<saml:Assertion ID="{assertion_id}" Version="2.0" IssueInstant="{iso(now)}">
      <saml:Issuer>{issuer}</saml:Issuer>
      <ds:Signature><ds:SignedInfo><ds:Reference URI="#{assertion_id}"/></ds:SignedInfo><ds:SignatureValue>LAB-PLACEHOLDER</ds:SignatureValue></ds:Signature>
      <saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">alice@example.com</saml:NameID>
        <saml:SubjectConfirmation Method="{SAML_BEARER}"><saml:SubjectConfirmationData Recipient="{recipient}" InResponseTo="{in_response_to}" NotOnOrAfter="{na}"/></saml:SubjectConfirmation>
      </saml:Subject>
      <saml:Conditions NotBefore="{nb}" NotOnOrAfter="{na}"><saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction></saml:Conditions>
      <saml:AuthnStatement AuthnInstant="{iso(now - 5)}" SessionIndex="_sess9"/>
      <saml:AttributeStatement><saml:Attribute Name="{GROUPS_CLAIM_URI}">{values}</saml:Attribute>
        <saml:Attribute Name="department"><saml:AttributeValue>finance</saml:AttributeValue></saml:Attribute></saml:AttributeStatement>
    </saml:Assertion>"""
    return (f'<samlp:Response xmlns:samlp="{NS_SAMLP}" xmlns:saml="{NS_SAML}" xmlns:ds="{NS_DS}" ID="_resp1" InResponseTo="{in_response_to}" Version="2.0">'
            f'<saml:Issuer>{issuer}</saml:Issuer><samlp:Status><samlp:StatusCode Value="{SAML_SUCCESS}"/></samlp:Status>{assertion}{assertion if extra_assertion else ""}</samlp:Response>')


class SamlIdentity(BaseModel):
    name_id: str
    session_index: str | None
    attributes: dict[str, list[str]]
    signature_verified: bool
    warning: str


class SamlAssertionValidator:
    """Validates the CONDITIONS of an assertion. It does not (and cannot safely) verify the XML signature on its own:
    canonicalization (C14N), reference resolution and signature-wrapping defenses belong to a vetted library such as
    python3-saml or xmlsec. `signature_check` is the seam where that library plugs in."""

    def __init__(self, *, idp_entity_id: str, sp_entity_id: str, acs_url: str, clock: Callable[[], float] = time.time, skew_s: int = 60,
                 signature_check: Callable[[str], bool] | None = None) -> None:
        self.idp, self.sp, self.acs, self.clock, self.skew = idp_entity_id, sp_entity_id, acs_url, clock, skew_s
        self.signature_check = signature_check
        self._seen_ids: dict[str, float] = {}

    @staticmethod
    def _ts(value: str | None, what: str) -> float:
        if not value:
            raise SamlError(f"missing {what}")
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            raise SamlError(f"bad timestamp in {what}") from None

    def validate(self, xml: str, expected_in_response_to: str) -> SamlIdentity:
        root = parse_xml_safely(xml)
        q = lambda ns, name: f"{{{ns}}}{name}"  # noqa: E731
        status = root.find(f"{q(NS_SAMLP, 'Status')}/{q(NS_SAMLP, 'StatusCode')}")
        if status is None or status.get("Value") != SAML_SUCCESS:
            raise SamlError("response status is not Success")
        assertions = root.findall(q(NS_SAML, "Assertion"))
        if len(assertions) != 1:  # more than one assertion is the classic signature-wrapping shape
            raise SamlError(f"expected exactly one Assertion, found {len(assertions)}")
        a = assertions[0]
        if a.findtext(q(NS_SAML, "Issuer")) != self.idp:
            raise SamlError("unexpected Issuer")
        now = self.clock()
        conditions = a.find(q(NS_SAML, "Conditions"))
        if conditions is None:
            raise SamlError("missing Conditions")
        if now + self.skew < self._ts(conditions.get("NotBefore"), "NotBefore") or now - self.skew >= self._ts(conditions.get("NotOnOrAfter"), "NotOnOrAfter"):
            raise SamlError("assertion outside its validity window")
        audiences = [e.text for e in conditions.iter(q(NS_SAML, "Audience"))]
        if self.sp not in audiences:
            raise SamlError("audience restriction does not include this service provider")
        confirmations = [c for c in a.iter(q(NS_SAML, "SubjectConfirmation")) if c.get("Method") == SAML_BEARER]
        data = confirmations[0].find(q(NS_SAML, "SubjectConfirmationData")) if confirmations else None
        if data is None:
            raise SamlError("no bearer SubjectConfirmationData")
        if data.get("Recipient") != self.acs:
            raise SamlError("Recipient does not match our ACS URL")
        if data.get("InResponseTo") != expected_in_response_to:  # binds the response to a login WE started
            raise SamlError("InResponseTo does not match the pending request")
        if now - self.skew >= self._ts(data.get("NotOnOrAfter"), "SubjectConfirmationData NotOnOrAfter"):
            raise SamlError("subject confirmation expired")
        assertion_id = a.get("ID", "")
        self._seen_ids = {i: t for i, t in self._seen_ids.items() if now - t < 3600}
        if not assertion_id or assertion_id in self._seen_ids:
            raise SamlError("assertion id missing or already used (replay)")
        verified = bool(self.signature_check and self.signature_check(xml))
        if self.signature_check is not None and not verified:
            raise SamlError("signature verification failed")
        self._seen_ids[assertion_id] = now
        attrs: dict[str, list[str]] = {}
        for attr in a.iter(q(NS_SAML, "Attribute")):
            attrs.setdefault(attr.get("Name", ""), []).extend(v.text or "" for v in attr.findall(q(NS_SAML, "AttributeValue")))
        name_id = a.findtext(f"{q(NS_SAML, 'Subject')}/{q(NS_SAML, 'NameID')}") or ""
        authn = a.find(q(NS_SAML, "AuthnStatement"))  # explicit None check: a childless Element is falsy
        session = authn.get("SessionIndex") if authn is not None else None
        warning = "signature verified by vetted library" if verified else "UNTRUSTED: structure and conditions checked, signature NOT verified"
        return SamlIdentity(name_id=name_id, session_index=session, attributes=attrs, signature_verified=verified, warning=warning)


def python3_saml_validate(settings: dict[str, Any], request_data: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Production adapter shape using python3-saml. It verifies the XML signature, conditions, Recipient and InResponseTo."""
    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
    except ImportError as exc:
        raise MissingDependency("python3-saml", "pip install python3-saml (needs the xmlsec system library)") from exc
    auth = OneLogin_Saml2_Auth(request_data, old_settings=settings)
    auth.process_response(request_id=request_id)
    if auth.get_errors() or not auth.is_authenticated():
        raise SamlError(f"saml rejected: {auth.get_errors()} {auth.get_last_error_reason()}")
    return {"name_id": auth.get_nameid(), "attributes": auth.get_attributes(), "session_index": auth.get_session_index()}


# ---- Syllabus extension: Azure AD (Entra ID) group mapping -------------------------------------------
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
GRAPH_HOSTS = {"graph.microsoft.com"}


class GraphTransport(ABC):
    """Interface: GET one Graph URL and return decoded JSON. Raises GraphThrottled (429) or GraphUnavailable (5xx)."""

    @abstractmethod
    async def get(self, url: str) -> dict[str, Any]:
        """Fetch one page of a Graph collection."""


class FakeGraphTransport(GraphTransport):
    """Pages transitiveMemberOf style results with @odata.nextLink and throttles once (429 with Retry-After)."""

    def __init__(self, memberships: dict[str, list[str]], page_size: int = 100, throttle_first: int = 1) -> None:
        self.memberships, self.page_size, self.throttle, self.calls = memberships, page_size, throttle_first, 0

    async def get(self, url: str) -> dict[str, Any]:
        self.calls += 1
        await asyncio.sleep(0.001)
        if self.throttle > 0:
            self.throttle -= 1
            raise GraphThrottled("graph 429", retry_after_s=0.01)
        parsed = urllib.parse.urlparse(url)
        oid = parsed.path.split("/")[3]
        skip = int(urllib.parse.parse_qs(parsed.query).get("skip", ["0"])[0])
        groups = self.memberships.get(oid, [])
        page = groups[skip:skip + self.page_size]
        body: dict[str, Any] = {"value": [{"@odata.type": "#microsoft.graph.group", "id": g} for g in page]}
        if skip + self.page_size < len(groups):
            body["@odata.nextLink"] = f"https://graph.microsoft.com/v1.0/users/{oid}/transitiveMemberOf?skip={skip + self.page_size}"
        return body


class UrllibGraphTransport(GraphTransport):
    """Production shape: bearer token from the environment (acquired with the MSAL library or a managed identity)."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._token = (os.environ if env is None else env).get("GRAPH_ACCESS_TOKEN", "")
        if not self._token:
            raise MissingCredential("set GRAPH_ACCESS_TOKEN (app-only token with GroupMember.Read.All)")

    async def get(self, url: str) -> dict[str, Any]:
        def call() -> dict[str, Any]:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 (host allowlisted by the caller)
                    return json.loads(resp.read(2_000_000))
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    raise GraphThrottled("graph 429", retry_after_s=float(exc.headers.get("Retry-After", "1"))) from exc
                if exc.code >= 500:
                    raise GraphUnavailable(f"graph {exc.code}") from exc
                raise AuthorizationError(f"graph refused the lookup ({exc.code})") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise GraphUnavailable(str(exc)) from exc

        return await asyncio.to_thread(call)


class GraphGroupResolver:
    def __init__(self, transport: GraphTransport, max_pages: int = 20) -> None:
        self.transport, self.max_pages, self.lookups = transport, max_pages, 0

    async def member_groups(self, user_oid: str) -> list[str]:
        if not GUID_RE.match(user_oid):
            raise AuthenticationError("oid claim is not a GUID")
        self.lookups += 1
        url: str | None = f"https://graph.microsoft.com/v1.0/users/{user_oid}/transitiveMemberOf"
        out: list[str] = []
        for _ in range(self.max_pages):  # hard page cap: a hostile or buggy tenant cannot make us loop forever
            if url is None:
                break
            if urllib.parse.urlparse(url).hostname not in GRAPH_HOSTS:  # nextLink is data: never follow it off-host
                raise AuthenticationError("unexpected Graph nextLink host")
            page = await retry_io(lambda u=url: self.transport.get(u), retry_on=(GraphUnavailable,), label="graph.memberOf")
            out.extend(g["id"].lower() for g in page["value"] if g.get("id"))
            url = page.get("@odata.nextLink")
        return out


class GroupRoleMap(BaseModel):
    """Configurable table: Entra group OBJECT ID -> application roles. Object ids, never display names (names can be reused)."""

    mapping: dict[str, frozenset[str]]

    @classmethod
    def from_json(cls, text: str) -> "GroupRoleMap":
        raw = json.loads(text)
        for gid in raw:
            if not GUID_RE.match(gid):
                raise ValueError(f"{gid!r} is not a group object id (GUID)")
        return cls(mapping={gid.lower(): frozenset(roles) for gid, roles in raw.items()})

    def roles_for(self, group_ids: list[str]) -> frozenset[str]:
        """Deny by default: a group that is not in the table grants nothing, and no groups means no roles."""
        return frozenset().union(*(self.mapping.get(g.lower(), frozenset()) for g in group_ids)) if group_ids else frozenset()


async def resolve_entra_groups(payload: dict[str, Any], resolver: GraphGroupResolver) -> tuple[list[str], str]:
    """Read the groups claim; when Entra signals overage (_claim_names.groups) the list is omitted and must be fetched."""
    if isinstance(payload.get("groups"), list):
        return [str(g).lower() for g in payload["groups"]], "token"
    if "groups" in (payload.get("_claim_names") or {}):  # overage: token had too many groups to embed
        return await resolver.member_groups(str(payload.get("oid", ""))), "graph"
    return [], "none"


# ---- Syllabus extension: RBAC with role hierarchy, plus ABAC conditions ------------------------------
class Subject(BaseModel, frozen=True):
    user: str
    tenant_id: str
    roles: frozenset[str]
    department: str | None = None
    clearance: int = 0  # 0 public .. 3 restricted


class Resource(BaseModel, frozen=True):
    resource_id: str
    tenant_id: str
    owner: str | None = None
    department: str | None = None
    sensitivity: int = 0


class Decision(BaseModel):
    allowed: bool
    reason: str


class RoleHierarchy:
    """Roles grant permissions and may inherit from other roles (admin > editor > viewer). Cycles are rejected at load."""

    def __init__(self, grants: dict[str, set[str]], inherits: dict[str, set[str]]) -> None:
        self.grants, self.inherits = grants, inherits
        for role in grants:
            self._walk(role, ())  # fail fast on a cycle instead of looping at request time

    def _walk(self, role: str, path: tuple[str, ...]) -> set[str]:
        if role in path:
            raise ValueError(f"role inheritance cycle: {' -> '.join((*path, role))}")
        perms = set(self.grants.get(role, set()))
        for parent in self.inherits.get(role, set()):
            perms |= self._walk(parent, (*path, role))
        return perms

    def permissions(self, roles: frozenset[str]) -> frozenset[str]:
        return frozenset().union(*(self._walk(r, ()) for r in roles)) if roles else frozenset()


AbacCondition = Callable[[Subject, Resource, dict[str, Any]], bool]


class PolicyEngine:
    """RBAC answers 'may this role do this kind of thing?'; ABAC conditions answer 'on THIS resource, right now?'.
    Evaluation: default deny; every ABAC rule registered for the permission must pass."""

    def __init__(self, hierarchy: RoleHierarchy) -> None:
        self.hierarchy = hierarchy
        self.rules: dict[str, list[tuple[str, AbacCondition]]] = {}

    def add_rule(self, permission: str, name: str, condition: AbacCondition) -> None:
        self.rules.setdefault(permission, []).append((name, condition))

    def decide(self, subject: Subject, permission: str, resource: Resource, env: dict[str, Any] | None = None) -> Decision:
        env = env or {}
        if permission not in self.hierarchy.permissions(subject.roles):
            return Decision(allowed=False, reason=f"RBAC: no role grants {permission}")
        for name, condition in self.rules.get(permission, []):
            if not condition(subject, resource, env):
                return Decision(allowed=False, reason=f"ABAC: rule '{name}' failed")
        return Decision(allowed=True, reason="RBAC grant and all ABAC rules passed")


def build_lab_policy() -> PolicyEngine:
    hierarchy = RoleHierarchy(
        grants={"viewer": {"docs:search", "docs:read"}, "editor": {"docs:write"}, "admin": {"tenant:admin", "docs:delete"}},
        inherits={"editor": {"viewer"}, "admin": {"editor"}})
    engine = PolicyEngine(hierarchy)
    for perm in ("docs:read", "docs:write", "docs:delete"):
        engine.add_rule(perm, "same tenant", lambda s, r, e: s.tenant_id == r.tenant_id)
    engine.add_rule("docs:read", "clearance covers sensitivity", lambda s, r, e: s.clearance >= r.sensitivity)
    engine.add_rule("docs:write", "owner or same department", lambda s, r, e: r.owner == s.user or (r.department is not None and r.department == s.department))
    engine.add_rule("docs:delete", "business hours only", lambda s, r, e: 8 <= int(e.get("hour_utc", -1)) < 18)
    return engine


# ---- Syllabus extension: data-level permissions in the retrieval layer -------------------------------
class Chunk(BaseModel):
    chunk_id: str
    tenant_id: str
    acl_groups: frozenset[str] = frozenset()
    acl_users: frozenset[str] = frozenset()
    sensitivity: int = 0
    text: str
    fields: dict[str, Any] = Field(default_factory=dict)


class AccessContext(BaseModel, frozen=True):
    tenant_id: str
    user: str
    groups: frozenset[str]
    clearance: int
    permissions: frozenset[str]


MASKED_FIELDS = {"salary": "pii:read", "ssn": "pii:read"}  # field name -> permission needed to see it unmasked
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


class RetrievedChunk(BaseModel):
    chunk_id: str
    text: str
    fields: dict[str, Any]
    masked: list[str]


def row_allowed(ctx: AccessContext, c: Chunk) -> bool:
    """The ORACLE: a plain-Python statement of the policy, written independently of the filter compiler and used by tests.
    Deny by default: a chunk with an empty ACL is visible to nobody."""
    return (c.tenant_id == ctx.tenant_id and c.sensitivity <= ctx.clearance
            and bool((c.acl_groups & ctx.groups) or ctx.user in c.acl_users))


def compile_filter(ctx: AccessContext) -> dict[str, Any]:
    """Compile the caller's context into a backend filter (shape similar to vector DB payload filters / SQL WHERE)."""
    return {"must": [{"key": "tenant_id", "eq": ctx.tenant_id}, {"key": "sensitivity", "lte": ctx.clearance},
                     {"any_of": [{"key": "acl_groups", "intersects": sorted(ctx.groups)}, {"key": "acl_users", "contains": ctx.user}]}]}


def eval_filter(flt: dict[str, Any], c: Chunk) -> bool:
    def one(cond: dict[str, Any]) -> bool:
        if "any_of" in cond:
            return any(one(x) for x in cond["any_of"])
        value = getattr(c, cond["key"])
        if "eq" in cond:
            return bool(value == cond["eq"])
        if "lte" in cond:
            return bool(value <= cond["lte"])
        if "intersects" in cond:
            return bool(set(value) & set(cond["intersects"]))
        return bool(cond["contains"] in value)
    return all(one(cond) for cond in flt["must"])


def apply_masks(ctx: AccessContext, c: Chunk) -> RetrievedChunk:
    """Field-level control: the row is visible, but sensitive fields and inline SSNs are redacted without 'pii:read'."""
    masked, fields, text = [], dict(c.fields), c.text
    for name, needed in MASKED_FIELDS.items():
        if name in fields and needed not in ctx.permissions:
            fields[name], _ = "[REDACTED]", masked.append(name)
    if "pii:read" not in ctx.permissions and SSN_RE.search(text):
        text, _ = SSN_RE.sub("[REDACTED-SSN]", text), masked.append("text:ssn")
    return RetrievedChunk(chunk_id=c.chunk_id, text=text, fields=fields, masked=masked)


class AclVectorStore:
    def __init__(self, *, enforce_canary: bool = True) -> None:
        self._rows: list[tuple[Chunk, list[float]]] = []
        self.audit: list[dict[str, Any]] = []
        self.enforce_canary = enforce_canary

    def upsert(self, chunk: Chunk) -> None:
        self._rows.append((chunk, embed(chunk.text)))

    def _filter_for(self, ctx: AccessContext) -> dict[str, Any]:
        return compile_filter(ctx)

    def _score(self, query: str, pool: list[tuple[Chunk, list[float]]]) -> list[tuple[float, Chunk]]:
        q = embed(query)
        return sorted(((sum(a * b for a, b in zip(q, v)), c) for c, v in pool), key=lambda x: -x[0])

    async def search(self, ctx: AccessContext, query: str, k: int = 3) -> list[RetrievedChunk]:
        """Filter pushdown: eligibility is decided BEFORE ranking, so top-k is always k authorized chunks (when they exist)."""
        flt = self._filter_for(ctx)
        eligible = [(c, v) for c, v in self._rows if eval_filter(flt, c)]
        hits = [c for _, c in self._score(query, eligible)[:k]]
        if self.enforce_canary and not all(row_allowed(ctx, c) for c in hits):  # runtime assertion: filter bug becomes an alarm, not a leak
            self.audit.append({"user": ctx.user, "event": "canary_violation"})
            raise IsolationViolation("retrieval returned a chunk the caller may not read")
        out = [apply_masks(ctx, c) for c in hits]
        self.audit.append({"user": ctx.user, "tenant": ctx.tenant_id, "filter": flt, "scanned": len(self._rows), "eligible": len(eligible),
                           "returned": [c.chunk_id for c in hits], "masked": {r.chunk_id: r.masked for r in out if r.masked}})
        return out

    async def naive_postfilter(self, ctx: AccessContext, query: str, k: int = 3) -> list[str]:
        """The anti-pattern: rank everything, take top-k, THEN drop what the caller may not see. Shown for contrast only."""
        top = [c for _, c in self._score(query, self._rows)[:k]]
        return [c.chunk_id for c in top if row_allowed(ctx, c)]


class LeakyAclStore(AclVectorStore):
    """Deliberately broken: forgets the ACL clause. Used to prove the isolation test matrix can actually fail."""

    def _filter_for(self, ctx: AccessContext) -> dict[str, Any]:
        return {"must": [{"key": "tenant_id", "eq": ctx.tenant_id}]}


async def isolation_matrix(store: AclVectorStore, contexts: list[AccessContext], chunks: list[Chunk], canaries: list[str]) -> dict[str, Any]:
    """Property test: for every user and for queries built from every chunk's own text (the strongest possible lure),
    every returned chunk must satisfy the oracle, and no canary marker may reach an unauthorized user."""
    checks = violations = canary_leaks = 0
    for ctx in contexts:
        for lure in chunks:
            try:
                results = await store.search(ctx, lure.text, k=5)
            except IsolationViolation:
                violations += 1
                continue
            by_id = {c.chunk_id: c for c in chunks}
            for r in results:
                checks += 1
                violations += 0 if row_allowed(ctx, by_id[r.chunk_id]) else 1
                canary_leaks += int(any(m in r.text for m in canaries) and not row_allowed(ctx, by_id[r.chunk_id]))
    return {"users": len(contexts), "queries_per_user": len(chunks), "chunks_checked": checks, "violations": violations, "canary_leaks": canary_leaks}


async def extension_demo() -> None:
    """Runs every syllabus-extension section offline, in the order of the Module 12 syllabus."""
    clock = LabClock()
    now = int(clock())
    key = b"lab-only-oauth-signing-key"
    jwks = KeySet({"k1": b"k1-secret-for-lab-only"})
    verifier = JwtVerifier(jwks, issuer="https://idp.example.com", audience="ai-gateway", clock=clock)

    # 1. Authentication vs authorization
    def tok(**over: Any) -> str:
        base = {"iss": "https://idp.example.com", "aud": "ai-gateway", "sub": "alice", "tid": "acme", "exp": now + 600, "roles": ["viewer"], "groups": ["all"]}
        return sign_jwt({**base, **over}, b"k1-secret-for-lab-only", "k1")
    cases = {"no credentials": (None, "docs:write", None), "garbage token": ("Bearer not.a.jwt", "docs:search", None),
             "valid, wrong permission": (f"Bearer {tok()}", "docs:write", None), "valid, allowed": (f"Bearer {tok()}", "docs:search", None),
             "valid, other tenant's object": (f"Bearer {tok()}", "docs:search", "globex")}
    for name, (header, perm, tenant) in cases.items():
        out = await guarded_endpoint(header, verifier, perm, tenant)
        log.info("authn vs authz", extra={"ctx": {"case": name, "status": out.status, "headers": out.headers, "body": out.body}})

    # 2. JWT anatomy
    good = tok()
    anatomy = decode_unverified(good, clock())
    log.info("jwt anatomy", extra={"ctx": {"header": anatomy.header, "claims": sorted(anatomy.payload), "signature_bytes": anatomy.signature_bytes, "findings": anatomy.findings}})
    h_b64, p_b64, s_b64 = good.split(".")
    forged_payload = {**json.loads(b64u_dec(p_b64)), "tid": "globex", "roles": ["admin"]}
    forged = f"{h_b64}.{b64u(json.dumps(forged_payload).encode())}.{s_b64}"
    shown = decode_unverified(forged, clock())
    try:
        await verifier.verify(forged)
        outcome = "ACCEPTED (bug)"
    except AuthenticationError as exc:
        outcome = f"rejected: {exc}"
    log.info("decode is not verify", extra={"ctx": {"decoded_claims_look_fine": {"tid": shown.payload["tid"], "roles": shown.payload["roles"]}, "verified": shown.verified, "verify_result": outcome}})

    # 3. OAuth grants
    redirect = "https://app.example.com/callback"
    secret = "lab-only-client-secret"
    clients = [OAuthClient(client_id="spa", redirect_uris=frozenset({redirect}), scopes=frozenset({"openid", "docs:read", "offline_access"}), grants=frozenset({GRANT_AUTH_CODE, GRANT_REFRESH})),
               OAuthClient(client_id="batch", secret_sha256=sha256_hex(secret), scopes=frozenset({"docs:read", "docs:write"}), grants=frozenset({GRANT_CLIENT_CREDS})),
               OAuthClient(client_id="tv", scopes=frozenset({"docs:read"}), grants=frozenset({GRANT_DEVICE}))]
    oauth_key = b"lab-only-oauth-signing-key"
    server = AuthorizationServer(clients, clock, oauth_key, kid="oauth1", issuer="https://idp.example.com", audience="ai-gateway", tenant="acme")
    oauth_verifier = JwtVerifier(KeySet({"oauth1": oauth_key}), issuer="https://idp.example.com", audience="ai-gateway", clock=clock)

    pkce = PkceClient("spa", redirect)
    params = pkce.begin("openid docs:read offline_access")
    callback = server.authorize(user="alice", **params)
    form = pkce.finish(callback)
    tokens = server.token(**form)
    claims = await oauth_verifier.verify(tokens["access_token"])
    log.info("auth code + PKCE", extra={"ctx": {"verifier_len": len(form["code_verifier"]), "challenge_method": params["code_challenge_method"], "sub": claims.sub, "scope": tokens["scope"], "has_refresh": "refresh_token" in tokens}})

    def attempt(label: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
            log.error("ATTACK SUCCEEDED", extra={"ctx": {"attack": label}})
        except OAuthError as exc:
            log.info("oauth blocked", extra={"ctx": {"attack": label, "error": exc.error, "description": exc.description}})

    p2 = PkceClient("spa", redirect)
    params2 = p2.begin("docs:read")
    attempt("unregistered redirect_uri", lambda: server.authorize(user="alice", **{**params2, "redirect_uri": "https://evil.example/cb"}))
    attempt("PKCE plain method", lambda: server.authorize(user="alice", **{**params2, "code_challenge_method": "plain"}))
    attempt("no PKCE for public client", lambda: server.authorize(user="alice", **{**params2, "code_challenge": None, "code_challenge_method": None}))
    attempt("implicit (response_type=token)", lambda: server.authorize(user="alice", **{**params2, "response_type": "token"}))
    attempt("state mismatch (CSRF)", lambda: PkceClient("spa", redirect).finish(callback.replace("state=", "state=x")))
    callback2 = server.authorize(user="alice", **params2)
    stolen = {**p2.finish(callback2), "code_verifier": new_code_verifier()}  # attacker has the code but not the verifier
    attempt("intercepted code without verifier", lambda: server.token(**stolen))
    p3 = PkceClient("spa", redirect)
    cb3 = server.authorize(user="alice", **p3.begin("docs:read offline_access"))
    f3 = p3.finish(cb3)
    first_ok = server.token(**f3)
    attempt("authorization code replay", lambda: server.token(**f3))
    attempt("refresh token after code replay revoked its family", lambda: server.token(grant_type=GRANT_REFRESH, client_id="spa", refresh_token=first_ok["refresh_token"]))

    cc = server.token(grant_type=GRANT_CLIENT_CREDS, client_id="batch", client_secret=secret, scope="docs:read")
    cc_claims = await oauth_verifier.verify(cc["access_token"])
    log.info("client credentials", extra={"ctx": {"sub": cc_claims.sub, "scope": cc["scope"], "refresh_token_issued": "refresh_token" in cc}})
    attempt("client credentials wrong secret", lambda: server.token(grant_type=GRANT_CLIENT_CREDS, client_id="batch", client_secret="nope", scope="docs:read"))
    attempt("client credentials scope escalation", lambda: server.token(grant_type=GRANT_CLIENT_CREDS, client_id="batch", client_secret=secret, scope="docs:read admin"))
    attempt("password grant (deprecated)", lambda: server.token(grant_type="password", username="alice", password="x"))

    device = server.device_authorize("tv", "docs:read")
    trace: list[str] = []
    dev_tokens = await poll_device_token(server, clock, "tv", device, user_acts_at=12, user="alice", trace=trace)
    log.info("device grant", extra={"ctx": {"user_code": device["user_code"], "poll_trace": trace, "token_scope": dev_tokens["scope"]}})

    refreshed = server.token(grant_type=GRANT_REFRESH, client_id="spa", refresh_token=tokens["refresh_token"])
    log.info("refresh rotation", extra={"ctx": {"new_refresh_differs": refreshed["refresh_token"] != tokens["refresh_token"]}})
    attempt("old refresh token reuse (theft signal)", lambda: server.token(grant_type=GRANT_REFRESH, client_id="spa", refresh_token=tokens["refresh_token"]))
    attempt("newest refresh token after family revoked", lambda: server.token(grant_type=GRANT_REFRESH, client_id="spa", refresh_token=refreshed["refresh_token"]))

    # 4. SAML assertion handling
    acs, sp, idp = "https://app.example.com/saml/acs", "https://app.example.com/sp", "https://sts.example.com/idp"
    saml_clock = LabClock()
    t0 = saml_clock()

    def validator() -> SamlAssertionValidator:
        return SamlAssertionValidator(idp_entity_id=idp, sp_entity_id=sp, acs_url=acs, clock=saml_clock)
    v = validator()
    ident = v.validate(build_lab_saml_response(t0), "_req123")
    log.info("saml valid conditions", extra={"ctx": ident.model_dump()})
    bad = {"expired": build_lab_saml_response(t0, not_before=t0 - 900, not_on_or_after=t0 - 600), "wrong audience": build_lab_saml_response(t0, audience="https://other.example/sp"),
           "wrong recipient": build_lab_saml_response(t0, recipient="https://evil.example/acs"), "wrong InResponseTo": build_lab_saml_response(t0, in_response_to="_other"),
           "wrong issuer": build_lab_saml_response(t0, issuer="https://rogue.example/idp"), "two assertions (wrapping shape)": build_lab_saml_response(t0, assertion_id="_zz", extra_assertion=True),
           "replayed assertion id": build_lab_saml_response(t0),
           "DOCTYPE/XXE": '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>'}
    for name, xml in bad.items():
        try:
            v.validate(xml, "_req123")
            log.error("SAML ATTACK SUCCEEDED", extra={"ctx": {"case": name}})
        except SamlError as exc:
            log.info("saml blocked", extra={"ctx": {"case": name, "reason": str(exc)}})
    strict = SamlAssertionValidator(idp_entity_id=idp, sp_entity_id=sp, acs_url=acs, clock=saml_clock, signature_check=lambda xml: False)
    try:
        strict.validate(build_lab_saml_response(t0, assertion_id="_fresh"), "_req123")
    except SamlError as exc:
        log.info("saml with a signature seam that fails", extra={"ctx": {"reason": str(exc)}})
    try:
        python3_saml_validate({}, {}, "_req123")
    except MissingDependency as exc:
        log.info("python3-saml adapter (optional)", extra={"ctx": {"note": str(exc)}})
    except Exception as exc:  # installed but given empty settings: not part of the offline demo
        log.info("python3-saml adapter (optional)", extra={"ctx": {"note": type(exc).__name__}})

    # 5. Entra ID groups and overage
    g = lambda n: f"00000000-0000-0000-0000-{n:012d}"  # noqa: E731
    role_map = GroupRoleMap.from_json(json.dumps({g(1): ["viewer"], g(2): ["editor"], g(900): ["admin"]}))
    oid_small, oid_big, oid_none = g(5001), g(5002), g(5003)
    memberships = {oid_big: [g(100 + i) for i in range(250)] + [g(900)]}  # 251 groups: past the 200-group token limit, admin is on page 3
    resolver = GraphGroupResolver(FakeGraphTransport(memberships, page_size=100))
    samples = {"small user": {"oid": oid_small, "groups": [g(1), g(2).upper(), g(777)]},
               "overage user": {"oid": oid_big, "_claim_names": {"groups": "src1"}, "_claim_sources": {"src1": {"endpoint": "graph lookup required"}}},
               "no groups claim": {"oid": oid_none}, "malformed oid": {"oid": "not-a-guid", "_claim_names": {"groups": "src1"}}}
    for name, payload in samples.items():
        try:
            groups, source = await resolve_entra_groups(payload, resolver)
            roles = role_map.roles_for(groups)
            log.info("entra groups", extra={"ctx": {"case": name, "source": source, "group_count": len(groups), "roles": sorted(roles), "graph_lookups": resolver.lookups}})
        except AuthenticationError as exc:
            log.info("entra groups", extra={"ctx": {"case": name, "error": str(exc)}})
    try:
        GroupRoleMap.from_json(json.dumps({"Finance Team": ["viewer"]}))
    except ValueError as exc:
        log.info("mapping by display name rejected", extra={"ctx": {"error": str(exc)}})

    # 6. RBAC hierarchy + ABAC
    policy = build_lab_policy()
    owner_doc = Resource(resource_id="d1", tenant_id="acme", owner="alice", department="finance", sensitivity=1)
    secret_doc = Resource(resource_id="d2", tenant_id="acme", owner="zed", department="legal", sensitivity=3)
    other_doc = Resource(resource_id="d3", tenant_id="globex", owner="gina", department="finance", sensitivity=0)
    alice = Subject(user="alice", tenant_id="acme", roles=frozenset({"editor"}), department="finance", clearance=1)
    root = Subject(user="root", tenant_id="acme", roles=frozenset({"admin"}), department="it", clearance=3)
    rows = [("alice read own", alice, "docs:read", owner_doc, {}), ("alice read restricted", alice, "docs:read", secret_doc, {}), ("alice read other tenant", alice, "docs:read", other_doc, {}),
            ("alice write own", alice, "docs:write", owner_doc, {}), ("alice write legal doc", alice, "docs:write", secret_doc, {}), ("alice delete (editor lacks it)", alice, "docs:delete", owner_doc, {"hour_utc": 10}),
            ("root delete at 10:00", root, "docs:delete", owner_doc, {"hour_utc": 10}), ("root delete at 23:00", root, "docs:delete", owner_doc, {"hour_utc": 23}),
            ("root inherits search via hierarchy", root, "docs:search", owner_doc, {})]
    for label, subj, perm, res, env in rows:
        d = policy.decide(subj, perm, res, env)
        log.info("policy decision", extra={"ctx": {"case": label, "allowed": d.allowed, "reason": d.reason}})
    try:
        RoleHierarchy({"a": {"x"}, "b": {"y"}}, {"a": {"b"}, "b": {"a"}})
    except ValueError as exc:
        log.info("cycle rejected at load", extra={"ctx": {"error": str(exc)}})

    # 7. Data-level permissions in retrieval
    chunks = [Chunk(chunk_id="a-pub", tenant_id="acme", acl_groups=frozenset({"all"}), text="acme handbook vacation policy and holidays"),
              Chunk(chunk_id="a-fin", tenant_id="acme", acl_groups=frozenset({"finance"}), sensitivity=2, text="acme revenue forecast board deck confidential",
                    fields={"owner": "cfo", "salary": 310000}),
              Chunk(chunk_id="a-hr", tenant_id="acme", acl_groups=frozenset({"hr"}), sensitivity=2, text="acme compensation review ssn 123-45-6789 salary bands",
                    fields={"ssn": "123-45-6789", "salary": 120000}),
              Chunk(chunk_id="a-pub2", tenant_id="acme", acl_groups=frozenset({"all"}), text="acme security awareness training schedule"),
              Chunk(chunk_id="a-priv", tenant_id="acme", acl_users=frozenset({"alice"}), sensitivity=1, text="acme private note for alice about revenue"),
              Chunk(chunk_id="a-open", tenant_id="acme", text="acme orphan chunk with empty acl revenue forecast"),
              Chunk(chunk_id="g-fin", tenant_id="globex", acl_groups=frozenset({"finance", "all"}), text="globex revenue forecast CANARY-ZEBRA-7731 merger plans")]
    store, leaky = AclVectorStore(), LeakyAclStore(enforce_canary=False)
    for c in chunks:
        store.upsert(c)
        leaky.upsert(c)
    perms_pii = frozenset({"docs:read", "pii:read"})
    ctxs = [AccessContext(tenant_id="acme", user="alice", groups=frozenset({"all", "finance"}), clearance=2, permissions=frozenset({"docs:read"})),
            AccessContext(tenant_id="acme", user="bob", groups=frozenset({"all"}), clearance=0, permissions=frozenset({"docs:read"})),
            AccessContext(tenant_id="acme", user="hrlead", groups=frozenset({"all", "hr"}), clearance=2, permissions=perms_pii),
            AccessContext(tenant_id="globex", user="carol", groups=frozenset({"finance"}), clearance=0, permissions=frozenset({"docs:read"}))]
    for ctx in ctxs:
        res = await store.search(ctx, "revenue forecast compensation policy", k=5)
        log.info("retrieval as user", extra={"ctx": {"user": ctx.user, "tenant": ctx.tenant_id, "returned": [r.chunk_id for r in res], "masked": {r.chunk_id: r.masked for r in res if r.masked}}})
    hr_view = (await store.search(ctxs[2], "compensation review ssn", k=1))[0]
    alice_hr_blocked = [r.chunk_id for r in await store.search(ctxs[0], "compensation review ssn", k=5)]
    log.info("field and text masking", extra={"ctx": {"hrlead_sees_ssn_field": hr_view.fields.get("ssn"), "alice_can_retrieve_hr_chunk": "a-hr" in alice_hr_blocked}})
    post, pre = await store.naive_postfilter(ctxs[1], "revenue forecast confidential", k=2), [r.chunk_id for r in await store.search(ctxs[1], "revenue forecast confidential", k=2)]
    log.info("prefilter vs postfilter (bob, k=2)", extra={"ctx": {"postfilter_returned": post, "prefilter_returned": pre}})
    matrix = await isolation_matrix(store, ctxs, chunks, canaries=["CANARY-ZEBRA-7731"])
    broken = await isolation_matrix(leaky, ctxs, chunks, canaries=["CANARY-ZEBRA-7731"])
    log.info("isolation matrix", extra={"ctx": {"correct_store": matrix, "deliberately_leaky_store": broken, "tests_have_teeth": broken["violations"] > 0}})
    log.info("filter audit tail", extra={"ctx": {"events": store.audit[3:4]}})


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
    await extension_demo()


if __name__ == "__main__":
    asyncio.run(main())
