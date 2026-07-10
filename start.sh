#!/usr/bin/env bash
# HWAX Agent Server — dev launcher. Points at the local vLLM (see HWAXPortal/docs/dev-vllm-setup.md).
set -euo pipefail
cd "$(dirname "$0")"

# Per-box overrides (VLLM_BASE_URL / VLLM_MODEL) live in a gitignored .env next to this script;
# source it first so its values win over the defaults below.
[ -f .env ] && { set -a; . ./.env; set +a; }

PORT="${AGENT_PORT:-9009}"                                   # 9000 is taken by MinIO on this box
export VLLM_BASE_URL="${VLLM_BASE_URL:-http://127.0.0.1:8000/v1}"
export VLLM_MODEL="${VLLM_MODEL:-qwen2.5-7b-dev}"

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

echo "==> Agent Server on :${PORT}  (vLLM=${VLLM_BASE_URL}, model=${VLLM_MODEL})"
exec .venv/bin/uvicorn app:app --host 0.0.0.0 --port "$PORT"
