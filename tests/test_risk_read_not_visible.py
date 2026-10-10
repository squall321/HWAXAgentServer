# 리스크 심사 좌석이 리스크 앱 읽기 도구(변경 전체·스냅샷)에서 not_visible 을 받으면 경고와 카드로 알린다 — risk-entry SEC-3
#
# 리스크 앱은 범위 밖 id 를 `{"error": "not_visible", …}` 정상 JSON 으로 돌려준다(isError 가 아니다 — HWAXRisk
# mcp_server._guarded). 그래서 게이트웨이 감사에는 성공으로 남고, 엔진은 좌석마다 ok:false 상태줄 한 줄만 냈다. 엔진이
# 질문의 diff_id= 를 보고 risk_get_diff 를 맨 앞에 세워 안내하는데(P-06), 좌석이 이 앱에 보이는 신원(잡 자격 → 게이트웨이
# 위임)이 그 과제를 못 보면 고정한 첫 호출이 버려진 채 패널이 done 으로 끝났다. 카드는 included=False 라 리스크 앱
# 러너가 패널에 옮겨 적는다(runner.withheld_by_engine).
#
#   실행:  .venv/bin/python -m pytest tests/test_risk_read_not_visible.py -q
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import deliberation as d  # noqa: E402

from test_delib_silent_drops import _cards, _pin_context, _stream, _Tool  # noqa: E402, F401
from test_window_budget import _Msg, _Recorder  # noqa: E402

_NV = '{"error": "not_visible", "message": "볼 수 없는 id 입니다 — d-1."}'      # HWAXRisk mcp_server._guarded 의 모양


def _seat(_key):
    return {"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["A", "B"], "position_short": "요약",
            "concede": [], "rebut": ["반박"], "deepen": "심화", "final_position": "최종", "non_negotiable": "",
            "vote": "진행", "stance": "동의"}


def _run(monkeypatch, chair, answer, tool="risk_get_diff"):
    import langgraph.prebuilt

    async def _llm(_obj, system, _human):
        return json.dumps(_seat(""), ensure_ascii=False) if "전문가입니다" in system else "결정문"

    msgs = [_Msg("ai", "", tool_calls=[{"id": "c0", "args": {"diff_id": "d-1", "part": "lines"}}]),
            _Msg("tool", answer, tool_call_id="c0", name=tool),
            _Msg("ai", "조회 요약: 끝")]
    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda _llm, _tools, **_k: _Recorder(msgs))
    seats = [{"key": "mech-a", "role": "역할"}, {"key": "rel-b", "role": "역할"}]
    return _stream(monkeypatch, {"personas": seats, "rounds": 3, "save_report": 0, "rebut_quote": 0, "free_tools": 1,
                                 "persona_knowledge": 0, "chair_template": chair},
                   tools={"agent_search": _Tool("agent_search"), tool: _Tool(tool)},
                   until=lambda ev, _data: ev == "done")


def _warn(events):
    return [x for ev, x in events if ev == "warning" and x.get("code") == "risk_read_not_visible"]


def _card(events):
    return [c for c in _cards(events, included=False) if c["source"] == "변경 원장 조회 거절"]


@pytest.mark.parametrize("tool", ("risk_get_diff", "risk_get_snapshot"))
def test_리스크_심사_좌석이_변경_원장에서_not_visible_을_받으면_경고와_카드를_한_번_낸다(monkeypatch, tool):
    events = _run(monkeypatch, "risk-review", _NV, tool)
    assert len(_warn(events)) == 1, _warn(events)
    cards = _card(events)
    assert len(cards) == 1, cards        # 라운드마다 되풀이하지 않는다(원장의 카드 자리는 30건이다)
    assert "mech-a" in cards[0]["text"] and "rel-b" in cards[0]["text"] and tool in cards[0]["text"], cards[0]
    assert "not_visible" in cards[0]["text"] and "DELIB_" not in cards[0]["text"]


def test_열리면_알리지_않는다(monkeypatch):
    events = _run(monkeypatch, "risk-review", '{"diff_id": "d-1", "total": 3, "lines": ["+ hinge"]}')
    assert not _warn(events) and not _card(events)


def test_리스크_심사가_아니면_종전대로다(monkeypatch):
    events = _run(monkeypatch, "diagnosis", _NV)
    assert not _warn(events) and not _card(events)
