#!/bin/bash
#SBATCH --job-name=embed-docs
#SBATCH --partition=cpu
#SBATCH --cpus-per-task=8
#SBATCH --mem=16G
#SBATCH --time=04:00:00
#SBATCH --output=/tmp/embed-%j.log

echo "=== Document embedding started: $(date) ==="
source ~/ai-env/bin/activate
python3 ~/local-ezai/scripts/embed_documents.py \
  --input-dir ~/documents \
  --qdrant-url http://localhost:6333 \
  --embed-url http://localhost:8001/v1 \
  --collection my-knowledge-base
echo "=== Done: $(date) ==="
