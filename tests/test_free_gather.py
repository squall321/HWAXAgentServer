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
