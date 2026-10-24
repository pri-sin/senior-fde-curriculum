# Senior FDE Curriculum: Production AI Systems for Enterprise

A 16-module, production-oriented curriculum for engineers who ship LLM systems into enterprise environments: regulated, multi-tenant, latency-bound and audited.

Each module in `modules/<NN_name>/` contains exactly three files:

| File | Purpose |
|---|---|
| `README.md` | Mental model, production architecture (ASCII), trade-off matrix, three war stories |
| `lab.py` | Runnable Python 3.11+ reference implementation (Pydantic v2, asyncio, JSON logs, backoff retries, typed errors) |
| `interview_prep.md` | A system-design "crucible" with sizing math, plus a word-for-word client simulation |

## Read it as a web page

`docs/index.html` is a single-file reader for all 16 modules (concepts, system design prep and lab code, with a module index and light/dark themes). Open it in a browser, or turn on GitHub Pages for the `main` branch with the `/docs` folder to host it. It loads the `marked` library from a CDN, so it needs an internet connection. It is generated from the module files, so regenerate it after editing any of them.

## Learning path

1. **Foundations (01–04):** async/Linux internals, streaming APIs, cloud networking, containers and CI/CD.
2. **LLM and retrieval (05–08):** structured outputs, hybrid RAG, GraphRAG, multimodal documents.
3. **Agents and integration (09–11):** state machines, multi-agent governance, MCP and legacy systems.
4. **Trust and operations (12–14):** tenant security, guardrails, observability and evals.
5. **Capstones (15–16):** OmniGuard (multi-tenant AI gateway) and AuditMesh (compliance verification engine).

## Running the labs

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 modules/01_python_linux/lab.py
```

Labs are self-contained and run offline: external systems (LLMs, Neo4j, ERP, vector DBs) are replaced by deterministic in-process adapters behind the same interfaces you would use in production, so swapping in the real client is a one-class change. Where a lab is described as an adapter, the adapter's protocol is the learning target.

Known limits of the offline stand-ins: the hashed "embeddings" in Modules 06, 08 and 12 reproduce the interfaces and scoring math (cosine, MaxSim) but not real semantic similarity, so paraphrase queries will not match as they would with a trained model; the lexical RAGAS-style metrics in Module 14 are deterministic proxies for LLM-judged ones; Module 12 verifies HS256 tokens where production would use RS256/ES256 with JWKS and a vetted JWT library.

Progress of the build is tracked in `progress.json`.
