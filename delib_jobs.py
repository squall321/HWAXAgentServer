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
import os
import time
import uuid
from pathlib import Path

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
# ⚠ 메모리에만 둔다 — 띄울 때 쓸 인자에 호출자 토큰과 근거 본문이 들어 있어 파일에 적지 않는다. 그래서
#   줄 선 잡은 재기동을 넘기지 못한다(reap_orphans 가 '다시 시작하라' 로 닫는다).
_PENDING: dict[str, tuple] = {}
# 서버가 내려가는 중이면 참 — 자리가 나도 줄 선 잡을 띄우지 않는다(_pump). 내려갈 때는 도는 심의가 전부
# 취소되면서 자리가 한꺼번에 나는데, 그 자리에 줄 선 잡을 띄우면 뜨자마자 죽는다. 헛도는 조회를 쏘고, 죽은
# 모양에 따라 **결정문 없는 done** 으로 남는다(재기동 정리는 done 을 건드리지 않는다). 줄에 그대로 두면
# 다음 기동이 '다시 시작하라' 로 닫는다(reap_orphans).
_CLOSING = False


def closing(on: bool = True) -> None:
    """서버가 내려간다(on) / 다시 받는다(off) — app 의 기동·종료 절차가 mcp_server 를 거쳐 부른다."""
    global _CLOSING  # noqa: PLW0603 — 프로세스에 하나뿐인 원장의 상태다
    _CLOSING = on


def _now() -> float:
    return time.time()


def _path(job_id: str) -> Path:
    return JOB_DIR / f"{job_id}.json"


def _persist(job: dict) -> None:
    """재기동 뒤에도 결과를 회수할 수 있게 파일로 남긴다. 실패해도 심의는 계속 간다."""
    try:
        JOB_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _path(job["id"]).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(_path(job["id"]))
    except OSError as exc:
        log.warning("잡 기록 실패 %s: %r", job.get("id"), exc)


def _load(job_id: str) -> dict | None:
    try:
        return json.loads(_path(job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def get(job_id: str) -> dict | None:
    """메모리에 없으면 파일에서 — 재기동 이전 잡도 회수된다."""
    return _JOBS.get(job_id) or _load(job_id)


def running_count() -> int:
    return sum(1 for j in _JOBS.values() if j.get("status") == "running")


def _owner(job: dict) -> str:
    return str(job.get("user") or "").strip().lower()


def _running_of(owner: str) -> list[str]:
    """그 사람이 돌리고 있는 잡 id. 신원 없는 호출은 누구의 것으로도 세지 않는다(start 의 주석)."""
    return [j["id"] for j in _JOBS.values() if owner and j.get("status") == "running" and _owner(j) == owner]


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
    제 사용자별 상한에 걸린 잡은 **건너뛴다** — 거기서 멈추면 한 사람이 줄 전체를 세운다."""
    if _CLOSING:
        return
    for jid, (job, spawn) in list(_PENDING.items()):
        if running_count() >= MAX_RUNNING:
            return
        if len(_running_of(_owner(job))) >= MAX_RUNNING_PER_USER:
            continue
        del _PENDING[jid]
        _launch(job, spawn)


def _settle(job: dict, task: asyncio.Task) -> None:
    """태스크가 끝나면 불린다 — _drive 의 finally 가 **돌지 못한** 잡의 자리를 돌려주는 그물이다.

    막 띄운 태스크가 첫 걸음을 떼기 전에 취소되면 코루틴 본문에 들어가 보지도 못해 finally 가 돌지
    않는다. 그러면 잡은 영영 running 이고 자리는 돌아오지 않는다. 줄을 섰던 잡은 job_id 가 이미 호출자
    손에 있어, 띄운 바로 그 틈에 deliberate_cancel 이 들어올 수 있다."""
    if job["status"] != "running":
        return      # _drive 가 제 손으로 끝냈다 — 자리도 거기서 넘겼다
    job["status"], job["error"] = "cancelled", "취소됨"
    job["finished_at"] = job["updated_at"] = _now()
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
    mine = len(_running_of(_owner(job)))
    why = (f"사용자별 상한 {MAX_RUNNING_PER_USER}건 — 내 진행 중 {mine}건이 끝나야 차례가 온다(그동안 뒤에 "
           "선 다른 사람이 먼저 갈 수 있다)" if mine >= MAX_RUNNING_PER_USER
           else f"전역 상한 {MAX_RUNNING}건 — 진행 중인 심의가 끝나야 차례가 온다")
    return {"position": pos, "ahead": pos - 1, "length": len(_PENDING), "max": QUEUE_MAX, "why": why,
            "waited_s": round(_now() - (job.get("queued_at") or _now()), 1)}


def queue_state(user_email: str = "") -> dict:
    """대기열 현황 — 길이와 **그 사람의** 순번만. 남의 잡은 길이에 수로만 들어간다."""
    me = (user_email or "").strip().lower()
    return {"length": len(_PENDING), "max": QUEUE_MAX,
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


def _apply(job: dict, event: str, data: dict) -> None:
    """SSE 이벤트 하나를 잡 상태에 반영. deliberation.py 의 이벤트 계약을 읽기만 한다."""
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
            # 좌석 발언 원문. 이것이 없으면 결과만 있고 과정이 없는 심의가 된다(이어하기·원장 제출의 재료).
            # 상한을 둔다 — 21석×6R 이 수십만 자라 원장 파일이 커진다.
            t = job.setdefault("turns", [])
            if len(t) < TURN_MAX:
                t.append({k: v for k, v in data.items() if k != "kind"})
            elif len(t) == TURN_MAX:
                t.append({"note": f"전사 상한 {TURN_MAX}턴 초과 — 이후 발언은 기록하지 않는다"})
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
            job["step"] = str(step)
            job["steps"] = (job.get("steps") or [])[-29:] + [str(step)]
            # 창(최근 30줄) 밖으로 밀려난 줄이 있는지 호출자가 알게 전체 수를 센다. 경고도 같다.
            job["steps_total"] = int(job.get("steps_total") or 0) + 1
    elif event == "result":
        content = data.get("content")
        if content:
            job["result_text"] = content
    elif event == "error":
        job["error"] = str(data.get("message") or "알 수 없는 오류")
    elif event == "warning":
        job["warnings"] = (job.get("warnings") or [])[-9:] + [str(data.get("message") or "")]
        job["warnings_total"] = int(job.get("warnings_total") or 0) + 1


async def _drive(job: dict, gen) -> None:
    """생성기를 끝까지 소비한다. 구독자가 없어도 완주해야 저장(대화·보고서)까지 간다."""
    try:
        async for chunk in gen:
            _apply(job, *_parse_sse(chunk))
            job["updated_at"] = _now()
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        job["error"] = "취소됨"
        raise
    except Exception as exc:  # noqa: BLE001 — 태스크 안 예외는 아무도 await 안 하면 무음이다
        log.exception("[delib-job %s] 심의 태스크 오류", job["id"])
        job["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        job["finished_at"] = _now()
        job["updated_at"] = job["finished_at"]
        if job["status"] == "running":
            job["status"] = "error" if job.get("error") else "done"
        _persist(job)
        _TASKS.pop(job["id"], None)
        _prune()
        _pump()      # 자리가 났다 — 끝났든 접혔든 터졌든. 상태를 바꾼 **같은 걸음에서** 넘겨준다


def start(app, job_kind: str, question: str, *, groups: list | None = None,
          delib_opts: dict | None = None, user_email: str = "", user_pat: str = "") -> dict:
    """심의를 백그라운드로 시작하고 잡 레코드를 즉시 돌려준다.

    돌려주는 잡의 status 는 running(곧바로 시작했다)이거나 queued(자리가 없어 줄을 섰다 — 자리가 나면
    스스로 시작한다)다. 줄까지 찼으면(또는 QUEUE_MAX 가 0 이면) RuntimeError 로 거절한다.

    job_kind 는 JOBS 의 키(별칭 허용). 진입 함수는 여기서 늦게 import 한다 —
    deliberation 모듈이 app.py 를 다시 부르는 순환을 피한다.

    ⚠ 웹과 같은 조합을 만든다 — 트리거(어느 run_*)와 chair_template 을 한 곳에서 세운다.
    호출자가 준 delib_opts 위에 Job 표의 chair/opts 를 **덮어쓴다**(Job 이 정본)."""
    j = resolve_job(job_kind)
    spec = JOBS[j]
    q = (question or "").strip()
    if not q:
        raise ValueError("question 이 비어 있다 — 심의할 화두가 필요하다")
    # ⚠ 거절 문구에 **남의 job_id 를 싣지 않는다.** 종전엔 진행 중인 잡 id 를 전부 찍었는데,
    #   deliberate_cancel·deliberate_result 는 id 만 받으므로 거절당한 사람이 남의 심의를 들여다보고
    #   접을 수 있었다(S26U 피드백 1-9). 제 것만 보여 준다 — 접을 수 있는 것도 그것뿐이다.
    #   신원 없는 호출(서비스 계정)끼리도 서로 남이다 — 빈 이름이 같다고 한 사람으로 묶지 않는다.
    #   그래서 사용자별 상한도 신원 있는 호출에만 건다(없는 호출은 전역 상한만 받는다).
    total = running_count()
    me = (user_email or "").strip().lower()
    mine = _running_of(me)
    # 어느 상한에 걸렸는지 말한다 — 사용자별이면 제 것을 접으면 풀리고, 전역뿐이면 기다려야 한다.
    hit = []
    if total >= MAX_RUNNING:
        hit.append(f"전역 {MAX_RUNNING}건 — DELIB_JOB_MAX_RUNNING")
    if me and len(mine) >= MAX_RUNNING_PER_USER:
        hit.append(f"사용자별 {MAX_RUNNING_PER_USER}건 — DELIB_JOB_MAX_RUNNING_PER_USER")
    # 상한에 걸려도 줄에 자리가 있으면 거절하지 않는다(아래에서 줄을 세운다). 거절은 줄을 끈 박스
    # (QUEUE_MAX=0 — 종전 문구 그대로)와 줄까지 찬 때뿐이다. 줄이 찼어도 상한에 안 걸린 사람은 막지 않는다.
    if hit and QUEUE_MAX <= 0:
        raise RuntimeError(
            f"동시 실행 상한에 걸렸다({' · '.join(hit)}) — "
            + (f"내 진행 중 {len(mine)}건{' ' + ', '.join(mine) if mine else ''}" if me
               else "신원 없는 호출이라 내 심의를 가려 보여 줄 수 없다")
            + f" · 전체 {total}/{MAX_RUNNING}. "
            + ("내 심의가 끝난 뒤 다시 하거나 deliberate_cancel 로 내 것 하나를 접어라." if mine
               else "진행 중인 심의가 끝난 뒤 다시 하라."))
    if hit and len(_PENDING) >= QUEUE_MAX:
        waiting = [x["job_id"] for x in queue_state(me)["mine"]]
        raise RuntimeError(
            f"동시 실행 상한에 걸렸고({' · '.join(hit)}) 대기열도 찼다"
            f"({len(_PENDING)}/{QUEUE_MAX}건 — DELIB_JOB_QUEUE_MAX) — "
            + (f"내 진행 중 {len(mine)}건{' ' + ', '.join(mine) if mine else ''}"
               f" · 내 대기 {len(waiting)}건{' ' + ', '.join(waiting) if waiting else ''}" if me
               else "신원 없는 호출이라 내 심의를 가려 보여 줄 수 없다")
            + f" · 전체 진행 {total}/{MAX_RUNNING}. "
            + ("줄이 빠진 뒤 다시 하거나 deliberate_cancel 로 내 것 하나를 접어라." if mine or waiting
               else "줄이 빠진 뒤 다시 하라."))

    from deliberation import (_SEALED_UNSUPPORTED, _seal, run_deliberation,  # noqa: PLC0415
                              run_sim_deliberation, run_test_plan)
    entry = {"general": run_deliberation, "sim": run_sim_deliberation,
             "test-plan": run_test_plan}[spec["engine"]]

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

    job_id = f"{j}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    job = {
        "id": job_id, "job": j, "kind": j, "label": spec["label"], "question": q,
        "chair_template": opts.get("chair_template"), "opts": _opts_echo(applied),
        "status": "queued", "stage": "queued", "step": "", "steps": [],
        "seats": [], "round": 0, "total_rounds": None,
        "decision": None, "result_text": None, "report_id": None, "plain": None,
        "turns": [], "checkpoint": None,
        "error": None, "warnings": [], "evidence_omitted": [],
        "started_at": None, "updated_at": _now(), "finished_at": None,
        "user": user_email or "",
    }
    _JOBS[job_id] = job
    # 누구든 줄 끝에 세운 다음 줄을 한 번 돌린다 — 자리가 있으면 그 자리에서 곧바로 뜬다(종전과 같다).
    # 띄우는 곳을 _pump 하나로 두어야 '먼저 선 잡보다 방금 온 잡이 먼저 뜬다' 가 생기지 않는다.
    # ⚠ 태스크는 **이 요청의 컨텍스트에서** 만든다. 태스크는 만든 자리의 컨텍스트 변수를 물려받는데, 줄 선
    #   잡은 앞 심의의 뒷정리 안에서 뜬다 — 그대로 만들면 앞 심의가 세운 요청 단위 표식(자격 강등
    #   _pat_degraded · 유령 ID 출처 _turn_ids)을 달고 돈다. 곧바로 뜨는 잡은 종전과 같은 컨텍스트다.
    groups, ctx = list(groups or []), contextvars.copy_context()

    def spawn() -> asyncio.Task:
        gen = entry(app, q, groups, opts or None, user_email, user_pat, None)
        return ctx.run(asyncio.create_task, _drive(job, gen), name=f"delib-job-{job_id}")

    _PENDING[job_id] = (job, spawn)
    _pump()
    if job["status"] == "queued":
        job["queued_at"] = job["updated_at"]
        _persist(job)       # 재기동하면 이 기록으로 '줄이 사라졌다' 를 알린다(reap_orphans)
        log.info("[delib-job %s] 대기 %d번째 job=%s q=%.60s", job_id, len(_PENDING), j, q)
    return job


def _opts_echo(opts: dict) -> dict:
    """무엇이 실제로 걸렸는지 잡에 남긴다 — 부피 큰 값(evidence 본문)은 개수만."""
    out = {}
    for k, v in (opts or {}).items():
        if isinstance(v, list):
            out[k] = len(v) if k in ("evidence", "personas") else v
        elif isinstance(v, str) and len(v) > 120:
            out[k] = v[:117] + "…"
        else:
            out[k] = v
    return out


def cancel(job_id: str, *, by: str = "") -> dict:
    """진행 중이거나 줄 선 심의를 접는다. 동시 상한에 걸렸을 때 사람이 자리를 비울 수 있어야 한다.

    by 는 접으려는 사람이다. **신원이 다른 사람의 심의는 접지 못한다** — 시작 거절 문구는 '접을 수 있는
    것은 제 것뿐' 이라고 말해 왔는데 막는 코드는 없었고, 줄이 생긴 뒤로는 앞에 선 남의 잡을 접으면 제
    순번이 당겨진다. 신원 없는 호출(서비스 계정 — 운영이 쓰는 길)과 주인이 안 적힌 잡은 종전대로다."""
    job = _JOBS.get(job_id)
    if not job:
        raise ValueError(f"그런 심의 잡이 없다(또는 이미 이 프로세스 밖이다): {job_id}")
    me = (by or "").strip().lower()
    if me and _owner(job) and _owner(job) != me:
        # 누구 것인지는 말하지 않는다 — 주인의 계정도 남의 정보다.
        raise PermissionError(f"내가 시작한 심의가 아니다: {job_id} — 접을 수 있는 것은 제 것뿐이다")
    if job_id in _PENDING:      # 줄 선 잡 — 태스크가 없다. 줄에서 빼면 끝이고, 도는 자리는 바뀌지 않는다
        del _PENDING[job_id]
        job["status"], job["error"] = "cancelled", "취소됨 — 줄을 선 채로 접었다(시작하지 않았다)"
        job["finished_at"] = job["updated_at"] = _now()
        _persist(job)
        return {"job_id": job_id, "status": "cancelled",
                "note": "대기열에서 뺐다 — 시작하지 않은 심의라 남은 것이 없다."}
    t = _TASKS.get(job_id)
    if job["status"] != "running" or t is None:
        return {"job_id": job_id, "status": job["status"], "note": "이미 끝난 잡이다 — 취소할 것이 없다"}
    t.cancel()
    return {"job_id": job_id, "status": "cancelling",
            "note": "취소를 요청했다. 잠시 뒤 deliberate_status 로 확인하라(저장은 중단된다)."}


def reap_orphans() -> int:
    """재기동 뒤 파일에 running·queued 로 남은 잡을 interrupted 로 정리한다.

    프로세스가 죽으면 태스크도 죽는데 파일은 running 인 채로 남는다 — 그대로 두면
    영원히 '진행 중'으로 보이고 동시 상한 계산도 어긋난다.
    줄 선 잡(queued)도 같이 닫는다. 띄울 때 쓸 인자(호출자 토큰·근거 본문·좌석)는 메모리에만 있었으니
    다시 줄을 세울 수가 없다 — queued 로 두면 호출자는 오지 않을 차례를 영영 기다린다."""
    n = 0
    try:
        for f in JOB_DIR.glob("*.json"):
            j = _load(f.stem)
            if j and j.get("status") in ("running", "queued") and j["id"] not in _JOBS:
                j["error"] = ("서버 재기동으로 대기열이 사라졌다 — 이 심의는 시작하지 못했다. "
                              "deliberate_start 로 다시 시작해야 한다" if j["status"] == "queued"
                              else "서버 재기동으로 중단됨 — 다시 시작해야 한다")
                j["status"] = "interrupted"
                j["finished_at"] = j.get("updated_at") or _now()
                _persist(j)
                n += 1
    except OSError:
        pass
    if n:
        log.info("재기동 정리: running·queued 로 남은 잡 %d건을 interrupted 로", n)
    return n


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
        "turn_count": len(job.get("turns") or []),
        "checkpoint": bool(job.get("checkpoint")),
        "error": job.get("error"),
    }
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
        out["applied_opts"] = job.get("opts") or {}
        if job.get("checkpoint"):
            out["checkpoint_payload"] = job["checkpoint"]
    return out


def rounds_end(job: dict) -> int:
    """그 잡이 끝난 시점의 누적 라운드 번호 — 이어하기가 여기서부터 이어 센다."""
    prev_off = int((job.get("opts") or {}).get("rounds_so_far") or 0)
    return prev_off + int(job.get("total_rounds") or job.get("round") or 0)


def transcript(job_id: str, *, rnd: int | None = None, seat: str = "",
               offset: int = 0, limit: int = 40) -> dict:
    """좌석 발언 전사를 페이지로 돌려준다. 전량은 클라이언트 컨텍스트를 터뜨린다."""
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
    return {"job_id": job_id, "total": total, "offset": off, "limit": lim,
            "turns": rows[off:off + lim],
            "note": ("전사가 없다 — 이 잡은 전사 보존 이전에 돌았거나 아직 라운드에 못 갔다"
                     if not total else None)}


def list_jobs(limit: int = 20) -> list[dict]:
    """메모리 원장 + 파일 원장을 합쳐 최신순으로."""
    seen = dict(_JOBS)
    try:
        for f in sorted(JOB_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit * 3]:
            j = _load(f.stem)
            if j and j["id"] not in seen:
                seen[j["id"]] = j
    except OSError:
        pass
    rows = sorted(seen.values(), key=lambda j: j.get("started_at") or j.get("queued_at") or 0, reverse=True)
    return [summary(j) for j in rows[:limit]]
