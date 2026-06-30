#!/usr/bin/env bash
# scripts/setup.sh
# ─────────────────────────────────────────────────────────────────────────────
# Automated system setup for Ubuntu 24.04.4 LTS
# Run once on a fresh machine before launching the AI service.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()    { echo -e "\n${BLUE}══ $* ══${NC}"; }

# ── Header ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║     AI Service — System Setup            ║${NC}"
echo -e "${BLUE}║     Ubuntu 24.04.4 LTS                   ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════╝${NC}"
echo ""

# ── Check Ubuntu version ──────────────────────────────────────────────────────
step "Checking Ubuntu version"
OS_VERSION=$(lsb_release -rs 2>/dev/null || echo "unknown")
OS_CODENAME=$(lsb_release -cs 2>/dev/null || echo "unknown")

if [[ "$OS_VERSION" != "24.04" ]]; then
    warn "This script is designed for Ubuntu 24.04. You have $OS_VERSION."
    warn "Proceeding anyway — some steps may need adjustments."
else
    success "Ubuntu $OS_VERSION ($OS_CODENAME) — supported"
fi

# ── Update system ──────────────────────────────────────────────────────────────
step "Updating system packages"
sudo apt-get update -qq
sudo apt-get upgrade -y -qq
sudo apt-get install -y -qq \
    curl wget git htop nano net-tools build-essential \
    ca-certificates gnupg lsb-release apt-transport-https
success "System packages updated"

# ── Install Docker ────────────────────────────────────────────────────────────
step "Installing Docker Engine"
if command -v docker &>/dev/null; then
    success "Docker already installed: $(docker --version)"
else
    # Remove old versions
    sudo apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true

    # Add Docker GPG key
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg

    # Add Docker repository
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/ubuntu ${OS_CODENAME} stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

    sudo apt-get update -qq
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin

    # Add user to docker group
    sudo usermod -aG docker "$USER"
    success "Docker installed: $(docker --version)"
    warn "Log out and back in (or run 'newgrp docker') for docker group to take effect"
fi

# ── Detect NVIDIA GPU ─────────────────────────────────────────────────────────
step "Checking for NVIDIA GPU"
if lspci 2>/dev/null | grep -qi nvidia; then
    GPU_NAME=$(lspci | grep -i nvidia | head -1 | sed 's/.*\[//;s/\].*//')
    success "NVIDIA GPU detected: $GPU_NAME"
    HAS_GPU=true
else
    warn "No NVIDIA GPU detected — will set up CPU-only mode"
    HAS_GPU=false
fi

# ── Install NVIDIA driver ─────────────────────────────────────────────────────
if [[ "$HAS_GPU" == "true" ]]; then
    step "Installing NVIDIA driver"
    if nvidia-smi &>/dev/null; then
        success "NVIDIA driver already installed: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader)"
    else
        sudo apt-get install -y ubuntu-drivers-common
        sudo ubuntu-drivers autoinstall
        success "NVIDIA driver installed — REBOOT REQUIRED"
        warn "Run: sudo reboot — then re-run this script to continue"
        exit 0
    fi

    step "Installing NVIDIA Container Toolkit"
    if dpkg -l nvidia-container-toolkit &>/dev/null; then
        success "NVIDIA Container Toolkit already installed"
    else
        curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
            | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

        curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
            | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
            | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

        sudo apt-get update -qq
        sudo apt-get install -y nvidia-container-toolkit
        sudo nvidia-ctk runtime configure --runtime=docker
        sudo systemctl restart docker
        success "NVIDIA Container Toolkit installed"
    fi
fi

# ── Install Python venv ────────────────────────────────────────────────────────
step "Setting up Python virtual environment"
sudo apt-get install -y python3-pip python3-venv python3-full -qq

if [[ -d "$HOME/ai-env" ]]; then
    success "Python venv already exists at ~/ai-env"
else
    python3 -m venv "$HOME/ai-env"
    success "Created Python venv at ~/ai-env"
fi

# Activate and install packages
source "$HOME/ai-env/bin/activate"
pip install -q --upgrade pip
pip install -q huggingface_hub sentence-transformers qdrant-client
success "Python packages installed in venv"

# Add to .bashrc if not already there
if ! grep -q "ai-env/bin/activate" "$HOME/.bashrc"; then
    echo 'source ~/ai-env/bin/activate' >> "$HOME/.bashrc"
    success "Added venv auto-activation to ~/.bashrc"
fi

# ── Install Node.js 20 ────────────────────────────────────────────────────────
step "Installing Node.js 20 LTS"
if node --version 2>/dev/null | grep -q "v20"; then
    success "Node.js already installed: $(node --version)"
else
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - -qq
    sudo apt-get install -y nodejs -qq
    success "Node.js installed: $(node --version)"
fi

# ── Create required directories ───────────────────────────────────────────────
step "Creating project directories"
mkdir -p "$HOME/ai-models/hf-cache"
mkdir -p "$HOME/documents"
success "Directories created: ~/ai-models/hf-cache, ~/documents"

# ── Create .env from template ─────────────────────────────────────────────────
step "Setting up environment configuration"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -f "$SCRIPT_DIR/.env" ]]; then
    success ".env already exists — skipping"
else
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    success "Created .env from .env.example"
    warn "⚠️  Edit .env and change the secret keys before starting the service"
    warn "    nano $SCRIPT_DIR/.env"
fi

# ── Final summary ─────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Setup complete!                                         ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Edit your config:     nano .env"
echo "  2. Download AI models:   bash scripts/download-models.sh"
echo "  3. Build images:         make build"
echo "  4. Pull images:          make pull"
echo "  5. Start services:       make up"
echo "  6. Health check:         make health"
echo ""

if [[ "$HAS_GPU" == "true" ]]; then
    echo -e "  ${GREEN}GPU mode:${NC} vLLM will run on your NVIDIA GPU"
else
    echo -e "  ${YELLOW}CPU mode:${NC} Use 'make up-cpu' (slower but works without GPU)"
fi
echo ""
