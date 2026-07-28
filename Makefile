.PHONY: help setup build pull up up-cpu pull-cpu download-cpu update-cpu \
        up-n97 pull-n97 download-n97 update-n97 \
        down restart logs health status embed \
        k8s k8s-delete update clean slurm-setup push-github monitor

# Load .env if it exists
-include .env
export

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

up-cpu: ## Start with vLLM on CPU (no GPU; slower than up-n97 on AVX2-only boxes)
	docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d
	@echo ""
	@echo "  vLLM CPU mode: http://localhost:$(or $(OPENWEBUI_PORT),3000)"
	@echo "  vLLM takes 1-3 minutes to load the model — run 'make health' after"
	@echo ""

pull-cpu: ## Pull images for the vLLM CPU stack
	docker compose -f docker-compose.yml -f docker-compose.cpu.yml pull openwebui litellm vllm qdrant searxng

download-cpu: ## Download models for the vLLM CPU stack (~3.6 GB)
	@. ~/ai-env/bin/activate && \
	huggingface-cli download $(or $(CPU_CHAT_MODEL),Qwen/Qwen2.5-1.5B-Instruct) --cache-dir $(or $(MODELS_DIR),~/ai-models/hf-cache) && \
	huggingface-cli download nomic-ai/nomic-embed-text-v1.5 --cache-dir $(or $(MODELS_DIR),~/ai-models/hf-cache)

update-cpu: ## Pull latest images and restart the vLLM CPU stack (do NOT use 'make update')
	docker compose -f docker-compose.yml -f docker-compose.cpu.yml pull
	docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d

up-n97: ## Start all services tuned for Intel N97 / low-power mini PCs (see docs/DEPLOY-N97.md)
	docker compose -f docker-compose.yml -f docker-compose.n97.yml up -d
	@echo ""
	@echo "  N97 mode (llama.cpp): http://localhost:3000"
	@echo "  First start loads the model — run 'make health' in ~1 minute"
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

status: ## Show status of all containers
	docker compose ps

embed: ## Embed documents from ~/documents into Qdrant knowledge base
	@echo "Embedding documents from ~/documents..."
	@. ~/ai-env/bin/activate && python3 scripts/embed_documents.py \
		--input-dir $(DOCUMENTS_DIR) \
		--qdrant-url http://localhost:$(or $(QDRANT_PORT),6333) \
		--embed-url http://localhost:$(or $(EMBED_PORT),8001)/v1 \
		--collection $(RAG_COLLECTION)

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
