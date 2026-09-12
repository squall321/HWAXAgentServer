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
