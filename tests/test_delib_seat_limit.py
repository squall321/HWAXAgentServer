# 도는 심의의 좌석 합 상한 — 심의마다 좌석 수가 달라(3석 ~ 20석) 잡 수만으로는 LLM 서버가 받는 수를 못 묶는다(HWAXPortal docs/delib-ops R4' · DO-D16)
#
# 한 심의는 단계마다 좌석 수만큼 LLM 호출을 한꺼번에 낸다 — 잡 수 한도 6 에 20석 여섯이면 120개, 3석 여섯이면 18개다. 좌석 합 상한
# (max_seats_running · 0 = 끔)을 걸면 좌석이 많은 심의는 건수 한도 안이어도 자리가 나기를 기다린다. 앉기 전은 어림이고 앉으면 고친다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_seat_limit.py -q
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_job_queue import ME, YOU, _caps, _end, _play, _st, _start, _tick, eng  # noqa: E402, F401
from test_delib_silent_drops import _pin_context  # noqa: E402, F401


def _seats(n):
    return {"personas": [{"key": f"k{i}", "role": f"역할 {i}"} for i in range(n)]}


def _cap(monkeypatch, seats, total=6, **kw):
    _caps(monkeypatch, total, **kw)
    monkeypatch.setattr(delib_jobs, "MAX_SEATS_RUNNING", seats)


def test_어림은_지정_좌석이면_그_수_더하기_하나_자동_발굴이면_기본_석수다():
    assert delib_jobs._seats_est(_seats(6)) == 7
    assert delib_jobs._seats_est({}) == d.N_PERSONAS + d._COUNTER_SEATS + 1
    assert delib_jobs._seats_est(_seats(40)) == d.MAX_REQ_SEATS + 1, "엔진이 받지 않는 좌석까지 세었다"


def test_꺼져_있으면_종전대로_잡_수만_센다(eng, monkeypatch):
    _cap(monkeypatch, 0, total=3)

    async def scenario():
        jobs = [_start(q, delib_opts=_seats(20)) for q in ("가", "나", "다")]
        await _tick()
        assert [_st(j) for j in jobs] == ["running"] * 3
    _play(scenario)


def test_좌석_합을_넘는_심의는_줄을_서고_자리가_나면_든다(eng, monkeypatch):
    _cap(monkeypatch, 10)

    async def scenario():
        a = _start("가", ME, delib_opts=_seats(6))        # 어림 7
        b = _start("나", YOU, delib_opts=_seats(6))       # 7 + 7 > 10
        await _tick()
        assert (_st(a), _st(b)) == ("running", "queued")
        why = delib_jobs.summary(b)["queue"]["why"]
        assert "좌석 합 상한 10석" in why and "7석(어림)" in why, why
        await _end(eng, a)
        assert _st(b) == "running"
    _play(scenario)


def test_혼자면_상한보다_커도_돈다(eng, monkeypatch):
    _cap(monkeypatch, 5)

    async def scenario():
        a = _start("가", delib_opts=_seats(20))
        await _tick()
        assert _st(a) == "running", "도는 잡이 없는데 상한보다 크다고 영영 못 뜬다"
    _play(scenario)


def test_먼저_선_큰_심의를_뒤의_작은_심의가_가로채지_않는다(eng, monkeypatch):
    _cap(monkeypatch, 12)

    async def scenario():
        a = _start("가", ME, delib_opts=_seats(6))        # 7
        b = _start("나", YOU, delib_opts=_seats(6))       # 7 + 7 = 14 > 12 — 줄
        c = _start("다", "c@example.com", delib_opts=_seats(2))     # 3 — 들어갈 자리(7 + 3)는 있지만 먼저 선 b 의 좌석이다
        await _tick()
        assert [_st(x) for x in (a, b, c)] == ["running", "queued", "queued"], "작은 심의가 먼저 선 큰 심의를 앞질렀다(큰 심의가 굶는다)"
        await _end(eng, a)
        assert [_st(x) for x in (b, c)] == ["running", "running"], "자리가 났는데 차례대로 들지 않았다(7 + 3 ≤ 12)"
    _play(scenario)


def test_좌석이_앉으면_어림을_고쳐_그_자리에서_줄을_다시_본다(monkeypatch, tmp_path):
    """자동 발굴 잡은 어림(기본 8석)으로 줄을 서고, 엔진이 좌석을 앉히면(personas 사건) 실제 수로 센다."""
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    for name in ("_JOBS", "_TASKS", "_PENDING"):
        monkeypatch.setattr(delib_jobs, name, {})
    monkeypatch.setattr(delib_jobs, "_CLOSING", False)
    monkeypatch.setattr(m, "_APP", object())
    _cap(monkeypatch, 12)
    gates: dict = {}

    async def entry(_app, q, groups, opts, user_email, user_pat, _x):
        yield d._sse("delib", {"kind": "personas", "totalRounds": 3,
                               "personas": [{"key": f"s{i}", "role": "", "origin": "primary"} for i in range(3)]})
        await gates.setdefault(q, asyncio.Event()).wait()
        yield d._sse("done", {})
    monkeypatch.setattr(d, "run_deliberation", entry)
    est = delib_jobs._seats_est({})
    assert est * 2 > 12 and 3 + est <= 12, "시험 전제 — 어림 둘은 상한을 넘고, 앉은 3석과 어림 하나는 상한 안이다"

    async def scenario():
        a = _start("가")
        b = _start("나")
        assert (_st(a), _st(b)) == ("running", "queued")
        await _tick()          # a 의 personas 사건 — 3석으로 고친다
        assert len(a["seats"]) == 3 and _st(b) == "running", "앉은 좌석이 어림보다 적은데 줄을 다시 보지 않았다"
        for g in list(gates.values()):
            g.set()
        await _tick()
    _play(scenario)


def test_외부_응답자_잡은_좌석을_세지_않는다(monkeypatch):
    monkeypatch.setattr(delib_jobs, "_JOBS", {
        "a": {"id": "a", "status": "running", "seats": [{}] * 5, "opts": {}},
        "b": {"id": "b", "status": "running", "seats": [{}] * 9, "opts": {"llm": "external"}},
        "c": {"id": "c", "status": "queued", "seats_est": 8, "opts": {}},
        "d": {"id": "d", "status": "running", "seats_est": 4, "opts": {}}})
    assert delib_jobs.seats_running() == 9, "외부 응답자 잡 · 줄 선 잡을 세었거나 앉기 전 어림을 빼먹었다"


def test_줄이_꺼진_박스에서는_좌석_합에_걸리면_덮개를_말하며_거절한다(eng, monkeypatch):
    _cap(monkeypatch, 0, queue=0)
    delib_jobs.set_limits({"max_seats_running": 10}, by="운영")

    async def scenario():
        _start("가", delib_opts=_seats(6))
        await _tick()
        with pytest.raises(RuntimeError) as e:
            _start("나", delib_opts=_seats(6))
        assert "좌석 합 10석(도는 7석 + 이 심의 7석 어림) — 박스 덮개 POST /limits" in str(e.value), str(e.value)
    _play(scenario)


def test_limits_와_health_와_deliberate_jobs_가_좌석_합을_보인다(eng, monkeypatch):
    _cap(monkeypatch, 0)
    c = TestClient(app.app)
    assert c.post("/limits", json={"max_seats_running": -1, "by": "운영"}).status_code == 422
    r = c.post("/limits", json={"max_seats_running": 40, "by": "운영"})
    assert r.status_code == 200 and r.json()["max_seats_running"] == 40 and r.json()["source"]["max_seats_running"] == "file"
    h = c.get("/health").json()
    assert h["limits"]["max_seats_running"] == 40 and h["delib_seats_running"] == 0
    out = asyncio.run(m.deliberate_jobs())
    assert (out["running_seats"], out["running_seats_max"]) == (0, 40)
    assert c.post("/limits", json={"max_seats_running": None, "by": "운영"}).json()["max_seats_running"] == 0
