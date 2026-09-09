# local-ezai

A complete self-hosted AI stack. Runs a chat interface, local LLM, embedding model, vector database, private web search, and RAG agent tools — entirely on your own hardware. No cloud API costs, no data leaves your machine.

```
Browser → OpenWebUI → LiteLLM → vLLM  (Qwen2.5-7B, GPU)
                              → Embed Server (nomic-embed, CPU)
                                         ↓
Agent tools (MCP):  filesystem · memory · web fetch · RAG search
Vector DB:          Qdrant
Web search:         SearXNG (private)
Monitor:            Real-time dashboard (http://localhost:8888)
```

---

## Autonomous SWE runtime (new)

Beyond chat, local-ezai now ships **agentd** — a minimum viable autonomous
software engineer that uses the stack's local models to plan a change, edit
a git repository on an isolated branch, run its tests, and commit the
result:

```bash
make swe-install && . .venv-agentd/bin/activate
cd ~/code/myapp
local-ezai run "fix the failing date parser and add a test"
local-ezai sprint sprint28.md     # or run a whole markdown spec
```

`local-ezai` works on Linux, macOS, and Windows (chat · plan · run · code ·
test · fix · review · commit · memory · sprint). It is fully additive — the
chat stack above is unchanged. Guide: **[agentd/README.md](agentd/README.md)**
· install: **[agentd/INSTALL.md](agentd/INSTALL.md)** · architecture:
**[docs/TARGET_ARCHITECTURE.md](docs/TARGET_ARCHITECTURE.md)**.

---

## Requirements

The same stack runs on anything from a fanless mini-PC to a GPU server —
you only swap the inference profile and the model:

| Hardware tier | One-command setup | Engine | Chat model (default) | Example machines |
|---|---|---|---|---|
| Low-power x86 CPU (±16 GB RAM) | `make setup-n97` | llama.cpp | Qwen2.5-1.5B 4-bit GGUF | Intel N97/N100 mini-PCs |
| Same, using the Intel iGPU | `make setup-n97` then `make up-n97-igpu` | llama.cpp + Vulkan | same, ~2-3× faster prompt processing | Alder Lake-N UHD graphics |
| Any x86 CPU with AVX2 | `make setup-cpu` | vLLM (CPU) | Qwen2.5-1.5B bf16 | when you specifically need vLLM |
| NVIDIA GPU, x86 or ARM | `make setup-gpu` | vLLM (CUDA) | Qwen2.5-7B (or far larger) | RTX workstation → HGX/MGX-class servers |

The inference service always answers on the same internal address with the
same OpenAI-compatible API, whichever engine backs it — so switching
hardware later means running a different `setup-*` command, not
reconfiguring the stack. Your knowledge base, accounts and settings carry
over untouched.

OS: Ubuntu 24.04/26.04 LTS (any Linux with Docker ≥ 24 and Compose ≥ 2.24.4
works). Disk: ~30 GB for the small profiles, 200 GB+ for large GPU models.

---

## Quick start — example: Intel N97 mini-PC with iGPU

This walks a fresh machine to a working chat + RAG service with a
1.5-billion-parameter model. Only Docker is required on the host — model
downloads and helper scripts all run in containers.

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/local-ezai.git
cd local-ezai

# 2. No Docker yet? This installs it (safe to skip otherwise):
bash scripts/setup.sh

# 3. Configure — one command, one edit
./install.sh
#    detects the hardware (here: a low-power CPU class), creates .env with
#    fresh secrets and the runtime for this machine, and opens it ONCE:
#      set:  REASONING_MODEL=auto  CODING_MODEL=auto  CHAT_MODEL=auto
#            (the platform picks GGUF models that fit this box; or name
#             your own hf: / gguf: sources)
#      set:  LAN_HOST=<this machine's IP>   (needed for browser-side tools)
#    Problems are printed with their fix before anything is downloaded.
#    Re-running later repairs .env and never overwrites your values.

# 4. Everything else in one command: the model seeds are downloaded,
#    validated and benchmarked into generation 1, images pulled and built,
#    all services started and waited for, a chat turn / RAG / plan smoke
#    run, and the URLs printed (~15-30 min first time). Re-run any time.
make setup-n97          # `make setup` detects the class instead of asserting it
#    Occupied ports are relocated automatically and saved to .env.

# 5. Optional: run inference on the Intel iGPU instead of the CPU
#    (~2-3x faster prompt processing, frees the CPU cores)
make up-n97-igpu
```

Then:

1. **Chat** — open `http://<LAN_HOST>:3000`, sign up (the first account
   becomes admin), pick your model in the selector, and talk to it.
2. **RAG** — open the monitor `http://<LAN_HOST>:8888` (log in as `admin`),
   upload a PDF/MD/TXT in the *Knowledge Base* bar, then ask about its
   content in any chat. Answers cite the source file automatically — RAG is
   injected at the LiteLLM proxy, so no per-chat setup is needed.
3. **Web search** — toggle the globe icon in the chat box to let answers
   use live web results via the bundled private SearXNG.

Useful afterwards: `make health` (all-service check), `make bench`
(tokens/sec), `make logs-vllm` (inference logs), `make reset-webui` /
`make reset-password` (account recovery).

---

## Service map

URLs below show the default ports. Every published port can be changed in
`.env` (e.g. `SEARXNG_PORT=8095` if 8092 is taken) — see the *Host ports*
section of `.env.example`.

| Service | URL | Auth |
|---------|-----|------|
| **OpenWebUI** — chat interface | http://localhost:3000 | create account on first visit |
| **Monitor** — live dashboard | http://localhost:8888 | none |
| **LiteLLM** — model proxy API | http://localhost:4000 | `LITELLM_MASTER_KEY` from `.env` |
| **vLLM** — LLM inference | http://localhost:8000 | none |
| **Embed Server** — embedding API | http://localhost:8001 | none |
| **Qdrant** — vector database | http://localhost:6333 | none |
| **SearXNG** — private web search | http://localhost:8092 | none |
| **mcpo** — MCP tools proxy (filesystem, memory, fetch, knowledge base, `/swe` SWE tools) | http://localhost:8200 | `MCP_API_KEY` from `.env` |
| **ezaid** — platform control plane (optional: `make control-up`) | http://localhost:8010 | `EZAI_CONTROL_TOKEN` from `.env` (bearer; `/health` open) |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  BROWSER                                                      │
│  OpenWebUI  :3000          Monitor dashboard  :8888          │
└──────────────┬───────────────────────────────────────────────┘
               │ OpenAI-compatible API
┌──────────────▼───────────────────────────────────────────────┐
│  ROUTING                                                      │
│  LiteLLM proxy  :4000                                        │
└──────┬───────────────────────────┬───────────────────────────┘
       │ text completions          │ embeddings
┌──────▼──────────┐     ┌──────────▼──────────────────────────┐
│  vLLM  :8000    │     │  Embed Server  :8001                │
│  Qwen2.5-7B     │     │  nomic-embed-text-v1.5              │
│  (GPU)          │     │  (CPU)                              │
└─────────────────┘     └───────────────────┬─────────────────┘
                                            │ vectors
┌───────────────────────────────────────────▼─────────────────┐
│  AGENT TOOLS — mcpo MCP proxy  :8200                        │
│  ├── filesystem  → ./documents  (read/write files)          │
│  ├── memory      → knowledge graph (persistent across chats)│
│  ├── fetch       → any public web page                      │
│  └── qdrant-rag  → semantic search over your documents      │
└─────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  DATA                                                        │
│  Qdrant  :6333     SearXNG  :8092                           │
└─────────────────────────────────────────────────────────────┘
```

---

## Repository layout

```
local-ezai/
├── docker-compose.yml          GPU stack (8 services)
├── docker-compose.n97.yml      CPU override: llama.cpp for N97/low-power boxes
├── docker-compose.cpu.yml      CPU override: real vLLM engine (vllm-openai-cpu)
├── .env.example                All configuration variables
├── Makefile                    Management commands
│
├── docs/
│   └── DEPLOY-N97.md           Deployment guide for Intel N97 / no-GPU boxes
│
├── config/
│   ├── models/                 Model registry + generations (written by `make bootstrap` / local-ezai)
│   ├── rendered/               GENERATED LiteLLM config + engine override per generation — never hand-edited
│   ├── governance/             Change-request queue + append-only audit log
│   ├── mcpo-config.json        MCP server list (reads from env)
│   ├── providers/              Runtime descriptors (llama.cpp, vLLM) — data for the V1 renderer
│   ├── searxng/settings.yml    Search engine config
│   └── prompts/
│       ├── web-search-assistant.md   System prompt for proactive web search
│       └── orchestrator.md           System preset of the Local-EZAI Orchestrator persona (make orchestrator)
│
├── embed-server/               Embedding API (FastAPI + sentence-transformers)
│   ├── Dockerfile
│   └── server.py
│
├── monitor/                    Admin Center: live dashboard (FastAPI + SSE) + platform console pages
│   ├── Dockerfile
│   ├── monitor.py              Health & knowledge dashboard, RBAC, RAG upload API
│   └── admin_center.py         /overview and /runs pages over the ezaid control plane (V1 P4)
│
├── mcpo/                       MCP tool proxy container
│   └── Dockerfile
│
├── mcp-servers/
│   └── qdrant-rag/             Custom RAG MCP server (Node.js)
│       ├── package.json
│       └── index.js
│
├── scripts/
│   ├── setup.sh                First-time system setup
│   ├── download-models.sh      Download HuggingFace models (GPU stack)
│   ├── download-models-n97.sh  Download quantized GGUF set (N97/CPU stack)
│   ├── health-check.sh         Check all 8 services
│   └── embed_documents.py      Ingest documents into Qdrant
│
├── tools/
│   ├── knowledge-base-search.py   OpenWebUI Python tool (paste into UI)
│   └── web-search.py              OpenWebUI Python tool — SearXNG web search
│
├── k8s/                        Kubernetes manifests (K3s)
│   ├── namespace.yaml
│   ├── qdrant.yaml
│   ├── openwebui.yaml
│   ├── litellm.yaml
│   └── ingress.yaml
│
└── slurm/                      HPC batch job scripts
    ├── setup-slurm.sh
    ├── embed-job.sh
    └── test-job.sh
```

---

## Configuration reference

All settings live in `.env`. Copy `.env.example` and edit before first launch.

```bash
cp .env.example .env
```

| Variable | Default | Notes |
|----------|---------|-------|
| `LITELLM_MASTER_KEY` | `sk-ai-service-2024` | ⚠️ Change this — API key for LiteLLM and OpenWebUI |
| `WEBUI_SECRET_KEY` | `change-this-to-a-random-string` | ⚠️ Change this — session signing key |
| `MCP_API_KEY` | `local-tools-key` | Key for the mcpo MCP proxy |
| `SEARXNG_SECRET` | `searxng-local-secret-change-this` | ⚠️ Change this — SearXNG HMAC key |
| `CHAT_MODEL` | `Qwen/Qwen2.5-7B-Instruct` | HuggingFace model ID |
| `CHAT_MODEL_NAME` | `qwen2.5-7b` | Short name used in API calls |
| `MAX_MODEL_LEN` | `4096` | Context window size in tokens |
| `GPU_MEMORY_UTILIZATION` | `0.85` | Fraction of VRAM to use (lower if OOM) |
| `MODELS_DIR` | `./models/hf-cache` | Host path where models are cached |
| `DOCUMENTS_DIR` | `./documents` | Host path for `make embed` to read from |
| `RAG_COLLECTION` | `my-knowledge-base` | Qdrant collection name for RAG |

Generate secure random values for the secret keys:
```bash
openssl rand -hex 32
```

---

## Make commands

```
make help        List all commands
make setup       First run, all steps: ./install.sh (edit .env once) → local-ezai setup (bootstrap, images, up, smoke, report)
make setup-system  System packages for a fresh Ubuntu host (Docker, NVIDIA toolkit, Python venv, Node)
make setup-offline  First run on an air-gapped host from a bundle (BUNDLE=<dir>), nothing downloaded
make bundle      Offline bundle of this bootstrapped platform: images + weights + seeds (BUNDLE=<dir>)
make install     First run, steps 1–3 only: detect hardware, create/repair .env with minted secrets, validate the seeds
make build       Build embed-server, mcpo, and monitor images
make pull        Pull official Docker images
make setup-gpu   First run asserting an accelerator (install.sh --profile gpu → local-ezai setup --profile gpu)
make up          Start all 8 services with the rendered engine (GPU profile; fetches the embedding model if missing)
make download-embed  Download the RAG embedding model (every profile; skipped when present)
make download-gpu  Legacy: download the .env CHAT_MODEL + embedding model for the GPU stack
make setup-n97   First run asserting the low-power class (install.sh --profile n97 → local-ezai setup --profile n97)
make up-n97      Start CPU-only via llama.cpp (fetches the embedding model if missing)
make up-n97-igpu Same, but llama.cpp runs on the Intel iGPU (Vulkan; faster prefill)
make pull-n97    Pull images for the N97 stack (llama.cpp)
make download-n97  Download the small quantized model set (~2.5 GB)
make update-n97  Pull latest images and restart the N97 stack
make setup-cpu   First run asserting the cpu-standard class (install.sh --profile cpu → local-ezai setup --profile cpu)
make up-cpu      Start CPU-only via vLLM (fetches the embedding model if missing)
make pull-cpu    Pull images for the vLLM CPU stack
make download-cpu  Download models for the vLLM CPU stack (~3.6 GB, runs in Docker)
make update-cpu  Pull latest images and restart the vLLM CPU stack
make wait-ready  Wait for the LLM to finish loading, then run the health check
make reset-webui Factory-reset OpenWebUI (deletes all users, chats, settings)
make reset-password  Reset an OpenWebUI password (EMAIL=... PASSWORD=..., EMAIL=all for everyone)
make down        Stop all services
make restart     Restart all services
make logs        Tail logs from all services
make logs-vllm   Tail logs from a specific service (any name after logs-)
make health      Check health of all 8 services
make bench       One-question LLM benchmark (prompt & generation tokens/sec)
make status      Show container status table
make embed       Ingest ./documents into the Qdrant knowledge base
make install-autorag  (optional) install the in-OpenWebUI RAG filter — RAG
                 already works via the LiteLLM hook without this
make orchestrator  Install/refresh the Local-EZAI Orchestrator persona in OpenWebUI (after first login)
make monitor     Open the Admin Center (monitor) in your browser — health & knowledge at /, /overview, /runs
make control-up  Start the ezaid control plane overlay (:8010; V1 P2, optional)
make control-down / control-logs / control-spec  Stop it · follow logs · regenerate docs/api/ezaid-openapi.json
make control-serve  Run ezaid on this host instead (sees your repos → SWE runs through the API)
make swe-drill   Chat-ops boundary drill (offline): governance unreachable from chat, prompt-injection red-team, chat/RAG byte-identical
make swe-accept  First-run acceptance suite F1–F11 (offline)
make swe-parity  Parity harness (release gate): every management operation via CLI-direct, CLI-connected and the API — same result, state, audit (offline)
make swe-gates   Agnosticism gates: the third-runtime drill (a mock runtime from descriptor data alone), the H1 word audit, the H2–H4 class fixtures (offline)
make release-gate  The P6 release gate in one command: lint · chat-stack baseline · drill · acceptance · parity · agnosticism gates · full suite
make update      Pull latest images and restart
make k8s         Deploy to K3s Kubernetes
make clean       Remove all containers, images, volumes (destructive)
```

---

## Step-by-step setup

### 1. System setup

```bash
bash scripts/setup.sh
```

Installs Docker CE, NVIDIA Container Toolkit, Python 3 virtualenv, and Node.js 22.
Supports Ubuntu 24.04 and 26.04 LTS.

> If NVIDIA drivers were installed, the script exits and asks you to reboot.  
> After rebooting, run `bash scripts/setup.sh` again to finish.

### 2. Configure — `.env`, once

```bash
./install.sh          # or: make install
```

Detects your hardware (class, not brand), creates `.env` from
`.env.example` with fresh secrets (`LITELLM_MASTER_KEY`, `WEBUI_SECRET_KEY`,
`MCP_API_KEY`, `EZAI_CONTROL_TOKEN`, `SEARXNG_SECRET`, the two monitor
passwords) and the runtime that fits the machine, then opens it **once** so
you can set the model seeds (`REASONING_MODEL` / `CODING_MODEL` /
`CHAT_MODEL` — `auto` lets the platform pick models that fit, or name a
`hf:` / `gguf:` source) and `LAN_HOST` if other devices will use the UI.
Every problem with the seeds is printed with its fix before anything is
downloaded. Re-running the script later *repairs* `.env` (missing or
placeholder secrets, new keys) and never overwrites your values — a backup
is written first. `./install.sh --yes` skips the edit stop, `--check` writes
nothing, `--profile n97` asserts the low-power class.

By hand instead: `cp .env.example .env` and change the values marked ⚠️.

### 3. Bring the platform up — one command

```bash
make setup            # or make setup-gpu / setup-cpu / setup-n97 to assert a class
```

`make setup` runs `./install.sh` again (a no-op once `.env` is complete) and
then `local-ezai setup`, which:

1. **bootstraps** the model seeds into generation 1 — download (resumable,
   checksummed), load-validation, benchmark, the one implicit activation,
   the rendered LiteLLM + engine configuration (`make bootstrap` on its own);
2. **pulls and builds** the images with the rendered engine override, and
   downloads the RAG embedding model (~500 MB) if missing;
3. **starts** all 8 services and **waits** until the engine answers its
   readiness probe and every service is healthy (the engine takes 1–5
   minutes to load a model);
4. **smoke-tests** the platform: one chat turn (required), a RAG answer over
   a sample document, a plan against the bundled sample project, the
   model probes of every role (the last three are advisory);
5. **prints the URLs**, writes `config/first-run/report.md` and shows the
   "Platform ready" card in the WebUI.

Every step is idempotent — re-run `make setup` after a failure or an
interruption and it continues where it stopped. Step by step instead:
`make bootstrap`, then `make up` (or `up-n97` / `up-cpu`), `make wait-ready`,
`make health`. No model seeds in `.env` yet? `local-ezai init` proposes the
catalog's recommended set for your hardware and continues with the same
pipeline.

Models are stored in `./models` (inside the project folder) and mounted
read-only into the containers. For **gated models** (Llama, Gemma): get a
token at https://huggingface.co/settings/tokens and add
`HF_TOKEN=hf_your_token` to `.env`.

**Air-gapped host?** On a connected machine that already ran `make setup`:
`make bundle BUNDLE=/media/usb/local-ezai-bundle` saves the images, the
model weights and the seeds of its generation. On the offline machine:
`./install.sh --offline /media/usb/local-ezai-bundle && make setup-offline
BUNDLE=/media/usb/local-ezai-bundle` — the same steps, nothing downloaded
(the bundle is a directory; `tar` it for transport; Docker and Python 3 are
still needed on the host).

### 4. Check

```bash
make health           # all 8 services
local-ezai status     # generation, models, approvals, health
```

### 5. First login

1. Open **http://localhost:3000** — the "Platform ready" banner shows the
   groups, the models that fill them, the smoke results and the links
2. Click **Sign up** → create your admin account
3. Pick a model in the chat dropdown — the role entries (`role-chat`, …) or
   the served model name
4. `make orchestrator` — installs the **Local-EZAI Orchestrator** persona
   (plans and starts autonomous engineering work on projects you registered
   with `local-ezai project add <path>`; needs the control plane:
   `make control-up` or `make control-serve`). Pick it in the dropdown.
5. Open the **Admin Center** at `http://localhost:8888/overview` (monitor
   login `admin` / `viewer`): platform health, roles → models, the
   governance queue, every run with its report at `/runs`, the model
   lifecycle at `/models` (install · benchmark · activate · rollback), the
   routing explanation at `/routing`, the engine slot at `/runtime`, the
   approval queue at `/governance` (evidence next to every decision;
   admins approve or reject there), sprints and evolution cycles at
   `/sprints` and `/evolution` (admins start them from the page), each
   project's memory at `/memory`, and the allowlist at `/projects`. Signed
   in to the WebUI on the same host? The console recognises that session
   (a WebUI admin is a console admin); the monitor login stays the fallback,
   and a reverse proxy can hand over an identity header instead
   (`MONITOR_SSO_*` in `.env.example`). The five zero-CLI walkthroughs of
   the console are its own Browser QA workflows
   (`agentd/examples/browser-qa.admin-center.yaml`).

### 6. Connect MCP agent tools

The mcpo tool servers are pre-registered at boot (Admin Panel → Settings →
Tools lists them; set `LAN_HOST` in `.env` when you use the UI from other
devices). To add them by hand instead:

1. In OpenWebUI: **Admin Panel → Settings → Tools**
2. Add a new tool server:
   - URL: `http://localhost:8200`
   - API Key: value of `MCP_API_KEY` from your `.env`
3. Enable the **🔧 wrench** icon in any chat to give the AI access to:
   - `filesystem` — read and write `./documents`
   - `memory` — persistent knowledge graph across sessions
   - `fetch` — retrieve any web page
   - `search_knowledge_base` — semantic search over your embedded documents
   - `swe` — the Local-EZAI SWE tools (plan / run / sprint / fix / evolve,
     status, reports; enabled by default for the Orchestrator persona only)

### 7. Add documents to the knowledge base

```bash
# Put .txt or .md files in ./documents, then:
make embed
```

The script chunks, embeds, and stores everything in Qdrant. Re-run whenever you add new documents. Progress is printed per file.

You can also use the **knowledge-base-search** OpenWebUI tool:
1. Admin Panel → Tools → + New Tool
2. Paste the contents of `tools/knowledge-base-search.py`
3. Save — the AI can now search your docs from any chat

---

## Monitor dashboard

**http://localhost:8888**

Shows live status for all 8 services:
- Green/red status badge per service
- Response time in milliseconds
- Consecutive failure count
- 20-point sparkline of response history
- Direct link to each service's UI
- Knowledge Base bar: upload documents (PDF/MD/TXT/CSV/LOG) into the RAG
  database, see every stored file with its chunk count, remove files with ✕

Updates automatically via Server-Sent Events (SSE) — no manual refresh needed.

```bash
make monitor   # opens the dashboard in your browser
```

### Access control (RBAC)

The dashboard asks for a login (HTTP Basic). Two roles, passwords set in `.env`:

| Login | Password from | Can do |
|---|---|---|
| `admin` | `MONITOR_ADMIN_PASSWORD` | everything, including RAG upload/remove |
| `viewer` | `MONITOR_VIEWER_PASSWORD` | read-only: status cards + file listing |

Scripts and other dashboards authenticate with a header instead:
`Authorization: Bearer <MCP_API_KEY>` (admin-equivalent). The health check
uses this automatically. Set `MONITOR_AUTH=false` in `.env` to disable the
login entirely (not recommended beyond an isolated lab).

---

## Adding & switching AI models

Every profile follows the same pattern: **tell `.env` which model, download
it, restart the inference service**. Nothing else in the stack changes.

**N97 / llama.cpp profile** — models are single 4-bit GGUF files. Example:
switch from the default 1.5B to the higher-quality 3B (non-commercial
license), or any other GGUF on HuggingFace:

```bash
# .env
N97_GGUF_REPO=Qwen/Qwen2.5-3B-Instruct-GGUF
N97_MODEL_FILE=qwen2.5-3b-instruct-q4_k_m.gguf
N97_MODEL_NAME=qwen2.5-3b

make download-n97
docker compose -f docker-compose.yml -f docker-compose.n97.yml up -d vllm
# pick the new name in OpenWebUI's model selector
```

A code-specialist variant is also pre-wired — **Qwen2.5-Coder-1.5B**
(Apache-2.0), better at writing and explaining code than the chat model at
the same speed; uncomment its block in `.env.example`'s N97 section the
same way. LiteLLM routing is **rendered from the model registry** (V1) — a
new model becomes routable the moment it is installed and activated; no
config file to edit. Keep ~2 GB headroom under the 6 GB memory cap; Q4_K_M
quantizations of 1-4B models fit comfortably.

**Day-2 model changes (V1, any profile)** — models are lifecycle-managed;
`.env` is read once by `make bootstrap`:

```bash
local-ezai model install hf:mistralai/Mistral-7B-Instruct-v0.3 --name mistral-7b
local-ezai model benchmark mistral-7b            # tokens/sec on this box
local-ezai model activate mistral-7b --group chat   # → change request
local-ezai governance approve cr-0001            # renders + reloads generation N+1
local-ezai model rollback                        # if you regret it
```

`model install` also takes `gguf:<url|hf://org/repo/file.gguf|path>`, a
catalog id (`local-ezai model catalog`), or `auto`. The legacy `.env`
families (`CHAT_MODEL`/`CHAT_MODEL_NAME`, `CPU_*`, `N97_*`) are migrated into
generation 1 automatically by the first `make bootstrap`, keeping your
served model name so existing chats keep working.

---

## Switching the RAG embedding model

The embedding model turns your documents (and questions) into vectors for
the knowledge-base search. It is independent of the chat model — switching
it changes *how well documents are found*, not how answers are written.

**The one rule:** vectors from different embedding models are incompatible.
Always pair a new embedding model with a **new collection name**, then
re-upload / re-embed your documents. Never mix models in one collection.

Recommended options (all run on the CPU embed-server, any profile):

| Model (`CPU_EMBED_MODEL`) | Languages | Size | Notes |
|---|---|---|---|
| `nomic-ai/nomic-embed-text-v1.5` (default) | English-optimised | ~0.5 GB | fast, great English retrieval |
| `intfloat/multilingual-e5-small` | 100+ languages | ~0.5 GB | best multilingual pick for N97-class CPUs |
| `BAAI/bge-m3` | 100+ languages | ~2.3 GB | strongest multilingual quality; heavy — raise the embed-server memory cap on 16 GB boxes and expect slower embedding |

Switch procedure (example: multilingual e5 on the N97 profile):

```bash
# 1. .env — new model AND new collection, always together
CPU_EMBED_MODEL=intfloat/multilingual-e5-small
RAG_COLLECTION=kb-e5

# 2. Download it (the download-* target of your profile fetches the
#    embedding model too; resumable)
make download-n97          # or download-cpu / download-gpu

# 3. Restart every service that touches the knowledge base
docker compose -f docker-compose.yml -f docker-compose.n97.yml \
    up -d embed-server litellm mcpo monitor

# 4. Re-add your documents (monitor upload bar or make embed) —
#    the new collection is created automatically with the right
#    vector size on first upload
```

Your old collection stays in Qdrant untouched, so switching back is just
reverting the two `.env` lines and restarting the same services. To
reclaim space from an abandoned collection:
`curl -X DELETE http://localhost:6333/collections/<name>`

Retrieval tuning knobs (`.env`, applied by the LiteLLM auto-RAG hook):
`RAG_TOP_K` (excerpts injected per question, default 3) and
`RAG_MIN_SCORE` (relevance cutoff 0-1, default 0.4 — raise it if answers
cite irrelevant excerpts, lower it if the KB misses things it does contain).

---

## Scaling up: NVIDIA GPU servers (x86 and ARM)

The GPU profile (`make up`) is the same stack with vLLM on CUDA — nothing
else changes, so a knowledge base built on an N97 works identically on an
HGX-class server. Sizing guidance:

| Machine class | Suggested starting model | .env |
|---|---|---|
| Single RTX GPU (8-24 GB) | Qwen2.5-7B-Instruct (default) | defaults work |
| Multi-GPU x86 server (HGX/DGX class) | 32B-72B+ models | raise `MAX_MODEL_LEN`; add `--tensor-parallel-size N` to the vllm command |
| ARM Grace + NVIDIA (GB300 / MGX class) | as above | set `VLLM_IMAGE` to an arm64 vLLM build (e.g. from NGC) — the default Docker Hub image is x86-64 |

Notes for big iron:

- **Image**: `VLLM_IMAGE` in `.env` swaps the inference image without
  touching compose files. On Grace/ARM systems use an aarch64 vLLM image
  (NVIDIA publishes vLLM containers on NGC); everything else in the stack
  (OpenWebUI, Qdrant, SearXNG, mcpo, monitor) is multi-arch or builds
  locally on arm64.
- **Tensor parallelism**: for multi-GPU serving add `--tensor-parallel-size`
  to the vllm `command:` in `docker-compose.yml` and raise the `count` under
  `deploy.resources` accordingly.
- **Throughput**: raise `MAX_MODEL_LEN` and `GPU_MEMORY_UTILIZATION` in
  `.env`; vLLM batches concurrent users automatically.
- **Embeddings**: the CPU embed-server is fine at any scale for personal or
  team KBs; it only runs when documents are added or questions asked.

---

## Multi-language chat & RAG

Three independent layers control language support:

1. **The chat model** does the talking. The default Qwen2.5 family is
   already strong in English and Chinese and usable in ~29 languages —
   just type in your language, no configuration needed. For deeper coverage
   of a specific language, swap the chat model (see *Changing the AI
   model*); good multilingual picks include larger Qwen2.5 sizes, or
   Gemma/Llama variants for European languages.
2. **The UI**: OpenWebUI follows your browser language automatically and
   can be forced per user under Settings → Interface → Language (Traditional
   Chinese, Japanese, German, etc.).
3. **RAG embeddings** decide how well non-English documents are *searched*.
   The default `nomic-embed-text-v1.5` is English-optimised — for
   multilingual knowledge bases switch to `intfloat/multilingual-e5-small`
   or `BAAI/bge-m3` following **Switching the RAG embedding model** above.

---

## Web search in chat

The stack ships a private SearXNG metasearch engine — chat can use it to
answer questions about current information, with no cloud search API and
no tracking. Two ways to use it:

### Option 1 — Globe toggle (retrieval before answering)

1. In the OpenWebUI message box, toggle the **globe icon (Web Search)**.
2. Ask something time-sensitive: *"What is the latest Ubuntu LTS release?"*
3. OpenWebUI queries SearXNG (`http://searxng:8080` internally), retrieves
   the top pages, and the model answers with citations.

Enabled by default via `ENABLE_WEB_SEARCH=true` (see `.env.example`).
Every message in that chat triggers retrieval, whether it needs the web
or not.

### Option 2 — Agentic search (model decides when to search)

The **web-search** OpenWebUI tool lets the model call SearXNG itself, only
when a question actually needs live data, and cite sources inline as
`[Title](URL)`:

1. Admin Panel → Tools → + New Tool
2. Paste the contents of `tools/web-search.py` → Save
3. Enable the **🔧 wrench** icon in a chat — the model can now search
   the web on its own

To make the model search *proactively* (and state clearly when live data
is unavailable), apply the ready-made system prompt in
`config/prompts/web-search-assistant.md` to your model — instructions are
in that file. The tool uses SearXNG's JSON API, which
`config/searxng/settings.yml` already enables.

Other paths to the web for agentic use: the mcpo `fetch` tool (model
fetches a specific URL) and SearXNG's own UI at `http://localhost:8092`.
On small models, keep web search off for ordinary chats — retrieving and
reading pages adds noticeable latency on low-power CPUs.

---

## CPU-only mode

No GPU? Two profiles run the whole stack on CPU; everything except the LLM
engine is identical.

**Option 1 — llama.cpp (recommended for low-power boxes):** quantized 3B
model, lowest RAM use, fastest on AVX2-only chips like the Intel N97/N100.

```bash
make download-n97   # ~2.5 GB of models
make up-n97
```

Full guide, tuning knobs, and hardware caveats:
**[docs/DEPLOY-N97.md](docs/DEPLOY-N97.md)**.

**Option 2 — vLLM on CPU:** the real vLLM engine via the official
`vllm/vllm-openai-cpu` image, serving Qwen2.5-1.5B in bf16. Use this when
you specifically want vLLM (API parity with the GPU stack, vLLM-specific
features, or testing before a GPU deployment).

```bash
make setup-cpu      # pull + build + download models (~3.6 GB) + start + health check
```

Or step by step: `make pull-cpu`, `make build`, `make download-cpu`,
`make up-cpu`. The model download runs inside a throwaway Docker container
straight into `MODELS_DIR`, so it needs no Python venv on the host and works
the same whether you run make as root or as a normal user.

vLLM's x86 CPU backend is optimized for AVX-512; on AVX2-only CPUs it runs
in "limited features" mode — expect it to be noticeably slower and heavier
than option 1 on the same hardware (that's why option 1 exists). Tune via
`CPU_CHAT_MODEL`, `CPU_MAX_MODEL_LEN`, and `VLLM_CPU_KVCACHE_SPACE` in
`.env` before the first `make bootstrap`; afterwards change models with
`local-ezai model …` (LiteLLM routing is rendered per generation).

---

## Development

### Run a service locally (outside Docker)

**embed-server:**
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install fastapi uvicorn sentence-transformers
python3 embed-server/server.py
# Listens on :8001
```

**qdrant-rag MCP server:**
```bash
cd mcp-servers/qdrant-rag
npm install
QDRANT_URL=http://localhost:6333 EMBED_URL=http://localhost:8001/v1 node index.js
```

**monitor:**
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install fastapi uvicorn httpx
python3 monitor/monitor.py
# Listens on :8888
```

### Embed documents manually

`make embed` runs this in Docker for you (drop files into `./documents`
first). To run it by hand with custom chunking options:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install requests qdrant-client

python3 scripts/embed_documents.py \
  --input-dir ./documents \
  --qdrant-url http://localhost:6333 \
  --embed-url http://localhost:8001/v1 \
  --collection my-knowledge-base \
  --chunk-size 400 \
  --overlap 50 \
  --batch-size 16
```

Options:
- `--chunk-size` — words per chunk (default 400)
- `--overlap` — word overlap between adjacent chunks (must be < chunk-size, default 50)
- `--batch-size` — how many chunks to embed per API call (default 16)

### Rebuild a single service after code changes

```bash
docker compose build embed-server   # or: monitor, mcpo
docker compose up -d embed-server   # hot-swap just that container
```

### Watch logs for a specific service

```bash
make logs-embed-server
make logs-vllm
make logs-monitor
make logs-qdrant
```

---

## Kubernetes deployment (K3s)

The `k8s/` directory has manifests for OpenWebUI, LiteLLM, Qdrant, and an Ingress. GPU/vLLM deployment on K8s requires a GPU node with the NVIDIA device plugin — not included but straightforward to add.

```bash
# Install K3s
curl -sfL https://get.k3s.io | sh -

# Create a secret for the API key
kubectl create secret generic ai-service-secrets \
  --from-literal=litellm-master-key=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2) \
  -n ai-service

# Deploy
make k8s
# Access at: http://ai.local (adds /etc/hosts entry automatically)
```

---

## Slurm (HPC)

For running batch embedding jobs on a shared GPU cluster:

```bash
make slurm-setup        # install single-node Slurm
sbatch slurm/embed-job.sh   # submit an embedding job
```

The embed job reads from `./documents` and writes to Qdrant on `localhost:6333`. Make sure Qdrant is running on the compute node or adjust the `--qdrant-url` in `slurm/embed-job.sh`.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Bind for 0.0.0.0:XXXX failed: port is already allocated` | Shouldn't happen anymore — `make up*` auto-relocates conflicting ports and saves them to `.env`. If starting compose directly, run `bash scripts/check-ports.sh` first |
| A service crash-loops with `ImportError: cannot import name …` | An upstream package broke compatibility. All images pin known-good versions — `git pull && make build && make up-<profile>` rebuilds with the pins; report the log if it persists |
| Logging in to OpenWebUI logs out another session | Sessions on **different devices/browsers are independent** and never affect each other. One *browser* holds a single login per address — two accounts on the same machine need two browsers, profiles, or a private window |
| All OpenWebUI users logged out after a restart | `WEBUI_SECRET_KEY` changed — set it to a fixed value in `.env` (sessions are signed with it) |
| `ContextWindowExceededError: request (N tokens) exceeds …` | The chat outgrew the model's context: start a new chat for a new topic, turn web search off when not needed, disable unused tools — or raise `N97_CTX=16384` in `.env` and `docker compose … up -d vllm` (more RAM, slower prefill) |
| `make health` shows vLLM offline | Still loading — wait 3–5 min, then: `make logs-vllm` |
| `Exited (137)` on vLLM | Out of VRAM — lower `GPU_MEMORY_UTILIZATION=0.75` in `.env` and `make restart` |
| LiteLLM returns 401 | `LITELLM_MASTER_KEY` in `.env` must match the key OpenWebUI is sending |
| mcpo tools not visible in chat | Admin Panel → Settings → Tools → verify URL and API key; `make logs-mcpo` |
| `make embed` fails with "Cannot connect" | Start Qdrant and embed-server first: `make up` |
| Qdrant search returns nothing | Run `make embed` to populate the collection |
| `externally-managed-environment` Python error | No host Python is needed anymore — downloads and `make embed` run in Docker; for development use a project venv: `python3 -m venv .venv` |
| No GPU found by Docker | Verify NVIDIA Container Toolkit: `docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi` |
| `no space left on device` | `docker system prune -af` to free unused images/volumes |
| Monitor shows all services unknown | It polls every 15 s from inside Docker — services must be on the `ai-net` network |

---

## License

MIT — use freely for personal and commercial projects.
