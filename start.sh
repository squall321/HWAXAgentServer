#!/usr/bin/env bash
# HWAX Agent Server — dev launcher. Points at the local vLLM (see HWAXPortal/docs/dev-vllm-setup.md).
set -euo pipefail
cd "$(dirname "$0")"

# Per-box overrides (VLLM_BASE_URL / VLLM_MODEL) live in a gitignored .env next to this script;
# source it first so its values win over the defaults below.
# 관대하게 소싱 — 한 줄이 잘못돼도(등호 양옆 공백·오타 등) errexit 로 기동 전체가 죽지 않게
# errexit 를 잠깐 끈다. 나쁜 줄은 건너뛰고 그 변수는 아래 기본값으로 진행(서버는 뜬다).
if [ -f .env ]; then
  set +e
  set -a; . ./.env; set +a
  set -e
fi

PORT="${AGENT_PORT:-9009}"                                   # 9000 is taken by MinIO on this box
export VLLM_BASE_URL="${VLLM_BASE_URL:-http://127.0.0.1:8000/v1}"
export VLLM_MODEL="${VLLM_MODEL:-qwen2.5-7b-dev}"
# MCP_CONFIG 기본값 — 없으면 서버가 mcp:[] 로 떠 도구(심의 페르소나 발굴 등)가 전부 소실된다.
# 서비스 매니페스트(services.yaml)는 이 값을 주입하지만 맨손 ./start.sh 는 .env 에만 의존했다 —
# .env 에 MCP_CONFIG 가 없는 박스에서 맨손 재시작 시 도구가 안 떠 심의가 실패하던 함정 제거.
# cd(위)로 cwd=레포디렉토리라 상대경로 mcp_servers.json 이 곧 이 레포의 파일이다.
export MCP_CONFIG="${MCP_CONFIG:-$(pwd)/mcp_servers.json}"

[ -d .venv ] || python3 -m venv .venv
# best-effort — 매 기동마다 도는데 cae00 사내 프록시가 공개 레지스트리를 막으면 pip 이 실패한다.
# set -e 하에서 그게 기동을 죽이지 않게(이미 설치된 venv 로 진행). deps 가 진짜 없으면 아래
# uvicorn import 가 크게 실패해 드러난다 — 조용한 재시작 실패만 막는다.
.venv/bin/pip install -q -r requirements.txt || echo "==> ⚠ pip install 실패 — 기존 venv 로 진행(오프라인/프록시 가능)"

# 재시작 겸용 — 이 포트를 잡은 기존 인스턴스를 먼저 내려야 bind 된다(안 그러면 'address already
# in use'). 포트 리스너 PID 를 직접 종료한다 — uvicorn 이 상대경로(.venv/bin/uvicorn)로 실행돼
# cmdline 절대경로 pkill 패턴이 빗나가던 것 방지(포트는 우리가 실제로 비워야 하는 대상 그 자체).
port_pids() { ss -ltnp 2>/dev/null | grep ":${PORT} " | grep -oP 'pid=\K[0-9]+' | sort -u; }
OLD_PIDS="$(port_pids || true)"
if [ -n "$OLD_PIDS" ]; then
  echo "==> stopping previous instance (${OLD_PIDS//$'\n'/ })"
  kill $OLD_PIDS 2>/dev/null || true
  sleep 2
  STILL="$(port_pids || true)"
  if [ -n "$STILL" ]; then kill -9 $STILL 2>/dev/null || true; sleep 1; fi   # 안 죽었으면 강제
fi

echo "==> Agent Server on :${PORT}  (vLLM=${VLLM_BASE_URL}, model=${VLLM_MODEL})"
# 기본은 포그라운드(exec). '-d'/'--daemon' 이면 nohup 백그라운드로 띄우고 즉시 반환한다
# (SSH 끊겨도 유지). 로그는 AGENT_LOG(기본 ./agent-server.log).
if [ "${1:-}" = "-d" ] || [ "${1:-}" = "--daemon" ]; then
  LOG="${AGENT_LOG:-$(pwd)/agent-server.log}"
  nohup .venv/bin/uvicorn app:app --host 0.0.0.0 --port "$PORT" >"$LOG" 2>&1 &
  NEWPID=$!
  # 기동 자체 검증 — 포트가 타 유저 프로세스에 잡혀 bind 실패하면 nohup 이 조용히 죽는다.
  # '떴다'고 오인하지 않게(전에 stale 서버가 계속 응답하던 그 부류) 실제 리슨을 확인한다.
  # 최대 ~12초 재시도(느린 박스·pip 이후 기동 대비), 프로세스가 죽으면 즉시 실패 판정.
  started=0
  for _ in 1 2 3 4 5 6; do
    sleep 2
    if ! kill -0 "$NEWPID" 2>/dev/null; then break; fi   # 프로세스 사망 → 더 기다릴 것 없음
    if [ -n "$(port_pids || true)" ]; then started=1; break; fi
  done
  if [ "$started" = 1 ]; then
    echo "==> started in background — pid=$NEWPID log=$LOG"
  else
    echo "==> ⚠ 기동 실패(포트 bind 불가 또는 즉시 종료) — 로그 마지막 20줄:"
    tail -n 20 "$LOG" 2>/dev/null || true
    exit 1
  fi
else
  exec .venv/bin/uvicorn app:app --host 0.0.0.0 --port "$PORT"
fi
