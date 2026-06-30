#!/bin/bash
#SBATCH --job-name=ai-test
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=00:02:00
#SBATCH --output=/tmp/ai-test-%j.log

echo "=== Job started: $(date) on $(hostname) ==="
echo "=== GPU info ==="
nvidia-smi
echo "=== Done ==="
