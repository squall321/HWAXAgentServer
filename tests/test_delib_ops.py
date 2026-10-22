# 새 심의 멈춤 · 재기동을 넘기는 줄 · 끊긴 잡 이어하기(HWAXPortal docs/delib-ops R1 · R2 · R3)
#
# 왜 생겼나. 10-22 새벽 한도 하나를 바꾸려는 재기동이 도는 잡과 줄 선 잡을 모두 닫을 판이었고, 그 사이 새 심의를 잠시 멈출 스위치가
# 없어 운영이 '아무도 안 거는 때' 를 기다렸다 — 그동안 줄 선 잡이 빈자리에 들어가 '잡아 두고 재기동' 할 때를 놓쳤다. 이제 멈추면 새 잡을
# 받지 않고 줄도 붙잡는다(R1), 줄은 시작 인자 파일로 재기동을 넘겨 같은 차례로 다시 선다(R2), 재기동으로 끊긴 잡은 이어하기 한 번으로
# 남은 라운드만 돈다(R3).
#
# **실제 asyncio 태스크를 돌린다** — 가짜는 엔진뿐이다(test_delib_job_queue 의 _Engine).
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_ops.py -q
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_job_queue import ME, YOU, _TOKEN, _caps, _end, _on_disk, _play, _st, _start, _tick, eng  # noqa: E402, F401
from test_delib_silent_drops import _pin_context  # noqa: E402, F401


@pytest.fixture(autouse=True)
def _fresh_pause(monkeypatch):
    monkeypatch.setattr(delib_jobs, "_PAUSE", {})
    monkeypatch.setattr(delib_jobs, "_REQUEUED", 0)


def _pause(ttl_s=1200, by="운영", **kw):
    return delib_jobs.pause_state().set_on(scope="delib", by=by, reason=kw.pop("reason", "한도 변경"), ttl_s=ttl_s, **kw)


def _ctx(user, aff="", groups=""):
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={
        "x-hwax-user": user, **({"x-hwax-affiliation": aff} if aff else {}), **({"x-hwax-groups": groups} if groups else {})})))


# ── R1 새 심의 멈춤 ──────────────────────────────────────────────────────────
def test_멈춤이면_시작은_점검_중으로_거절되고_retry_after_를_싣는다(eng, monkeypatch):
    _caps(monkeypatch, 2)
    _pause(ttl_s=1200)
    with pytest.raises(delib_jobs.DelibPaused) as e:
        _start("가")
    assert 1100 <= e.value.retry_after_s <= 1200 and "점검 중 — 한도 변경" in str(e.value) and "약 20분" in str(e.value)
    assert "retry_after_s=" in str(e.value) and delib_jobs._JOBS == {} and not list(delib_jobs.JOB_DIR.glob("*.json"))
    with pytest.raises(delib_jobs.DelibPaused):
        asyncio.run(m.deliberate_start(question="힌지 크랙"))       # MCP 도구도 같은 거절(도구 오류의 글)


def test_사람이_고정한_멈춤은_10분_뒤를_말한다(eng, monkeypatch):
    _caps(monkeypatch, 2)
    delib_jobs.pause_state().set_on(scope="delib", by="운영", hold=True)
    with pytest.raises(delib_jobs.DelibPaused) as e:
        _start("가")
    assert e.value.retry_after_s == delib_jobs.PAUSE_HOLD_RETRY_S


def test_멈춤이면_이어하기도_거절된다(eng, monkeypatch):
    _caps(monkeypatch, 2)
    done = {"id": "default-20261022-000000-done01", "job": "default", "kind": "default", "question": "힌지", "status": "done",
            "decision": "결정: 보강한다", "opts": {}, "turns": [], "seats": [], "user": "", "updated_at": 1.0}
    delib_jobs._persist(done)
    _pause()
    with pytest.raises(delib_jobs.DelibPaused):
        asyncio.run(m.deliberate_continue(previous_job_id=done["id"], human_note="의견"))


def test_멈춤이면_줄_선_잡을_띄우지_않고_도는_잡은_끝까지_가고_풀면_띄운다(eng, monkeypatch):
    _caps(monkeypatch, 1)

    async def scenario():
        a, b = _start("가"), _start("나")
        await _tick()
        assert [_st(a), _st(b)] == ["running", "queued"]
        _pause()
        await _end(eng, a)
        assert _st(a) == "done" and _st(b) == "queued", "멈춤인데 줄 선 잡을 빈자리에 띄웠다(10-22 06:35 의 그 일)"
        delib_jobs.pause_state().set_off(by="운영")
        delib_jobs._pump()
        await _tick()
        assert _st(b) == "running"
    _play(scenario)


def test_멈춤이_수명으로_풀리면_주기_펌프가_줄을_띄운다(eng, monkeypatch):
    _caps(monkeypatch, 1)
    monkeypatch.setattr(delib_jobs, "PUMP_TICK_S", 0.01)
    clock = [1_000_000.0]
    delib_jobs.pause_state().clock = lambda: clock[0]

    async def scenario():
        a, b = _start("가"), _start("나")
        await _tick()
        _pause(ttl_s=60)
        delib_jobs.start_ticker()
        try:
            await _end(eng, a)
            await asyncio.sleep(0.05)
            assert _st(b) == "queued"
            clock[0] += 61                                  # 멈춤의 수명이 끝났다 — 아무도 /drain 을 부르지 않았다
            for _ in range(50):
                await asyncio.sleep(0.01)
                if _st(b) == "running":
                    break
            assert _st(b) == "running", "수명으로 풀린 멈춤 뒤에 줄이 그대로다 — 다음 잡이 끝날 때까지 기다린다"
        finally:
            delib_jobs.stop_ticker()
    _play(scenario)


def test_drain_scope_delib_는_멈춤만_세우고_카드_대조_깃발과_섞이지_않는다(eng, monkeypatch):
    calls = []
    monkeypatch.setattr(delib_jobs, "_pump", lambda: calls.append(1))
    c = TestClient(app.app)
    r = c.post("/drain", json={"on": True, "scope": "delib", "by": "update-all", "reason": "에이전트 재기동", "ttl_s": 600})
    assert r.status_code == 200 and r.json()["on"] is True and r.json()["scope"] == "delib" and r.json()["retry_after_s"] >= 540
    g = c.get("/drain").json()
    assert g["on"] is False and "delib_pause" not in g, "카드 대조 깃발이 섰거나 그 응답 모양(나눠 쓰는 계약)이 바뀌었다"
    h = c.get("/health").json()
    assert h["delib_pause"]["by"] == "update-all" and h["delib_pause"]["retry_after_s"] >= 540
    assert h["delib_paused"] is True and h["delib_pause"]["reason"] == "에이전트 재기동" and h["drain"]["on"] is False
    # 종전 끄기(scope 를 안 적은 요청)는 카드 대조 깃발이다 — 멈춤은 그대로다
    assert c.post("/drain", json={"on": False, "by": "update-all"}).status_code == 200
    assert c.get("/health").json()["delib_paused"] is True
    # 주인이 아니면 못 끈다(비우기 깃발과 같은 규칙) · until_idle 은 받지 않는다
    assert c.post("/drain", json={"on": False, "scope": "delib", "by": "남"}).status_code == 409
    assert c.post("/drain", json={"on": True, "scope": "delib", "by": "남", "ttl_s": 60}).json()["code"] == "drain_owned"
    r = c.post("/drain", json={"on": True, "scope": "delib", "by": "update-all", "ttl_s": 60,
                               "until_idle": {"grace_s": 10, "max_s": 60}})
    assert r.status_code == 422 and "멈춤은 until_idle 을 받지 않는다" in r.json()["message"], "until_idle 을 말없이 버렸다"
    assert calls == [], "끄지도 않았는데 줄을 띄웠다"
    r = c.post("/drain", json={"on": False, "scope": "delib", "by": "update-all"})
    assert r.status_code == 200 and r.json()["on"] is False and calls == [1], "멈춤을 내리고 붙잡아 둔 줄을 띄우지 않았다"
    assert c.get("/health").json()["delib_paused"] is False


def test_카드_대조_모듈이_없어도_멈춤은_받는다(eng, monkeypatch):
    monkeypatch.setattr(app, "_card_review", None)
    c = TestClient(app.app)
    assert c.post("/drain", json={"on": True, "scope": "all", "by": "u", "ttl_s": 60}).status_code == 503
    assert c.post("/drain", content=b"{not json").status_code == 503, "종전 그대로 — 모듈이 없으면 무엇을 보내든 503"
    assert c.post("/drain", json={"on": True, "scope": "delib", "by": "u", "ttl_s": 60}).status_code == 200


def _sse_of(text: str) -> list:
    return [(ev, json.loads(data)) for ev, data in
            ((b.split("\n")[0][7:], b.split("\n")[1][6:]) for b in text.strip().split("\n\n") if b.startswith("event: "))]


def test_웹_세_트리거도_멈춤이면_점검_중이고_앱이_띄운_패널은_막지_않는다(eng, monkeypatch):
    seen = []

    async def _fake(req):
        seen.append(req.message)
        yield d._sse("result", {"type": "text", "content": "결정"})
        yield d._sse("done", {})
    monkeypatch.setattr(app, "_delib_with_topic", _fake)
    _pause()
    c = TestClient(app.app)
    for msg in ("/심의 힌지 크랙 원인", "/시뮬심의 낙하 충격", "/시험계획 접착 강도"):
        evs = _sse_of(c.post("/chat", json={"message": msg}).text)
        assert evs[0][0] == "error" and evs[0][1]["code"] == "delib_paused" and evs[-1][0] == "done", (msg, evs)
        assert evs[0][1]["retry_after_s"] >= 60 and "점검 중" in evs[0][1]["message"]
    evs = _sse_of(c.post("/chat", json={"message": "/심의 패널 쟁점", "delib_opts": {"started_by": "app"}}).text)
    assert ("result", {"type": "text", "content": "결정"}) in evs and seen == ["/심의 패널 쟁점"], evs
    delib_jobs.pause_state().set_off(by="운영")
    evs = _sse_of(c.post("/chat", json={"message": "/심의 힌지"}).text)
    assert all(e[1].get("code") != "delib_paused" for e in evs)


# ── R2 줄이 재기동을 넘긴다 ──────────────────────────────────────────────────
def test_시작_인자_파일은_0600_이고_토큰이_없고_끝나면_지운다(eng, monkeypatch):
    _caps(monkeypatch, 1)

    async def scenario():
        a = _start("가", ME, delib_opts={"human_note": "현장 의견", "evidence": [{"source": "측정", "result": "본문 " * 50}]})
        b = _start("나", ME)
        p = delib_jobs._args_path(a["id"])
        assert oct(os.stat(p).st_mode & 0o777) == "0o600"
        args = json.loads(p.read_text(encoding="utf-8"))
        assert args["question"] == "가" and args["opts"]["human_note"] == "현장 의견" and args["opts"]["evidence"][0]["source"] == "측정"
        assert args["user_email"] == ME and "pat" not in json.dumps(args).lower()
        assert a["durable"] is True and delib_jobs.queue_durability()["durable"] is True
        await _end(eng, a)
        assert not p.exists(), "끝난 잡의 시작 인자 파일(근거 본문)이 남았다"
        await _tick()
        c = _start("다", ME)
        assert _st(c) == "queued" and delib_jobs._args_path(c["id"]).exists()
        delib_jobs.cancel(c["id"])
        assert not delib_jobs._args_path(c["id"]).exists(), "줄에서 접은 잡의 파일(근거 본문)이 남았다"
        return b
    _play(scenario)


def test_토큰을_쥔_잡은_시작_인자_파일을_쓰지_않고_줄은_재기동을_넘긴다고_말하지_않는다(eng, monkeypatch):
    _caps(monkeypatch, 1)

    async def scenario():
        _start("가")
        b = _start("나", ME, user_pat=_TOKEN)
        assert not delib_jobs._args_path(b["id"]).exists() and b["durable"] is False
        assert delib_jobs.queue_durability()["durable"] is False, "재기동에 사라질 잡이 줄에 있는데 넘긴다고 말했다"
        assert not any(_TOKEN in f.read_text(encoding="utf-8") for f in delib_jobs.JOB_DIR.rglob("*.json"))
    _play(scenario)


def test_시작_인자를_못_쓰면_그_잡은_durable_거짓(eng, monkeypatch):
    _caps(monkeypatch, 1)
    real = os.open

    def _deny(path, *a, **k):
        if "/args/" in str(path):
            raise PermissionError(13, "권한 없음")
        return real(path, *a, **k)
    monkeypatch.setattr(delib_jobs.os, "open", _deny)

    async def scenario():
        _start("가")
        b = _start("나")
        assert b["durable"] is False and delib_jobs.queue_durability()["durable"] is False
    _play(scenario)


def _crash(eng, monkeypatch, states: dict, queued_at: dict | None = None):
    """잡 셋을 띄워 두고 프로세스가 죽은 꼴을 만든다 — 원장과 시작 인자 파일만 남고 메모리는 빈다. (화두 → 잡)을 돌려준다."""
    _caps(monkeypatch, 1)
    saved = {}

    async def scenario():
        jobs = {q: _start(q, ME, delib_opts={"human_note": f"{q} 의견"}) for q in states}
        await _tick()
        for j in jobs.values():
            saved[j["id"]] = delib_jobs._args_path(j["id"]).read_bytes()
        return jobs
    jobs = _play(scenario)                        # 뒷정리가 잡을 접고 파일을 지운다 — 아래에서 죽기 전 꼴로 되돌린다
    for q, j in jobs.items():
        j["status"] = states[q]
        if queued_at and q in queued_at:
            j["queued_at"] = queued_at[q]
        delib_jobs._persist(j)
        delib_jobs._args_path(j["id"]).write_bytes(saved[j["id"]])
    for store in (delib_jobs._JOBS, delib_jobs._TASKS, delib_jobs._PENDING):
        store.clear()
    eng._gates.clear()                            # 가짜 엔진의 문은 죽은 프로세스(앞 이벤트 루프)의 것이다
    return jobs


def test_재기동_뒤_줄_선_잡은_처음_줄_선_차례로_다시_서고_도는_잡은_끊긴다(eng, monkeypatch):
    jobs = _crash(eng, monkeypatch, {"가": "running", "나": "queued", "다": "queued"}, queued_at={"나": 2000.0, "다": 1000.0})
    eng.started.clear()
    m.bind(object())                              # 다음 기동
    assert list(delib_jobs._PENDING) == [jobs["다"]["id"], jobs["나"]["id"]], "먼저 줄 선 잡(다)이 앞이어야 한다"
    assert [_on_disk(jobs[q])["status"] for q in ("가", "나", "다")] == ["interrupted", "queued", "queued"]
    assert _on_disk(jobs["나"])["requeued"] == 1 and delib_jobs._REQUEUED == 2
    h = TestClient(app.app).get("/health").json()
    assert (h["delib_requeued"], h["delib_queue_durable"], h["delib_queued"]) == (2, True, 2)

    async def after():
        delib_jobs._pump()
        await _tick()
        assert eng.started == ["다"], eng.started
        assert eng.seen["다"].opts["human_note"] == "다 의견" and eng.seen["다"].user == ME, "처음 인자로 다시 서지 않았다"
    _play(after)


def test_멈춘_채로_재기동하면_다시_선_줄은_붙잡혀_있다가_풀면_든다(eng, monkeypatch):
    jobs = _crash(eng, monkeypatch, {"가": "queued", "나": "queued"}, queued_at={"가": 1000.0, "나": 2000.0})
    _pause()
    eng.started.clear()
    m.bind(object())

    async def after():
        delib_jobs._pump()
        await _tick()
        assert eng.started == [] and [_st(delib_jobs._JOBS[j["id"]]) for j in jobs.values()] == ["queued", "queued"]
        delib_jobs.pause_state().set_off(by="운영")
        delib_jobs._pump()
        await _tick()
        assert eng.started == ["가"]
    _play(after)


def test_시작_인자_파일이_없거나_깨진_줄_선_잡은_종전처럼_닫힌다(eng, monkeypatch):
    jobs = _crash(eng, monkeypatch, {"가": "queued", "나": "queued", "다": "queued"})
    delib_jobs._args_path(jobs["가"]["id"]).unlink()
    delib_jobs._args_path(jobs["나"]["id"]).write_text("{깨짐", encoding="utf-8")
    m.bind(object())
    for q in ("가", "나"):
        rec = _on_disk(jobs[q])
        assert rec["status"] == "interrupted" and "시작하지 못했다" in rec["error"], rec["error"]
    assert list(delib_jobs._PENDING) == [jobs["다"]["id"]]
    assert not delib_jobs._args_path(jobs["나"]["id"]).exists(), "닫은 잡의 깨진 파일이 남았다"


def test_재기동_정리는_끝난_잡과_오래된_끊긴_잡의_시작_인자를_치운다(eng, monkeypatch):
    jobs = _crash(eng, monkeypatch, {"가": "done", "나": "interrupted", "다": "interrupted", "라": "interrupted"})
    for q, extra in (("나", {"finished_at": delib_jobs._now() - delib_jobs.ARGS_KEEP_S - 10}),
                     ("다", {"finished_at": delib_jobs._now()}), ("라", {"finished_at": delib_jobs._now(), "resumed_by": "x"})):
        delib_jobs._persist({**_on_disk(jobs[q]), **extra})
    m.bind(object())
    left = {q for q, j in jobs.items() if delib_jobs._args_path(j["id"]).exists()}
    assert left == {"다"}, f"남은 시작 인자 파일 {left} — 끊긴 지 얼마 안 되고 이어 돌리지 않은 잡의 것만 남아야 한다"


# ── R3 끊긴 잡 이어하기 ───────────────────────────────────────────────────────
def _interrupted(stage="r3", total=4, *, args_opts=None, user=ME, report_id=None, job_id="default-20261022-010000-cut001",
                 rounds_so_far=0):
    """재기동으로 끊긴 잡의 원장 — 두 좌석이 1~2라운드를 마쳤고 3라운드는 한 좌석만 말했다."""
    turns = [{"round": r, "display_round": r + rounds_so_far, "persona": p, "say": f"{p} {r}R 줄임",
              "say_full": f"{p} {r}R 입장 전문"} for r in (1, 2) for p in ("기구", "열")]
    turns.append({"round": 3, "display_round": 3 + rounds_so_far, "persona": "기구", "say": "기구 3R 반쯤"})
    job = {"id": job_id, "job": "default", "kind": "default", "label": "자유 심의", "question": "힌지 크랙 원인",
           "chair_template": None, "opts": {"rounds": total, **({"rounds_so_far": rounds_so_far} if rounds_so_far else {})},
           "status": "interrupted", "stage": stage, "step": "", "steps": [], "round": 3,
           "seats": [{"key": "mech-hinge", "role": "기구 요약"}, {"key": "thermal", "role": "열 요약"}],
           "seat_roles": {"mech-hinge": "기구 역할 전문"}, "total_rounds": total, "turns": turns, "decision": None,
           "result_text": None, "report_id": report_id, "plain": None, "checkpoint": None, "error": delib_jobs.INTERRUPTED_MSG,
           "warnings": [], "evidence_omitted": [], "started_at": 1000.0, "updated_at": 2000.0, "finished_at": 2000.0,
           "user": user, "owner_aff": "CAEG", "started_by": "human"}
    delib_jobs._persist(job)
    if args_opts is not None:
        assert delib_jobs._write_args(job_id, {"job_kind": "default", "question": job["question"], "groups": ["CAEG"],
                                               "opts": args_opts, "user_email": user, "owner": "", "owner_aff": "CAEG"})
    return job


_ORIG = {"rounds": 4, "human_note": "현장 의견", "evidence": [{"source": "측정", "result": "본문"}],
         "personas": [{"key": "mech-hinge", "role": "기구 역할 전문"}], "timeout_s": 3600}


def _resume(eng, job_id, **kw):
    async def go():
        out = await m.deliberate_continue(previous_job_id=job_id, **kw)
        await _tick()
        return out
    return _play(go)


def test_끊긴_잡은_끝난_라운드_수를_싣는다(eng, monkeypatch):
    job = _interrupted(args_opts=_ORIG)
    st = asyncio.run(m.deliberate_status(job["id"]))
    assert st["resume"] == {"rounds_done": 2, "total_rounds": 4, "rounds_left": 2, "args": True, "resumed_by": None}
    assert "deliberate_continue" in st["error"]


def test_이어하기는_사람_의견_없이_남은_라운드만_처음_인자로_돈다(eng, monkeypatch):
    _caps(monkeypatch, 2)
    job = _interrupted(args_opts=_ORIG)
    out = _resume(eng, job["id"])
    o = eng.seen["힌지 크랙 원인"].opts
    assert (o["rounds"], o["rounds_so_far"]) == (2, 2), "남은 라운드 · 이어 센 라운드 번호가 아니다"
    cs = o["continue_summary"]
    assert "끝난 2라운드" in cs and "결정문은 아직 없다" in cs and "기구 2R 입장 전문" in cs and "열 2R 입장 전문" in cs
    assert "3R" not in cs.replace("2R", ""), "끝나지 않은 3라운드 발언을 요약에 실었다"
    assert o["human_note"] == "현장 의견" and o["evidence"] == _ORIG["evidence"] and o["timeout_s"] == 3600, "처음 인자를 잇지 못했다"
    assert [p["key"] for p in o["personas"]] == ["mech-hinge", "thermal"] and o["personas"][0]["role"] == "기구 역할 전문"
    assert out["resumed"] == {"rounds_done": 2, "rounds": 2, "args": True, "rounds_start_at": 3} and out["continued_from"] == job["id"]
    prev = _on_disk(job)
    assert prev["resumed_by"] == out["job_id"] and not delib_jobs._args_path(job["id"]).exists()
    assert _on_disk({"id": out["job_id"]})["resumed_from"] == job["id"]
    with pytest.raises(ValueError, match="이미 이어 돌렸다"):
        asyncio.run(m.deliberate_continue(previous_job_id=job["id"]))


def test_결정문에서_끊긴_잡은_2라운드로_잇고_첫_라운드에서_끊긴_잡은_처음부터(eng, monkeypatch):
    _caps(monkeypatch, 2)
    a = _interrupted(stage="decide", total=3, args_opts=_ORIG, job_id="default-20261022-010000-cut00a")
    assert asyncio.run(m.deliberate_status(a["id"]))["resume"]["rounds_left"] == 2, "엔진 하한(2)을 밑도는 수를 알렸다"
    _resume(eng, a["id"])
    o = eng.seen["힌지 크랙 원인"].opts
    assert (o["rounds"], o["rounds_so_far"]) == (2, 3) and "끝난 3라운드" in o["continue_summary"]
    eng.seen.clear()
    last = _interrupted(stage="r4", total=4, args_opts=_ORIG, job_id="default-20261022-010000-cut00c")
    assert asyncio.run(m.deliberate_status(last["id"]))["resume"]["rounds_left"] == 2
    _resume(eng, last["id"])
    assert eng.seen["힌지 크랙 원인"].opts["rounds"] == 2, "마지막 라운드에서 끊긴 잡을 1라운드로 청했다(엔진 하한 2)"
    eng.seen.clear()
    b = _interrupted(stage="r1", total=4, args_opts=_ORIG, job_id="default-20261022-010000-cut00b")
    out = _resume(eng, b["id"])
    o = eng.seen["힌지 크랙 원인"].opts
    assert o["rounds"] == 4 and "continue_summary" not in o and "rounds_so_far" not in o, "끝난 라운드가 없는데 이어 셌다"
    assert out["resumed"]["rounds_done"] == 0 and "처음부터" in out["note"]


def test_사람_의견을_주면_처음_의견_뒤에_덧붙이고_화두_종류는_못_바꾼다(eng, monkeypatch):
    _caps(monkeypatch, 2)
    job = _interrupted(args_opts=_ORIG)
    with pytest.raises(ValueError, match="같은 화두"):
        asyncio.run(m.deliberate_continue(previous_job_id=job["id"], question="다른 화두"))
    with pytest.raises(ValueError, match="같은 화두"):
        asyncio.run(m.deliberate_continue(previous_job_id=job["id"], job="diagnosis"))
    _resume(eng, job["id"], human_note="재기동 뒤 측정값 추가")
    assert eng.seen["힌지 크랙 원인"].opts["human_note"] == "현장 의견\n\n[이어 돌리며 덧붙인 의견]\n재기동 뒤 측정값 추가"


def test_이어하기였던_잡을_이으면_앞_결정_요약_뒤에_붙는다(eng, monkeypatch):
    _caps(monkeypatch, 2)
    job = _interrupted(args_opts={**_ORIG, "continue_summary": "앞 회차 결정: 보강", "rounds_so_far": 3}, rounds_so_far=3)
    _resume(eng, job["id"])
    o = eng.seen["힌지 크랙 원인"].opts
    assert o["continue_summary"].startswith("앞 회차 결정: 보강\n\n[서버 재기동으로 끊긴") and o["rounds_so_far"] == 5


def test_시작_인자_파일이_없는_끊긴_잡도_잇고_근거를_못_이었다고_말한다(eng, monkeypatch):
    _caps(monkeypatch, 2)
    job = _interrupted(args_opts=None)
    out = _resume(eng, job["id"])
    o = eng.seen["힌지 크랙 원인"].opts
    assert o["rounds"] == 2 and "evidence" not in o and "시작 인자 파일이 없는" in out["note"] and out["resumed"]["args"] is False


def test_남의_끊긴_잡을_이으면_그_사람_보고서에_붙이지_않고_내_것이면_붙인다(eng, monkeypatch):
    _caps(monkeypatch, 2)
    job = _interrupted(args_opts={**_ORIG, "append_to_report_id": 41}, report_id=77)

    async def go(ctx):
        await m.deliberate_continue(previous_job_id=job["id"], ctx=ctx)
        await _tick()
    _play(lambda: go(_ctx(YOU, "CAEG")))          # 같은 소속의 남 — 볼 수 있고 이어 돌 수도 있다
    assert "append_to_report_id" not in eng.seen["힌지 크랙 원인"].opts, "남의 보고서에 붙였다"
    eng.seen.clear()
    mine = _interrupted(args_opts=_ORIG, report_id=77, job_id="default-20261022-010000-cut002")
    _play(lambda: go2(mine))
    assert eng.seen["힌지 크랙 원인"].opts["append_to_report_id"] == 77, "보고서 단계에서 끊긴 내 잡의 보고서에 잇지 않았다"


async def go2(job):
    await m.deliberate_continue(previous_job_id=job["id"], ctx=_ctx(ME, "CAEG"))
    await _tick()


def test_정상_종료로_접힌_도는_잡은_cancelled_가_아니라_interrupted_다(eng, monkeypatch):
    """사람이 접은 것(cancelled)과 서버가 내려가며 접힌 것을 가른다 — 내려갈 때 루프가 남은 태스크를 취소한다."""
    _caps(monkeypatch, 1)

    async def scenario():
        a = _start("가")
        await _tick()
        delib_jobs.closing(True)
        delib_jobs._TASKS[a["id"]].cancel()
        await _tick()
        return a
    try:
        a = _play(scenario)
    finally:
        delib_jobs.closing(False)
    rec = _on_disk(a)
    assert rec["status"] == "interrupted" and "deliberate_continue" in rec["error"], rec
    assert delib_jobs._args_path(a["id"]).exists(), "이어 돌 재료(시작 인자 파일)를 지웠다"


def _many_seats(n: int, chars: int, job_id: str, **opts) -> dict:
    """좌석 n 석이 1~2라운드를 마치고 3라운드에서 끊긴 잡 — 입장마다 chars 자(좌석마다 끝에 표지)."""
    seats = [f"좌석{i:02d}" for i in range(n)]
    turns = [{"round": r, "display_round": r, "persona": p, "say": p[:20],
              "say_full": (f"{p} {r}R 입장 " + "가" * chars)[:chars - 6] + f"<{p}끝>"} for r in (1, 2) for p in seats]
    job = {"id": job_id, "job": "default", "kind": "default", "label": "자유 심의", "question": "대형 패널 쟁점", "chair_template": None,
           "opts": {"rounds": 4}, "status": "interrupted", "stage": "r3", "round": 3, "total_rounds": 4,
           "seats": [{"key": f"k{i}", "role": "역할"} for i in range(n)], "seat_roles": {}, "turns": turns, "decision": None,
           "result_text": None, "report_id": None, "checkpoint": None, "error": delib_jobs.INTERRUPTED_MSG, "warnings": [],
           "evidence_omitted": [], "started_at": 1000.0, "updated_at": 2000.0, "finished_at": 2000.0, "user": ME, "owner_aff": "CAEG",
           "started_by": "human"}
    delib_jobs._persist(job)
    assert delib_jobs._write_args(job_id, {"job_kind": "default", "question": job["question"], "groups": [], "opts": dict(opts),
                                           "user_email": ME, "owner": "", "owner_aff": "CAEG"})
    return job


def test_좌석이_많아도_이어하기_요약에_모든_좌석이_실리고_엔진_상한_안이다(eng, monkeypatch):
    """심의와 리스크 심사 패널은 좌석 수가 다르다 — 입장 전문을 이어 붙이면 엔진이 앞 8,000자만 받아(20만 · 30만 창) 뒤쪽 좌석이 빠졌다."""
    _caps(monkeypatch, 2)
    cap = d._summary_max()
    assert cap == 8_000, "시험 전제 — 고정된 창(128K)에서 이어하기 요약 상한은 8,000자다"
    job = _many_seats(20, 2_000, "default-20261022-020000-big001")
    _resume(eng, job["id"])
    cs = eng.seen["대형 패널 쟁점"].opts["continue_summary"]
    assert len(cs) <= cap, f"요약 {len(cs):,}자가 엔진 상한 {cap:,}자를 넘었다 — 엔진이 뒤쪽 좌석부터 자른다"
    missing = [i for i in range(20) if f"• 좌석{i:02d} (2R): 좌석{i:02d} 2R 입장" not in cs]
    assert not missing, f"입장이 빠진 좌석 {missing}"
    assert cs.count("자 중 앞 ") == 20, "줄인 입장에 원래 길이 표식이 없다"


def test_짧은_입장은_그대로_두고_긴_입장만_줄인다(eng, monkeypatch):
    _caps(monkeypatch, 2)
    job = _many_seats(4, 600, "default-20261022-020000-big002")         # 4석 × 600자 — 상한 안
    _resume(eng, job["id"])
    cs = eng.seen["대형 패널 쟁점"].opts["continue_summary"]
    assert "자 중 앞 " not in cs and all(f"<좌석{i:02d}끝>" in cs for i in range(4)), "상한 안인데 입장을 줄였다"


def test_긴_앞_회차_요약과_함께여도_좌석_입장이_상한_안에_다_실린다(eng, monkeypatch):
    _caps(monkeypatch, 2)
    cap = d._summary_max()
    job = _many_seats(12, 1_500, "default-20261022-020000-big003", continue_summary="앞 회차 결정: " + "나" * 20_000)
    _resume(eng, job["id"])
    cs = eng.seen["대형 패널 쟁점"].opts["continue_summary"]
    assert len(cs) <= cap and cs.startswith("앞 회차 결정: ") and "[앞 회차 결정 요약 " in cs, cs[:200]
    assert cs.index("[서버 재기동으로 끊긴") > 0 and all(f"• 좌석{i:02d} (2R)" in cs for i in range(12)), "새 입장이 앞 요약에 밀려 빠졌다"
    assert cs.index("[서버 재기동으로 끊긴") <= cap // 3 + 80, "앞 회차 요약이 셋 중 하나를 넘게 차지했다"
