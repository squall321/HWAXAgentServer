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
import json
import logging
import os
import time
import uuid
from pathlib import Path

log = logging.getLogger("agent.delib_jobs")

# 잡 산출물은 artifacts 와 같은 뿌리에 둔다(이관 시 함께 움직이도록 — ARTIFACT_DIR 이 /data 를 가리키면 여기도 따라간다).
_ART = os.environ.get("ARTIFACT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts"))
JOB_DIR = Path(os.environ.get("DELIB_JOB_DIR", os.path.join(os.path.dirname(_ART.rstrip("/")), "delib-jobs")))

# 동시 실행 상한 — 심의 하나가 좌석 수만큼 LLM 을 물고 있어서, 무제한이면 vLLM 큐가 잠긴다.
MAX_RUNNING = int(os.environ.get("DELIB_JOB_MAX_RUNNING", "2") or 2)
# 메모리 원장 보존 개수(파일은 지우지 않는다 — 결과 회수는 파일에서도 된다).
KEEP_IN_MEM = 200

# kind → (트리거 접두어, 설명). 트리거는 deliberation.py 의 *_TRIGGERS 정본과 같은 문자열이어야 한다.
KINDS: dict[str, dict] = {
    "general": {
        "trigger": "/심의",
        "label": "전문가 심의",
        "what": "여러 도메인 전문가가 라운드를 돌며 근거 위에서 수렴한다. 원인 규명·안 선택·신뢰 판정 등 범용.",
    },
    "sim": {
        "trigger": "/시뮬심의",
        "label": "시뮬레이션 심의",
        "what": "2단 심의. 1단에서 메커니즘을 좁히고, 2단에서 CAE 좌석이 해석 계획서를 쓴다. "
                "수치 스파인(정식화·이산화·검증) 좌석이 고정 착석한다.",
    },
    "test-plan": {
        "trigger": "/시험계획",
        "label": "시험 계획 심의",
        "what": "무엇을 먼저 측정할 것인가를 정한다. 물성 공백을 도구로 조회한 뒤 우선순위와 조건축까지.",
    },
}

_JOBS: dict[str, dict] = {}
_TASKS: dict[str, asyncio.Task] = {}


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


def _prune() -> None:
    if len(_JOBS) <= KEEP_IN_MEM:
        return
    done = sorted((j for j in _JOBS.values() if j["status"] != "running"),
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
    elif event == "status":
        step = data.get("step")
        if step:
            job["step"] = str(step)
            job["steps"] = (job.get("steps") or [])[-29:] + [str(step)]
    elif event == "result":
        content = data.get("content")
        if content:
            job["result_text"] = content
    elif event == "error":
        job["error"] = str(data.get("message") or "알 수 없는 오류")
    elif event == "warning":
        job["warnings"] = (job.get("warnings") or [])[-9:] + [str(data.get("message") or "")]


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


def start(app, kind: str, question: str, *, groups: list | None = None,
          delib_opts: dict | None = None, user_email: str = "", user_pat: str = "") -> dict:
    """심의를 백그라운드로 시작하고 잡 레코드를 즉시 돌려준다.

    kind 는 KINDS 의 키. 본체 진입 함수는 여기서 늦게 import 한다 — deliberation 모듈이
    app.py 를 다시 부르는 순환을 피한다."""
    kind = (kind or "general").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"알 수 없는 심의 종류 '{kind}' — 가능한 값: {', '.join(KINDS)}")
    q = (question or "").strip()
    if not q:
        raise ValueError("question 이 비어 있다 — 심의할 화두가 필요하다")
    if running_count() >= MAX_RUNNING:
        raise RuntimeError(
            f"동시 실행 상한({MAX_RUNNING})에 걸렸다 — 진행 중인 심의가 끝난 뒤 다시. "
            f"진행 중: {[j['id'] for j in _JOBS.values() if j['status'] == 'running']}")

    from deliberation import run_deliberation, run_sim_deliberation, run_test_plan  # noqa: PLC0415
    entry = {"general": run_deliberation, "sim": run_sim_deliberation, "test-plan": run_test_plan}[kind]

    job_id = f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    job = {
        "id": job_id, "kind": kind, "label": KINDS[kind]["label"], "question": q,
        "status": "running", "stage": "start", "step": "", "steps": [],
        "seats": [], "round": 0, "total_rounds": None,
        "decision": None, "result_text": None, "report_id": None, "plain": None,
        "error": None, "warnings": [],
        "started_at": _now(), "updated_at": _now(), "finished_at": None,
        "user": user_email or "",
    }
    _JOBS[job_id] = job
    _persist(job)

    gen = entry(app, q, list(groups or []), delib_opts, user_email, user_pat, None)
    task = asyncio.create_task(_drive(job, gen), name=f"delib-job-{job_id}")
    _TASKS[job_id] = task
    log.info("[delib-job %s] 시작 kind=%s q=%.60s", job_id, kind, q)
    return job


def summary(job: dict, *, full: bool = False) -> dict:
    """도구 응답용 축약. full 이면 결정문 전문을 싣는다."""
    elapsed = (job.get("finished_at") or _now()) - (job.get("started_at") or _now())
    out = {
        "job_id": job["id"], "kind": job["kind"], "label": job.get("label"),
        "status": job["status"], "question": job["question"],
        "stage": job.get("stage"), "step": job.get("step"),
        "round": job.get("round"), "total_rounds": job.get("total_rounds"),
        "seats": [s.get("key") for s in (job.get("seats") or [])],
        "elapsed_s": round(elapsed, 1),
        "report_id": job.get("report_id"),
        "error": job.get("error"),
    }
    if full:
        out["decision"] = job.get("decision") or job.get("result_text")
        out["plain"] = job.get("plain")
        out["warnings"] = job.get("warnings") or []
        out["steps"] = job.get("steps") or []
    return out


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
    rows = sorted(seen.values(), key=lambda j: j.get("started_at") or 0, reverse=True)
    return [summary(j) for j in rows[:limit]]
