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

# 재시작 겸용 — 이 디렉토리의 기존 인스턴스를 먼저 내려야 포트 bind 가 된다(안 그러면
# 'address already in use'). pkill 패턴은 이 박스의 HWAXAgentServer uvicorn 만 정확히 겨냥한다.
STOP_PAT="$(pwd)/.venv/bin/uvicorn"
if pkill -f "$STOP_PAT" 2>/dev/null; then
  echo "==> stopping previous instance"; sleep 2
  pkill -9 -f "$STOP_PAT" 2>/dev/null || true   # 안 죽었으면 강제
fi

echo "==> Agent Server on :${PORT}  (vLLM=${VLLM_BASE_URL}, model=${VLLM_MODEL})"
# 기본은 포그라운드(exec). '-d'/'--daemon' 이면 nohup 백그라운드로 띄우고 즉시 반환한다
# (SSH 끊겨도 유지). 로그는 AGENT_LOG(기본 ./agent-server.log).
if [ "${1:-}" = "-d" ] || [ "${1:-}" = "--daemon" ]; then
  LOG="${AGENT_LOG:-$(pwd)/agent-server.log}"
  nohup .venv/bin/uvicorn app:app --host 0.0.0.0 --port "$PORT" >"$LOG" 2>&1 &
  echo "==> started in background — pid=$! log=$LOG"
else
  exec .venv/bin/uvicorn app:app --host 0.0.0.0 --port "$PORT"
fi
