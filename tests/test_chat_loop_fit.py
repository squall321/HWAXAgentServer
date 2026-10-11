# 챗 ReAct 루프가 한 턴에 큰 도구 결과를 여럿 받아도 모델 창을 넘기지 않는다 — 넘으면 오래된 결과부터 줄이고 알린다
#
# m1 2차 AF1-loop-room-single — 운영 모델은 창 1,048,576 토큰(GLM-5.2)이고 사용자 지침은 '자르지 않는다' 다. e55bd69 는 이력 몫을
# 나누기 전에 도구 결과 **한 건** 분(TOOL_RESULT_MAX 200,000자 최악 환산 ≈ 190K 토큰)만 떼어, 이력이 찬 긴 대화에서 큰 조회를
# 두 번 하면(또는 12만 자급 세 번) 루프 안에서 창을 넘어 400 으로 턴이 통째로 실패했다. 자동 재시도 둘도 같은 이력으로 같은 400
# 이었다. 모델 호출 직전(agent 노드의 prompt)에 쌓인 메시지를 재어, 창 − 예비분을 넘으면 오래된 도구 결과부터 원문 길이 표식을
# 달고 줄인다. 그 맞추기가 그래프 단계를 더 쓰지 않는지(연속 도구 왕복 수)도 본다(m1 3차 발견 1, 파일 아래쪽).
#
#   실행:  .venv/bin/python -m pytest tests/test_chat_loop_fit.py -q
import asyncio
import json
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult  # noqa: E402
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


def _agent(monkeypatch, llm, tools=(big_a, big_b)):
    """앱이 실제로 만드는 길(_agent_for)로 에이전트를 만든다 — 게이트웨이 클라이언트만 스텁이다(test_app 의 캐시 시험과 같다)."""
    class _StubClient:
        def __init__(self, _connections):
            pass

        async def get_tools(self):
            return list(tools)

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


# ── 창 맞추기가 그래프 한 단계를 더 쓰지 않는다 — 연속 도구 왕복 수 (m1 3차 발견 1) ──────────────────────────────────────────
# b27d414 는 창 맞추기를 pre_model_hook 으로 달았다. langgraph 1.2.5 는 그 훅을 **별도 노드**로 넣어 왕복마다 superstep 을 하나
# 더 써서, AGENT_RECURSION_LIMIT=80 에서 한 턴의 연속 도구 왕복이 39회 → 25회로 줄고 26번째에 GraphRecursionError 가 났다. 앱은
# 그 오류를 일반 예외로 받아 CHAT_AUTO_RETRY(2회)로 같은 한도에서 처음부터 다시 불러, 도구 26개를 두 번 더 실행하고(부작용 있는
# 도구도) '내부 오류' 로 끝났다. 훅 없이는 39회가 끝까지 돌고 40회째는 langgraph 가 'need more steps' 로 얌전히 끝낸다.

_STEPS: list = []


@tool
def step(q: str) -> str:
    """연속 조회 한 번 — 실행 횟수를 센다(부작용 있는 도구의 대역)."""
    _STEPS.append(q)
    return f"조회 {len(_STEPS)}회째 결과"


_RESCUE_SYS = "조회 결과를 사용자에게 설명하는 역할"     # 빈 응답 구제(요약만 시킨다)의 시스템 글 머리
# 오류 턴의 자동 재시도(CHAT_AUTO_RETRY — 같은 질문을 처음부터 다시 돌린다)가 내는 상태 글. 빈 응답 구제의 '도구 결과 뒤 응답이
# 없어 자동 재시도' 는 도구 없이 요약만 시키는 다른 길이라 세지 않는다(그 길이 도구를 다시 돌리면 실행 수 단언이 잡는다).
_AUTO_RETRY = re.compile(r"자동 재시도 \d+/\d+")


class _LoopLLM(BaseChatModel):
    """도구 step 을 n 번 연달아 부르고 답하는 모델 — 받은 메시지의 도구 결과 수로 몇 번째인지 안다(처음부터 다시 부르면 다시
    센다). 빈 응답 구제의 요약 요청에는 도구 없이 답한다."""

    n: int = 39

    @property
    def _llm_type(self) -> str:
        return "loop-script"

    def bind_tools(self, tools, **_kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **_kw):
        if messages and _RESCUE_SYS in str(messages[0].content):
            msg = AIMessage(content="받은 결과로 요약했다.")
        else:
            done = sum(isinstance(m, ToolMessage) for m in messages)
            msg = (AIMessage(content="", tool_calls=[{"name": "step", "args": {"q": "다음"}, "id": f"s{done + 1}"}])
                   if done < self.n else AIMessage(content=f"연속 {done}회 조회를 마쳤다."))
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(self, messages, stop=None, run_manager=None, **kw):
        """챗 스트림 길(on_chat_model_stream)로도 같은 답을 한 조각으로 낸다 — 운영 기본이 스트리밍이다."""
        msg = self._generate(messages, stop, run_manager, **kw).generations[0].message
        chunk = AIMessageChunk(content=msg.content, tool_call_chunks=[
            {"name": c["name"], "args": json.dumps(c["args"], ensure_ascii=False), "id": c["id"], "index": i}
            for i, c in enumerate(msg.tool_calls)])
        yield ChatGenerationChunk(message=chunk)


def _chat(monkeypatch, n):
    """실제 챗 턴(_agent_stream)을 돌린다 — 에이전트는 앱이 만드는 길(_agent_for)로 만든 것을 그대로 쓴다. (SSE 사건 목록, 도구 실행 수)."""
    monkeypatch.setattr(app, "AGENT_RECURSION_LIMIT", 80)        # 운영 기본값
    _STEPS.clear()
    agent = _agent(monkeypatch, _LoopLLM(n=n), tools=(step,))

    async def _built(*_a, **_k):
        return agent

    monkeypatch.setattr(app, "_agent_for", _built)
    monkeypatch.setenv("CHAT_AUTO_RETRY", "2")
    req = app.ChatRequest(message="연속으로 조회해 줘", groups=["portal-admin"], history=[])

    async def run():
        return [c.decode() if isinstance(c, bytes) else str(c) async for c in app._agent_stream(app.app, req)]

    evs = []
    for c in asyncio.run(run()):
        head, _, body = c.partition("\ndata: ")
        evs.append((head.removeprefix("event: "), json.loads(body)))
    return evs, len(_STEPS)


def test_재귀_한도_80에서_연속_도구_왕복_39회가_끝까지_돈다(monkeypatch):
    evs, ran = _chat(monkeypatch, 39)
    assert ran == 39, ran
    assert not [b for k, b in evs if k == "error"], evs
    assert not [b for k, b in evs if k == "status" and _AUTO_RETRY.search(str(b.get("step")))], evs
    result = [b for k, b in evs if k == "result"]
    assert result and "연속 39회 조회를 마쳤다." in result[-1]["content"], result


def test_재귀_한도_80에서_40회째는_재시도_없이_정상_종료한다(monkeypatch):
    evs, ran = _chat(monkeypatch, 40)
    # 40번째 호출은 langgraph 가 'need more steps' 로 막는다 — 앞 39회만 돌고, 처음부터 다시 돌리지 않는다.
    assert ran == 39, ran
    assert not [b for k, b in evs if k == "error"], evs
    assert not [b for k, b in evs if k == "status" and _AUTO_RETRY.search(str(b.get("step")))], evs
    assert [k for k, _ in evs][-2:] == ["result", "done"], evs[-3:]


def test_에이전트_직접_호출도_39회가_끝까지_돈다(monkeypatch):
    """재시도·강제 호출 길(ainvoke)도 같은 그래프다 — 왕복 39회가 같은 한도 안에서 끝난다."""
    _STEPS.clear()
    agent = _agent(monkeypatch, _LoopLLM(n=39), tools=(step,))
    out = asyncio.run(agent.ainvoke({"messages": [("user", "연속으로 조회해 줘")]}, config={"recursion_limit": 80}))
    assert out["messages"][-1].content == "연속 39회 조회를 마쳤다.", out["messages"][-1]
    assert len(_STEPS) == 39


class _RecursionAgent:
    """그래프 재귀 한도에 걸리는 에이전트 — 스트림 길은 GraphRecursionError 로 끝나고, 다시 부르면 센다."""

    def __init__(self, partial: str = ""):
        self.partial = partial
        self.again = 0

    async def astream_events(self, inputs, version=None, config=None):
        from langgraph.errors import GraphRecursionError
        if self.partial:
            yield {"event": "on_chat_model_stream", "data": {"chunk": types.SimpleNamespace(content=self.partial)}}
        raise GraphRecursionError("Recursion limit of 80 reached without hitting a stop condition.")

    async def ainvoke(self, payload, config=None):
        self.again += 1
        return {"messages": [types.SimpleNamespace(type="ai", content="다시 돌린 답")]}


@pytest.mark.parametrize("partial", ["", "앞부분 답이다. "])
def test_그래프_재귀_한도에_걸리면_자동_재시도하지_않고_도구_왕복_상한을_알린다(monkeypatch, partial):
    """같은 한도로 다시 부르면 같은 도구를 처음부터 다시 실행하고(부작용 있는 도구도) 같은 자리에서 또 걸린다."""
    fake = _RecursionAgent(partial)

    async def _fake_agent_for(*_a, **_k):
        return fake

    monkeypatch.setattr(app, "_agent_for", _fake_agent_for)
    monkeypatch.setenv("CHAT_AUTO_RETRY", "2")
    req = app.ChatRequest(message="연속으로 조회해 줘", groups=["portal-admin"], history=[])

    async def run():
        return [c.decode() if isinstance(c, bytes) else str(c) async for c in app._agent_stream(app.app, req)]

    evs = []
    for c in asyncio.run(run()):
        head, _, body = c.partition("\ndata: ")
        evs.append((head.removeprefix("event: "), json.loads(body)))
    assert fake.again == 0, "같은 한도로 처음부터 다시 불렀다"
    assert not [b for k, b in evs if k == "status" and _AUTO_RETRY.search(str(b.get("step")))], evs
    said = " ".join(str(b.get("message") or b.get("content") or b.get("delta") or "")
                    for k, b in evs if k in ("error", "result", "token"))
    assert "도구 왕복 상한" in said and "AGENT_RECURSION_LIMIT" in said, said
    # 끊긴 턴 표지(_INTERRUPTED_RE)를 달지 않는다 — '계속' 재촉이 원래 질문을 처음부터 다시 돌려 같은 도구를 또 실행한다.
    assert not app._INTERRUPTED_RE.search(said), said
    if partial:
        assert [b for k, b in evs if k == "result"][-1]["content"].startswith(partial.strip()), evs
