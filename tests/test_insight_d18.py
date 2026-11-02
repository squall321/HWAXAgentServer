# 과제 인사이트 도구(insight_*)와 리스크 앱의 새 읽기 도구가 리스크 심사 좌석에 닿지 않는다 — 지정 도구 · 자유 조회 · CALL 줄(D-18, 과제 인사이트 그래프 M2)
#
# 사용자 결정 11-02 — D-18 은 범위 그대로이고 새 길을 열지 않는다(HWAXPortal docs/project-insight-graph PLAN §7.2 · contracts §8). 게이트웨이의
# insight_find · insight_get 은 같은 과제의 앞선 심의 결론을 내준다. 리스크 앱의 risk_find_projects_by_subject 는 과제의 대상 · 등록부 상위를 낸다.
#   · 지정 도구(delib_opts.tools) — 리스크 심사는 부르지 않고 까닭(D-18)을 남긴다. 다른 심의는 종전대로 부른다.
#   · 자유 조회 후보 — 리스크 심사는 insight_* 를 접두 허용과 상관없이 뺀다(지금은 접두 허용에 걸리지 않지만 이름이 바뀌어도 닫힌다).
#   · CALL 줄(외부 응답자) — 좌석 목록에 없는 도구는 실행하지 않는다. 리스크 심사 좌석의 목록에는 insight_* 가 없다.
#
#   실행:  .venv/bin/python -m pytest tests/test_insight_d18.py -q
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import deliberation as d  # noqa: E402

from test_risk_registry_closed import _designate, _refused, _seat_given  # noqa: E402
from test_text_react_agent import _Script, _call  # noqa: E402

_INSIGHT = ("insight_find", "insight_get")
_RISK_NEW = ("risk_find_projects_by_subject", "risk_set_project_subject")


def test_인사이트_도구는_리스크_심사_지정_도구_갈래가_insight_다():
    for n in _INSIGHT:
        assert d._risk_designated_refusal(n) == "insight", n
    assert "insight" in d._RISK_DESIGNATED_REFUSE
    # 다른 게이트웨이 자체 도구는 이 갈래로 닫지 않는다(지금 동작 그대로)
    assert d._risk_designated_refusal("search_tools") == ""


def test_리스크_심사는_인사이트_도구를_지정해도_부르지_않고_다른_심의는_부른다(monkeypatch):
    runs, tools = _designate(monkeypatch, "risk-review", _INSIGHT)
    assert not [n for n in _INSIGHT if tools[n].calls]
    _refused(runs, _INSIGHT)
    _runs, tools = _designate(monkeypatch, "default", _INSIGHT)
    assert all(tools[n].calls for n in _INSIGHT), {n: tools[n].calls for n in _INSIGHT}


@pytest.mark.parametrize("amap", ({n: "heax-hwax_risk" for n in _RISK_NEW}, {}), ids=("mapped", "map-unavailable"))
def test_리스크_앱의_새_도구는_risk_이름이라_리스크_심사가_부르지_않는다(monkeypatch, amap):
    """리스크 앱 M2b 의 읽기 · 쓰기 도구는 이름이 risk_ 로 시작한다 — 지정 도구 갈래 app(변경 · 스냅샷 밖의 리스크 앱 도구)으로 닫힌다.
    매핑을 못 받아도 닫는다. 겹침 접두 꼴(heaxhwax_risk_<도구>)도 같다."""
    for n in _RISK_NEW:
        assert n.startswith("risk_") and n not in d._RISK_READ_TOOLS["heax-hwax_risk"], n
        assert d._risk_designated_refusal(f"heaxhwax_risk_{n}") == "app", n
    runs, tools = _designate(monkeypatch, "risk-review", _RISK_NEW, amap)
    assert not [n for n in _RISK_NEW if tools[n].calls]
    _refused(runs, _RISK_NEW)


def test_인사이트_도구와_리스크_앱의_새_도구는_자유_조회_접두에_걸리지_않는다():
    for n in _INSIGHT + _RISK_NEW:
        assert not d._free_tool_ok(n), f"{n} 이 접두 허용에 걸린다 — 자유 조회 후보에 들어간다"
        assert n not in d._RISK_KEEP_TOOLS and all(n not in v for v in d._RISK_READ_TOOLS.values()), n


def test_리스크_심사_좌석의_자유_조회_후보에는_접두가_열려도_인사이트_도구가_없다(monkeypatch):
    """이름이 바뀌어 접두 허용에 걸리게 돼도(여기서는 접두를 열어 흉내 낸다) 리스크 심사 좌석은 받지 않는다. 다른 심의는 받는다 — 거름이
    리스크 심사에만 걸린다는 것을 함께 본다."""
    monkeypatch.setattr(d, "_FREE_ALLOW", d._FREE_ALLOW + (d._INSIGHT_TOOL_HEAD,))
    extra = {n: "_gateway" for n in _INSIGHT}
    risk = _seat_given(monkeypatch, "risk-review", "[리스크심사 P1 T-1] 판정하라.", {"reports": []}, apps=(), extra=extra)
    assert not [n for n in risk if n.startswith(d._INSIGHT_TOOL_HEAD)], sorted(risk)
    other = _seat_given(monkeypatch, "default", "힌지 크랙의 원인", {"reports": []}, apps=(), extra=extra)
    assert all(n in other for n in _INSIGHT), sorted(other)


def test_외부_응답자의_CALL_줄도_리스크_심사_좌석에_없는_인사이트_도구를_실행하지_않는다(monkeypatch):
    """CALL 줄은 좌석 목록 안의 도구만 실행한다 — 리스크 심사 좌석의 목록(위 후보)에는 insight_* 가 없으므로 이름을 적어도 돌지 않는다."""
    monkeypatch.setattr(d, "_FREE_ALLOW", d._FREE_ALLOW + (d._INSIGHT_TOOL_HEAD,))
    risk = _seat_given(monkeypatch, "risk-review", "[리스크심사 P1 T-1] 판정하라.", {"reports": []}, apps=(),
                       extra={n: "_gateway" for n in _INSIGHT})
    llm = _Script(_call("insight_find", sp_task="알파"), "조회 요약: 없음")
    agent = d._TextReactAgent(llm, list(risk.values()), 2)
    assert "insight_find" not in agent.tools and "search_reports" in agent.tools
    _k, calls, _block, err = asyncio.run(d._free_gather_one(agent, {"key": "mech-a", "role": "기구"}, "질문", "맥락", 2))
    assert err == "" and calls == [], calls
    assert "목록에 없는 도구다(insight_find)" in llm.seen[1][1], llm.seen[1][1]
