# 🤖 Self-Hosted AI Service

A complete, production-ready self-hosted AI stack with agents and MCP tools.
Runs entirely on your own hardware — no cloud API costs, full privacy.

```
Browser → OpenWebUI → LiteLLM → vLLM (Qwen2.5-7B, GPU)
                              → Embed Server (nomic-embed, CPU)

Agent Tools (MCP):  filesystem · memory · web fetch · RAG search
Vector Database:    Qdrant
Web Search:         SearXNG (private)
Orchestration:      Docker Compose → K3s → Slurm
```

---

## ⚡ Quick Start (5 commands)

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/ai-service.git
cd ai-service

# 2. Run the automated setup (installs Docker, NVIDIA toolkit, Python, Node.js)
bash scripts/setup.sh

# 3. Download AI models (~15 GB, takes 10–40 min)
bash scripts/download-models.sh

# 4. Build and launch
make build && make pull && make up

# 5. Check everything is healthy
make health
```

Then open **http://localhost:3000** and start chatting.

---

## 📋 Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| OS | Ubuntu 24.04.4 LTS | Ubuntu 24.04.4 LTS |
| CPU | 8 cores | 16 cores |
| RAM | 32 GB | 64 GB |
| GPU | NVIDIA 8 GB VRAM | NVIDIA 16 GB VRAM |
| Disk | 200 GB SSD | 500 GB NVMe |

> **No GPU?** Use the CPU-only override: `make up-cpu` — uses a small model (0.5B) on CPU.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  USER LAYER                                                      │
│  OpenWebUI  (http://localhost:3000)                             │
└─────────────────────────┬───────────────────────────────────────┘
                          │ OpenAI-compatible API
┌─────────────────────────▼───────────────────────────────────────┐
│  ROUTING LAYER                                                   │
│  LiteLLM proxy  (http://localhost:4000)                         │
└──────────┬──────────────────────────────┬───────────────────────┘
           │                              │
┌──────────▼──────────┐     ┌────────────▼────────────────────────┐
│  vLLM (GPU)         │     │  Embed Server (CPU)                 │
│  Qwen2.5-7B         │     │  nomic-embed-text-v1.5              │
│  port 8000          │     │  port 8001                          │
└─────────────────────┘     └────────────────┬────────────────────┘
                                             │ embeddings
┌─────────────────────────────────────────────▼───────────────────┐
│  AGENT TOOLS                                                     │
│  mcpo MCP proxy  (http://localhost:8200)                        │
│  ├── filesystem  →  ~/documents                                 │
│  ├── memory      →  knowledge graph                             │
│  ├── fetch       →  any web page                                │
│  └── qdrant-rag  →  Qdrant vector search                       │
└──────────────────────────────────────────────┬──────────────────┘
                                               │
┌──────────────────────────────────────────────▼──────────────────┐
│  DATA LAYER                                                      │
│  Qdrant vector DB  (http://localhost:6333)                      │
│  SearXNG web search  (http://localhost:8090)                    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 📁 Repository Structure

```
ai-service/
├── docker-compose.yml         Main stack (GPU)
├── docker-compose.cpu.yml     CPU-only override
├── .env.example               Configuration template
├── Makefile                   All management commands
│
├── config/
│   ├── litellm-config.yaml    Model routing config
│   ├── searxng/settings.yml   Private web search config
│   └── mcpo-config.json       MCP tool server list
│
├── embed-server/              CPU embedding service
│   ├── Dockerfile
│   └── server.py
│
├── mcpo/                      MCP tool proxy
│   └── Dockerfile
│
├── mcp-servers/
│   └── qdrant-rag/            Custom RAG search MCP server
│       ├── package.json
│       └── index.js
│
├── scripts/
│   ├── setup.sh               Automated system setup
│   ├── download-models.sh     Download HuggingFace models
│   ├── health-check.sh        Verify all services running
│   └── embed_documents.py     Add documents to knowledge base
│
├── k8s/                       Kubernetes manifests (K3s)
│   ├── namespace.yaml
│   ├── qdrant.yaml
│   ├── openwebui.yaml
│   ├── litellm.yaml
│   └── ingress.yaml
│
├── slurm/                     GPU job scheduling
│   ├── setup-slurm.sh
│   ├── test-job.sh
│   └── embed-job.sh
│
└── tools/
    └── knowledge-base-search.py  OpenWebUI Python tool (paste into UI)
```

---

## 🚀 Step-by-Step Setup

### Step 1 — Run the setup script

```bash
bash scripts/setup.sh
```

This installs: Docker, NVIDIA Container Toolkit, Python 3 venv, Node.js 20

### Step 2 — Configure your environment

```bash
cp .env.example .env
nano .env   # change the secret keys
```

### Step 3 — Download models

```bash
bash scripts/download-models.sh
```

Downloads Qwen2.5-7B-Instruct (~15 GB) and nomic-embed-text-v1.5 (~500 MB).
Models are cached in `~/ai-models/hf-cache`.

### Step 4 — Build and launch

```bash
make build   # builds embed-server and mcpo images (~5 min)
make pull    # pulls official images (~10 min)
make up      # starts all 7 services
make health  # verify everything is healthy
```

### Step 5 — Open the chat interface

Go to **http://localhost:3000**, create an account, select `qwen2.5-7b`, start chatting.

### Step 6 — Connect MCP tools

1. Admin Panel → Settings → Tools → Add Tool Server
2. URL: `http://mcpo:8200`  |  API Key: `local-tools-key`
3. Enable the 🔧 wrench icon in chat to activate tools

### Step 7 — Add your documents

```bash
# Put your .txt or .md files in ~/documents, then:
make embed
```

---

## ⚙️ Configuration

All settings are in `.env`. Key values to change:

| Variable | Default | Description |
|----------|---------|-------------|
| `LITELLM_MASTER_KEY` | `sk-ai-service-2024` | API key for LiteLLM ⚠️ change this |
| `WEBUI_SECRET_KEY` | `change-this` | OpenWebUI session secret ⚠️ change this |
| `MCP_API_KEY` | `local-tools-key` | MCP tool server key |
| `CHAT_MODEL` | `Qwen/Qwen2.5-7B-Instruct` | HuggingFace model ID |
| `CHAT_MODEL_NAME` | `qwen2.5-7b` | Short name used in API calls |
| `MAX_MODEL_LEN` | `4096` | Max context window tokens |
| `GPU_MEMORY_UTILIZATION` | `0.85` | Fraction of VRAM to use |

### Changing the AI model

Edit `.env`:
```bash
CHAT_MODEL=mistralai/Mistral-7B-Instruct-v0.3
CHAT_MODEL_NAME=mistral-7b
```

Then download and restart:
```bash
source ~/ai-env/bin/activate
huggingface-cli download mistralai/Mistral-7B-Instruct-v0.3 --cache-dir ~/ai-models/hf-cache
make restart
```

---

## 🛠️ Make Commands

```
make help       Show all commands
make setup      Run automated setup script
make build      Build custom Docker images
make pull       Pull official Docker images
make up         Start all services
make up-cpu     Start in CPU-only mode (no GPU)
make down       Stop all services
make restart    Restart all services
make logs       Show live logs (all services)
make health     Run health check on all services
make status     Show container status
make embed      Embed ~/documents into knowledge base
make k8s        Deploy to K3s Kubernetes
make update     Pull latest images and restart
make clean      Remove all containers and images
```

---

## 🔍 Service URLs

| Service | URL | Credentials |
|---------|-----|-------------|
| OpenWebUI | http://localhost:3000 | Create on first visit |
| LiteLLM API | http://localhost:4000 | Key from `.env` |
| vLLM API | http://localhost:8000 | None |
| Embed Server | http://localhost:8001 | None |
| Qdrant | http://localhost:6333 | None |
| SearXNG | http://localhost:8090 | None |
| mcpo tools | http://localhost:8200 | Key from `.env` |

---

## 🩺 Troubleshooting

| Error | Fix |
|-------|-----|
| `externally-managed-environment` | `source ~/ai-env/bin/activate` |
| Container `Exited (137)` | OOM — reduce `GPU_MEMORY_UTILIZATION` to `0.75` in `.env` |
| vLLM health check fails | Still loading — wait 3 min, check: `docker compose logs vllm \| tail -20` |
| LiteLLM 401 | `OPENAI_API_KEY` must match `LITELLM_MASTER_KEY` in `.env` |
| mcpo tools not showing | `docker compose logs mcpo` — wait for "Listening on port 8200" |
| Qdrant returns no results | Run `make embed` to populate the knowledge base |
| No space on device | `docker system prune -af` |

---

## 📚 Learn More

This repo comes with a complete step-by-step guide. See the [GUIDE.md](GUIDE.md) for:
- Detailed explanation of every step
- K3s (Kubernetes) deployment
- Slurm GPU job scheduling
- Prometheus + Grafana monitoring
- Your AI infrastructure engineer learning roadmap

---

## 📄 License

MIT License — use freely for personal and commercial projects.
