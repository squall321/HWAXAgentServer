# 챗의 그림 — 올린 그림 · 앞 대화의 그림 · 도구 그림을 챗 모델이 보는지, 그림을 못 받는 모델(넘김 받는 GLM)에는 빼는지(HWAXPortal docs/chat-depth-context T5)
"""왜 — HWAX → LLM 요청에 image_url 이 어디에도 없어 모델을 바꿔도 그림을 못 읽었다(docs/chat-vision 진단). 사용자 지시(10-18) — 이전 대화 ·
심의에 넘기는 것에 올린 그림과 디지털워커가 조회한 그림도 들어가야 한다, 자료를 기반으로 판단해야 한다. 결정 — 챗(Qwen)은 그림 그대로,
띵킹 · 심의(GLM)는 탐침 뒤 자동(받으면 그림, 못 받으면 Qwen 판독문).

  · 판정 — 빨간 그림 탐침: 받으면 True · 400 이면 False · 닿지 않으면 모름(굳히지 않는다). 설정 1 · 0 은 묻지 않는다.
  · 올린 그림 — 아티팩트로 저장, 'user_images' 사건으로 주소, 챗 모델이 받으면 그 발화의 그림 조각.
  · 그림을 못 받는 챗 모델 — 그림을 보내지 않고 그렇다고 말한다(판독할 모델도 없으면).
  · 넘김 — 챗 서버가 죽어 GLM(그림을 못 받는다)으로 넘어가면 그림 조각을 빼고 보낸다(안 빼면 그 순간 400).
  · 앞 대화의 그림 — 이력 항목의 images · 근거에 나온 그림을 최근부터 다시 싣는다.
  · 도구 그림 — 도구 결과 묶음 바로 뒤에 싣는다(도구 결과 사이에는 끼우지 않는다).

  실행:  .venv/bin/python -m pytest tests/test_chat_vision.py -q
"""
import asyncio
import base64
import json
import os
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
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402
import review_gate as rg  # noqa: E402
import vision as v  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PNG = v.red_png(8)
PNG_B64 = base64.b64encode(PNG).decode()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port != 9009
    return port


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    art = tmp_path / "artifacts"
    art.mkdir()
    monkeypatch.setattr(a, "ARTIFACT_DIR", str(art))
    v._probe_cache.clear()
    v._DU_CACHE.clear()
    yield art
    v._probe_cache.clear()
    v._DU_CACHE.clear()


@pytest.fixture
def fake_server(tmp_path):
    procs = []

    def start(model="fake-glm", vision=1, script="oracle"):
        port, log = _free_port(), tmp_path / f"fake-{len(procs)}.log"
        with open(log, "w") as out:
            p = subprocess.Popen([sys.executable, str(ROOT / "tests/fakes/llm_server.py"), "--port", str(port), "--ctx", "262144",
                                  "--model", model, "--script", script, "--vision", str(vision)],
                                 cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"},
                                 stdout=out, stderr=subprocess.STDOUT)
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


def _images_seen(base: str) -> list:
    with httpx.Client(timeout=5, trust_env=False) as c:
        return c.get(f"{base}/_stats").json()["images"]


def _boot(monkeypatch, **env):
    @asynccontextmanager
    async def _no_session_manager(_app):
        yield
    for k in ("CHAT_LLM_BASE_URL", "CHAT_LLM_MODEL", "CHAT_LLM_API_KEY", "CHAT_LLM_DISABLE_STREAMING", "CHAT_LLM_MAX_RETRIES",
              "CHAT_LLM_FALLBACK", "LLM_DISABLE_STREAMING", "CHAT_LLM_VISION", "LLM_VISION", "LLM_MAX_RETRIES"):
        monkeypatch.delenv(k, raising=False)
    for k, val in env.items():
        monkeypatch.setenv(k, str(val))
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


@tool
def noop(q: str) -> str:
    """아무것도 하지 않는 도구."""
    return "ok"


def _turn(monkeypatch, st, *, message="이 그림 봐 줘", images=(), history=()):
    class _StubClient:
        def __init__(self, _c):
            pass

        async def get_tools(self):
            return [noop]
    monkeypatch.setattr(a, "MultiServerMCPClient", _StubClient)
    state = types.SimpleNamespace(llm=st.llm, chat_llm=st.chat_llm, connections={"gateway": {"url": "http://gw/mcp", "headers": {}}},
                                  agent_cache={}, tool_degraded={}, tool_load_error={}, tool_snapshot={})
    agent = asyncio.run(a._agent_for(types.SimpleNamespace(state=state), ["admin"]))

    async def built(*_a, **_k):
        return agent
    monkeypatch.setattr(a, "_agent_for", built)
    monkeypatch.setattr(a, "_CHAT_LOAD", rg.ChatLoad())
    for k, val in (("review_gate", None), ("llm_nostream", st.llm_nostream), ("chat_llm_cfg", st.chat_llm_cfg),
                   ("chat_llm_info", st.chat_llm_info), ("chat_llm", st.chat_llm), ("llm", st.llm)):
        monkeypatch.setattr(a.app.state, k, val, raising=False)
    monkeypatch.setattr(a, "_chat_ctx_cache", {})
    monkeypatch.setenv("CHAT_AUTO_RETRY", "0")

    async def go():
        req = a.ChatRequest(message=message, groups=["portal-admin"], history=list(history), images=list(images))
        return [c async for c in a._agent_stream(a.app, req)]
    out = []
    for c in asyncio.run(go()):
        t = c.decode()
        if t.startswith("event: "):
            out.append((t.split("\n", 1)[0][7:], json.loads(t.split("data: ", 1)[1])))
    return out


def _glm(monkeypatch, base):
    monkeypatch.setattr(a, "VLLM_BASE_URL", f"{base}/v1")
    monkeypatch.setattr(a, "VLLM_MODEL", "fake-glm")


def test_판정은_탐침으로_하고_모름은_굳히지_않는다(fake_server):
    yes, no = fake_server(model="q", vision=1), fake_server(model="g", vision=0)
    assert v.supports("auto", f"{yes}/v1", "k", "q") is True
    assert v.supports("auto", f"{no}/v1", "k", "g") is False
    closed = f"http://127.0.0.1:{_free_port()}/v1"
    assert v.supports("auto", closed, "k", "x") is False and v.cached("auto", closed, "x") is None, "닿지 않음은 모름 — 굳히지 않는다"
    assert v.supports("1", closed, "k", "x") is True and v.supports("0", f"{yes}/v1", "k", "q") is False, "설정은 묻지 않는다"
    assert any(val is True for val in v.state().values()) and any(val is False for val in v.state().values())


def test_올린_그림은_아티팩트로_남고_꼴이_틀리면_건너뛰고_센다(_iso):
    urls, skipped = a._stash_uploads([{"name": "a.png", "mime": "image/png", "data": PNG_B64},
                                      {"name": "b.png", "data": f"data:image/png;base64,{PNG_B64}"},     # 같은 그림 — 한 파일
                                      {"name": "c.txt", "mime": "text/plain", "data": PNG_B64},
                                      {"name": "d.png", "mime": "image/png", "data": "!!!"},
                                      # 그림이라 적었지만 내용은 글 — 그림 조각으로 보내면 서버가 400 으로 그 턴을 죽인다
                                      {"name": "e.png", "mime": "image/png", "data": base64.b64encode(b"not an image").decode()}])
    assert len(urls) == 1 and urls[0].startswith("/agent/artifacts/") and urls[0].endswith(".png")
    assert skipped == 3, "건너뛴 것은 센다(화면에 알린다)"
    assert (_iso / urls[0].rsplit("/", 1)[-1]).read_bytes() == PNG
    jpg = b"\xff\xd8\xff\xe0" + b"0" * 32
    u2, _s = a._stash_uploads([{"name": "x.png", "mime": "image/png", "data": base64.b64encode(jpg).decode()}])
    assert u2[0].endswith(".jpg"), "확장자는 선언이 아니라 내용으로"
    _u, over = a._stash_uploads([{"name": f"{i}.png", "mime": "image/png", "data": PNG_B64} for i in range(v.MAX_IMAGES + 2)])
    assert over == 2, "장수 상한 밖도 센다"


def test_챗_모델이_그림을_받으면_올린_그림을_그_발화에_싣는다(monkeypatch, fake_server):
    glm, chat = fake_server(model="fake-glm", vision=0), fake_server(model="qwen", vision=1)
    _glm(monkeypatch, glm)
    st = _boot(monkeypatch, CHAT_LLM_BASE_URL=f"{chat}/v1", CHAT_LLM_MODEL="qwen", CHAT_LLM_MAX_RETRIES="0")
    evs = _turn(monkeypatch, st, images=[{"name": "s.png", "mime": "image/png", "data": PNG_B64}])
    ui = [d for e, d in evs if e == "user_images"]
    assert ui and ui[0]["urls"][0].startswith("/agent/artifacts/"), evs[:4]
    assert max(_images_seen(chat)) >= 1, "챗 서버가 그림 조각을 받았다"


def test_그림을_못_받는_챗_모델이면_보내지_않고_그렇다고_말한다(monkeypatch, fake_server, _iso):
    (_iso / "old111.png").write_bytes(PNG)
    glm, chat = fake_server(model="fake-glm", vision=0), fake_server(model="qwen", vision=0)
    _glm(monkeypatch, glm)
    st = _boot(monkeypatch, CHAT_LLM_BASE_URL=f"{chat}/v1", CHAT_LLM_MODEL="qwen", CHAT_LLM_MAX_RETRIES="0")
    hist = [{"role": "user", "content": "앞 그림", "images": ["/agent/artifacts/old111.png"]}, {"role": "assistant", "content": "네"}]
    evs = _turn(monkeypatch, st, images=[{"name": "s.png", "mime": "image/png", "data": PNG_B64}], history=hist)
    steps = [d.get("step", "") for e, d in evs if e == "status"]
    assert any("올린 그림을 모델에 싣지 못했다" in x for x in steps), steps
    assert any("앞 대화의 그림 1장 — 챗 모델도 판독할 모델도 그림을 읽지 못해 싣지 못했다" in x for x in steps), "앞 그림도 조용히 빼지 않는다"
    assert all(n == 0 for n in _images_seen(chat)[1:]), "탐침 뒤 챗 요청에는 그림이 없다"
    assert not any(e == "error" for e, _ in evs)


def test_챗_모델이_못_읽어도_그림을_읽는_모델이_있으면_판독문으로_싣는다(monkeypatch, fake_server, _iso):
    """챗 전용 LLM 은 그림을 못 받고 VLLM_* 가 받는 경우 — 판독문(그 서버가 옮긴 글)이 올린 그림 · 앞 그림 자리에 들어간다."""
    (_iso / "old222.png").write_bytes(PNG + b"y")
    glm, chat = fake_server(model="fake-glm", vision=1), fake_server(model="qwen", vision=0)
    _glm(monkeypatch, glm)
    st = _boot(monkeypatch, CHAT_LLM_BASE_URL=f"{chat}/v1", CHAT_LLM_MODEL="qwen", CHAT_LLM_MAX_RETRIES="0")
    hist = [{"role": "user", "content": "앞 그림", "images": ["/agent/artifacts/old222.png"]}, {"role": "assistant", "content": "네"}]
    evs = _turn(monkeypatch, st, images=[{"name": "s.png", "mime": "image/png", "data": PNG_B64}], history=hist)
    steps = [d.get("step", "") for e, d in evs if e == "status"]
    assert any("올린 그림을 판독문으로 실었다" in x for x in steps), steps
    assert any("앞 대화의 그림 판독문 1장을 실었습니다" in x for x in steps), steps
    assert sum(1 for n in _images_seen(glm) if n) >= 3, "탐침 + 판독 둘(올린 그림 · 앞 그림)은 그림을 받는 서버로 갔다"
    assert all(n == 0 for n in _images_seen(chat)[1:]), "챗 서버에는 그림 조각이 가지 않았다"
    assert len(list((_iso / ".transcripts").glob("*.txt"))) == 2, "판독문은 그림 해시로 남는다"
    with httpx.Client(timeout=5, trust_env=False) as c:
        sent = "\n".join(c.get(f"{chat}/_stats").json()["humans"])
    assert "올린 그림을 이 챗 모델은 읽지 못해 그림을 읽는 모델이 판독문으로 옮겼다" in sent, "올린 그림의 판독문이 챗 요청에 실렸다"
    assert "앞 대화에 나온 그림의 판독문이다" in sent, "앞 그림의 판독문이 챗 요청에 실렸다"


def test_넘겨받는_GLM_이_그림을_못_받으면_그림을_빼고_보낸다(monkeypatch, fake_server):
    glm = fake_server(model="fake-glm", vision=0)
    _glm(monkeypatch, glm)
    closed = f"http://127.0.0.1:{_free_port()}/v1"
    st = _boot(monkeypatch, CHAT_LLM_BASE_URL=closed, CHAT_LLM_MODEL="qwen", CHAT_LLM_MAX_RETRIES="0", CHAT_LLM_VISION="1",
               LLM_CONNECT_TIMEOUT_S="3")
    assert _images_seen(glm) == [], "기동은 LLM 을 부르지 않는다 — 판정은 그림이 실린 호출이 올 때"
    evs = _turn(monkeypatch, st, images=[{"name": "s.png", "mime": "image/png", "data": PNG_B64}])
    assert "".join(d.get("delta", "") for e, d in evs if e == "token").strip(), evs[-4:]
    seen = _images_seen(glm)
    assert seen[0] == 1 and all(n == 0 for n in seen[1:]), f"넘김 첫 호출에서 탐침 한 번(그림 1장), 그 뒤 요청엔 그림이 없다 — {seen}"
    assert st.chat_llm_info["fallback"]["count"] >= 1


def test_앞_대화의_그림을_최근부터_다시_싣는다(monkeypatch, fake_server, _iso):
    (_iso / "abc123.png").write_bytes(PNG)
    glm, chat = fake_server(model="fake-glm", vision=0), fake_server(model="qwen", vision=1)
    _glm(monkeypatch, glm)
    st = _boot(monkeypatch, CHAT_LLM_BASE_URL=f"{chat}/v1", CHAT_LLM_MODEL="qwen", CHAT_LLM_MAX_RETRIES="0")
    hist = [{"role": "user", "content": "이 응력 지도", "images": ["/agent/artifacts/abc123.png"]},
            {"role": "assistant", "content": "최대 432 MPa 입니다"}]
    evs = _turn(monkeypatch, st, message="아까 그림에서 최대 위치는?", history=hist)
    assert any(e == "status" and "앞 대화의 그림 1장" in d.get("step", "") for e, d in evs), evs[:6]
    assert max(_images_seen(chat)) >= 1


def test_도구_그림은_결과_묶음_뒤에_끼우고_결과_사이에는_끼우지_않는다(_iso):
    (_iso / "plot1.png").write_bytes(PNG)
    msgs = [HumanMessage(content="그려 줘"),
            AIMessage(content="", tool_calls=[{"name": "t1", "args": {}, "id": "c1"}, {"name": "t2", "args": {}, "id": "c2"}]),
            ToolMessage(content="그림: /agent/artifacts/plot1.png", tool_call_id="c1"),
            ToolMessage(content="표", tool_call_id="c2")]
    out = a._with_tool_images(msgs, 4)
    assert [type(m).__name__ for m in out] == ["HumanMessage", "AIMessage", "ToolMessage", "ToolMessage", "HumanMessage"]
    assert out[-1].content[1]["type"] == "image_url" and out[-1].content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert a._with_tool_images(msgs[:2], 4) == msgs[:2], "도구 그림이 없으면 그대로"


def test_그림_빼기는_글을_남기고_뺀_수를_적는다():
    m = HumanMessage(content=[{"type": "text", "text": "질문"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}])
    out = v.strip_images([m, AIMessage(content="답")])
    assert out[0].content == "질문\n[그림 1장 — 이 모델은 그림을 읽지 못해 뺐다]" and out[1].content == "답"


def test_도구_그림은_run_config_의_hwax_vision_이_켜졌을_때만_싣는다(_iso):
    """문맥 변수는 LangGraph 실행 맥락을 넘지 못한다(app 의 turn_calls 주석 — 실측 빈 값) — 턴이 run config 로 알린다."""
    (_iso / "plot2.png").write_bytes(PNG)
    st = {"messages": [HumanMessage(content="q"),
                       AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c"}]),
                       ToolMessage(content="/agent/artifacts/plot2.png", tool_call_id="c")]}
    on = asyncio.run(a._loop_fit_prompt(st, {"configurable": {"hwax_vision": True}}))
    off = asyncio.run(a._loop_fit_prompt(st, {"configurable": {"hwax_vision": False}}))
    assert isinstance(on[-1], HumanMessage) and on[-1].content[1]["type"] == "image_url"
    assert len(off) == 3, "꺼져 있으면 그대로"
    import inspect
    src = inspect.getsource(a._agent_stream_turn)
    assert '"configurable": {"hwax_vision": locals().get("_turn_vision")}' in src


def test_턴이_모르면_도구가_그림을_낸_호출에서_묻는다(monkeypatch, _iso):
    """그림 없는 턴은 묻지 않고(None 그대로), 도구가 그림을 실제로 낸 호출에서 한 번 묻는다."""
    (_iso / "plot3.png").write_bytes(PNG)
    asked = []
    monkeypatch.setattr(a, "_chat_vision", lambda _app: asked.append(1) or True)
    plain = {"messages": [HumanMessage(content="q"), AIMessage(content="a")]}
    assert asyncio.run(a._loop_fit_prompt(plain, {"configurable": {"hwax_vision": None}})) and asked == [], "도구 그림이 없으면 묻지 않는다"
    st = {"messages": [HumanMessage(content="q"), AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c"}]),
                       ToolMessage(content="/agent/artifacts/plot3.png", tool_call_id="c")]}
    out = asyncio.run(a._loop_fit_prompt(st, {"configurable": {"hwax_vision": None}}))
    assert asked == [1] and out[-1].content[1]["type"] == "image_url"
