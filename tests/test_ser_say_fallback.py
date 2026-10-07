# 구조화에 실패한 좌석의 원문(say)이 다음 라운드와 의장에게 표식 없이 800자로 가지 않는지
#
# 좌석이 JSON 을 못 내면 원문을 say 로 보존한다(최대 2,000자, '(구조화 실패 — 원문 강등…)' 머리말). 그런데 그
# say 를 다음 라운드·의장에게 줄 때 직렬화(_ser)가 **800자에서 표식 없이** 끊었다. 다른 값은 DELIB_SER_CLIP 에서
# '…' 를 달고 끊기고 끊었다는 알림이 나가는데, say 만 다른 길이었다 — 끊은 판과 온전한 판이 같아 보여 알림도
# 안 나갔고, 의장은 창이 남아도 그 좌석만 800자를 받았다.
#
# **스트림을 실제로 돌려서** 다음 라운드 좌석과 의장이 받은 프롬프트를 본다.
#
#   실행:  .venv/bin/python -m pytest tests/test_ser_say_fallback.py -q
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deliberation as d  # noqa: E402

from test_delib_silent_drops import _cards, _pin_context, _steps, _stream  # noqa: E402, F401

_SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}]
_PROSE = "".join(f"<{i:02d}>" + "가" * 96 for i in range(18))        # 1,800자 — 100자마다 꼬리표가 있다
_SER_CARD = "직렬화 값 상한 초과"


def _run(monkeypatch, ser_clip=None):
    """mech-a 가 1라운드에서 JSON 대신 산문을 낸다(재시도해도). (이벤트, 2라운드 rel-b 가 받은 프롬프트, 의장 프롬프트)"""
    seen = {"seat2": "", "chair": ""}

    async def _llm(_obj, system, human):
        who = re.search(r"당신은 '([^']+)' 전문가", system)
        if who and who.group(1) == "mech-a" and "[1라운드 전원]" not in human:
            return _PROSE                                   # 구조화 실패 — 원문이 say 로 보존된다
        if who:
            if who.group(1) == "rel-b" and "[1라운드 전원]" in human:
                seen["seat2"] = human
            return json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["가", "나"],
                               "position_short": "요약", "final_position": "최종", "non_negotiable": "",
                               "vote": "진행", "stance": "동의"}, ensure_ascii=False)
        if "엔지니어링 톤" in system:
            seen["chair"] = human
        return "결정문"

    monkeypatch.setattr(d, "_llm_text", _llm)
    if ser_clip is not None:
        monkeypatch.setattr(d, "_SER_CLIP", ser_clip)
    events = _stream(monkeypatch, {"personas": _SEATS, "rounds": 2, "save_report": 0, "persona_knowledge": 0},
                     until=lambda ev, _data: ev == "done")
    assert seen["seat2"] and seen["chair"], "시험 전제 — 2라운드 좌석과 의장이 불렸다"
    return events, seen["seat2"], seen["chair"]


def _say_of(prompt):
    """프롬프트의 라운드 전사에서 mech-a 줄에 실린 say."""
    line = next(ln for ln in prompt.splitlines() if ln.startswith("• mech-a: "))
    return json.loads(line[len("• mech-a: "):])["say"]


def _tags(text):
    return len(re.findall(r"<\d\d>", text))


def test_의장은_전사_상한이_받는_만큼_원문을_온전히_받는다(monkeypatch):
    """종전엔 창이 남아도 이 좌석만 800자였다(꼬리표 8개). 의장 쪽은 전사 상한(_decision_ctx)이 지킨다."""
    _events, _seat2, chair = _run(monkeypatch)
    say = _say_of(chair)
    assert say.startswith("(구조화 실패 — 원문 강등"), say[:40]
    assert _tags(say) == 18 and not say.endswith("…"), f"의장이 받은 원문이 잘렸다 — 꼬리표 {_tags(say)}/18"


def test_다음_라운드_좌석에게는_다른_값과_같은_상한에서_표식을_달고_끊는다(monkeypatch):
    events, seat2, _chair = _run(monkeypatch)
    say = _say_of(seat2)
    assert say.endswith("…") and len(say) == d._SER_CLIP + 1, f"{len(say)}자 — 표식 없이 끊었거나 상한이 다르다"
    # 끊었다는 알림이 나간다 — 종전엔 끊은 판과 온전한 판이 같아 보여 안 나갔다.
    assert any(s_.startswith("직렬화 값 상한") for s_ in _steps(events)), _steps(events)
    (card,) = [c for c in _cards(events, included=False) if c["source"] == _SER_CARD]
    assert "DELIB_SER_CLIP" in card["knob"], card


def test_상한을_풀면_좌석도_원문을_온전히_받고_알림이_없다(monkeypatch):
    events, seat2, chair = _run(monkeypatch, ser_clip=0)
    assert _tags(_say_of(seat2)) == 18 and _tags(_say_of(chair)) == 18
    assert not [c for c in _cards(events, included=False) if c["source"] == _SER_CARD]


def test_직렬화는_say_폴백에도_값_상한을_그대로_건다():
    o = {"persona": "mech-a", "say": "가" * 1500}
    for keys, primary in ((("lens", "recommendation"), "lens"), (("deepen",), "")):
        cut = json.loads(d._ser(o, keys, primary=primary, clip=700))["say"]
        assert cut == "가" * 700 + "…", f"{len(cut)}자"
        assert json.loads(d._ser(o, keys, primary=primary, clip=0))["say"] == "가" * 1500
    # 핵심 키는 비고 부수 키만 있을 때도 say 를 같이 싣는다(종전 커버리지 규칙 그대로) — 그 say 도 같은 상한이다.
    both = json.loads(d._ser({**o, "concerns": ["우려"]}, ("lens", "concerns"), primary="lens", clip=700))
    assert both["concerns"] == "우려" and both["say"].endswith("…") and len(both["say"]) == 701
