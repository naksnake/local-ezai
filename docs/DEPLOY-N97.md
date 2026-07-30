# Deploying on an Intel N97 mini PC (16 GB RAM, no GPU)

This guide covers running local-ezai on a low-power mini PC such as a
**Limyee Intel N97 / 16 GB DDR5 / 1 TB SSD** box. The standard stack assumes
an NVIDIA GPU and 32 GB+ RAM, so this profile swaps the inference engine and
right-sizes every service.

```
make up          → needs NVIDIA GPU          ✗ N97 has none
make up-cpu      → vLLM engine on CPU        △ works, but slower here (see barriers)
make up-n97      → llama.cpp + 3B Q4 model   ✓ recommended — this guide
```

---

## Your hardware vs. the standard requirements

| | Standard minimum | Intel N97 box | Impact |
|--|-----------------|---------------|--------|
| CPU | 8 cores | 4 E-cores (no hyper-threading) | slower inference, thread limits needed |
| RAM | 32 GB | 16 GB (single-channel DDR5) | small quantized model only |
| GPU | NVIDIA 8 GB VRAM | Intel UHD iGPU only | no vLLM/CUDA — use llama.cpp |
| Disk | 200 GB SSD | 1 TB SSD | ✓ no problem |

---

## Barriers you need to be aware of

### 1. No NVIDIA GPU — vLLM cannot run at all

The default stack serves the LLM with the `vllm/vllm-openai` Docker image,
which is a **CUDA build**. Without an NVIDIA GPU and the NVIDIA container
runtime the container will not start (`could not select device driver
"nvidia"`). This rules out `make up`.

### 2. No AVX-512 — vLLM runs on this CPU, but as a second-class citizen

vLLM's CPU image (`vllm/vllm-openai-cpu`, wired up as `make up-cpu`) does
start on the N97, but its optimized x86 kernels key off **AVX-512**; on an
AVX2-only chip like the N97 (Alder Lake-N) it runs in vLLM's officially
documented "limited features" mode — slower and heavier than a purpose-built
AVX2 engine. The best-performing CPU inference engine for this class of
hardware is **llama.cpp**, which is what the N97 profile deploys — same
OpenAI-compatible API, so LiteLLM, OpenWebUI, and the MCP tools all work
unchanged. If you specifically need vLLM, see
[Using vLLM instead of llama.cpp](#using-vllm-instead-of-llamacpp) below.

### 3. 16 GB RAM shared by everything — the 7B model doesn't fit

Qwen2.5-7B-Instruct in FP16 is ~15 GB of weights alone; the OS, Docker, and
seven other services also live in your 16 GB. The N97 profile uses
**Qwen2.5-1.5B-Instruct quantized to 4-bit (Q4_K_M GGUF, ~1.1 GB)** and puts a
memory limit on every container. Rough steady-state budget:

| Component | RAM |
|-----------|-----|
| llama.cpp (3B Q4 + 8k context) | ~3.5 GB |
| embed-server (PyTorch + nomic-embed) | ~1.5–2 GB |
| OpenWebUI | ~0.5–1 GB |
| LiteLLM + Qdrant + SearXNG + mcpo + monitor | ~1.5 GB |
| Ubuntu + Docker | ~1.5–2 GB |
| **Total** | **~8–10 GB** — comfortable in 16 GB |

Add a swap file (or zram) as a safety net so a spike degrades instead of
OOM-killing containers:

```bash
sudo fallocate -l 8G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### 4. Single-channel memory bandwidth caps your speed

Token generation is memory-bandwidth-bound. The N97's single-channel
DDR5-4800 gives ~38 GB/s theoretical (~25–30 GB/s real), so expect roughly:

- **Qwen2.5-1.5B Q4_K_M: ~12-20 tokens/sec** generation (3B optional: ~6-10 tok/s, better quality)
- Qwen2.5-1.5B Q4_K_M: ~12–16 tokens/sec — snappier, noticeably less capable
- 7B Q4: ~3–4 tokens/sec — technically fits, painful in practice

Prompt processing (reading your input + RAG context) is compute-bound on the
4 E-cores and is what you'll actually feel: with a couple of thousand tokens
of RAG context, end-to-end throughput can drop to a fraction of the raw
generation rate. The default is 8192 — OpenWebUI sends the mcpo tool schemas (~2-3k tokens) with every request, so 4096 overflows in normal tool-enabled chats. Drop to 4096 only if you disable tools and need the RAM back.

Adding more RAM won't speed generation up (bandwidth, not capacity, is the
limit), and the iGPU shares the same memory bus, so GPU offload doesn't help
generation either. The one real iGPU win is prefill: llama.cpp's Vulkan
backend processes prompts ~2–3× faster than the 4 E-cores. The profile keeps
everything on CPU for simplicity; if long-context prefill becomes your
bottleneck, the `:server-vulkan` image variant is the upgrade path to
experiment with.

### 5. Thermals — fanless/industrial cases throttle under sustained load

Limyee boxes are typically fanless. LLM generation pins all 4 cores at 100%
for the duration of each response; a heat-soaked passive case will clock down
and your tokens/sec sink over a long session. Give the box airflow, and check
under load:

```bash
sudo apt install lm-sensors && sensors    # watch CPU temp during a chat
grep MHz /proc/cpuinfo                     # clocks dropping = throttling
```

### 6. One user at a time

llama.cpp runs with `--parallel 1`. Two simultaneous chats will queue, and
each background feature multiplies load. In OpenWebUI (**Admin Panel →
Settings → Interface**) it's worth disabling automatic **title generation**
and **tag generation** — each one is an extra LLM call per message on a
machine with no headroom.

### 7. Model licensing

Qwen2.5-**3B** is under the **Qwen Research License** (non-commercial). Fine
for personal/home-lab use; for anything commercial switch to Qwen2.5-**1.5B**
(Apache-2.0) — see "Choosing a different model" below.

### 8. What still works fine

Everything except raw LLM speed: Qdrant, SearXNG, the embedding server, MCP
tools (filesystem / memory / fetch / RAG), and the monitor dashboard are all
lightweight and run happily on this hardware. The 1 TB SSD is far more than
the ~3 GB of models this profile needs.

---

## Step-by-step deployment

### 1. System setup

Ubuntu 24.04 or 26.04 LTS (Desktop or Server) on the N97 box, then:

```bash
git clone https://github.com/naksnake/local-ezai.git
cd local-ezai
bash scripts/setup.sh
```

The script detects that there is no NVIDIA GPU and skips all the
NVIDIA/CUDA steps automatically. Log out and back in (or `newgrp docker`)
so your user picks up the `docker` group.

### 2. Configure

```bash
cp .env.example .env
nano .env
```

Change `LITELLM_MASTER_KEY`, `WEBUI_SECRET_KEY`, `SEARXNG_SECRET`
(`openssl rand -hex 32` for each). The N97 defaults at the bottom of the
file are fine as-is.

### 3. Download the small model set (~2.5 GB total)

```bash
make download-n97
```

### 4. Build and start

```bash
make build       # embed-server, mcpo, monitor images
make pull-n97    # pulls llama.cpp instead of vLLM
make up-n97
```

### 5. Verify

The 3B model loads from SSD in well under a minute (vs. minutes for vLLM):

```bash
make health      # all 8 checks should pass
```

Then open **http://localhost:3000**, create your admin account, and pick
`qwen2.5-1.5b` in the model dropdown. The monitor at **http://localhost:8888**
will show the LLM card as "llama.cpp".

Continue from **step 7 (Connect MCP agent tools)** of the main README —
those steps are identical.

---

## Choosing a different model

1. Set these in `.env` (example — faster + Apache-2.0 licensed, recommended
   if 3B feels sluggish):

   ```bash
   N97_GGUF_REPO=Qwen/Qwen2.5-1.5B-Instruct-GGUF
   N97_MODEL_FILE=qwen2.5-1.5b-instruct-q4_k_m.gguf
   N97_MODEL_NAME=qwen2.5-1.5b
   ```

2. `make download-n97`
3. Edit `config/litellm-config.n97.yaml` so `model_name` (and the
   `openai/...` value) match the new `N97_MODEL_NAME`
4. `make up-n97` — recreates the llama.cpp container with the new model
5. `docker compose restart litellm` — LiteLLM reads its config only at
   startup, so without this the new model name never appears in OpenWebUI

Any single-file GGUF from HuggingFace works the same way. Stay at or below
~3B parameters at Q4 on this hardware.

## Updating

Use `make update-n97`, **not** `make update` — the plain `update` target runs
against the base compose file only, so it would pull the multi-GB CUDA vLLM
image and recreate the LLM container from the GPU definition, which cannot
start on this machine.

## Using vLLM instead of llama.cpp

If you need the actual vLLM engine on this box (API parity with a GPU
deployment, vLLM-specific behavior, staging before moving to GPU hardware),
the `up-cpu` profile runs the official `vllm/vllm-openai-cpu` image with
Qwen2.5-1.5B in bf16:

```bash
make download-cpu   # Qwen2.5-1.5B safetensors + embeddings (~3.6 GB)
make pull-cpu
make up-cpu         # instead of up-n97 — stop the other profile first: make down
make health
```

The chat model appears as `qwen2.5-1.5b` in OpenWebUI. Knobs in `.env`:
`CPU_CHAT_MODEL`, `CPU_CHAT_MODEL_NAME`, `CPU_MAX_MODEL_LEN` (default 2048),
`VLLM_CPU_KVCACHE_SPACE` (GiB of RAM for KV cache, default 2 here).

What to expect on the N97 vs. the llama.cpp profile:

- **Slower.** vLLM's AVX2 path is its documented "limited features" mode,
  and the model runs in bf16 (~3.1 GB weights) instead of 4-bit (~1 GB for
  the same 1.5B) — on a memory-bandwidth-bound machine that alone costs ~3×
  in generation speed. Expect a few tokens/sec.
- **Heavier.** Weights + KV cache + vLLM's Python runtime put the container
  around 6–8 GB (capped at 10 GB), vs ~3 GB for llama.cpp with a larger 3B
  model.
- **Slower startup.** vLLM initializes in 1–3 minutes; llama.cpp loads in
  seconds.

Run one LLM profile at a time (`make down` first) — both bind the same
`vllm` service name and LLM port.

## Port conflicts with other containers

Already running other services on the box (lab containers, dashboards,
etc.)? Every published host port is configurable in `.env` — the stack's
defaults are 3000, 4000, 6333, 8000, 8001, 8090, 8200, and 8888. Only the
host side changes; inter-service traffic uses the internal Docker network
and is unaffected. Example: if 8090 is taken (the SearXNG default), set:

```bash
SEARXNG_PORT=8095
```

then `make up-n97` (or `up-cpu`) again. `make health` and the monitor
dashboard's links pick up the overrides automatically.

## Tuning knobs

| `.env` variable | Default | Notes |
|-----------------|---------|-------|
| `N97_CTX` | `8192` | Context window; more = more RAM + slower prefill |
| `N97_THREADS` | `4` | Match the 4 physical cores; don't oversubscribe |
| `N97_GGUF_DIR` | `./models/gguf` | Host folder mounted into the container |

## Troubleshooting (N97-specific)

| Symptom | Fix |
|---------|-----|
| LLM container exits immediately | `docker compose logs vllm` — usually a wrong `N97_MODEL_FILE` path/name |
| LLM broke after `make update` | `update` reverts to the GPU stack — run `make up-n97` to recover, and use `make update-n97` from now on |
| Model switch not showing in OpenWebUI | LiteLLM only reads its config at startup — `docker compose restart litellm` |
| `docker compose` errors on `!override` | Docker Compose too old — needs ≥ 2.24.4; `docker compose version`, then update docker-ce |
| Everything crawls after ~10 min of use | Thermal throttling — check `sensors`; improve case airflow |
| Container killed (`Exited (137)`) | Memory limit hit — check `docker stats`; use the 1.5B model or raise the limit in `docker-compose.n97.yml` |
| First token takes ages on long chats | Prefill is CPU-bound; shorten context, disable OpenWebUI title/tag generation |
