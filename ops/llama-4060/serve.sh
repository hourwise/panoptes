#!/bin/bash
# Serve one GGUF model on the RTX 4060 Ti ONLY (CUDA device 0), loopback only, for Panoptes worker 2.
#   serve.sh [model.gguf] [context]      env KV=q8_0 quantises the KV cache (dense models on 16 GB)
# Defaults: gpt-oss-20b MXFP4, 32k context. The R9700 is not visible to this CUDA build, so Radiance is never touched.
set -euo pipefail
MODEL="${1:-/mnt/models/gguf/gpt-oss-20b-MXFP4.gguf}"
CTX="${2:-32768}"
BIN="$HOME/Applications/llama.cpp/build-cuda/bin/llama-server"
KVARGS=()
[ -n "${KV:-}" ] && KVARGS=(-ctk "$KV" -ctv "$KV")
export CUDA_VISIBLE_DEVICES=0
exec "$BIN" -m "$MODEL" --host 127.0.0.1 --port 8001 \
  -ngl 99 -c "$CTX" -np 1 -fa on --jinja -b 2048 -ub 2048 \
  "${KVARGS[@]}" --metrics --slots --no-webui \
  --alias "$(basename "$MODEL" .gguf)"
