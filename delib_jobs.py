# 심의를 MCP 도구로 열기 위한 잡 원장 — 심의는 수 분~수 시간이라 도구 호출 한 번으로 못 끝낸다.
"""심의 잡 원장 — start / status / result 3단으로 나눈 비동기 실행기.

**왜 필요한가.** 게이트웨이에 붙는 MCP 클라이언트(Claude Desktop·Claude Code·사내 챗)에서
심의를 시작할 방법이 없었다. 심의 엔진은 두 곳에 있는데 둘 다 MCP 로 안 보인다 —
`infra/pipeline/*.js` 는 Claude Code 의 Workflow 런타임 전용이고, 이 리포의 파이썬 엔진은
`POST /chat` 에 슬래시 트리거를 넣어야만 열린다. 그래서 MCP 클라이언트가 "심의" 를 찾으면
이름에 '심의' 가 든 유일한 도구군(발표자료 생성기)으로 수렴했다.

**왜 3단인가.** MCP 도구 호출은 동기 요청-응답이고 클라이언트 타임아웃이 보통 수십 초다.
심의는 라운드마다 좌석 수만큼 LLM 을 돌려 5분에서 수 시간이 걸린다. 한 호출로 끝내려 하면
반드시 타임아웃으로 끊기고, 그때 심의는 이미 GPU 를 쓰고 있다. 그래서 시작과 회수를 나눈다.

**본체는 건드리지 않는다.** `run_deliberation` 계열 async generator 를 그대로 구동하고
SSE 청크를 파싱해 진행 상태만 기록한다. 웹(SSE) 경로의 코드 경로는 변경이 없다.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import os
import signal
import threading
import time
import uuid
from urllib.parse import unquote
from pathlib import Path

import call_trace
import review_gate

log = logging.getLogger("agent.delib_jobs")


def _env_int(name: str, default: int) -> int:
    """정수 설정을 읽는다 — 숫자가 아니면 경고하고 기본값이다(deliberation._env_int 와 같은 규칙).

    여기서 예외를 올리면 `import mcp_server` 가 죽고, app.py 는 경고 한 줄만 남긴 채 /mcp 없이 뜬다 —
    서버는 살아 있는데 게이트웨이에서 deliberate_* 가 전부 사라진다. env 키트의 줄을 줄 끝 설명째 옮겨
    적으면 그 값이 된다(start.sh 는 `#` 뒤를 떼지 않는다). 엔진 것을 가져다 쓰지 않는 까닭 — 이 모듈은
    deliberation 을 늦게 import 한다(start 의 주석)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("env %s=%r 정수 파싱 실패 — 기본값 %d 로 읽는다", name, raw[:60], default)
        return default

# 잡 산출물은 artifacts 와 같은 뿌리에 둔다(이관 시 함께 움직이도록 — ARTIFACT_DIR 이 /data 를 가리키면 여기도 따라간다).
_ART = os.environ.get("ARTIFACT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts"))
# ⚠ realpath 로 먼저 푼다 — 리포의 artifacts 는 /data 로 가는 심링크라, 그냥 dirname 하면
# 원장만 홈 디스크에 남아 /data 이관·백업 대상에서 빠진다.
JOB_DIR = Path(os.environ.get(
    "DELIB_JOB_DIR", os.path.join(os.path.dirname(os.path.realpath(_ART).rstrip("/")), "delib-jobs")))

# 동시 실행 상한 — 심의 하나가 좌석 수만큼 LLM 을 물고 있어서, 무제한이면 vLLM 큐가 잠긴다.
# 0 이하는 기본값으로 읽는다 — `int("0" or 2)` 는 0 이라, 0 을 넣은 박스는 모든 시작이 '상한 0건' 에
# 걸렸다(빈 값만 기본값으로 가고 "0" 은 문자열이라 참이다). 사용자별 상한의 0 과 같은 뜻으로 맞춘다.
MAX_RUNNING = _env_int("DELIB_JOB_MAX_RUNNING", 2)
if MAX_RUNNING < 1:
    log.warning("env DELIB_JOB_MAX_RUNNING=%d 는 1 미만 — 기본값 2 로 읽는다", MAX_RUNNING)
    MAX_RUNNING = 2
# 사용자별 상한 — 한 사람이 전역 자리를 다 차지하지 못하게 한다. **기본은 전역 상한과 같다**(비우거나
# 0 이면 전역을 따른다 = 따로 걸리지 않는다). 전역 2 는 LLM 큐 보호선이고 용량은 여기서 잴 수 없어
# 기본 동작을 바꾸지 않는다 — 운영이 전역을 올리고 이 값을 낮춰 쓴다(예: 6 · 2). 자리가 없으면
# 줄을 세운다(아래 QUEUE_MAX).
_PER_USER = _env_int("DELIB_JOB_MAX_RUNNING_PER_USER", 0)
MAX_RUNNING_PER_USER = _PER_USER if _PER_USER > 0 else MAX_RUNNING
# 대기열 길이 — 자리가 없으면 거절하지 않고 줄을 세우고, 자리가 나면 스스로 시작한다. **0 이면 종전처럼
# 거절한다.** 종전엔 상한에 걸리면 거절뿐이라, 패널 14개를 돌리려는 사람이 빈 자리를 지켜보다 하나씩
# 다시 불러야 했다(S26U 피드백 1-9 ③). 줄은 먼저 선 순서로 빠지되, 제 사용자별 상한에 걸린 잡은
# 건너뛴다(_pump) — 한 사람이 줄 맨 앞을 차지해도 다른 사람은 간다. 다만 사용자별 상한이 전역과 같은
# 기본값에서는 건너뛸 일이 없어 한 사람의 잡 여럿이 줄을 통째로 차지할 수 있다 — 그게 싫으면 사용자별
# 상한을 전역보다 낮춘다.
QUEUE_MAX = max(0, _env_int("DELIB_JOB_QUEUE_MAX", 20))
# 도는 GLM 잡의 좌석 합 상한(HWAXPortal docs/delib-ops R4' · DO-D16) — 0 이면 끔(종전 — 잡 수만 센다). 위 셋은 **잡 수**를 세서 3석 심의도 20석
# 심의도 1건이다. 한 심의는 단계마다 좌석 수만큼 LLM 호출을 한꺼번에 내니(한도 6 에 20석 여섯이면 120개), LLM 서버가 받는 수를 묶으려면 좌석을
# 센다. 좌석이 앉기 전(자동 발굴 · 줄 선 잡)은 어림이다(_seats_est). 혼자면 상한보다 커도 돈다 — 아니면 그 잡은 영영 못 뜬다.
MAX_SEATS_RUNNING = max(0, _env_int("DELIB_JOB_MAX_SEATS_RUNNING", 0))

# 동시 한도 덮개(HWAXPortal docs/delib-ops R4) — 박스 파일(<잡 원장>/state/limits.json)이 위 셋(env 값) 위에 덮인다. **판정 때마다** 읽어(수십
# 바이트다) 재기동 없이 바뀐다. 쓰는 길은 루프백 POST /limits(app — /drain 과 같은 신뢰)이고 사람이 파일을 고쳐도 된다.
# 종전엔 셋이 import 때 한 번 읽는 상수라 한도 하나를 바꾸는 데 재기동이 들었고, 재기동은 도는 잡과 줄 선 잡을 닫았다(10-22 새벽 반나절).
# 내리면 도는 잡은 그대로 두고 새 시작만 줄인다. 깨진 파일은 읽지 않고(env 값으로 돈다) 사유를 limits() 에 싣는다(/health). 위 셋은 env 층으로
# 그대로 둔다 — 시험이 그 이름을 바꿔 끼운다.
_LIMIT_KEYS = ("max_running", "max_running_per_user", "queue_max", "max_seats_running")
# max_running_per_user 0 = 전역을 따른다 · max_seats_running 0 = 좌석 합 상한 없음
_LIMIT_LO = {"max_running": 1, "max_running_per_user": 0, "queue_max": 0, "max_seats_running": 0}
_LIMIT_HI = 1000          # 손잡이 하나의 위쪽 — 오타(60000)가 LLM 을 통째로 묶지 않게
_lim_cache: dict = {}

# 새 심의 멈춤(HWAXPortal docs/delib-ops R1) — 카드 대조 비우기 깃발(review_gate.DrainState)과 같은 규칙(수명 상한 6시간 · 주인 by · 사람의
# 고정 hold · 원자적 쓰기)의 **따로 된 파일**(<잡 원장>/state/delib-pause.json · scope delib). 켜져 있으면 새 잡(시작 · 이어하기)을 받지 않고
# 줄 선 잡도 띄우지 않는다(잡아 두고 재기동 — 줄은 시작 인자 파일로 재기동을 넘긴다, R2). 도는 잡은 끝까지 간다. 쓰는 길은 루프백
# POST /drain {scope: "delib"}(app). 종전엔 이 스위치가 없어 운영이 '아무도 안 거는 때' 를 기다렸고, 그 사이 줄 선 잡이 빈자리에 들어갔다(10-22).
PAUSE_HOLD_RETRY_S = 600      # 사람이 고정한 멈춤(끝 시각 없음)의 '약 N분 뒤' — 풀 때를 모르니 10분 뒤 다시 보라고 한다
_PAUSE: dict = {}
# 줄을 다시 보는 간격(초) — 멈춤이 수명으로 풀리거나 사람이 한도 덮개를 손으로 고친 것을 이만큼 안에 따라간다. 종전엔 줄은 잡이 끝날 때만
# 움직였다(그때뿐이라 바깥 사정이 바뀌어도 다음 잡이 끝날 때까지 그대로였다).
PUMP_TICK_S = 15.0
_TICKER = None
# 시작 인자 파일(R2 · R3) — 잡마다 시작 인자 전부(근거 본문 · 좌석 · 사람 의견 · 손잡이 · 신원 — **토큰 없음**)를 <잡 원장>/args/<id>.json(0600)에
# 적는다. 재기동 정리가 줄 선 잡을 같은 차례로 다시 세우고(reap_orphans), 재기동으로 끊긴 잡의 이어하기가 처음 인자를 그대로 쓴다. 끝나면
# 지운다(끊긴 잡만 남긴다 — 이어 돌리면 지운다). 끊긴 잡의 것도 ARGS_KEEP_S 가 지나면 재기동 정리가 지운다(근거 본문이 남지 않게).
_ARGS_DIR = "args"
ARGS_KEEP_S = 7 * 86400
# 진행 일지(HWAXPortal docs/request-1027/design-ha09-ha20.md HA-09 S0) — 도는 잡의 사건을 <잡 원장>/journal/<id>.jsonl(폴더 0700 · 파일 0600)에
# 한 줄씩 덧붙인다. 원장은 띄울 때와 끝날 때만 쓰는데, 재기동(TERM)은 uvicorn 0.49 가 정상 종료를 마친 뒤 같은 신호를 다시 올려 잡 태스크의
# finally(원장 쓰기)가 돌 틈이 없고 KILL · 메모리 고갈은 아무것도 돌지 않는다 — 끊긴 잡의 원장은 늘 띄울 때 모양(stage start · 턴 0)이었고,
# 끝난 라운드 뒤부터 잇는 길(R3)과 끝난 단 건너뛰기(체크포인트)가 실제 재기동에서는 걸리지 않고 처음부터 돌았다. 줄은 os.write 로 곧바로
# 커널에 넘긴다(프로세스가 죽어도 남는다 — 전원이 나갈 때만 마지막 묶음을 잃는다). 재기동 정리(reap_orphans)가 원장 위에 같은 _apply 를
# 다시 돌려 접는다 — 접은 원장은 죽기 직전의 메모리와 같다. 토큰은 없다(사건에 토큰이 실리지 않는다 — 시작 인자 파일과 같은 규칙). 끝(done ·
# error · cancelled)이면 원장을 쓴 다음 지우고, 끊긴 잡의 것은 시작 인자 파일과 같이 이어 돌릴 때까지 · ARGS_KEEP_S 동안 둔다(_drop_args).
_JOURNAL_DIR = "journal"
JOURNAL_V = 1                 # 일지 꼴 번호 — 머리 줄과 다르면 접지 않는다
JOURNAL_MAX_BYTES = max(1, _env_int("DELIB_JOURNAL_MAX_MB", 64)) * 1024 * 1024
_JOURNAL_EVENTS = ("status", "delib", "warning", "error", "result")       # _apply 가 읽는 사건 — 토큰 조각 · ping · done 은 적지 않는다
_JOURNAL_SYNC = ("stage", "personas", "checkpoint", "decision", "outcome")  # 이 delib 사건 뒤에는 fsync — 라운드 · 단 · 결정문 · 보고서 경계
# 내려갈 때(lifespan 종료 — mcp_server.unbind) 도는 잡이 접히기를 기다리는 상한(초). 취소된 잡의 finally 가 interrupted 와 원장을 쓴다.
# start.sh 의 uvicorn 정상 종료 상한(AGENT_GRACEFUL_S) 뒤에 오므로 둘의 합이 TERM 뒤 KILL 까지(AGENT_STOP_GRACE_S)보다 작아야 한다.
SHUTDOWN_WAIT_S = 1.0
_REQUEUED = 0                 # 이번 기동에서 다시 세운 줄 선 잡 수(/health.delib_requeued — update-all 이 재기동 뒤 찍는다, O2)
INTERRUPTED_MSG = ("서버 재기동으로 중단됨 — deliberate_continue(previous_job_id=이 잡) 한 번으로 끝난 라운드 뒤부터 남은 라운드만 이어 돈다"
                   "(사람 의견은 비워도 된다 · 처음 인자를 그대로 쓴다). 처음부터 다시 시작하려면 deliberate_start")
# 외부 응답자 심의(delib_opts.llm=external)의 자리 — 판단 호출이 전부 잡 주인의 Claude Code 워크플로로 가서 이 서버의 LLM 을 물지
# 않는다. 사람 속도의 워커를 기다리는 그 잡이 전역 자리(MAX_RUNNING)를 쥐면 남의 GLM 심의가 그 몇 시간 동안 줄에 묶였다(X-D6).
# 그래서 전역 상한·사용자별 상한(GLM 몫)에 세지 않고 주인(과제를 받는 사람)마다 이만큼만 돌린다 — 나머지는 줄에서 그 주인의 것이
# 끝나기를 기다린다. 도는 수(running_count — /health 의 delib_active·재기동 미룸)에는 그대로 든다(재기동하면 끊긴다).
EXTERNAL_PER_OWNER = 1
# 메모리 원장 보존 개수(파일은 지우지 않는다 — 결과 회수는 파일에서도 된다).
KEEP_IN_MEM = 200
# 좌석 발언 전사 보존 상한(턴 수). 넘으면 이후 발언은 버리고 그 사실을 한 줄 남긴다.
TURN_MAX = _env_int("DELIB_JOB_TURN_MAX", 400)
# 좌석에 주지 않은 근거(화면의 included=False 카드) 보존 상한 — 건수와 건당 글자. 이름을
# excluded 로 짓지 않는다 — 리스크 앱에서 그 말은 '사람이 뺀 근거' 다. 좌석별 자유 조회 실패 카드는
# 좌석 × 라운드로 불어나므로 막아 둔다. **먼저 온 것**을 남긴다 — 사전 근거의 드롭은 라운드가
# 돌기 전에 오고, 그게 호출자가 가장 먼저 알아야 하는 것이다.
# 그리고 **맨 나중에 온 것**도 조금 남긴다(OMITTED_TAIL). 먼저 온 것만 남기면 심의 맨 끝에 오는 알림
# (의장 전사 상한 초과)이 늘 빠진다 — 실패 카드가 자리를 다 채우는 큰 패널이 바로 의장 전사도 줄어드는
# 패널이라 둘은 같이 난다. 머리 30건은 그대로 두고, 그 뒤로는 마지막 몇 건만 굴리며 사이에서 빠진 수를 적는다.
OMITTED_MAX = 30
OMITTED_TAIL = 5
OMITTED_TEXT_MAX = 400

# ── 심의 메뉴 — 포털 웹의 정본 택소노미를 그대로 옮긴다 ──────────────────────────────
# 정본: HWAXPortal/frontend/src/components/chat/delibTaxonomy.ts (JOBS + JOB_ROUTING)
#   · 웹은 Job 을 골라 트리거 문자열 + delib_opts.chair_template 로 보낸다. 여기서도 같은 조합을 만든다.
#   · engine 은 어느 run_* 진입 함수로 가는지. chair 는 delib_opts.chair_template 값(없으면 서버가 세운다).
#   · 이 표가 웹과 어긋나면 같은 이름의 심의가 경로마다 다른 산출을 낸다 — 바꿀 땐 delibTaxonomy.ts 와 함께.
JOBS: dict[str, dict] = {
    "diagnosis": {
        "engine": "general", "chair": "diagnosis", "group": "판단", "label": "원인 규명",
        "what": "증거 위에서 지배원인을 좁힌다(FTA↔FMEA). 조치가 아니라 원인·cut set·미지영역까지가 산출.",
        "input": "불량 현상",
    },
    "option-select": {
        "engine": "general", "chair": "option-select", "group": "판단", "label": "안 선택",
        "what": "기준·가중을 먼저 합의하고 Pugh 2라운드로 고른다. 뒤집힘 임계까지 낸다.",
        "input": "비교할 안들과 결정 문제",
    },
    "credibility": {
        "engine": "general", "chair": "credibility", "group": "판단", "label": "신뢰 판정",
        "what": "NASA-7009 축별로 신뢰도를 채점하고 red-team 지정석이 결론을 깨본다. go/no-go 판정.",
        "input": "판정할 해석·결정",
    },
    "sim-plan": {
        "engine": "sim", "chair": None, "group": "계획", "label": "해석 설계",
        "what": "2단 심의. 메커니즘을 먼저 좁히고 그 위에서 CAE 좌석이 해석 계획서와 sim_spec 을 쓴다.",
        "input": "계산으로 풀 현상",
    },
    "test-plan": {
        "engine": "test-plan", "chair": None, "group": "계획", "label": "시험 설계",
        "what": "계측·CAE·프로그램 전문가가 고정 착석해 무엇을 먼저 측정할지 정한다. 시험 계획서·상관 계약.",
        "input": "확보하려는 물성·성능",
    },
    "build-plan": {
        "engine": "sim", "chair": None, "opts": {"build_plan": 1}, "group": "계획", "label": "구축 계획",
        "what": "메커니즘→해석 계획을 거쳐 반복 파라메트릭 모듈 구축 계획서까지 3단. P1~P4 게이트.",
        "input": "반복해서 돌릴 해석",
    },
    "risk-review": {
        "engine": "general", "chair": "risk-review", "group": "판단", "label": "리스크 심사",
        "what": "설계 변경·스냅샷의 위험을 도출하고 기준선 옹호 지정석과 겨뤄 판정한다. "
                "원장 연동 없이 단발로 도는 심사이고, 원장에 넣으려면 risk_submit_panel_result 를 이어 부른다.",
        "input": "심사할 설계 변경·해석 결과",
    },
    # 봉인 리스크 심사 — 소급 검증용. 웹 메뉴(delibTaxonomy.ts)에는 없다(MCP 전용).
    # 무엇을 닫는지는 여기 늘어놓지 않는다 — 엔진이 쥔다(deliberation._SEALED_CLOSE). 손잡이를 이 표에
    # 복제해 두면 엔진에 유입 경로가 하나 늘 때 이 표만 낡고, 봉인이라고 적힌 심의에 그 길이 열린다.
    "risk-review-sealed": {
        "engine": "general", "chair": "risk-review", "opts": {"sealed": 1},
        "group": "판단", "label": "리스크 심사(봉인)",
        "what": "소급 검증용 리스크 심사 — '사람이 찾기 전에, 그때 있던 자료만으로 심사가 찾았겠는가'. "
                "호출자가 준 자료(화두·evidence·human_note)만으로 돈다. 엔진이 심의 도중 바깥에서 자료를 "
                "가져오는 길을 한꺼번에 닫고 호출자가 다시 열 수 없으며, 봉인 사실과 닫은 경로가 잡 기록과 "
                "결정문 머리에 남는다. 닫는 것·닫지 않는 것은 옵션 안내의 sealed 를 보라.",
        "input": "그 시점 자료(evidence)와 심사할 설계 변경",
    },
    "mechanism": {
        "engine": "general", "chair": "mechanism", "group": "판단", "label": "메커니즘 규명",
        "what": "현상의 지배 물리를 좁힌다. 상태변수·지배방정식 후보·미지 파라미터·반증 관측까지. "
                "해석 설계(sim-plan) 1단만 따로 돌리고 싶을 때.",
        "input": "규명할 현상",
    },
    "default": {
        "engine": "general", "chair": None, "group": "자유", "label": "자유 심의",
        "what": "관련 전문가들이 여러 라운드로 자유롭게 심의한다. 위 틀에 안 맞거나 보고서를 통째로 심의할 때.",
        "input": "화두",
    },
}

# 옛 이름 → 정본 키. 초판(2026-09-05)이 general/sim 으로 열었으므로 깨지지 않게 남긴다.
JOB_ALIASES = {"general": "default", "sim": "sim-plan", "simulation": "sim-plan",
               "testplan": "test-plan", "test": "test-plan", "free": "default",
               "sealed": "risk-review-sealed", "risk-sealed": "risk-review-sealed"}


def resolve_job(name: str) -> str:
    j = (name or "default").strip().lower()
    j = JOB_ALIASES.get(j, j)
    if j not in JOBS:
        raise ValueError(f"알 수 없는 심의 종류 '{name}' — 가능한 값: {', '.join(JOBS)}")
    return j

_JOBS: dict[str, dict] = {}
_TASKS: dict[str, asyncio.Task] = {}
# 줄 선 잡 — job_id → (잡 기록, 그 잡의 태스크를 만드는 함수). **넣은 순서가 순번이다.** 상태가 queued 인
# 잡과 이 사전의 키는 늘 같다.
# 띄울 함수는 메모리에만 있다. 재기동을 넘기는 것은 시작 인자 파일(<잡 원장>/args — 근거 본문 · 좌석 · 신원, **토큰 없음**)이다 —
# 재기동 정리(reap_orphans)가 그 파일로 같은 차례에 다시 세운다(HWAXPortal docs/delib-ops R2). 파일을 못 쓴 잡은 종전처럼 '다시 시작하라' 로 닫힌다.
_PENDING: dict[str, tuple] = {}
# 서버가 내려가는 중이면 참 — 자리가 나도 줄 선 잡을 띄우지 않는다(_pump). 내려갈 때는 도는 심의가 전부
# 취소되면서 자리가 한꺼번에 나는데, 그 자리에 줄 선 잡을 띄우면 뜨자마자 죽는다. 헛도는 조회를 쏘고, 죽은
# 모양에 따라 **결정문 없는 done** 으로 남는다(재기동 정리는 done 을 건드리지 않는다). 줄에 그대로 두면
# 다음 기동이 같은 차례로 다시 세운다(reap_orphans — 시작 인자 파일이 없으면 '다시 시작하라' 로 닫는다). 내려가며 접힌 도는 잡은
# interrupted 로 남는다(_drive — 사람이 접은 것과 가른다, 이어하기 R3).
_CLOSING = False


def closing(on: bool = True) -> None:
    """서버가 내려간다(on) / 다시 받는다(off) — app 의 기동·종료 절차가 mcp_server 를 거쳐 부른다."""
    global _CLOSING  # noqa: PLW0603 — 프로세스에 하나뿐인 원장의 상태다
    _CLOSING = on


def hook_exit_signals() -> bool:
    """내려가기 시작하는 그 자리에서 줄을 닫는다 — uvicorn 이 받는 종료 신호(TERM · INT)의 처리기 앞에 closing(True) 를 끼운다(기동 때 한 번 —
    mcp_server.bind). 끼웠으면 참.

    줄 닫기는 lifespan 종료(mcp_server.unbind)에서 했다. uvicorn 은 신호를 받으면 리스너를 닫고 열린 연결(웹 챗 답 · 웹 심의 구독)을
    AGENT_GRACEFUL_S 까지 기다린 **뒤에야** lifespan 종료를 돈다 — 그 사이 자리가 나면(도는 잡이 끝나거나 주기 펌프) 내려가는 프로세스가 줄 선
    잡을 띄웠고, 그 잡은 곧 interrupted 로 접혀 재기동 뒤 같은 차례로 다시 서지 못했다(R2 — HA-09 S0 반대 검토 10-31). uvicorn 의 처리기
    (Server.handle_exit — 바운드 메서드)일 때만 끼운다 — 다른 처리기(시험 · 기본 처리기)는 건드리지 않는다. 메인 스레드가 아니면(시험의
    TestClient) 아무것도 안 한다 — 신호 처리기는 메인 스레드에서만 건다. 도는 잡은 여기서 접지 않는다(lifespan 종료의 shutdown 이 접는다)."""
    if threading.current_thread() is not threading.main_thread():
        return False
    hooked = False
    for sig in (signal.SIGTERM, signal.SIGINT):
        prev = signal.getsignal(sig)
        if not callable(prev) or getattr(prev, "_delib_closing", False) or getattr(prev, "__name__", "") != "handle_exit":
            continue

        def _on_exit(signum, frame, _prev=prev):
            closing(True)
            _prev(signum, frame)
        _on_exit._delib_closing = True      # type: ignore[attr-defined] — 두 번 끼우지 않게
        signal.signal(sig, _on_exit)
        hooked = True
    return hooked


def _now() -> float:
    return time.time()


def _state_dir() -> Path:
    """원장 옆의 상태 파일 자리 — 한도 덮개 · 심의 멈춤 깃발. 원장의 glob("*.json") 에 걸리지 않게 하위 폴더다."""
    return JOB_DIR / "state"


# 권한 — 잡 폴더 안의 하위 폴더(args · state · list)는 0700, 원장 · 색인 파일은 0640, 상태 파일은 0600. 하위 폴더가 상위(공유 그룹 2775)를
# 따라 만들어지고 원장이 664 였다 — 근거 본문 · 사람 의견 · 화두가 든 파일이다(HWAXPortal docs/request-1027 HA-10). JOB_DIR 자체는
# 건드리지 않는다(운영이 정한 자리 · 같은 그룹의 백업이 읽는다). 고칠 수 없으면(남의 파일 · 읽기 전용) 조용히 둔다 — 기록이 먼저다.
_LEDGER_MODE = 0o640
_STATE_MODE = 0o600


def _private_dir(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    try:
        if d.stat().st_mode & 0o7777 != 0o700:
            os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _chmod_quiet(f: Path, mode: int) -> None:
    try:
        os.chmod(f, mode)
    except OSError:
        pass


def limits_path() -> Path:
    return _state_dir() / "limits.json"


def pause_path() -> Path:
    return _state_dir() / "delib-pause.json"


def pause_state() -> review_gate.DrainState:
    """새 심의 멈춤 깃발(R1) — 잡 원장 자리마다 하나(시험이 JOB_DIR 을 바꿔 끼운다). 규칙은 review_gate.DrainState 그대로다."""
    p = str(pause_path())
    st = _PAUSE.get(p)
    if st is None:
        # 깃발은 DrainState 가 쓴다(파일 0600) — 쓸 때 폴더를 0700 으로(읽기는 아무것도 만들지 않는다 — 거절된 시작은 흔적이 없다)
        st = _PAUSE[p] = review_gate.DrainState(p, scopes=("delib",), dir_mode=0o700)
    return st


def pause_info() -> dict | None:
    """멈춤이 켜져 있으면 그 상태(DrainState.snapshot 꼴 + retry_after_s — 다시 해 볼 때까지 초), 아니면 None. 깃발 파일을 못 읽거나 깨졌으면
    꺼짐이다(DrainState 규칙 — 멈춤은 운영 손잡이라 못 읽어 막지 않는다)."""
    st = pause_state()
    snap = st.snapshot()
    if not snap.get("on"):
        return None
    until = snap.get("until")
    left = math.ceil(float(until) - st.clock()) if until is not None else PAUSE_HOLD_RETRY_S
    return {**snap, "retry_after_s": max(60, int(left))}


def pause_message(info: dict) -> str:
    mins = max(1, round(info["retry_after_s"] / 60))
    why = f" — {str(info['reason'])[:200]}" if info.get("reason") else ""
    return (f"새 심의를 잠시 멈췄다(점검 중{why}) — 약 {mins}분 뒤 다시 하라(retry_after_s={info['retry_after_s']}). "
            "도는 심의는 끝까지 가고, 줄 선 심의는 그대로 기다린다.")


class DelibPaused(RuntimeError):
    """새 심의 멈춤으로 거절한다 — MCP 두 도구는 이 글을 도구 오류로, 웹 트리거는 SSE error(code delib_paused)로 낸다."""

    def __init__(self, info: dict):
        self.info = info
        self.retry_after_s = int(info["retry_after_s"])
        super().__init__(pause_message(info))


def _paused() -> bool:
    try:
        return pause_info() is not None
    except Exception:  # noqa: BLE001 — 깃발을 판정하지 못하면 꺼짐으로 읽는다(DrainState 와 같은 쪽)
        log.exception("새 심의 멈춤을 읽지 못했다 — 꺼짐으로 읽는다")
        return False


def _args_path(job_id: str) -> Path:
    return JOB_DIR / _ARGS_DIR / f"{job_id}.json"


def _write_args(job_id: str, args: dict) -> bool:
    """시작 인자 파일을 쓴다(0600 · 원자적). 못 쓰면(디스크 · 권한 · JSON 이 아닌 값) 거짓 — 그 잡은 종전처럼 재기동을 넘기지 못한다."""
    d = JOB_DIR / _ARGS_DIR
    tmp = d / f".{job_id}.{os.getpid()}.tmp"
    try:
        data = json.dumps({"v": 1, **args}, ensure_ascii=False)
        _private_dir(d)
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, _args_path(job_id))
        return True
    except (OSError, TypeError, ValueError) as exc:
        log.warning("[delib-job %s] 시작 인자 파일을 못 썼다 — 이 잡은 재기동을 넘기지 못한다: %r", job_id, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _read_args(job_id: str) -> dict | None:
    try:
        a = json.loads(_args_path(job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return a if isinstance(a, dict) and a.get("v") == 1 and isinstance(a.get("question"), str) else None


def _drop_args(job_id: str) -> None:
    """시작 인자 파일과 진행 일지를 지운다 — 둘은 같은 규칙이다(끊긴 잡의 것만 이어 돌릴 때까지 남긴다 · 근거 본문 · 발언이 든다)."""
    for p, what in ((_args_path(job_id), "시작 인자 파일을"), (_journal_path(job_id), "진행 일지를")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("[delib-job %s] %s 못 지웠다: %r", job_id, what, exc)


def _journal_path(job_id: str) -> Path:
    return JOB_DIR / _JOURNAL_DIR / f"{job_id}.jsonl"


def _journal_form(ev: str, data: dict, turns_before: int) -> dict | None:
    """일지에 적을 꼴 — _apply 가 읽는 것 그대로라 접으면 메모리와 같다. 적지 않는 것(None) — _apply 가 안 읽는 사건(토큰 조각 · ping · done ·
    모르는 이름). 줄여 적는 것 둘은 _apply 가 상태에 넣지 않고 마지막 일(last_step)만 고치는 사건이다 — 좌석에 준 근거 카드(included 가 거짓이
    아닌 것 — 원장도 본문을 싣지 않는다)는 이름만, 전사 상한(TURN_MAX)을 넘은 턴은 좌석과 라운드만 적는다."""
    if ev not in _JOURNAL_EVENTS:
        return None
    if ev == "delib":
        kind = data.get("kind")
        if kind == "evidence" and data.get("included") is not False:
            return {"kind": "evidence", "source": str(data.get("source") or "")[:80]}
        if kind == "turn" and turns_before >= TURN_MAX:
            return {k: data[k] for k in ("kind", "persona", "round", "display_round") if k in data}
    return data


class _Journal:
    """잡 하나의 진행 일지 — 덧붙이기만 한다(_drive 가 열고 닫는다). 줄마다 os.write 로 커널에 넘기고(사용자 공간 버퍼가 없다 — 프로세스가
    죽어도 쓴 줄은 남는다) 경계 사건(_JOURNAL_SYNC) 뒤에만 fsync 한다. 쓰기가 한 번이라도 실패하면 그 잡은 더 쓰지 않는다 — 경고 한 번이고,
    재기동하면 거기까지 접힌다(일지가 없던 때보다 나빠지지 않는다). 상한(JOURNAL_MAX_BYTES)에 닿으면 그 사실을 한 줄 적고 멈춘다 — 접은 원장이
    그 줄까지의 메모리와 같게(앞부분만 — 단계 · 턴 · 체크포인트가 서로 어긋나지 않는다)."""

    def __init__(self, job_id: str):
        self.job_id, self.fd, self.size = job_id, None, 0
        try:
            _private_dir(JOB_DIR / _JOURNAL_DIR)
            # 같은 잡 id 는 한 번만 뜬다(이어하기는 새 잡이다) — 남은 파일이 있으면 앞의 것이 아니라 비운다(두 번 접히지 않게)
            self.fd = os.open(_journal_path(job_id), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND, 0o600)
            os.fchmod(self.fd, 0o600)
        except OSError as exc:
            self._fail(exc)
            return
        self._line({"k": "j:head", "v": JOURNAL_V, "id": job_id}, sync=True)

    def _fail(self, exc) -> None:
        log.warning("[delib-job %s] 진행 일지를 쓰지 못했다 — 재기동하면 여기까지만 원장에 접힌다: %r", self.job_id, exc)
        self.close()

    def _line(self, rec: dict, *, sync: bool = False) -> None:
        if self.fd is None:
            return
        try:
            data = (json.dumps({"t": _now(), **rec}, ensure_ascii=False) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            self._fail(exc)
            return
        if self.size + len(data) > JOURNAL_MAX_BYTES and rec.get("k") != "j:cap":
            log.warning("[delib-job %s] 진행 일지가 상한 %dMB 에 닿았다 — 그 뒤 진행은 재기동을 넘기지 못한다(설정 DELIB_JOURNAL_MAX_MB)",
                        self.job_id, JOURNAL_MAX_BYTES // (1024 * 1024))
            self._line({"k": "j:cap", "max_mb": JOURNAL_MAX_BYTES // (1024 * 1024)}, sync=True)
            self.close()
            return
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(self.fd, view):]
            self.size += len(data)
            if sync:
                os.fsync(self.fd)
        except OSError as exc:
            self._fail(exc)

    def event(self, ev: str, data: dict, t: float, turns_before: int) -> None:
        """_drive 가 _apply 한 사건 하나 — 시각은 그 사건을 반영한 때(updated_at)다(접으면 그 값이 된다)."""
        form = _journal_form(ev, data, turns_before)
        if form is not None:
            self._line({"t": t, "k": ev, "d": form}, sync=ev == "delib" and data.get("kind") in _JOURNAL_SYNC)

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def _journal_rec(raw: bytes):
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _fold_journal(job: dict) -> int:
    """재기동 정리 — 끊긴 잡의 진행 일지를 원장에 접는다. 접은 사건 수를 돌려준다.

    원장은 도는 동안 띄울 때 모양 그대로다(_launch 뒤로는 끝날 때까지 쓰지 않는다). 그 위에 일지의 사건을 같은 _apply 로 다시 돌리면 죽기
    직전의 메모리와 같아진다 — 단계 · 라운드 · 좌석 · 턴(상한 TURN_MAX 그대로) · 체크포인트 · 결과. 마지막 시각(updated_at)은 마지막 사건의
    시각이다(_close_orphan 이 끝난 시각으로 쓴다 — 종전엔 띄운 시각이 끝난 시각으로 남았다). 반쯤 쓴 마지막 줄(쓰는 도중에 죽었다)은 버린다.
    머리가 없거나 꼴 번호 · 잡 id 가 다른 일지는 접지 않는다(원장 그대로 — 종전과 같다)."""
    try:
        raw = _journal_path(job["id"]).read_bytes()
    except FileNotFoundError:
        return 0
    except OSError as exc:
        log.warning("[delib-job %s] 진행 일지를 읽지 못했다 — 접지 않는다: %r", job["id"], exc)
        return 0
    lines = raw.split(b"\n")
    lines.pop()                   # 마지막 조각 — 줄바꿈으로 끝났으면 빈 것이고, 아니면 반쯤 쓴 줄이다
    head = _journal_rec(lines[0]) if lines else None
    if not (head and head.get("k") == "j:head" and head.get("v") == JOURNAL_V and head.get("id") == job["id"]):
        if lines:
            log.warning("[delib-job %s] 진행 일지의 머리가 이 판의 꼴이 아니다 — 접지 않는다", job["id"])
        return 0
    n = bad = 0
    capped, last = False, None
    for ln in lines[1:]:
        rec = _journal_rec(ln)
        if rec is None:
            bad += 1
            continue
        k, d, t = rec.get("k"), rec.get("d"), rec.get("t")
        if k in _JOURNAL_EVENTS and isinstance(d, dict):
            _apply(job, k, d)
            n += 1
            if isinstance(t, (int, float)) and not isinstance(t, bool):
                last = float(t)
        elif k == "j:cap":
            capped = True
    if last is not None:
        job["updated_at"] = last
    notes = ([f"진행 일지가 상한 {JOURNAL_MAX_BYTES // (1024 * 1024)}MB 에 닿아 그 뒤 진행은 원장에 없다 — 이어 돌면 그 앞까지의 끝난 라운드 "
              "뒤부터다(설정 DELIB_JOURNAL_MAX_MB)"] if capped else []) + (
        [f"진행 일지의 깨진 줄 {bad}개를 건너뛰고 접었다"] if bad else [])
    for msg in notes:
        job["warnings"] = (job.get("warnings") or [])[-9:] + [msg]
        job["warnings_total"] = int(job.get("warnings_total") or 0) + 1
    log.info("[delib-job %s] 재기동 정리 — 진행 일지 %d건을 원장에 접었다(단계 %s · 턴 %d)", job["id"], n, job.get("stage"),
             len(job.get("turns") or []))
    return n


def _limit_value(k: str, v) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not (_LIMIT_LO[k] <= v <= _LIMIT_HI):
        raise ValueError(f"{k} 는 {_LIMIT_LO[k]}~{_LIMIT_HI} 의 정수다(받은 값 {str(v)[:40]})")
    return v


def _limits_file() -> tuple[dict, dict, str]:
    """(덮개 값, 메타 {by · reason · set_at}, 사유) — 파일이 없으면 ({}, {}, ""). 깨졌거나 못 읽으면 ({}, {}, 사유).
    판정 때마다 읽는다 — 수십 바이트라 싸다. 바뀐 때 · 크기로 캐시하면 같은 크기의 값을 같은 틱에 고쳐 쓴 것을 놓친다(사람이 손으로
    6 을 8 로 고치는 것이 그 꼴이다). 경고는 사유가 바뀔 때만 남긴다."""
    p = limits_path()
    vals, meta, err = {}, {}, ""
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("JSON 객체가 아니다")
        for k in _LIMIT_KEYS:
            if raw.get(k) is not None:
                vals[k] = _limit_value(k, raw[k])
        meta = {m: raw[m] for m in ("by", "reason", "set_at") if raw.get(m) is not None}
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        vals, meta = {}, {}
        err = (f"읽지 못했다({type(exc).__name__})" if isinstance(exc, OSError) else f"깨졌다({str(exc)[:160]})") + " — env 값으로 돈다"
    if err and _lim_cache.get("err") != err:
        log.warning("동시 한도 덮개 %s %s", p, err)
    _lim_cache["err"] = err
    return vals, meta, err


def limits() -> dict:
    """지금 걸린 동시 한도 셋과 출처 — {max_running, max_running_per_user, queue_max, source{키: file|env}, by, reason, set_at, error}.
    사용자별 상한은 덮개에 없으면 env 를 따르되, env 도 따로 적지 않았으면(전역을 따르던 값) 덮인 전역을 따른다."""
    over, meta, err = _limits_file()
    mr = over.get("max_running", MAX_RUNNING)
    if over.get("max_running_per_user"):
        pu = over["max_running_per_user"]
    elif "max_running_per_user" in over or (_PER_USER <= 0 and MAX_RUNNING_PER_USER == MAX_RUNNING):
        pu = mr                     # 0(전역을 따른다) · env 도 전역을 따르던 박스
    else:
        pu = MAX_RUNNING_PER_USER
    return {"max_running": mr, "max_running_per_user": pu, "queue_max": over.get("queue_max", QUEUE_MAX),
            "max_seats_running": over.get("max_seats_running", MAX_SEATS_RUNNING),
            "source": {k: ("file" if k in over else "env") for k in _LIMIT_KEYS}, **meta,
            **({"error": err} if err else {})}


def max_running() -> int:
    return limits()["max_running"]


def max_running_per_user() -> int:
    return limits()["max_running_per_user"]


def queue_max() -> int:
    return limits()["queue_max"]


def set_limits(values: dict, *, by: str, reason: str = "", reset: bool = False) -> dict:
    """덮개를 고친다 — 준 칸만 바꾸고(None 이면 그 칸의 덮개를 지운다) reset 이면 파일째 지운다. 원자적으로 쓰고, 올렸으면 그 자리에서 줄을
    띄운다(_pump). 틀린 값은 ValueError. 돌려주는 것은 limits()."""
    by = str(by or "").strip()[:120]
    if not by:
        raise ValueError("by(바꾼 주체)가 비었다")
    if reset:
        try:
            limits_path().unlink()
        except FileNotFoundError:
            pass
    else:
        bad = sorted(set(values) - set(_LIMIT_KEYS))
        if bad:
            raise ValueError(f"모르는 칸 {bad[:5]} — 받는 칸은 {'·'.join(_LIMIT_KEYS)}")
        cur, _meta, _err = _limits_file()
        new = dict(cur)
        for k, v in values.items():
            if v is None:
                new.pop(k, None)
            else:
                new[k] = _limit_value(k, v)
        d = _private_dir(_state_dir())
        tmp = d / f".limits.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({**new, "by": by, "reason": str(reason or "")[:300], "set_at": _now()},
                                  ensure_ascii=False), encoding="utf-8")
        _chmod_quiet(tmp, _STATE_MODE)
        tmp.replace(limits_path())
    _lim_cache.clear()
    out = limits()
    log.info("동시 한도 %s — 전역 %d · 사용자별 %d · 줄 %d · 좌석 합 %s (by %s)", "덮개를 지웠다" if reset else "바꿨다",
             out["max_running"], out["max_running_per_user"], out["queue_max"], out["max_seats_running"] or "끔", by)
    _pump()
    return out


def _path(job_id: str) -> Path:
    return JOB_DIR / f"{job_id}.json"


# 목록 색인 — 목록 한 행(summary)과 목록·재기동 정리가 고르는 데 쓰는 칸만 담은 작은 파일. 원장 파일은 넓은 창에서 턴마다
# say·say_full 을 함께 적어 수백 KB~수 MB 가 된다 — 목록 한 번이 남의 잡을 지나가며 그 파일을 최대 LIST_SCAN_MAX 개 통째로
# 읽었고(300 × 2~6MB), 재기동 정리는 전부 읽었다(검토 A-ledger-growth). 원장과 같은 걸음에서 쓰고(_persist), 색인에 적은 원장의
# (mtime_ns, 크기)가 지금 원장과 다르거나 색인이 없으면(옛 원장·쓰기 실패·다른 판이 원장만 고쳐 씀) 원장을 읽고 다시 쓴다
# (_list_load). 시각을 견주지 않는다 — 다른 박스에서 옮겨 온 원장처럼 mtime 이 앞서 있으면 '색인이 더 새것' 이 영영 안 선다.
# 원장 옆 하위 폴더라 원장의 glob("*.json") 에 걸리지 않는다.
_LIST_DIR = "list"
_LIST_KEYS = ("id", "job", "kind", "label", "status", "question", "chair_template", "stage", "step", "round",
              "total_rounds", "report_id", "error", "started_at", "updated_at", "finished_at", "queued_at", "user",
              "last_step", "owner_aff", "tool_mode", "resumed_by", "resumed_from")


def _list_path(job_id: str) -> Path:
    return JOB_DIR / _LIST_DIR / f"{job_id}.json"


def _list_rec(job: dict) -> dict:
    """목록 한 행에 쓰는 칸만 — summary() 가 원장 대신 이것을 받아도 같은 행이 나온다(턴은 수, 좌석은 키, 체크포인트는 유무)."""
    rec = {k: job[k] for k in _LIST_KEYS if k in job}
    rec["opts"] = {"sealed": (job.get("opts") or {}).get("sealed"),
                   # 외부 응답자로 돈 잡만 그 표지를 싣는다(summary 의 llm·tool_mode) — 나머지 잡의 색인은 종전 모양이다.
                   **({"llm": "external"} if _external(job) else {})}
    rec["seats"] = [{"key": s.get("key")} for s in (job.get("seats") or [])]
    rec["turn_count"] = len(job.get("turns") or [])
    rec["checkpoint"] = bool(job.get("checkpoint"))
    return rec


def _external(job: dict) -> bool:
    """외부 응답자(판단 LLM 이 잡 주인의 Claude Code 워크플로)로 돈 잡인가 — 잡 기록의 걸린 값으로 본다."""
    return (job.get("opts") or {}).get("llm") == "external"


def _tool_mode(job: dict) -> str:
    """외부 응답자 잡의 좌석 자유 조회 길 — tools(글 왕복) · evidence_only. 칸이 없는 잡은 글 왕복 전에 돈 것이라 evidence_only 다."""
    return job.get("tool_mode") or "evidence_only"


def _stamp(st: os.stat_result) -> list:
    """원장 파일의 도장 — 색인이 어느 원장에서 나왔는지(_list_load 가 지금 원장과 견준다)."""
    return [st.st_mtime_ns, st.st_size]


def _write_list(job: dict, st: os.stat_result) -> None:
    _private_dir(JOB_DIR / _LIST_DIR)
    tmp = _list_path(job["id"]).with_suffix(".json.tmp")
    tmp.write_text(json.dumps({**_list_rec(job), "src": _stamp(st)}, ensure_ascii=False), encoding="utf-8")
    _chmod_quiet(tmp, _LEDGER_MODE)
    tmp.replace(_list_path(job["id"]))


def _persist(job: dict) -> None:
    """재기동 뒤에도 결과를 회수할 수 있게 파일로 남긴다. 실패해도 심의는 계속 간다.

    원장을 쓴 다음 그 원장의 도장을 담아 목록 색인을 쓴다(_list_load)."""
    try:
        JOB_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _path(job["id"]).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
        _chmod_quiet(tmp, _LEDGER_MODE)
        tmp.replace(_path(job["id"]))
        _write_list(job, _path(job["id"]).stat())
    except OSError as exc:
        log.warning("잡 기록 실패 %s: %r", job.get("id"), exc)


def _load(job_id: str) -> dict | None:
    try:
        return json.loads(_path(job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _closed_elsewhere(job: dict) -> bool:
    """도는 잡의 끝 쓰기 전에 본다 — 원장이 더는 이 프로세스가 띄운 running 이 아니면(다른 프로세스의 재기동 정리가 interrupted 로 닫았고 그 위에
    이어하기가 resumed_by 를 적었을 수 있다) 참. 그때 끝 쓰기(_drive · _settle · shutdown)는 원장을 덮지 않고 시작 인자 파일 · 진행 일지도 두며
    그렇다고 적는다(HA-09 S0 반대 검토 10-31 — 재기동이 겹치면 옛 프로세스의 끝 쓰기가 resumed_by 를 지워 이어하기가 한 번 더 받아졌다). 도는
    동안 원장은 띄울 때(_launch) 한 번만 쓰므로 이 프로세스만 쓴 원장이면 늘 running 이다. 원장이 없거나 못 읽으면 거짓(종전처럼 쓴다)."""
    rec = _list_load(_path(job["id"]))
    if not rec or rec.get("status") == "running":
        return False
    log.warning("[delib-job %s] 원장을 다른 프로세스가 이미 %s 로 닫았다%s — 이 프로세스의 끝(%s)은 원장에 쓰지 않는다(재기동이 겹쳤다)",
                job["id"], rec.get("status"), f"(이어 돈 잡 {rec['resumed_by']})" if rec.get("resumed_by") else "", job.get("status"))
    return True


def _list_load(f: Path) -> dict | None:
    """목록·재기동 정리가 읽는 잡 — 색인의 도장이 지금 원장 파일과 같으면 색인을, 아니면 원장 전부를 읽고 색인을 다시
    쓴다(색인 이전의 옛 원장은 처음 한 번만 통째로 읽는다). 행은 summary() 로 만든다 — 색인에 없는 칸은 쓰지 않는다."""
    try:
        st = f.stat()
    except OSError:
        return None
    try:
        rec = json.loads(_list_path(f.stem).read_text(encoding="utf-8"))
        if isinstance(rec, dict) and rec.get("src") == _stamp(st):
            return rec
    except (OSError, ValueError):
        pass
    j = _load(f.stem)
    if j:
        try:
            _write_list(j, st)
        except OSError as exc:
            log.debug("목록 색인 쓰기 실패 %s: %r", f.stem, exc)
    return j


def get(job_id: str) -> dict | None:
    """메모리에 없으면 파일에서 — 재기동 이전 잡도 회수된다."""
    return _JOBS.get(job_id) or _load(job_id)


def running_count() -> int:
    return sum(1 for j in _JOBS.values() if j.get("status") == "running")


def running_human_count() -> int:
    """도는 잡 가운데 사람이 띄운 것(started_by ≠ app) — 카드 대조 관문의 절반 판정만 이 수를 본다(M3.md #26)."""
    return sum(1 for j in _JOBS.values() if j.get("status") == "running" and j.get("started_by") != "app")


def _norm(who: str) -> str:
    """신원 이메일을 견주는 꼴로 — 게이트웨이가 헤더에 퍼센트 인코딩해 싣고(+ 가 %2B) 원장은 그 원문을 적어 둔다. 양쪽을 같은
    꼴로 풀어야 '+' 가 든 이메일에서 어긋나지 않는다(HWAXPortal docs/followups-1017 결정 6 조사)."""
    return unquote(str(who or "")).strip().lower()


def _owner(job: dict) -> str:
    return _norm(job.get("user"))


def _foreign(job: dict, viewer: str) -> bool:
    """그 잡이 viewer 에게 **남의 것**인가. 신원 없는 호출(서비스 계정 — 운영이 쓰는 길)에게는 남의 것이
    없고, 주인이 안 적힌 잡은 누구에게도 남의 것이 아니다(누구 것인지 알 수 없다). 접기·이어 붙이기가
    같은 규칙을 봐야 해서 한 곳에 둔다. **읽기(상태 · 결과 · 발언 · 이어하기 · 목록)는 can_read** 가 본다."""
    me = _norm(viewer)
    return bool(me and _owner(job) and _owner(job) != me)


def can_read(job: dict, viewer: str, viewer_aff: str = "", viewer_admin: bool = False) -> bool:
    """viewer 가 그 심의를 볼 수 있나(사용자 결정 10-18 — HWAXPortal docs/followups-1017 결정 6 · FU-D17).
    · 신원 없는 호출(서비스 계정 — 운영 · 워크플로가 쓰는 길) · 관리자 — 다 본다
    · 주인 — 본다
    · 같은 소속 — 잡을 열 때 적은 소속(owner_aff)과 지금 호출자의 소속이 같고 둘 다 비지 않았을 때(빈 소속끼리는 묶지 않는다)
    · 주인이 안 적힌 옛 잡 — 신원 없는 호출과 관리자만(누구 것인지 알 수 없다)"""
    me = _norm(viewer)
    if not me or viewer_admin:
        return True
    owner = _owner(job)
    if not owner:
        return False
    if owner == me:
        return True
    aff = str(job.get("owner_aff") or "").strip()
    return bool(aff and str(viewer_aff or "").strip() == aff)


def _glm_running_count() -> int:
    """GLM 자리를 쓰는 도는 잡 수 — 전역 상한(MAX_RUNNING)이 이것을 본다. 외부 응답자 잡은 세지 않는다(EXTERNAL_PER_OWNER)."""
    return sum(1 for j in _JOBS.values() if j.get("status") == "running" and not _external(j))


def _seats_of(job: dict) -> int:
    """그 잡이 쥐는(쥘) 좌석 수 — 앉은 좌석(엔진의 personas 사건 — 다단 심의는 지금 단의 좌석)이 있으면 그 수, 아직이면 시작 때 어림."""
    n = len(job.get("seats") or [])
    return n if n else max(1, int(job.get("seats_est") or 1))


def seats_running() -> int:
    """도는 GLM 잡의 좌석 합 — 좌석 합 상한(max_seats_running)이 이것을 본다. 외부 응답자 잡은 GLM 을 물지 않아 세지 않는다."""
    return sum(_seats_of(j) for j in _JOBS.values() if j.get("status") == "running" and not _external(j))


def _seats_est(opts: dict) -> int:
    """좌석이 앉기 전의 어림 — 지정 좌석이 있으면 그 수(상한 DELIB_MAX_SEATS) + 1(종류마다 엔진이 더하는 반대석 · 고정석 몫), 없으면 자동 발굴
    기본(DELIB_PERSONAS + DELIB_COUNTER_SEATS) + 1. 넉넉한 쪽으로 어림한다 — 적게 어림하면 앉는 순간 상한을 넘는다. 앉으면 실제 수로 고친다."""
    from deliberation import _COUNTER_SEATS, MAX_REQ_SEATS, N_PERSONAS  # noqa: PLC0415 — 늦은 import(순환 방지)
    req = [p for p in (opts.get("personas") or []) if isinstance(p, dict)]
    return (min(len(req), MAX_REQ_SEATS) if req else N_PERSONAS + max(0, _COUNTER_SEATS)) + 1


def _running_of(owner: str) -> list[str]:
    """그 사람이 돌리고 있는 GLM 잡 id — 사용자별 상한(GLM 몫)이 본다. 신원 없는 호출은 누구의 것으로도 세지 않는다(start 의 주석).
    외부 응답자 잡은 따로 센다(_external_running_of)."""
    return [j["id"] for j in _JOBS.values()
            if owner and j.get("status") == "running" and not _external(j) and _owner(j) == owner]


def _external_running_of(task_owner: str) -> list[str]:
    """그 주인(과제를 받는 사람 — 잡의 task_owner)이 돌리고 있는 외부 응답자 잡 id — 주인마다 EXTERNAL_PER_OWNER 건이다."""
    return [j["id"] for j in _JOBS.values()
            if task_owner and j.get("status") == "running" and _external(j) and j.get("task_owner") == task_owner]


def _launch(job: dict, spawn) -> None:
    job["status"], job["stage"] = "running", "start"
    job["started_at"] = job["updated_at"] = _now()
    _persist(job)
    task = spawn()
    _TASKS[job["id"]] = task
    task.add_done_callback(lambda t: _settle(job, t))
    log.info("[delib-job %s] 시작 job=%s chair=%s q=%.60s", job["id"], job["job"],
             job.get("chair_template"), job["question"])


def _pump() -> None:
    """빈 자리만큼 줄 앞에서부터 띄운다. **잡을 띄우는 곳은 여기 하나뿐이다.**

    자리 사정이 바뀌는 곳마다 부른다 — 새 요청, 끝남·취소·실패(_drive 의 finally), 본문에 들어가 보지도
    못하고 접힌 태스크(_settle). 세고 띄우는 사이에 await 가 없어 이벤트 루프가 끼어들지 못한다 — 그래서 자리
    하나에 둘이 뜨지 않고, 부른 뒤에는 '자리가 비었는데 뜰 수 있는 잡이 줄에 있다' 가 남지 않는다.
    제 사용자별 상한에 걸린 잡은 **건너뛴다** — 거기서 멈추면 한 사람이 줄 전체를 세운다. 외부 응답자 잡은 전역 자리를 보지 않고
    주인마다 EXTERNAL_PER_OWNER 건으로 뜬다 — 그래서 전역 자리가 차도 멈추지 않고 끝까지 본다(GLM 잡은 어차피 다 건너뛴다)."""
    if _CLOSING or _paused():
        return              # 멈춤(R1)이면 줄 선 잡을 띄우지 않는다 — 잡아 두고 재기동한다(풀리면 주기 펌프 · /drain 끄기가 띄운다)
    lim = limits()          # 한도는 판정 때마다 읽는다(R4) — 한 번 읽어 이 걸음 안에서 같은 값을 쓴다
    seat_cap = lim["max_seats_running"]
    seats = seats_running() if seat_cap > 0 else 0
    seat_hold = False       # 먼저 선 GLM 잡이 좌석을 기다리는 중 — 뒤의 GLM 잡이 그 좌석을 가로채지 않는다(큰 심의가 굶지 않게)
    for jid, (job, spawn) in list(_PENDING.items()):
        if _external(job):
            if len(_external_running_of(job.get("task_owner") or "")) >= EXTERNAL_PER_OWNER:
                continue
        else:
            if _glm_running_count() >= lim["max_running"]:
                continue
            if len(_running_of(_owner(job))) >= lim["max_running_per_user"]:
                continue
            if seat_cap > 0:
                w = _seats_of(job)
                # 혼자면(도는 GLM 잡이 없으면) 상한보다 커도 띄운다 — 아니면 그 잡은 영영 못 뜬다
                if seat_hold or (_glm_running_count() > 0 and seats + w > seat_cap):
                    seat_hold = True
                    continue
                seats += w
        del _PENDING[jid]
        _launch(job, spawn)


def _settle(job: dict, task: asyncio.Task) -> None:
    """태스크가 끝나면 불린다 — _drive 의 finally 가 **돌지 못한** 잡의 자리를 돌려주는 그물이다.

    막 띄운 태스크가 첫 걸음을 떼기 전에 취소되면 코루틴 본문에 들어가 보지도 못해 finally 가 돌지
    않는다. 그러면 잡은 영영 running 이고 자리는 돌아오지 않는다. 줄을 섰던 잡은 job_id 가 이미 호출자
    손에 있어, 띄운 바로 그 틈에 deliberate_cancel 이 들어올 수 있다."""
    if job["status"] != "running":
        return      # _drive 가 제 손으로 끝냈다 — 자리도 거기서 넘겼다
    mine = not _closed_elsewhere(job)
    if _CLOSING:    # 서버가 내려가며 접혔다 — 사람이 접은 것이 아니다(_drive 와 같다)
        job["status"], job["error"] = "interrupted", INTERRUPTED_MSG
    else:
        job["status"], job["error"] = "cancelled", "취소됨"
        if mine:
            _drop_args(job["id"])
    job["finished_at"] = job["updated_at"] = _now()
    if mine:
        _persist(job)
    if _TASKS.get(job["id"]) is task:
        del _TASKS[job["id"]]
    _pump()


def queue_info(job: dict) -> dict | None:
    """줄 선 잡의 순번, 줄을 서 있지 않으면 None. **수만 싣는다** — 앞에 선 잡이 누구 것이고 무엇을 묻는지는
    싣지 않는다. id 를 주면 그걸로 남의 심의를 들여다보고 접을 수 있고, 접으면 제 순번이 당겨진다."""
    if job["id"] not in _PENDING:
        return None
    pos = list(_PENDING).index(job["id"]) + 1
    # 무엇을 기다리는가 — 제 몫을 다 쓴 것이면 제 심의가 끝나야(또는 접어야) 차례가 오고, 그 사이
    # 뒤에 선 다른 사람이 먼저 갈 수 있다.
    if _external(job):
        mine = len(_external_running_of(job.get("task_owner") or ""))
        why = (f"외부 응답자 심의는 주인마다 {EXTERNAL_PER_OWNER}건씩 돈다 — 내 외부 응답자 심의 {mine}건이 끝나야 차례가 "
               "온다(GLM 심의 자리와는 따로다)")
    else:
        lim = limits()
        mine = len(_running_of(_owner(job)))
        if mine >= lim["max_running_per_user"]:
            why = (f"사용자별 상한 {lim['max_running_per_user']}건 — 내 진행 중 {mine}건이 끝나야 차례가 온다(그동안 뒤에 "
                   "선 다른 사람이 먼저 갈 수 있다)")
        elif _glm_running_count() < lim["max_running"] and lim["max_seats_running"] > 0:
            w = _seats_of(job)
            why = (f"좌석 합 상한 {lim['max_seats_running']}석 — 도는 심의가 좌석 {seats_running()}석을 쓰고 이 심의는 {w}석"
                   f"{'' if job.get('seats') else '(어림)'}이라, 자리가 나면 먼저 선 심의부터 차례로 든다")
        else:
            why = f"전역 상한 {lim['max_running']}건 — 진행 중인 심의가 끝나야 차례가 온다"
    return {"position": pos, "ahead": pos - 1, "length": len(_PENDING), "max": queue_max(), "why": why,
            "waited_s": round(_now() - (job.get("queued_at") or _now()), 1)}


def queue_state(user_email: str = "") -> dict:
    """대기열 현황 — 길이와 **그 사람의** 순번만. 남의 잡은 길이에 수로만 들어간다."""
    me = (user_email or "").strip().lower()
    return {"length": len(_PENDING), "max": queue_max(),
            "mine": [{"job_id": jid, "position": i + 1, "ahead": i}
                     for i, (jid, (job, _spawn)) in enumerate(_PENDING.items()) if me and _owner(job) == me]}


def _prune() -> None:
    if len(_JOBS) <= KEEP_IN_MEM:
        return
    # 줄 선 잡은 끝난 것이 아니다 — 끝난 시각이 없어, 걸러 내지 않으면 '가장 오래 전에 끝난 잡' 으로
    # 정렬돼 맨 먼저 버려진다(줄에는 있는데 원장에는 없는 잡이 된다).
    done = sorted((j for j in _JOBS.values() if j["status"] not in ("running", "queued")),
                  key=lambda j: j.get("finished_at") or 0)
    for j in done[: len(_JOBS) - KEEP_IN_MEM]:
        _JOBS.pop(j["id"], None)


def _parse_sse(chunk: bytes) -> tuple[str, dict]:
    """`event: X\\ndata: {...}\\n\\n` 한 덩이를 (이벤트명, 페이로드)로. 형식이 어긋나면 ('', {})."""
    try:
        text = chunk.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return "", {}
    event, data = "", {}
    for line in text.splitlines():
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            try:
                data = json.loads(line[6:])
            except ValueError:
                data = {}
    return event, (data if isinstance(data, dict) else {})


def _event_label(event: str, data: dict) -> str:
    """방금 온 이벤트를 한 줄로 — 진행 조회의 last_step 이다(무엇이 마지막으로 일어났나). 진행이 아닌
    이벤트(토큰 조각·ping·모르는 이름)는 빈 문자열이다 — 적지 않는다."""
    if event == "status":
        return str(data.get("step") or "")[:160]
    if event == "delib":
        kind = data.get("kind")
        if kind == "turn":
            return f"좌석 발언 — {data.get('persona')} ({data.get('display_round') or data.get('round')}R)"
        if kind == "stage":
            return f"단계 — {data.get('stage')}"
        if kind == "evidence":
            return f"근거 — {str(data.get('source') or '')[:80]}"
        return str(kind or "")
    if event in ("warning", "error"):
        return f"{'경고' if event == 'warning' else '오류'} — {str(data.get('message') or '')[:120]}"
    return ""


def _apply(job: dict, event: str, data: dict) -> None:
    """SSE 이벤트 하나를 잡 상태에 반영. deliberation.py 의 이벤트 계약을 읽기만 한다."""
    # 마지막으로 일어난 일 — 상태줄뿐 아니라 좌석 발언·근거·단계 전환도 적는다. 상태줄(step)은 라운드가 도는
    # 동안 그대로라, 그것만 보면 좌석이 하나씩 발언하고 있는 것과 멈춘 것이 구분되지 않는다.
    if event and (label := _event_label(event, data)):
        job["last_step"] = label
    if event == "delib":
        kind = data.get("kind")
        if kind == "stage":
            job["stage"] = str(data.get("stage") or "")
            if job["stage"].startswith("r") and job["stage"][1:].isdigit():
                job["round"] = int(job["stage"][1:])
        elif kind == "personas":
            job["seats"] = data.get("personas") or []
            job["total_rounds"] = data.get("totalRounds")
        elif kind == "decision":
            job["decision"] = data.get("text") or job.get("decision")
        elif kind == "outcome":
            job["report_id"] = data.get("report_id")
            job["title"] = data.get("title") or job.get("title")
        elif kind == "plain":
            job["plain"] = data.get("text") or data.get("content")
        elif kind == "turn":
            # 좌석 발언. 이것이 없으면 결과만 있고 과정이 없는 심의가 된다(이어하기·원장 제출의 재료).
            # say 는 화면용으로 줄인 글이고, 줄였을 때는 줄이지 않은 발언이 say_full 로 함께 온다(transcript 가
            # 가려 내준다). 상한을 둔다 — 21석×6R 이 수십만 자라 원장 파일이 커진다(say_full 은 좁은 창에서
            # DELIB_TRANSCRIPT_CLIP 까지, 넓은 창은 끊지 않는다 — 보통의 21석 6라운드는 0.4M 자쯤이다).
            t = job.setdefault("turns", [])
            if len(t) < TURN_MAX:
                t.append({k: v for k, v in data.items() if k != "kind"})
            elif len(t) == TURN_MAX:
                t.append({"note": f"전사 상한 {TURN_MAX}턴 초과 — 이후 발언은 기록하지 않는다"})
                # 전사 안의 note 한 줄로는 deliberate_status 를 보는 호출자가 모른다 — 경고로도 한 번 알린다.
                job["warnings"] = (job.get("warnings") or [])[-9:] + [
                    f"전사 상한 {TURN_MAX}턴 초과 — 이후 좌석 발언은 원장(deliberate_transcript)에 기록하지 않는다"
                    "(결정문·보고서 회의록에는 실린다) (설정 DELIB_JOB_TURN_MAX)"]
                job["warnings_total"] = int(job.get("warnings_total") or 0) + 1
        elif kind == "checkpoint":
            job["checkpoint"] = {k: v for k, v in data.items() if k != "kind"}
        elif kind == "evidence" and data.get("included") is False:
            # 좌석에 **주지 않은** 근거. 웹은 근거 패널에 카드로 뜨지만 MCP 호출자가 보는 것은
            # 이 원장뿐인데, 근거 카드를 통째로 안 적고 있었다 — 본문 키가 달라 근거 25건이
            # 전부 버려진 심의가 끝까지 돌았고 호출자는 결정문을 받고도 몰랐다(2026-10-07).
            # 좌석에 준 카드는 싣지 않는다(근거 본문만큼 원장이 커진다).
            ex = job.setdefault("evidence_omitted", [])
            txt = str(data.get("text") or "")
            # 잘랐으면 표식을 붙인다 — 없으면 호출자는 꼬리가 원래 없던 줄 안다.
            if len(txt) > OMITTED_TEXT_MAX:
                txt = txt[:OMITTED_TEXT_MAX] + f"… (앞 {OMITTED_TEXT_MAX}자만 · 전체 {len(txt):,}자)"
            # 그 상한을 바꾸는 설정 이름(knob) — 카드 글에는 없다(웹 사용자가 읽는 글이라 엔진이 따로
            # 싣는다). 호출자가 그 이름을 볼 곳은 여기뿐이라 붙여 둔다. 자른 **뒤에** 붙여야 안 잘린다.
            if data.get("knob"):
                txt += f" (설정 {data['knob']})"
            row = {"source": str(data.get("source") or ""), "text": txt}
            if len(ex) < OMITTED_MAX:
                ex.append(row)
            else:
                # 상한 뒤 — [머리 OMITTED_MAX 건] [안내 한 줄] [마지막 OMITTED_TAIL 건] 모양으로 둔다(온 순서 그대로).
                over = job["evidence_omitted_over"] = int(job.get("evidence_omitted_over") or 0) + 1
                tail = (ex[OMITTED_MAX + 1:] + [row])[-OMITTED_TAIL:]
                gap = over - len(tail)
                ex[OMITTED_MAX:] = [{"note": f"상한 {OMITTED_MAX}건 초과 — 그 뒤로 온 {over}건 중 "
                                             + (f"{gap}건은 기록하지 않고 " if gap else "")
                                             + f"마지막 {len(tail)}건만 아래에 남긴다"}] + tail
    elif event == "status":
        step = data.get("step")
        if step:
            # 설정 이름(knob)은 좌석에 주지 않은 카드와 같다 — 화면 글에는 없고 여기서 붙인다.
            step = str(step) + (f" (설정 {data['knob']})" if data.get("knob") else "")
            job["step"] = step
            job["steps"] = (job.get("steps") or [])[-29:] + [step]
            # 창(최근 30줄) 밖으로 밀려난 줄이 있는지 호출자가 알게 전체 수를 센다. 경고도 같다.
            job["steps_total"] = int(job.get("steps_total") or 0) + 1
    elif event == "result":
        content = data.get("content")
        if content:
            job["result_text"] = content
        if data.get("tool_mode") in ("tools", "evidence_only"):      # 외부 응답자 심의만 싣는다 — 실제로 돈 길
            job["tool_mode"] = data["tool_mode"]
    elif event == "error":
        # 설정 이름(knob)은 여기서 붙인다(상태줄·카드·경고와 같다).
        job["error"] = (str(data.get("message") or "알 수 없는 오류")
                        + (f" (설정 {data['knob']})" if data.get("knob") else ""))
    elif event == "warning":
        # 설정 이름(knob)은 상태줄·카드와 같다 — 화면 글에는 없고 여기서 붙인다.
        job["warnings"] = (job.get("warnings") or [])[-9:] + [
            str(data.get("message") or "") + (f" (설정 {data['knob']})" if data.get("knob") else "")]
        job["warnings_total"] = int(job.get("warnings_total") or 0) + 1


async def _drive(job: dict, gen) -> None:
    """생성기를 끝까지 소비한다. 구독자가 없어도 완주해야 저장(대화·보고서)까지 간다.

    반영한 사건은 진행 일지에도 한 줄씩 적는다(HA-09 S0) — 원장은 끝날 때 쓰는데, 프로세스가 죽으면 그 finally 가 돌지 않는다."""
    jr = _Journal(job["id"])
    try:
        async for chunk in gen:
            ev, data = _parse_sse(chunk)
            turns_before = len(job.get("turns") or [])
            _apply(job, ev, data)
            job["updated_at"] = _now()
            jr.event(ev, data, job["updated_at"], turns_before)
            if ev == "delib" and data.get("kind") == "personas" and _PENDING:
                _pump()     # 좌석이 앉았다 — 어림보다 적으면 줄 선 잡이 들 자리가 났다(좌석 합 상한)
    except asyncio.CancelledError:
        if _CLOSING:
            # 서버가 내려가며 접혔다(정상 종료 — 루프가 남은 태스크를 취소한다). 사람이 접은 것이 아니다 — interrupted 로 남기고 시작 인자
            # 파일을 둔다(이어하기 — R3). 종전엔 '취소됨' 으로 남아, 재기동 한 번이 사람이 접은 것과 같아 보였다.
            job["status"], job["error"] = "interrupted", INTERRUPTED_MSG
        else:
            job["status"] = "cancelled"
            job["error"] = "취소됨"
        raise
    except Exception as exc:  # noqa: BLE001 — 태스크 안 예외는 아무도 await 안 하면 무음이다
        log.exception("[delib-job %s] 심의 태스크 오류", job["id"])
        job["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        jr.close()
        job["finished_at"] = _now()
        job["updated_at"] = job["finished_at"]
        if job["status"] == "running":
            job["status"] = "error" if job.get("error") else "done"
        if not _closed_elsewhere(job):      # 다른 프로세스가 이미 닫은 원장(재기동이 겹쳤다)은 덮지 않는다 — 시작 인자 파일 · 진행 일지도 그쪽 것이다
            _persist(job)
            if job["status"] != "interrupted":
                _drop_args(job["id"])       # 시작 인자 파일 · 진행 일지 — 원장을 쓴 **다음** 지운다(그 사이에 죽으면 원장이 끝난 잡이라 접지 않는다)
        _TASKS.pop(job["id"], None)
        _prune()
        _pump()      # 자리가 났다 — 끝났든 접혔든 터졌든. 상태를 바꾼 **같은 걸음에서** 넘겨준다


def _entry(spec: dict):
    """잡 종류의 진입 함수 — 늦게 import 한다(deliberation 이 app 을 다시 부르는 순환을 피한다)."""
    from deliberation import run_deliberation, run_sim_deliberation, run_test_plan  # noqa: PLC0415
    return {"general": run_deliberation, "sim": run_sim_deliberation, "test-plan": run_test_plan}[spec["engine"]]


def _make_spawn(app, job: dict, entry, q: str, groups: list, opts: dict, user_email: str, user_pat: str, ext_kw: dict, ctx):
    """잡을 띄울 때 부를 것 — 시작(start)과 재기동 뒤 다시 세우기(_requeue)가 같은 것을 만든다."""
    def spawn() -> asyncio.Task:
        gen = entry(app, q, groups, opts or None, user_email, user_pat, None, **ext_kw)
        ctx.run(call_trace.tag, job=job["id"])   # 호출 추적 — 이 잡의 LLM·도구 호출에 잡 id(꺼져 있으면 아무것도 안 한다)
        return ctx.run(asyncio.create_task, _drive(job, gen), name=f"delib-job-{job['id']}")
    return spawn


def start(app, job_kind: str, question: str, *, groups: list | None = None,
          delib_opts: dict | None = None, user_email: str = "", user_pat: str = "", owner: str = "",
          owner_aff: str = "") -> dict:
    """심의를 백그라운드로 시작하고 잡 레코드를 즉시 돌려준다.

    돌려주는 잡의 status 는 running(곧바로 시작했다)이거나 queued(자리가 없어 줄을 섰다 — 자리가 나면
    스스로 시작한다)다. 줄까지 찼으면(또는 QUEUE_MAX 가 0 이면) RuntimeError 로 거절한다.

    job_kind 는 JOBS 의 키(별칭 허용). 진입 함수는 여기서 늦게 import 한다 —
    deliberation 모듈이 app.py 를 다시 부르는 순환을 피한다.

    ⚠ 웹과 같은 조합을 만든다 — 트리거(어느 run_*)와 chair_template 을 한 곳에서 세운다.
    호출자가 준 delib_opts 위에 Job 표의 chair/opts 를 **덮어쓴다**(Job 이 정본).

    owner 는 외부 응답자 심의(delib_opts.llm=external)의 과제 주인이다 — MCP 호출자 길이 게이트웨이 헤더를 푼 이름을 준다
    (external_llm.header_owner). 비우면 user_email 이다. 잡 기록의 주인(user)은 user_email 그대로다."""
    j = resolve_job(job_kind)
    spec = JOBS[j]
    q = (question or "").strip()
    if not q:
        raise ValueError("question 이 비어 있다 — 심의할 화두가 필요하다")
    paused = pause_info()       # 새 심의 멈춤(R1) — 시작 · 이어하기 모두 여기를 지난다
    if paused:
        raise DelibPaused(paused)
    # ⚠ 거절 문구에 **남의 job_id 를 싣지 않는다.** 종전엔 진행 중인 잡 id 를 전부 찍었는데,
    #   deliberate_cancel·deliberate_result 는 id 만 받으므로 거절당한 사람이 남의 심의를 들여다보고
    #   접을 수 있었다(S26U 피드백 1-9). 제 것만 보여 준다 — 접을 수 있는 것도 그것뿐이다.
    #   신원 없는 호출(서비스 계정)끼리도 서로 남이다 — 빈 이름이 같다고 한 사람으로 묶지 않는다.
    #   그래서 사용자별 상한도 신원 있는 호출에만 건다(없는 호출은 전역 상한만 받는다).
    # 외부 응답자 잡(delib_opts.llm=external)은 GLM 자리(전역·사용자별) 대신 주인마다 EXTERNAL_PER_OWNER 건이다 — 어느 몫을 셀지 먼저
    # 가린다. 주인은 과제를 받는 사람이다(owner — 비면 user_email, 엔진과 같게 공백·대소문자만 맞춘다).
    from deliberation import _wants_external  # noqa: PLC0415 — 아래의 늦은 import 와 같은 까닭(순환 방지)
    external = _wants_external(delib_opts)
    x_owner = str(owner or user_email or "").strip().lower() if external else ""
    total = _glm_running_count()
    lim = limits()          # 판정 때마다 읽는다(R4) — 박스 파일이 env 위에 덮인다
    _mr, _pu, _qm = lim["max_running"], lim["max_running_per_user"], lim["queue_max"]
    # 거절 문구가 말하는 손잡이 — 값이 덮개에서 왔으면 덮개다(env 이름을 대면 그 값을 고쳐도 안 바뀐다).
    _knob = {k: ("박스 덮개 POST /limits" if lim["source"][k] == "file" else env)
             for k, env in (("max_running", "DELIB_JOB_MAX_RUNNING"), ("max_running_per_user", "DELIB_JOB_MAX_RUNNING_PER_USER"),
                            ("queue_max", "DELIB_JOB_QUEUE_MAX"), ("max_seats_running", "DELIB_JOB_MAX_SEATS_RUNNING"))}
    me = (user_email or "").strip().lower()
    mine = _external_running_of(x_owner) if external else _running_of(me)
    who = x_owner if external else me
    # 어느 상한에 걸렸는지 말한다 — 사용자별이면 제 것을 접으면 풀리고, 전역뿐이면 기다려야 한다.
    hit = []
    if external:
        if x_owner and len(mine) >= EXTERNAL_PER_OWNER:
            hit.append(f"외부 응답자 심의 주인마다 {EXTERNAL_PER_OWNER}건 — GLM 심의 자리와는 따로다")
    else:
        if total >= _mr:
            hit.append(f"전역 {_mr}건 — {_knob['max_running']}")
        if me and len(mine) >= _pu:
            hit.append(f"사용자별 {_pu}건 — {_knob['max_running_per_user']}")
        _sc = lim["max_seats_running"]
        if _sc > 0 and total > 0:
            _w, _sr = _seats_est(dict(delib_opts or {})), seats_running()
            if _sr + _w > _sc:
                hit.append(f"좌석 합 {_sc}석(도는 {_sr}석 + 이 심의 {_w}석 어림) — {_knob['max_seats_running']}")
    # 상한에 걸려도 줄에 자리가 있으면 거절하지 않는다(아래에서 줄을 세운다). 거절은 줄을 끈 박스
    # (QUEUE_MAX=0 — 종전 문구 그대로)와 줄까지 찬 때뿐이다. 줄이 찼어도 상한에 안 걸린 사람은 막지 않는다.
    # '전체 n/상한' 은 GLM 자리의 수다 — 외부 응답자 잡의 거절 문구에는 싣지 않는다.
    if hit and _qm <= 0:
        raise RuntimeError(
            f"동시 실행 상한에 걸렸다({' · '.join(hit)}) — "
            + (f"내 진행 중 {len(mine)}건{' ' + ', '.join(mine) if mine else ''}" if who
               else "신원 없는 호출이라 내 심의를 가려 보여 줄 수 없다")
            + ("" if external else f" · 전체 {total}/{_mr}") + ". "
            + ("내 심의가 끝난 뒤 다시 하거나 deliberate_cancel 로 내 것 하나를 접어라." if mine
               else "진행 중인 심의가 끝난 뒤 다시 하라."))
    if hit and len(_PENDING) >= _qm:
        waiting = [x["job_id"] for x in queue_state(me)["mine"]]
        raise RuntimeError(
            f"동시 실행 상한에 걸렸고({' · '.join(hit)}) 대기열도 찼다"
            f"({len(_PENDING)}/{_qm}건 — {_knob['queue_max']}) — "
            + (f"내 진행 중 {len(mine)}건{' ' + ', '.join(mine) if mine else ''}"
               f" · 내 대기 {len(waiting)}건{' ' + ', '.join(waiting) if waiting else ''}" if who
               else "신원 없는 호출이라 내 심의를 가려 보여 줄 수 없다")
            + ("" if external else f" · 전체 진행 {total}/{_mr}") + ". "
            + ("줄이 빠진 뒤 다시 하거나 deliberate_cancel 로 내 것 하나를 접어라." if mine or waiting
               else "줄이 빠진 뒤 다시 하라."))

    from deliberation import (_INT_KEYS, _SEALED_UNSUPPORTED, _external_refusal,  # noqa: PLC0415
                              _resolve_opts, _role_req_max, _seal, _seat_key, _started_by)
    entry = _entry(spec)

    opts = dict(delib_opts or {})
    if spec.get("chair"):
        opts["chair_template"] = spec["chair"]
    opts.update(spec.get("opts") or {})
    # 봉인(sealed) — 닫는 것은 엔진이다(_resolve_opts). 여기서는 잡 기록에 **실제로 걸린 값**이 남게
    # 닫힌 사본을 적는다. 엔진에는 호출자가 보낸 그대로 넘긴다 — 무엇을 열려다 닫혔는지를 엔진이
    # 화면과 원장에 남기려면 원래 값을 봐야 한다. 다단 엔진은 봉인이 서지 않으니 잡을 만들기 전에 막는다.
    applied, _ = _seal(opts)
    if applied.get("sealed") and spec["engine"] != "general":
        raise ValueError(_SEALED_UNSUPPORTED)
    # 손잡이는 엔진이 **읽은 값**으로 적는다. 보낸 값을 그대로 적으면 못 읽어 기본값으로 돈 것("끔")이나
    # 상한에서 줄인 것(rounds=99)이 걸린 값처럼 남는다 — 호출자가 손잡이를 끈 줄 아는 그 기록이 된다.
    # 근거 본문은 빼고 읽힌다(여기서 맞춰 자를 까닭이 없다 — 기록에는 건수만 남는다).
    _read = _resolve_opts({k: v for k, v in applied.items() if k != "evidence"})
    # timeout_s 도 같다 — 상한에서 죈 값을 적는다(보낸 20000 이 걸린 것처럼 남지 않게). 이어하기가 이 값을 넘긴다.
    # knowledge_query 도 같다 — 못 읽어 버린 좌석 항목을 센 수가 남지 않게.
    applied = {**applied, **{k: getattr(_read, k) for k in applied
                             if k in _INT_KEYS or k in ("voc", "timeout_s", "knowledge_query")}}
    # 외부 응답자(advanced.llm=external) — 받을 수 없으면(꺼진 서버·반출 동의 표지 없음·신원 없음) 잡을 만들기 전에 거절한다.
    # 받으면 잡 기록에 판단 LLM 을 적는다.
    _refused = _external_refusal(app, applied, owner or user_email)
    if _refused:
        raise ValueError(f"{_refused['code']}: {_refused['message']}")
    if _read.llm == "external":
        applied = {**applied, "llm": "external"}
    elif "llm" in applied:
        applied = {**applied, "llm": _read.llm}

    job_id = f"{j}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    job = {
        # 좌석이 앉기 전의 어림 — 좌석 합 상한(max_seats_running)이 앉을 때까지 이 수로 센다(앉으면 seats 의 수).
        "seats_est": _seats_est(opts),
        "id": job_id, "job": j, "kind": j, "label": spec["label"], "question": q,
        "chair_template": opts.get("chair_template"), "opts": _opts_echo(applied),
        "status": "queued", "stage": "queued", "step": "", "steps": [],
        "seats": [], "round": 0, "total_rounds": None,
        # 호출자가 준 좌석 역할 — **줄이지 않은 것**(엔진이 읽는 상한까지). seats 는 엔진의 personas 이벤트에서
        # 오는데 그 역할은 소개 카드용 앞 280자뿐이다. 이어하기는 좌석을 여기서 되넘기므로, 레지스트리에 없는
        # 좌석(호출자가 지어 준 키 — 엔진이 원본을 다시 읽어 올 데가 없다)은 그 280자가 역할이 됐다.
        # seats 는 화면·목록이 읽는 모양이라 그대로 두고 역할 전문만 따로 적는다(mcp_server._carry_seats).
        # 길이는 엔진이 받는 것과 같다(_role_req_max — 좁은 창 2,000자, 넓은 창 창에서 유도). 종전엔 넓은 창에서도 2,000자로
        # 적어, 엔진은 전문을 받았는데 이어하기는 2,000자로 되넘겼다.
        "seat_roles": {_seat_key(p): str(p["role"])[:_role_req_max()]
                       for p in (opts.get("personas") or [])
                       if _seat_key(p) and isinstance(p.get("role"), str) and p["role"]},
        "decision": None, "result_text": None, "report_id": None, "plain": None,
        "turns": [], "checkpoint": None,
        "error": None, "warnings": [], "evidence_omitted": [],
        "started_at": None, "updated_at": _now(), "finished_at": None,
        "user": user_email or "",
        # 잡을 연 사람의 소속(게이트웨이가 실어 준 포털 원장의 그때 값) — 같은 소속 열람(can_read)이 이것과 견준다(결정 6, 10-18).
        "owner_aff": str(owner_aff or "").strip(),
        # 누가 띄웠나(M3.md #26) — app 이면 카드 대조 관문의 절반 판정에 세지 않는다(running_human_count).
        "started_by": _started_by(opts.get("started_by")),
        # 외부 응답자 잡만 — 과제를 받는 주인. 그 잡의 자리(주인마다 EXTERNAL_PER_OWNER 건)를 이 이름으로 센다(_pump). tool_mode 는
        # 좌석 자유 조회의 길(tools — 글 왕복 · evidence_only) — 여기서는 요청으로 적고, 엔진의 result 프레임이 실제로 돈 길로 고쳐 적는다.
        **({"task_owner": x_owner, "tool_mode": "tools" if _read.free_tools else "evidence_only"}
           if _read.llm == "external" else {}),
    }
    _JOBS[job_id] = job
    # 시작 인자 파일(R2 · R3) — 줄이 재기동을 넘기고 끊긴 잡이 이어 돈다. 토큰을 쥔 잡은 적지 않는다(MCP 잡은 애초에 토큰이 없다).
    job["durable"] = (not user_pat) and _write_args(job_id, {
        "job_kind": j, "question": q, "groups": list(groups or []), "opts": opts,
        "user_email": user_email or "", "owner": owner or "", "owner_aff": owner_aff or ""})
    # 누구든 줄 끝에 세운 다음 줄을 한 번 돌린다 — 자리가 있으면 그 자리에서 곧바로 뜬다(종전과 같다).
    # 띄우는 곳을 _pump 하나로 두어야 '먼저 선 잡보다 방금 온 잡이 먼저 뜬다' 가 생기지 않는다.
    # ⚠ 태스크는 **이 요청의 컨텍스트에서** 만든다. 태스크는 만든 자리의 컨텍스트 변수를 물려받는데, 줄 선
    #   잡은 앞 심의의 뒷정리 안에서 뜬다 — 그대로 만들면 앞 심의가 세운 요청 단위 표식(자격 강등
    #   _pat_degraded · 유령 ID 출처 _turn_ids)을 달고 돈다. 곧바로 뜨는 잡은 종전과 같은 컨텍스트다.
    groups, ctx = list(groups or []), contextvars.copy_context()
    # 외부 응답자 잡은 과제 주인과 잡 id(과제 머리의 job_id — 워크플로가 맡은 잡의 과제만 답한다)를, 위키에 남기는 잡(delib_opts.wiki —
    # HWAXPortal docs/mxwp-wiki P8)은 잡 id(원천 열쇠)와 시작한 사람의 소속(범위)을 따로 넘긴다. 둘 다 아니면 진입 호출은 종전 그대로다.
    ext_kw = {"owner": owner or user_email, "job_id": job_id} if _read.llm == "external" else {}
    if _read.wiki:
        ext_kw.update(job_id=job_id, affiliation=str(owner_aff or "").strip())
    spawn = _make_spawn(app, job, entry, q, groups, opts, user_email, user_pat, ext_kw, ctx)
    _PENDING[job_id] = (job, spawn)
    _pump()
    if job["status"] == "queued":
        job["queued_at"] = job["updated_at"]
        _persist(job)       # 재기동하면 이 기록으로 '줄이 사라졌다' 를 알린다(reap_orphans)
        log.info("[delib-job %s] 대기 %d번째 job=%s q=%.60s", job_id, len(_PENDING), j, q)
    return job


def _opts_echo(opts: dict) -> dict:
    """무엇이 실제로 걸렸는지 잡에 남긴다 — 부피 큰 값(evidence 본문)은 개수만. 글(사람 의견·이어받은 요약)은
    **전문**을 남긴다 — 종전엔 앞 117자 + '…' 로만 남아 원장으로는 좌석이 무엇을 받았는지 재현할 수 없었다.
    응답에 실을 때 줄이는 것은 _opts_view 다(화면용과 원장을 가른다)."""
    out = {}
    for k, v in (opts or {}).items():
        if k == "knowledge_query" and isinstance(v, dict):
            # 좌석별 검색어 사전은 항목 수만 — 진행 조회(applied_opts)가 좌석 수 × 600자를 매번 실어 나르지 않게.
            out[k] = {"seats": len(v)}
        elif isinstance(v, list):
            out[k] = len(v) if k in ("evidence", "personas") else v
        else:
            out[k] = v
    return out


def _opts_view(echo: dict) -> dict:
    """원장의 요청 값을 응답(applied_opts)에 싣는 모양 — 긴 글은 앞부분과 전체 길이만(전문은 원장 파일에 있다)."""
    return {k: ({"stage": v.get("stage")} if k == "resume_checkpoint" and isinstance(v, dict)     # 결정문 전문은 원장에 있다(HA-09)
                else v[:117] + f"… (전체 {len(v):,}자)" if isinstance(v, str) and len(v) > 120 else v)
            for k, v in (echo or {}).items()}


def cancel(job_id: str, *, by: str = "") -> dict:
    """진행 중이거나 줄 선 심의를 접는다. 동시 상한에 걸렸을 때 사람이 자리를 비울 수 있어야 한다.

    by 는 접으려는 사람이다. **신원이 다른 사람의 심의는 접지 못한다** — 시작 거절 문구는 '접을 수 있는
    것은 제 것뿐' 이라고 말해 왔는데 막는 코드는 없었고, 줄이 생긴 뒤로는 앞에 선 남의 잡을 접으면 제
    순번이 당겨진다. 신원 없는 호출(서비스 계정 — 운영이 쓰는 길)과 주인이 안 적힌 잡은 종전대로다."""
    job = _JOBS.get(job_id)
    if not job:
        raise ValueError(f"그런 심의 잡이 없다(또는 이미 이 프로세스 밖이다): {job_id}")
    if _foreign(job, by):
        # 누구 것인지는 말하지 않는다 — 주인의 계정도 남의 정보다.
        raise PermissionError(f"내가 시작한 심의가 아니다: {job_id} — 접을 수 있는 것은 제 것뿐이다")
    if job_id in _PENDING:      # 줄 선 잡 — 태스크가 없다. 줄에서 빼면 끝이고, 도는 자리는 바뀌지 않는다
        del _PENDING[job_id]
        job["status"], job["error"] = "cancelled", "취소됨 — 줄을 선 채로 접었다(시작하지 않았다)"
        job["finished_at"] = job["updated_at"] = _now()
        _persist(job)
        _drop_args(job_id)
        return {"job_id": job_id, "status": "cancelled",
                "note": "대기열에서 뺐다 — 시작하지 않은 심의라 남은 것이 없다."}
    t = _TASKS.get(job_id)
    if job["status"] != "running" or t is None:
        return {"job_id": job_id, "status": job["status"], "note": "이미 끝난 잡이다 — 취소할 것이 없다"}
    t.cancel()
    return {"job_id": job_id, "status": "cancelling",
            "note": "취소를 요청했다. 잠시 뒤 deliberate_status 로 확인하라(저장은 중단된다)."}


def _tighten_existing() -> None:
    """기동 때 한 번 — 이 판 전에 넓게 만들어진 하위 폴더 · 원장 · 색인을 좁힌다(HA-10). 이미 좁으면 건드리지 않는다."""
    for sub in (_ARGS_DIR, _LIST_DIR, "state", _JOURNAL_DIR):
        if (JOB_DIR / sub).is_dir():
            _private_dir(JOB_DIR / sub)
    for pat, mode in ((JOB_DIR.glob("*.json"), _LEDGER_MODE), ((JOB_DIR / _LIST_DIR).glob("*.json"), _LEDGER_MODE),
                      ((JOB_DIR / "state").glob("*.json"), _STATE_MODE), ((JOB_DIR / _JOURNAL_DIR).glob("*.jsonl"), _STATE_MODE)):
        try:
            for f in pat:
                try:
                    if f.stat().st_mode & 0o7777 & ~mode:
                        _chmod_quiet(f, mode)
                except OSError:
                    pass
        except OSError:
            pass


def reap_orphans(app=None) -> int:
    """재기동 뒤 파일에 running·queued 로 남은 잡을 정리한다 — 닫은(interrupted) 수를 돌려준다.

    프로세스가 죽으면 태스크도 죽는데 파일은 running 인 채로 남는다 — 그대로 두면 영원히 '진행 중'으로 보이고 동시 상한 계산도
    어긋난다. 도는 잡은 interrupted 로 닫고 시작 인자 파일을 둔다 — deliberate_continue 한 번으로 남은 라운드만 이어 돈다(R3).
    줄 선 잡(queued)은 app 을 받았고 시작 인자 파일이 있으면 **처음 줄 선 차례대로 다시 세운다**(R2 — 띄우기는 주기 펌프 · 다음
    시작이 한다. 멈춤이 켜져 있으면 그대로 붙잡힌다). 파일이 없거나 깨졌으면(이 판 이전의 잡 · 쓰기 실패) 종전처럼 '시작하지 못했다' 로
    닫는다 — queued 로 두면 호출자는 오지 않을 차례를 영영 기다린다."""
    global _REQUEUED  # noqa: PLW0603 — 이번 기동의 수(/health)
    n, back = 0, []
    _tighten_existing()
    try:
        for f in JOB_DIR.glob("*.json"):
            # 상태는 목록 색인에서 본다 — 끝난 잡의 원장(수 MB)까지 통째로 읽지 않는다. 고칠 잡만 원장 전부를 읽는다.
            rec = _list_load(f)
            if not (rec and rec.get("status") in ("running", "queued") and rec["id"] not in _JOBS):
                continue
            j = _load(f.stem)
            if not (j and j.get("status") in ("running", "queued") and j["id"] not in _JOBS):
                continue
            args = _read_args(j["id"]) if j["status"] == "queued" and app is not None else None
            if args is not None:
                back.append((j, args))
                continue
            if j["status"] == "running":
                _fold_journal(j)        # 끊기기 전의 진행 — 원장은 띄울 때 모양이다(HA-09 S0). 닫기 전에 접어야 끝난 시각이 마지막 사건의 시각이다
            _close_orphan(j)
            n += 1
    except OSError:
        pass
    requeued = 0
    for j, args in sorted(back, key=lambda x: x[0].get("queued_at") or x[0].get("updated_at") or 0):
        try:
            _requeue(app, j, args)
            requeued += 1
        except Exception as exc:  # noqa: BLE001 — 한 잡이 깨져도 나머지는 다시 세운다
            log.warning("[delib-job %s] 줄에 다시 세우지 못했다 — 닫는다: %r", j.get("id"), exc)
            _close_orphan(j, why=f"{type(exc).__name__}")
            n += 1
    _REQUEUED = requeued
    _sweep_args()
    if n:
        log.info("재기동 정리: running·queued 로 남은 잡 %d건을 interrupted 로", n)
    if requeued:
        log.info("재기동 정리: 줄 선 잡 %d건을 처음 차례대로 다시 세웠다", requeued)
    return n


def _close_orphan(j: dict, *, why: str = "") -> None:
    if j["status"] == "queued":
        j["error"] = ("서버 재기동으로 대기열이 사라졌다 — 이 심의는 시작하지 못했다"
                      + (f"(시작 인자를 다시 읽지 못했다 — {why})" if why else "") + ". deliberate_start 로 다시 시작해야 한다")
        _drop_args(j["id"])
    else:
        j["error"] = INTERRUPTED_MSG
    j["status"] = "interrupted"
    j["finished_at"] = j.get("updated_at") or _now()
    _persist(j)


def _requeue(app, job: dict, args: dict) -> None:
    """재기동 전 줄 선 잡을 시작 인자 파일로 다시 세운다 — 띄우는 것은 start() 와 같은 _make_spawn 이다. 그룹은 줄에 설 때의 값이다(메모리
    줄도 같은 값으로 돈다 — HWAXPortal docs/delib-ops DO-D2 ③)."""
    spec = JOBS[resolve_job(str(args.get("job_kind") or job.get("job") or "default"))]
    ext_kw = ({"owner": args.get("owner") or args.get("user_email"), "job_id": job["id"]} if _external(job) else {})
    if (job.get("opts") or {}).get("wiki"):
        ext_kw.update(job_id=job["id"], affiliation=str(job.get("owner_aff") or "").strip())     # start() 와 같은 칸
    spawn = _make_spawn(app, job, _entry(spec), args["question"], list(args.get("groups") or []), dict(args.get("opts") or {}),
                        str(args.get("user_email") or ""), "", ext_kw, contextvars.copy_context())
    job.update(status="queued", stage="queued", durable=True, requeued=int(job.get("requeued") or 0) + 1, requeued_at=_now())
    _JOBS[job["id"]] = job
    _PENDING[job["id"]] = (job, spawn)
    _persist(job)


def _sweep_args() -> None:
    """남은 시작 인자 파일 · 진행 일지를 치운다 — 원장이 없거나 끝난 잡(끝남과 지우기 사이에 죽었다)의 것, 끊긴 잡이라도 이어 돌렸거나
    ARGS_KEEP_S 가 지난 것. 근거 본문 · 발언이 원장 옆에 오래 남지 않게 한다. 둘은 잡마다 같은 규칙이다(_drop_args 가 함께 지운다) — 시작
    인자 파일을 못 쓴 잡(durable 거짓)의 일지도 여기서 본다."""
    files = []
    for sub, pat in ((_ARGS_DIR, "*.json"), (_JOURNAL_DIR, "*.jsonl")):
        try:
            files += list((JOB_DIR / sub).glob(pat))
        except OSError:
            pass
    now = _now()
    for jid in dict.fromkeys(f.stem for f in files):
        if jid in _JOBS:
            continue
        rec = _list_load(JOB_DIR / f"{jid}.json")
        status = (rec or {}).get("status")
        if status in ("running", "queued"):
            continue
        if status == "interrupted" and not rec.get("resumed_by") and now - float(rec.get("finished_at") or now) < ARGS_KEEP_S:
            continue
        _drop_args(jid)


def resume_info(job: dict) -> dict | None:
    """재기동으로 끊긴(interrupted) 잡을 이어 돌릴 재료 — {rounds_done, total_rounds, rounds_left, args, resumed_by}. 아니면 None.

    끝난 라운드는 단계로 센다 — 라운드 N 에 들어서면(stage rN) 앞의 N-1 이 끝난 것이고, 의장 결정 이후 단계(decide · explain · report)면 전부다.
    단계 rN 에서 끊긴 라운드는 일부 좌석만 말했을 수 있어 세지 않는다. 다단 심의(sim · test-plan)는 단 사이 승계가 엔진 안에 있어 끝난
    라운드로 잇지 않는다(rounds_done 0 — 같은 인자로 처음부터). rounds_left 는 이어 돌 라운드 수다 — 엔진의 하한이 2 라, 마지막 라운드나
    결정문에서 끊긴 잡도 2라운드(앞 입장을 이어받은 입장 · 수렴)를 돈다."""
    if job.get("status") != "interrupted":
        return None
    try:
        general = JOBS[resolve_job(str(job.get("job") or job.get("kind") or "default"))]["engine"] == "general"
    except (KeyError, ValueError):
        general = False
    total = int(job.get("total_rounds") or 0)       # 엔진이 좌석을 앉힐 때 적는다 — 그 전에 끊겼으면 끝난 라운드가 없다(목록 색인에도 이 칸뿐이다)
    stage = str(job.get("stage") or "")
    done = 0
    if general and total:
        if stage.startswith("r") and stage[1:].isdigit():
            done = max(0, min(total, int(stage[1:]) - 1))
        elif stage in ("decide", "explain", "report"):
            done = total
    ck = job.get("checkpoint")
    return {"rounds_done": done, "total_rounds": total or None,
            "rounds_left": (max(2, min(8, total - done)) if done else None),
            "args": _args_path(job["id"]).exists(), "resumed_by": job.get("resumed_by"),
            # 다단 심의의 끝난 단(HA-09) — 이으면 그 단까지 건너뛴다(sim-1 = 1단 · sim-2 = 2단까지 끝났다)
            "checkpoint": ck.get("stage") if isinstance(ck, dict) else None}


def queue_durability() -> dict:
    """/health 의 delib_queue_durable · delib_requeued — 줄 선 잡이 전부 시작 인자 파일을 가졌으면 참(재기동이 줄을 잃지 않는다 — 재기동
    판정이 줄 선 수를 막는 수로 세지 않는다, R2'). 줄이 비었으면 참이다. 하나라도 파일을 못 썼으면 거짓(그 잡은 재기동에 사라진다)."""
    return {"durable": all(job.get("durable") for job, _spawn in _PENDING.values()), "requeued": _REQUEUED}


async def _tick() -> None:
    while True:
        await asyncio.sleep(PUMP_TICK_S)
        try:
            if _PENDING:
                _pump()
        except Exception:  # noqa: BLE001 — 한 번 실패로 줄 다시 보기를 멈추지 않는다
            log.exception("줄 다시 보기 실패 — 다음 간격에 다시 본다")


def start_ticker() -> None:
    """줄을 PUMP_TICK_S 마다 다시 본다 — 기동 때(mcp_server.bind, 도는 이벤트 루프 안). 루프 밖에서 부르면 아무것도 안 한다(시험)."""
    global _TICKER  # noqa: PLW0603
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _TICKER is None or _TICKER.done():
        _TICKER = loop.create_task(_tick(), name="delib-pump-tick")


def stop_ticker() -> None:
    global _TICKER  # noqa: PLW0603
    if _TICKER is not None:
        _TICKER.cancel()
        _TICKER = None


async def shutdown(wait_s: float | None = None) -> int:
    """서버가 내려간다(lifespan 종료 — mcp_server.unbind) — 도는 잡 태스크를 취소하고 wait_s(기본 SHUTDOWN_WAIT_S)까지 기다린다. 접은 잡 수를
    돌려준다(HA-09 S0).

    취소된 잡은 _drive 가 interrupted 로 적고 그 자리에서 원장에 진행을 쓴다(DO-D7 — 사람이 접은 것과 가른다). 종전엔 아무도 취소하지 않았다 —
    uvicorn 은 정상 종료를 마친 뒤 받은 신호를 다시 올려 프로세스가 그 자리에서 끝나므로, 루프가 남은 태스크를 취소하는 정리도 finally 도 오지
    않았다. 상한 안에 접히지 않은 잡(엔진이 취소를 늦게 받는다)은 메모리 상태 그대로 interrupted 로 쓴다. 쓰기가 끝내 안 돼도 진행 일지가 있다 —
    다음 기동이 접는다. 이 이벤트 루프의 태스크만 본다(시험이 남긴 다른 루프의 잡은 건드리지 않는다)."""
    closing(True)
    loop = asyncio.get_running_loop()
    live = {jid: t for jid, t in list(_TASKS.items()) if not t.done() and t.get_loop() is loop}
    for t in live.values():
        t.cancel()
    if live:
        await asyncio.wait(list(live.values()), timeout=SHUTDOWN_WAIT_S if wait_s is None else wait_s)
    late = []
    for jid in live:
        job = _JOBS.get(jid)
        if job is not None and job.get("status") == "running":      # finally 가 아직이다 — 메모리 상태 그대로 남긴다
            mine = not _closed_elsewhere(job)                       # 다른 프로세스가 이미 닫은 원장은 덮지 않는다(재기동이 겹쳤다)
            job["status"], job["error"] = "interrupted", INTERRUPTED_MSG
            job["finished_at"] = job["updated_at"] = _now()
            if mine:
                _persist(job)
            late.append(jid)
    if live:
        log.info("내려가는 중 — 도는 심의 잡 %d건을 interrupted 로 접어 원장에 남겼다%s", len(live),
                 f"(그 가운데 {len(late)}건은 상한 {SHUTDOWN_WAIT_S if wait_s is None else wait_s}초 안에 접히지 않아 메모리 상태 그대로)" if late else "")
    return len(live)


def summary(job: dict, *, full: bool = False) -> dict:
    """도구 응답용 축약. full 이면 결정문 전문을 싣는다."""
    # 걸린 시간은 **돈 시간**이다 — 줄을 선 시간은 따로 적는다(waited_s). 시작도 못 하고 접힌 잡은 0 이다.
    end = job.get("finished_at") or _now()
    elapsed = end - (job.get("started_at") or end)
    out = {
        "job_id": job["id"], "job": job.get("job") or job.get("kind"), "label": job.get("label"),
        "status": job["status"], "question": job["question"],
        "chair_template": job.get("chair_template"),
        "sealed": bool((job.get("opts") or {}).get("sealed")),
        "stage": job.get("stage"), "step": job.get("step"),
        "round": job.get("round"), "total_rounds": job.get("total_rounds"),
        "seats": [s.get("key") for s in (job.get("seats") or [])],
        "elapsed_s": round(elapsed, 1),
        "report_id": job.get("report_id"),
        # 목록 색인(_list_rec)은 턴 대신 수만 담는다.
        "turn_count": len(job["turns"] or []) if "turns" in job else int(job.get("turn_count") or 0),
        "checkpoint": bool(job.get("checkpoint")),
        "error": job.get("error"),
    }
    if _external(job):
        # 판단 LLM 이 외부 응답자였다 — tool_mode 는 좌석 자유 조회의 길(리스크 원장의 tool_mode 와 같은 말).
        out["llm"], out["tool_mode"] = "external", _tool_mode(job)
    if job["status"] == "interrupted":
        # 재기동으로 끊겼다 — 끝난 라운드 수와 이어 돌 라운드 수(deliberate_continue 한 번으로 잇는다 — R3).
        out["resume"] = resume_info(job)
    if job.get("resumed_from"):
        out["resumed_from"] = job["resumed_from"]
    if job["status"] == "running":
        # 마지막 이벤트 뒤 경과(초)와 그 이벤트. 종전엔 단계·상태줄·걸린 시간뿐이라, 같은 단계가 30분째면
        # LLM 을 기다리는 것인지 멈춘 것인지 알 수 없었다. 심의는 좌석 하나의 발언이 끝날 때까지 이벤트가 없다 —
        # 그 발언이 잇는 호출들((1 + 파싱 재시도)번, 호출마다 시도 횟수 포함)의 한도 안이면 기다리는 중이다
        # (mcp_server 가 그 값을 quiet_ok_s 로 붙인다. 상한은 아니다 — 자유 조회는 더 길게 잇는다).
        # ⚠ 이 값으로 잡을 끊지 않는다. 시간으로 죽은 것을 판정하면 긴 심의를 죽은 것으로 읽는다.
        out["idle_s"] = round(max(0.0, _now() - (job.get("updated_at") or _now())), 1)
        out["last_step"] = job.get("last_step") or job.get("step")
    q = queue_info(job)
    if q:
        out["queue"] = q
    elif job.get("queued_at"):
        out["waited_s"] = round((job.get("started_at") or end) - job["queued_at"], 1)
    if full:
        out["decision"] = job.get("decision") or job.get("result_text")
        out["plain"] = job.get("plain")
        out["warnings"] = job.get("warnings") or []
        out["warnings_total"] = int(job.get("warnings_total") or len(out["warnings"]))
        out["evidence_omitted"] = job.get("evidence_omitted") or []
        out["steps"] = job.get("steps") or []
        out["steps_total"] = int(job.get("steps_total") or len(out["steps"]))
        out["seats_detail"] = job.get("seats") or []
        out["applied_opts"] = _opts_view(job.get("opts") or {})
        if job.get("checkpoint"):
            out["checkpoint_payload"] = job["checkpoint"]
    return out


def rounds_end(job: dict) -> int:
    """그 잡이 끝난 시점의 누적 라운드 번호 — 이어하기가 여기서부터 이어 센다."""
    prev_off = int((job.get("opts") or {}).get("rounds_so_far") or 0)
    return prev_off + int(job.get("total_rounds") or job.get("round") or 0)


def _turn_view(t: dict, full: bool) -> dict:
    """원장의 턴 하나를 응답 모양으로. say 는 화면용으로 줄인 글이고, 줄였을 때만 줄이지 않은 발언이
    say_full 에 있다. 기본은 줄인 글에 **줄였다는 표식**(say_clipped)과 온전한 길이를 붙여 준다. full 이면
    줄이지 않은 발언을 say 로 준다 — 저장 상한(DELIB_TRANSCRIPT_CLIP)에서도 잘렸으면 그때도 표식을 남긴다."""
    if "say_full" not in t:
        return t        # 줄이지 않은 턴 · 표식 이전에 돈 잡의 턴
    row = {k: v for k, v in t.items() if k != "say_full"}
    if full:
        row["say"] = t["say_full"]
        if int(t.get("say_full_chars") or 0) > len(t["say_full"]):
            row["say_clipped"] = True
        else:
            row.pop("say_full_chars", None)
    else:
        row["say_clipped"] = True
    return row


def transcript(job_id: str, *, rnd: int | None = None, seat: str = "",
               offset: int = 0, limit: int = 40, full: bool = False) -> dict:
    """좌석 발언 전사를 묶음(offset 부터 limit 턴)으로 돌려준다. 전량은 클라이언트 컨텍스트를 터뜨린다.

    기본은 화면에 뜨는 줄인 발언이다(줄인 턴은 say_clipped). full 이면 줄이지 않은 발언을 준다 — 한 묶음이
    몇 배로 커지므로 기본으로 주지 않는다. 묶음이 MCP 결과 상한보다 크면 도구(mcp_server)가 쪽으로 나눈다(mcp_pages).
    next_offset 은 다음 묶음의 시작(없으면 None), remaining 은 그 뒤에 남은 턴 수다 — 종전엔 total·offset·limit 만 줘서
    호출자가 다음 묶음을 셈해야 했다."""
    job = get(job_id)
    if not job:
        raise ValueError(f"그런 심의 잡이 없다: {job_id}")
    rows = job.get("turns") or []
    if rnd is not None:
        rows = [t for t in rows if t.get("round") == rnd or t.get("rnd") == rnd]
    if seat:
        rows = [t for t in rows if seat in str(t.get("persona") or t.get("key") or "")]
    total = len(rows)
    off = max(0, int(offset))
    lim = max(1, min(200, int(limit)))
    page = [_turn_view(t, full) for t in rows[off:off + lim]]
    clipped = sum(1 for t in page if t.get("say_clipped"))
    return {"job_id": job_id, **({"llm": "external", "tool_mode": _tool_mode(job)} if _external(job) else {}),
            "total": total, "offset": off, "limit": lim,
            "next_offset": off + lim if off + lim < total else None,
            "remaining": max(0, total - off - lim),
            "turns": page,
            "note": ("전사가 없다 — 이 잡은 전사 보존 이전에 돌았거나 아직 라운드에 못 갔다"
                     if not total else
                     f"이 묶음의 {clipped}턴은 say 가 화면용으로 줄인 글이다(say_clipped · 온전한 길이는 "
                     "say_full_chars). 줄이지 않은 발언은 full=true 로 받는다."
                     if clipped and not full else
                     f"이 묶음의 {clipped}턴은 저장 상한에서 잘린 발언이다(say_clipped · 온전한 길이는 "
                     "say_full_chars)." if clipped else None)}


# 목록을 만들 때 훑는 원장 파일 수의 상한. 남의 잡은 세지 않고 지나가므로(아래) 제 잡이 드문 사람은 파일을
# 더 깊이 훑는다 — 원장이 아주 큰 박스에서 목록 한 번이 파일 전부를 읽지 않게 막아 둔다.
LIST_SCAN_MAX = 300


def list_jobs(limit: int = 20, *, viewer: str = "", viewer_aff: str = "", viewer_admin: bool = False) -> list[dict]:
    """메모리 원장 + 파일 원장을 합쳐 최신순으로. viewer(신원 있는 호출자)에게는 **볼 수 있는 잡만**(can_read — 제 잡 · 같은
    소속의 잡, 관리자는 전부) 싣는다. 신원 없는 호출(서비스 계정 — 운영이 쓰는 길)은 종전대로 전부 본다.

    행마다 mine(내가 시작한 잡인가)과 owner(시작한 사람 — 사용자 결정 10-18 '같은 소속 것도 목록에, 주인 표시')를 싣는다.
    종전엔 남의 잡을 아예 싣지 않아 주인을 적을 일이 없었다(거절 · 순번 문구에서 남의 job_id 를 뺀 까닭과 같다 — 지금은
    볼 수 있는 잡만 싣는다).
    볼 수 없는 것을 **뺀 뒤에** limit 을 건다. 먼저 자르면 남의 최근 잡이 제 잡을 목록 밖으로 민다."""
    me = _norm(viewer)

    def ok(j: dict) -> bool:
        return can_read(j, viewer, viewer_aff, viewer_admin)
    seen = {jid: j for jid, j in _JOBS.items() if ok(j)}
    try:
        kept = 0
        for f in sorted(JOB_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime,
                        reverse=True)[:max(limit * 3, LIST_SCAN_MAX)]:
            if kept >= limit * 3:
                break
            j = _list_load(f)        # 목록 색인 — 원장 파일(수 MB)을 통째로 읽지 않는다
            if not j or not ok(j):
                continue
            kept += 1
            if j["id"] not in seen:
                seen[j["id"]] = j
    except OSError:
        pass
    rows = sorted(seen.values(), key=lambda j: j.get("started_at") or j.get("queued_at") or 0, reverse=True)
    return [{**summary(j), "mine": bool(me and _owner(j) == me), "owner": _owner(j)} for j in rows[:limit]]
