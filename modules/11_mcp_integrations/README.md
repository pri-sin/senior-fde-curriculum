# Intuition & Mental Model

Models are only useful to a business when they can **look things up and do things** in the systems where the business actually runs: a 15-year-old Oracle database, a SOAP service from a 2009 ERP, Jira, a mainframe export. The **Model Context Protocol (MCP)** is a standard "USB port" for this: a server advertises *tools* (name, description, JSON Schema for inputs) and *resources*; any MCP-capable client or agent can discover and call them over JSON-RPC 2.0 (stdio for local, streamable HTTP for remote).

The FDE's job is the adapter layer behind each tool:
- **Translate** between the model's clean JSON arguments and the legacy system's quirks (XML envelopes, fixed-width fields, session cookies, odd date formats).
- **Constrain**: read-only database roles, parameterized SQL, allowlisted operations, row limits. The model never writes SQL strings that get concatenated.
- **Protect legacy systems from the AI**: rate limits, timeouts, circuit breakers, caching. A SOAP service built for 20 users will fall over when an agent loops.
- **Make errors legible**: return structured errors the model can act on (`retryable: true`, `retry_after_s`) instead of stack traces.

Tool design rule: few, well-named, narrowly scoped tools beat one `run_anything` tool. Treat tool outputs as untrusted input when you feed them back to a model (a Jira ticket can contain an injection).

# Production Architecture & Data Flow

```
 Agent / LLM client ──(MCP over streamable HTTP, OAuth2 bearer, Module 12)──▶ ┌──────────── MCP server (async) ────────────┐
        │ initialize ─────────────────────────────────────────────────────▶ │ capabilities {tools}                       │
        │ tools/list ─────────────────────────────────────────────────────▶ │ registry: name → (schema, handler, policy) │
        │ tools/call {name, arguments} ───────────────────────────────────▶ │ 1 validate args (JSON Schema/Pydantic)     │
        │                                                                   │ 2 authz: caller scopes ⊇ tool scopes       │
        │                                                                   │ 3 rate limit + circuit breaker per backend │
        │                                                                   │ 4 call adapter (timeout, retry w/ jitter)  │
        │ ◀── result {content[], isError, structured}  ◀────────────────────│ 5 redact + truncate + audit log            │
                                                                            └───────┬─────────────┬──────────────┬───────┘
                                                                                    ▼             ▼              ▼
                                                                          SQL adapter      SOAP adapter     REST adapter (Jira)
                                                                          read-only role   XML envelope     429 → Retry-After
                                                                          param queries    WS-Security      pagination, JQL allowlist
                                                                          row cap          fault→error      idempotency keys on writes
                                                                                    │             │              │
                                                                          ──────── private network / VPN boundary ────────
                                                                          Postgres/Oracle    ERP (SAP/Oracle EBS)   Jira Cloud
 Failure handling: breaker states CLOSED → OPEN (after N failures) → HALF_OPEN (probe) → CLOSED; open breaker returns
 {isError:true, retryable:true, retry_after_s} immediately, shielding the backend.
```

# Architectural Trade-Off Matrix

| Approach | Latency | Scalability | Implementation Complexity | Cost |
|---|---|---|---|---|
| **MCP server fronting adapters** | +1 hop (~5-20 ms) | High: stateless servers, one integration serves many agents/clients | Medium: schemas, authz, adapters | Low-Medium; reusable across agents |
| **Hard-coded per-agent function tools** | Lowest | Poor: each agent re-implements each integration | Low per agent, High across the org | Medium: duplicated maintenance |
| **iPaaS / ESB (MuleSoft, Boomi) + generic API tool** | Higher (flow engine) | High; strong governance | High: licensing, separate team and tooling | High: license + services |

# Enterprise War Stories & Edge Cases

**1. The agent that DDoS'd the ERP.** An agent paging through purchase orders issued 4,000 SOAP calls in two minutes; the ERP's single app server stalled month-end close. *Remediation:* token-bucket limits per backend sized to the legacy system's capacity (not the model's appetite), response caching, a circuit breaker, bulk-query tools (`list_orders(since, limit)`), and an alert on per-backend call rate.

**2. SQL injection through the "natural language to SQL" tool.** A tool accepted a raw `where` string; a crafted ticket made the model emit `1=1; DROP TABLE...`. The DB role could write. *Remediation:* read-only database role (the real control), parameterized queries with an allowlist of tables/columns, enumerated filters instead of free-form SQL, `statement_timeout` and `LIMIT`, and tests that feed injection strings.

**3. Timezone and encoding drift through SOAP.** Dates came back as local time without offsets and names in Latin-1; invoices were assigned to the wrong fiscal day and customer names were corrupted. *Remediation:* normalize at the adapter boundary (explicit timezone, UTF-8 decoding, schema-validated output), contract tests against recorded legacy responses, and surfacing the original raw value in an audit field.
