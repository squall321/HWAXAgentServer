# 자유 조회(free-gather) — 도구를 부른 좌석이 근거를 싣고 오는가
#
# 왜 생겼나. 짝 키(tool_call_id)를 싣느라 `calls` 항목을 3-튜플에서 4-튜플로 늘리면서
# 소비처 한 곳(`good = [... for n, ap, b in calls]`)을 안 고쳤다. 그 줄은 try 밖이라
# `ValueError` 가 호출부의 blanket except 로 올라가 `continue` 되고, **도구를 부른
# 좌석만** 근거 0건이 됐다. 아무것도 안 부른 좌석은 멀쩡하니 화면은 "이번엔 조회할 게
# 없었나 보다" 와 똑같이 보였다 — 이 리포가 반복해서 만나는 모양이다.
#
# 그래서 이 파일은 **반환값을 실제로 받아 본다.** 이름이 있는지만 보면 또 못 잡는다.
import asyncio

import deliberation as d


class _Msg:
    def __init__(self, type_, content, tool_calls=None, tool_call_id=None):
        self.type, self.content = type_, content
        self.tool_calls, self.tool_call_id = tool_calls or [], tool_call_id


class _Agent:
    """`astream` 으로 메시지를 흘리는 가짜 — 실제 계약과 같은 모양이다."""

    def __init__(self, msgs):
        self._msgs = msgs

    async def astream(self, _inp, config=None, stream_mode=None):
        yield {"messages": self._msgs}


PERSONA = {"key": "구조", "role": "구조 해석"}


def _run(msgs, budget=3):
    return asyncio.run(d._free_gather_one(_Agent(msgs), PERSONA, "질문", "맥락", budget))


def test_도구를_부른_좌석이_근거를_싣고_온다():
    msgs = [
        _Msg("ai", "", tool_calls=[{"id": "c1", "args": {"name": "PLATE"}}]),
        _Msg("tool", '{"parts": ["PLATE_1"]}', tool_call_id="c1"),
        _Msg("ai", "조회 요약: 판 1장"),
    ]
    key, calls, block, err = _run(msgs)
    assert key == "구조"
    assert err == "", f"멀쩡한 조회가 실패로 보고됐다: {err}"
    assert len(calls) == 1, calls
    assert len(calls[0]) == 4, "짝 키까지 네 칸이다 — 소비처와 폭이 어긋나면 터진다"
    assert calls[0][3] == "c1", "짝 키가 안 실렸다 — 인자와 결과를 다시 못 붙인다"
    assert "PLATE_1" in block, f"근거 블록이 비었다 — 좌석이 기억으로 말하게 된다: {block!r}"


def test_빈_결과는_근거로_치지_않는다():
    """`[]` 는 에러는 아니지만 근거도 아니다 — 주입하면 '조회했으나 없음' 이 수치처럼 보인다."""
    msgs = [
        _Msg("ai", "", tool_calls=[{"id": "c1", "args": {}}]),
        _Msg("tool", "[]", tool_call_id="c1"),
    ]
    _key, calls, block, err = _run(msgs)
    assert len(calls) == 1, "이력에는 남아야 한다"
    assert block == "" and err == ""


def test_아무것도_안_부른_좌석과_구분된다():
    """이 둘이 같은 모양이면 결함이 숨는다 — 위 검사들이 의미를 갖는 근거다."""
    _key, calls, block, err = _run([_Msg("ai", "조회 불필요")])
    assert calls == [] and block == "" and err == ""


def test_여러_번_부르면_예산까지만_남는다():
    msgs = [_Msg("ai", "", tool_calls=[{"id": f"c{i}", "args": {"i": i}} for i in range(5)])]
    msgs += [_Msg("tool", f'{{"v": {i}}}', tool_call_id=f"c{i}") for i in range(5)]
    _key, calls, block, _err = _run(msgs, budget=2)
    assert len(calls) == 2, calls
    assert block.count("\n- ") + 1 == 2, block


class _Dies:
    """중간까지 흘리고 터지는 가짜 — 클라이언트 끊김·컨텍스트 초과가 이 모양이다."""

    def __init__(self, msgs):
        self._msgs = msgs

    async def astream(self, _inp, config=None, stream_mode=None):
        yield {"messages": self._msgs}
        raise RuntimeError("컨텍스트 초과")


def test_중단은_AI_머리말이_있어도_보고된다():
    """중단 표식을 `summary` 에만 두면 **해석 루프가 AI 메시지로 덮어쓴다** — 모델이
    머리말 한 줄만 냈어도 표식이 사라지고 경고 경로를 안 탄다. 그러면 화면이
    '이 좌석은 조회할 게 없다고 판단했다' 와 픽셀 단위로 같아진다(7차 감사)."""
    # ① AI 머리말이 **있는** 경우 — 여기가 덮이던 자리다
    msgs = [_Msg("ai", "먼저 재료를 찾아보겠습니다."),
            _Msg("ai", "", tool_calls=[{"id": "c1", "args": {}}]),
            _Msg("tool", '{"v": 1}', tool_call_id="c1")]
    _k, _calls, _blk, err = asyncio.run(
        d._free_gather_one(_Dies(msgs), PERSONA, "질문", "맥락", 3))
    assert err and "중단" in err, f"중단이 보고되지 않았다: {err!r}"

    # ② 머리말이 **없는** 경우도 그대로 — 원래 되던 것을 깨지 않았는지
    _k2, _c2, _b2, err2 = asyncio.run(
        d._free_gather_one(_Dies([_Msg("tool", '{"v": 1}', tool_call_id="c1")]),
                           PERSONA, "질문", "맥락", 3))
    assert err2 and "중단" in err2

    # ③ 정상 완주는 사유가 비어 있어야 한다 — 아무 때나 경고하면 경고가 소음이 된다
    _k3, _c3, _b3, err3 = _run([_Msg("ai", "조회 요약: 끝")])
    assert err3 == ""


# ── 조회 단계의 지침(lookup_hint) — 리스크 심사 좌석에게만, 역할 뒤에 한 번 ─────────────────────
# 리스크 심사 좌석 계약의 '필수 도구' 줄은 역할 접미로 붙는데, 조회 단계는 역할의 앞 280자만 봐서 그 줄이
# 도구를 쥔 단계에 닿지 않았다(설계 R-28). 지침이 없는 좌석은 글이 한 글자도 바뀌지 않아야 한다.
import hashlib  # noqa: E402


class _Recorder(_Agent):
    """받은 프롬프트(시스템·사람 글)를 적어 두는 가짜."""

    def __init__(self):
        super().__init__([])
        self.seen = []

    async def astream(self, inp, config=None, stream_mode=None):
        self.seen.append(inp["messages"])
        yield {"messages": []}


def _prompts(persona, question="질문 diff_id=3f2a"):
    rec = _Recorder()
    asyncio.run(d._free_gather_one(rec, persona, question, "맥락", 3))
    (msgs,) = rec.seen
    return msgs[0][1], msgs[1][1]


_RISK_SEAT = {"key": "mech-housing-structure",
              "role": "원본 역할\n" + d._RISK_SEAT_CONTRACT["_common"] + "\n" + d._RISK_SEAT_CONTRACT["mech"]}


def test_조회_지침이_없는_좌석의_시스템_글은_종전과_한_글자도_다르지_않다():
    """특성화 — 해시는 지침을 넣기 전 코드에서 뜬 값이다. 다른 의장 틀·합성 지정석·발굴된 좌석이 이 길이다."""
    for persona, want in ((PERSONA, "dd52a224c163a55b5311a4d081c6fcaa17d6c8621e92241aa83caf4bfeb7f33d"),
                          (_RISK_SEAT, "1e04c375fb1d0208112aabd102a634f8c16f4a22e3ce3af4ad7791a7d6ba7a77")):
        sysmsg, _human = _prompts(persona)
        assert hashlib.sha256(sysmsg.encode()).hexdigest() == want, (persona["key"], sysmsg)


def test_조회_지침은_역할_뒤에_한_번_실린다():
    hint = d._risk_lookup_hint("mech", "질문 diff_id=3f2a")
    sysmsg, _human = _prompts({**_RISK_SEAT, "lookup_hint": hint})
    assert sysmsg.count(f"[이 심의의 조회 지침] {hint} ") == 1, sysmsg
    assert sysmsg.index("[이 심의의 조회 지침]") > sysmsg.index("전문가("), "역할보다 앞에 왔다"
    assert sysmsg.index("[이 심의의 조회 지침]") < sysmsg.index("지금은 심의 발언 전의 데이터 조회 단계다")


def test_조회_프롬프트에_diff_id_값이_실제로_들어_있다():
    """안내는 '심의 주제에 diff_id= 가 있으면' 이라고 말한다 — 조회 단계가 받는 글에 그 값이 실제로 있어야 따를 수
    있다(설계 P-06). 질문 글은 엔진의 표식 형식을 보는 것이라 손으로 적는다 — 리스크 앱이 실제로 만든 질문으로
    보는 것은 세 리포 사슬 시험(EX1)의 몫이다."""
    q = "[리스크 심사] 힌지 브래킷 변경 — diff_id=3f2a9c 기준 변경 18건"
    hint = d._risk_lookup_hint("mech", q)
    sysmsg, human = _prompts({**_RISK_SEAT, "lookup_hint": hint}, question=q)
    assert hint.startswith(d._RISK_LOOKUP_NOTE)
    assert "diff_id=3f2a9c" in sysmsg + human


def test_모든_도메인의_조회_지침이_자르지_않고_실린다():
    """조회 지침에는 글자 상한을 두지 않는다 — 엔진 상수로만 만든 글이라 길이는 계약 줄이 정하고, 잘리면 계약 줄의
    뒤쪽('산출:')이 말없이 빠진다(사용자 지침 — 1백만 토큰 창을 가정하고 조용히 자르지 않는다)."""
    doms = [k for k in d._RISK_SEAT_CONTRACT if k != "_common"]
    assert len(doms) == 15
    for dom in doms:
        hint = d._risk_lookup_hint(dom, "질문 diff_id=3f2a")
        sysmsg, _human = _prompts({"key": f"{dom}-seat", "role": "역할", "lookup_hint": hint})
        assert f"[이 심의의 조회 지침] {d._RISK_LOOKUP_NOTE} {d._RISK_SEAT_CONTRACT[dom]} " in sysmsg, dom
