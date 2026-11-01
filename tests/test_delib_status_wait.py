# 심의 진행 조회를 서버가 기다린다(wait_s) · 부를 때마다 같은 큰 칸을 뺀다(brief) — 기본 호출은 종전 응답과 바이트까지 같다
"""HWAXPortal docs/token-saving PLAN §3 · §5 P3 · TS-5(사용자 결정 11-02).

왜 생겼나. MCP 로 심의를 연 Claude 는 결과를 기다리며 deliberate_status 를 고리로 불렀다 — dev 하루(09-06) 168번 대 deliberate_result 5번이고,
부를 때마다 화두 전문(리스크 심사는 변경 요지를 담아 수천 자)과 좌석 · 경고 · 빠진 근거 목록이 되풀이됐다. 이제 wait_s 로 한 번 부르면 서버가
잡 원장을 짧은 간격으로 보며(LLM 호출 없음) 상태 · 단계 · 라운드가 바뀌거나 끝날 때까지 기다렸다 답하고(상한 DELIB_STATUS_WAIT_MAX_S), brief 는
그 큰 칸을 머리 · 수로 바꾼다. 서버가 내려가거나 운영이 새 심의를 멈추면 일찍 돌아온다.

가짜는 엔진뿐이다 — 시험이 넣은 사건을 그대로 내는 원격 엔진(화두마다 줄). 잡 원장 · 태스크 · 진행 조회 · 쪽 나누기는 진짜다.

  실행:  .venv/bin/python -m pytest tests/test_delib_status_wait.py -q
"""
import asyncio
import json
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_pages  # noqa: E402
import mcp_server as m  # noqa: E402
from test_delib_job_queue import _play, _tick  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401 — 모델 창 고정(자동 픽스처)

Q = "힌지 크랙이 낙하 각도에 따라 커지는가"


class _Remote:
    """원격으로 모는 가짜 엔진 — 화두마다 줄이 있고, 시험이 넣은 사건 덩이를 그대로 낸다. None 을 넣으면 끝난다(done)."""

    def __init__(self):
        self.boxes: dict = {}

    def box(self, q):
        return self.boxes.setdefault(q, asyncio.Queue())

    def feed(self, q, *chunks):
        for c in chunks:
            self.box(q).put_nowait(c)

    async def entry(self, _app, q, groups, opts, user_email, user_pat, _x):
        box = self.box(q)
        while True:
            c = await box.get()
            if c is None:
                return
            yield c


@pytest.fixture
def remote(monkeypatch, tmp_path):
    """빈 원장 · 빈 줄 · 원격 엔진. 기다림 간격은 짧게, 상한은 시험이 견딜 만큼(종전 판에는 이 이름이 없다 — 없어도 건다)."""
    r = _Remote()
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    for name in ("_JOBS", "_TASKS", "_PENDING", "_PAUSE"):
        monkeypatch.setattr(delib_jobs, name, {})
    monkeypatch.setattr(delib_jobs, "_CLOSING", False)
    monkeypatch.setattr(d, "run_deliberation", r.entry)
    monkeypatch.setattr(m, "_APP", SimpleNamespace(state=SimpleNamespace(delib_llm=None)))
    monkeypatch.setattr(m, "STATUS_POLL_S", 0.01, raising=False)
    monkeypatch.setattr(m, "STATUS_WAIT_MAX_S", 5.0, raising=False)
    return r


def _start(q=Q, **kw) -> dict:
    return delib_jobs.start(object(), "default", q, **kw)


async def _running(remote, q=Q, *chunks) -> dict:
    """도는 잡 하나 — 엔진이 1라운드에 든 뒤(단계 r1)."""
    job = _start(q)
    remote.feed(q, d._delib("stage", stage="r1"), *chunks)
    await _tick()
    assert job["status"] == "running" and job["stage"] == "r1" and job["round"] == 1, "시험 전제 — 1라운드에 든 잡"
    return job


# ── 기다림 — 바뀌면 · 끝나면 · 상한에서 돌아온다 ─────────────────────────────────────────────────────────────────
def test_기다리는_조회는_단계와_라운드가_바뀌면_곧바로_돌아온다(remote):
    async def go():
        job = await _running(remote)
        t0 = time.monotonic()
        call = asyncio.ensure_future(m.deliberate_status(job["id"], wait_s=4))
        await asyncio.sleep(0.3)
        held = not call.done()
        remote.feed(Q, d._delib("stage", stage="r2"))
        out = await asyncio.wait_for(call, 3)
        return held, out, time.monotonic() - t0

    held, out, took = _play(go)
    assert held, "바뀐 것이 없는데 기다리지 않고 돌아왔다"
    assert out["stage"] == "r2" and out["round"] == 2, out
    w = out["wait"]
    assert w["woke"] == "changed" and set(w["changed"]) == {"stage", "round"} and w["wait_s"] == 4.0, w
    assert 0.3 <= took < 2.5 and w["waited_s"] < 2.5, (took, w)


def test_기다리는_조회는_잡이_끝나면_곧바로_돌아온다(remote):
    async def go():
        job = await _running(remote)
        call = asyncio.ensure_future(m.deliberate_status(job["id"], wait_s=4))
        await asyncio.sleep(0.2)
        held = not call.done()
        remote.feed(Q, d._delib("decision", text="결정문"), None)
        return held, await asyncio.wait_for(call, 3)

    held, out = _play(go)
    assert held and out["status"] == "done", out
    assert out["wait"]["woke"] == "ended" and "status" in out["wait"]["changed"], out["wait"]


def test_끝난_잡은_기다리지_않는다(remote):
    async def go():
        job = await _running(remote)
        remote.feed(Q, d._delib("decision", text="결정문"), None)
        await _tick()
        t0 = time.monotonic()
        out = await m.deliberate_status(job["id"], wait_s=4)
        return out, time.monotonic() - t0

    out, took = _play(go)
    assert out["status"] == "done" and out["wait"]["woke"] == "ended" and out["wait"]["waited_s"] == 0.0, out
    assert "changed" not in out["wait"] and took < 0.5, (out["wait"], took)


def test_바뀐_것이_없으면_상한까지_기다리고_그렇다고_말한다(remote, monkeypatch):
    monkeypatch.setattr(m, "STATUS_WAIT_MAX_S", 0.4)

    async def go():
        job = await _running(remote)
        t0 = time.monotonic()
        out = await m.deliberate_status(job["id"], wait_s=0.4)
        return out, time.monotonic() - t0

    out, took = _play(go)
    w = out["wait"]
    assert w["woke"] == "timeout" and w["wait_s"] == 0.4 and 0.4 <= w["waited_s"] < 1.5, w
    assert "clamped" not in w and "changed" not in w and 0.4 <= took < 1.5, (w, took)
    assert out["status"] == "running" and out["stage"] == "r1"


@pytest.mark.parametrize("asked,used", [(600, 0.3), (math.inf, 0.3), (-5, 0.0), (math.nan, 0.0)])
def test_범위_밖의_기다림은_상한_안으로_죄고_그렇다고_말한다(remote, monkeypatch, asked, used):
    """상한(DELIB_STATUS_WAIT_MAX_S)을 넘거나 0 보다 작거나 수가 아니면 죈 값으로 기다리고 wait.clamped 가 그 사실을 말한다."""
    monkeypatch.setattr(m, "STATUS_WAIT_MAX_S", 0.3)

    async def go():
        job = await _running(remote)
        t0 = time.monotonic()
        out = await asyncio.wait_for(m.deliberate_status(job["id"], wait_s=asked), used + 2)    # 죄지 않으면 붙잡히지 않고 붉게 끝난다
        return out, time.monotonic() - t0

    out, took = _play(go)
    w = out["wait"]
    assert w["wait_s"] == used and w["woke"] == "timeout", w
    assert "DELIB_STATUS_WAIT_MAX_S" in w["clamped"] and f"wait_s={asked:g}" in w["clamped"], w
    assert took < used + 1.0, took


def test_상한_설정은_게이트웨이_호출_한도보다_넉넉히_아래로만_받는다(monkeypatch):
    """시간 층 — 게이트웨이 호출 한도 600초가 바깥 층 가운데 가장 작다. 기다림 상한이 그보다 크면 기다리던 호출이 게이트웨이에서 오류로
    끊긴다(안쪽이 먼저 걸려야 한다). 수가 아니면 기본값, 0 보다 작으면 0(기다림 끔)이다."""
    for raw, want in (("", 240.0), ("90", 90.0), ("600", m.STATUS_WAIT_CEIL_S), ("1e9", m.STATUS_WAIT_CEIL_S),
                      ("abc", 240.0), ("nan", 240.0), ("-3", 0.0), ("0", 0.0)):
        assert m._status_wait_cap({"DELIB_STATUS_WAIT_MAX_S": raw}) == want, raw
    assert m.STATUS_WAIT_CEIL_S < 600 and m._status_wait_cap({}) == 240.0


# ── 일찍 돌아오기 — 서버가 내려간다 · 새 심의를 멈췄다 · 이 서버가 돌리지 않는 잡 ─────────────────────────────────────────
def test_서버가_내려가기_시작하면_기다리던_조회가_곧바로_돌아온다(remote):
    """종료 신호를 받으면(delib_jobs.closing) uvicorn 은 열린 연결을 AGENT_GRACEFUL_S(기본 1초)까지만 기다린다 — 그 안에 답한다."""
    async def go():
        job = await _running(remote)
        call = asyncio.ensure_future(m.deliberate_status(job["id"], wait_s=4))
        await asyncio.sleep(0.2)
        delib_jobs.closing(True)
        t0 = time.monotonic()
        out = await asyncio.wait_for(call, 3)
        took = time.monotonic() - t0
        again = await m.deliberate_status(job["id"], wait_s=4)        # 내려가는 중에 온 조회 — 기다리지 않는다
        return out, took, again

    out, took, again = _play(go)
    assert out["wait"]["woke"] == "closing" and took < 0.5, (out["wait"], took)
    assert again["wait"]["woke"] == "closing" and again["wait"]["waited_s"] == 0.0, again["wait"]


def test_기다리는_사이_새_심의를_멈추면_돌아오고_이미_멈춘_뒤면_종전처럼_기다린다(remote, monkeypatch):
    """운영의 새 심의 멈춤(POST /drain scope delib)은 재기동 앞에 선다 — 그때 기다리던 조회는 일찍 돌아온다. 이미 멈춘 뒤에 온 조회까지 곧바로
    돌려보내면 부르는 쪽이 멈춤 내내(최대 6시간 · 사람의 고정은 끝없이) 쉬지 않고 다시 부른다 — 그때는 종전처럼 기다린다."""
    monkeypatch.setattr(m, "STATUS_WAIT_MAX_S", 0.4)

    async def go():
        job = await _running(remote)
        call = asyncio.ensure_future(m.deliberate_status(job["id"], wait_s=0.4))
        await asyncio.sleep(0.1)
        delib_jobs.pause_state().set_on(scope="delib", by="update-all", ttl_s=60)
        first = await asyncio.wait_for(call, 3)
        t0 = time.monotonic()
        second = await m.deliberate_status(job["id"], wait_s=0.4)
        return first, second, time.monotonic() - t0

    first, second, took = _play(go)
    assert first["wait"]["woke"] == "paused" and first["wait"]["waited_s"] < 0.4, first["wait"]
    assert second["wait"]["woke"] == "timeout" and took >= 0.4, (second["wait"], took)


def test_멈춤이_풀렸다가_기다리는_사이_다시_서도_돌아온다(remote, monkeypatch):
    """깨는 것은 꺼짐 → 켜짐이다 — 부를 때 서 있던 멈춤이 풀린 뒤 다시 서면(다음 update-all) 그것도 재기동 앞이다."""
    async def go():
        job = await _running(remote)
        st = delib_jobs.pause_state()
        st.set_on(scope="delib", by="update-all", ttl_s=60)
        call = asyncio.ensure_future(m.deliberate_status(job["id"], wait_s=4))
        await asyncio.sleep(0.15)
        held = not call.done()
        st.set_off(by="update-all")
        await asyncio.sleep(0.15)
        st.set_on(scope="delib", by="update-all", ttl_s=60)
        return held, await asyncio.wait_for(call, 3)

    held, out = _play(go)
    assert held and out["wait"]["woke"] == "paused", out["wait"]


def test_이_서버가_돌리지_않는_잡은_기다리지_않는다(remote):
    """원장 파일에만 '도는 중' 으로 남은 잡(다른 프로세스가 쓴 원장 · 재기동 정리 전) — 이 프로세스에서는 바뀌지 않는다. 기다리면 원장 파일(수
    MB)을 상한까지 거듭 읽는다."""
    job = {"id": "default-20261102-000000-aaaaaa", "status": "running", "question": "q", "stage": "r2", "round": 2,
           "started_at": delib_jobs._now(), "updated_at": delib_jobs._now(), "turns": []}
    delib_jobs._persist(job)
    t0 = time.monotonic()
    out = asyncio.run(m.deliberate_status(job["id"], wait_s=4))
    assert out["wait"]["woke"] == "untracked" and out["wait"]["waited_s"] == 0.0 and time.monotonic() - t0 < 0.5, out["wait"]


# ── brief — 부를 때마다 같은 큰 칸을 머리 · 수로 ────────────────────────────────────────────────────────────────────
_DROPPED = ("question", "seats", "warnings", "evidence_omitted")
_ADDED = ("question_head", "question_chars", "seat_count", "evidence_omitted_total", "brief")


def _ledger_job(**kw) -> dict:
    """원장 한 건(끝난 잡) — 화두는 길고 좌석 · 경고 · 빠진 근거가 있다."""
    now = delib_jobs._now()
    job = {"id": "risk-review-20261102-010101-abcdef", "job": "risk-review", "kind": "risk-review", "label": "리스크 심사",
           "status": "done", "question": "변경 요지 — " + "보스 리브 두께 0.8 → 0.6 mm · " * 1500, "chair_template": "risk-review",
           "opts": {"sealed": None}, "stage": "report", "step": "Report Archive 보고서 저장 중", "round": 3, "total_rounds": 3,
           "seats": [{"key": f"seat-{i}", "role": "역할 " * 40} for i in range(20)], "turns": [{"persona": "seat-0", "round": 1, "say": "말"}],
           "decision": "결정문", "report_id": 41, "error": None, "warnings": ["좌석 유실 — seat-3 " * 20] * 10, "warnings_total": 14,
           "evidence_omitted": [{"source": f"근거 {i}", "text": "빠진 까닭 " * 40} for i in range(30)]
           + [{"note": "상한 30건 초과 — 그 뒤로 온 7건 중 2건은 기록하지 않고 마지막 5건만 아래에 남긴다"}]
           + [{"source": f"늦은 근거 {i}", "text": "까닭"} for i in range(5)],
           "evidence_omitted_over": 7, "checkpoint": None,
           "started_at": now - 3600, "updated_at": now, "finished_at": now, "user": "", "owner_aff": ""}
    job.update(kw)
    return job


def _put(job: dict) -> dict:
    delib_jobs._JOBS[job["id"]] = job
    return job


def test_brief_는_화두_전문과_목록을_빼고_머리와_수만_준다(remote):
    job = _put(_ledger_job())
    full = asyncio.run(m.deliberate_status(job["id"]))
    out = asyncio.run(m.deliberate_status(job["id"], brief=True))
    assert "page" in full, "시험 전제 — 화두 전문 때문에 종전 응답은 쪽으로 나뉜다"
    assert "page" not in out and len(mcp_pages.wire(out)) < 1500, len(mcp_pages.wire(out))
    q = job["question"]
    assert out["question_head"] == q[:80] + "…" and out["question_chars"] == len(q), out["question_head"]
    assert out["seat_count"] == 20 and out["warnings_total"] == 14 and out["evidence_omitted_total"] == 37, out
    assert out["brief"] is True and not [k for k in _DROPPED if k in out], sorted(out)


@pytest.mark.parametrize("kind", ["done", "interrupted", "queued_like", "error"])
def test_brief_는_다음_걸음을_정할_칸을_그대로_둔다(remote, kind):
    """뺀 칸 말고는 종전 응답의 칸이 같은 값으로 다 있다 — 상태 · 단계 · 라운드 · 걸린 시간 · 턴 수 · 오류 · 재개(이어하기) · 시도 · 보고서 번호 ·
    마무리 중 · 외부 응답자 표지."""
    extra = {"done": {},
             "interrupted": {"status": "interrupted", "error": delib_jobs.INTERRUPTED_MSG, "stage": "r2", "round": 2,
                             "resumed_from": "default-prev", "attempts": [{"cut_at": 1, "resumed_at": 2, "by": "me", "round": 2}]},
             "queued_like": {"status": "cancelled", "error": "취소됨 — 줄을 선 채로 접었다", "queued_at": 1.0, "started_at": None},
             "error": {"status": "error", "error": "RuntimeError: 엔진이 터졌다", "opts": {"llm": "external"}, "tool_mode": "tools"}}[kind]
    job = _put(_ledger_job(question="짧은 화두", **extra))
    full = asyncio.run(m.deliberate_status(job["id"]))
    out = asyncio.run(m.deliberate_status(job["id"], brief=True))
    keep = {k: v for k, v in full.items() if k not in _DROPPED}
    assert {k: v for k, v in out.items() if k not in _ADDED} == keep, sorted(set(full) ^ set(out))


def test_brief_가_쪽으로_나뉘면_다음_쪽도_brief_로_부르게_한다(remote, monkeypatch):
    """줄인 응답도 쪽 크기를 넘으면(아주 긴 오류 글) 쪽으로 나눈다 — page.next 가 brief 를 실어야 다음 쪽이 같은 꼴로 이어진다."""
    monkeypatch.setattr(mcp_pages, "PAGE_CHARS", 4000)
    job = _put(_ledger_job(status="error", error="RuntimeError: " + "엔진 호출 실패 사유 " * 600))
    p1 = asyncio.run(m.deliberate_status(job["id"], brief=True))
    assert p1["page"]["next"] == {"job_id": job["id"], "brief": True, "part": 2}, p1["page"]
    text, part = "", 1
    while True:
        page = asyncio.run(m.deliberate_status(job["id"], brief=True, part=part))
        text += page.get("error") or ""
        if page["page"]["next"] is None:
            break
        part = page["page"]["next"]["part"]
    assert text == job["error"], "쪽을 이은 오류 글이 원장과 다르다"


def test_기다린_응답이_쪽으로_나뉘면_다음_쪽은_기다리지_않는다(remote):
    """화두가 길어 쪽으로 나뉘는 응답 — 첫 쪽에 기다림 칸이 있고, page.next 에는 wait_s 가 없다(다음 쪽은 곧바로 온다)."""
    job = _put(_ledger_job())
    p1 = asyncio.run(m.deliberate_status(job["id"], wait_s=1))
    assert p1["wait"]["woke"] == "ended" and p1["page"]["part"] == 1, p1.get("wait")
    nxt = p1["page"]["next"]
    assert nxt == {"job_id": job["id"], "part": 2}, nxt
    t0 = time.monotonic()
    p2 = asyncio.run(m.deliberate_status(**nxt))
    assert "wait" not in p2 and time.monotonic() - t0 < 0.5


# ── 기본 호출은 종전과 바이트까지 같다 ──────────────────────────────────────────────────────────────────────────────
async def _old_status(job_id: str, part: int = 1) -> dict:
    """종전(main 9f08507) deliberate_status 의 몸통 그대로 — 기본 호출(wait_s=0 · brief=False)이 이것과 바이트까지 같아야 한다."""
    job = delib_jobs.get(job_id)
    out = delib_jobs.summary(job)
    out["evidence_omitted"] = job.get("evidence_omitted") or []
    out["warnings"] = job.get("warnings") or []
    out["warnings_total"] = int(job.get("warnings_total") or len(out["warnings"]))
    if "idle_s" in out:
        out["quiet_ok_s"] = m._quiet_ok_s(job)
    return mcp_pages.paged(out, part=part, ids=m._JOB_IDS, tail=m._JOB_TAIL, tail_page=True, again={"job_id": job_id})


def test_기본_호출은_종전_응답과_바이트까지_같다(remote, monkeypatch):
    """루틴(routine._job_status — {job_id} 만) · 포털 · 게이트웨이는 기본값으로 부른다 — 새 인자를 안 주면 응답이 한 글자도 바뀌지 않는다."""
    monkeypatch.setattr(delib_jobs, "_now", lambda: 1_000_000.0)

    async def go():
        running = await _running(remote, "도는 화두")
        done = _put(_ledger_job())
        short = _put(_ledger_job(id="default-20261102-020202-bbbbbb", question="짧은 화두", status="interrupted",
                                 error=delib_jobs.INTERRUPTED_MSG))
        rows = []
        for jid in (running["id"], done["id"], short["id"]):
            first = await _old_status(jid)
            parts = (first.get("page") or {}).get("parts") or 1
            for p in range(1, parts + 1):
                old = mcp_pages.wire(await _old_status(jid, p))
                rows.append((jid, p, old, mcp_pages.wire(await m.deliberate_status(jid, part=p)),
                             mcp_pages.wire(await m.deliberate_status(jid, part=p, wait_s=0, brief=False))))
        return rows

    rows = _play(go)
    assert len(rows) > 3, "시험 전제 — 쪽으로 나뉜 응답도 본다"
    for jid, p, old, new, explicit in rows:
        assert new == old, f"{jid} {p}쪽 — 기본 호출의 응답이 종전과 다르다"
        assert explicit == old, f"{jid} {p}쪽 — wait_s=0 · brief=False 를 적어 부른 응답이 종전과 다르다"


# ── 설명 — 고리로 묻지 말고 wait_s 로 한 번 ───────────────────────────────────────────────────────────────────────
def _listed(name: str):
    return next(t for t in asyncio.run(m.mcp.list_tools()) if t.name == name)


def test_진행_조회_설명이_고리_대신_wait_s_한_번을_가르친다():
    t = _listed("deliberate_status")
    props = t.inputSchema["properties"]
    assert "wait_s" in t.description and "고리" in t.description, t.description[:300]
    assert props["wait_s"]["default"] == 0 and props["brief"]["default"] is False
    assert f"{m.STATUS_WAIT_MAX_S:g}초" in props["wait_s"]["description"], props["wait_s"]["description"]
    for code in ("ended", "changed", "timeout", "closing", "paused", "untracked"):
        assert code in props["wait_s"]["description"], code
    assert "question_head" in props["brief"]["description"]
    assert t.inputSchema.get("required") == ["job_id"]
