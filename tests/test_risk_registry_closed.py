# 리스크 심사 좌석에게 등록부 도구(앞 패널 판정)가 어떤 길로도 닿지 않는다 — 사용자 결정 D-18 의 지정 도구 길
#
# 자유 조회 후보는 tests/test_delib_seats.py 의 등록부 가드가 본다. 이 파일은 호출자가 고르는 지정 도구(delib_opts.tools)
# 길이다 — 엔진이 그 도구를 주제 기반 인자로 불러 결과를 **전 좌석 프롬프트에** 싣는다. 리스크 심사에 risk_get_registry 를
# 지정하면 좌석이 앞 패널 판정을 읽고 같은 말을 해, 원장(D-23)의 지지 수가 독립 근거 없이 오른다.
#
#   실행:  .venv/bin/python -m pytest tests/test_risk_registry_closed.py -q
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import deliberation as d  # noqa: E402

from test_delib_silent_drops import _cards, _pin_context, _steps, _stream, _Tool  # noqa: E402, F401

_REG = '{"target_key": "T-1", "claims": [{"ref": "hinge", "verdict": "FAIL", "panel": 1}]}'


def _after_tools(ev, data):
    """지정 도구 단계가 끝난 뒤(좌석 발굴 문턱)에서 멈춘다 — 거기까지는 좌석 LLM 이 필요 없다."""
    return ev == "delib" and data.get("kind") == "stage" and data.get("stage") == "discover"


def _run(monkeypatch, chair):
    planned = []

    async def _planner(_llm, _system, human):
        planned.append(human)
        return '{"target_key": "T-1"}'

    monkeypatch.setattr(d, "_llm_text", _planner)
    tools = {n: _Tool(n, _REG) for n in d._RISK_REGISTRY_TOOLS}
    tools["agent_search"] = _Tool("agent_search")
    events = _stream(monkeypatch, {"chair_template": chair, "tools": list(d._RISK_REGISTRY_TOOLS)},
                     tools=tools, until=_after_tools)
    return events, tools, planned


def test_리스크_심사는_지정한_등록부_도구를_부르지_않고_까닭을_남긴다(monkeypatch):
    events, tools, planned = _run(monkeypatch, "risk-review")
    assert all(not tools[n].calls for n in d._RISK_REGISTRY_TOOLS), {n: tools[n].calls for n in tools}
    assert not planned, "거절할 도구의 인자를 구성했다"
    assert not [c for c in _cards(events, included=True) if "risk_" in c.get("source", "")], "등록부 결과가 좌석에 갔다"
    card = next(c for c in _cards(events, included=False) if c["source"] == "지정 도구 미실행")
    assert "risk_get_registry" in card["text"] and "risk_claims_for_ref" in card["text"] and "D-18" in card["text"], card
    assert any("지정 도구 거절: risk_get_registry" in s for s in _steps(events)), _steps(events)


@pytest.mark.parametrize("chair", ("default", "diagnosis"))
def test_리스크_심사가_아니면_지정한_등록부_도구를_그대로_부른다(monkeypatch, chair):
    """D-18 은 리스크 심사 좌석의 결정이다 — 다른 심의에서 호출자가 직접 고른 도구는 종전대로 돈다."""
    events, tools, _planned = _run(monkeypatch, chair)
    assert all(tools[n].calls for n in d._RISK_REGISTRY_TOOLS), {n: tools[n].calls for n in tools}
    assert not [c for c in _cards(events, included=False) if c["source"] == "지정 도구 미실행"]
