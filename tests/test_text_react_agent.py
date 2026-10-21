# 외부 응답자 좌석의 글 왕복 조회(_TextReactAgent) — CALL 줄 읽기 · 예산 · 차례 상한 · 도구 오류 · 도중 실패가 _free_gather_one 에 그대로 맞는지 본다
#
# 응답자 자리에는 대본대로 답하는 가짜 LLM 을 둔다(과제 줄 · 일꾼은 test_external_llm_delib 이 실제로 돌린다). 실제 Claude 는 부르지 않는다.
#
#   실행:  .venv/bin/python -m pytest tests/test_text_react_agent.py -q
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

import deliberation as d  # noqa: E402

PERSONA = {"key": "mech-a", "role": "기구"}


class _Script:
    """차례마다 정해 둔 답을 낸다 — 받은 (system, human) 을 적는다. 답이 예외면 그것을 올린다."""

    def __init__(self, *answers):
        self.answers, self.seen = list(answers), []

    async def ainvoke(self, msgs):
        self.seen.append((msgs[0].content, msgs[1].content))
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return AIMessage(a)


class _Tool:
    def __init__(self, name, answer='{"v": 1}', *, desc="", schema=None):
        self.name, self.answer, self.description, self.args_schema, self.calls = name, answer, desc, schema, []

    async def ainvoke(self, args):
        self.calls.append(args)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _call(tool, **args) -> str:
    return "CALL " + json.dumps({"tool": tool, "args": args}, ensure_ascii=False)


def _gather(agent, budget=3):
    return asyncio.run(d._free_gather_one(agent, PERSONA, "질문", "맥락", budget))


def test_CALL_줄이_없으면_한_차례로_끝나고_도구를_부르지_않는다():
    llm, t = _Script("조회 불필요"), _Tool("get_x")
    key, calls, block, err = _gather(d._TextReactAgent(llm, [t], 3))
    assert (key, calls, block, err) == ("mech-a", [], "", "") and t.calls == [] and len(llm.seen) == 1
    system, human = llm.seen[0]
    assert "[글로 도구 부르기" in system and "- get_x — " in system and "최대 3회" in system
    assert human.endswith("당신 발언에 필요한 조회를 지금 수행하라."), "첫 차례에는 남은 호출 줄을 덧붙이지 않는다"


def test_도구_목록에는_이름_설명_인자_스키마가_실린다():
    sch = {"type": "object", "properties": {"q": {"type": "string", "description": "질의"}}, "required": ["q"]}
    llm = _Script("조회 불필요")
    _gather(d._TextReactAgent(llm, [_Tool("search_cards", desc="카드를 찾는다", schema=sch), _Tool("get_y")], 2))
    system = llm.seen[0][0]
    assert "- search_cards — 카드를 찾는다\n  인자 스키마: " + json.dumps(sch, ensure_ascii=False) in system
    assert "[쓸 수 있는 도구 — 2종]" in system and "- get_y — \n  인자 스키마: {}" in system


def test_실행한_호출은_도구_이름_인자_결과_짝_키로_돌아오고_요약이_블록에_붙는다():
    t = _Tool("get_x", '{"gap_mm": 0.21}')
    llm = _Script(_call("get_x", id="A-1") + "\n" + _call("get_x", id="A-2"), "조회 요약: 간극 0.21 mm")
    _k, calls, block, err = _gather(d._TextReactAgent(llm, [t], 3))
    assert err == "" and t.calls == [{"id": "A-1"}, {"id": "A-2"}]
    assert [(n, json.loads(a), out) for n, a, out, _c in calls] == [
        ("get_x", {"id": "A-1"}, '{"gap_mm": 0.21}'), ("get_x", {"id": "A-2"}, '{"gap_mm": 0.21}')]
    assert len({c for *_x, c in calls}) == 2 and all(c.startswith("text-") for *_x, c in calls)
    assert '{"gap_mm": 0.21}' in block and block.endswith("(전문가 자체 요약) 조회 요약: 간극 0.21 mm")
    second = llm.seen[1][1]
    assert _call("get_x", id="A-1") + "\n결과:\n" + '{"gap_mm": 0.21}' in second
    assert "(남은 호출 1회 — 더 부를 것이 있으면 CALL 줄로" in second


def test_예산은_차례를_넘어_합으로_센다_다_쓰면_요약만_묻는다():
    t = _Tool("get_x")
    llm = _Script(_call("get_x", n=1) + "\n" + _call("get_x", n=2), _call("get_x", n=3) + "\n" + _call("get_x", n=4),
                  "조회 요약: 끝")
    _k, calls, _b, _e = _gather(d._TextReactAgent(llm, [t], 3))
    assert [c["n"] for c in t.calls] == [1, 2, 3] and len(calls) == 3 and len(llm.seen) == 3
    assert "호출 예산(3회)을 넘었다" in llm.seen[2][1] and '"n": 4' in llm.seen[2][1]
    assert "호출 예산을 다 썼다 — 이번 답은 CALL 줄 없이" in llm.seen[2][1]


def test_마지막_차례의_CALL_줄은_실행하지_않고_차례는_예산_더하기_하나까지다():
    t = _Tool("get_x")
    # 예산 1 — 첫 차례에 하나를 부르면 둘째 차례가 마지막이다. 거기서 또 적은 줄은 실행되지 않는다.
    llm = _Script(_call("get_x", n=1), "조회 요약: 하나\n" + _call("get_x", n=2))
    _k, calls, block, _e = _gather(d._TextReactAgent(llm, [t], 1))
    assert t.calls == [{"n": 1}] and len(llm.seen) == 2 and len(calls) == 1
    assert block.endswith("(전문가 자체 요약) 조회 요약: 하나"), "마지막 차례의 CALL 줄이 요약에 남았다"
    # 줄이 하나도 실행되지 않는 차례도 한 차례다 — 차례는 예산 + 1 번을 넘지 않고(되풀이해 묻지 않는다) 마지막 차례라고 알린다.
    # 예산이 남았어도 마지막 차례의 답이 요약이다.
    t.calls.clear()
    llm = _Script(_call("get_x", n=1), "CALL {깨진}", "조회 요약: 끝\nCALL {깨진}", "남은 답")
    _k, calls, block, _e = _gather(d._TextReactAgent(llm, [t], 2))
    assert len(llm.seen) == 3 and len(calls) == 1 and llm.answers == ["남은 답"] and t.calls == [{"n": 1}]
    assert "(이번이 이 조회 단계의 마지막 차례다 — 이번 답은 CALL 줄 없이" in llm.seen[2][1]
    assert "호출 예산을 다 썼다" not in llm.seen[2][1] and "(남은 호출 1회" in llm.seen[1][1]
    assert block.endswith("(전문가 자체 요약) 조회 요약: 끝")


@pytest.mark.parametrize("line,why", [
    ('CALL {"tool": "write_x", "args": {}}', "목록에 없는 도구다(write_x)"),
    ('CALL {"tool": 7, "args": {}}', "목록에 없는 도구다(7)"),
    ('CALL {"args": {}}', "목록에 없는 도구다(null)"),
    ('CALL {"tool": "get_x", "args": [1]}', "args 가 JSON 객체가 아니다"),
    ('CALL {"tool": "get_x" "args": {}}', "JSON 으로 읽지 못했다"),
])
def test_실행하지_않은_줄은_까닭과_함께_다음_차례에_알린다(line, why):
    t = _Tool("get_x")
    llm = _Script(line, "조회 불필요")
    _k, calls, _b, _e = _gather(d._TextReactAgent(llm, [t], 3))
    assert t.calls == [] and calls == [] and len(llm.seen) == 2
    assert f"✖ 실행하지 않음 — {line[5:]}: {why}" in llm.seen[1][1]


def test_args_가_없으면_빈_인자로_부른다_줄_앞뒤_공백_백틱은_받고_산문_속_CALL_은_가린다():
    t = _Tool("get_x")
    llm = _Script('먼저 확인한다.\n   CALL {"tool": "get_x"}   \n줄 맨 앞이 아닌 CALL {"tool": "get_x", "args": {"n": 9}}\n'
                  '`CALL {"tool": "get_x", "args": {"n": 8}}`', "조회 요약: 끝")
    _k, calls, _b, _e = _gather(d._TextReactAgent(llm, [t], 3))
    assert t.calls == [{}, {"n": 8}] and len(calls) == 2


def test_도구가_예외를_올리면_오류_글이_결과가_되고_근거로_세지_않는다():
    t, ok = _Tool("get_x", RuntimeError("게이트웨이 거절")), _Tool("get_y", '{"v": 2}')
    llm = _Script(_call("get_x") + "\n" + _call("get_y"), "조회 요약: y 만")
    _k, calls, block, err = _gather(d._TextReactAgent(llm, [t, ok], 3))
    assert err == "" and [n for n, *_x in calls] == ["get_x", "get_y"]
    assert calls[0][2].startswith("Error: RuntimeError('게이트웨이 거절')") and not d._delib_tool_result_ok(calls[0][2])
    assert '{"v": 2}' in block and "게이트웨이 거절" not in block
    assert "결과:\nError: RuntimeError('게이트웨이 거절')" in llm.seen[1][1], "실패도 응답자에게 알린다"


def test_도구_결과가_글이_아니면_글로_바꿔_싣는다():
    t = _Tool("get_x", [{"type": "text", "text": "가"}, {"type": "text", "text": "나"}])
    u = _Tool("get_y", {"v": [1, 2]})
    llm = _Script(_call("get_x") + "\n" + _call("get_y"), "조회 요약: 끝")
    _k, calls, _b, _e = _gather(d._TextReactAgent(llm, [t, u], 3))
    assert [out for _n, _a, out, _c in calls] == ["가나", '{"v": [1, 2]}']


def test_도중에_응답자가_실패하면_받은_조회는_살리고_중단을_알린다():
    t = _Tool("get_x", '{"v": 1}')
    llm = _Script(_call("get_x"), RuntimeError("외부 응답자 한도"))
    _k, calls, block, err = _gather(d._TextReactAgent(llm, [t], 3))
    assert len(calls) == 1 and '{"v": 1}' in block and err == "(조회 도중 중단: RuntimeError)"
    # 첫 차례부터 실패하면 조회는 0건이고 중단이 남는다 — create_react_agent 도 입력 상태(system · human)를 먼저 흘린 뒤 터지므로
    # 같은 글이다(langgraph stream_mode="values" 실측)
    _k, calls, block, err = _gather(d._TextReactAgent(_Script(RuntimeError("없다")), [t], 3))
    assert (calls, block, err) == ([], "", "(조회 도중 중단: RuntimeError)")


def test_같은_차례의_호출은_한꺼번에_실행된다():
    started, release = [], asyncio.Event()

    class _Slow(_Tool):
        async def ainvoke(self, args):
            started.append(args["n"])
            if len(started) == 2:
                release.set()
            await asyncio.wait_for(release.wait(), 2)
            return '{"ok": 1}'

    llm = _Script(_call("get_x", n=1) + "\n" + _call("get_x", n=2), "조회 요약: 끝")
    _k, calls, _b, _e = _gather(d._TextReactAgent(llm, [_Slow("get_x")], 3))
    assert sorted(started) == [1, 2] and len(calls) == 2
    assert all(out == '{"ok": 1}' for _n, _a, out, _c in calls), "한 차례의 호출을 하나씩 기다렸다"
