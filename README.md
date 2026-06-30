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

## Requirements

| | Minimum | Recommended |
|--|---------|-------------|
| OS | Ubuntu 24.04 LTS | Ubuntu 24.04 LTS |
| CPU | 8 cores | 16 cores |
| RAM | 32 GB | 64 GB |
| GPU | NVIDIA 8 GB VRAM | NVIDIA 16+ GB VRAM |
| Disk | 200 GB SSD | 500 GB NVMe |

> **No GPU?** Use `make up-cpu` — runs a small 0.5B model on CPU only.

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/local-ezai.git
cd local-ezai

# 2. First-time system setup (installs Docker, NVIDIA toolkit, Python, Node.js)
bash scripts/setup.sh
# ↳ If the script installs the NVIDIA driver, reboot, then re-run to continue.

# 3. Copy config and set your secret keys
cp .env.example .env
nano .env   # change LITELLM_MASTER_KEY, WEBUI_SECRET_KEY, SEARXNG_SECRET

# 4. Download models (~15 GB, takes 10–40 min depending on connection)
bash scripts/download-models.sh

# 5. Build images, pull the rest, start everything
make build && make pull && make up

# 6. Wait ~3 minutes for vLLM to load the model, then check health
make health
```

Open **http://localhost:3000** to chat, **http://localhost:8888** for the monitor.

---

## Service map

| Service | URL | Auth |
|---------|-----|------|
| **OpenWebUI** — chat interface | http://localhost:3000 | create account on first visit |
| **Monitor** — live dashboard | http://localhost:8888 | none |
| **LiteLLM** — model proxy API | http://localhost:4000 | `LITELLM_MASTER_KEY` from `.env` |
| **vLLM** — LLM inference | http://localhost:8000 | none |
| **Embed Server** — embedding API | http://localhost:8001 | none |
| **Qdrant** — vector database | http://localhost:6333 | none |
| **SearXNG** — private web search | http://localhost:8090 | none |
| **mcpo** — MCP tools proxy | http://localhost:8200 | `MCP_API_KEY` from `.env` |

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
│  ├── filesystem  → ~/documents  (read/write files)          │
│  ├── memory      → knowledge graph (persistent across chats)│
│  ├── fetch       → any public web page                      │
│  └── qdrant-rag  → semantic search over your documents      │
└─────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  DATA                                                        │
│  Qdrant  :6333     SearXNG  :8090                           │
└─────────────────────────────────────────────────────────────┘
```

---

## Repository layout

```
local-ezai/
├── docker-compose.yml          GPU stack (8 services)
├── docker-compose.cpu.yml      CPU-only override for vLLM
├── .env.example                All configuration variables
├── Makefile                    Management commands
│
├── config/
│   ├── litellm-config.yaml     Model routing (reads from env)
│   ├── mcpo-config.json        MCP server list (reads from env)
│   └── searxng/settings.yml    Search engine config
│
├── embed-server/               Embedding API (FastAPI + sentence-transformers)
│   ├── Dockerfile
│   └── server.py
│
├── monitor/                    Live monitoring dashboard (FastAPI + SSE)
│   ├── Dockerfile
│   └── monitor.py
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
│   ├── download-models.sh      Download HuggingFace models
│   ├── health-check.sh         Check all 8 services
│   └── embed_documents.py      Ingest documents into Qdrant
│
├── tools/
│   └── knowledge-base-search.py   OpenWebUI Python tool (paste into UI)
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
| `MODELS_DIR` | `~/ai-models/hf-cache` | Host path where models are cached |
| `DOCUMENTS_DIR` | `~/documents` | Host path for `make embed` to read from |
| `RAG_COLLECTION` | `my-knowledge-base` | Qdrant collection name for RAG |

Generate secure random values for the secret keys:
```bash
openssl rand -hex 32
```

---

## Make commands

```
make help        List all commands
make setup       First-time system setup (Docker, NVIDIA, Python, Node)
make build       Build embed-server, mcpo, and monitor images
make pull        Pull official Docker images
make up          Start all 8 services (GPU mode)
make up-cpu      Start in CPU-only mode
make down        Stop all services
make restart     Restart all services
make logs        Tail logs from all services
make logs-vllm   Tail logs from a specific service (any name after logs-)
make health      Check health of all 8 services
make status      Show container status table
make embed       Ingest ~/documents into the Qdrant knowledge base
make monitor     Open the monitor dashboard in your browser
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

Installs Docker CE, NVIDIA Container Toolkit, Python 3 virtualenv, and Node.js 20.

> If NVIDIA drivers were installed, the script exits and asks you to reboot.  
> After rebooting, run `bash scripts/setup.sh` again to finish.

### 2. Configure secrets

```bash
cp .env.example .env
```

Open `.env` and change at minimum:
- `LITELLM_MASTER_KEY` — used as the API key everywhere
- `WEBUI_SECRET_KEY` — signs OpenWebUI session cookies
- `SEARXNG_SECRET` — HMAC key for SearXNG

Everything else can stay as-is for a local-only deployment.

### 3. Download models

```bash
bash scripts/download-models.sh
```

Downloads:
- **Qwen2.5-7B-Instruct** (~15 GB) — main chat model
- **nomic-embed-text-v1.5** (~500 MB) — embedding model for RAG

Models are stored in `~/ai-models/hf-cache` and mounted read-only into the containers. You only download once; rebuilding images does not re-download.

For **gated models** (Llama, Gemma): get a token at https://huggingface.co/settings/tokens and add `HF_TOKEN=hf_your_token` to `.env`.

### 4. Build and start

```bash
make build   # builds embed-server, mcpo, and monitor (~5 min)
make pull    # pulls openwebui, litellm, vllm, qdrant, searxng
make up      # starts all 8 services in the background
```

### 5. Wait for vLLM

vLLM takes 2–5 minutes to load the model into VRAM. Watch it:

```bash
make logs-vllm
# Wait until you see: "Application startup complete."
```

Then verify all services:

```bash
make health
```

All 8 checks should pass.

### 6. First login

1. Open **http://localhost:3000**
2. Click **Sign up** → create your admin account
3. Select model `qwen2.5-7b` in the chat dropdown

### 7. Connect MCP agent tools

1. In OpenWebUI: **Admin Panel → Settings → Tools**
2. Add a new tool server:
   - URL: `http://localhost:8200`
   - API Key: value of `MCP_API_KEY` from your `.env`
3. Enable the **🔧 wrench** icon in any chat to give the AI access to:
   - `filesystem` — read and write `~/documents`
   - `memory` — persistent knowledge graph across sessions
   - `fetch` — retrieve any web page
   - `search_knowledge_base` — semantic search over your embedded documents

### 8. Add documents to the knowledge base

```bash
# Put .txt or .md files in ~/documents, then:
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

Updates automatically via Server-Sent Events (SSE) — no manual refresh needed.

```bash
make monitor   # opens the dashboard in your browser
```

---

## Changing the AI model

Any vLLM-compatible model on HuggingFace works. Example — switch to Mistral 7B:

```bash
# 1. Download the model
. ~/ai-env/bin/activate
huggingface-cli download mistralai/Mistral-7B-Instruct-v0.3 \
  --cache-dir ~/ai-models/hf-cache

# 2. Update .env
CHAT_MODEL=mistralai/Mistral-7B-Instruct-v0.3
CHAT_MODEL_NAME=mistral-7b

# 3. Update LiteLLM config to match the new short name
nano config/litellm-config.yaml
# change model_name: qwen2.5-7b → model_name: mistral-7b

# 4. Restart
make restart
```

---

## CPU-only mode

No GPU? Use the CPU override — it loads a small 0.5B model instead:

```bash
make up-cpu
```

This uses `docker-compose.cpu.yml` as an override, which:
- Switches to `Qwen/Qwen2.5-0.5B-Instruct`
- Sets `--device cpu`
- Removes the GPU resource reservation

Responses will be slower (~1–5 tokens/sec) but everything else works identically.

---

## Development

### Run a service locally (outside Docker)

**embed-server:**
```bash
. ~/ai-env/bin/activate
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
. ~/ai-env/bin/activate
pip install fastapi uvicorn httpx
python3 monitor/monitor.py
# Listens on :8888
```

### Embed documents manually

```bash
. ~/ai-env/bin/activate
pip install requests qdrant-client

python3 scripts/embed_documents.py \
  --input-dir ~/documents \
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

The embed job reads from `~/documents` and writes to Qdrant on `localhost:6333`. Make sure Qdrant is running on the compute node or adjust the `--qdrant-url` in `slurm/embed-job.sh`.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `make health` shows vLLM offline | Still loading — wait 3–5 min, then: `make logs-vllm` |
| `Exited (137)` on vLLM | Out of VRAM — lower `GPU_MEMORY_UTILIZATION=0.75` in `.env` and `make restart` |
| LiteLLM returns 401 | `LITELLM_MASTER_KEY` in `.env` must match the key OpenWebUI is sending |
| mcpo tools not visible in chat | Admin Panel → Settings → Tools → verify URL and API key; `make logs-mcpo` |
| `make embed` fails with "Cannot connect" | Start Qdrant and embed-server first: `make up` |
| Qdrant search returns nothing | Run `make embed` to populate the collection |
| `externally-managed-environment` Python error | Run `. ~/ai-env/bin/activate` first |
| No GPU found by Docker | Verify NVIDIA Container Toolkit: `docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi` |
| `no space left on device` | `docker system prune -af` to free unused images/volumes |
| Monitor shows all services unknown | It polls every 15 s from inside Docker — services must be on the `ai-net` network |

---

## License

MIT — use freely for personal and commercial projects.
