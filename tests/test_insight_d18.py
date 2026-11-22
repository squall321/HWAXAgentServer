# 과제 인사이트 도구(insight_*)와 리스크 앱의 새 읽기 도구가 리스크 심사 좌석에 닿지 않는다 — 지정 도구 · 자유 조회 · CALL 줄(D-18, 과제 인사이트 그래프 M2)
#
# 사용자 결정 11-02 — D-18 은 범위 그대로이고 새 길을 열지 않는다(HWAXPortal docs/project-insight-graph PLAN §7.2 · contracts §8). 게이트웨이의
# insight_find · insight_get 은 같은 과제의 앞선 심의 결론을 내준다. 리스크 앱의 risk_find_projects_by_subject 는 과제의 대상 · 등록부 상위를 낸다.
#   · 지정 도구(delib_opts.tools) — 리스크 심사는 부르지 않고 까닭(D-18)을 남긴다. 다른 심의는 종전대로 부른다.
#   · 자유 조회 후보 — 리스크 심사는 insight_* 를 접두 허용과 상관없이 뺀다(지금은 접두 허용에 걸리지 않지만 이름이 바뀌어도 닫힌다).
#   · CALL 줄(외부 응답자) — 좌석 목록에 없는 도구는 실행하지 않는다. 리스크 심사 좌석의 목록에는 insight_* 가 없다.
#   · 호출자 근거(delib_opts.evidence) — 리스크 심사는 도구 칸이 과제 인사이트인 근거 항목을 빼고 알린다(M3 의 받침). 다른 심의는 그대로 싣는다.
#
#   실행:  .venv/bin/python -m pytest tests/test_insight_d18.py -q
import asyncio
import json
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


# ── 받침 — 호출자 근거(opts.evidence)로 온 과제 인사이트(M3 다시 쓰기 · HWAXPortal docs/project-insight-graph PLAN §7.1) ───────────────────
# 일반 심의는 앞선 인사이트를 근거 항목으로 받는다 — 웹은 포털이 깔고(tool=insight_get), MCP 는 호출자가 insight_find · insight_get 결과를 넘긴다.
# 리스크 심사에는 깔지 않는다 — 포털은 넣지 않고, 엔진은 의장이 risk-review 면 도구 칸이 과제 인사이트인 근거 항목을 뺀다(사람이 글만 베껴 넣는
# 것까지는 막지 못한다 — 이름이 있는 길만 닫는다).
from test_delib_silent_drops import _cards, _pin_context, _stream  # noqa: E402, F401

_INS_TEXT = "앞선 인사이트 — 리브를 1.2 mm 로 보강한다"
_EV = [{"source": "앞선 인사이트 — 검증 대상, 독립 근거 아님", "tool": "insight_get", "args": '{"id": "ins:web:c1:r1:default"}',
        "result": _INS_TEXT},
       {"source": "개인 Claude", "tool": "mcp__hwax__insight_find", "args": '{"sp_task": "알파"}', "result": _INS_TEXT + " (찾기)"},
       {"source": "StepForge", "tool": "find_part", "args": '{"q": "힌지"}', "result": "힌지 2종"},
       {"source": "메모", "result": "도구 없는 글"}]


@pytest.mark.parametrize("name, want", [
    ("insight_get", True), ("insight_find", True), (" Insight_Get ", True), ("mcp__hwax__insight_find", True),
    ("gateway.insight_get", True), ("hwax:insight_find", True), ("insight_future_tool", True),
    # 낱말 경계로 넓힘(반대 검토 11-22 M3) — 접두 · 띄어쓰기 · 괄호 · 머리의 BOM · 정보 분리 문자가 붙어도 과제 인사이트다
    ("hwax-insight_get", True), ("hwax insight_get", True), ("hwax_insight_get", True), ("(insight_get)", True),
    ("\ufeffinsight_get", True), ("\x1cinsight_get", True), ("INSIGHT_FIND", True),
    # 밑줄 뒤의 insight_ 도 닫힌 쪽으로 읽는다(다른 앱의 get_insight_… 가 생기면 리스크 심사에서만 빠진다)
    ("get_insight_summary", True), ("my_insight_tool", True),
    ("find_part", False), ("myinsight_get", False), ("insights_get", False), ("in\u017fight_get", False), ("\u0131nsight_get", False),
    ("invoke_tool", False), ("", False), (None, False), (7, False)])
def test_근거_항목의_도구_이름이_과제_인사이트인지_가린다(name, want):
    assert d._insight_evidence_tool(name) is want, name


@pytest.mark.parametrize("tool, args, want", [
    # 게이트웨이의 범용 실행기 — 가벼운 보기의 Claude 가 실제로 부르는 이름이다. 껍데기면 인자의 name 으로 가린다
    ("mcp__hwax__invoke_tool", {"name": "insight_get", "arguments": {"id": "ins:web:c1:r1:default"}}, True),
    ("invoke_tool", '{"name": "insight_find", "arguments": {"sp_task": "알파"}}', True),
    ("hwax.invoke_tool", "{'name': 'insight_get', 'arguments': {'id': 'ins:wf:z'}}", True),      # 사전을 글로 옮긴 꼴
    ("invoke_tool", {"arguments": {"name": "insight_x"}}, True),                                  # 어느 깊이의 name 이든(닫힌 쪽)
    ("invoke_tool", {"name": "find_part", "arguments": {"q": "insight_get"}}, False),
    ("invoke_tool", None, False), ("invoke_tool", "", False), ("invoke_tools", {"name": "insight_get"}, False),
    ("find_part", {"name": "insight_get"}, False),                                                # 껍데기가 아니면 인자를 보지 않는다
    ("insight_get", None, True)])
def test_범용_실행기로_부른_근거는_인자의_도구_이름으로_가린다(tool, args, want):
    assert d._insight_evidence_tool(tool, args) is want, (tool, args)


def test_리스크_심사는_범용_실행기로_부른_과제_인사이트_근거도_뺀다():
    ev = [{"source": "개인 Claude", "tool": "mcp__hwax__invoke_tool", "args": {"name": "insight_get", "arguments": {"id": "ins:wf:z"}},
           "result": _INS_TEXT}, {"source": "개인 Claude", "tool": "hwax_insight_get", "args": "{}", "result": _INS_TEXT},
          {"source": "StepForge", "tool": "mcp__hwax__invoke_tool", "args": {"name": "find_part", "arguments": {}}, "result": "힌지 2종"}]
    o = d._resolve_opts({"chair_template": "risk-review", "evidence": ev})
    assert [e["result"] for e in o.evidence] == ["힌지 2종"] and o.evidence_insight_dropped == 2
    assert len(d._resolve_opts({"evidence": ev}).evidence) == 3


def test_리스크_심사는_호출자_근거의_과제_인사이트_항목을_빼고_센다():
    o = d._resolve_opts({"chair_template": "risk-review", "evidence": [dict(e) for e in _EV]})
    assert [e["tool"] for e in o.evidence] == ["find_part", ""], "과제 인사이트가 아닌 근거는 그대로다"
    assert o.evidence_insight_dropped == 2
    assert _INS_TEXT not in json.dumps(o.evidence, ensure_ascii=False)


@pytest.mark.parametrize("chair", ["default", "diagnosis", "option-select", None])
def test_리스크_심사가_아니면_과제_인사이트_근거를_그대로_싣는다(chair):
    req = {"evidence": [dict(e) for e in _EV], **({"chair_template": chair} if chair else {})}
    o = d._resolve_opts(req)
    assert [e["tool"] for e in o.evidence] == ["insight_get", "mcp__hwax__insight_find", "find_part", ""]
    assert o.evidence_insight_dropped == 0


def test_근거가_없거나_과제_인사이트가_없으면_리스크_심사의_근거는_종전_그대로다():
    """받침이 다른 근거를 건드리지 않는다 — 과제 인사이트 항목이 없는 리스크 심사의 정규화 결과가 다른 의장과 글자까지 같다."""
    plain = [dict(e) for e in _EV[2:]]
    risk, other = d._resolve_opts({"chair_template": "risk-review", "evidence": plain}), d._resolve_opts({"evidence": plain})
    assert risk.evidence == other.evidence and risk.evidence_insight_dropped == 0
    assert d._resolve_opts({"chair_template": "risk-review"}).evidence_insight_dropped == 0


def test_리스크_심사_스트림은_과제_인사이트_근거를_좌석에_주지_않고_뺐다고_알린다(monkeypatch, _pin_context):
    events = _stream(monkeypatch, {"chair_template": "risk-review", "evidence": [dict(e) for e in _EV]})
    given = _cards(events, included=True)
    assert not [c for c in given if _INS_TEXT in json.dumps(c, ensure_ascii=False)], "과제 인사이트가 좌석 근거 카드에 실렸다"
    assert [c for c in given if "힌지 2종" in str(c.get("text"))], "다른 근거는 그대로 실린다"
    note = [c for c in _cards(events, included=False) if c.get("source") == "과제 인사이트 근거 거름"]
    assert len(note) == 1 and "2건" in note[0]["text"] and "D-18" in note[0]["text"], note
    assert _INS_TEXT not in note[0]["text"], "알림에 인사이트의 글을 싣지 않는다"


def test_일반_심의_스트림은_과제_인사이트_근거를_싣고_거름_알림이_없다(monkeypatch, _pin_context):
    events = _stream(monkeypatch, {"chair_template": "default", "evidence": [dict(e) for e in _EV]})
    assert len([c for c in _cards(events, included=True) if _INS_TEXT in str(c.get("text"))]) == 2
    assert not [c for c in _cards(events) if c.get("source") == "과제 인사이트 근거 거름"]
