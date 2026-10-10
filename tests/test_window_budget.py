# 좌석 프롬프트의 몫을 모델 창에서 유도한다 — 좁은 창(16K·128K·200K)은 종전 값 그대로이고 1백만 토큰 창에서만 는다
#
# 사용자 지침(2026-10-10) — 운영 모델은 GLM-5.2, 창 1,048,576 토큰이다. 16K·128K 시절에 박은 글자 상한(다음 라운드에
# 넘기는 값당 700자, 직전 라운드 48,000자, 좌석이 직접 조회한 결과 호출당 900자·합 3,500자, 지정 도구 결과 2,000/5,000자,
# 이어하기 요약 8,000자, 조회 턴의 역할 280자)이 1백만 토큰 창에서도 그대로 걸려 '글자가 잘려서 생긴 문제' 가 났다.
# 이 값들을 창에서 유도하되, 좁은 창은 종전 동작을 지킨다(바닥값) — dev 의 16K 모델에서 좌석이 넘치면 안 된다.
#
# 앞 절반은 **특성화**다 — 구현 전 코드에서 뜬 값이고, 좁은 창에서는 한 글자도 달라지면 안 된다.
#
#   실행:  .venv/bin/python -m pytest tests/test_window_budget.py -q
import asyncio
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import deliberation as d  # noqa: E402

_SMALL = (16384, 128000, 200000)
_BIG = 1048576          # GLM-5.2


@pytest.fixture(autouse=True)
def _restore_ctx():
    saved = dict(app._ctx_cache)
    yield
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)
    d._evid_cache.clear()
    d._free_tok_cache.clear()


def _win(tokens):
    app._ctx_cache["n"] = tokens
    d._evid_cache.clear()
    d._free_tok_cache.clear()


# ── 특성화 — 좁은 창은 종전 값 그대로 ───────────────────────────────────────────────
# (사전 컨텍스트, 근거, 챗 맥락, 공용 조회 1·2·3·8라운드, 의장 라운드당(고정부 100,000자·3라운드))
_OLD = {
    16384: (2000, 2000, 1000, [600, 600, 600, 600], 6000),
    128000: (25668, 17967, 7700, [3850, 3850, 3850, 3850], 6000),
    200000: (95976, 67183, 28792, [8000, 14396, 14396, 14396], 28892),
}


@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_예산은_종전_값_그대로다(tokens):
    _win(tokens)
    pre, evid, chat, share, dec = _OLD[tokens]
    assert d._pre_budget() == pre
    assert d._evid_budget() == evid
    assert d._chat_ctx_budget() == chat
    assert [d._share_budget(r) for r in (1, 2, 3, 8)] == share
    assert d._decision_ctx(100000, 3) == dec


def _tool_got():
    return [(f"[지정 도구 t{i}]\n", f"<t{i}>" + "가" * 6000 + f"</t{i}>") for i in range(3)]


@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_지정_도구_결과는_종전_상한_그대로다(tokens):
    """도구당 2,000자·합 5,000자 — 구현 전 코드에서 뜬 글의 해시다."""
    _win(tokens)
    text, share = d._fit_tool_results(_tool_got(), d._TOOL_INJECT_MAX)
    assert share == 1625, share
    assert hashlib.sha256(text.encode()).hexdigest() == (
        "6cc798592a805e91a33250ceaf6769cae119d96a8c38676907b4c677336ee8cb"), text[:200]


@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_이어하기_요약은_8000자다(tokens):
    _win(tokens)
    o = d._resolve_opts({"continue_summary": "가" * 9000})
    assert o.continue_summary == "가" * 8000
    assert any("8,000" in v for v in o.req_cut.values()), o.req_cut


@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_다음_라운드_값은_700자에서_끊긴다(tokens):
    _win(tokens)
    assert d._ser_val("가" * 1000) == "가" * 700 + "…"


class _Recorder:
    """_free_gather_one 이 부르는 에이전트 자리 — 받은 메시지를 적고, 정해 둔 메시지를 흘린다."""

    def __init__(self, msgs=()):
        self.seen, self._msgs = [], list(msgs)

    async def astream(self, inp, config=None, stream_mode=None):
        self.seen.append(inp["messages"])
        yield {"messages": self._msgs}


class _Msg:
    def __init__(self, type_, content, tool_calls=None, tool_call_id=None, name=None):
        self.type, self.content, self.name = type_, content, name
        self.tool_calls, self.tool_call_id = tool_calls or [], tool_call_id


_ROLE = "".join(f"<{i:02d}>" + "역" * 36 for i in range(50))      # 2,000자 — 40자마다 꼬리표


@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_조회_단계는_역할_앞_280자만_받는다(tokens):
    _win(tokens)
    rec = _Recorder()
    asyncio.run(d._free_gather_one(rec, {"key": "mech-a", "role": _ROLE}, "질문", "맥락", 3))
    sysmsg = rec.seen[0][0][1]
    assert f"전문가({_ROLE[:280]}" in sysmsg and _ROLE[:281] not in sysmsg


def _calls(n, size):
    msgs = [_Msg("ai", "", tool_calls=[{"id": f"c{i}", "args": {"i": i}} for i in range(n)])]
    msgs += [_Msg("tool", f"<r{i}>" + "값" * size + f"</r{i}>", tool_call_id=f"c{i}", name=f"tool{i}")
             for i in range(n)]
    return msgs + [_Msg("ai", "조회 요약: 끝")]


@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_자기_조회_블록은_3500자를_넘지_않는다(tokens):
    _win(tokens)
    _k, _c, block, _e = asyncio.run(d._free_gather_one(_Recorder(_calls(3, 5000)), {"key": "mech-a", "role": "r"},
                                                       "질문", "맥락", 3))
    body = block.split("\n(전문가 자체 요약)")[0]
    assert 0 < len(body) <= 3500, len(body)


# ── 1백만 토큰 창 — 직전 라운드 몫과 값당 직렬화 상한 ────────────────────────────────────
def _worst_seat_chars():
    """좌석 발언 턴 하나에 실릴 수 있는 가변부의 최악 합(자) — 근거·챗 맥락·직전 라운드·덧붙는 몫 전부."""
    return (d._evid_budget() + d._chat_ctx_budget() + d._seat_ctx() + d._lookup_budget()
            + max(d._share_budget(r) for r in range(1, 9)) + d._tool_inject_caps()[1] + d._summary_max())


def test_넓은_창은_직전_라운드_몫이_창에서_는다():
    _win(_BIG)
    assert d._seat_ctx() >= 150000, f"1백만 토큰 창인데 직전 라운드 몫이 {d._seat_ctx():,}자다"
    for t in _SMALL:
        _win(t)
        assert d._seat_ctx() == 48000, (t, d._seat_ctx())


@pytest.mark.parametrize("tokens", (200000, 302000, 310000, 400000, _BIG, 2 * _BIG))
def test_몫을_다_채워도_좌석_발언_턴이_창_안에_든다(tokens):
    """발언 턴은 도구를 안 묶으므로 스키마 몫(_EVID_RESERVE 의 40,000토큰)이 빈다 — 그것까지 넘기지 않는다."""
    _win(tokens)
    worst = app._est_tokens("가" * _worst_seat_chars()) + d._EVID_RESERVE - 40000
    assert worst <= tokens * 0.99, f"창 {tokens:,} 에서 좌석 발언 턴 최악 {worst:,}토큰 — 넘는다"


def test_넓은_창은_값당_직렬화_상한이_없다():
    _win(_BIG)
    assert d._ser_clip() == 0 and d._ser_val("가" * 5000) == "가" * 5000
    for t in _SMALL:
        _win(t)
        assert d._ser_clip() == 700, (t, d._ser_clip())


def test_넓은_창에서도_명시한_값당_상한은_그대로_걸린다(monkeypatch):
    """환경변수는 낮출 때만 쓰는 천장이다 — 넓은 창이라고 명시값을 무시하지 않는다."""
    monkeypatch.setattr(d, "_SER_CLIP", 1500)
    monkeypatch.setattr(d, "_SER_CLIP_SET", True)
    _win(_BIG)
    assert d._ser_clip() == 1500


def test_넓은_창에서도_명시한_직전_라운드_몫은_낮추기만_한다(monkeypatch):
    monkeypatch.setattr(d, "_SEAT_CTX", 30000)
    monkeypatch.setattr(d, "_SEAT_CTX_SET", True)
    _win(_BIG)
    assert d._seat_ctx() == 30000
    monkeypatch.setattr(d, "_SEAT_CTX", 10 ** 9)
    assert d._seat_ctx() < 10 ** 9, "명시값이 창에서 유도한 몫보다 크면 창 쪽이 이긴다"


# 스트림을 실제로 돌려 다음 라운드 좌석이 받은 글을 본다.
from test_delib_silent_drops import _cards, _steps, _stream  # noqa: E402

_LONG = 20000           # 좌석당 관점 길이 — 5석이면 100,000자(종전 48,000자 몫과 값당 700자를 모두 넘는다)


def _run_rounds(monkeypatch, tokens, seats=5):
    import json
    import re
    seen = []

    async def _llm(_obj, system, human):
        m = re.search(r"당신은 '([^']+)' 전문가", system)
        if not m:
            return "결정문"
        k = m.group(1)
        seen.append((k, human))
        body = f"<{k}>" + "가" * _LONG + f"</{k}>"
        return json.dumps({"lens": body, "reads": [], "recommendation": "권장", "concerns": ["A", "B"],
                           "position_short": "요약", "concede": [], "rebut": ["반박"], "deepen": body,
                           "final_position": "최종", "non_negotiable": "", "vote": "진행", "stance": "동의"},
                          ensure_ascii=False)

    monkeypatch.setattr(d, "_llm_text", _llm)
    _win(tokens)
    events = _stream(monkeypatch, {"personas": [{"key": f"dom{i}-seat", "role": "역할"} for i in range(seats)],
                                   "rounds": 2, "save_report": 0, "persona_knowledge": 0, "rebut_quote": 0,
                                   "cross_exam": 0},
                     until=lambda ev, _data: ev == "done")
    later = [h for _k, h in seen if "[1라운드 전원]" in h]
    assert later, "시험 전제 — 2라운드 좌석이 직전 라운드를 받았다"
    return events, later


def test_넓은_창에서는_다음_라운드_좌석이_긴_발언을_온전히_받는다(monkeypatch):
    events, later = _run_rounds(monkeypatch, _BIG)
    for h in later:
        missing = [i for i in range(5) if f"</dom{i}-seat>" not in h]
        assert not missing, f"직전 라운드 발언의 끝이 좌석에게 안 갔다 — {missing}"
    cut = {"좌석 직전 라운드 상한 초과", "직렬화 값 상한 초과"}
    assert not [c for c in _cards(events, included=False) if c["source"] in cut]
    assert not [s for s in _steps(events) if s.startswith("직렬화 값 상한")]


def test_좁은_창에서는_다음_라운드_좌석이_종전대로_값마다_끊긴_판을_받는다(monkeypatch):
    events, later = _run_rounds(monkeypatch, 128000)
    assert all("</dom0-seat>" not in h for h in later)
    assert [c for c in _cards(events, included=False) if c["source"] == "직렬화 값 상한 초과"]


# ── 자기 조회 결과(발언 턴에 싣는 블록)와 조회 턴의 역할 ─────────────────────────────────────
def _gather(tokens, n, size, role="r"):
    _win(tokens)
    rec = _Recorder(_calls(n, size))
    out = asyncio.run(d._free_gather_one(rec, {"key": "mech-a", "role": role}, "질문", "맥락", n))
    return out[2], rec.seen[0][0][1]


def test_넓은_창은_자기_조회_결과를_온전히_발언_턴에_싣는다():
    block, _sys = _gather(_BIG, 3, 5000)
    assert all(f"</r{i}>" in block for i in range(3)), "조회 결과의 끝이 발언 턴에 안 갔다 — 호출당 900자에서 끊었다"


@pytest.mark.parametrize("tokens", (128000, _BIG))
def test_넘치면_호출마다_같은_몫으로_줄이고_원문_길이를_남긴다(tokens):
    size = 300000 if tokens == _BIG else 5000
    block, _sys = _gather(tokens, 3, size)
    body = block.split("\n(전문가 자체 요약)")[0]
    assert len(body) <= d._lookup_budget(), (len(body), d._lookup_budget())
    for i in range(3):
        line = next(ln for ln in body.splitlines() if ln.startswith(f"- tool{i}("))
        assert f"{size + 9:,}자 중 앞 " in line, f"끊은 줄에 원문 길이가 없다 — {line[-60:]}"
    assert len(body) >= d._lookup_budget() * 0.9, "몫이 남는데 덜 실었다"


def test_넓은_창의_조회_단계는_역할을_자르지_않는다():
    _block, sysmsg = _gather(_BIG, 1, 10, role=_ROLE)
    assert f"전문가({_ROLE}). " in sysmsg


def test_좁은_창의_조회_단계도_역할을_잘랐으면_말한다():
    _block, sysmsg = _gather(128000, 1, 10, role=_ROLE)
    assert f"전문가({_ROLE[:280]} …[2,000자 중 앞 280자]). " in sysmsg, sysmsg[:400]


def _stream_gather(monkeypatch, tokens, size):
    import json
    import langgraph.prebuilt

    from test_delib_silent_drops import _Tool

    async def _llm(_obj, system, _human):
        if "전문가입니다" not in system:
            return "결정문"
        return json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["A", "B"],
                           "position_short": "요약", "concede": [], "rebut": ["반박"], "deepen": "심화",
                           "final_position": "최종", "non_negotiable": "", "vote": "진행", "stance": "동의"},
                          ensure_ascii=False)

    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda _llm, _tools, **_k: _Recorder(_calls(3, size)))
    _win(tokens)
    return _stream(monkeypatch, {"personas": [{"key": "mech-a", "role": "역할"}, {"key": "rel-b", "role": "역할"}],
                                 "rounds": 2, "save_report": 0, "rebut_quote": 0, "free_tools": 1,
                                 "persona_knowledge": 0},
                   tools={"agent_search": _Tool("agent_search"), "get_material": _Tool("get_material")},
                   until=lambda ev, _data: ev == "done")


def test_자기_조회_결과를_줄였으면_라운드마다_한_장으로_알린다(monkeypatch):
    events = _stream_gather(monkeypatch, 128000, 5000)
    cards = [c for c in _cards(events, included=False) if c["source"] == "자기 조회 결과 상한 초과"]
    assert len(cards) == 1, [c["source"] for c in _cards(events, included=False)]   # 수렴 라운드는 조회하지 않는다
    assert "mech-a" in cards[0]["text"] and "rel-b" in cards[0]["text"] and "15,027자" in cards[0]["text"], cards[0]
    assert "DELIB_" not in cards[0]["text"]
    assert any(s.startswith("자기 조회 결과 상한") for s in _steps(events))


def test_넓은_창에서_다_실었으면_알리지_않는다(monkeypatch):
    events = _stream_gather(monkeypatch, _BIG, 5000)
    assert [c for c in _cards(events, included=True) if c["source"].startswith("mech-a · tool")], "시험 전제 — 조회가 돌았다"
    assert not [c for c in _cards(events, included=False) if c["source"] == "자기 조회 결과 상한 초과"]
