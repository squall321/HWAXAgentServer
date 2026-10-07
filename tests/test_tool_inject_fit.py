# 지정 도구 결과를 좌석에 실을 때 합계 상한에서 뒤쪽 도구가 통째로 빠지지 않는지 — 좌석이 받은 프롬프트로 본다
#
# 지정 도구(delib_opts.tools) 결과는 도구마다 2,000자까지 모은 뒤 **이어 붙이고 5,000자에서 잘랐다.** 도구가
# 셋만 길어도 셋째는 토막 나고, 넷째부터는 좌석에 아예 안 갔다. 그런데 도구마다 '심의에 포함' 카드가 뜨고
# 상태줄은 '근거 확보 — 6건', 의장의 근거 프로파일은 '도구 조회 6건' 이었다. 목록·검색 도구를 먼저 돌리므로
# 잘려 나가는 쪽은 늘 수치가 든 상세 도구다.
#
# **끝까지 돌려서 좌석 프롬프트를 본다.** 카드와 상태줄만 보면 종전에도 전부 실린 것처럼 보였다.
#
#   실행:  .venv/bin/python -m pytest tests/test_tool_inject_fit.py -q
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deliberation as d  # noqa: E402

from test_delib_silent_drops import _cards, _mcp_view, _pin_context, _steps, _stream, _Tool  # noqa: E402, F401

_CARD = "지정 도구 근거 상한 초과"
_HEAD = "[사용자 지정 도구 정량 결과"


def _run(monkeypatch, sizes):
    """크기가 sizes 인 결과를 내는 지정 도구들로 2라운드 문턱까지 돌린다.
    반환 (도구 이름들, 이벤트, 1라운드 좌석이 받은 지정 도구 블록)."""
    names = [f"get_probe_{i}" for i in range(1, len(sizes) + 1)]
    tools = {nm: _Tool(nm, answer=json.dumps({"head": f"MARK-{i}-HEAD", "body": "x" * sz}))
             for i, (nm, sz) in enumerate(zip(names, sizes), start=1)}
    tools["agent_search"] = _Tool("agent_search")
    seat_prompts = []

    async def _llm(_obj, system, human):
        if "도구 호출 계획자" in system:
            return '{"q": "힌지"}'
        if re.search(r"당신은 '[^']+' 전문가", system):
            seat_prompts.append(human)
        return json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["a", "b"],
                           "position_short": "입장"}, ensure_ascii=False)

    monkeypatch.setattr(d, "_llm_text", _llm)
    events = _stream(monkeypatch, {"tools": names, "rounds": 2, "persona_knowledge": 0}, tools=tools,
                     until=lambda ev, data: ev == "delib" and data.get("kind") == "stage"
                     and str(data.get("stage")) == "r2")
    assert all(len(t.calls) == 1 for nm, t in tools.items() if nm != "agent_search"), "시험 전제 — 도구가 전부 불렸다"
    seat = next(h for h in seat_prompts if _HEAD in h)
    return names, events, seat[seat.index(_HEAD):].split("\n당신의 관점")[0]


def test_도구_여섯이_다_길어도_전부_좌석에_닿는다(monkeypatch):
    names, events, block = _run(monkeypatch, [2500] * 6)
    reached = [i for i in range(1, 7) if f"MARK-{i}-HEAD" in block]
    assert reached == [1, 2, 3, 4, 5, 6], f"뒤쪽 도구가 좌석에 안 갔다 — 닿은 것 {reached}"
    assert all(f"### {nm} ←" in block for nm in names)
    assert len(block) <= d._TOOL_INJECT_MAX + 100, f"합계 상한을 넘겨 실었다 — {len(block):,}자"
    assert block.count(" …[") == 6, "줄인 도구마다 표식이 있어야 한다 — 없으면 좌석은 그것이 전부인 줄 안다"
    # 화면이 말하는 것과 좌석이 받은 것이 같다.
    assert len([c for c in _cards(events, included=True) if c["source"].startswith("지정 도구 ")]) == 6
    assert any(s.startswith("지정 도구 근거 확보 — 6건") for s in _steps(events))
    note = [c for c in _cards(events, included=False) if c["source"] == _CARD]
    assert len(note) == 1, [c["source"] for c in _cards(events, included=False)]
    for want in ("6건", f"{d._TOOL_INJECT_MAX:,}자", "get_probe_1", "get_probe_6"):
        assert want in note[0]["text"], (want, note[0]["text"])
    for name, view in _mcp_view(monkeypatch, events).items():
        assert _CARD in [x.get("source") for x in view["evidence_omitted"]], (name, view["evidence_omitted"])


def test_합계_상한_안이면_종전_그대로다(monkeypatch):
    _names, events, block = _run(monkeypatch, [2500, 2500])
    assert block.count(" …[") == 2 and f"자 중 {d._TOOL_CHUNK_MAX:,}자]" in block, block[-200:]
    assert block.count("x") == 2 * (d._TOOL_CHUNK_MAX - len('{"head": "MARK-1-HEAD", "body": "')), "도구당 상한이 달라졌다"
    assert not [c for c in _cards(events, included=False) if c["source"] == _CARD]


def test_짧은_결과는_그대로_싣고_남는_몫을_긴_결과에_돌린다(monkeypatch):
    _names, events, block = _run(monkeypatch, [100, 100, 2500, 2500, 2500])
    assert all(f"MARK-{i}-HEAD" in block for i in range(1, 6))
    assert block.count(" …[") == 3, "짧은 결과에 줄였다는 표식을 붙였다"
    note = next(c for c in _cards(events, included=False) if c["source"] == _CARD)
    assert "get_probe_1" not in note["text"] and "get_probe_2" not in note["text"], note["text"]
    assert all(f"get_probe_{i}" in note["text"] for i in (3, 4, 5)), note["text"]
    assert len(block) >= d._TOOL_INJECT_MAX * 0.9, f"상한이 남는데 덜 실었다 — {len(block):,}자"
