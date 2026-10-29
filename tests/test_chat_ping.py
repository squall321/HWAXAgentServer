# 챗 · 띵킹 · 도우미 스트림의 ping(HWAXPortal docs/request-1029 P-1 ④) — 조용한 동안 흐르고, 턴의 ContextVar 가 걸음마다 이어지고, 끊기면 턴도 멈춘다
"""포털 챗 중계의 침묵 한도를 46,800초에서 900초로 줄이려면(결정 D-5) 먼저 챗 스트림이 조용한 동안에도 바이트가 흘러야 한다 — 생각 · 비스트리밍
LLM 호출 · 긴 도구가 몇 분씩 조용하다. 심의 스트림은 이미 ping 을 낸다(_detach_stream · tests/test_delib_heartbeat.py). 구독자를 실제로 돌려서 본다."""
import asyncio
import contextvars
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as a  # noqa: E402
import delib_jobs  # noqa: E402

_CV = contextvars.ContextVar("turn_cv", default=None)


def _names(chunks):
    return [delib_jobs._parse_sse(c)[0] for c in chunks]


def test_조용한_동안_ping_이_흐르고_프레임은_그대로_온다(monkeypatch):
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.02)

    async def turn():
        yield a._sse("status", {"step": "생각 중"})
        await asyncio.sleep(0.25)                      # 생각 · 비스트리밍 LLM 호출 한 번
        yield a._sse("result", {"type": "text", "content": "답"})
        yield a._sse("done", {})

    async def go():
        return [c async for c in a._pinged_stream(turn())]
    names = _names(asyncio.run(go()))
    assert [n for n in names if n != "ping"] == ["status", "result", "done"]
    assert names.count("ping") >= 2 and set(names[1:names.index("result")]) == {"ping"}, names


def test_턴_안에서_건_ContextVar_가_다음_걸음에도_있다(monkeypatch):
    """_agent_stream 은 이 턴의 문맥 창(_external_llm.ctx_override)을 턴의 태스크에 건다 — 걸음마다 다른 태스크로 돌리면 사라진다."""
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.02)
    seen = []

    async def turn():
        tok = _CV.set("이 턴의 창")
        try:
            yield a._sse("status", {"step": "1"})
            await asyncio.sleep(0.06)
            seen.append(_CV.get())
            yield a._sse("done", {})
        finally:
            _CV.reset(tok)                             # 같은 컨텍스트에서 되돌린다 — 다른 컨텍스트면 ValueError

    async def go():
        return [c async for c in a._pinged_stream(turn())]
    asyncio.run(go())
    assert seen == ["이 턴의 창"]


def test_구독이_끊기면_턴도_멈추고_정리가_돈다(monkeypatch):
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.02)
    state = {"cleaned": False, "after": False}

    async def turn():
        try:
            yield a._sse("status", {"step": "1"})
            await asyncio.sleep(5)
            state["after"] = True
            yield a._sse("done", {})
        finally:
            state["cleaned"] = True

    async def go():
        sub = a._pinged_stream(turn())
        first = await sub.__anext__()
        await sub.aclose()                             # 브라우저가 떠났다 — 포털 중계가 상류를 끊는다
        return first
    assert delib_jobs._parse_sse(asyncio.run(go()))[0] == "status"
    assert state == {"cleaned": True, "after": False}, "끊긴 뒤에도 턴이 돌았다"


def test_턴의_예외는_끝에서_그대로_오른다(monkeypatch):
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.02)

    async def turn():
        yield a._sse("status", {"step": "1"})
        raise RuntimeError("턴 오류")

    async def go():
        got = []
        try:
            async for c in a._pinged_stream(turn()):
                got.append(c)
        except RuntimeError as exc:
            return got, str(exc)
        return got, None
    got, err = asyncio.run(go())
    assert err == "턴 오류" and _names(got) == ["status"]


def test_끄면_생성기를_그대로_돌려준다(monkeypatch):
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)

    async def turn():
        yield a._sse("done", {})
    g = turn()
    assert a._pinged_stream(g) is g


def test_챗_끝점은_심의가_아니면_ping_을_씌우고_심의는_그대로(monkeypatch):
    seen = {}

    def fake_ping(gen):
        seen["wrapped"] = True
        return gen
    monkeypatch.setattr(a, "_pinged_stream", fake_ping)

    async def turn(*_a, **_k):
        yield a._sse("done", {})
    monkeypatch.setattr(a, "_agent_stream", turn)
    asyncio.run(a.chat(a.ChatRequest(message="안녕", groups=[])))
    assert seen.get("wrapped") is True
    seen.clear()
    monkeypatch.setattr(a, "_detach_stream", lambda gen, *_a, **_k: gen)
    monkeypatch.setattr(a, "_delib_with_topic", lambda req: turn())
    monkeypatch.setattr(a, "_denied_feature", lambda req: None)
    monkeypatch.setattr(a, "_delib_pause_info", lambda: None)
    asyncio.run(a.chat(a.ChatRequest(message="/심의 낙하", groups=[])))
    assert "wrapped" not in seen, "심의는 _detach_stream 의 ping 을 쓴다 — 두 번 씌우지 않는다"
