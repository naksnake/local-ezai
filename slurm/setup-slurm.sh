#!/usr/bin/env bash
# slurm/setup-slurm.sh — Automated Slurm single-node setup for Ubuntu 24.04
set -euo pipefail

GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}   $*"; }

info "Installing Slurm and Munge"
sudo apt-get install -y slurmctld slurmd slurm-client munge

info "Generating Munge key"
sudo /usr/sbin/create-munge-key -f
sudo systemctl enable munge --now

info "Testing Munge"
munge -n | unmunge || { echo "Munge test failed!"; exit 1; }
ok "Munge working"

info "Creating Slurm directories"
sudo mkdir -p /etc/slurm /var/lib/slurm/{slurmctld,slurmd} /var/log/slurm
sudo chown slurm:slurm /var/lib/slurm/slurmctld /var/lib/slurm/slurmd /var/log/slurm

HOSTNAME=$(hostname)
CPUS=$(nproc)
MEM=$(free -m | awk '/^Mem:/{print int($2*0.95)}')

info "Writing slurm.conf (Host: $HOSTNAME | CPUs: $CPUS | Mem: ${MEM}MB)"
sudo tee /etc/slurm/slurm.conf > /dev/null << EOF
ClusterName=ai-cluster
SlurmctldHost=${HOSTNAME}
AuthType=auth/munge
SchedulerType=sched/backfill
SelectType=select/cons_tres
SelectTypeParameters=CR_Core_Memory
SlurmctldLogFile=/var/log/slurm/slurmctld.log
SlurmdLogFile=/var/log/slurm/slurmd.log
SlurmctldPidFile=/var/run/slurmctld.pid
SlurmdPidFile=/var/run/slurmd.pid
StateSaveLocation=/var/lib/slurm/slurmctld
SlurmdSpoolDir=/var/lib/slurm/slurmd
SlurmctldPort=6817
SlurmdPort=6818
NodeName=${HOSTNAME} CPUs=${CPUS} RealMemory=${MEM} Gres=gpu:1 State=UNKNOWN
PartitionName=gpu Nodes=${HOSTNAME} Default=YES MaxTime=INFINITE State=UP
PartitionName=cpu Nodes=${HOSTNAME} MaxTime=INFINITE State=UP
EOF

if lspci 2>/dev/null | grep -qi nvidia; then
    info "Writing gres.conf for NVIDIA GPU"
    sudo tee /etc/slurm/gres.conf > /dev/null << 'EOF'
Name=gpu Type=nvidia File=/dev/nvidia0
EOF
fi

info "Starting Slurm services"
sudo systemctl enable slurmctld slurmd --now

sleep 3
ok "Slurm installed. Verify with: sinfo"
