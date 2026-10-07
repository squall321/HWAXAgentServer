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
