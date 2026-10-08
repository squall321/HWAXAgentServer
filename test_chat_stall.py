# 챗 스트림 회귀 — 도구를 부른 뒤 모델이 말없이 끝나는 턴(예고만 남는 중단)이 구제되는지
import asyncio, json, types
import app as A


class _FakeAgent:
    """astream_events: 예고 한 줄 → 도구 호출 → 도구 결과 → 끝(말 없음).
       ainvoke: 구제 호출에 응답을 준다."""
    async def astream_events(self, inputs, version=None, config=None):
        yield {"event": "on_chat_model_stream",
               "data": {"chunk": types.SimpleNamespace(content="먼저 가이드와 템플릿을 확인하겠습니다.")}}
        yield {"event": "on_tool_start", "name": "get_guide", "data": {"input": {"topic": "report"}}}
        yield {"event": "on_tool_end", "name": "get_guide",
               "data": {"output": "가이드 v3: 표지·요약·본문·결론 4장 구성. 템플릿 ID=T-12."}}

    async def ainvoke(self, payload, config=None):
        return {"messages": [types.SimpleNamespace(type="ai", content="가이드는 4장 구성이고 템플릿은 T-12 입니다.")]}


def _collect(req):
    async def _run():
        out = []
        async for chunk in A._agent_stream(A.app, req):
            out.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
        return "".join(out)
    return asyncio.run(_run())


def test_예고만_남은_턴이_구제된다(monkeypatch):
    async def _fake_agent_for(*a, **k):
        return _FakeAgent()
    monkeypatch.setattr(A, "_agent_for", _fake_agent_for)
    req = A.ChatRequest(message="보고서 써줘", groups=["portal-admin"], history=[])
    sse = _collect(req)
    result = [json.loads(l[6:]) for l in sse.splitlines() if l.startswith("data: ") and '"content"' in l]
    final = result[-1]["content"] if result else ""
    print("FINAL:", final)
    assert "확인하겠습니다" in final, "예고는 남아야 한다(이미 화면에 흘러간 글자다)"
    assert "T-12" in final or "4장" in final, "도구 결과로 만든 답이 이어붙어야 한다"


# ── LLM 호출 한도에 걸린 챗 — '연결하지 못했다' 가 아니라 '답이 한도 안에 안 왔다' 고 말한다 ──────────
def _llm_error(cause):
    """openai SDK 가 올리는 모양 그대로 — httpx 예외를 원인으로 문 APITimeoutError."""
    import httpx
    import openai

    req = httpx.Request("POST", "http://llm.invalid/v1/chat/completions")
    try:
        try:
            raise cause("가로챈 요청", request=req)
        except httpx.HTTPError as low:
            raise openai.APITimeoutError(request=req) from low
    except openai.APITimeoutError as exc:
        return exc


class _DeadAgent:
    def __init__(self, exc):
        self.exc = exc

    async def astream_events(self, inputs, version=None, config=None):
        raise self.exc
        yield {}      # noqa — 생성기로 만들려고 둔다

    async def ainvoke(self, payload, config=None):
        raise self.exc


def _error_of(monkeypatch, exc, llm=None):
    async def _fake_agent_for(*a, **k):
        return _DeadAgent(exc)

    monkeypatch.setattr(A, "_agent_for", _fake_agent_for)
    monkeypatch.setenv("CHAT_AUTO_RETRY", "1")          # 한 번만 다시 해 보고 끝낸다(쉬는 시간 없음)
    if llm is not None:
        monkeypatch.setattr(A.app.state, "llm", llm, raising=False)
    sse = _collect(A.ChatRequest(message="안녕", groups=["portal-admin"], history=[]))
    blocks = [b for b in sse.split("\n\n") if b.startswith("event: error")]
    assert len(blocks) == 1, sse
    return json.loads(blocks[0].split("data: ", 1)[1])["message"]


def test_챗_LLM_시간_초과는_한도와_설정_이름을_말한다(monkeypatch):
    """심의가 공유 LLM 을 차지한 동안 챗이 이렇게 끝난다 — 종전 문구는 'LLM 서버에 연결하지 못했습니다' 라
    서버가 내려간 줄 알았다."""
    import httpx

    llm = types.SimpleNamespace(request_timeout=httpx.Timeout(900.0, connect=10.0), max_retries=2)
    msg = _error_of(monkeypatch, _llm_error(httpx.ReadTimeout), llm)
    assert "LLM 응답이 900초 안에 오지 않았습니다(LLM_TIMEOUT_S · 3회 시도)" in msg, msg
    assert "연결하지 못했습니다" not in msg and "질문을 바꿔도 해결되지 않습니다" in msg, msg


def test_챗_LLM_연결_실패는_종전_문구_그대로다(monkeypatch):
    import httpx

    msg = _error_of(monkeypatch, _llm_error(httpx.ConnectTimeout))
    assert "LLM 서버에 연결하지 못했습니다" in msg and "LLM_TIMEOUT_S" not in msg, msg


# ── 스트리밍 청크 침묵 한도 — 걸렸으면 '내부 오류' 가 아니라 그 한도와 설정 이름을 말한다 ──────────────
# 스트리밍 박스에서는 이 한도(300초)가 LLM_TIMEOUT_S(900초)보다 먼저 걸린다 — 심의가 공유 LLM 을 차지한 동안
# 챗의 첫 토큰이 그만큼 늦다. langchain-openai 가 올리는 StreamChunkTimeoutError 는 openai SDK 예외가 아니라
# 위의 'APITimeout'·'Connection' 가름 어디에도 안 걸렸다. 화면에는 '처리 중 내부 오류' 나 '에이전트 처리 중
# 오류' 가 떴고, 사용자는 같은 질문을 곧바로 다시 보내 밀린 LLM 에 한 건을 더 얹었다.
_CHUNK_SAYS = "LLM 이 300초 동안 토큰을 보내지 않았습니다(LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S)"


def _chunk_timeout(chunks_received=0):
    """라이브러리가 올리는 모양 그대로 — asyncio.TimeoutError 를 원인으로 문 StreamChunkTimeoutError."""
    from langchain_openai import StreamChunkTimeoutError

    try:
        try:
            raise asyncio.TimeoutError()
        except asyncio.TimeoutError as low:
            raise StreamChunkTimeoutError(300.0, model_name="m", chunks_received=chunks_received) from low
    except StreamChunkTimeoutError as exc:
        return exc


class _StallAfterPreface:
    """예고 한 줄과 도구 호출 뒤에 터진다 — ReAct 턴의 두 번째 LLM 호출이 줄을 서다 첫 토큰이 한도를 넘긴 모양."""

    def __init__(self, exc):
        self.exc = exc

    async def astream_events(self, inputs, version=None, config=None):
        yield {"event": "on_chat_model_stream",
               "data": {"chunk": types.SimpleNamespace(content="먼저 가이드를 확인하겠습니다.")}}
        yield {"event": "on_tool_start", "name": "get_guide", "data": {"input": {"topic": "report"}}}
        yield {"event": "on_tool_end", "name": "get_guide", "data": {"output": "가이드 v3"}}
        raise self.exc

    async def ainvoke(self, payload, config=None):
        raise AssertionError("부분 응답이 있으면 자동 재시도를 하지 않는다")


def _chat(monkeypatch, agent, **env):
    """그 에이전트로 챗 한 턴을 돌려 (마지막 result 본문, 상태줄들, error 문구들)을 돌려준다."""
    async def _fake_agent_for(*a, **k):
        return agent

    monkeypatch.setattr(A, "_agent_for", _fake_agent_for)
    monkeypatch.delenv("AGENT_DEBUG_ERRORS", raising=False)     # 켜 둔 박스에서는 예외 원문이 문구에 붙는다
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sse = _collect(A.ChatRequest(message="보고서 써줘", groups=["portal-admin"], history=[]))
    got = {"result": [], "status": [], "error": []}
    for block in sse.split("\n\n"):
        if block.startswith("event: ") and "\ndata: " in block:
            name, data = block[len("event: "):].split("\ndata: ", 1)
            if name in got:
                got[name].append(json.loads(data))
    return ((got["result"][-1]["content"] if got["result"] else ""),
            [s.get("step", "") for s in got["status"]], [e["message"] for e in got["error"]])


def test_청크_한도에_걸린_부분_응답은_한도와_설정_이름을_말한다(monkeypatch):
    final, _steps, errors = _chat(monkeypatch, _StallAfterPreface(_chunk_timeout(3)))
    assert final.startswith("먼저 가이드를 확인하겠습니다."), "이미 화면에 흘러간 글자는 남는다"
    assert _CHUNK_SAYS in final and "질문을 바꿔도 해결되지 않습니다" in final, final
    assert "내부 오류" not in final, final
    assert not errors, "부분 응답이 있으면 오류 이벤트가 아니라 본문 끝의 알림으로 끝난다"
    # 끊긴 턴 뒤의 재촉('계속')이 원래 질문을 되살리는 판정은 이 문구를 본다 — 바뀐 알림도 끊긴 턴으로 읽혀야 한다.
    assert A._INTERRUPTED_RE.search(final), final


def test_청크_한도에_걸리고_재시도를_끈_챗은_한도와_설정_이름을_말한다(monkeypatch):
    final, _steps, errors = _chat(monkeypatch, _DeadAgent(_chunk_timeout()), CHAT_AUTO_RETRY="0")
    assert not final and len(errors) == 1, (final, errors)
    assert _CHUNK_SAYS in errors[0] and "질문을 바꿔도 해결되지 않습니다" in errors[0], errors
    assert "에이전트 처리 중 오류" not in errors[0] and "연결하지 못했습니다" not in errors[0], errors


def test_청크_한도에_걸려_다시_받는_동안에도_그_한도를_말한다(monkeypatch):
    """기본 설정에서는 부분 응답이 없으면 스트리밍 없이 다시 받는다 — 길게는 LLM_TIMEOUT_S × 시도 횟수를 기다리는데
    그 동안 화면에 뜨는 것은 이 상태줄뿐이다. '오류 발생' 만으로는 무엇에 걸렸는지 알 수 없었다."""
    class _StreamStalls(_DeadAgent):
        async def ainvoke(self, payload, config=None):
            return {"messages": [types.SimpleNamespace(type="ai", content="가이드는 4장 구성입니다.")]}

    final, steps, errors = _chat(monkeypatch, _StreamStalls(_chunk_timeout()), CHAT_AUTO_RETRY="2")
    assert final == "가이드는 4장 구성입니다." and not errors, (final, errors)
    retry = [s for s in steps if "자동 재시도" in s]
    assert len(retry) == 1 and _CHUNK_SAYS in retry[0] and "자동 재시도 1/2" in retry[0], steps


def test_다른_오류의_문구는_그대로다(monkeypatch):
    """청크 한도가 아닌 실패에 그 한도를 갖다 붙이지 않는다."""
    final, _steps, _errors = _chat(monkeypatch, _StallAfterPreface(ValueError("파서 오류")))
    assert "처리 중 내부 오류로 응답이 여기서 중단되었습니다. 같은 질문을 다시 보내면 재시도합니다." in final, final
    assert "LANGCHAIN_OPENAI" not in final, final
    final, steps, errors = _chat(monkeypatch, _DeadAgent(ValueError("파서 오류")), CHAT_AUTO_RETRY="1")
    assert "오류 발생 — 자동 재시도 1/1" in steps, steps
    assert errors == ["에이전트 처리 중 오류 (자동 재시도 1회 모두 실패)"], errors
