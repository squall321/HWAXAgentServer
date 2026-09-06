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
# ⚠ realpath 로 먼저 푼다 — 리포의 artifacts 는 /data 로 가는 심링크라, 그냥 dirname 하면
# 원장만 홈 디스크에 남아 /data 이관·백업 대상에서 빠진다.
JOB_DIR = Path(os.environ.get(
    "DELIB_JOB_DIR", os.path.join(os.path.dirname(os.path.realpath(_ART).rstrip("/")), "delib-jobs")))

# 동시 실행 상한 — 심의 하나가 좌석 수만큼 LLM 을 물고 있어서, 무제한이면 vLLM 큐가 잠긴다.
MAX_RUNNING = int(os.environ.get("DELIB_JOB_MAX_RUNNING", "2") or 2)
# 메모리 원장 보존 개수(파일은 지우지 않는다 — 결과 회수는 파일에서도 된다).
KEEP_IN_MEM = 200
# 좌석 발언 전사 보존 상한(턴 수). 넘으면 이후 발언은 버리고 그 사실을 한 줄 남긴다.
TURN_MAX = int(os.environ.get("DELIB_JOB_TURN_MAX", "400") or 400)

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
               "testplan": "test-plan", "test": "test-plan", "free": "default"}


def resolve_job(name: str) -> str:
    j = (name or "default").strip().lower()
    j = JOB_ALIASES.get(j, j)
    if j not in JOBS:
        raise ValueError(f"알 수 없는 심의 종류 '{name}' — 가능한 값: {', '.join(JOBS)}")
    return j

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


def start(app, job_kind: str, question: str, *, groups: list | None = None,
          delib_opts: dict | None = None, user_email: str = "", user_pat: str = "") -> dict:
    """심의를 백그라운드로 시작하고 잡 레코드를 즉시 돌려준다.

    job_kind 는 JOBS 의 키(별칭 허용). 진입 함수는 여기서 늦게 import 한다 —
    deliberation 모듈이 app.py 를 다시 부르는 순환을 피한다.

    ⚠ 웹과 같은 조합을 만든다 — 트리거(어느 run_*)와 chair_template 을 한 곳에서 세운다.
    호출자가 준 delib_opts 위에 Job 표의 chair/opts 를 **덮어쓴다**(Job 이 정본)."""
    j = resolve_job(job_kind)
    spec = JOBS[j]
    q = (question or "").strip()
    if not q:
        raise ValueError("question 이 비어 있다 — 심의할 화두가 필요하다")
    if running_count() >= MAX_RUNNING:
        raise RuntimeError(
            f"동시 실행 상한({MAX_RUNNING})에 걸렸다 — 진행 중인 심의가 끝난 뒤 다시. "
            f"진행 중: {[x['id'] for x in _JOBS.values() if x['status'] == 'running']}. "
            f"필요하면 deliberate_cancel 로 하나를 접어라.")

    from deliberation import run_deliberation, run_sim_deliberation, run_test_plan  # noqa: PLC0415
    entry = {"general": run_deliberation, "sim": run_sim_deliberation,
             "test-plan": run_test_plan}[spec["engine"]]

    opts = dict(delib_opts or {})
    if spec.get("chair"):
        opts["chair_template"] = spec["chair"]
    opts.update(spec.get("opts") or {})

    job_id = f"{j}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    job = {
        "id": job_id, "job": j, "kind": j, "label": spec["label"], "question": q,
        "chair_template": opts.get("chair_template"), "opts": _opts_echo(opts),
        "status": "running", "stage": "start", "step": "", "steps": [],
        "seats": [], "round": 0, "total_rounds": None,
        "decision": None, "result_text": None, "report_id": None, "plain": None,
        "turns": [], "checkpoint": None,
        "error": None, "warnings": [],
        "started_at": _now(), "updated_at": _now(), "finished_at": None,
        "user": user_email or "",
    }
    _JOBS[job_id] = job
    _persist(job)

    gen = entry(app, q, list(groups or []), opts or None, user_email, user_pat, None)
    task = asyncio.create_task(_drive(job, gen), name=f"delib-job-{job_id}")
    _TASKS[job_id] = task
    log.info("[delib-job %s] 시작 job=%s chair=%s q=%.60s", job_id, j, opts.get("chair_template"), q)
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


def cancel(job_id: str) -> dict:
    """진행 중인 심의를 접는다. 동시 상한에 걸렸을 때 사람이 자리를 비울 수 있어야 한다."""
    job = _JOBS.get(job_id)
    if not job:
        raise ValueError(f"그런 심의 잡이 없다(또는 이미 이 프로세스 밖이다): {job_id}")
    t = _TASKS.get(job_id)
    if job["status"] != "running" or t is None:
        return {"job_id": job_id, "status": job["status"], "note": "이미 끝난 잡이다 — 취소할 것이 없다"}
    t.cancel()
    return {"job_id": job_id, "status": "cancelling",
            "note": "취소를 요청했다. 잠시 뒤 deliberate_status 로 확인하라(저장은 중단된다)."}


def reap_orphans() -> int:
    """재기동 뒤 파일에 running 으로 남은 잡을 interrupted 로 정리한다.

    프로세스가 죽으면 태스크도 죽는데 파일은 running 인 채로 남는다 — 그대로 두면
    영원히 '진행 중'으로 보이고 동시 상한 계산도 어긋난다."""
    n = 0
    try:
        for f in JOB_DIR.glob("*.json"):
            j = _load(f.stem)
            if j and j.get("status") == "running" and j["id"] not in _JOBS:
                j["status"] = "interrupted"
                j["error"] = "서버 재기동으로 중단됨 — 다시 시작해야 한다"
                j["finished_at"] = j.get("updated_at") or _now()
                _persist(j)
                n += 1
    except OSError:
        pass
    if n:
        log.info("재기동 정리: running 으로 남은 잡 %d건을 interrupted 로", n)
    return n


def summary(job: dict, *, full: bool = False) -> dict:
    """도구 응답용 축약. full 이면 결정문 전문을 싣는다."""
    elapsed = (job.get("finished_at") or _now()) - (job.get("started_at") or _now())
    out = {
        "job_id": job["id"], "job": job.get("job") or job.get("kind"), "label": job.get("label"),
        "status": job["status"], "question": job["question"],
        "chair_template": job.get("chair_template"),
        "stage": job.get("stage"), "step": job.get("step"),
        "round": job.get("round"), "total_rounds": job.get("total_rounds"),
        "seats": [s.get("key") for s in (job.get("seats") or [])],
        "elapsed_s": round(elapsed, 1),
        "report_id": job.get("report_id"),
        "turn_count": len(job.get("turns") or []),
        "checkpoint": bool(job.get("checkpoint")),
        "error": job.get("error"),
    }
    if full:
        out["decision"] = job.get("decision") or job.get("result_text")
        out["plain"] = job.get("plain")
        out["warnings"] = job.get("warnings") or []
        out["steps"] = job.get("steps") or []
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
    rows = sorted(seen.values(), key=lambda j: j.get("started_at") or 0, reverse=True)
    return [summary(j) for j in rows[:limit]]
