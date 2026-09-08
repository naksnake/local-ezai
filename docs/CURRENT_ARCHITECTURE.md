# Current Architecture — local-ezai

**Status:** Baseline assessment (as-is)
**Date:** 2026-08-14
**Author:** Principal Architect assessment
**Scope:** Everything on `main` as of commit `fab97ec`

---

## 1. What local-ezai is today

local-ezai is a **self-hosted AI chat + RAG stack**. It gives a single machine
(anything from a fanless Intel N97 mini-PC to a multi-GPU server) a private
ChatGPT-like service: chat UI, local LLM inference, embeddings, a vector
knowledge base with automatic retrieval, private web search, and a small set
of agent tools exposed over MCP.

It is an **assistant platform**, not an autonomous platform: every capability
is oriented around answering a human's chat message. There is no planning, no
multi-step task execution, no code awareness, and no sandboxed action-taking.

```
Browser ──► OpenWebUI ──► LiteLLM ──► vLLM / llama.cpp   (chat model)
                              │  └──► Embed Server        (embeddings)
                              └── auto-RAG hook ──► Qdrant
Agent tools (mcpo/MCP): filesystem · memory · fetch · qdrant-rag
Web search:             SearXNG (private metasearch)
Ops:                    Monitor dashboard (SSE, RBAC, RAG uploads)
```

---

## 2. Service catalog (8 runtime services)

All services share one Docker bridge network (`ai-net`) and address each other
by container name. All external configuration flows through `.env`
(`.env.example` is the schema).

| # | Service | Image / source | Port | Role |
|---|---------|----------------|------|------|
| 1 | **openwebui** | `ghcr.io/open-webui/open-webui:main` | 3000 | Chat UI; talks OpenAI API to LiteLLM; web-search toggle wired to SearXNG; MCP tool servers auto-registered at boot via `TOOL_SERVER_CONNECTIONS` |
| 2 | **litellm** | `ghcr.io/berriai/litellm:main-latest` | 4000 | OpenAI-compatible proxy; routes chat → inference engine and embeddings → embed-server; hosts the **auto-RAG pre-call hook** (`config/litellm_custom_callbacks.py`) |
| 3 | **vllm** | `vllm/vllm-openai` (GPU), `vllm-openai-cpu` (CPU), `ghcr.io/ggml-org/llama.cpp:server[-vulkan]` (N97/iGPU) | 8000 | LLM inference. The service *name* stays `vllm` in every profile so routing never changes (engine-agnostic slot) |
| 4 | **embed-server** | local build — FastAPI + sentence-transformers (`embed-server/server.py`) | 8001 | OpenAI-compatible `/v1/embeddings`; CPU-only by design so it never competes for VRAM; model swappable via `CPU_EMBED_MODEL` |
| 5 | **qdrant** | `qdrant/qdrant:latest` | 6333 | Vector DB for the knowledge base (REST only; gRPC deliberately unexposed) |
| 6 | **searxng** | `searxng/searxng:latest` | 8092 | Private metasearch; JSON API enabled for agentic search |
| 7 | **mcpo** | local build (`mcpo/Dockerfile`) | 8200 | MCP→OpenAPI proxy exposing 4 MCP servers as bearer-authenticated REST tools |
| 8 | **monitor** | local build — FastAPI + SSE (`monitor/monitor.py`, 813 lines) | 8888 | Live health dashboard; RAG upload/list/delete UI; HTTP Basic RBAC (admin/viewer) + Bearer machine credential |

### Deployment profiles (same topology, different engine)

| Profile | Compose files | Engine | Default model |
|---|---|---|---|
| GPU | `docker-compose.yml` | vLLM CUDA | Qwen2.5-7B-Instruct |
| CPU (vLLM) | `+ docker-compose.cpu.yml` | vLLM CPU (bf16) | Qwen2.5-1.5B-Instruct |
| N97 / low-power | `+ docker-compose.n97.yml` | llama.cpp (GGUF Q4_K_M) | Qwen2.5-1.5B (Coder-1.5B and 3B pre-wired) |
| N97 iGPU | `+ docker-compose.n97-igpu.yml` | llama.cpp + Vulkan | same, ~2-3× faster prefill |
| Kubernetes | `k8s/*.yaml` (K3s) | partial stack (OpenWebUI, LiteLLM, Qdrant, Ingress) | n/a |
| HPC batch | `slurm/*.sh` | batch embedding jobs only | n/a |

Profile overrides use the Compose `!override` YAML tag to *replace* the GPU
`deploy` block (memory caps per service on 16 GB boxes), and swap the LiteLLM
config file per profile (`config/litellm-config{,.n97,.cpu}.yaml`).

---

## 3. Key mechanisms (the parts worth preserving)

### 3.1 Engine-agnostic inference slot
The inference container is always named `vllm` and always serves the
OpenAI-compatible API on `:8000`, whether it is actually vLLM-CUDA, vLLM-CPU,
or llama.cpp. Consumers (LiteLLM, monitor, health check) never change when
hardware changes. **This is the platform's strongest architectural invariant**
and the reason a knowledge base built on an N97 works identically on an HGX
server.

### 3.2 Proxy-injected RAG (auto-RAG hook)
`config/litellm_custom_callbacks.py` registers an `async_pre_call_hook` on the
LiteLLM proxy. On every chat completion it:

1. Strips OpenWebUI's built-in knowledge tools (they query OpenWebUI's own
   empty store and confuse small models).
2. Embeds the last user message, searches Qdrant
   (`RAG_TOP_K`, `RAG_MIN_SCORE`), and prepends matching excerpts as system
   context with source attributions.

Properties: applies to **every client** with zero per-client setup, and
**fails open** — if Qdrant/embed-server are down, chat proceeds without
context. This is the right failure mode for retrieval; it will be exactly the
*wrong* default for action-taking tools (see GAP_ANALYSIS §5).

### 3.3 MCP tool plane (mcpo)
`config/mcpo-config.json` declares four MCP servers, proxied as REST by mcpo:

| Tool | Implementation | Capability |
|---|---|---|
| `filesystem` | `@modelcontextprotocol/server-filesystem` | read/write **only** `./documents` |
| `memory` | `@modelcontextprotocol/server-memory` | JSON knowledge graph persisted in a named volume |
| `fetch` | `mcp-server-fetch` (PyPI) | fetch any public web page |
| `qdrant-rag` | vendored Node server (`mcp-servers/qdrant-rag/index.js`) | `search_knowledge_base`, `list_collections` |

`mcpo/entrypoint.sh` renders `${VAR:-default}` placeholders into the config
(mcpo itself does no substitution). All versions are pinned after a live
incident (mcp 2.0.0 broke mcpo 0.0.20). OpenWebUI auto-registers all four
tool servers at boot through the `TOOL_SERVER_CONNECTIONS` environment JSON.

### 3.4 Ingestion pipeline
Two equivalent ingestion paths write the same Qdrant payload shape
(`source`, `source_path`, `chunk_index`, `text`):

- `scripts/embed_documents.py` (via `make embed`, runs in a throwaway Docker
  container): word-window chunking (400 words / 50 overlap), batch embedding,
  dimension-mismatch guard when the embed model changed.
- `monitor.py /api/rag/upload`: browser upload (PDF/MD/TXT/CSV/LOG, ≤20 MB),
  same chunking, plus list/delete per source file.

### 3.5 Operations layer
- `Makefile` is the operator UX: per-profile `setup-*` one-shots
  (pull → build → download → up → wait-ready), `health`, `bench`, `embed`,
  `logs-%`, resets.
- `scripts/check-ports.sh` auto-relocates occupied host ports and persists
  the choice to `.env` before every `up`.
- `scripts/health-check.sh` verifies all 8 services (uses the monitor's
  Bearer machine credential).
- Model downloads run inside throwaway containers (`python:3.11-slim` +
  `hf download`) — **no host Python required anywhere**; images run with
  `HF_HUB_OFFLINE=1` against the shared read-only model cache.

### 3.6 Security model (current)
- Secrets in `.env` (never committed): `LITELLM_MASTER_KEY` (LiteLLM +
  OpenWebUI), `WEBUI_SECRET_KEY`, `MCP_API_KEY` (mcpo + monitor machine
  credential), `SEARXNG_SECRET`, monitor role passwords.
- Monitor RBAC: `admin` / `viewer` HTTP Basic roles + Bearer machine token;
  constant-time comparisons; `MONITOR_AUTH=false` escape hatch for labs.
- Trust boundary is essentially **the LAN**: vLLM, embed-server, Qdrant, and
  SearXNG publish ports with no auth. Acceptable for a private lab; documented
  as such.

---

## 4. Data & configuration model

| Store | Technology | Contents | Persistence |
|---|---|---|---|
| Knowledge base | Qdrant collection (`RAG_COLLECTION`, default `my-knowledge-base`) | document chunks + payload | named volume `qdrant-data` |
| Chat history / users | OpenWebUI SQLite | accounts, chats, UI-registered tools | named volume `openwebui-data` |
| Agent memory | JSON knowledge graph | entities/relations from the `memory` MCP tool | named volume `mcpo-memory` |
| Documents | host bind mount `./documents` | source files (uploads land here too, so the filesystem tool sees them) | host FS |
| Models | host bind mount `./models` | HF cache + GGUF files, mounted read-only | host FS |
| Compile cache | named volume `vllm-cache` | torch.compile artifacts (fast restarts) | volume |

Configuration is a single `.env` layered over defaults in the Makefile and
compose files. There is **one rule with data integrity implications**:
changing the embedding model requires a new collection name (vector-dimension
incompatibility is guarded in `embed_documents.py`).

---

## 5. Current capabilities — summary

What the platform can do today:

1. **Chat** with a local LLM through a polished multi-user UI (OpenAI API
   surface end to end).
2. **Automatic RAG** over user documents at the proxy layer — zero client
   setup, source-cited answers, tunable top-k/threshold.
3. **Document ingestion** via CLI/Make, browser upload, or Slurm batch job.
4. **Agentic tool use in chat** (single-turn, model-driven): file read/write
   in `./documents`, persistent knowledge-graph memory, web fetch, semantic
   KB search — via MCP with tool-call parsing enabled in the engine
   (`--enable-auto-tool-choice --tool-call-parser hermes`).
5. **Private web search**: passive (globe toggle retrieval) and active (model
   calls the `web_search` tool when it decides it needs live data), with a
   ready-made proactive-search system prompt.
6. **Multi-hardware portability**: N97-class CPU → iGPU → CPU vLLM → CUDA GPU
   with one command each and no reconfiguration; multilingual embedding
   options documented.
7. **Operations**: live dashboard w/ RBAC, health checks, benchmarking, port
   auto-resolution, factory resets, pinned reproducible images.

## 6. What the platform is *not* (today)

Stated plainly, because the target state depends on it:

- **No agent runtime.** The only "loop" is OpenWebUI's built-in single-chat
  tool-calling. Nothing plans, decomposes, retries, or verifies.
- **No code awareness.** The filesystem tool sees `./documents` only; there is
  no repo checkout, no code search, no AST/symbol index, no diff/patch model.
- **No execution.** No shell, no test runner, no builds — the model cannot
  *do* anything except read/write documents and fetch URLs.
- **No sandbox.** Consequently no isolation model for actions either.
- **No git integration.** No branches, commits, PRs, or review flows.
- **No workflow state.** Nothing persists task state; a container restart
  forgets everything except chat logs and vectors.
- **No permission system for actions.** Tools are all-or-nothing behind one
  bearer key.
- **No project memory convention.** There is no `CLAUDE.md` / `.agent/`
  context for agents working *on this repo* (this assessment introduces
  `.agent/`).
- **No tests or CI** for the platform's own code (the Python/Node services
  have zero automated tests).

## 7. Quality-attribute assessment

| Attribute | Rating | Evidence |
|---|---|---|
| Portability | ★★★★★ | 4 hardware profiles, engine-agnostic slot, multi-arch notes for Grace/ARM |
| Operability | ★★★★☆ | Make one-shots, health checks, port auto-fix, dashboard; no metrics/log aggregation |
| Reproducibility | ★★★★☆ | pinned images/deps after real breakage; models cached offline; `:latest`/`:main` tags on 4 upstream images remain a risk |
| Security | ★★☆☆☆ | LAN-trust model; several unauthenticated ports; fine for labs, not for autonomous action-taking |
| Extensibility (tools) | ★★★★☆ | MCP-first tool plane; adding an MCP server = one JSON block |
| Extensibility (behavior) | ★★☆☆☆ | behavior lives in a chat UI + one proxy hook; no place to put an agent loop |
| Testability | ★☆☆☆☆ | no automated tests anywhere in the repo |
| Autonomy | ☆☆☆☆☆ | none — by design, until now |

## 8. Architectural strengths to build on

1. **OpenAI-compatible seams everywhere** — any new agent runtime can consume
   models through LiteLLM untouched.
2. **The engine-agnostic inference slot** — model/hardware tiering already
   works; a "planner model + coder model" split is a LiteLLM config change.
3. **MCP as the tool protocol** — the tool plane the target platform needs
   already speaks the right protocol; it needs scoping and policy, not
   replacement.
4. **Qdrant + embed pipeline** — the memory substrate exists; code-aware
   indexing is an extension, not a rewrite.
5. **Profile/override discipline** — new services can ship as an additive
   compose overlay without touching the existing runtime (a hard requirement
   of this transformation).
6. **Ops maturity for its size** — health, RBAC, port hygiene, pinning: the
   habits needed for running autonomous workloads are already present in
   miniature.

---

*Companion documents:*
- Where we are going: [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md)
- What's missing and why: [GAP_ANALYSIS.md](GAP_ANALYSIS.md)
- How we get there: [MIGRATION_PLAN.md](MIGRATION_PLAN.md)
- Who does the work: [AGENT_DESIGN.md](AGENT_DESIGN.md)
- How work flows: [WORKFLOW_DESIGN.md](WORKFLOW_DESIGN.md)
