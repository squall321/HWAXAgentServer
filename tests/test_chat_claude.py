# 웹 챗의 Claude(CLAUDE_CHAT_*) — 고른 사람의 챗 턴만 Claude 로 가고, 생각 블록을 받은 그대로 되돌리며, 실패하면 기본 챗 길로 넘기는지
"""사용자 지시(2026-11-22) — dev 는 Claude API 가 정본, cae00 은 운영자가 지정한 몇 사람만 Claude 이고 나머지는 지금처럼 Qwen.

  · CLAUDE_CHAT_MODEL 이 비면 아무것도 바뀌지 않는다(꾸러미도 들이지 않는다) — 켜도 고르지 않은 사람의 턴은 LLM 에 가는 요청이 바이트까지 같다.
  · 고르기 — CLAUDE_CHAT_FOR=feat:chat-claude(기본)면 그 권한을 가진 사람만, all 이면 모두. 권한 칸이 없는 옛 호출 · 사람 신원이 없는 호출은
    주지 않는다(닫힌 쪽). 에이전트 캐시 열쇠가 고른 모델로 갈린다.
  · Claude 요청 — temperature · top_p · top_k 없음 · 생각 adaptive(summarized) · effort · 자동 캐시 · 서버 대체 모델 · 대화 머리 없음.
  · 도구 왕복 둘 이상에서 생각 블록이 바이트 그대로 되돌아가고(앞부분은 덧붙이기만), 생각 요약은 '생각 중' 칸에, 글은 한 번씩만 본문에.
  · 거절(refusal)은 까닭을 본문에 · 거절 전의 도구 호출은 실행하지 않는다 · 서버 대체 모델 경계 앞의 생각 · 도구 호출은 되돌리지 않는다.
  · 넘김 — 5xx(529) · 429 · 401/403 · 연결 실패는 기본 챗 길(여기서는 VLLM_*)로, 400 은 넘기지 않는다. 넘겨받은 쪽에는 OpenAI 꼴로 간다.
  · 자기검증 검토자는 같은 Claude 로 가되 '생각 끔' 이 effort low 로 바뀐다(OpenAI 전용 칸은 싣지 않는다).

Anthropic API 는 부르지 않는다 — Claude 는 httpx.MockTransport 에 진짜 SSE 사건 꼴을 흘리고, 넘겨받는 VLLM_* 도 가짜 전송이다. 키는 시험이
만든 가짜 파일이다. langchain-anthropic 이 없는 박스에서는 Claude 를 켜는 시험만 건너뛰고 기본 길 시험은 돈다.
"""
import asyncio
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
import openai  # noqa: E402
import pytest  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

import app as a  # noqa: E402
import review_gate as rg  # noqa: E402
import test_chat_llm as tcl  # noqa: E402 — 기동 절차를 여는 꾸밈(_boot)을 같이 쓴다

try:
    import langchain_anthropic  # noqa: F401
    HAVE_ANTHROPIC = True
except ImportError:
    HAVE_ANTHROPIC = False
needs_anthropic = pytest.mark.skipif(not HAVE_ANTHROPIC, reason="langchain-anthropic 이 없는 박스(requirements-anthropic.txt)")

FAKE_KEY = "sk-ant-test-0000-not-a-real-key"
MODEL = "claude-opus-5-5"


# ── 진짜 SSE 사건 꼴 ──────────────────────────────────────────────────────────────────────────────────────────────

def _thinking(i, text, sig):
    half = len(text) // 2
    return [{"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": text[:half]}},
            {"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": text[half:]}},
            {"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": sig}},
            {"type": "content_block_stop", "index": i}]


def _text(i, text):
    half = len(text) // 2
    return [{"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": text[:half]}},
            {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": text[half:]}},
            {"type": "content_block_stop", "index": i}]


def _tool(i, tid, name, args):
    raw = json.dumps(args, ensure_ascii=False)
    return [{"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": tid, "name": name, "input": {}}},
            {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": raw[:3]}},
            {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": raw[3:]}},
            {"type": "content_block_stop", "index": i}]


def _fallback(i, frm=MODEL, to="claude-opus-5"):
    return [{"type": "content_block_start", "index": i,
             "content_block": {"type": "fallback", "from": {"model": frm}, "to": {"model": to},
                               "trigger": {"type": "refusal", "category": "cyber"}}},
            {"type": "content_block_stop", "index": i}]


def _message(*blocks, stop="end_turn", details=None, model=MODEL):
    evs = [{"type": "message_start", "message": {"id": "msg_x", "type": "message", "role": "assistant", "model": model, "content": [],
                                                 "stop_reason": None, "stop_sequence": None,
                                                 "usage": {"input_tokens": 12, "output_tokens": 1}}},
           {"type": "ping"}]
    for b in blocks:
        evs += b
    evs.append({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None,
                                                   **({"stop_details": details} if details else {})},
                "usage": {"output_tokens": 30}})
    evs.append({"type": "message_stop"})
    return evs


def _sse_body(evs) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in evs).encode()


class _Claude:
    """가짜 Anthropic 전송 — 대본(SSE 사건 목록 또는 (상태, 본문))을 차례로 답하고 받은 요청(머리 · 본문)을 적는다."""

    def __init__(self, script):
        self.script, self.seen = list(script), []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.seen.append((dict(req.headers), json.loads(req.content)))
        item = self.script.pop(0)
        if isinstance(item, tuple):
            status, body = item
            return httpx.Response(status, json=body, headers={"x-should-retry": "false", "request-id": "req_test"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse_body(item))

    @property
    def bodies(self):
        return [b for _h, b in self.seen]


class _OpenAI:
    """가짜 OpenAI 호환 전송(넘겨받는 VLLM_*) — 비스트리밍 완결 응답. 받은 요청의 날 바이트를 적는다(바이트 동일 비교)."""

    def __init__(self, script):
        self.script, self.raw = list(script), []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.raw.append(req.content)
        msg = self.script.pop(0)
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "created": 0, "model": "fake-glm",
                                         "choices": [{"index": 0, "message": {"role": "assistant", **msg},
                                                      "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    @property
    def bodies(self):
        return [json.loads(r) for r in self.raw]


def _glm_text(text):
    return {"content": text}


def _glm_call(cid, q):
    return {"content": None, "tool_calls": [{"id": cid, "type": "function",
                                              "function": {"name": "lookup", "arguments": json.dumps({"q": q})}}]}


def _mock_openai(llm, transport) -> None:
    cli = openai.AsyncOpenAI(api_key="x", base_url="http://glm.test/v1", max_retries=0,
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    llm.root_async_client = cli
    llm.async_client = cli.chat.completions


def _key_file(tmp_path, mode=0o600, text=FAKE_KEY + "\n") -> Path:
    p = tmp_path / "anthropic.key"
    p.write_text(text)
    p.chmod(mode)
    return p


def _boot(monkeypatch, tmp_path, claude=None, glm=None, **env):
    """기동 절차를 열어(test_chat_llm._boot) Claude 와 넘겨받는 쪽에 가짜 전송을 단다. LLM_DISABLE_STREAMING=1 — 넘겨받는 VLLM_* 는 비스트리밍
    (cae00 의 GLM 우회와 같은 꼴)이라, Claude 의 스트리밍 글이 턴 끝에 한 번 더 나가지 않는지도 함께 본다."""
    if "CLAUDE_CHAT_MODEL" in env and "CLAUDE_CHAT_API_KEY_FILE" not in env:
        env["CLAUDE_CHAT_API_KEY_FILE"] = str(_key_file(tmp_path))
    env.setdefault("LLM_DISABLE_STREAMING", "1")
    st = tcl._boot(monkeypatch, **env)
    if glm is not None:
        _mock_openai(st.llm, glm)
    if st.claude_chat is not None:
        if claude is not None:
            st.claude_chat.runnable.hwax_http_client = httpx.AsyncClient(transport=httpx.MockTransport(claude))
            st.claude_chat_pick.http_client = st.claude_chat.runnable.hwax_http_client     # 사람이 고른 쌍도 같은 가짜(CC-8)
        if glm is not None:
            _mock_openai(st.claude_chat.fallbacks[0], glm)
    return st


@tool
def lookup(q: str) -> str:
    """사내 자료에서 q 의 값을 찾는다."""
    return f"[{q}] 값은 42 이다"


def _events(chunks) -> list:
    out = []
    for c in chunks:
        ev, data = a._sse_parse(c)
        if ev:
            out.append((ev, data))
    return out


def _deltas(evs, kind: str) -> str:
    return "".join(d.get("delta", "") for e, d in evs if e == kind)


def _turn(monkeypatch, tmp_path, st, **req_kw) -> list:
    """챗 한 턴(_agent_stream) — 에이전트는 앱의 _agent_for 가 짓는다(게이트웨이만 스텁). 창은 고정 값(Claude 턴은 그 턴의 창 덮어쓰기)이다."""
    class _StubClient:
        def __init__(self, _connections):
            pass

        async def get_tools(self):
            return [lookup]

    monkeypatch.setattr(a, "MultiServerMCPClient", _StubClient)
    monkeypatch.setattr(a, "ARTIFACT_DIR", str(tmp_path))
    seen_ctx = st.__dict__.setdefault("seen_ctx", [])       # 턴 안에서 읽힌 창 — Claude 턴은 그 턴의 덮어쓰기(창 − 출력 상한)

    def ctx():
        seen_ctx.append(a._external_llm.ctx_override.get())
        return seen_ctx[-1] or 200_000
    monkeypatch.setattr(a, "_model_context_tokens", ctx)
    for k, v in dict(llm=st.llm, chat_llm=st.chat_llm, chat_llm_info=st.chat_llm_info, chat_llm_cfg=st.chat_llm_cfg,
                     claude_chat=st.claude_chat, claude_chat_info=st.claude_chat_info, llm_nostream=st.llm_nostream,
                     claude_chat_pick=getattr(st, "claude_chat_pick", None),
                     connections={"gateway": {"url": "http://gw/mcp", "headers": {"Authorization": "Bearer x"}}},
                     agent_cache={}, tool_degraded={}, tool_load_error={}, tool_snapshot={}, review_gate=None).items():
        monkeypatch.setattr(a.app.state, k, v, raising=False)
    monkeypatch.setattr(a, "_CHAT_LOAD", rg.ChatLoad())
    monkeypatch.setenv("CHAT_AUTO_RETRY", "0")
    req = a.ChatRequest(**{"message": "A 와 B 의 값을 찾아 비교해 줘", "groups": ["portal-admin"], "user_email": "kim@corp.test",
                           "entitlements": ["feat:chat-claude"], **req_kw})

    async def go():
        return [c async for c in a._agent_stream(a.app, req)]
    return _events(asyncio.run(go()))


# ── 꺼짐 · 기본값 ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_비면_꺼져_있고_꾸러미도_들이지_않는다(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "chat_claude", None)        # 들이려 하면 ImportError — 꺼진 박스는 시도조차 하지 않아야 한다
    st = _boot(monkeypatch, tmp_path)
    assert st.claude_chat is None and st.claude_chat_info == {"enabled": False}
    monkeypatch.setattr(a.app.state, "claude_chat_info", st.claude_chat_info, raising=False)
    assert a._claude_chat_health() == {"enabled": False}


def test_꾸러미가_없으면_끄고_까닭을_남긴다(monkeypatch, tmp_path, capsys):
    monkeypatch.setitem(sys.modules, "chat_claude", None)
    st = _boot(monkeypatch, tmp_path, CLAUDE_CHAT_MODEL=MODEL)
    assert st.claude_chat is None and st.claude_chat_info["enabled"] is False
    assert "requirements-anthropic.txt" in st.claude_chat_info["error"]
    assert "requirements-anthropic.txt" in capsys.readouterr().out


class _Stub(BaseChatModel):
    """에이전트를 짓기만 하는 모델(부르지 않는다)."""

    @property
    def _llm_type(self) -> str:
        return "stub"

    def bind_tools(self, tools, **_kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **_kw):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])


def test_캐시_열쇠는_고른_모델로_갈리고_기본_턴의_열쇠는_종전_꼴이다(monkeypatch):
    class _StubClient:
        def __init__(self, _connections):
            pass

        async def get_tools(self):
            return [lookup]

    monkeypatch.setattr(a, "MultiServerMCPClient", _StubClient)
    base, claude = _Stub(), _Stub()
    state = types.SimpleNamespace(llm=base, claude_chat=claude, agent_cache={}, tool_degraded={}, tool_load_error={},
                                  tool_snapshot={}, connections={"gateway": {"url": "http://gw/mcp", "headers": {}}})
    app_ = types.SimpleNamespace(state=state)

    def build(with_claude: bool):
        tok = a._turn_claude.set(claude) if with_claude else None
        try:
            return asyncio.run(a._agent_for(app_, ["admin"], user="kim@corp.test", user_pat="pat-1"))
        finally:
            if tok is not None:
                a._turn_claude.reset(tok)

    q1 = build(False)
    c1 = build(True)
    assert c1 is not q1, "기본 챗 길로 지은 에이전트를 Claude 턴이 재사용하면 안 된다"
    assert build(False) is q1 and build(True) is c1, "같은 모델끼리는 재사용한다"
    keys = list(state.agent_cache)
    assert len(keys) == 2
    base_key = next(k for k in keys if "claude" not in k)
    claude_key = next(k for k in keys if "claude" in k)
    assert len(base_key) == 8, "기본 턴의 열쇠는 종전 꼴(칸 여덟) 그대로다"
    assert claude_key[:-2] == base_key[:-1] and claude_key[-2] == "claude" and claude_key[-1] == base_key[-1], \
        "Claude 칸은 자격증명 해시 앞에 하나 붙는다(강등 저장이 마지막 칸을 갈아 끼운다)"


def test_고르기는_권한_칸과_사람_신원이_있을_때만이다(monkeypatch):
    claude = object()
    st = types.SimpleNamespace(claude_chat=claude, claude_chat_info={"enabled": True, "for": "feat:chat-claude"})
    app_ = types.SimpleNamespace(state=st)

    def pick(**kw):
        return a._claude_for(app_, a.ChatRequest(message="질문", **{"user_email": "kim@corp.test", **kw}))

    assert pick(entitlements=["feat:chat-claude", "feat:thinking"]) is claude
    assert pick(entitlements=["feat:thinking"]) is None, "키가 없는 사람은 기본 챗 길"
    assert pick(entitlements=None) is None, "권한 칸이 없는 옛 호출은 주지 않는다(닫힌 쪽)"
    assert pick(entitlements=["feat:chat-claude"], user_email="") is None, "사람 신원 없이 Claude 로 가는 길은 없다"
    st.claude_chat_info = {"enabled": True, "for": "all"}
    assert pick(entitlements=[]) is claude and pick(entitlements=["feat:thinking"]) is claude, "all 이면 모든 사람"
    assert pick(entitlements=None) is None and pick(entitlements=[], user_email=" ") is None, "all 이어도 닫힌 쪽은 그대로"
    st.claude_chat = None
    assert pick(entitlements=["feat:chat-claude"]) is None, "꺼진 박스"


# ── 켜기 · 설정 · /health ─────────────────────────────────────────────────────────────────────────────────────────

@needs_anthropic
def test_켜면_설정대로_짓고_health_에는_키가_없다(monkeypatch, tmp_path, capsys):
    st = _boot(monkeypatch, tmp_path, CLAUDE_CHAT_MODEL=MODEL, CLAUDE_CHAT_EFFORT="high")
    primary, (fb,) = st.claude_chat.runnable, st.claude_chat.fallbacks
    assert primary.model == MODEL and primary.max_tokens == 64000 and primary.streaming is True
    assert primary.temperature is None and primary.top_p is None and primary.top_k is None
    assert primary.thinking == {"type": "adaptive", "display": "summarized", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert primary.output_config == {"effort": "high"} and primary.effort is None, \
        "effort 칸이 아니라 output_config 로 — 호출 인자(검토자의 effort low)가 이기게"
    assert primary.betas == ["thinking-binding-controls-2026-08-01", "server-side-fallback-2026-07-01"]
    assert primary.model_kwargs == {"cache_control": {"type": "ephemeral"}, "fallbacks": "default"}
    assert isinstance(fb, a._VisionGuardLLM) and fb.model_name == a.VLLM_MODEL, "챗 전용 LLM 이 없는 박스는 VLLM_*(그림 막이)로 넘긴다"
    info = st.claude_chat_info
    assert info["enabled"] and info["for"] == "feat:chat-claude" and info["effort"] == "high"
    assert info["ctx_tokens"] == 1_000_000 and info["max_tokens"] == 64000 and info["server_fallback"] == "default"
    assert info["key_file"] == {"present": True, "read": True, "mode": "0600"}
    assert info["fallback"] == {"enabled": True, "to": a.VLLM_MODEL, "count": 0, "last_at": None}
    monkeypatch.setattr(a.app.state, "claude_chat_info", info, raising=False)
    shown = json.dumps(a._claude_chat_health(), ensure_ascii=False) + capsys.readouterr().out + repr(primary) + str(info)
    assert FAKE_KEY not in shown and "anthropic.key" not in json.dumps(a._claude_chat_health()), "키 값 · 경로는 어디에도 없다"
    st = _boot(monkeypatch, tmp_path, CLAUDE_CHAT_MODEL=MODEL, CLAUDE_CHAT_SERVER_FALLBACK="0", CLAUDE_CHAT_FOR="all")
    assert st.claude_chat.runnable.betas == ["thinking-binding-controls-2026-08-01"]
    assert st.claude_chat.runnable.model_kwargs == {"cache_control": {"type": "ephemeral"}}
    assert st.claude_chat_info["server_fallback"] is None and st.claude_chat_info["for"] == "all"


@needs_anthropic
def test_챗_전용_LLM_이_있는_박스는_그_사슬로_넘긴다(monkeypatch, tmp_path):
    st = _boot(monkeypatch, tmp_path, CLAUDE_CHAT_MODEL=MODEL, CHAT_LLM_BASE_URL="http://127.0.0.1:1/v1", CHAT_LLM_MODEL="qwen-x")
    assert st.claude_chat.fallbacks == [st.chat_llm], "cae00 — Claude 가 실패하면 Qwen → GLM 사슬 그대로"
    assert st.claude_chat_info["fallback"]["to"] == "qwen-x"


@needs_anthropic
@pytest.mark.parametrize("env,why", [
    ({"CLAUDE_CHAT_FOR": "everyone"}, "CLAUDE_CHAT_FOR"),
    ({"CLAUDE_CHAT_EFFORT": "ultra"}, "CLAUDE_CHAT_EFFORT"),
    ({"CLAUDE_CHAT_MAX_TOKENS": "lots"}, "CLAUDE_CHAT_MAX_TOKENS"),
    ({"CLAUDE_CHAT_MAX_TOKENS": "600000"}, "절반"),
    ({"CLAUDE_CHAT_API_KEY_FILE": "/nonexistent/anthropic.key"}, "키 파일이 없다"),
    ({"CLAUDE_CHAT_API_KEY_FILE": ""}, "비었다"),
])
def test_모르는_값_키_없음은_끄고_까닭을_남긴다(monkeypatch, tmp_path, env, why):
    st = _boot(monkeypatch, tmp_path, CLAUDE_CHAT_MODEL=MODEL, **env)
    assert st.claude_chat is None and st.claude_chat_info["enabled"] is False
    assert why in st.claude_chat_info["error"], st.claude_chat_info


@needs_anthropic
def test_빈_키_파일과_넓은_권한(monkeypatch, tmp_path):
    st = _boot(monkeypatch, tmp_path, CLAUDE_CHAT_MODEL=MODEL, CLAUDE_CHAT_API_KEY_FILE=str(_key_file(tmp_path, text="\n")))
    assert st.claude_chat is None and st.claude_chat_info["key_file"] == {"present": True, "read": False, "mode": "0600"}
    st = _boot(monkeypatch, tmp_path, CLAUDE_CHAT_MODEL=MODEL, CLAUDE_CHAT_API_KEY_FILE=str(_key_file(tmp_path, mode=0o644)))
    assert st.claude_chat is not None and "chmod 600" in st.claude_chat_info["key_file"]["warning"]


# ── 챗 한 턴(가짜 전송) ──────────────────────────────────────────────────────────────────────────────────────────

THINK1, SIG1, TEXT1 = "A 부터 조회해야 한다 — 도구 lookup.", "c2lnLUEtYmluZGluZy0x==", "A 를 먼저 조회합니다. "
THINK2, SIG2, TEXT2 = "A 는 42. 이제 B 를 본다.", "c2lnLUItYmluZGluZy0y==", "A 는 42 이고, B 를 조회합니다. "
THINK3, SIG3, TEXT3 = "둘 다 42 — 같다.", "c2lnLUMtYmluZGluZy0z==", "A 와 B 는 둘 다 42 로 같습니다."


def _three_calls():
    return [_message(_thinking(0, THINK1, SIG1), _text(1, TEXT1), _tool(2, "toolu_A", "lookup", {"q": "A"}), stop="tool_use"),
            _message(_thinking(0, THINK2, SIG2), _text(1, TEXT2), _tool(2, "toolu_B", "lookup", {"q": "B"}), stop="tool_use"),
            _message(_thinking(0, THINK3, SIG3), _text(1, TEXT3))]


def _assistant(body, k):
    return [m for m in body["messages"] if m["role"] == "assistant"][k]


@needs_anthropic
def test_Claude_턴_도구_왕복_둘_생각_블록을_바이트_그대로_되돌린다(monkeypatch, tmp_path):
    claude, glm = _Claude(_three_calls()), _OpenAI([])
    st = _boot(monkeypatch, tmp_path, claude, glm, CLAUDE_CHAT_MODEL=MODEL)
    evs = _turn(monkeypatch, tmp_path, st)
    assert len(claude.seen) == 3 and not glm.raw, "세 호출 모두 Claude — 넘겨받는 쪽은 부르지 않았다"
    assert st.seen_ctx and set(st.seen_ctx) == {1_000_000 - 64_000}, "Claude 턴의 창은 CLAUDE_CHAT_CONTEXT_TOKENS − 출력 상한"
    b1, b2, b3 = claude.bodies
    for h, b in claude.seen:
        assert not {"temperature", "top_p", "top_k", "extra_body", "chat_template_kwargs", "reasoning_effort"} & set(b), b.keys()
        assert b["model"] == MODEL and b["max_tokens"] == 64000 and b["stream"] is True
        assert b["thinking"] == {"type": "adaptive", "display": "summarized", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        assert b["output_config"] == {"effort": "medium"} and b["cache_control"] == {"type": "ephemeral"} and b["fallbacks"] == "default"
        assert b.get("tool_choice") in (None, {"type": "auto"}), "강제 tool_choice 는 보내지 않는다"
        assert h["anthropic-beta"] == "thinking-binding-controls-2026-08-01,server-side-fallback-2026-07-01"
        assert h["x-api-key"] == FAKE_KEY and "x-session-affinity" not in h, "대화 머리(부하분산기용)는 Anthropic 에 싣지 않는다"
    # 도구 목록 · 시스템 글은 왕복마다 바이트까지 같다(캐시 · 생각 블록 묶임) — 앞부분은 덧붙이기만 한다.
    assert b1["system"] == b2["system"] == b3["system"] and b1["tools"] == b2["tools"] == b3["tools"]
    assert len(b1["system"]) == 1 and b1["system"][0]["type"] == "text" and b1["system"][0]["text"] == a._system_prompt()
    assert b1["system"][0]["cache_control"] == {"type": "ephemeral"}, "도구 + 시스템 글은 턴을 넘어 읽히게 표지를 단다"
    assert b2["messages"] == b3["messages"][:len(b2["messages"])], "앞 왕복의 메시지는 바뀌지 않는다(덧붙이기만)"
    want1 = [{"type": "thinking", "thinking": THINK1, "signature": SIG1}, {"type": "text", "text": TEXT1},
             {"type": "tool_use", "name": "lookup", "input": {"q": "A"}, "id": "toolu_A"}]
    want2 = [{"type": "thinking", "thinking": THINK2, "signature": SIG2}, {"type": "text", "text": TEXT2},
             {"type": "tool_use", "name": "lookup", "input": {"q": "B"}, "id": "toolu_B"}]
    assert _assistant(b2, 0)["content"] == want1
    assert _assistant(b3, 0)["content"] == want1 and _assistant(b3, 1)["content"] == want2
    results = [c for m in b3["messages"] if m["role"] == "user" and isinstance(m["content"], list)
               for c in m["content"] if c.get("type") == "tool_result"]
    assert [r["tool_use_id"] for r in results] == ["toolu_A", "toolu_B"] and "[A] 값은 42" in json.dumps(results, ensure_ascii=False)
    # 화면 — 생각 요약은 '생각 중' 칸, 글은 한 번씩만(넘겨받는 쪽이 비스트리밍이어도 겹치지 않는다).
    think = _deltas(evs, "reasoning")
    assert THINK1 in think and THINK2 in think and THINK3 in think
    body = _deltas(evs, "token")
    assert body.startswith(TEXT1 + TEXT2 + TEXT3) and all(body.count(t) == 1 for t in (TEXT1, TEXT2, TEXT3)), body
    assert [d["tool"] for e, d in evs if e == "status" and str(d.get("step", "")).startswith("도구 호출")] == ["lookup", "lookup"]
    meta = [d for e, d in evs if e == "meta"][-1]
    assert meta["models"] == {MODEL: 3}, "답한 모델 표시는 Claude"
    assert [d for e, d in evs if e == "result"][-1]["content"].startswith(TEXT1 + TEXT2 + TEXT3)
    assert [e for e, _ in evs][-1] == "done"


@needs_anthropic
def test_고르지_않은_사람의_턴은_LLM_요청이_바이트까지_같다(monkeypatch, tmp_path):
    def run(entitlements, **env):
        glm = _OpenAI([_glm_call("call_1", "A"), _glm_text("A 는 42 입니다.")])
        claude = _Claude([])
        st = _boot(monkeypatch, tmp_path, claude, glm, **env)
        evs = _turn(monkeypatch, tmp_path, st, entitlements=entitlements)
        assert not claude.seen, "Claude 를 부르지 않는다"
        assert set(st.seen_ctx) == {None}, "고르지 않은 턴은 창을 덮어쓰지 않는다"
        return glm.raw, _deltas(evs, "token"), [d for e, d in evs if e == "meta"][-1]["models"]

    off = run(["feat:thinking"])
    assert len(off[0]) == 2 and off[2] == {a.VLLM_MODEL: 2}
    assert run(["feat:thinking"], CLAUDE_CHAT_MODEL=MODEL) == off, "켜도 키가 없는 사람은 바이트까지 같다"
    assert run(None, CLAUDE_CHAT_MODEL=MODEL, CLAUDE_CHAT_FOR="all") == off, "권한 칸이 없는 옛 호출은 all 이어도 종전 그대로"


@needs_anthropic
def test_FOR_all_이면_권한_키가_없어도_Claude(monkeypatch, tmp_path):
    claude = _Claude([_message(_thinking(0, THINK3, SIG3), _text(1, TEXT3))])
    st = _boot(monkeypatch, tmp_path, claude, _OpenAI([]), CLAUDE_CHAT_MODEL=MODEL, CLAUDE_CHAT_FOR="all")
    evs = _turn(monkeypatch, tmp_path, st, entitlements=["feat:thinking"])
    assert len(claude.seen) == 1 and _deltas(evs, "token").startswith(TEXT3)


@needs_anthropic
def test_거절은_까닭을_본문에_남기고_거절_전의_도구_호출은_실행하지_않는다(monkeypatch, tmp_path):
    det = {"type": "refusal", "category": "cyber", "explanation": "보안 정책에 걸렸다"}
    claude = _Claude([_message(_thinking(0, THINK1, SIG1), _text(1, "일부 답 "), _tool(2, "toolu_A", "lookup", {"q": "A"}),
                               stop="refusal", details=det)])
    st = _boot(monkeypatch, tmp_path, claude, _OpenAI([]), CLAUDE_CHAT_MODEL=MODEL)
    evs = _turn(monkeypatch, tmp_path, st)
    body = _deltas(evs, "token")
    assert body.startswith("일부 답 ") and "거절(범주: cyber): 보안 정책에 걸렸다" in body, body
    assert len(claude.seen) == 1 and not [d for e, d in evs if e == "status" and "도구 호출" in str(d.get("step", ""))], \
        "거절된 응답의 도구 호출은 실행하지 않는다"
    assert "거절" in [d for e, d in evs if e == "result"][-1]["content"]


@needs_anthropic
def test_5xx_는_기본_챗_길로_넘기고_넘겨받는_쪽에는_OpenAI_꼴로_간다(monkeypatch, tmp_path):
    over = (529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    claude = _Claude([_three_calls()[0], over])
    glm = _OpenAI([_glm_text("GLM 이 이어서 답한다.")])
    st = _boot(monkeypatch, tmp_path, claude, glm, CLAUDE_CHAT_MODEL=MODEL)
    evs = _turn(monkeypatch, tmp_path, st)
    assert len(claude.seen) == 2 and len(glm.raw) == 1, "둘째 호출이 529 → 그 호출만 VLLM_* 로"
    raw = glm.raw[0].decode()
    for leak in ("signature", "hwax_claude_blocks", "reasoning_content", SIG1, THINK1, "cache_control", "fallbacks"):
        assert leak not in raw, f"넘겨받는 쪽에 Claude 전용 칸이 새면 안 된다 — {leak}"
    msgs = glm.bodies[0]["messages"]
    ai = [m for m in msgs if m["role"] == "assistant"]
    assert ai == [{"role": "assistant", "content": TEXT1,
                   "tool_calls": [{"type": "function", "id": "toolu_A", "function": {"name": "lookup", "arguments": '{"q": "A"}'}}]}]
    assert [m["tool_call_id"] for m in msgs if m["role"] == "tool"] == ["toolu_A"]
    body = _deltas(evs, "token")
    assert body.startswith(TEXT1 + "GLM 이 이어서 답한다.")
    meta = [d for e, d in evs if e == "meta"][-1]
    assert meta["models"] == {MODEL: 2, a.VLLM_MODEL: 1}, "넘겼으면 넘긴 모델도 보인다"
    info = st.claude_chat_info
    assert info["fallback"]["count"] == 1 and info["fallback"]["last_at"] and info["last_error"].startswith("OverloadedError")


@needs_anthropic
@pytest.mark.parametrize("status,etype,exc", [(429, "rate_limit_error", "RateLimitError"),
                                              (401, "authentication_error", "AuthenticationError"),
                                              (403, "permission_error", "PermissionDeniedError"),
                                              (500, "api_error", "InternalServerError")])
def test_429_401_403_500_도_넘긴다(monkeypatch, tmp_path, status, etype, exc):
    claude = _Claude([(status, {"type": "error", "error": {"type": etype, "message": "x"}})])
    glm = _OpenAI([_glm_text("기본 길의 답.")])
    st = _boot(monkeypatch, tmp_path, claude, glm, CLAUDE_CHAT_MODEL=MODEL)
    evs = _turn(monkeypatch, tmp_path, st)
    assert len(glm.raw) == 1 and _deltas(evs, "token").startswith("기본 길의 답.")
    assert st.claude_chat_info["last_error"].startswith(exc) and st.claude_chat_info["fallback"]["count"] == 1


@needs_anthropic
def test_400_은_넘기지_않는다(monkeypatch, tmp_path):
    claude = _Claude([(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad shape"}})])
    glm = _OpenAI([])
    st = _boot(monkeypatch, tmp_path, claude, glm, CLAUDE_CHAT_MODEL=MODEL)
    evs = _turn(monkeypatch, tmp_path, st)
    assert not glm.raw and any(e == "error" for e, _ in evs), "설정 오류는 숨기지 않는다"
    assert st.claude_chat_info["fallback"]["count"] == 0 and st.claude_chat_info["last_error"].startswith("BadRequestError")


@needs_anthropic
def test_자기검증_검토자는_같은_Claude_로_가고_생각_끔은_effort_low(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAT_SELF_CHECK", "1")
    draft = "M3 나사의 권장 체결 토크는 12.5 N·m 입니다."
    review = ("[판정] 고침\n[고친 점]\n- 12.5 는 근거가 없어 추정으로 표시\n[바꿀 곳]\n[원문]\n12.5 N·m 입니다.\n"
              "[새 글]\n12.5 N·m (근거 없음 — 추정) 입니다.")
    claude = _Claude([_message(_thinking(0, THINK3, SIG3), _text(1, draft)), _message(_thinking(0, "검토", "c2ln"), _text(1, review))])
    st = _boot(monkeypatch, tmp_path, claude, _OpenAI([]), CLAUDE_CHAT_MODEL=MODEL)
    evs = _turn(monkeypatch, tmp_path, st, message="M3 나사 체결 토크는?")
    assert len(claude.seen) == 2
    rb = claude.bodies[1]
    assert rb["output_config"] == {"effort": "low"} and rb["max_tokens"] == a._SC_OUT_CAP, "검토자 — 생각을 끌 수 없어 effort low"
    assert not {"extra_body", "chat_template_kwargs", "temperature"} & set(rb) and "tools" not in rb
    assert "[답 초안]" in json.dumps(rb["messages"], ensure_ascii=False)
    assert _deltas(evs, "token").startswith("M3 나사의 권장 체결 토크는 12.5 N·m (근거 없음 — 추정) 입니다.")
    meta = [d for e, d in evs if e == "meta"][-1]
    assert meta["self_check"]["status"] == "fixed" and meta["models"] == {MODEL: 2}


@needs_anthropic
def test_Claude_턴은_그림을_탐침_없이_싣는다(monkeypatch, tmp_path):
    import base64

    import vision as _vision  # noqa: PLC0415
    png = _vision.red_png() if hasattr(_vision, "red_png") else None
    if png is None:
        pytest.skip("vision.red_png 가 없다")
    monkeypatch.setattr(a, "_chat_vision", lambda _app: pytest.fail("Claude 턴은 그림 탐침을 부르지 않는다"))
    claude = _Claude([_message(_thinking(0, THINK3, SIG3), _text(1, "빨간 그림입니다."))])
    st = _boot(monkeypatch, tmp_path, claude, _OpenAI([]), CLAUDE_CHAT_MODEL=MODEL)
    evs = _turn(monkeypatch, tmp_path, st, message="이 그림은?",
                images=[{"name": "r.png", "mime": "image/png", "data": base64.b64encode(png).decode()}])
    user = claude.bodies[0]["messages"][-1]["content"]
    imgs = [c for c in user if isinstance(c, dict) and c.get("type") == "image"]
    assert imgs and imgs[0]["source"]["type"] == "base64" and imgs[0]["source"]["media_type"] == "image/png"
    assert _deltas(evs, "token").startswith("빨간 그림입니다.")


# ── 어댑터(서버 대체 모델 경계 · 호출 인자) ──────────────────────────────────────────────────────────────────────────

@needs_anthropic
def test_서버_대체_모델_경계_앞의_생각과_도구_호출은_되돌리지_않고_경계는_그_자리에(monkeypatch):
    import chat_claude as cc  # noqa: PLC0415

    claude = _Claude([
        _message(_thinking(0, "거절당한 모델의 생각", "c2lnLWRlY2xpbmVk"), _text(1, "부분 글 "), _tool(2, "toolu_X", "lookup", {"q": "X"}),
                 _fallback(3), _thinking(4, THINK1, SIG1), _text(5, TEXT1), _tool(6, "toolu_A", "lookup", {"q": "A"}),
                 stop="tool_use", model="claude-opus-5"),
        _message(_thinking(0, THINK2, SIG2), _text(1, "끝."))])
    cfg = {"for": "all", "effort": "medium", "max_tokens": 64000, "ctx_tokens": 1_000_000, "server_fallback": True}
    llm = cc.build(model=MODEL, key=FAKE_KEY, cfg=cfg, timeout=httpx.Timeout(30, connect=5), read_s=30,
                   http_client=httpx.AsyncClient(transport=httpx.MockTransport(claude))).bind_tools([lookup])
    msgs = [SystemMessage("sys"), HumanMessage("질문")]

    async def go():
        first = None
        async for c in llm.astream(msgs):
            first = c if first is None else first + c
        ai = AIMessage(content=first.content, additional_kwargs=first.additional_kwargs, tool_calls=first.tool_calls,
                       response_metadata=first.response_metadata)
        await llm.ainvoke(msgs + [ai, ToolMessage("[A] 값은 42 이다", tool_call_id="toolu_A")])
        return first
    first = asyncio.run(go())
    assert [tc["id"] for tc in first.tool_calls] == ["toolu_A"], "경계 앞의 tool_use 는 실행하지 않는다(되돌리지 않으므로)"
    assert first.content == "부분 글 " + TEXT1 and first.response_metadata["model_name"] == "claude-opus-5"
    replay = _assistant(claude.bodies[1], 0)["content"]
    assert replay == [{"type": "text", "text": "부분 글 "},
                      {"type": "fallback", "from": {"model": MODEL}, "to": {"model": "claude-opus-5"},
                       "trigger": {"type": "refusal", "category": "cyber"}},
                      {"type": "thinking", "thinking": THINK1, "signature": SIG1}, {"type": "text", "text": TEXT1},
                      {"type": "tool_use", "name": "lookup", "input": {"q": "A"}, "id": "toolu_A"}]


@needs_anthropic
def test_호출_인자는_Anthropic_꼴로_바꾼다():
    import chat_claude as cc  # noqa: PLC0415

    kw = cc._anthropic_kwargs({"max_tokens": 4096, "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
                               "temperature": 0, "top_p": 1, "reasoning_effort": "xhigh", "tool_choice": {"type": "any"},
                               "output_config": {"format": {"type": "json_schema"}}})
    assert kw == {"max_tokens": 4096, "tool_choice": {"type": "auto"},
                  "output_config": {"format": {"type": "json_schema"}, "effort": "low"}}
    assert cc._anthropic_kwargs({"max_completion_tokens": 10, "tool_choice": {"type": "tool", "name": "x",
                                                                               "disable_parallel_tool_use": True}}) == \
        {"max_tokens": 10, "tool_choice": {"type": "auto", "disable_parallel_tool_use": True}}
    assert cc._anthropic_kwargs({"chat_template_kwargs": {"enable_thinking": True}}) == {}, "생각 켬은 손대지 않는다"


# ── 사람별 LLM 토큰 — Claude 턴도 센다(dev 11-22 — 두 갈래가 나란히 지어져 기록기가 Claude LLM 에 안 붙었다) ──────────────────────────
def test_Claude_턴의_토큰을_사람_종류_모델로_센다(monkeypatch, tmp_path):
    import usage_tokens as ut

    monkeypatch.setenv("USAGE_TOKENS_DIR", str(tmp_path / "usage"))
    ut._reset_for_tests()
    # 진짜 꼴 — message_delta 의 usage 는 누적 입력 · 캐시 읽기를 함께 싣고(SDK MessageDeltaUsage), langchain-anthropic 은 message_start 의 usage 를
    # 버리고 이것을 쓴다. 이 파일의 _message 는 끝 사건에 출력만 실어(옛 꼴) 입력이 0 으로 보였다 — 이 시험만 진짜 꼴로 둔다.
    evs = _message(_text(0, "답이다."))
    evs[-2] = {**evs[-2], "usage": {"input_tokens": 12, "cache_read_input_tokens": 5, "output_tokens": 30}}
    st = _boot(monkeypatch, tmp_path, claude=_Claude([evs]), CLAUDE_CHAT_MODEL=MODEL, CLAUDE_CHAT_FOR="all")
    cbs = [type(c).__name__ for c in (st.claude_chat.runnable.callbacks or [])]
    assert "UsageTokensRecorder" in cbs, f"Claude LLM 에 사용량 기록기가 없다 — {cbs}"
    import contextvars

    def run():
        ut.tag(user="kim@corp.test", kind="chat")     # /chat 창구가 거는 표지 — 복사한 문맥 안에서만(다른 시험으로 새지 않게)
        return _turn(monkeypatch, tmp_path, st)
    evs = contextvars.copy_context().run(run)
    assert _deltas(evs, "token").strip() == "답이다.", evs
    with ut._LOCK:
        rows = {k: list(v) for k, v in ut._ACC.items()}
    mine = {k: v for k, v in rows.items() if k[1] == "kim@corp.test" and k[3] == MODEL}
    assert mine, f"Claude 턴이 기록되지 않았다 — {rows}"
    (_day, _u, kind, _m), (calls, unknown, tin, tout, _tc) = next(iter(mine.items()))
    # 입력은 캐시 읽기를 포함한 합이다(langchain usage_metadata 의 관례 — OpenAI prompt_tokens 와 같은 셈) — 12 + 5. 캐시 읽기는 따로 5.
    assert kind == "chat" and calls >= 1 and unknown == 0 and (tin, tout) == (17, 30) and _tc == 5, mine



# ── 사람이 고르는 모델 · 생각 깊이(CC-8 — 사용자 요청 11-22) ──────────────────────────────────────────────────────────────

HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"


@needs_anthropic
@pytest.mark.parametrize("env,models,efforts,why", [
    ({}, (MODEL,), ("low", "medium", "high", "xhigh", "max"), None),
    ({"CLAUDE_CHAT_MODELS": f"{SONNET}, {HAIKU}"}, (MODEL, SONNET, HAIKU), None, None),
    ({"CLAUDE_CHAT_MODELS": f"{HAIKU},{MODEL},{HAIKU}"}, (HAIKU, MODEL), None, None),
    ({"CLAUDE_CHAT_EFFORTS": "high,medium"}, (MODEL,), ("medium", "high"), None),
    ({"CLAUDE_CHAT_MODELS": "gpt-5"}, (), (), "모델 이름 꼴"),
    ({"CLAUDE_CHAT_EFFORTS": "medium,ultra"}, (), (), "CLAUDE_CHAT_EFFORTS"),
    ({"CLAUDE_CHAT_EFFORTS": "high,max"}, (), (), "CLAUDE_CHAT_EFFORTS 에 없다"),
])
def test_고를_수_있는_목록은_설정에서_닫힌_쪽으로_읽는다(monkeypatch, env, models, efforts, why):
    import chat_claude as cc

    for k in ("CLAUDE_CHAT_MODELS", "CLAUDE_CHAT_EFFORTS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    got_m, got_e, got_why = cc.choices(MODEL, "medium")
    assert (got_why is None) == (why is None) and (why is None or why in got_why), got_why
    assert got_m == models, "적은 차례 · 기본이 없으면 맨 앞 · 겹침은 한 번"
    if efforts is not None:
        assert got_e == efforts, "effort 는 낮은 쪽부터 · 비면 다섯 다"


@needs_anthropic
def test_모르는_목록이면_끄고_까닭을_남긴다(monkeypatch, tmp_path):
    st = _boot(monkeypatch, tmp_path, CLAUDE_CHAT_MODEL=MODEL, CLAUDE_CHAT_MODELS="opus")
    assert st.claude_chat is None and st.claude_chat_pick is None and "CLAUDE_CHAT_MODELS" in st.claude_chat_info["error"]


@needs_anthropic
def test_Haiku_에는_서버_대체_모델을_싣지_않는다(monkeypatch, tmp_path):
    st = _boot(monkeypatch, tmp_path, CLAUDE_CHAT_MODEL=MODEL, CLAUDE_CHAT_MODELS=f"{MODEL},{SONNET},{HAIKU}")
    assert st.claude_chat_info["models"] == [MODEL, SONNET, HAIKU] and st.claude_chat_info["no_server_fallback"] == [HAIKU]
    h = st.claude_chat_pick.get(HAIKU, "low").runnable
    assert h.model == HAIKU and h.output_config == {"effort": "low"}
    assert h.betas == ["thinking-binding-controls-2026-08-01"] and "fallbacks" not in h.model_kwargs, \
        "Haiku 5.5 는 fallbacks 를 400 으로 거절한다 — 칸도 beta 머리도 싣지 않는다"
    so = st.claude_chat_pick.get(SONNET, "high").runnable
    assert so.model_kwargs.get("fallbacks") == "default" and "server-side-fallback-2026-07-01" in so.betas
    assert st.claude_chat_pick.get(MODEL, "medium") is st.claude_chat, "기본 쌍은 기동 때 지은 그것"
    assert st.claude_chat_pick.get(HAIKU, "low") is st.claude_chat_pick.get(HAIKU, "low"), "한 번 지은 쌍은 다시 쓴다"
    assert st.claude_chat_pick.get(HAIKU, "low").fallbacks == st.claude_chat.fallbacks, "넘김 대상은 같은 기본 챗 길"


@needs_anthropic
def test_고른_모델과_생각_깊이로_묻고_답_아래에_그_값이_보인다(monkeypatch, tmp_path):
    claude = _Claude([_message(_text(0, "짧은 답."), model=HAIKU)])
    st = _boot(monkeypatch, tmp_path, claude, _OpenAI([]), CLAUDE_CHAT_MODEL=MODEL, CLAUDE_CHAT_MODELS=f"{MODEL},{HAIKU}",
               CLAUDE_CHAT_FOR="all", CHAT_SELF_CHECK="0")
    evs = _turn(monkeypatch, tmp_path, st, claude_model=HAIKU, claude_effort="low")
    (h, b), = claude.seen
    assert b["model"] == HAIKU and b["output_config"] == {"effort": "low"} and "fallbacks" not in b
    assert "server-side-fallback" not in h["anthropic-beta"]
    meta = [d for e, d in evs if e == "meta"][-1]
    assert meta["chat_model"] == HAIKU and meta["effort"] == "low" and meta["thinking"] is True and "claude_note" not in meta, \
        "답 옆의 생각 깊이는 Claude 가 고른 값이다(종전에는 Qwen 의 값이 실렸다)"


@needs_anthropic
def test_목록_밖의_값은_기본으로_돌고_알린다(monkeypatch, tmp_path):
    claude = _Claude([_message(_text(0, "답."))])
    st = _boot(monkeypatch, tmp_path, claude, _OpenAI([]), CLAUDE_CHAT_MODEL=MODEL, CLAUDE_CHAT_EFFORTS="low,medium",
               CLAUDE_CHAT_FOR="all", CHAT_SELF_CHECK="0")
    evs = _turn(monkeypatch, tmp_path, st, claude_model="claude-fable-5-2", claude_effort="max")
    (_h, b), = claude.seen
    assert b["model"] == MODEL and b["output_config"] == {"effort": "medium"}, "목록 밖은 짐작하지 않고 박스 기본"
    meta = [d for e, d in evs if e == "meta"][-1]
    assert meta["chat_model"] == MODEL and meta["effort"] == "medium"
    assert "claude-fable-5-2" in meta["claude_note"] and "max" in meta["claude_note"], meta


@needs_anthropic
def test_고르지_않으면_기본이고_고르는_사람이_아니면_고른_값을_읽지_않는다(monkeypatch, tmp_path):
    claude = _Claude([_message(_text(0, "답."))])
    st = _boot(monkeypatch, tmp_path, claude, _OpenAI([]), CLAUDE_CHAT_MODEL=MODEL, CLAUDE_CHAT_MODELS=f"{MODEL},{HAIKU}",
               CHAT_SELF_CHECK="0")
    _turn(monkeypatch, tmp_path, st)
    assert claude.bodies[0]["model"] == MODEL and claude.bodies[0]["output_config"] == {"effort": "medium"}
    app_ = types.SimpleNamespace(state=st)
    req = a.ChatRequest(message="q", user_email="kim@corp.test", entitlements=["feat:thinking"], claude_model=HAIKU)
    assert a._claude_choice(app_, req) is None and a._claude_for(app_, req) is None, "권한 없는 사람의 고른 값은 Claude 를 열지 않는다"


def test_캐시_열쇠의_Claude_칸은_고른_쌍마다_다르다():
    tok = a._turn_claude_pick.set((HAIKU, "low", None))
    try:
        assert a._claude_key_slot() == f"claude:{HAIKU}:low"
    finally:
        a._turn_claude_pick.reset(tok)
    assert a._claude_key_slot() == "claude", "고른 쌍이 없는 턴(시험의 가짜 턴)은 종전 칸"


@needs_anthropic
def test_chat_models_는_고를_목록만_싣고_키는_없다(monkeypatch, tmp_path):
    st = _boot(monkeypatch, tmp_path)
    monkeypatch.setattr(a.app.state, "claude_chat_info", st.claude_chat_info, raising=False)
    assert a.chat_models() == {"claude": {"enabled": False}}
    st = _boot(monkeypatch, tmp_path, CLAUDE_CHAT_MODEL=MODEL, CLAUDE_CHAT_MODELS=f"claude-fable-5-2,{MODEL},{HAIKU}",
               CLAUDE_CHAT_EFFORT="high")
    monkeypatch.setattr(a.app.state, "claude_chat_info", st.claude_chat_info, raising=False)
    got = a.chat_models()
    assert got == {"claude": {"enabled": True, "for": "feat:chat-claude",
                              "models": [{"id": "claude-fable-5-2", "label": "Claude Fable 5.2"},
                                         {"id": MODEL, "label": "Claude Opus 5.5"}, {"id": HAIKU, "label": "Claude Haiku 5.5"}],
                              "efforts": ["low", "medium", "high", "xhigh", "max"], "default_model": MODEL, "default_effort": "high"}}
    assert FAKE_KEY not in json.dumps(got) and "key_file" not in json.dumps(got)
