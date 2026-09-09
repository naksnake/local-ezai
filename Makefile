.PHONY: help setup setup-system setup-offline bundle install build pull up up-cpu pull-cpu download-cpu update-cpu setup-cpu wait-ready \
        bootstrap require-rendered download-embed \
        up-n97 pull-n97 download-n97 update-n97 setup-n97 up-n97-igpu bench \
        setup-gpu download-gpu \
        down restart logs health status embed \
        reset-webui reset-password install-autorag orchestrator \
        k8s k8s-delete update clean slurm-setup push-github monitor

# Load .env if it exists
-include .env
export

# Defaults (a .env value wins). Everything lives inside the project folder —
# absolute paths so docker compose mounts the same folder no matter which
# user runs it, and no host Python venv is needed anywhere.
CPU_CHAT_MODEL  ?= Qwen/Qwen2.5-1.5B-Instruct
CPU_EMBED_MODEL ?= nomic-ai/nomic-embed-text-v1.5
N97_MODEL_FILE  ?= qwen2.5-1.5b-instruct-q4_k_m.gguf
# nomic-embed loads its custom modeling code (trust_remote_code) from this
# separate repo at runtime — the offline cache must contain it too
EMBED_CODE_REPO ?= nomic-ai/nomic-bert-2048
MODELS_DIR      ?= $(CURDIR)/models/hf-cache
N97_GGUF_DIR    ?= $(CURDIR)/models/gguf
DOCUMENTS_DIR   ?= $(CURDIR)/documents
RAG_COLLECTION  ?= my-knowledge-base

COMPOSE_CPU = docker compose -f docker-compose.yml -f docker-compose.cpu.yml

# V1 (ADR-027): LiteLLM routing and the engine slot are RENDERED per model
# generation by `make bootstrap`, which reads the .env model seeds once.
# Every start includes the rendered engine override; `require-rendered`
# guards starts on a platform that was never bootstrapped.
RENDERED_DIR     = config/rendered
RENDERED_ENGINE  = $(RENDERED_DIR)/docker-compose.engine.yml
COMPOSE_RENDERED = -f $(RENDERED_ENGINE)
AGENTD_CLI      ?= $(if $(wildcard .venv-agentd/bin/local-ezai),.venv-agentd/bin/local-ezai,local-ezai)

# V1 P2 (ADR-028): the ezaid control plane is an OPT-IN compose overlay
# (ADR-002 — rollback = don't start it) until CLI connected mode lands.
COMPOSE_CONTROL  = -f docker-compose.control.yml
EZAID_CLI       ?= $(if $(wildcard .venv-agentd/bin/ezaid),.venv-agentd/bin/ezaid,ezaid)
EZAID_SPEC       = docs/api/ezaid-openapi.json

# V1 P5 (ADR-031): the first run. `make setup` is the five-step contract of
# FINAL_FIRST_RUN_EXPERIENCE — install.sh (detect, .env, secrets; stops once
# for the model seeds when .env is new) then `local-ezai setup` (bootstrap,
# images, up, wait-ready, smoke, report). The CLI lives in the venv install.sh
# creates, so it is resolved when the recipe runs, not when make parses.
EZAI_CLI_RUN = CLI=$$( [ -x .venv-agentd/bin/local-ezai ] && echo .venv-agentd/bin/local-ezai || echo local-ezai ); $$CLI
EZAI_SETUP = $(EZAI_CLI_RUN) setup
# Offline bundle (PR-23): make bundle BUNDLE=<dir> on a connected host; on the
# air-gapped host make setup-offline BUNDLE=<dir> (no egress).
BUNDLE ?= ./local-ezai-bundle

help: ## Show all available commands
	@echo ""
	@echo "  ╔══════════════════════════════════╗"
	@echo "  ║   AI Service — Make Commands     ║"
	@echo "  ╚══════════════════════════════════╝"
	@echo ""
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""

setup: ## First run, all steps: ./install.sh (edit .env once when asked) → local-ezai setup (bootstrap, images, up, verify, report)
	@bash install.sh $(INSTALL_ARGS)
	@$(EZAI_SETUP) $(SETUP_ARGS)

setup-system: ## System packages for a fresh Ubuntu host: Docker, NVIDIA toolkit, Python venv, Node (was `make setup`)
	@bash scripts/setup.sh

bundle: ## Offline bundle of THIS bootstrapped platform (images + weights + seeds) → make bundle BUNDLE=/media/usb/local-ezai-bundle
	@$(EZAI_CLI_RUN) bundle create $(BUNDLE) $(BUNDLE_ARGS)

setup-offline: ## First run on an air-gapped host from a bundle, no egress: ./install.sh --offline $(BUNDLE) → local-ezai setup --offline
	@bash install.sh --offline $(BUNDLE) $(INSTALL_ARGS)
	@$(EZAI_SETUP) --offline $(SETUP_ARGS)

# V1 first run, steps 1–3 (ADR-031, PR-21): detect hardware → generate or REPAIR
# .env (secrets minted, model seeds validated before any download) → one review
# stop. Pass flags with INSTALL_ARGS="--yes --profile n97". Then: make setup.
install: ## First run, steps 1–3 only: detect hardware, generate/repair .env with minted secrets, validate the model seeds (./install.sh)
	@bash install.sh $(INSTALL_ARGS)

build: ## Build custom Docker images (embed-server, mcpo, monitor)
	docker compose build embed-server mcpo monitor

pull: ## Pull all official Docker images
	docker compose pull openwebui litellm vllm qdrant searxng

# Port conflicts are fixed in .env by check-ports.sh; the actual start runs
# in a sub-make so the corrected .env is re-read (make's exported copies of
# the old values would otherwise override it inside docker compose).
up: ## Start all services with GPU (auto-resolves port conflicts)
	@bash scripts/check-ports.sh
	@$(MAKE) --no-print-directory up-run

up-run: require-rendered
	@bash scripts/download-embed.sh
	docker compose -f docker-compose.yml $(COMPOSE_RENDERED) up -d
	@echo ""
	@echo "  Services starting... run 'make health' in 2-3 minutes"
	@echo "  Chat UI:  http://localhost:$(or $(OPENWEBUI_PORT),3000)"
	@echo "  Monitor:  http://localhost:$(or $(MONITOR_PORT),8888)"
	@echo ""

setup-gpu: ## First run asserting an accelerator: install.sh --profile gpu → local-ezai setup --profile gpu (bootstrap, images, up, verify, report)
	@bash install.sh --profile gpu $(INSTALL_ARGS)
	@$(EZAI_SETUP) --profile gpu $(SETUP_ARGS)

bootstrap: ## Consume the .env model seeds ONCE into model generation 1 and render LiteLLM + engine config (V1)
	@test -x .venv-agentd/bin/local-ezai || command -v local-ezai >/dev/null 2>&1 || $(MAKE) swe-install
	$(AGENTD_CLI) bootstrap --env .env

require-rendered:
	@if [ ! -f "$(RENDERED_DIR)/litellm-config.yaml" ] || [ ! -f "$(RENDERED_ENGINE)" ]; then \
		echo ""; \
		echo "  No rendered platform config yet: LiteLLM routing and the engine slot are"; \
		echo "  generated from the model registry. Run once:  make bootstrap"; \
		echo "  (reads AI_RUNTIME / REASONING_MODEL / CODING_MODEL / CHAT_MODEL from .env;"; \
		echo "   legacy CHAT_MODEL / CPU_* / N97_* settings are migrated automatically)"; \
		echo ""; \
		exit 1; \
	fi

download-gpu: ## Legacy: download the .env CHAT_MODEL + embedding model for the GPU stack (~15 GB default; runs in Docker)
	@bash scripts/download-models.sh

download-embed: ## Download the RAG embedding model (every profile; skipped when present; runs in Docker)
	@bash scripts/download-embed.sh

setup-cpu: ## First run asserting the cpu-standard class: install.sh --profile cpu → local-ezai setup --profile cpu
	@bash install.sh --profile cpu $(INSTALL_ARGS)
	@$(EZAI_SETUP) --profile cpu $(SETUP_ARGS)

up-cpu: ## Start with vLLM on CPU (auto-downloads models, auto-resolves port conflicts)
	@bash scripts/check-ports.sh
	@$(MAKE) --no-print-directory up-cpu-run

up-cpu-run: require-rendered
	@bash scripts/download-embed.sh
	$(COMPOSE_CPU) $(COMPOSE_RENDERED) up -d
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
	$(COMPOSE_CPU) $(COMPOSE_RENDERED) up -d

setup-n97: ## First run asserting the low-power class: install.sh --profile n97 → local-ezai setup --profile n97
	@bash install.sh --profile n97 $(INSTALL_ARGS)
	@$(EZAI_SETUP) --profile n97 $(SETUP_ARGS)

up-n97: ## Start all services for the low-power CPU profile (the n97 preset = class cpu-low; auto-downloads models, auto-resolves ports)
	@bash scripts/check-ports.sh
	@$(MAKE) --no-print-directory up-n97-run

up-n97-run: require-rendered
	@bash scripts/download-embed.sh
	docker compose -f docker-compose.yml -f docker-compose.n97.yml $(COMPOSE_RENDERED) up -d
	@echo ""
	@echo "  N97 mode (llama.cpp): http://localhost:$(or $(OPENWEBUI_PORT),3000)"
	@echo "  First start loads the model — run 'make wait-ready' or 'make health'"
	@echo ""

up-n97-igpu: ## N97 profile with llama.cpp on the Intel iGPU (Vulkan): ~2-3x faster prompt processing
	@bash scripts/check-ports.sh
	@$(MAKE) --no-print-directory up-n97-igpu-run

up-n97-igpu-run: require-rendered
	docker compose -f docker-compose.yml -f docker-compose.n97.yml -f docker-compose.n97-igpu.yml $(COMPOSE_RENDERED) up -d
	@echo ""
	@echo "  N97 iGPU mode (llama.cpp + Vulkan): http://localhost:$(or $(OPENWEBUI_PORT),3000)"
	@echo "  Check the iGPU was picked up:  docker compose logs vllm | grep -i vulkan"
	@echo ""

pull-n97: ## Pull images for the N97 stack (llama.cpp instead of vLLM)
	docker compose -f docker-compose.yml -f docker-compose.n97.yml pull openwebui litellm vllm qdrant searxng

download-n97: ## Download the small quantized model set for the N97 stack
	@bash scripts/download-models-n97.sh

update-n97: ## Pull latest images and restart the N97 stack (do NOT use 'make update')
	docker compose -f docker-compose.yml -f docker-compose.n97.yml pull
	docker compose -f docker-compose.yml -f docker-compose.n97.yml $(COMPOSE_RENDERED) up -d

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

orchestrator: ## Install/refresh the "Local-EZAI Orchestrator" persona in OpenWebUI (after the first login; no UI steps)
	@bash scripts/register-orchestrator.sh

status: ## Show status of all containers
	docker compose ps

embed: ## Embed documents from the documents folder into the Qdrant knowledge base (runs in Docker)
	@bash scripts/embed-documents.sh

reset-webui: ## Factory-reset OpenWebUI — deletes ALL users, passwords, chats and settings (asks first)
	@bash scripts/reset-openwebui.sh wipe

reset-password: ## Reset an OpenWebUI password: make reset-password EMAIL=you@example.com PASSWORD=newpass  (EMAIL=all → every user)
	@bash scripts/reset-openwebui.sh password "$(EMAIL)" "$(PASSWORD)"

monitor: ## Open the Admin Center (monitor): health & knowledge at /, overview at /overview, runs at /runs
	@echo "Admin Center (monitor): http://localhost:8888   overview: /overview   runs: /runs"
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
	docker compose -f docker-compose.yml $(COMPOSE_RENDERED) up -d

slurm-setup: ## Run the automated Slurm setup script
	@bash slurm/setup-slurm.sh

clean: ## Remove all containers, images, and volumes (WARNING: deletes data)
	@echo "⚠️  This will delete all containers, images and volumes."
	@read -p "Continue? (y/N): " confirm && [ "$$confirm" = "y" ]
	docker compose down -v
	docker system prune -af

# ═══════════════════════════════════════════════════════════════════════════
# Autonomous SWE runtime (agentd) — additive targets, see agentd/README.md
# ═══════════════════════════════════════════════════════════════════════════
.PHONY: swe-install swe-browsers swe-test swe-lint swe-drill swe-accept swe-parity swe-gates \
        release-gate soak swe-run swe-plan control-up control-down control-logs control-spec \
        control-serve

swe-install: ## Install the agentd runtime into ./.venv-agentd (editable, dev + browser + control extras)
	python3 -m venv .venv-agentd
	.venv-agentd/bin/pip install --upgrade pip -q
	.venv-agentd/bin/pip install -e './agentd[dev,browser,control]'
	@echo ""
	@echo "  agentd installed. Try:  .venv-agentd/bin/ezai run \"...\" --repo /path/to/repo"
	@echo "  For Browser QA, also run:  make swe-browsers"
	@echo ""

control-up: ## Start the ezaid control plane (:EZAI_CONTROL_PORT, default 8010) as a compose overlay
	docker compose -f docker-compose.yml $(COMPOSE_CONTROL) up -d --build ezaid
	@echo ""
	@echo "  ezaid: http://localhost:$(or $(EZAI_CONTROL_PORT),8010)/health   (docs: /docs, spec: /openapi.json)"
	@echo "  Calls under /v1 need:  Authorization: Bearer $$EZAI_CONTROL_TOKEN"
	@echo ""

control-down: ## Stop and remove the ezaid control plane container (the stack keeps running)
	docker compose -f docker-compose.yml $(COMPOSE_CONTROL) rm -sf ezaid

control-logs: ## Follow the ezaid control plane logs
	docker compose -f docker-compose.yml $(COMPOSE_CONTROL) logs -f ezaid

control-spec: ## Regenerate the versioned OpenAPI contract artifact ($(EZAID_SPEC)) from the app
	$(EZAID_CLI) --write-spec $(EZAID_SPEC)

control-serve: ## Run the ezaid control plane ON THIS HOST (foreground) — sees your repositories, so SWE runs through the API work
	@test -x .venv-agentd/bin/ezaid || command -v ezaid >/dev/null 2>&1 || $(MAKE) swe-install
	$(EZAID_CLI) --platform config --host $(or $(EZAI_CONTROL_HOST),127.0.0.1) --port $(or $(EZAI_CONTROL_PORT),8010)

swe-browsers: ## Download the Playwright Chromium used by Browser QA
	.venv-agentd/bin/playwright install chromium

swe-test: ## Run the agentd test suite (offline — no models needed)
	cd agentd && ../.venv-agentd/bin/python -m pytest tests

swe-lint: ## Lint the agentd runtime with ruff
	cd agentd && ../.venv-agentd/bin/python -m ruff check src tests

swe-drill: ## Chat-ops boundary drill: governance unreachable from chat, prompt-injection red-team, chat/RAG byte-identical (offline)
	cd agentd && ../.venv-agentd/bin/python -m pytest tests/security -v

swe-accept: ## First-run acceptance suite F1–F11 (docs/FIRST_RUN_EXPERIENCE.md §6, FINAL_FIRST_RUN_EXPERIENCE §6), offline
	cd agentd && ../.venv-agentd/bin/python -m pytest tests/acceptance -v

swe-parity: ## Parity harness (P6 release gate): every CLI_AND_WEBUI_STRATEGY §3 row via CLI-direct · CLI-connected · API — same bodies, state, audit (offline)
	cd agentd && ../.venv-agentd/bin/python -m pytest tests/parity -v

swe-gates: ## Agnosticism gates (P6): the third-runtime drill (a mock runtime from descriptor data alone), the H1 word audit, the H2–H4 class fixtures (offline)
	cd agentd && ../.venv-agentd/bin/python -m pytest tests/gates -v

release-gate: ## The P6 release gate in one command: lint · chat-stack baseline · boundary drill · F1–F11 acceptance · parity harness · agnosticism gates · the full suite
	$(MAKE) swe-lint
	python3 scripts/chat-stack-baseline.py --check
	$(MAKE) swe-drill
	$(MAKE) swe-accept
	$(MAKE) swe-parity
	$(MAKE) swe-gates
	$(MAKE) swe-test
	@echo ""
	@echo "  release gate: green — next: make soak (docs/SOAK_RUNBOOK.md), then docs/V1_RELEASE_REPORT.md §7 (sign-off, tag)"
	@echo ""

soak: ## 72 h soak on this host (docs/SOAK_RUNBOOK.md): health · status · bench · SWE runs · lifecycle churn with rollback · evolution → config/soak/<stamp>/results.md — make soak HOURS=72 [SOAK_ARGS="--dry-run"]
	@bash scripts/soak.sh --hours $(or $(HOURS),72) $(SOAK_ARGS)

swe-run: ## Autonomous run: make swe-run TASK="fix the bug" REPO=/path/to/repo
	.venv-agentd/bin/ezai run "$(TASK)" --repo "$(REPO)"

swe-plan: ## Dry-run (plan only): make swe-plan TASK="..." REPO=/path/to/repo
	.venv-agentd/bin/ezai plan "$(TASK)" --repo "$(REPO)"

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
