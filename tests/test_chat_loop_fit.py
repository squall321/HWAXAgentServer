# 챗 ReAct 루프가 한 턴에 큰 도구 결과를 여럿 받아도 모델 창을 넘기지 않는다 — 넘으면 오래된 결과부터 줄이고 알린다
#
# m1 2차 AF1-loop-room-single — 운영 모델은 창 1,048,576 토큰(GLM-5.2)이고 사용자 지침은 '자르지 않는다' 다. e55bd69 는 이력 몫을
# 나누기 전에 도구 결과 **한 건** 분(TOOL_RESULT_MAX 200,000자 최악 환산 ≈ 190K 토큰)만 떼어, 이력이 찬 긴 대화에서 큰 조회를
# 두 번 하면(또는 12만 자급 세 번) 루프 안에서 창을 넘어 400 으로 턴이 통째로 실패했다. 자동 재시도 둘도 같은 이력으로 같은 400
# 이었다. 모델 호출 전 훅이 쌓인 메시지를 재어, 창 − 예비분을 넘으면 오래된 도구 결과부터 원문 길이 표식을 달고 줄인다.
#
#   실행:  .venv/bin/python -m pytest tests/test_chat_loop_fit.py -q
import asyncio
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from pydantic import Field  # noqa: E402

import app  # noqa: E402
import deliberation as d  # noqa: E402

_BIG = 1_048_576


@pytest.fixture(autouse=True)
def _win():
    saved = dict(app._ctx_cache)
    app._ctx_cache["n"] = _BIG
    d._evid_cache.clear()
    yield
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)
    d._evid_cache.clear()


def _tok(text: str) -> int:
    """시험용 토큰 수 — 엔진 환산(한글 1.05자/토큰·그 밖 3.6자/토큰)을 글 전체에 독립으로 적용한다."""
    ko = sum(1 for ch in text if "가" <= ch <= "힯")
    return int(ko / 1.05 + (len(text) - ko) / 3.6)


def _msgs_tok(msgs) -> int:
    total = 0
    for m in msgs:
        c = m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False)
        total += _tok(c) + _tok(json.dumps(getattr(m, "tool_calls", None) or [], ensure_ascii=False))
    return total


class _ScriptLLM(BaseChatModel):
    """대본대로 답하는 모델 — 첫 호출은 big_a, 둘째는 big_b 를 부르고 셋째에 답한다. 받은 메시지를 호출마다 적는다."""

    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "script"

    def bind_tools(self, tools, **_kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **_kw):
        self.seen.append(list(messages))
        n = len(self.seen)
        if n <= 2:
            name = "big_a" if n == 1 else "big_b"
            msg = AIMessage(content="", tool_calls=[{"name": name, "args": {"q": name}, "id": f"c{n}"}])
        else:
            msg = AIMessage(content="두 결과를 다 봤다.")
        return ChatResult(generations=[ChatGeneration(message=msg)])


_RESULT_A = "가" * (app.TOOL_RESULT_MAX - 10) + "[결과A-끝]"
_RESULT_B = "나" * (app.TOOL_RESULT_MAX - 10) + "[결과B-끝]"


@tool
def big_a(q: str) -> str:
    """큰 조회 A"""
    return _RESULT_A


@tool
def big_b(q: str) -> str:
    """큰 조회 B"""
    return _RESULT_B


def _agent(monkeypatch, llm):
    """앱이 실제로 만드는 길(_agent_for)로 에이전트를 만든다 — 게이트웨이 클라이언트만 스텁이다(test_app 의 캐시 시험과 같다)."""
    class _StubClient:
        def __init__(self, _connections):
            pass

        async def get_tools(self):
            return [big_a, big_b]

    monkeypatch.setattr(app, "MultiServerMCPClient", _StubClient)
    state = types.SimpleNamespace(
        llm=llm, connections={"gateway": {"url": "http://gw/mcp", "headers": {"Authorization": "Bearer x"}}},
        agent_cache={}, tool_degraded={}, tool_load_error={}, tool_snapshot={})
    return asyncio.run(app._agent_for(types.SimpleNamespace(state=state), ["admin"]))


def _long_history():
    """이력 몫을 다 채운 긴 대화 — 챗 턴이 실제로 싣는 이력(_history_messages)이다."""
    hist = [{"role": "user" if k % 2 == 0 else "assistant", "content": f"<{k}>" + "다" * 20_000} for k in range(60)]
    got = app._history_messages(hist)
    assert _tok("".join(c for _, c in got)) > app._hist_budget_tokens() * 0.97, "시험 전제 — 이력 몫을 다 채우지 못했다"
    return got


def _run(agent, history):
    msgs = [("system", "너는 사내 엔지니어링 도우미다."), *history, ("user", "A 와 B 를 다 조회해서 비교해 줘")]

    async def go():
        return [ev async for ev in agent.astream_events({"messages": msgs}, version="v2",
                                                          config={"recursion_limit": 25})]

    return asyncio.run(go())


def test_한_턴에_큰_결과_둘을_받아도_모델에_가는_메시지는_창_안이다(monkeypatch):
    llm = _ScriptLLM()
    agent = _agent(monkeypatch, llm)
    history = _long_history()
    limit = app._model_context_tokens() - app.DOC_RESERVE_TOKENS
    # 시험 전제 — 줄이지 않으면 셋째 호출이 창을 넘는다(이력 몫 + 결과 둘).
    raw_third = _tok("".join(c for _, c in history)) + _tok(_RESULT_A) + _tok(_RESULT_B)
    assert raw_third > limit, (raw_third, limit)

    events = _run(agent, history)
    assert len(llm.seen) == 3, len(llm.seen)
    for k, got in enumerate(llm.seen, 1):
        assert _msgs_tok(got) <= limit, (k, _msgs_tok(got), limit)
    # 오래된 결과(A)부터 줄이고 원문 길이를 적는다. 가장 최근 결과(B)는 온전하다.
    tools = [m for m in llm.seen[2] if isinstance(m, ToolMessage)]
    assert [t.tool_call_id for t in tools] == ["c1", "c2"], tools
    assert tools[0].content.startswith("가" * 1000) and "[결과A-끝]" not in tools[0].content
    assert f"{len(_RESULT_A):,}자 중 앞" in tools[0].content, tools[0].content[-120:]
    assert tools[1].content.endswith("[결과B-끝]"), tools[1].content[-40:]
    # 줄였다는 사실이 스트림에 나온다(챗이 경고로 바꾼다).
    fit = [e for e in events if e["event"] == "on_custom_event" and e["name"] == "hwax_loop_fit"]
    assert fit and fit[-1]["data"]["n"] == 1, fit


def test_창이_남으면_도구_결과를_건드리지_않는다(monkeypatch):
    llm = _ScriptLLM()
    agent = _agent(monkeypatch, llm)
    events = _run(agent, [])
    tools = [m for m in llm.seen[2] if isinstance(m, ToolMessage)]
    assert [t.content for t in tools] == [_RESULT_A, _RESULT_B]
    assert not [e for e in events if e["event"] == "on_custom_event" and e["name"] == "hwax_loop_fit"]


class _FitAgent:
    """루프 훅이 결과를 줄였다는 사건을 내는 에이전트 — 챗 스트림이 그것을 경고로 바꾸는지 본다."""

    async def astream_events(self, inputs, version=None, config=None):
        yield {"event": "on_custom_event", "name": "hwax_loop_fit",
               "data": {"n": 1, "chars": 200_000, "before": 1_060_000, "after": 990_000, "limit": 992_576}}
        yield {"event": "on_custom_event", "name": "hwax_loop_fit",
               "data": {"n": 1, "chars": 200_000, "before": 1_060_000, "after": 990_000, "limit": 992_576}}
        yield {"event": "on_chat_model_stream", "data": {"chunk": types.SimpleNamespace(content="비교했다.")}}

    async def ainvoke(self, payload, config=None):
        return {"messages": [types.SimpleNamespace(type="ai", content="비교했다.")]}


def test_줄였으면_챗이_한_번_경고로_알린다(monkeypatch):
    async def _fake_agent_for(*_a, **_k):
        return _FitAgent()

    monkeypatch.setattr(app, "_agent_for", _fake_agent_for)
    req = app.ChatRequest(message="비교해 줘", groups=["portal-admin"], history=[])

    async def run():
        return [c.decode() if isinstance(c, bytes) else str(c) async for c in app._agent_stream(app.app, req)]

    warns = [json.loads(c.split("data: ", 1)[1]) for c in asyncio.run(run()) if c.startswith("event: warning\n")]
    cut = [w for w in warns if w.get("code") == "loop_results_cut"]
    assert len(cut) == 1, warns
    assert "1건" in cut[0]["message"] and "앞부분" in cut[0]["message"], cut[0]


def test_재시도_길_ainvoke_도_같은_훅으로_창_안에서_돈다(monkeypatch):
    """챗의 자동 재시도·강제 재시도는 같은 에이전트를 스트림 없이 부른다(ainvoke) — 사건을 받을 곳이 없어도 턴이 죽지 않고
    같은 몫으로 줄인다."""
    llm = _ScriptLLM()
    agent = _agent(monkeypatch, llm)
    msgs = [("system", "너는 사내 엔지니어링 도우미다."), *_long_history(), ("user", "A 와 B 를 다 조회해서 비교해 줘")]
    out = asyncio.run(agent.ainvoke({"messages": msgs}, config={"recursion_limit": 25}))
    assert out["messages"][-1].content == "두 결과를 다 봤다."
    limit = app._model_context_tokens() - app.DOC_RESERVE_TOKENS
    assert len(llm.seen) == 3 and all(_msgs_tok(got) <= limit for got in llm.seen), [_msgs_tok(g) for g in llm.seen]
    # 대화 상태(돌려받는 메시지)는 원문 그대로다 — 모델에 가는 글만 줄였다.
    assert [m.content for m in out["messages"] if isinstance(m, ToolMessage)] == [_RESULT_A, _RESULT_B]
