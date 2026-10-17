# 챗 전용 LLM(CHAT_LLM_*) — 챗만 다른 서버로 보내고, 그 서버가 죽으면 호출 단위로 VLLM_* 로 넘기는지(HWAXPortal docs/chat-llm-stcx)
"""왜 — cae00 에 stcx GPU LLM(Qwen3.8-Flash-Next 10벌, 127.0.0.1:18700)이 생겼다. 사용자 결정(10-17): 챗은 그 서버, 띵킹 · 심의는 GLM,
그 서버가 죽으면 챗은 GLM 으로 넘기고, 챗에서는 Qwen 의 생각 모드를 끈다. VLLM_BASE_URL 을 바꾸면 챗 · 심의 클라이언트가 함께 바뀌므로
챗 전용 클라이언트를 따로 둔다.

  · 비면 챗도 VLLM_* 클라이언트 그대로(바뀌는 것 없음). 채우면 챗만 그 서버 · 생각 끔 · 심의 · 띵킹 · 심의 준비는 VLLM_*.
  · 넘김은 LLM 호출 단위 — 연결 실패 · 5xx · 401/403 · 첫 조각 전 침묵 · '창이 모자라다' 400. 다른 400 은 넘기지 않는다.
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
          "CHAT_LLM_MAX_RETRIES", "CHAT_LLM_FALLBACK", "CHAT_LLM_CONTEXT_TOKENS", "CHAT_LLM_REASONING_EFFORT", "CHAT_LLM_MAX_TOKENS",
          "CHAT_LLM_OUTPUT_RESERVE",
          "LLM_MAX_TOKENS", "LLM_DISABLE_STREAMING",
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


def test_채우면_챗만_그_서버이고_생각은_최고_강도로_켜며_심의는_그대로다(monkeypatch):
    """사용자 결정(10-18) — 기본 챗은 Qwen 의 최고 강도(xhigh). 10-17 의 '생각 끔' 을 뒤집었다(HWAXPortal docs/chat-depth-context)."""
    st = _boot(monkeypatch, CHAT_LLM_BASE_URL="http://127.0.0.1:1/v1/", CHAT_LLM_MODEL="qwen3.8-flash-next",
               LLM_REASONING_EFFORT="high")
    assert isinstance(st.chat_llm, RunnableWithFallbacks)
    primary, (fallback,) = st.chat_llm.runnable, st.chat_llm.fallbacks
    assert primary.model_name == "qwen3.8-flash-next" and primary.openai_api_base == "http://127.0.0.1:1/v1"
    assert primary.extra_body == {"chat_template_kwargs": {"reasoning_effort": "xhigh", "enable_thinking": True}}, \
        "생각 켬 · 강도는 CHAT_LLM_REASONING_EFFORT(기본 xhigh) — GLM 의 reasoning_effort 는 챗 서버에 보내지 않는다"
    assert primary.max_tokens is None and fallback.max_tokens is None, \
        "생각을 켜도 출력 상한은 보내지 않는다 — stcx 서버가 131,072 로 막는다(요청서 v1.1 0장, CD-D6)"
    assert fallback.model_name == a.VLLM_MODEL and fallback.openai_api_base == a.VLLM_BASE_URL
    assert fallback.extra_body == {"chat_template_kwargs": {"reasoning_effort": "high"}}, "넘겨받는 쪽은 VLLM_* 설정 그대로"
    assert st.llm.model_name == a.VLLM_MODEL and st.delib_llm.model_name == a.VLLM_MODEL, "띵킹 · 심의 준비 · 심의는 그대로"
    assert st.chat_llm_info["enabled"] and st.chat_llm_info["fallback"] == {"enabled": True, "to": a.VLLM_MODEL,
                                                                           "count": 0, "last_at": None}
    assert st.chat_llm_info["thinking"] is True and st.chat_llm_info["effort"] == "xhigh"
    assert st.chat_llm_info["max_tokens"] is None and st.chat_llm_cfg["max_tokens"] == 0
    assert st.chat_llm_cfg["output_reserve"] == 65536 and st.chat_llm_info["output_reserve"] == 65536, "대신 출력 몫을 비운다"
    st = _boot(monkeypatch, CHAT_LLM_BASE_URL="http://127.0.0.1:1/v1", CHAT_LLM_MODEL="q", CHAT_LLM_ENABLE_THINKING="0",
               LLM_MAX_TOKENS="4096")
    assert st.chat_llm.runnable.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}, "끄면 종전 그대로"
    assert st.chat_llm.runnable.max_tokens == 4096 and st.chat_llm_info["effort"] is None, "끄면 상한도 공용 값"
    assert st.chat_llm_cfg["output_reserve"] == 4096, "보내는 상한이 있으면 출력 몫은 적어도 그 값(입력 + max_tokens ≤ 창)"
    st = _boot(monkeypatch, CHAT_LLM_BASE_URL="http://127.0.0.1:1/v1", CHAT_LLM_MODEL="q", CHAT_LLM_REASONING_EFFORT="medium",
               CHAT_LLM_MAX_TOKENS="100000", CHAT_LLM_OUTPUT_RESERVE="20000")
    assert st.chat_llm.runnable.extra_body == {"chat_template_kwargs": {"reasoning_effort": "medium", "enable_thinking": True}}
    assert st.chat_llm.runnable.max_tokens == 100000 and st.chat_llm_cfg["output_reserve"] == 100000, \
        "손잡이로 상한을 보내면 그 값을 보내고, 출력 몫은 그보다 작을 수 없다"
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


def test_창이_모자라다는_400_은_넘긴다_비스트리밍_스트리밍_도구_묶음(monkeypatch, fake_server):
    """stcx 를 1M(YaRN)으로 올리는 시범 · rolling 동안 복제본 창이 섞인다(262,144 와 1,010,000). 에이전트가 새 복제본에서 1M 을 읽고 만든
    긴 요청을 부하분산기가 옛 복제본에 보내면 vLLM 이 '창이 모자라다' 400 을 낸다 — 챗 턴의 창은 두 서버 중 작은 값이라 GLM 은 같은
    까닭으로 실패하지 않는다(HWAXPortal docs/chat-llm-stcx CL-D9). 챗 에이전트는 bind_tools 로 묶은 것을 부르므로 그 길도 본다."""
    glm = fake_server(model="fake-glm")
    _glm(monkeypatch, glm)
    chat = fake_server(model="qwen3.8-flash-next", ctx=262_144, script="ctx_overflow")
    for nostream in ("1", "0"):              # 1 — 비스트리밍(cae00 기본) · bind_tools 길, 0 — 스트리밍
        st = _boot(monkeypatch, CHAT_LLM_BASE_URL=f"{chat}/v1", CHAT_LLM_MODEL="qwen3.8-flash-next", CHAT_LLM_MAX_RETRIES="0",
                   CHAT_LLM_DISABLE_STREAMING=nostream)
        before = _calls(glm)

        async def go():
            if nostream == "1":
                return (await st.chat_llm.bind_tools([noop]).ainvoke([("human", "안녕")])).content
            return "".join([str(c.content) async for c in st.chat_llm.astream([("human", "안녕")])])
        assert asyncio.run(go()), f"넘겨받은 VLLM_* 가 답해야 한다(CHAT_LLM_DISABLE_STREAMING={nostream})"
        assert _calls(glm) == before + 1 and st.chat_llm_info["fallback"]["count"] == 1, nostream
        err = st.chat_llm_info["last_error"] or ""
        assert err.startswith("ChatCtxOverflow") and "maximum context length is 262144" in err, err


def test_창이_모자라다는_vLLM_의_두_문구만이고_다른_400_은_그대로_올린다():
    """vLLM 0.23.0 원문 — OpenAI 호환 서버의 검사(renderers/params.py)와 엔진의 검사(input_processor.py). 도구 스키마 · 인자 같은 다른 400 은
    GLM 에서도 같은 까닭으로 실패할 수 있어 그대로 올린다(test_5xx_는_넘기고_400_은_넘기지_않는다)."""
    import httpx as _httpx

    req = _httpx.Request("POST", "http://chat.test/v1/chat/completions")

    def bad(msg: str) -> openai.BadRequestError:
        body = {"error": {"message": msg, "type": "BadRequestError", "param": None, "code": 400}}
        return openai.BadRequestError(f"Error code: 400 - {body}", response=_httpx.Response(400, request=req, json=body),
                                      body=body["error"])

    def raised(msg: str) -> BaseException:
        try:
            with a._ctx_overflow_falls_back():
                raise bad(msg)
        except BaseException as e:  # noqa: BLE001
            return e
        raise AssertionError("올리지 않았다")

    for msg in ("This model's maximum context length is 262144 tokens. However, you requested 1 output tokens and your prompt "
                "contains 300000 input tokens, for a total of 300001 tokens. Please reduce the length of the input prompt or the "
                "number of requested output tokens.",
                "The decoder prompt (length 300000) is longer than the maximum model length of 262144. Make sure that "
                "`max_model_len` is no smaller than the number of text tokens."):
        e = raised(msg)
        assert isinstance(e, a.ChatCtxOverflow) and isinstance(e, openai.BadRequestError), msg
        assert isinstance(e.__cause__, openai.BadRequestError) and e.status_code == 400
    for msg in ("fake 400", "Invalid 'tools': schema of tool 'x' is not valid JSON Schema"):
        e = raised(msg)
        assert type(e) is openai.BadRequestError, f"다른 400 은 그대로 — {msg}"
    assert a.ChatCtxOverflow in a._chat_fallback_errors() and openai.BadRequestError not in a._chat_fallback_errors()


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


def test_챗_턴의_창에서_출력_몫을_뗀다(monkeypatch, fake_server):
    """보내지 않아도 입력이 창을 채우면 생각할 자리가 없어 답이 잘린다 — 상한을 보내면 vLLM 은 입력 + max_tokens 가 창을 넘을 때 400 이고
    그 400 은 GLM 으로 넘어가 Qwen 대신 GLM 이 조용히 답한다(HWAXPortal docs/chat-depth-context CD-D4 · CD-D6). 창의 절반보다 크게는 떼지 않는다."""
    chat = fake_server(model="qwen3.8-flash-next", ctx=262_144)
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    seen = []

    async def stub(_app, _req):
        seen.append(a._model_context_tokens())
        yield b"x"

    monkeypatch.setattr(a, "_agent_stream_turn", stub)
    for out, want in ((65_536, 262_144 - 65_536), (0, 262_144), (400_000, 262_144 // 2)):
        monkeypatch.setattr(a.app.state, "chat_llm_cfg", {"url": f"{chat}/v1", "model": "qwen3.8-flash-next", "key": "EMPTY",
                                                          "ctx_fallback": 128_000, "max_tokens": 0, "output_reserve": out},
                            raising=False)
        monkeypatch.setattr(a, "_chat_ctx_cache", {})
        seen.clear()

        async def run():
            return [c async for c in a._agent_stream(a.app, types.SimpleNamespace())]
        asyncio.run(run())
        assert seen == [want], (out, seen)


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


def test_401_403_은_그_서버의_키라_넘기고_사유를_남긴다(monkeypatch, fake_server):
    """키 없이 켜거나 키를 바꾸는 동안(stcx rolling — 옛 키 · 새 키가 섞인다) 챗이 그대로 실패했다(10-17 cae00 리뷰 A1).
    VLLM_* 는 자격이 따로라 같은 까닭으로 실패하지 않는다 — 넘기되 사유는 /health 의 last_error 로 남긴다."""
    glm = fake_server(model="fake-glm")
    _glm(monkeypatch, glm)
    for script, err in (("http_401", "AuthenticationError"), ("http_403", "PermissionDeniedError")):
        chat = fake_server(model="qwen3.8-flash-next", ctx=262_144, script=script)
        st = _boot(monkeypatch, CHAT_LLM_BASE_URL=f"{chat}/v1", CHAT_LLM_MODEL="qwen3.8-flash-next", CHAT_LLM_MAX_RETRIES="0")
        before = _calls(glm)
        assert asyncio.run(st.chat_llm.ainvoke([("human", "안녕")])).content, script
        assert _calls(glm) == before + 1 and st.chat_llm_info["fallback"]["count"] == 1, script
        assert st.chat_llm_info["last_error"].startswith(err), st.chat_llm_info["last_error"]


def test_스트림이_첫_조각_전에_멈추면_넘긴다(monkeypatch, fake_server):
    """StreamChunkTimeoutError 는 openai SDK 예외가 아니라 넘기지 못했다 — stcx 복제본 하나가 받고 멈추면 그 사용자가 300초 뒤 챗 오류를
    봤다(10-17 cae00 리뷰 A2). 한도를 0.1초로 줄이고 첫 조각 전 0.3초 침묵하는 서버(slow)로 본다."""
    glm = fake_server(model="fake-glm")
    _glm(monkeypatch, glm)
    chat = fake_server(model="qwen3.8-flash-next", ctx=262_144, script="slow")
    monkeypatch.setenv("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S", "0.1")
    st = _boot(monkeypatch, CHAT_LLM_BASE_URL=f"{chat}/v1", CHAT_LLM_MODEL="qwen3.8-flash-next",
               CHAT_LLM_MAX_RETRIES="0", CHAT_LLM_DISABLE_STREAMING="0")

    async def go():
        return "".join([str(c.content) async for c in st.chat_llm.astream([("human", "안녕")])])
    assert asyncio.run(go()), "넘겨받은 VLLM_* 가 답해야 한다"
    assert st.chat_llm_info["fallback"]["count"] == 1, st.chat_llm_info
    assert "Timeout" in (st.chat_llm_info["last_error"] or ""), st.chat_llm_info["last_error"]


def test_창_조회는_200_만_받고_실패해도_굳히지_않으며_주기적으로_다시_묻는다(monkeypatch):
    """401 본문까지 r.json() 으로 읽어 '모델 없음' 으로 영원히 굳혔다 — 키가 틀린 것이 모델 이름 문제로 보였고, 키를 고쳐도 재기동 전까지
    /health 가 옛 답이었다(10-17 cae00 리뷰 A3)."""
    cfg = {"url": "http://chat.test/v1", "model": "qwen", "key": "k", "ctx_fallback": 128_000}
    monkeypatch.setattr(a.app.state, "chat_llm_cfg", cfg, raising=False)
    monkeypatch.setattr(a.app.state, "chat_llm_info", {"enabled": True, "fallback": {}}, raising=False)
    monkeypatch.setattr(a, "_chat_ctx_cache", {})
    replies = [httpx.Response(401, json={"error": "Unauthorized"}),
               httpx.Response(200, json={"data": [{"id": "qwen", "max_model_len": 262_144}]}),
               httpx.Response(401, json={"error": "Unauthorized"})]
    calls: list = []

    def fake_get(url, **kw):
        calls.append(url)
        return replies.pop(0)
    monkeypatch.setattr(httpx, "get", fake_get)
    clock = [1000.0]
    monkeypatch.setattr(a.time, "monotonic", lambda: clock[0])

    assert a._chat_context_tokens() == 128_000
    assert a._chat_ctx_cache["probe_error"].endswith("HTTP 401") and "served" not in a._chat_ctx_cache, "401 을 '모델 없음' 으로 굳혔다"
    assert a._chat_llm_health()["ctx_probe_error"].endswith("HTTP 401"), "/health 가 키 거절을 보인다"
    assert a._chat_context_tokens() == 128_000 and len(calls) == 1, "다시 묻는 간격 전에는 묻지 않는다"
    clock[0] += a._CTX_RETRY_S + 1
    assert a._chat_context_tokens() == 262_144 and a._chat_ctx_cache["served"] and a._chat_ctx_cache["probe_error"] is None
    assert a._chat_context_tokens() == 262_144 and len(calls) == 2, "받은 답은 주기 동안 쓴다"
    clock[0] += a._CHAT_CTX_TTL_S + 1
    assert a._chat_context_tokens() == 262_144, "주기가 지나 다시 물어 실패해도 앞서 받은 창을 쓴다"
    assert len(calls) == 3 and a._chat_ctx_cache["probe_error"].endswith("HTTP 401"), "키가 바뀐 것을 다음 주기에 본다"
