# 좌석 지식카드 조회의 시간 한도(KNOWLEDGE_TIMEOUT_S) — 기본값 180초와, 걸렸을 때 어느 설정인지 남는지
#
# 120초는 게이트웨이 호출 한도와 같은 값이라 어느 쪽이 먼저 걸릴지 정해지지 않았고, AIDataHub 가 풀 자리를
# 60초까지 기다린 뒤 검색이 44초까지 가면 104초로 거의 닿았다. 20석 넘는 패널이 조회를 몰아 쏘면 멀쩡한 조회가
# 강등됐고, 강등 알림에는 '몇 초 초과' 만 있고 어느 값을 올려야 하는지는 없었다.
#
# **스트림을 실제로 돌려서** 본다 — 한도보다 늦게 답하는 조회 도구를 물린다.
#
#   실행:  .venv/bin/python -m pytest tests/test_knowledge_timeout_knob.py -q
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import deliberation as d  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _cards, _mcp_view, _pin_context, _stream, _Tool  # noqa: E402, F401

_KNOB = "KNOWLEDGE_TIMEOUT_S"


class _SlowHybrid(_Tool):
    """hybrid 로 물으면 한도보다 늦게 답하고, semantic 으로 되물으면 바로 답한다. slow_all 이면 둘 다 늦다."""

    def __init__(self, slow_all=False):
        super().__init__("agent_search", '{"hits": [{"title": "카드", "snippet": "발췌"}]}')
        self.slow_all = slow_all

    async def ainvoke(self, args):
        if self.slow_all or args.get("mode") == "hybrid":
            await asyncio.sleep(1.0)
        return await super().ainvoke(args)


def _kn_events(events):
    status = [data for ev, data in events if ev == "status" and data.get("step", "").startswith("지식카드 ")
              and "/" in data["step"].split("—")[0]]
    warning = [data for ev, data in events if ev == "warning" and data.get("code") == "knowledge_degraded"]
    card = [c for c in _cards(events, included=False) if c["source"] == "지식카드 조회 강등"]
    return status, warning, card


def test_기본값은_180초다():
    """AIDataHub 의 풀 대기 60초 + 검색 문장 한도 90초보다 커야 그쪽이 먼저 걸려 원인이 구체적으로 나온다.
    환경값이 아니라 **소스의 기본값**을 본다."""
    src = (ROOT / "deliberation.py").read_text(encoding="utf-8")
    m = re.search(r'^KNOWLEDGE_TIMEOUT_S = _env_float\("KNOWLEDGE_TIMEOUT_S", (\d+)\.0\)$', src, re.M)
    assert m and int(m.group(1)) == 180 and int(m.group(1)) > 60 + 90, m and m.group(0)


def test_한도에_걸려_되물은_좌석의_줄에_설정_이름이_실린다(monkeypatch):
    monkeypatch.setattr(d, "KNOWLEDGE_TIMEOUT_S", 0.05)
    events = _stream(monkeypatch, {}, tools={"agent_search": _SlowHybrid()})
    status, warning, card = _kn_events(events)
    assert len(status) == 2 and all(s_.get("knob") == _KNOB for s_ in status), status
    for s_ in status:
        assert "hybrid 검색 0초 초과 → semantic 로 되물음" in s_["step"], s_["step"]
        assert _KNOB not in s_["step"], "화면에 뜨는 글에 설정 이름을 넣었다"
    assert warning[0]["knob"] == card[0]["knob"] == _KNOB
    # 되물어 받은 지식은 실린다 — 강등이지 유실이 아니다.
    assert len([c for c in _cards(events, included=True) if c["source"].endswith("· 지식카드")]) == 2
    for name, view in _mcp_view(monkeypatch, events).items():
        row = next(x for x in view["evidence_omitted"] if x.get("source") == "지식카드 조회 강등")
        assert row["text"].endswith(f"(설정 {_KNOB})"), (name, row)
        assert any(w.endswith(f"(설정 {_KNOB})") for w in view["warnings"]), (name, view["warnings"])


def test_폴백까지_늦으면_지식_없이_가고_그때도_설정_이름이_남는다(monkeypatch):
    monkeypatch.setattr(d, "KNOWLEDGE_TIMEOUT_S", 0.05)
    events = _stream(monkeypatch, {}, tools={"agent_search": _SlowHybrid(slow_all=True)})
    status, warning, card = _kn_events(events)
    assert all("폴백도 실패" in s_["step"] and s_.get("knob") == _KNOB for s_ in status), status
    assert card[0]["knob"] == _KNOB and warning[0]["knob"] == _KNOB
    assert not [c for c in _cards(events, included=True) if c["source"].endswith("· 지식카드")]


def test_시간과_무관한_강등에는_설정_이름을_붙이지_않는다(monkeypatch):
    """조회가 오류로 끝난 것은 한도를 올려도 풀리지 않는다 — 엉뚱한 설정을 가리키지 않는다."""
    monkeypatch.setattr(d, "KNOWLEDGE_TIMEOUT_S", 5.0)
    broken = _Tool("agent_search", "(tool agent_search error: backend 500)")
    events = _stream(monkeypatch, {}, tools={"agent_search": broken})
    status, warning, card = _kn_events(events)
    assert status and all("knob" not in s_ for s_ in status), status
    assert "knob" not in warning[0] and "knob" not in card[0], (warning, card)


def test_제때_답하면_강등_알림이_없다(monkeypatch):
    monkeypatch.setattr(d, "KNOWLEDGE_TIMEOUT_S", 5.0)
    events = _stream(monkeypatch, {}, tools={"agent_search": _Tool("agent_search")})
    status, warning, card = _kn_events(events)
    assert status and not warning and not card and all("knob" not in s_ for s_ in status)
