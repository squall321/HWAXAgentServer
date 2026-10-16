# 챗 전용 LLM(CHAT_LLM_*) — 챗만 다른 서버로 보내고, 그 서버가 죽으면 호출 단위로 VLLM_* 로 넘기는지(HWAXPortal docs/chat-llm-stcx)
"""왜 — cae00 에 stcx GPU LLM(Qwen3.8-Flash-Next 10벌, 127.0.0.1:18700)이 생겼다. 사용자 결정(10-17): 챗은 그 서버, 띵킹 · 심의는 GLM,
그 서버가 죽으면 챗은 GLM 으로 넘기고, 챗에서는 Qwen 의 생각 모드를 끈다. VLLM_BASE_URL 을 바꾸면 챗 · 심의 클라이언트가 함께 바뀌므로
챗 전용 클라이언트를 따로 둔다.

  · 비면 챗도 VLLM_* 클라이언트 그대로(바뀌는 것 없음). 채우면 챗만 그 서버 · 생각 끔 · 심의 · 띵킹 · 심의 준비는 VLLM_*.
  · 넘김은 LLM 호출 단위 — 연결 실패 · 5xx 만. 400 은 넘기지 않는다.
  · 챗 서버는 스트리밍인데 넘겨받은 VLLM_* 는 비스트리밍이어도 그 답의 글이 화면에 나간다(호출마다 본다).
  · 챗 턴의 문맥 예산은 두 서버 창 중 작은 값 — 턴이 끝나면 되돌린다.
  · /health 의 chat_llm 은 키를 싣지 않는다.

기동 절차(app.lifespan)를 실제로 열어 만든 클라이언트를 본다(test_llm_limits 와 같은 방식). LLM 은 가짜 서버(tests/fakes/llm_server.py)다.
"""
import asyncio
import json
import signal
import socket
import subprocess
import sys
import time
import types
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import openai  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from langchain_core.runnables import RunnableWithFallbacks  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402
import review_gate as rg  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
_KNOBS = ("CHAT_LLM_BASE_URL", "CHAT_LLM_MODEL", "CHAT_LLM_API_KEY", "CHAT_LLM_ENABLE_THINKING", "CHAT_LLM_DISABLE_STREAMING",
          "CHAT_LLM_MAX_RETRIES", "CHAT_LLM_FALLBACK", "CHAT_LLM_CONTEXT_TOKENS", "LLM_DISABLE_STREAMING",
          "LLM_REASONING_EFFORT", "LLM_TIMEOUT_S", "LLM_CONNECT_TIMEOUT_S", "LLM_MAX_RETRIES")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port != 9009
    return port


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    """박스의 프록시 설정이 가짜 서버로 가는 요청을 가로채지 않게."""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def fake_server(tmp_path):
    """가짜 OpenAI 호환 서버를 빈 포트에 띄운다 — start(model=, ctx=, script=) → 'http://127.0.0.1:<포트>'."""
    procs = []

    def start(model="fake-glm", ctx=1_048_576, script="oracle"):
        port, log = _free_port(), tmp_path / f"fake-{len(procs)}.log"
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
        with open(log, "w") as out:
            p = subprocess.Popen([PY, str(ROOT / "tests" / "fakes" / "llm_server.py"), "--port", str(port), "--ctx",
                                  str(ctx), "--model", model, "--script", script],
                                 cwd=str(tmp_path), env=env, stdout=out, stderr=subprocess.STDOUT)
        procs.append(p)
        base = f"http://127.0.0.1:{port}"
        t0 = time.monotonic()
        with httpx.Client(timeout=2, trust_env=False) as c:
            while True:
                try:
                    if c.get(f"{base}/health").status_code == 200:
                        return base
                except httpx.HTTPError:
                    pass
                if p.poll() is not None or time.monotonic() - t0 > 60:
                    pytest.fail(f"가짜 LLM 서버가 뜨지 않았다 — {log.read_text()[-1500:]}")
                time.sleep(0.1)

    yield start
    for p in procs:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=10)


def _calls(base: str) -> int:
    with httpx.Client(timeout=5, trust_env=False) as c:
        return c.get(f"{base}/_stats").json()["calls"]


def _glm(monkeypatch, base: str) -> None:
    """넘겨받는 쪽(VLLM_*)을 가짜 GLM 으로 — VLLM_BASE_URL · VLLM_MODEL 은 들여올 때 읽는 모듈 상수라 환경 변수가 아니라 속성을 바꾼다."""
    monkeypatch.setattr(a, "VLLM_BASE_URL", f"{base}/v1")
    monkeypatch.setattr(a, "VLLM_MODEL", "fake-glm")


def _boot(monkeypatch, **env):
    """기동 절차를 열었다 닫고 app.state 를 돌려준다(test_llm_limits._boot 와 같다). 챗 손잡이는 시험이 준 것만 건다."""
    @asynccontextmanager
    async def _no_session_manager(_app):
        yield

    for k in _KNOBS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setattr(a, "_load_mcp_config", dict)
    monkeypatch.setattr(a._DELIB_MCP.router, "lifespan_context", _no_session_manager)
    api = FastAPI()

    async def go():
        async with a.lifespan(api):
            pass

    try:
        asyncio.run(go())
    finally:
        delib_jobs.closing(False)
    return api.state


def test_비면_챗도_VLLM_클라이언트다(monkeypatch):
    st = _boot(monkeypatch)
    assert st.chat_llm is st.llm, "설정이 없는 박스는 바뀌는 것이 없다"
    assert st.chat_llm_info == {"enabled": False} and st.chat_llm_cfg is None


def test_채우면_챗만_그_서버이고_생각을_끄며_심의는_그대로다(monkeypatch):
    st = _boot(monkeypatch, CHAT_LLM_BASE_URL="http://127.0.0.1:1/v1/", CHAT_LLM_MODEL="qwen3.8-flash-next",
               LLM_REASONING_EFFORT="high")
    assert isinstance(st.chat_llm, RunnableWithFallbacks)
    primary, (fallback,) = st.chat_llm.runnable, st.chat_llm.fallbacks
    assert primary.model_name == "qwen3.8-flash-next" and primary.openai_api_base == "http://127.0.0.1:1/v1"
    assert primary.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}, \
        "생각 끔 · GLM 의 reasoning_effort 는 챗 서버에 보내지 않는다"
    assert fallback.model_name == a.VLLM_MODEL and fallback.openai_api_base == a.VLLM_BASE_URL
    assert fallback.extra_body == {"chat_template_kwargs": {"reasoning_effort": "high"}}, "넘겨받는 쪽은 VLLM_* 설정 그대로"
    assert st.llm.model_name == a.VLLM_MODEL and st.delib_llm.model_name == a.VLLM_MODEL, "띵킹 · 심의 준비 · 심의는 그대로"
    assert st.chat_llm_info["enabled"] and st.chat_llm_info["fallback"] == {"enabled": True, "to": a.VLLM_MODEL,
                                                                           "count": 0, "last_at": None}
    st = _boot(monkeypatch, CHAT_LLM_BASE_URL="http://127.0.0.1:1/v1", CHAT_LLM_MODEL="q", CHAT_LLM_ENABLE_THINKING="1")
    assert st.chat_llm.runnable.extra_body is None, "켜면 모델 기본값(아무것도 보내지 않는다)"
    st = _boot(monkeypatch, CHAT_LLM_BASE_URL="http://127.0.0.1:1/v1", CHAT_LLM_MODEL="q", CHAT_LLM_FALLBACK="0")
    assert isinstance(st.chat_llm, ChatOpenAI), "넘김을 끄면 챗 서버 하나"
    st = _boot(monkeypatch, CHAT_LLM_BASE_URL="http://127.0.0.1:1/v1")
    assert st.chat_llm is st.llm and "CHAT_LLM_MODEL" in st.chat_llm_info["error"], "모델이 없으면 켜지 않고 그 사실을 남긴다"


def test_스트리밍은_비우면_LLM_DISABLE_STREAMING_을_따르고_넘겨받는_쪽은_제_설정이다(monkeypatch):
    st = _boot(monkeypatch, LLM_DISABLE_STREAMING="1", CHAT_LLM_BASE_URL="http://127.0.0.1:1/v1", CHAT_LLM_MODEL="q")
    assert st.chat_llm.runnable.disable_streaming is True and st.llm_nostream is True
    st = _boot(monkeypatch, LLM_DISABLE_STREAMING="1", CHAT_LLM_BASE_URL="http://127.0.0.1:1/v1", CHAT_LLM_MODEL="q",
               CHAT_LLM_DISABLE_STREAMING="0")
    assert st.chat_llm.runnable.disable_streaming is False and st.llm_nostream is False, "챗 경로는 챗 서버의 값을 본다"
    assert st.chat_llm.fallbacks[0].disable_streaming is True, "GLM 우회(비스트리밍)는 넘겨받는 쪽에 그대로 남는다"


def test_챗_서버가_닫혀_있으면_호출을_VLLM_으로_넘기고_센다(monkeypatch, fake_server):
    glm = fake_server(model="fake-glm")
    _glm(monkeypatch, glm)
    st = _boot(monkeypatch, CHAT_LLM_BASE_URL=f"http://127.0.0.1:{_free_port()}/v1",
               CHAT_LLM_MODEL="qwen3.8-flash-next", CHAT_LLM_MAX_RETRIES="0", LLM_CONNECT_TIMEOUT_S="3")
    out = asyncio.run(st.chat_llm.ainvoke([("human", "안녕")]))
    assert out.content and _calls(glm) == 1, "닫힌 챗 서버 대신 VLLM_* 가 답했다"
    assert st.chat_llm_info["fallback"]["count"] == 1 and st.chat_llm_info["fallback"]["last_at"]
    assert st.chat_llm_info["last_error"].startswith("APIConnectionError")


def test_5xx_는_넘기고_400_은_넘기지_않는다(monkeypatch, fake_server):
    glm = fake_server(model="fake-glm")
    _glm(monkeypatch, glm)
    for script, falls in (("http_503", True), ("http_500", True), ("http_400", False)):
        chat = fake_server(model="qwen3.8-flash-next", ctx=262_144, script=script)
        st = _boot(monkeypatch, CHAT_LLM_BASE_URL=f"{chat}/v1",
                   CHAT_LLM_MODEL="qwen3.8-flash-next", CHAT_LLM_MAX_RETRIES="0")
        before = _calls(glm)
        if falls:
            assert asyncio.run(st.chat_llm.ainvoke([("human", "안녕")])).content, script
            assert _calls(glm) == before + 1 and st.chat_llm_info["fallback"]["count"] == 1, script
        else:
            with pytest.raises(openai.BadRequestError):
                asyncio.run(st.chat_llm.ainvoke([("human", "안녕")]))
            assert _calls(glm) == before and st.chat_llm_info["fallback"]["count"] == 0, \
                "400 은 GLM 에서도 같은 까닭으로 실패할 수 있다 — 넘기지 않는다"
            assert st.chat_llm_info["last_error"].startswith("BadRequestError")


@tool
def noop(q: str) -> str:
    """아무것도 하지 않는 도구 — 에이전트를 앱이 만드는 길로 만들기 위한 자리."""
    return "ok"


def test_스트리밍_챗_서버가_죽어_비스트리밍_GLM_이_답해도_그_글이_화면에_나간다(monkeypatch, fake_server):
    glm = fake_server(model="fake-glm")
    _glm(monkeypatch, glm)
    st = _boot(monkeypatch, LLM_DISABLE_STREAMING="1",
               CHAT_LLM_BASE_URL=f"http://127.0.0.1:{_free_port()}/v1", CHAT_LLM_MODEL="qwen3.8-flash-next",
               CHAT_LLM_DISABLE_STREAMING="0", CHAT_LLM_MAX_RETRIES="0", LLM_CONNECT_TIMEOUT_S="3")
    assert st.llm_nostream is False and st.chat_llm.fallbacks[0].disable_streaming is True

    class _StubClient:
        def __init__(self, _connections):
            pass

        async def get_tools(self):
            return [noop]

    monkeypatch.setattr(a, "MultiServerMCPClient", _StubClient)
    state = types.SimpleNamespace(
        llm=st.llm, chat_llm=st.chat_llm, connections={"gateway": {"url": "http://gw/mcp", "headers": {"Authorization": "Bearer x"}}},
        agent_cache={}, tool_degraded={}, tool_load_error={}, tool_snapshot={})
    agent = asyncio.run(a._agent_for(types.SimpleNamespace(state=state), ["admin"]))

    async def built(*_a, **_k):
        return agent

    monkeypatch.setattr(a, "_agent_for", built)
    monkeypatch.setattr(a, "_CHAT_LOAD", rg.ChatLoad())
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    monkeypatch.setattr(a.app.state, "llm_nostream", st.llm_nostream, raising=False)
    monkeypatch.setattr(a.app.state, "chat_llm_cfg", None, raising=False)
    monkeypatch.setenv("CHAT_AUTO_RETRY", "0")

    async def chat_turn():
        req = a.ChatRequest(message="안녕", groups=["portal-admin"], history=[])
        return [c async for c in a._agent_stream(a.app, req)]

    chunks = asyncio.run(chat_turn())
    tokens = [json.loads(c.decode().split("data: ", 1)[1]) for c in chunks
              if c.startswith(b"event: token")]
    text = "".join(t.get("delta", "") for t in tokens)
    assert text.strip(), f"넘겨받은 비스트리밍 답의 글이 나가야 한다 — {chunks[-3:]}"
    # 글을 버리면 턴이 '빈 답' 으로 끝나 확정 종결 절차가 돈다 — 그 절차도 글(안내)을 내므로 글이 있다는 것만으로는 못 가른다.
    assert not any("확정 종결" in c.decode() for c in chunks), "넘겨받은 답의 글이 정상 경로로 나갔다(빈 답 종결 아님)"
    assert _calls(glm) == 1
    assert st.chat_llm_info["fallback"]["count"] == 1, "챗 에이전트는 챗 전용 클라이언트로 먼저 불렀다(닫힌 서버 → 넘김)"


def test_챗_턴의_창은_두_서버_중_작은_쪽이고_끝나면_되돌린다(monkeypatch, fake_server):
    chat = fake_server(model="qwen3.8-flash-next", ctx=262_144)
    monkeypatch.setattr(a.app.state, "chat_llm_cfg",
                        {"url": f"{chat}/v1", "model": "qwen3.8-flash-next", "key": "EMPTY", "ctx_fallback": 128_000},
                        raising=False)
    monkeypatch.setattr(a, "_chat_ctx_cache", {})
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    seen, after = [], []

    async def stub(_app, _req):
        seen.append(a._model_context_tokens())
        yield b"x"

    monkeypatch.setattr(a, "_agent_stream_turn", stub)

    async def run():
        out = [c async for c in a._agent_stream(a.app, types.SimpleNamespace())]
        after.append(a._model_context_tokens())        # 같은 태스크 — 덮어쓴 창이 되돌려졌나
        return out

    assert asyncio.run(run()) == [b"x"]
    assert seen == [262_144], "챗 턴 안에서는 챗 서버의 창(작은 쪽)"
    assert after == [1_048_576], "턴이 끝나면 같은 태스크에서 VLLM_* 창으로 돌아온다"
    assert a._chat_ctx_cache["served"] is True and a._chat_ctx_cache["src"] == "server"
    # 챗 서버에 못 물으면 CHAT_LLM_CONTEXT_TOKENS(여기 128,000) — 굳히지 않는다
    monkeypatch.setattr(a.app.state, "chat_llm_cfg",
                        {"url": f"http://127.0.0.1:{_free_port()}/v1", "model": "q", "key": "EMPTY", "ctx_fallback": 128_000},
                        raising=False)
    monkeypatch.setattr(a, "_chat_ctx_cache", {})
    seen.clear()
    asyncio.run(run())
    assert seen == [128_000] and "n" not in a._chat_ctx_cache


def test_health_의_chat_llm_은_키를_싣지_않는다(monkeypatch):
    info = {"enabled": True, "server": "127.0.0.1:18700", "model": "qwen3.8-flash-next", "thinking": False,
            "streaming": False, "last_error": None, "last_error_at": None,
            "fallback": {"enabled": True, "to": "GLM-5.2", "count": 2, "last_at": "2026-10-17T00:00:00+00:00"}}
    monkeypatch.setattr(a.app.state, "chat_llm_info", info, raising=False)
    monkeypatch.setattr(a.app.state, "chat_llm_cfg", {"url": "http://127.0.0.1:18700/v1", "model": "q",
                                                      "key": "secret-key-xyz", "ctx_fallback": 128_000}, raising=False)
    monkeypatch.setattr(a, "_chat_ctx_cache", {"n": 262_144, "src": "server", "served": True})
    h = a._chat_llm_health()
    assert "secret-key-xyz" not in json.dumps(h, ensure_ascii=False)
    assert h["model"] == "qwen3.8-flash-next" and h["fallback"]["count"] == 2 and h["ctx_tokens"] == 262_144
    monkeypatch.setattr(a.app.state, "chat_llm_info", {"enabled": False}, raising=False)
    assert a._chat_llm_health() == {"enabled": False}
