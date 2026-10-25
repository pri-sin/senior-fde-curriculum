# Syllabus Coverage

Maps every topic in the AI Forward Deployed Engineer Bootcamp curriculum on krishnaik.in (16 modules, 198 topics, read on 2026-10-24) to the place in this repository where it is taught. Each module's own `README.md` carries the same table under *Syllabus Coverage Map*, plus concept notes.

## How to read this

- **Where it is taught** names a file and a section or function. Most topics have runnable code in that module's `lab.py` (see `extension_demo()`); tool and console topics (IDE setup, cloud consoles, Slack/Teams admin, consulting templates) are explained in the README concept notes and, where possible, backed by code that generates or validates the artifacts.

- Labs run offline with only `pydantic` and the standard library. Third-party tools named by the syllabus (FastAPI, Strawberry, Zeep, SQLAlchemy, boto3, Presidio, NeMo Guardrails, LiteLLM, LangGraph, Neo4j, Pinecone, Qdrant, Streamlit, Gradio and others) appear as lazily imported adapters beside an offline implementation. **Those adapters were written from API knowledge and have not been run against the real libraries or services**, with a few exceptions noted in module reports (FastAPI and Graphene wiring in Module 2 were checked in a throwaway environment). Pin versions and verify against current docs before relying on them.

- Stand-ins: embeddings are hashed vectors, RAGAS-style metrics and the LLM judge are deterministic proxies, OCR and image models are toy implementations on synthetic pixel grids, Presidio/Colang/Bedrock-guardrail behavior is re-implemented in compact form, and SAML signature verification is deliberately not implemented (structure and conditions only; use a vetted library). Cost, price and ROI figures are illustrative inputs, not vendor data.

## Module 01: Python & Linux Foundations

Code: `modules/01_python_linux/lab.py` · Notes: `modules/01_python_linux/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Python Core data structures | `lab.py` section "Python core data structures": `demo_data_structures()`; README "Concept notes" 1 |
| 2 | Memory management fundamentals | `lab.py` section "memory management fundamentals": `demo_memory()`; README "Concept notes" 2 |
| 3 | Object-oriented programming basics | `lab.py` section "object-oriented programming basics": `Invoice`, `Notifier`, `SupportsClose`, `demo_oop()` |
| 4 | Exception handling | `lab.py` section "exception handling": `demo_exceptions()`; base `LabError` hierarchy and `retry_async()` at the top of the file |
| 5 | File I/O operations | `lab.py` section "file I/O operations": `atomic_write_text()`, `read_text_retrying()`, `demo_file_io()` |
| 6 | Event loop architecture | Existing: `LoopLagMonitor`, `WorkerPool`, `install_signal_handlers()`, `main()`; README "Intuition & Mental Model" and the architecture diagram |
| 7 | Coroutines and tasks | Existing: `WorkerPool._worker()` / `start()` (tasks), `retry_async()` (coroutines), `main()`; plus `asyncio.TaskGroup` in `demo_exceptions()` |
| 8 | Async context managers | `lab.py` section "async context managers": `PooledConnection` (`__aenter__`/`__aexit__`), `timed_section()` (`@asynccontextmanager`), `demo_async_context_managers()` |
| 9 | Concurrency vs parallelism | `lab.py` section "concurrency vs parallelism": `demo_concurrency_vs_parallelism()`; README "Concept notes" 9 |
| 10 | ThreadPoolExecutor integration | `lab.py` section "ThreadPoolExecutor integration": `ThreadBridge`, `BlockingClient`, `demo_thread_pool_integration()`; existing `ProcessPoolExecutor` use in `WorkerPool._process()` |
| 11 | Navigating the file system | `lab.py` section "navigating the file system": `build_sample_tree()`, `walk_tree()`; README "Concept notes" 11 |
| 12 | Permission and user management | `lab.py` section "permission and user management": `safe_chmod()`, `describe_user()`, `demo_permissions()`; README "Concept notes" 12 |
| 13 | Process monitoring | `lab.py` section "process monitoring": `process_info()` (`/proc` on Linux, `ps` fallback), `run_command()`, `demo_process_monitoring()`; also existing `proc_snapshot()` |
| 14 | Shell scripting basics | `lab.py` section "shell scripting basics": `SHELL_SCRIPT` run by `demo_shell_script()` via asyncio subprocess with timeout; README "Concept notes" 14 |
| 15 | Environment variable configurations | `lab.py` section "environment variable configuration": `AppConfig`, `load_config()`, `demo_env_config()`; README "Concept notes" 15 |

## Module 02: Modern API Development

Code: `modules/02_api_backend/lab.py` · Notes: `modules/02_api_backend/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Path and query parameters | `lab.py`: `MiniRouter` (typed `{item_id:int}` path params), `ListQuery` (validated query string), `install_extension_routes()` (FastAPI `Path`/`Query`) |
| 2 | Pydantic data validation | `lab.py`: `ItemCreate`, `ItemPatch`, `validate_model()` to typed 422; existing `ChatRequest`, `BucketConfig` |
| 3 | Dependency injection | `lab.py`: `Depends`, `DependencyContainer.overrides`, `DependencyScope`, `get_repo()`/`get_service()`; test `test_di_override_cleanup_and_cache` |
| 4 | CORS middleware | `lab.py`: `CorsPolicy`, `evaluate_cors()` (preflight), `cors_middleware()`; FastAPI `CORSMiddleware` in `install_extension_routes()` |
| 5 | Building scalable CRUD endpoints | `lab.py`: `ItemRepository`, `ItemService`, `Page` (offset pagination), `build_v2_router()`, `h_*_item` handlers |
| 6 | Designing GraphQL schemas and object types using Python frameworks like Strawberry or Graphene | `lab.py`: `GObject`/`GField`/`GraphQLSchema.sdl()`, `build_library_schema()` (offline); `build_strawberry_schema()`, `build_graphene_schema()` (lazy adapters) |
| 7 | Writing efficient data resolvers | `lab.py`: `_r_authors`, `_r_books_batched`, `_r_book_author_batched`, `execute_graphql()` |
| 8 | Handling N+1 query problems | `lab.py`: `DataLoader`, `BookStore.query_log`; `extension_demo()` logs `queries_before` 9 vs `queries_after` 2 |
| 9 | Executing queries vs mutations | `lab.py`: `execute_graphql()` (queries concurrent, mutations serial), `_r_add_book`, mutation demo and `test_graphql_n_plus_one_and_mutation` |
| 10 | Implementing URL-based vs Header-based API versioning | `lab.py`: `VersionedApi` (`/v1/...`, `X-API-Version`, `Accept: application/vnd.lab.vN+json`), `build_api()`; `/items/{id}` header route in `install_extension_routes()` |
| 11 | Unit testing fundamentals | `lab.py`: `ItemApiTests` (13 `unittest` tests), `run_self_tests()` executed by the demo |
| 12 | Fixtures and mocking | `lab.py`: `asyncSetUp`/`addCleanup`/`subTest` fixtures, `unittest.mock` (`AsyncMock`, `patch.object`, `patch.dict`) in `test_retry_recovers_with_mock`, `test_dataloader_batches_and_dedupes` |
| 13 | Test coverage analysis | `lab.py`: `measure_self_test_coverage()` (stdlib `trace` + `dis`); README Concept notes for `coverage.py`/`pytest-cov` commands |
| 14 | Debugging configurations | README Concept notes (launch.json); `lab.py`: `LAUNCH_JSON`, `LaunchFile` validation, `enable_debug_hooks()`, `demo_debug_and_ide()` |
| 15 | Essential IDE extensions | README Concept notes; `lab.py`: `EXTENSIONS_JSON` validated by `ExtensionsFile` in `demo_debug_and_ide()` |

## Module 03: Cloud Fundamentals & Networking

Code: `modules/03_cloud_networking/lab.py` · Notes: `modules/03_cloud_networking/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Provisioning virtual machines | `lab.py`, section "provisioning virtual machines": `VmLaunchSpec` (validator, IMDSv2 and tag rules), `pick_instance` and `monthly_cost` (sizing and cost), `vm_cli`, `vm_terraform`, `validate_vm_artifacts`. README: Concept notes, "Provisioning virtual machines" |
| 2 | Object storage lifecycle policies | `lab.py`, section "object storage lifecycle policies": `LifecycleRule`, `lifecycle_findings`, `apply_lifecycle` (age-based simulator), `lifecycle_json`, `lifecycle_from_json`. README: Concept notes, "Object storage lifecycle policies" |
| 3 | Managed relational databases setup | `lab.py`, section "managed relational databases setup": `DbInstanceConfig`, `audit_db_config`, `db_cli`, `db_terraform`, `validate_db_artifacts`. README: Concept notes, "Managed relational databases" |
| 4 | Event-driven serverless function basics | `lab.py`, section "event-driven serverless function basics": `EventPattern`, `EventDispatcher` (retries, idempotency, dead-letter queue), `invoke_lambda_boto3` (lazy adapter). README: Concept notes, "Event-driven functions" |
| 5 | Creating virtual private clouds | `lab.py`, section "creating VPCs ...": `carve_subnets`, `VpcDesign`, `build_three_tier_vpc`, `validate_design`, `vpc_cli`, `validate_vpc_cli`; existing `Vpc`, `Topology.validate_peerings` (CIDR overlap). README: first mermaid diagram |
| 6 | Public vs private subnet routing | `lab.py`: `classify_subnet` (public / private-with-nat / isolated), `internet_can_initiate`, existing `longest_prefix_match`. README: Concept notes, "VPC, subnets and NAT" |
| 7 | Outbound traffic via NAT | `lab.py`: `egress_path` (NAT must sit in a public subnet whose table reaches the internet gateway), `validate_design`. README: Concept notes, "VPC, subnets and NAT" |
| 8 | Configuring strict security groups | `lab.py`, section "configuring strict security groups": `audit_security_groups`, `tighten_world_rules`, `sg_revoke_cli`; existing `SecurityRule`, `sg_allows`. README: Concept notes, "Strict security groups" |
| 9 | Principle of least privilege | `lab.py`, section "principle of least privilege ...": `analyze_least_privilege` (granted vs used diff), `verify_tightened`; existing `evaluate_iam`. README: Intuition (point 3) and war story 2 |
| 10 | Creating identity policies | `lab.py`: `to_iam_json`, `validate_iam_json`, `from_iam_json` (round trip). README: Concept notes, "Identity policies" |
| 11 | Assuming cross-account roles | `lab.py`, section "assuming cross-account roles": `TrustPolicy`, `can_assume`, `audit_trust_policy`, `trust_json`, `assume_role_cli`, `assume_role_boto3` (lazy adapter). README: second mermaid diagram and Concept notes, "Cross-account roles" |
| 12 | Navigating the AWS Billing console | README only for the click path: Concept notes, "Billing console walkthrough". Code for the same data: `lab.py`, section "AWS Billing console ...": `cost_explorer_cli`, `fetch_costs_boto3`, `rollup_by_tag`, `rollup_by_service` |
| 13 | Setting automated budget thresholds | `lab.py`: `Budget`, `BudgetMonitor` (50/80/100% actual plus forecast alerts), `forecast_month_end`, `budget_artifacts`, `validate_budget_artifacts`. README: Concept notes, "Budgets" |

## Module 04: Containerization & CI/CD

Code: `modules/04_containers_cicd/lab.py` · Notes: `modules/04_containers_cicd/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Writing optimized Dockerfiles | `lab.py`, section "writing optimized Dockerfiles": `optimization_findings`, `simulate_build_cache` (which steps re-run after a code edit); existing `parse_dockerfile`, `lint_dockerfile` (security rules); `FASTAPI_DOCKERFILE` vs `NAIVE_DOCKERFILE`. README: Concept notes, "Optimized Dockerfiles"; Intuition point 1 |
| 2 | Managing multi-container environments | `lab.py`, section "multi-container environments ...": `load_compose`, `validate_compose`, `startup_layers` (depends_on ordering and cycles), `simulate_startup`, `compose_to_docker_run`. README: Concept notes, "Compose" |
| 3 | Volume mounting and networking | `lab.py`: `parse_volume`, `volume_report`, `can_reach` (shared user-defined network), `parse_port`, published-port and docker.sock checks in `validate_compose`. README: Concept notes, "Volumes and networking" |
| 4 | Containerizing FastAPI backends | `lab.py`, section "containerizing FastAPI backends": `FASTAPI_APP_PY` (`/healthz`, `/readyz`), `FASTAPI_DOCKERFILE` (checked by `lint_dockerfile`), `fastapi_contract_check`, `run_fastapi_smoke_test` (lazy adapter). README: Concept notes, "FastAPI container" |
| 5 | Serverless container concepts | `lab.py`: `choose_runtime`, `WorkloadProfile`, `smallest_fargate_size`. README: Concept notes, "Serverless containers" |
| 6 | Configuring task definitions | `lab.py`, section "serverless container concepts and configuring task definitions": `TaskDefinition`, `validate_task_definition`, `FARGATE_MEMORY_MIB`, `taskdef_json`, `validate_taskdef_json`. README: Concept notes, "Task definitions" |
| 7 | Load balancer integration | `lab.py`, section "load balancer integration": `TargetGroup`, `HealthTracker`, `LoadBalancerSim` (rules, weights, fail-open), `check_service_wiring`, `ecs_service_cli`. README: Concept notes, "Load balancer integration" |
| 8 | Auto-scaling policy configuration | `lab.py`, section "auto-scaling policy configuration": `ScalingPolicy`, `desired_capacity`, `simulate_autoscaling` (cooldowns), `scaling_artifacts`, `validate_scaling_artifacts`. README: Concept notes, "Auto-scaling" |
| 9 | Creating workflow YAML files | `lab.py`, section "workflow YAML": `Workflow`/`WfJob`/`WfStep` models, `build_ci_cd_workflow`, `emit_yaml`, `parse_yaml`, `validate_workflow`. README: Concept notes, "Workflow YAML" |
| 10 | Triggering automated builds | `lab.py`, section "triggering automated builds": `workflow_triggers` (push, pull_request, branch/tag/path filters), `gh_glob`, `eval_condition`, `jobs_to_run`. README: Concept notes, "Build triggers"; existing mermaid diagram (CI stages) |
| 11 | Managing GitHub secrets | `lab.py`, section "managing GitHub secrets": `validate_secret_name`, `collect_secret_refs`, `reconcile_secrets`, `LogMasker`, secret-handling rules in `validate_workflow`, `github_oidc_trust`, `oidc_allows`, `audit_oidc_trust`. README: Concept notes, "GitHub secrets and OIDC" |
| 12 | Continuous deployment to AWS | `lab.py`, section "continuous deployment to AWS with rollback": `build_ci_cd_workflow` (test, build, deploy jobs), `plan_rolling_deploy`, `deploy_with_rollback`, `ecs_deploy_boto3` (lazy adapter). README: Concept notes, "Continuous deployment"; existing mermaid diagram (canary, auto-rollback) |

## Module 05: LLM Fundamentals & Prompting

Code: `modules/05_llm_foundations/lab.py` · Notes: `modules/05_llm_foundations/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Zero-shot and few-shot prompting | `lab.py`, section "zero-shot, few-shot and chain-of-thought prompting": `build_zero_shot`, `build_few_shot` |
| 2 | Chain of thought reasoning | `lab.py`, same section: `build_chain_of_thought`, `parse_reasoned_output` (reasoning vs answer split, why the reasoning is never logged) |
| 3 | Token calculation | `lab.py`, section "token calculation": `MiniBPE` (train and count), `chat_tokens`, `request_cost`; baseline `estimate_tokens` |
| 4 | Sliding window techniques | `lab.py`, section "sliding window over history and over long text": `SlidingWindowHistory.trim`, `sliding_windows`; budget-based packing in `ContextBudgeter` |
| 5 | Context compression | `lab.py`, section "context compression": `compress_context` (extractive, budgeted); truncation in `ContextBudgeter.pack` |
| 6 | Enforcing strict JSON output schemas via APIs | `lab.py`, section "strict JSON schemas...": `to_strict_schema`, `openai_response_format`, `anthropic_forced_tool`; local enforcement in `StructuredExtractor` |
| 7 | Defining complex nested Pydantic models | `lab.py`, same section: `PurchaseOrder` -> `Party` -> `Address`, `LineItem`, `dict[str, list[LineItem]]` |
| 8 | Validating LLM responses natively against type hints | `lab.py`, same section: `validate_against_hint` (`TypeAdapter`, lax vs `strict=True`), `StrictModel` (`extra="forbid"`) |
| 9 | Handling and retrying output parsing errors gracefully | `lab.py`: `StructuredExtractor.extract` (bounded repair loop with exact errors), `extract_json`, `retry_with_backoff`; demo repairs a bad `ActionPlan` |
| 10 | Using discriminator fields for union types | `lab.py`: `NextAction = Annotated[... Field(discriminator="kind")]`, `ActionPlan`, JSON round trip in `extension_demo` |
| 11 | Defining precise function schemas for LLMs | `lab.py`, section "function/tool schemas...": `RegisteredTool.openai_schema`, `RegisteredTool.anthropic_schema`, `ToolRegistry.schemas` |
| 12 | Parsing and validating tool arguments | `lab.py`: `parse_tool_arguments` (JSON string or object, then Pydantic), `ToolArgumentError` |
| 13 | Handling multi-tool parallel execution | `lab.py`: `execute_tool_calls` (`asyncio.gather`), `execute_tool_call` (per-tool timeout, transient retry, call ids preserved) |
| 14 | Processing tool results into chat history | `lab.py`, section "tool results in chat history...": `assistant_tool_message`, `tool_result_messages` (OpenAI and Anthropic shapes) |
| 15 | Managing hallucinated tool calls | `lab.py`: `ToolCallGuard.check` (unknown name with suggestion, bad args, repeated call, reused id), `run_tool_loop` (error results fed back, round cap) |

## Module 06: Vector Search & Core RAG

Code: `modules/06_advanced_rag/lab.py` · Notes: `modules/06_advanced_rag/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Fixed-size and semantic chunking | `lab.py`, section "fixed-size vs semantic chunking...": `fixed_size_chunk`, `semantic_chunk` (sentence-similarity breakpoints); structure-aware baseline `recursive_chunk` |
| 2 | Overlap optimization | `lab.py`, same section: `overlap_sweep` over `LABELED_QA` (coverage, recall@1, index bloat vs overlap) |
| 3 | Understanding vector representations | `lab.py`, section "vector representations...": `sparse_vector` vs `embed` / `toy_semantic_embed` |
| 4 | Dimensionality trade-offs | `lab.py`, same section: `dimension_sweep` (collisions and recall vs dims), `quantize_int8`, `vector_memory_bytes` |
| 5 | BM25 sparse matrices | `lab.py`, section "BM25 as an explicit sparse matrix": `SparseBM25Matrix` (CSR `indptr/indices/data`, checked equal to `BM25Index`) |
| 6 | Index creation | `lab.py`, section "index creation": `FlatIndex`, `IVFIndex`, `ivf_probe_sweep`; managed-index creation in `provision_index` and `VectorDB.create_index` |
| 7 | Distance metrics (Cosine, Euclidean) | `lab.py`, section "distance metrics": `cosine`, `euclidean`, `dot`, `l2_normalize`, `metric_equivalence_demo` |
| 8 | Metadata filtering | `lab.py`, section "vector DB client Protocol...": `metadata_matches`, `InMemoryVectorDB.query` (pre-filter), `QdrantVectorDB.to_qdrant_filter`; pre vs post filter demo |
| 9 | Cloud vector database provisioning | `lab.py`: `IndexSpec`, `provision_index` (idempotent, drift guard), `PineconeVectorDB`, `QdrantVectorDB`, `ResilientVectorDB` |
| 10 | End-to-end basic retrieval | `lab.py`, section "end-to-end basic retrieval": `build_basic_index`, `basic_retrieve`; full hybrid path in `HybridRetriever` |
| 11 | Keyword-based search mechanisms | `lab.py`: `PositionalIndex` (boolean AND/OR/NOT, phrase), `tokenize`, `BM25Index`, `SparseBM25Matrix` |
| 12 | Combining dense and sparse signals | `lab.py`: `HybridRetriever.retrieve`, `weighted_fusion` (min-max normalised, alpha), `rrf` |
| 13 | Implementing RRF algorithms | `lab.py`: `rrf` (existing), `rrf_explain` (per-document 1/(k+rank) terms), k sweep in `extension_demo` |
| 14 | Precision and recall metrics | `lab.py`, section "relevance metrics": `precision_at_k`, `recall_at_k`, `reciprocal_rank` (MRR), `ndcg_at_k`, `score_system` on `EVAL_QUERIES` |
| 15 | Cross-encoder reranking models | `lab.py`: `Reranker` protocol, `LexicalCrossEncoder` (stand-in), `SentenceTransformersCrossEncoder` (lazy, real model), `FallbackReranker` |
| 16 | API integration for rerankers | `lab.py`, section "cross-encoder rerankers and API integration": `HTTPReranker`, `HTTPTransport`, `UrllibTransport`, `FakeRerankTransport` (used in the demo), retries on 429/5xx |

## Module 07: Enterprise Graph Architecture

Code: `modules/07_graph_rag/lab.py` · Notes: `modules/07_graph_rag/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Nodes, edges, and properties | lab.py, banner "typed property graph": `PropertyGraph`, `GNode`, `GRel`, `LabelSchema`, `build_demo_property_graph` (unique and required constraints, indexes, `merge_node` / `merge_rel`); README Concept notes "Nodes, relationships, properties" |
| 2 | Ontologies vs taxonomies | lab.py, banner "taxonomy vs ontology": `Taxonomy` (single-parent tree, `ancestors`, `lowest_common_ancestor`), `Ontology` + `RelationDef` (domain/range, `infer` for transitive, inverse and symmetric rules, `validate`), `build_logistics_ontology` |
| 3 | Transforming relational data tables to graph structures | lab.py, banner "relational tables to a graph": `introspect`, `relational_to_cypher` (idempotent `MERGE` statements), `load_cypher`, `make_sales_db` (sqlite3, FKs and a join table) |
| 4 | AuraDB cloud provisioning | README Concept notes "AuraDB provisioning" (console flow, URI, env vars, verification); lab.py `Neo4jAdapter` (`validate_uri`, `from_env`, lazy `neo4j` import; the live connection is not exercised offline) |
| 5 | Pattern matching syntax | lab.py, banner "pattern matching and path traversal": `parse_query` and `PatternEngine.run` (labels, types, directions, property maps, `WHERE`, `RETURN`, `LIMIT`); README Concept notes "Pattern syntax cheat sheet" |
| 6 | Path traversal queries | lab.py: variable-length `*1..3` in `PatternEngine`, `shortest_path` (Dijkstra, blocked nodes), `k_paths` and `score_path` (path ranking) |
| 7 | Optimizing complex graph joins | lab.py: `PatternEngine.plan` / `_cost` (anchor choice by cardinality estimate), `PropertyGraph.create_index`, `PropertyGraph.touched`; `extension_demo` step 5 prints rows touched for left, right, auto and indexed plans |
| 8 | Overview of Amazon Neptune | README Concept notes "Amazon Neptune overview"; lab.py `TraversalSpec`, `to_open_cypher`, `to_gremlin` (same traversal as both languages) |
| 9 | Mapping logistics networks | lab.py, banner "logistics network modeling": `build_logistics_graph`, `fastest_route` (avoid closed hub), `exposure_if_hub_fails` |
| 10 | Modeling user interactions | lab.py, banner "modeling user interactions": `build_interaction_graph`, `user_vector` (recency decay), `recommend`, `co_occurrence` |
| 11 | Graph-based retrieval logic for AI chatbot | lab.py, banner "graph-based retrieval logic": `GraphRetriever` (link, bounded expand, cited context), `unresolvable_citations`; existing `CypherPlanner`, `guard_cypher`, `read_with_retry` and `InMemoryGraph` cover the guarded text-to-Cypher path |

## Module 08: Multimodal RAG & Vision AI

Code: `modules/08_multimodal_rag/lab.py` · Notes: `modules/08_multimodal_rag/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Identifying document structures | lab.py, banner "identifying document structures": `classify_page_source`, `classify_document` (born-digital vs scanned vs mixed routing), `detect_structure` (headings, lists, tables, figure captions, built on the existing `parse_page`) |
| 2 | Optical character recognition pipelines | lab.py, banner "OCR pipeline": `run_ocr_pipeline` with `median3`, `otsu_threshold`, `binarize`, `estimate_skew`, `rotate`, `recognize_glyph` and the accept / review / reject gate; existing `FlakyOcr` + `ocr_with_retry` for the service side; `grid_from_pillow` lazy adapter |
| 3 | OCR-free embedding strategies via ColPali | lab.py, banner "OCR-free page embeddings": `patchify`, `encode_patch`, `PixelPageIndex` (MaxSim over pixel patches, `index_bytes`); the existing "ColPali-style late interaction" section (`vec`, `PageIndex`) is the word-level version |
| 4 | Architecture of vision-language bridges | lab.py, banner "vision-language bridge": `BridgeSpec` (patch and token math), `vision_features`, `merge_patches`, `LinearProjector`, `build_sequence`; README Concept notes "Vision-language bridges" |
| 5 | Prompting with images | lab.py, banner "prompting with images": `make_png`, `validate_image`, `anthropic_user_message`, `openai_user_message`, `anthropic_image_tokens`, `tile_image_tokens`, `AnthropicVisionAdapter` (lazy, not exercised offline) |
| 6 | Contrastive language-image pretraining (CLIP) concepts | lab.py, banner "CLIP-style contrastive learning": `info_nce` (symmetric loss, chance level ln N), `zero_shot`, `contrastive_step` (one gradient step), `text_embed`, `image_embed` |
| 7 | Image-to-image similarity search | lab.py, banner "image-to-image similarity search": `average_hash`, `difference_hash`, `hamming`, `color_histogram`, `ImageIndex` (flat index), `make_rgb_image`, `perturb` |
| 8 | Extracting nested tabular data natively | lab.py, banner "nested and merged-cell tables": `SpanCell`, `expand_spans`, `span_table_to_records` (multi-level header paths, row paths, total check), `records_to_tree`, `make_nested_table`; existing `table_to_records` and `validate_totals` handle flat tables |
| 9 | Reasoning over complex chart visuals and infographs | lab.py, banner "chart reasoning from a pixel grid": `render_bar_chart`, `extract_bars`, `reconcile` (VLM answer vs pixel measurement), `describe_trend`, `CHART_PROMPT`; infographics are README-only (Concept notes "Charts and infographics") |
| 10 | Ingesting unstructured legacy enterprise PDFs | lab.py, banner "ingesting unstructured legacy PDFs": `strip_headers_footers`, `repair_hyphenation`, `reading_order`, `text_quality`, `ExtractorChain` (text layer then OCR fallback), `LegacyPdfAdapters` (lazy pypdf and Docling) |
| 11 | Aligning bounding boxes with text chunks | lab.py, banner "aligning bounding boxes with text chunks": `Box`, `iou`, `containment`, `assign_words_to_regions`, `chunk_citation`, `locate_quote` |
| 12 | Managing multi-page visual context | lab.py, banner "managing multi-page visual context": `select_pages` (token-budgeted page window), `stitch_tables` (cross-page table merge with repeated-header handling), `make_table_page`, `PageRef` |

## Module 09: Agentic Frameworks & LangGraph

Code: `modules/09_langgraph_agents/lab.py` · Notes: `modules/09_langgraph_agents/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Defining agency | lab.py, "defining agency": `Agent` Protocol (goal, tools, memory, perceive/decide/act), `OrderAgent`, `run_agent` (termination by a `finish` action plus a step guard) versus `fixed_chain`; the "agency" log lines run three inputs through both |
| 2 | ReAct framework loops | lab.py, "ReAct loop": `react_loop` (Thought/Action/Observation transcript), `ScriptedLLM`, tool registry dict, `AgentLoopError` step guard, unknown-tool and malformed-reply observations |
| 3 | Plan & Execute structures | lab.py, "Plan-and-Execute": typed `Plan`/`PlanStep`, `revenue_planner` (planner and replanner), `plan_and_execute` (executor, replan on `ToolPermanentError`, `max_replans`) |
| 4 | Supervisor and Router patterns | lab.py, "router vs supervisor": `build_router_graph` (classify once, route to a specialist subgraph) and `build_supervisor_graph` (control returns to the supervisor after every worker; it decides when to FINISH); the older consensus/governance version is Module 10 |
| 5 | Defining graphs and state management | lab.py: `AgentState` + `apply_update` + `Checkpoint`/`FileCheckpointer` (original engine); `ConcurrentGraph(state_cls, reducers)` and `merge_updates` (typed state per graph, per-field reducers) |
| 6 | Creating nodes and edges | lab.py: `StateGraph.add_node`, `add_edge`, `set_entry` (original engine; inherited and extended by `ConcurrentGraph`, which adds `add_fanout`, `add_map`, `add_subgraph`) |
| 7 | Conditional routing logic | lab.py: `StateGraph.add_conditional_edges` with `after_critic` in `build_graph` (revise loop); `ConcurrentGraph.add_conditional_edges(..., targets=[...])` with `route` in `build_router_graph` and `decide` in `build_supervisor_graph` |
| 8 | Compiling graphs | lab.py: `StateGraph.compile` (edge check, attaches checkpointer) and `ConcurrentGraph.validate` / `compile_concurrent` (missing edges, unreachable nodes, cycles with no path to END, bad targets); `build_invalid_graph` shows the `GraphValidationError` report |
| 9 | Running agent tasks concurrently using async execution | lab.py, "concurrent fan-out": `ConcurrentGraph.add_fanout`, `CompiledConcurrent._gather` (`asyncio.gather` with per-node `asyncio.wait_for` timeouts, `NodeTimeoutError`), `build_research_graph`; `one_run` in `extension_demo` runs whole graphs concurrently under a semaphore; optional `build_langgraph_app` |
| 10 | Managing nested parent-child graph architectures | lab.py: `ConcurrentGraph.add_subgraph` (child has its own state schema, `input_map`/`output_map`), `build_parent_child_graph`, `make_specialist` |
| 11 | Aggregating parallel outputs with map-reduce patterns | lab.py: `ConcurrentGraph.add_map` (one branch per item, `max_concurrency` semaphore) and the reduce node in `build_map_reduce_graph`, with `FlakySummariser` + `with_backoff` inside the worker |
| 12 | Handling state conflicts during concurrent node execution | lab.py: `merge_updates` (raises `StateConflictError` when two branches write one field without a reducer), reducers `reduce_append`, `reduce_add`, `reduce_max`, `reduce_lww` over `Versioned` (version, then writer, so finish order never matters) |

## Module 10: Advanced Agent Orchestration

Code: `modules/10_multi_agent_systems/lab.py` · Notes: `modules/10_multi_agent_systems/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Check-pointing graph states | lab.py, "check-pointing, time travel...": `Checkpoint`, `CheckpointStore` (history, latest, get; writes retried with backoff), `DurableRunner` (checkpoint after every node), `DurableRunner.fork` for time travel. Module 09's `FileCheckpointer` is the durable-file variant |
| 2 | Semantic long-term memory retrieval | lab.py, "semantic long-term memory": `embed` (hashed embedding), `LongTermMemory.remember` / `recall` (similarity + recency decay + importance) / `forget`, one namespace per user via `namespace_for` |
| 3 | Interrupting graph execution | lab.py: `DurableRunner(interrupt_before=...)` persists a checkpoint with `pending_approval` and returns status `interrupted`; module 09 `Interrupted` is the exception-style equivalent |
| 4 | Requesting manual state approval | lab.py: `ResumeCommand` (approver, justification, state `edits`), `DurableRunner._apply_resume` (editable-field allowlist, audit trail, rejection raises `ApprovalRejectedError`); the risk-tiered queue is the original `govern` + `ApprovalQueue` |
| 5 | Detecting infinite ReAct loops | lab.py, "detecting infinite ReAct loops": `LoopDetector` (repeated signature, A-B-A-B ping-pong, no-new-observation window, token budget) and `guarded_react` (stops, files an `ApprovalQueue` escalation, raises `LoopDetectedError`) |
| 6 | Self-correction prompting mechanisms | lab.py, "self-correction prompting": `CRITIC_TEMPLATE`, `REFLECT_TEMPLATE`, typed `Critique`, `self_correct` (bounded rounds, target score, plateau/regression stop, returns the best answer) |
| 7 | Managed agent provisioning | lab.py, "managed agent provisioning": `AgentSpec`, `ControlPlane.create` / `update` (optimistic version check) / `retire` / `provision` (short-lived `ScopedLease`), `check_policy`, audit log |
| 8 | Integrating knowledge bases | lab.py, "knowledge-base integration": `KnowledgeBase.ingest` (concurrent fetch with backoff, chunking, embedding + keyword index), `search` (hybrid score), `answer` (citations or abstain), `verify_citations` |
| 9 | Connecting agents to external tools safely | lab.py, "connecting agents to external tools safely": `SafeToolGateway.call` (allowlist, Pydantic argument schema with `extra="forbid"`, egress host allowlist, per-tool rate limit, confirmation for write tools, timeout, no auto-retry of writes, audit) |
| 10 | Host/Client/Server architectures | lab.py, "MCP Host / Client / Server": `McpHost` (policy, consent, aggregation), `McpClient` (1:1 session, initialize and capability negotiation), `McpServer`, `InProcessTransport` (JSON-RPC 2.0 messages, wire log); module 11 covers MCP-style integrations in depth |
| 11 | Standardizing tool access boundaries | lab.py, "standardized tool access boundaries": `ToolManifest` / `ManifestEntry` (boundary, scopes, data classification, write flag, `digest()` pin), `Principal`, `BoundaryEnforcer` (cross-boundary, scope and clearance checks, filtered discovery, audit) |

## Module 11: Legacy Systems & Integrations

Code: `modules/11_mcp_integrations/lab.py` · Notes: `modules/11_mcp_integrations/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Webhooks and bot tokens for Slack/Teams | `lab.py`, sections "Slack-style webhooks and bot tokens" (`SlackRequestVerifier`, `SlackWebhookReceiver`, `load_bot_token`, `SlackWebApiAdapter`) and "Teams-style webhooks and Adaptive Cards" (`adaptive_card`, `teams_incoming_webhook_payload`, `teams_bot_reply`) |
| 2 | Interactive message payloads | `lab.py`, section "interactive message payloads (Slack Block Kit)" (`approval_blocks`, `parse_interactive_body`, `BlockActionsPayload`, `SlackInteractivity`, `encode_action_value`) |
| 3 | Reading internal Jira pages | `lab.py`, section "reading Jira pages and automating ticket creation" (`JiraRestClient.read_issue`, `adf_to_text`, `JiraRestClient.search_all`, `FakeJiraTransport`, `UrllibJiraTransport`); the original `JiraAdapter` in the MCP server section is the tool-facing version |
| 4 | Automating ticket creation | `lab.py`, same section (`TicketRequest`, `JiraRestClient.create_ticket` with dedupe label, 429 `Retry-After` handling in `JiraRestClient._call`) |
| 5 | Understanding WSDL document structures and types | `lab.py`, section "WSDL structure, typed catalog, Pydantic models, valid envelopes" (`INVENTORY_WSDL`, `parse_wsdl`, `WsdlCatalog`, `build_request_models`); concept notes below |
| 6 | Constructing valid XML SOAP envelopes using Python libraries like Zeep | `lab.py`, same section (`build_soap_envelope`, `WsdlSoapClient`, lazy Zeep adapter `zeep_call`); the original `SoapAdapter.build_envelope` is the minimal hand-built version |
| 7 | Parsing complex legacy XML responses safely | `lab.py`, section "safe parsing of legacy XML" (`safe_parse_xml`, `find_local`, `safe_parse_with_defusedxml`) |
| 8 | Handling SOAP faults and legacy error codes | `lab.py`, section "SOAP faults and legacy error codes" (`extract_fault` for SOAP 1.1 and 1.2, `LEGACY_ERROR_TABLE`, `fault_to_error`, typed `SoapClientFault` / `SoapServerFault` / `LegacyBusy`) |
| 9 | Converting XML payloads to modern JSON formats | `lab.py`, section "XML to modern JSON" (`xml_to_json`, `coerce_scalar`, `LEGACY_XML_SAMPLE`) |
| 10 | Establishing secure connections using Python-native drivers (pyodbc, oracledb) and SQLAlchemy | `lab.py`, section "secure database connections" (`DbConnectionConfig`, `build_sqlalchemy_engine`, `native_connect`) |
| 11 | Constructing safe parameterized queries to prevent SQL injection | `lab.py`, section "the lab ERP database, schema allowlist, safe query builder" (`build_select`, `INJECTION_CORPUS`, `injection_test`); the original `SqlAdapter.query_orders` is the single-table version |
| 12 | Implementing read-only database roles | `lab.py`, section "read-only roles" (`classify_sql`, `ReadOnlyExecutor`, `prove_read_only`); real `GRANT` statements in Concept notes below |
| 13 | Mapping complex database schemas to LLM context | `lab.py`, section "schema to LLM context" (`map_schema`, `schema_context`, `approx_tokens`) |
| 14 | Handling Text-to-SQL logic constraints and fallbacks | `lab.py`, section "Text-to-SQL pipeline with constraints and fallbacks" (`TextToSqlPipeline.validate`, `ScriptedLLM`, `SQL_TEMPLATES`, repair loop and refusal in `TextToSqlPipeline.answer`) |

## Module 12: Identity & Access Management

Code: `modules/12_rbac_security/lab.py` · Notes: `modules/12_rbac_security/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Authentication vs authorization | `lab.py`, section "authentication vs authorization (401 vs 403)" (`guarded_endpoint`, with the original `AuthenticationError` / `AuthorizationError` and `authorize`); also "Intuition & Mental Model" above |
| 2 | Understanding JWT tokens | `lab.py`, section "JWT anatomy (decoding is NOT verifying)" (`decode_unverified`, `REGISTERED_CLAIMS`) plus the original `sign_jwt`, `JwtVerifier`, `KeySet` (verification, `kid` rotation, `alg` allowlist) |
| 3 | OAuth grant types | `lab.py`, section "OAuth 2.0 grant types as a state machine" (`AuthorizationServer`, `PkceClient`, `poll_device_token`; authorization code + PKCE, client credentials, device authorization, refresh rotation with reuse detection, deprecated implicit and password grants refused) |
| 4 | SAML assertions | `lab.py`, section "SAML 2.0 assertions" (`SamlAssertionValidator`, `build_lab_saml_response`, `parse_xml_safely`, lazy `python3_saml_validate`); signature verification is deliberately not implemented, see Concept notes |
| 5 | Mapping Azure AD groups | `lab.py`, section "Azure AD (Entra ID) group mapping" (`resolve_entra_groups`, `GraphGroupResolver`, `FakeGraphTransport`, `GroupRoleMap`) |
| 6 | Implementing Role-Based Access Control | `lab.py`, section "RBAC with role hierarchy, plus ABAC conditions" (`RoleHierarchy`, `PolicyEngine`, `build_lab_policy`) plus the original `ROLE_PERMISSIONS` and `authorize` |
| 7 | Enforcing data-level permissions in retrieval layers | `lab.py`, section "data-level permissions in the retrieval layer" (`AclVectorStore`, `compile_filter`, `eval_filter`, `apply_masks`, `isolation_matrix`, audit entries) plus the original `TenantVectorStore` |

## Module 13: Production AI Security & Guardrails

Code: `modules/13_guardrails_safety/lab.py` · Notes: `modules/13_guardrails_safety/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Identifying major LLM vulnerabilities | `lab.py`, section "LLM vulnerability catalog (paraphrased working catalog)": `VulnId`, `VULN_REGISTRY` (10 entries, each with mitigations mapped to lab code), `PROBES`, `run_vulnerability_scan()` |
| 2 | Prompt injection defenses | `lab.py`, section "prompt-injection defenses in depth": `assemble_prompt` (instruction hierarchy), `spotlight_datamark`, `spotlight_encode`, `ToolGate`, `validate_output`, `run_attack` and `attack_matrix()` (before/after). Earlier base code: `injection_score`, `spotlight`, `GuardedAssistant` |
| 3 | Jailbreak prevention techniques | `lab.py`, section "jailbreak prevention": `deep_views` (leetspeak, rot13, base64, spaced letters, payload concatenation), `JailbreakDetector`, many-shot check, `ConversationRiskTracker`, `build_attack_corpus()` |
| 4 | Implementing Microsoft Presidio analyzers and anonymizers | `lab.py`, section "Presidio-style analyzers and anonymizers": `PatternRecognizerLite`, `RecognizerRegistryLite`, `ContextEnhancer`, `AnalyzerEngineLite`, `RecognizerResult` with `AnalysisExplanation`, `AnonymizerEngineLite`, `OperatorConfigLite`; real library via `lazy_presidio_adapter` |
| 5 | Redacting sensitive entities (SSN, credit cards, emails) | `lab.py`, `build_default_engine` (`EMAIL_ADDRESS`, `CREDIT_CARD` with `luhn_validate`, `US_SSN` with `ssn_validate` plus context words) and the replace/mask/hash/redact/tokenize operators in `AnonymizerEngineLite`. Earlier base code: `analyze_pii`, `TokenVault` |
| 6 | Customizing regex patterns for domain-specific PII | `lab.py`, `build_default_engine(with_domain_recognizers=True)`: `EMPLOYEE_ID`, `MEDICAL_RECORD_NUMBER` (labeled and bare pattern plus context), `INTERNAL_TICKET_ID`, `PROJECT_CODENAME` (deny list), `IBAN_CODE` (mod-97 `iban_validate`) |
| 7 | Reversing masks safely post-generation | `lab.py`, `ReversibleVault`, `RestorePrincipal`, `RestoreDenied`, `VaultCipher`/`FernetCipher`, `restore_controls_selftest()` (owner, scope, expiry, request id, forged token, never-restore entities) |
| 8 | Evaluating false-positive redaction rates | `lab.py`, section "evaluating false-positive / false-negative redaction rates": `parse_labeled`, `LABELED_MARKUP`, `BENIGN_TEXTS`, `evaluate_redaction`, `sweep_thresholds`, `pick_threshold`, strict-vs-naive comparison in `extension_demo` |
| 9 | Writing programmable conversational rails using Colang | `lab.py`, section "programmable conversational rails": `COLANG_SOURCE`, `parse_colang`, `ColangRuntime`, `to_nemo_colang1`, `lazy_nemo_rails`, `nemo_generate` |
| 10 | Configuring strict input and output filtering pipelines natively in Python | `lab.py`, section "strict input/output filtering pipeline": `RailPipeline`, `BaseRail` subclasses (`LengthRail`, `NormalizeRail`, `JailbreakRail`, `PiiMaskRail`, `TopicRail`, `CanaryRail`, `SecretRail`, `UrlAllowlistRail`), `RailMode`, `FlakyRail` |
| 11 | Enforcing topical boundaries to prevent off-topic chatter | `lab.py`, section "topical boundary enforcement": `TopicClassifier`, `ALLOWED_TOPICS`, `TOPIC_EVAL_SET`, `TopicRail`; also the `politics_rail` and `topic_guard` flows inside `COLANG_SOURCE` |
| 12 | Setting up AWS Bedrock managed guardrail configurations via Boto3 | `lab.py`, section "AWS Bedrock managed guardrails via boto3": `GuardrailSpec` and sub-models, `to_create_kwargs`, `BedrockGuardrailsAdapter` (`from_boto3`, `create`, `apply`), `FakeBedrockClient`, `FakeBedrockRuntime`, `support_assistant_guardrail_spec()` |
| 13 | Testing rails against jailbreak libraries | `lab.py`, section "testing rails against a jailbreak library": `evaluate_rail`, `RailReport`, `ci_gate`, `ci_exit_code`, corpus from `build_attack_corpus()`, five rails compared in `extension_demo` |

## Module 14: AI Observability & Gateway Management

Code: `modules/14_observability_evals/lab.py` · Notes: `modules/14_observability_evals/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Idempotency keys for safe tool execution | `lab.py`, section "idempotency keys for safe tool execution": `derive_idempotency_key`, `IdempotencyStore` (in-flight join, replay, TTL, `IdempotencyConflict`), `FakeEmailService` lost-acknowledgement demo |
| 2 | Exponential backoff strategies | `lab.py`, section "exponential backoff strategies and a retry-storm simulator": `ExponentialCapped`, `FullJitter`, `EqualJitter`, `DecorrelatedJitter`, `retry_with_strategy`, `simulate_retry_storm`; the shared `with_backoff` helper wraps every I/O adapter |
| 3 | Centralizing provider API keys via LiteLLM/Portkey | `lab.py`, section "LiteLLM-style gateway config, virtual keys, and a lazy litellm adapter": `parse_yamlish`, `GatewayConfig`, `resolve_secret` (os.environ references only), `load_gateway_config`, `GATEWAY_CONFIG_TEXT`, `VirtualKeyStore`, `AliasRouter`, `lazy_litellm_router` |
| 4 | Configuring rate limiting and fallback routing | `lab.py`, sections "rate-limiting algorithms" (`TokenBucket`, `FixedWindow`, `SlidingWindowLog`, `SlidingWindowCounter`, `ConcurrencyLimiter`, `compare_limiters`) and "fallback routing policies" (`PriorityPolicy`, `LatencyPolicy`, `CostPolicy`, `LeastBusyPolicy`, `LimitedProvider`, `AliasRouter` fallbacks); wired into `Gateway(limiter=, concurrency=, policy=)` |
| 5 | Automating LLM-as-a-judge scoring pipelines | `lab.py`, section "LLM-as-judge scoring pipeline": `Rubric`, `render_pointwise_prompt`, `render_pairwise_prompt`, `parse_json_verdict`, `judge_pointwise`, `judge_pairwise` (order-swap), `cohen_kappa`, `DeterministicJudge`, `AnthropicJudge` |
| 6 | Calculating RAGAS faithfulness and answer relevance metrics | `lab.py`, section "RAGAS-style metrics, fully defined": `extract_claims`, `claim_supported`, `ragas_faithfulness`, `TemplateQuestionGenerator`, `ragas_answer_relevancy`. Earlier base code: `faithfulness`, `answer_relevancy` |
| 7 | Measuring context precision and recall | `lab.py`, same section: `chunk_relevant`, `ragas_context_precision`, `ragas_context_recall`, `ragas_scores`. Earlier base code: `context_precision` |
| 8 | Creating synthetic benchmark datasets from source documents | `lab.py`, section "synthetic benchmark datasets from source documents": `SOURCE_DOCS`, `generate_synthetic_dataset` (factoid, multi-hop, negative, unanswerable; spans, difficulty, dedupe), `NaiveRagSystem`, `evaluate_system` |
| 9 | Tracking evaluation scores across deployment runs | `lab.py`, section "tracking evaluation scores across deployment runs": `RunMeta`, `EvalRun`, `RunRegistry` (JSONL round trip), `bootstrap_ci`, `compare_runs`. Earlier base code: `gate` regression check |
| 10 | Capturing deep span-level execution traces | `lab.py`, section "deep span-level traces, token/cost accounting, latency histograms": `SpanFactory`, `llm_attrs` (OpenTelemetry GenAI-style attribute names), `generate_traffic` (http.request, retrieval, llm.call). Earlier base code: `Tracer`, `Span` |
| 11 | Monitoring granular token costs and endpoint latency | `lab.py`, same section: `token_cost`, `LatencyHistogram`, `endpoint_report` (p50/p95/p99, tokens, cost per call), `cost_by_model`, `detect_cost_anomaly` |
| 12 | Debugging multi-step agent reasoning and tool inputs | `lab.py`, section "debugging multi-step agent reasoning and tool inputs": `AGENT_SCRIPT`, `build_agent_trace`, `reconstruct_agent_run` (repeated call, tool error, ballooning context, slow step, too many steps) |
| 13 | Creating custom user-session tracking metrics | `lab.py`, section "custom user-session tracking metrics": `SessionEvent`, `summarize_sessions`, `aggregate_sessions` (turns, time to first token, thumbs, abandonment, cost per session), `retention_buckets` (D1/D3/D7) |
| 14 | Exporting trace data for continuous model improvement | `lab.py`, section "exporting trace data for continuous improvement": `export_jsonl`, `to_otlp_json`, `mask_pii`, `build_dataset` (quality filters, dedupe, masked chat-format records), `LangfuseShapeExporter`, `InMemoryLangfuse` |

## Module 15: OmniGuard : Secure AI Integration

Code: `modules/15_capstone_omniguard/lab.py` · Notes: `modules/15_capstone_omniguard/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Conducting technical discovery and scoping workshops | `lab.py`, section "technical discovery and scoping workshops": `QUESTION_BANK`, `build_agenda`, `assess_discovery` (answers to coverage, risk flags, scope tier, SOW assumptions). README: Concept notes 1 |
| 2 | Defining data classifications | `lab.py`, section "data classifications": `DataClass`, `HandlingRule`, `ClassificationPolicy.classify_rows`, `ClassificationPolicy.enforce`, `DEFAULT_POLICY`. README: Concept notes 2 |
| 3 | Drafting architecture SOWs | `lab.py`, section "architecture SOW builder": `SowDoc`, `validate_sow`, `render_sow_markdown`, `sow_from_assessment`. README: Concept notes 3 |
| 4 | Preparing ROI presentations for CISO/executives | `lab.py`, section "ROI model for CISO / executives": `RoiInputs`, `compute_roi`, `sensitivity_table`, `risk_adjusted_range`, `render_exec_summary`. README: Concept notes 4 |
| 5 | Delivering User Acceptance Testing (UAT) runbooks | `lab.py`, section "UAT runbook generator": `generate_uat_cases`, `check_entry_criteria`, `run_uat_cases`, `evaluate_exit`, `render_runbook_markdown`. README: Concept notes 5 |
| 6 | Executing mock OAuth 2.0 / RBAC flows | `lab.py`, section "mock OAuth 2.0 (auth code + PKCE, client credentials) and RBAC": `MockIdP`, `pkce_challenge`, `ROLE_PERMISSIONS`, `effective_permissions`, `SecuredGateway`; 14 negative tests in `extension_demo`. README: Concept notes 6 |
| 7 | Constructing Hybrid RAG alongside secure Text-to-SQL for MS SQL databases | `lab.py`, sections "hybrid RAG" (`BM25`, `hashed_vector`, `rrf_fuse`, `HybridRetriever`) and "secure Text-to-SQL for SQL Server" (`SchemaCatalog`, `validate_tsql`, `tsql_to_sqlite`, `SecureTextToSql`, `route_question`, `HybridAnswerer`, `PyodbcExecutor`). README: Concept notes 7 |
| 8 | Implementing NeMo & Presidio guardrails | `lab.py`, section "Presidio-style analyzer/anonymizer and NeMo-style (Colang) rails": `AnalyzerEngine`, `AnonymizerEngine`, `COLANG`, `ColangRails`, `GuardedOmniGuard` (wired into the gateway), lazy `PresidioAdapter` and `NemoGuardrailsAdapter`. README: Concept notes 8 |
| 9 | Finalizing Dockerized FastAPI cloud deployments | `lab.py`, section "Dockerized FastAPI cloud deployment": `DeploySettings`, `render_dockerfile` + `lint_dockerfile`, `render_ecs_task_definition`, `render_compose`, `render_github_workflow`, `HealthState`, `GracefulShutdown`, `AppCore`, `create_app` (lazy FastAPI), `evaluate_deploy_checklist`. README: Concept notes 9 |

## Module 16: AuditMesh : Multi-Agent Compliance System

Code: `modules/16_capstone_auditmesh/lab.py` · Notes: `modules/16_capstone_auditmesh/README.md`

| # | Site topic | Where it is taught |
|---|---|---|
| 1 | Mapping 5-step manual compliance workflows | `lab.py`, section "mapping the 5-step manual compliance workflow": `ManualStep`, `WorkflowMap` (rejects anything but 5 ordered steps), `MANUAL_WORKFLOW`, `score_automation`, `render_workflow_markdown`. README: Concept notes 1 |
| 2 | Identifying Human-in-the-Loop bottlenecks | `lab.py`, section "human-in-the-loop bottleneck analysis": `QueueScenario`, `simulate_review_queue` (discrete-event simulation), `advise_review_capacity`. README: Concept notes 2 |
| 3 | Drafting latency/cost SLAs | `lab.py`, section "latency and cost SLAs with error-budget burn": `SlaSpec`, `SlaObjective`, `evaluate_sla`, `render_sla_markdown`. README: Concept notes 3 |
| 4 | Defining Model Context Protocol trust boundaries | `lab.py`, section "Model Context Protocol trust boundaries": `ServerBoundary`, `ToolPolicy`, `TrustBoundaryPolicy`, `TrustBoundaryEnforcer.check` / `verify_manifest`. README: Concept notes 4 |
| 5 | Leading operations and training handoffs | `lab.py`, section "operations and training handoff": `generate_handoff`, `readiness`, `render_handoff_markdown`, `TrainingModule`. README: Concept notes 5 |
| 6 | Developing a LangGraph Multi-Agent Supervisor | `lab.py`, section "LangGraph-style multi-agent supervisor": `SharedState`, `AuditAgents`, `LlmSupervisorRouter` + `legal_next`, `SupervisorGraph` (checkpoint, resume, human interrupt), lazy `build_langgraph`. Existing engine: `AuditEngine`, `consensus`, `AuditLog`. README: Concept notes 6 |
| 7 | Deploying a custom MCP server for secure Jira ticketing | `lab.py`, section "custom MCP server for secure Jira ticketing": `JiraMcpServer` (JSON-RPC `initialize`, `tools/list`, `tools/call`), `JiraBackend`, `McpClient`, lazy `build_fastmcp_adapter`. README: Concept notes 7 |
| 8 | Building comprehensive token cost/trace dashboards | `lab.py`, section "token cost and trace dashboards": `Tracer`, `TraceSpan`, `aggregate_spans`, `build_dashboard` (JSON), `render_dashboard_markdown`, `sparkline`. README: Concept notes 8 |
| 9 | Creating Streamlit/Gradio UIs for human approval workflows | `lab.py`, section "approval controller and Streamlit / Gradio UIs": `ApprovalController`, `submit_decision`, `build_streamlit_app`, `build_gradio_app` (lazy imports; the Streamlit view is smoke-tested offline with a stub module). README: Concept notes 9 |
