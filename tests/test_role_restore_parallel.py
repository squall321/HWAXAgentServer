# 지정 좌석의 역할 원문 복원 — 한꺼번에 돌리고 좌석마다 줄을 내는지, 못 받은 좌석을 말하는지
#
# 지정 좌석은 좌석마다 도구 호출 하나로 역할 원문을 받아 온다. 좌석 수만큼 **직렬**로 불렀고 줄은 다 끝난 뒤에
# 한 번 나왔다 — 22석이면 22건이 조용히 돌았다. 게이트웨이 호출 한도가 600초로 늘면 느린 날의 최악이 좌석 수만큼
# 곱해진다(3.7시간). 못 받은 좌석은 호출자가 준 역할로 조용히 앉았다.
#
# **스트림을 실제로 돌려서** 본다 — 답이 늦은 역할 도구를 물리고 몇 건이 겹쳐 도는지 센다.
#
#   실행:  .venv/bin/python -m pytest tests/test_role_restore_parallel.py -q
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as a  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402

from test_delib_silent_drops import _pin_context, _steps, _stream, _Tool  # noqa: E402, F401

_N = 12
_SEATS = [{"key": f"dom{i:02d}-seat", "role": f"호출자가 준 역할 {i}"} for i in range(1, _N + 1)]
_DELAY = 0.05


class _Roles(_Tool):
    """역할 원문 도구 — 건마다 _DELAY 만큼 걸리고, 겹쳐 도는 수의 최대값을 적는다. broken 의 좌석은 호출이 실패한다."""

    def __init__(self, broken=()):
        super().__init__("get_agent_session")
        self.now = self.peak = 0
        self.broken = set(broken)

    async def ainvoke(self, args):
        self.calls.append(args)
        self.now += 1
        self.peak = max(self.peak, self.now)
        try:
            await asyncio.sleep(_DELAY)
        finally:
            self.now -= 1
        key = args["agent_type"]
        if key in self.broken:
            return (a._TOOL_FAIL_MARK + " 도구 get_agent_session 호출 실패: 900초 안에 답하지 않았다"
                    "(MCP_CALL_TIMEOUT_S) — 게이트웨이 무응답이다.")
        return json.dumps({"data": {"description": f"{key} 의 역할 원문"}}, ensure_ascii=False)


def _run(monkeypatch, roles, conc=6, **req):
    monkeypatch.setattr(d, "_KN_CONC", conc)
    t0 = time.monotonic()
    events = _stream(monkeypatch, {"personas": _SEATS, "persona_knowledge": 0, **req},
                     tools={"get_agent_session": roles})
    return events, time.monotonic() - t0


def _lines(events):
    return [s_ for s_ in _steps(events) if s_.startswith("역할 복원 ")]


def test_좌석_역할을_한꺼번에_받고_좌석마다_줄을_낸다(monkeypatch):
    roles = _Roles()
    events, took = _run(monkeypatch, roles)
    assert len(roles.calls) == _N
    assert 1 < roles.peak <= 6, f"한 번에 {roles.peak}건이 돌았다 — 직렬이거나 상한(6)을 넘겼다"
    assert took < _N * _DELAY, f"{took:.2f}초 — 직렬({_N * _DELAY:.2f}초)보다 빠르지 않다"
    lines = _lines(events)
    assert [ln.split(" — ")[0] for ln in lines] == [f"역할 복원 {i}/{_N}" for i in range(1, _N + 1)], lines
    assert {ln.split(" — ")[1] for ln in lines} == {p["key"] for p in _SEATS}
    head = next(s_ for s_ in _steps(events) if s_.startswith("지정 전문가 소집"))
    assert f"{_N}명" in head and "한 번에 6명" in head, head
    assert _steps(events).index(head) < _steps(events).index(lines[0]), "시작한다는 줄이 끝난 뒤에 나왔다"


def test_좌석_순서와_받은_역할은_그대로다(monkeypatch):
    """끝나는 순서대로 줄을 내더라도 좌석 순서는 호출자가 준 그대로다."""
    events, _took = _run(monkeypatch, _Roles())
    seated = next(data for ev, data in events if ev == "delib" and data.get("kind") == "personas")["personas"]
    assert [p["key"] for p in seated] == [p["key"] for p in _SEATS]
    assert all(p["role"] == f"{p['key']} 의 역할 원문" for p in seated), seated[:2]


def test_동시_실행_수는_지식카드_조회와_같은_설정을_따른다(monkeypatch):
    for conc, want in ((1, 1), (3, 3), (0, _N)):                 # 0 은 무제한이다
        roles = _Roles()
        _run(monkeypatch, roles, conc=conc)
        assert roles.peak == want, (conc, roles.peak)


def test_원문을_못_받은_좌석은_그렇다고_말하고_호출자가_준_역할로_앉는다(monkeypatch):
    events, _took = _run(monkeypatch, _Roles(broken={"dom03-seat"}))
    (bad,) = [ln for ln in _lines(events) if "dom03-seat" in ln]
    assert "원문을 받지 못해 호출자가 준 역할로 간다" in bad and "MCP_CALL_TIMEOUT_S" in bad, bad
    assert sum("받지 못해" in ln for ln in _lines(events)) == 1, "멀쩡히 받은 좌석까지 못 받았다고 적었다"
    seated = next(data for ev, data in events if ev == "delib" and data.get("kind") == "personas")["personas"]
    by = {p["key"]: p["role"] for p in seated}
    assert by["dom03-seat"] == "호출자가 준 역할 3" and by["dom04-seat"] == "dom04-seat 의 역할 원문", by


def test_리스크_심사_좌석_계약은_받은_역할_뒤에_붙는다(monkeypatch):
    """복원이 role 을 통째로 덮는다 — 한꺼번에 돌리면서 순서가 뒤집히면 계약이 사라진다."""
    monkeypatch.setattr(d, "_KN_CONC", 6)

    async def _fake_tools(*_a, **_k):
        return {"get_agent_session": _Roles(), "agent_search": _Tool("agent_search")}

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    seats = [{"key": "mech-housing", "role": "지어 준 역할"}, {"key": "rel-drop", "role": ""}]
    opts = d._resolve_opts({"personas": seats, "chair_template": "risk-review", "free_tools": 0, "voc": "off",
                            "rescreen": 0, "persona_knowledge": 0})
    out: dict = {}

    async def go():
        gen = d._deliberation_stream(SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None)),
                                     "힌지 크랙 원인", [], opts, out=out)
        try:
            async for chunk in gen:
                ev, data = delib_jobs._parse_sse(chunk)
                assert ev != "error", data
                if ev == "delib" and data.get("stage") == "r1":
                    break
        finally:
            await gen.aclose()

    asyncio.run(go())
    role = next(p["role"] for p in out["personas"] if p["key"] == "mech-housing")
    assert role.startswith("mech-housing 의 역할 원문\n") and d._RISK_SEAT_CONTRACT["mech"] in role, role[:120]
