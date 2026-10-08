#!/usr/bin/env bash
# HWAX Agent Server — dev launcher. Points at the local vLLM (see HWAXPortal/docs/dev-vllm-setup.md).
set -euo pipefail
cd "$(dirname "$0")"

# Per-box overrides (VLLM_BASE_URL / VLLM_MODEL) live in a gitignored .env next to this script;
# source it first so its values win over the defaults below.
# 관대하게 소싱 — 한 줄이 잘못돼도(등호 양옆 공백·오타 등) errexit 로 기동 전체가 죽지 않게
# errexit 를 잠깐 끈다. 나쁜 줄은 건너뛰고 그 변수는 아래 기본값으로 진행(서버는 뜬다).
if [ -f .env ]; then
  # `. ./.env` 는 '나쁜 줄만 건너뛰지' 않는다. 따옴표 불균형 같은 파스 에러가 있으면 그 줄
  # 이후가 통째로 버려진다(실측: 2행에 열린 따옴표 → 3·4행이 미설정). 그러면 아래 @FROM_RA
  # 마커 가드가 검사할 VLLM_BASE_URL 자체가 없어 가드까지 무력화되고, 서버는 기본값으로 떠서
  # 왜 상암 GLM 이 아니라 로컬을 보는지 알 수 없게 된다.
  # KEY=VALUE 로 읽히는 줄만 골라 export 하고, 건너뛴 줄은 말한다.
  _skipped=0
  while IFS= read -r _line || [ -n "$_line" ]; do
    case "$_line" in ''|'#'*) continue ;; esac
    if printf '%s' "$_line" | grep -qE '^[A-Za-z_][A-Za-z0-9_]*='; then
      _k="${_line%%=*}"; _v="${_line#*=}"
      # 양끝 따옴표만 벗긴다(값 안의 따옴표는 그대로 둔다)
      _q1='"'; _q2="'"
      case "$_v" in
        "$_q1"*"$_q1") _v="${_v#$_q1}"; _v="${_v%$_q1}" ;;
        "$_q2"*"$_q2") _v="${_v#$_q2}"; _v="${_v%$_q2}" ;;
      esac
      export "$_k=$_v"
    else
      _skipped=$((_skipped+1))
      echo "  ⚠ .env 형식 아님 — 건너뜀: $(printf '%.60s' "$_line")" >&2
    fi
  done < .env
  [ "$_skipped" -gt 0 ] && echo "  ⚠ .env 에서 ${_skipped}줄을 건너뛰었다 — 그 키들은 기본값으로 진행한다" >&2
fi

PORT="${AGENT_PORT:-9009}"                                   # 9000 is taken by MinIO on this box
# ⚠ 루프백에 묶는다. 이 서버에는 인증이 없다 — 엔드포인트에 Depends 가 하나도 없고,
# 인가에 쓰는 groups·user_email 을 요청 본문에서 받아 그대로 게이트웨이에 신원으로 싣는다
# (app.py 의 _with_groups). 게이트웨이는 GW_TOKEN 분기에서 그 헤더를 "내부 에이전트"로
# 신뢰한다. 즉 9009 에 닿는 누구든 로그인 없이 임의 사내 이메일을 자칭해 도구를 돌릴 수 있다.
#
# 초기 커밋(3dd6676, dev minimal)부터 --host 0.0.0.0 이 하드코딩돼 있었다. 이 박스는
# enp13s0 에 공인 IP 가 직접 붙어 있고 iptables INPUT policy 가 ACCEPT 라, 실측으로
# 공인 IP:9009/health 가 자격증명 없이 200 을 줬다. 게이트웨이는 같은 문제를
# GW.get("host","127.0.0.1") 로 이미 막아 뒀다 — 원칙이 한쪽에만 적용돼 있었다.
#
# 소비자는 전부 같은 박스다(포털 config.py agent_server_url, services.yaml health,
# update-all·verify-e2e 프로브 — 전수 확인 결과 모두 127.0.0.1/localhost). 다른 박스에서
# 붙여야 하면 AGENT_HOST 로 열되, 그때는 이 서버 앞에 인증을 먼저 두어야 한다.
HOST="${AGENT_HOST:-127.0.0.1}"
export VLLM_BASE_URL="${VLLM_BASE_URL:-http://127.0.0.1:8000/v1}"
export VLLM_MODEL="${VLLM_MODEL:-qwen2.5-7b-dev}"
# 챗 **스트리밍** 응답에서 청크 사이 침묵을 기다리는 한도(초 — 첫 토큰 대기도 여기 든다). langchain-openai 의
# 기본은 120 이고 어디에도 안 적혀 있었다. 좌석 많은 심의가 공유 LLM 을 수 시간 차지하면 챗의 첫 토큰이 큐에서
# 120초를 넘겨 끊긴다 — 300 으로 넉넉히 둔다. 심의 경로는 비스트리밍이라 이 값과 무관하다(DELIB_TIMEOUT_S).
export LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S="${LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S:-300}"
# 미치환 마커 가드 — .env 가 apply-envs.sh 치환 없이 킷을 그대로 복사·수동편집돼 @FROM_RA:...@
# 마커가 남으면, 서버는 그 엉터리 주소로 vLLM 에 붙으려다 매 요청 APIConnectionError 로 죽는다.
# cryptic 한 연결 에러 대신 기동 시 즉시·명확히 멈춘다(값 교체 방법도 안내).
case "$VLLM_BASE_URL $VLLM_MODEL ${VLLM_API_KEY:-}" in
  *@FROM_RA:*|*@GENERATE_*)
    echo "==> ⚠ vLLM 설정에 미치환 마커가 있습니다 — 서버를 띄우지 않습니다."
    echo "    VLLM_BASE_URL=$VLLM_BASE_URL"
    echo "    VLLM_MODEL=$VLLM_MODEL"
    echo "    조치: .env 의 @FROM_RA:...@ 를 ReportArchive/.env 의 실제 LLM_* 값으로 바꾸거나,"
    echo "          그 줄들을 지우고 'apply-envs.sh agent-server' 재실행(RA 값으로 치환)."
    exit 1 ;;
esac
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
# 초로 읽는 손잡이 둘(AGENT_STOP_GRACE_S · AGENT_HEALTH_PROBE_S)은 쓰기 전에 숫자인지 본다 — 아니면 경고하고
# 기본값으로 돈다(파이썬이 읽는 손잡이의 _env_int 와 같은 규칙이고, 킷이 그렇게 적는다). 위 .env 로더는 `=` 뒤를
# 통째로 값으로 읽어 줄 끝 설명·끝 공백이 값에 남는다. 종전엔 그 값을 그대로 넘겼다 —
#  · sleep 이 실패하면 set -e 가 **옛 서버를 내린 직후** 스크립트를 끝냈다(옛 것은 죽고 새 것은 안 떴다).
#  · curl 이 --max-time 을 거절하면 답이 비고, 빈 답은 '모름' 이라 도는 심의를 끊고 재기동했다 — 바쁜 서버를 더
#    기다리려고 올린 값이 보호를 껐다.
# ⚠ .env 로더는 고치지 않는다 — 값에 `#` 가 들어갈 수 있다(URL·키). 숫자여야 하는 자리에서만 본다.
secs_or() {   # secs_or <이름> <읽은 값> <기본값> — 쓸 값을 찍는다
  case "$2" in
    ''|.|*[!0-9.]*|*.*.*)
      echo "  ⚠ $1='$(printf '%.40s' "$2")' 를 초로 읽지 못했다 — 기본값 ${3}초로 돈다(.env 의 값 줄 끝에 설명·공백·단위를 두지 않는다)" >&2
      printf '%s' "$3" ;;
    *) printf '%s' "$2" ;;
  esac
}
STOP_GRACE_S="$(secs_or AGENT_STOP_GRACE_S "${AGENT_STOP_GRACE_S:-2}" 2)"
HEALTH_PROBE_S="$(secs_or AGENT_HEALTH_PROBE_S "${AGENT_HEALTH_PROBE_S:-3}" 3)"
# 떠 있는 인스턴스가 돌리고 있는 심의 수 — "<진행> <대기>" 를 찍는다. **모르면 아무것도 안 찍는다**
# (/health 무응답 · 그 수를 안 주는 옛 빌드 · curl 없음). '모름' 과 '0건' 을 섞지 않는다 — 섞으면 답 없는
# 서버를 '심의 없음' 으로 읽는다. 출력을 먼저 변수에 받고 나서 가른다(판정을 파이프에 걸지 않는다).
delib_load() {
  local body act que probe="$HOST"
  [ "$probe" = "0.0.0.0" ] && probe="127.0.0.1"
  body="$(curl -s --noproxy '*' --max-time "$HEALTH_PROBE_S" "http://${probe}:${PORT}/health" 2>/dev/null || true)"
  act="$(printf '%s' "$body" | grep -oE '"delib_active": *[0-9]+' | grep -oE '[0-9]+$' || true)"
  que="$(printf '%s' "$body" | grep -oE '"delib_queued": *[0-9]+' | grep -oE '[0-9]+$' || true)"
  if [ -n "$act" ] && [ -n "$que" ]; then echo "$act $que"; fi
}
OLD_PIDS="$(port_pids || true)"
if [ -n "$OLD_PIDS" ]; then
  # 재기동은 도는 심의와 줄 선 심의를 **전부 끊는다**(도는 것은 interrupted 로 남고, 줄 선 것은 사라진다).
  # 좌석 20석 넘는 패널은 수 시간 돈다 — 종전엔 코드를 고치고 이 스크립트를 부르는 순간(update-forges 도
  # 직접 부른다) 그 심의가 말없이 사라졌다. 유예를 늘리는 것은 답이 아니다(수 시간을 기다릴 수는 없다).
  # 도는 심의가 있으면 **내리지 않고 나간다**(exit 3 — 건너뜀). 강행은 AGENT_RESTART_FORCE=1.
  # 수를 모르면(위 delib_load) 그대로 재기동한다 — 답 없는 서버는 다시 띄울 수 있어야 한다.
  LOAD="$(delib_load || true)"
  if [ -n "$LOAD" ]; then
    D_ACT="${LOAD%% *}"; D_QUE="${LOAD##* }"
    if [ $((D_ACT + D_QUE)) -gt 0 ]; then
      echo "==> ⚠⚠ 심의 ${D_ACT}건 진행 중, ${D_QUE}건 대기 — 재기동하면 전부 끊긴다"
      if [ "${AGENT_RESTART_FORCE:-0}" != "1" ]; then
        echo "○ agent-server 재기동 건너뜀 — 심의 ${D_ACT}건 진행 중, ${D_QUE}건 대기. 끝난 뒤 ./start.sh -d, 지금 강행하려면 AGENT_RESTART_FORCE=1"
        echo "    (떠 있는 인스턴스는 그대로 둔다. 진행 상황: curl -s http://127.0.0.1:${PORT}/health)"
        exit 3
      fi
      echo "==> AGENT_RESTART_FORCE=1 — 강행한다. 진행 중 ${D_ACT}건은 interrupted 로 끊기고 대기 ${D_QUE}건은 사라진다"
    fi
  else
    echo "==> 진행 중 심의 수를 확인하지 못했다(/health 무응답 또는 그 수를 안 주는 옛 빌드) — 그대로 재기동한다"
  fi
  echo "==> stopping previous instance (${OLD_PIDS//$'\n'/ })"
  kill $OLD_PIDS 2>/dev/null || true
  # TERM 뒤 KILL 까지의 유예(초). 긴 심의를 기다리는 값이 아니다 — 프로세스가 스스로 내려갈 짧은 틈이다.
  # `|| true` — 옛 서버를 이미 내린 자리다. 여기서 sleep 이 무슨 까닭으로든 실패해 스크립트가 끝나면 아무것도 안 뜬다.
  sleep "$STOP_GRACE_S" || true
  STILL="$(port_pids || true)"
  if [ -n "$STILL" ]; then kill -9 $STILL 2>/dev/null || true; sleep 1; fi   # 안 죽었으면 강제
fi

echo "==> Agent Server on :${PORT}  (vLLM=${VLLM_BASE_URL}, model=${VLLM_MODEL})"
# 기본은 포그라운드(exec). '-d'/'--daemon' 이면 nohup 백그라운드로 띄우고 즉시 반환한다
# (SSH 끊겨도 유지). 로그는 AGENT_LOG(기본 ./agent-server.log).
if [ "${1:-}" = "-d" ] || [ "${1:-}" = "--daemon" ]; then
  LOG="${AGENT_LOG:-$(pwd)/agent-server.log}"
  nohup .venv/bin/uvicorn app:app --host "$HOST" --port "$PORT" >"$LOG" 2>&1 &
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
  exec .venv/bin/uvicorn app:app --host "$HOST" --port "$PORT"
fi
