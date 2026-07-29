.PHONY: help setup build pull up up-cpu pull-cpu download-cpu update-cpu setup-cpu wait-ready \
        up-n97 pull-n97 download-n97 update-n97 setup-n97 up-n97-igpu bench \
        down restart logs health status embed \
        reset-webui reset-password \
        k8s k8s-delete update clean slurm-setup push-github monitor

# Load .env if it exists
-include .env
export

# Defaults (a .env value wins). Everything lives inside the project folder —
# absolute paths so docker compose mounts the same folder no matter which
# user runs it, and no host Python venv is needed anywhere.
CPU_CHAT_MODEL  ?= Qwen/Qwen2.5-1.5B-Instruct
CPU_EMBED_MODEL ?= nomic-ai/nomic-embed-text-v1.5
N97_MODEL_FILE  ?= qwen2.5-3b-instruct-q4_k_m.gguf
# nomic-embed loads its custom modeling code (trust_remote_code) from this
# separate repo at runtime — the offline cache must contain it too
EMBED_CODE_REPO ?= nomic-ai/nomic-bert-2048
MODELS_DIR      ?= $(CURDIR)/models/hf-cache
N97_GGUF_DIR    ?= $(CURDIR)/models/gguf
DOCUMENTS_DIR   ?= $(CURDIR)/documents
RAG_COLLECTION  ?= my-knowledge-base

# HF hub cache layout: hf download puts each model in models--<org>--<name>
CHAT_MODEL_DIR  = $(MODELS_DIR)/models--$(subst /,--,$(CPU_CHAT_MODEL))
EMBED_MODEL_DIR = $(MODELS_DIR)/models--$(subst /,--,$(CPU_EMBED_MODEL))
EMBED_CODE_DIR  = $(MODELS_DIR)/models--$(subst /,--,$(EMBED_CODE_REPO))

COMPOSE_CPU = docker compose -f docker-compose.yml -f docker-compose.cpu.yml

help: ## Show all available commands
	@echo ""
	@echo "  ╔══════════════════════════════════╗"
	@echo "  ║   AI Service — Make Commands     ║"
	@echo "  ╚══════════════════════════════════╝"
	@echo ""
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""

setup: ## Run the automated system setup script (first time only)
	@bash scripts/setup.sh

build: ## Build custom Docker images (embed-server, mcpo, monitor)
	docker compose build embed-server mcpo monitor

pull: ## Pull all official Docker images
	docker compose pull openwebui litellm vllm qdrant searxng

up: ## Start all services with GPU
	docker compose up -d
	@echo ""
	@echo "  Services starting... run 'make health' in 2-3 minutes"
	@echo "  Chat UI:  http://localhost:3000"
	@echo "  Monitor:  http://localhost:8888"
	@echo ""

setup-cpu: ## One command for the vLLM CPU stack: pull, build, download models, start, wait until healthy
	$(MAKE) pull-cpu
	$(MAKE) build
	$(MAKE) download-cpu
	$(MAKE) up-cpu
	$(MAKE) wait-ready

up-cpu: ## Start with vLLM on CPU (auto-downloads models if missing; slower than up-n97 on AVX2-only boxes)
	@if [ ! -d "$(CHAT_MODEL_DIR)/snapshots" ] || [ ! -d "$(EMBED_MODEL_DIR)/snapshots" ] || [ ! -d "$(EMBED_CODE_DIR)/snapshots" ]; then \
		echo "Models not found in $(MODELS_DIR) — downloading them first (~3.6 GB)..."; \
		$(MAKE) download-cpu; \
	fi
	$(COMPOSE_CPU) up -d
	@echo ""
	@echo "  vLLM CPU mode: http://localhost:$(or $(OPENWEBUI_PORT),3000)"
	@echo "  vLLM takes 1-3 minutes to load the model — run 'make wait-ready' or 'make health'"
	@echo ""

wait-ready: ## Wait for the LLM server to finish loading, then run the health check
	@echo "Waiting for the LLM server to load the model (up to 5 minutes)..."
	@ok=0; for i in $$(seq 1 60); do \
		if curl -sf -o /dev/null http://localhost:$(or $(LLM_PORT),8000)/health; then ok=1; break; fi; \
		sleep 5; \
	done; \
	if [ "$$ok" != "1" ]; then \
		echo "LLM server is still not answering — check: docker compose logs vllm | tail -30"; \
	fi
	@bash scripts/health-check.sh

pull-cpu: ## Pull images for the vLLM CPU stack
	$(COMPOSE_CPU) pull openwebui litellm vllm qdrant searxng

download-cpu: ## Download models for the vLLM CPU stack (~3.6 GB; runs in Docker, resumable)
	@mkdir -p "$(MODELS_DIR)"
	@echo "Downloading models (several GB) — do NOT interrupt; re-running resumes."
	docker run --rm $(shell [ -t 1 ] && echo -t) \
		-v "$(MODELS_DIR)":/hf-cache \
		-e HF_HUB_CACHE=/hf-cache \
		-e HF_TOKEN \
		python:3.11-slim \
		bash -c "pip install -q 'huggingface_hub[cli]' && \
		         hf download $(CPU_CHAT_MODEL) && \
		         hf download $(CPU_EMBED_MODEL) && \
		         hf download $(EMBED_CODE_REPO)"

update-cpu: ## Pull latest images and restart the vLLM CPU stack (do NOT use 'make update')
	$(COMPOSE_CPU) pull
	$(COMPOSE_CPU) up -d

setup-n97: ## One command for the N97 stack: pull, build, download models, start, wait until healthy
	$(MAKE) pull-n97
	$(MAKE) build
	$(MAKE) download-n97
	$(MAKE) up-n97
	$(MAKE) wait-ready

up-n97: ## Start all services tuned for Intel N97 / low-power mini PCs (auto-downloads models if missing)
	@if [ ! -f "$(N97_GGUF_DIR)/$(N97_MODEL_FILE)" ] || [ ! -d "$(EMBED_MODEL_DIR)/snapshots" ] || [ ! -d "$(EMBED_CODE_DIR)/snapshots" ]; then \
		echo "Models not found — downloading them first..."; \
		$(MAKE) download-n97; \
	fi
	docker compose -f docker-compose.yml -f docker-compose.n97.yml up -d
	@echo ""
	@echo "  N97 mode (llama.cpp): http://localhost:$(or $(OPENWEBUI_PORT),3000)"
	@echo "  First start loads the model — run 'make wait-ready' or 'make health'"
	@echo ""

up-n97-igpu: ## N97 profile with llama.cpp on the Intel iGPU (Vulkan): ~2-3x faster prompt processing
	docker compose -f docker-compose.yml -f docker-compose.n97.yml -f docker-compose.n97-igpu.yml up -d
	@echo ""
	@echo "  N97 iGPU mode (llama.cpp + Vulkan): http://localhost:3000"
	@echo "  Check the iGPU was picked up:  docker compose logs vllm | grep -i vulkan"
	@echo ""

pull-n97: ## Pull images for the N97 stack (llama.cpp instead of vLLM)
	docker compose -f docker-compose.yml -f docker-compose.n97.yml pull openwebui litellm vllm qdrant searxng

download-n97: ## Download the small quantized model set for the N97 stack
	@bash scripts/download-models-n97.sh

update-n97: ## Pull latest images and restart the N97 stack (do NOT use 'make update')
	docker compose -f docker-compose.yml -f docker-compose.n97.yml pull
	docker compose -f docker-compose.yml -f docker-compose.n97.yml up -d

down: ## Stop all services
	docker compose down

restart: ## Restart all services
	docker compose restart

logs: ## Show live logs from all services (Ctrl+C to stop)
	docker compose logs -f

logs-%: ## Show logs for a specific service (e.g. make logs-vllm)
	docker compose logs -f $*

health: ## Run health check on all services
	@bash scripts/health-check.sh

bench: ## One-question LLM benchmark — prints prompt & generation tokens/sec
	@bash scripts/bench.sh

install-autorag: ## Install the Auto-RAG filter into OpenWebUI (global, no UI steps)
	@bash scripts/install-autorag.sh

status: ## Show status of all containers
	docker compose ps

embed: ## Embed documents from the documents folder into the Qdrant knowledge base (runs in Docker)
	@echo "Embedding documents from $(DOCUMENTS_DIR)..."
	@mkdir -p "$(DOCUMENTS_DIR)"
	docker run --rm --network host \
		-v "$(CURDIR)/scripts":/scripts:ro \
		-v "$(DOCUMENTS_DIR)":/documents:ro \
		python:3.11-slim \
		bash -c "pip install -q qdrant-client requests pypdf && \
		         python3 /scripts/embed_documents.py \
		           --input-dir /documents \
		           --qdrant-url http://localhost:$(or $(QDRANT_PORT),6333) \
		           --embed-url http://localhost:$(or $(EMBED_PORT),8001)/v1 \
		           --collection $(RAG_COLLECTION)"

reset-webui: ## Factory-reset OpenWebUI — deletes ALL users, passwords, chats and settings (asks first)
	@bash scripts/reset-openwebui.sh wipe

reset-password: ## Reset an OpenWebUI password: make reset-password EMAIL=you@example.com PASSWORD=newpass  (EMAIL=all → every user)
	@bash scripts/reset-openwebui.sh password "$(EMAIL)" "$(PASSWORD)"

monitor: ## Open the web monitoring dashboard
	@echo "Monitor dashboard: http://localhost:8888"
	@xdg-open http://localhost:8888 2>/dev/null || open http://localhost:8888 2>/dev/null || true

k8s: ## Deploy to K3s Kubernetes cluster
	kubectl apply -f k8s/namespace.yaml
	kubectl apply -f k8s/qdrant.yaml
	kubectl apply -f k8s/openwebui.yaml
	kubectl apply -f k8s/litellm.yaml
	kubectl apply -f k8s/ingress.yaml
	@echo "  127.0.0.1 ai.local" | sudo tee -a /etc/hosts
	@echo "  Access at: http://ai.local"

k8s-delete: ## Delete all Kubernetes resources
	kubectl delete namespace ai-service

update: ## Pull latest images and restart
	docker compose pull
	docker compose up -d

slurm-setup: ## Run the automated Slurm setup script
	@bash slurm/setup-slurm.sh

clean: ## Remove all containers, images, and volumes (WARNING: deletes data)
	@echo "⚠️  This will delete all containers, images and volumes."
	@read -p "Continue? (y/N): " confirm && [ "$$confirm" = "y" ]
	docker compose down -v
	docker system prune -af

push-github: ## Initialize git and push to GitHub (run after cloning)
	@echo "Enter your GitHub username:"
	@read USERNAME; \
	echo "Enter your repo name (e.g. ai-service):"; \
	read REPO; \
	git init && \
	git add . && \
	git commit -m "Initial commit — self-hosted AI service" && \
	git branch -M main && \
	git remote add origin https://github.com/$$USERNAME/$$REPO.git && \
	git push -u origin main
