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
            + max(d._share_budget(r) for r in range(1, 9)) + d._tool_inject_caps()[1] + d._summary_max()
            + d._human_note_max())


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


# ── 지정 도구 결과 — 도구당 2,000자·합 5,000자 ───────────────────────────────────────────
def test_넓은_창은_지정_도구_결과를_온전히_싣는다():
    _win(_BIG)
    text, share = d._fit_tool_results(_tool_got(), d._tool_inject_caps()[1])
    assert share == 0 and all(f"</t{i}>" in text for i in range(3)) and " …[" not in text


def test_넓은_창에서_도구_하나가_몫을_넘으면_원문_길이를_남긴다():
    _win(_BIG)
    total = d._tool_inject_caps()[1]
    text, share = d._fit_tool_results([("[지정 도구 t0]\n", "가" * 300000)], total)
    assert len(text) <= total and "300,000자 중 " in text and share > 0, (len(text), text[-40:])


def test_좁은_창에서_도구_하나만_길어도_줄였다고_원장에_남긴다(monkeypatch):
    """종전엔 합이 상한 안이면 도구 하나의 상한(2,000자)에서 끊어도 글의 표식뿐이라 호출자·원장은 몰랐다."""
    from test_tool_inject_fit import _CARD, _run as _tool_run
    _win(128000)
    _names, events, block = _tool_run(monkeypatch, [50000])
    assert "자 중 2,000자]" in block
    (card,) = [c for c in _cards(events, included=False) if c["source"] == _CARD]
    assert "get_probe_1" in card["text"] and "2,000자" in card["text"], card["text"]


def test_넓은_창에서는_긴_지정_도구_결과가_좌석에_온전히_간다(monkeypatch):
    from test_tool_inject_fit import _CARD, _run as _tool_run
    _win(_BIG)
    _names, events, block = _tool_run(monkeypatch, [15000, 15000])        # 합 30,070자 — 좁은 창이면 도구당 2,000자
    assert block.count("x") == 30000 and " …[" not in block, block[-200:]
    assert not [c for c in _cards(events, included=False) if c["source"] == _CARD]


class _SchemaTool:
    name = "get_probe"
    description = "설" * 500
    args_schema = {"type": "object", "required": ["a00"],
                   "properties": {f"a{i:02d}": {"type": "string", "description": f"<d{i}>" + "명" * 150,
                                                **({"enum": ["x", "y"]} if i == 15 else {})}
                                  for i in range(20)}}


def test_넓은_창은_인자_스키마를_자르지_않는다():
    _win(_BIG)
    brief = d._tool_schema_brief(_SchemaTool())
    assert all(f"- a{i:02d}" in brief for i in range(20)) and "명" * 150 in brief
    assert "x" in brief.split("- a15")[1].split("\n")[0], "선택지(enum)가 계획자에게 안 갔다"


def test_좁은_창은_인자_스키마를_종전대로_줄이되_말한다():
    _win(128000)
    brief = d._tool_schema_brief(_SchemaTool())
    assert all(f"- a{i:02d}" in brief for i in range(12)) and "- a12" not in brief
    assert "인자 20개 중 앞 12개" in brief


# ── 이어하기 요약(이전 결정문) — 8,000자 ─────────────────────────────────────────────────
def test_넓은_창은_이어하기_요약을_창에서_유도한_길이까지_받는다():
    """리스크 심사 결정문은 산문 뒤에 risk_spec JSON 이 붙어 8,000자를 쉽게 넘는다 — 뒤의 risk_spec 이 잘렸다."""
    _win(_BIG)
    cap = d._summary_max()
    assert cap > 8000
    o = d._resolve_opts({"continue_summary": "가" * (cap - 1) + "끝"})
    assert o.continue_summary.endswith("끝") and not o.req_cut
    o = d._resolve_opts({"continue_summary": "가" * (cap + 5)})
    assert len(o.continue_summary) == cap and f"{cap + 5:,}자 중" in o.req_cut["continue_summary"], o.req_cut


def test_넓은_창은_의장_실패_결정문_자리도_이어하기가_받는_만큼_싣는다():
    rows = [(f"seat{i:02d}", "가" * 2000 + f"<끝{i}>") for i in range(10)]
    _win(_BIG)
    text = d._chair_fail_text("시간 초과", 2, "좌석 10석", rows, "3R 최종")
    assert all(f"<끝{i}>" in text for i in range(10)) and len(text) <= d._summary_max()
    _win(128000)
    text = d._chair_fail_text("시간 초과", 2, "좌석 10석", rows, "3R 최종")
    assert len(text) <= 8000 and "<끝0>" not in text


# ── 좌석 JSON 이 깨졌을 때 — 원문 2,000자 ─────────────────────────────────────────────────
def _persona_say(monkeypatch, reply):
    async def _llm(_obj, _system, _human):
        return reply

    monkeypatch.setattr(d, "_llm_text", _llm)
    return asyncio.run(d._persona_round(object(), {"key": "mech-a", "role": "기구"}, "말하라",
                                        required=("lens",)))["say"]


def test_구조화에_실패해도_원문을_전부_보존한다(monkeypatch):
    """창과 무관하다 — 다음 라운드·의장에게 줄 때의 길이는 직렬화가 지킨다(좁은 창은 값당 700자)."""
    prose = "".join(f"<{i:03d}>" + "가" * 95 for i in range(60))       # 6,000자 — 100자마다 꼬리표
    say = _persona_say(monkeypatch, prose)
    assert say.startswith("(구조화 실패 — 원문 강등") and say.endswith(prose), say[:80]
    assert "6,000자" in say.split(")")[0], "강등 표식에 원문 길이가 없다"


def test_요구_키가_빠져도_원문을_전부_보존한다(monkeypatch):
    import json
    say = _persona_say(monkeypatch, json.dumps({"say": "나" * 5000}, ensure_ascii=False))
    assert say.startswith("(요구 키 누락 — 원문 보존) ") and say.endswith("나" * 5000)


# ── 지식카드 한 줄 — 700자(표식 없음) ─────────────────────────────────────────────────────
_HIT = {"title": "힌지 피로", "record_id": "R-1", "section_id": "s3", "snippet": "".join(
    f"<{i:02d}>" + "가" * 96 for i in range(20)) + "<끝>"}                       # 2,003자


def test_지식카드_줄을_잘랐으면_원문_길이를_남긴다():
    full = d.knowledge_line(_HIT, limit=0)
    assert full.endswith("<끝>") and "(출처: R-1 §s3)" in full
    cut = d.knowledge_line(_HIT)                                  # 챗·띵킹이 쓰는 기본 — 종전 700자
    assert cut.startswith(full[:700]) and cut.endswith(f" …[{len(full):,}자 중 앞 700자]"), cut[-40:]


def test_심의_좌석은_지식카드_줄을_자기_예산까지_받는다(monkeypatch):
    """줄마다 700자에서 말없이 끊겨, 예산(좌석당 합)이 남아도 카드 한 장의 뒤쪽이 좌석에 안 갔다."""
    import json
    import re

    from test_delib_silent_drops import _Tool
    seen = []

    async def _llm(_obj, system, human):
        if re.search(r"당신은 '[^']+' 전문가", system):
            seen.append(human)
        return json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["a", "b"],
                           "position_short": "입장"}, ensure_ascii=False)

    monkeypatch.setattr(d, "_llm_text", _llm)
    _win(128000)
    _stream(monkeypatch, {"rounds": 2, "persona_knowledge": 1},
            tools={"agent_search": _Tool("agent_search", answer=json.dumps({"hits": [_HIT]}, ensure_ascii=False))},
            until=lambda ev, data: ev == "delib" and data.get("kind") == "stage" and str(data.get("stage")) == "r2")
    kb = [h for h in seen if "[당신의 지식카드에서" in h]
    assert kb and all("<끝>" in h for h in kb), "지식카드 한 장의 뒤쪽이 좌석에 안 갔다 — 줄당 700자에서 끊었다"


# ── 다른 좌석의 조회 결과(공용 풀) — 항목당 420자와 틀린 '전체 N자' 표식 ─────────────────────────
class _PerSeat:
    """좌석마다 다른 인자로 조회하는 가짜 에이전트 — 서명이 같으면 공용 블록이 자기 것으로 보고 뺀다."""

    def __init__(self, size):
        self.size = size

    async def astream(self, inp, config=None, stream_mode=None):
        import re
        k = re.search(r"당신은 '([^']+)' 전문가", inp["messages"][0][1]).group(1)
        yield {"messages": [_Msg("ai", "", tool_calls=[{"id": f"{k}-c", "args": {"seat": k}}]),
                            _Msg("tool", f"<{k}>" + "값" * self.size + f"</{k}>", tool_call_id=f"{k}-c",
                                 name="get_probe")]}


def _share_prompts(monkeypatch, tokens, size):
    import json
    import re

    import langgraph.prebuilt

    from test_delib_silent_drops import _Tool
    seen = {}

    async def _llm(_obj, system, human):
        m = re.search(r"당신은 '([^']+)' 전문가", system)
        if not m:
            return "결정문"
        seen.setdefault(m.group(1), []).append(human)
        return json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["A", "B"],
                           "position_short": "요약", "concede": [], "rebut": ["반박"], "deepen": "심화",
                           "final_position": "최종", "non_negotiable": "", "vote": "진행", "stance": "동의"},
                          ensure_ascii=False)

    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda _llm, _tools, **_k: _PerSeat(size))
    _win(tokens)
    _stream(monkeypatch, {"personas": [{"key": "mech-a", "role": "역할"}, {"key": "rel-b", "role": "역할"}],
                          "rounds": 3, "save_report": 0, "rebut_quote": 0, "free_tools": 1, "persona_knowledge": 0},
            tools={"agent_search": _Tool("agent_search"), "get_probe": _Tool("get_probe")},
            until=lambda ev, _data: ev == "done")
    blk = "[다른 전문가가 조회한 결과"
    return [h[h.index(blk):] for h in seen["rel-b"] if blk in h]


def test_공용_조회_결과의_표식은_원문_길이를_적는다(monkeypatch):
    """풀에 넣을 때 이미 840자로 잘라 '전체 N자' 가 최대 840 이었다 — 15만 자 결과도 '전체 840자'."""
    got = _share_prompts(monkeypatch, 128000, 150000)
    assert got, "시험 전제 — rel-b 가 mech-a 의 조회 결과를 받았다"
    line = next(ln for ln in got[0].splitlines() if "get_probe" in ln and "mech-a" in ln)
    assert "원문 150,017자" in line, line[-80:]


def test_넓은_창은_공용_조회_결과를_몫이_닿는_데까지_싣는다(monkeypatch):
    got = _share_prompts(monkeypatch, _BIG, 5000)
    assert got and all("</mech-a>" in g for g in got), "다른 좌석의 조회 결과가 420자에서 끊겼다"


def test_좁은_창의_공용_조회_결과는_종전대로_항목당_420자다(monkeypatch):
    got = _share_prompts(monkeypatch, 128000, 5000)
    line = next(ln for ln in got[0].splitlines() if "get_probe" in ln and "mech-a" in ln)
    assert "</mech-a>" not in line and "앞 420자만" in line, line[-80:]


# ── 챗에서 이어진 맥락(포털 웹 챗 → 심의) — 천장 150,000자·발화당 4,000/2,500자·앞 60건 ────────────────
def test_넓은_창의_챗_맥락은_천장에_걸리지_않는다():
    _win(_BIG)
    assert d._chat_ctx_budget() == int(d._pre_budget() * d._CHAT_CTX_SHARE) > d.CHAT_CONTEXT_BUDGET


def test_넓은_창은_긴_사람_발화를_통째로_싣는다():
    _win(_BIG)
    note = d._chat_context_note([{"role": "user", "content": "가" * 10000 + "<끝>"}])
    assert "<끝>" in note


def test_좁은_창은_발화를_종전_길이에서_끊되_말한다():
    _win(128000)
    note = d._chat_context_note([{"role": "user", "content": "가" * 10000 + "<끝>"}])
    assert "<끝>" not in note and "10,003자 중 앞 4,000자" in note, note[-80:]


@pytest.mark.parametrize("tokens", (128000, _BIG))
def test_긴_대화는_최근_발화부터_남긴다(tokens):
    """앞에서 60건을 잘라, 긴 대화에서는 정작 최근 발화(심의를 부른 맥락)가 빠졌다."""
    _win(tokens)
    hist = [{"role": "user", "content": f"<{i:03d}> 발화"} for i in range(70)]
    note = d._chat_context_note(hist)
    assert "<069>" in note, "가장 최근 발화가 빠졌다"
    if tokens == _BIG:
        assert "<000>" in note
    else:
        assert "<000>" not in note and "앞선 발화 10건" in note, note[:400]


# ── 사람 의견(human_note) — 2,000자 ───────────────────────────────────────────────────────
def test_넓은_창은_사람_의견을_창에서_유도한_길이까지_싣는다():
    """포털은 이 칸에 8,000자까지 받고 MCP 호출자는 더 보낸다 — 1백만 토큰 창에서도 앞 2,000자만 좌석에 갔다."""
    _win(_BIG)
    assert d._human_note_max() >= 8000
    o = d._resolve_opts({"human_note": "가" * 8000})
    assert o.human_note == "가" * 8000 and not o.human_note_cut
    for t in _SMALL:
        _win(t)
        assert d._human_note_max() == d.HUMAN_NOTE_MAX == 2000


def test_넓은_창에서도_명시한_사람_의견_상한은_그대로다(monkeypatch):
    monkeypatch.setattr(d, "HUMAN_NOTE_MAX", 1500)
    monkeypatch.setattr(d, "_HUMAN_NOTE_MAX_SET", True)
    _win(_BIG)
    assert d._human_note_max() == 1500


def test_넓은_창의_메뉴가_지금_걸리는_사람_의견_상한을_알린다():
    import mcp_server as m
    _win(_BIG)
    menu = asyncio.run(m.deliberate_jobs())
    assert menu["limits"]["human_note_chars"] == d._human_note_max() > 2000


# ── 좌석 출력의 JSON 찾기 — 앞 100,000자만 훑었다 ──────────────────────────────────────────
_BIGJ = {"lens": "관점", "reads": [f"<{i:05d}> " + "해석" * 40 for i in range(1500)], "recommendation": "권장"}


def test_펜스_안의_긴_JSON_도_읽는다():
    import json
    txt = "산문 " * 100 + "\n```json\n" + json.dumps(_BIGJ, ensure_ascii=False) + "\n```"
    assert len(txt) > 100000
    got = d._parse_json(txt)
    assert isinstance(got, dict) and len(got.get("reads") or []) == 1500, type(got)


def test_긴_산문_뒤의_JSON_도_읽는다():
    import json
    txt = "긴 산문 논증. " * 15000 + json.dumps({"lens": "끝의 관점", "recommendation": "권장"}, ensure_ascii=False)
    assert len(txt) > 100000
    assert (d._parse_json(txt) or {}).get("lens") == "끝의 관점"


def test_산문_뒤의_긴_JSON_은_안쪽_객체가_아니라_바깥_객체다():
    import json
    txt = "산문 논증. " * 100 + json.dumps({**_BIGJ, "reads": [{"r": i, "t": "가" * 80} for i in range(1500)]},
                                         ensure_ascii=False)
    got = d._parse_json(txt)
    assert isinstance(got, dict) and got.get("lens") == "관점" and len(got["reads"]) == 1500


# ── 근거 항목의 곁 필드 — 출처 200·도구 80·인자 1,200자와 형식 밖 키를 말없이 버렸다 ─────────────────────
def test_근거_항목의_곁_필드를_잘랐으면_원문_길이를_남긴다():
    o = d._resolve_opts({"evidence": [{"source": "출" * 300, "tool": "t" * 100, "args": "a" * 2000, "result": "본문"}]})
    e = o.evidence[0]
    assert "300자 중 앞 200자" in e["source"] and "100자 중 앞 80자" in e["tool"], e
    assert f"2,000자 중 앞 {d._EVID_ARGS_MAX:,}자" in e["args"], e["args"][-40:]


def test_형식_밖_근거_키는_버리되_알린다():
    o = d._resolve_opts({"evidence": [{"key": "E 3]", "result": "본문"}, {"key": "E4", "result": "본문"}]})
    assert [e["key"] for e in o.evidence] == ["", "E4"]
    note = " ".join(o.req_unread.values())
    assert "1건" in note and "E 3]" in note, o.req_unread


# ── 기록용 발언(say_full) — 필드를 골라 합성해 해석(reads)·우려·양보 불가 조항이 빠졌다 ───────────────────
# say_full 은 잡 원장 → deliberate_transcript(full=true) → Claude 가 리스크 앱에 내는 패널 결과로 간다. 리스크 심사 좌석은
# 지적을 해석(reads)에 적는데, 1라운드 say_full 은 관점·권장 둘뿐이었다(글자 상한이 아니라 필드를 골라서 빠졌다).
_R1 = {"lens": "관점 문장.", "reads": ["해석 하나 [e:1]", {"claim": "해석 둘", "cite": "c:2"}, "해석 셋"],
       "recommendation": "권장 문장.", "concerns": ["우려 A", "우려 B"], "position_short": "요약"}
_R2 = {"concede": ["수용"], "rebut": [{"target": "rel-b", "quote": "인용 문구가 여기에 있다 " * 8, "counter": "반박",
                                     "basis": "근거"}], "deepen": "심화"}
_R3 = {"final_position": "최종 입장.", "non_negotiable": "양보 불가 조항", "vote": "진행", "stance": "조건부 동의"}


def test_화면_버블은_종전_그대로다():
    """특성화 — 구현 전 코드에서 뜬 해시(앞 16자리)."""
    got = [hashlib.sha256(d._say_of(r, o).encode()).hexdigest()[:16] for r, o in ((1, _R1), (2, _R2), (3, _R3))]
    assert got == ["8234c6d0f8ec953b", "a6ae4ad0022370ed", "d1ad40a2952a4dd6"], got


def test_기록용_발언은_라운드가_요구한_필드를_전부_싣는다():
    full1 = d._say_of(1, _R1, full=True)
    for want in ("관점 문장.", "권장 문장.", "해석 하나 [e:1]", "해석 둘", "c:2", "해석 셋", "우려 A", "우려 B"):
        assert want in full1, (want, full1)
    full3 = d._say_of(3, _R3, full=True)
    assert "양보 불가 조항" in full3 and "진행" in full3, full3          # 스탠스는 턴에 따로 실린다
    full2 = d._say_of(2, _R2, full=True)
    assert _R2["rebut"][0]["quote"].strip() in full2, "기록용 발언에서 반박 인용이 80자에서 끊겼다"


# ── 요청 값의 항목 길이 — 양보 불가 조항 1,200자·후보안 400자·지정 좌석 역할 2,000자 ─────────────────────
def test_넓은_창은_요청_값의_항목을_길이로_자르지_않는다():
    _win(_BIG)
    o = d._resolve_opts({"non_negotiables": ["조" * 3000], "options": ["안" * 1000, "B안"],
                         "personas": [{"key": "mech-a", "role": "역" * 5000}]})
    assert o.continue_non_negotiables == ["조" * 3000] and o.options[0] == "안" * 1000
    assert o.continue_personas[0]["role"] == "역" * 5000
    assert not o.req_cut, o.req_cut


def test_좁은_창은_요청_값의_항목을_종전대로_자르고_알린다():
    _win(128000)
    o = d._resolve_opts({"non_negotiables": ["조" * 3000], "options": ["안" * 1000, "B안"],
                         "personas": [{"key": "mech-a", "role": "역" * 5000}]})
    assert len(o.continue_non_negotiables[0]) == d._NN_ITEM_MAX and len(o.options[0]) == d._OPTION_ITEM_MAX
    assert len(o.continue_personas[0]["role"]) == d._ROLE_REQ_MAX
    assert {"non_negotiables", "options", "personas"} <= set(o.req_cut), o.req_cut


# ── 자산·물성 스냅샷(해석 설계·시험 설계 2단) — 블록마다 500~1,400자에서 말없이 끊었다 ───────────────────
def _snap_tools(size):
    from test_delib_silent_drops import _Tool
    names = ("list_tool_apps", "list_materials", "list_operations", "operation_usage", "section_contact_usage",
             "corpus_summary", "slurm_list_templates")
    return {n: _Tool(n, answer=f"<{n}>" + "값" * size + f"</{n}>") for n in names}


def test_넓은_창의_자산_스냅샷은_블록을_자르지_않는다():
    _win(_BIG)
    snap = asyncio.run(d._asset_snapshot(_snap_tools(2000), "힌지"))
    for n in _snap_tools(1):
        assert f"</{n}>" in snap, f"{n} 블록의 끝이 안 실렸다"


def test_좁은_창의_자산_스냅샷은_블록을_끊되_말한다():
    _win(128000)
    snap = asyncio.run(d._asset_snapshot(_snap_tools(2000), "힌지"))
    assert "</list_materials>" not in snap and "자 중 앞 700자]" in snap, snap[:300]


def test_스냅샷_전체_상한에서_끊으면_말한다():
    _win(_BIG)
    snap = asyncio.run(d._asset_snapshot(_snap_tools(6000), "힌지"))
    assert f"앞 {d._ASSET_SNAP_MAX:,}자" in snap and "</slurm_list_templates>" not in snap, snap[-300:]
