# 웹 챗의 Claude(Anthropic API) 모델 — 고른 사람의 챗 턴만 이 모델로 가고, 생각 블록을 도구 왕복 사이에 받은 그대로 되돌린다
"""웹 챗의 Claude 길(사용자 지시 2026-11-22 — dev 는 Claude 가 정본, cae00 은 운영자가 지정한 사람만 Claude).

app.py 가 CLAUDE_CHAT_MODEL 이 있을 때만 이 모듈을 들인다(langchain-anthropic 은 requirements-anthropic.txt — 꺼진 박스는 꾸러미가 없어도 돈다).
고르기(누가 Claude 인가) · 넘김(실패하면 그 박스의 기본 챗 길) · /health 는 app.py 가 맡고, 이 모듈은 설정 읽기 · 키 파일 · 모델 어댑터다.

어댑터(ClaudeChatLLM — ChatAnthropic 하위 클래스)가 하는 일. 앱의 나머지 코드는 챗 모델의 content 를 글(str)로, 생각 글을
additional_kwargs.reasoning_content 로 읽는다(Qwen 길과 같은 꼴). Claude 의 응답은 블록 목록(생각 · 글 · 도구 호출)이고 생각 블록은 서명이 붙어
도구 왕복마다 받은 그대로 되돌려야 한다. 앱 본문을 넓게 고치지 않고 이 한 곳에서 맞춘다.
  · 받기 — 스트림 조각마다 글 블록은 content 글로, 생각 요약은 reasoning_content 로, 원래 블록 조각(인덱스 · 서명 포함)은
    additional_kwargs[BLOCKS] 에 둔다. langchain_core 의 조각 합치기(merge_lists)가 같은 index 의 사전을 합치고 글 칸을 이어 붙이므로
    한 호출이 끝나면 블록이 받은 그대로 모인다.
  · 도구 호출 조각은 메시지 끝(message_delta)까지 붙잡았다가 낸다 — 거절(refusal)이면 버리고, 서버 대체 모델이 끼었으면(fallback 블록)
    그 경계 앞의 것을 버린다(경계 앞의 tool_use 는 되돌리지 않는 규칙이라 실행하면 짝 없는 tool_result 가 된다).
  · 되돌리기 — 요청을 지을 때 BLOCKS 를 가진 AIMessage 의 content 를 그 블록 목록으로 바꾼다(경계 앞의 생각 · 도구 호출은 뺀다).
  · OpenAI 전용 호출 인자(extra_body · chat_template_kwargs · reasoning_effort · max_completion_tokens · temperature · top_p · top_k)는
    Anthropic 요청에 싣지 않는다 — 같은 호출이 넘김 대상(OpenAI 호환)에게는 그대로 간다. '생각 끔'(enable_thinking=false)은 Opus 5.5 에서
    끌 수 없어 effort low 로 바꾼다. 강제 tool_choice(any · tool)는 auto 로 바꾼다(이 모델들에서 400).
  · 거절(stop_reason refusal)은 빈 답으로 두지 않고 까닭을 답 글 끝에 붙인다. max_tokens 끊김은 finish_reason 'length' 로 알린다(앱의 절단
    경고 · 자기검증의 끊김 판정이 읽는 칸).
"""
from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Iterator
from functools import cached_property
from pathlib import Path
from typing import Any

import anthropic
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

FEATURE = "feat:chat-claude"                 # 포털 합성 권한 — '*' 에 딸려 오지 않고 개별 허가로만 열린다(포털 세션이 짓는다)
FOR_VALUES = ("all", FEATURE)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
BLOCKS = "hwax_claude_blocks"                # additional_kwargs 칸 — 받은 블록 조각(되돌리기의 원본)
THINKING_BETA = "thinking-binding-controls-2026-08-01"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CACHE = {"type": "ephemeral"}                # 5분 캐시 — 최상위 자동(SDK 0.125 beta create 의 cache_control) + 마지막 시스템 블록
_OPENAI_ONLY = ("extra_body", "chat_template_kwargs", "reasoning_effort", "temperature", "top_p", "top_k")
_DROP_BEFORE_FALLBACK = ("thinking", "redacted_thinking", "tool_use")

# 넘기는 예외 — 연결 실패 · 시간 초과 · 429 · 401/403 · 5xx(529 Overloaded 포함). 400(요청 꼴 · 창 초과)과 404(모델 이름)는 설정 오류라
# 넘기지 않는다(숨기지 않는다 — 챗 전용 LLM 의 규칙과 같다, HWAXPortal docs/chat-llm-stcx CL-D3 · CL-D8).
FALLBACK_ERRORS = (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.RateLimitError,
                   anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.InternalServerError,
                   anthropic.OverloadedError, anthropic.ServiceUnavailableError, anthropic.DeadlineExceededError,
                   asyncio.TimeoutError, TimeoutError)


def _int(name: str, default: int) -> tuple[int, str | None]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default, None
    try:
        v = int(raw.replace(",", "").replace("_", ""))
    except ValueError:
        return default, f"{name}={raw!r} 는 정수가 아니다"
    return (v, None) if v > 0 else (default, f"{name} 은 0 보다 커야 한다")


def settings() -> tuple[dict, str | None]:
    """(설정, 켜지 못하는 까닭 또는 None). 모르는 값은 짐작하지 않고 끈다 — 그 사람들은 기본 챗 길로 간다(닫힌 쪽)."""
    who = (os.environ.get("CLAUDE_CHAT_FOR") or FEATURE).strip()
    effort = (os.environ.get("CLAUDE_CHAT_EFFORT") or "medium").strip().lower()
    max_tokens, e1 = _int("CLAUDE_CHAT_MAX_TOKENS", 64000)
    ctx, e2 = _int("CLAUDE_CHAT_CONTEXT_TOKENS", 1_000_000)
    sfb = (os.environ.get("CLAUDE_CHAT_SERVER_FALLBACK") or "1").strip() != "0"
    cfg = {"for": who, "effort": effort, "max_tokens": max_tokens, "ctx_tokens": ctx, "server_fallback": sfb}
    if who not in FOR_VALUES:
        return cfg, f"CLAUDE_CHAT_FOR={who!r} 를 모른다 — all 또는 {FEATURE} 만 받는다"
    if effort not in EFFORTS:
        return cfg, f"CLAUDE_CHAT_EFFORT={effort!r} 를 모른다 — {' · '.join(EFFORTS)}"
    for e in (e1, e2):
        if e:
            return cfg, e
    if max_tokens * 2 > ctx:
        return cfg, f"CLAUDE_CHAT_MAX_TOKENS({max_tokens:,})가 창(CLAUDE_CHAT_CONTEXT_TOKENS {ctx:,})의 절반보다 크다"
    return cfg, None


_MODEL_RE = re.compile(r"claude-[a-z0-9]+(?:-[a-z0-9]+)*")


def _split(raw: str) -> list[str]:
    return [x for x in (p.strip() for p in raw.replace(";", ",").split(",")) if x]


def choices(default_model: str, default_effort: str) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    """사람이 웹 챗에서 고를 수 있는 (모델들, effort 들, 켜지 못하는 까닭 또는 None) — CLAUDE_CHAT_MODELS · CLAUDE_CHAT_EFFORTS(쉼표).
    모델은 적은 차례대로이고 기본 모델(CLAUDE_CHAT_MODEL)이 없으면 맨 앞에 둔다 — 비면 기본 하나뿐이다(운영 박스는 운영자가 적어야 넓어진다).
    effort 는 비면 다섯 다(low ~ max — 지금 고를 수 있는 Claude 5.x 모델은 모두 다섯을 받는다). 모르는 값은 짐작하지 않고 끈다(닫힌 쪽)."""
    listed = [m.lower() for m in _split(os.environ.get("CLAUDE_CHAT_MODELS") or "")]
    for m in listed:
        if len(m) > 64 or not _MODEL_RE.fullmatch(m):
            return (), (), f"CLAUDE_CHAT_MODELS 의 {m!r} 는 모델 이름 꼴(claude-…)이 아니다"
    models = tuple(dict.fromkeys(listed if default_model in listed else [default_model, *listed]))
    raw_e = [e.lower() for e in _split(os.environ.get("CLAUDE_CHAT_EFFORTS") or "")]
    for e in raw_e:
        if e not in EFFORTS:
            return (), (), f"CLAUDE_CHAT_EFFORTS 의 {e!r} 를 모른다 — {' · '.join(EFFORTS)}"
    efforts = tuple(e for e in EFFORTS if e in raw_e) if raw_e else EFFORTS
    if default_effort not in efforts:
        return (), (), f"CLAUDE_CHAT_EFFORT={default_effort!r} 가 CLAUDE_CHAT_EFFORTS 에 없다"
    return models, efforts, None


def server_fallback_ok(model: str) -> bool:
    """그 모델이 서버 쪽 대체 모델(fallbacks)을 받는가 — Haiku 는 받지 않는다(Haiku 5.5 — 'no server-side fallback', fallbacks 를 실으면 400).
    Opus 5.5 · Sonnet 5.5 · Fable 5.2 는 받는다. 받지 않는 모델에는 그 칸과 beta 머리를 싣지 않는다(거절은 종전처럼 본문에 까닭을 남긴다)."""
    return not model.startswith("claude-haiku-")




def read_key(path: str) -> tuple[str | None, dict, str | None]:
    """(키 또는 None, /health 에 실을 것, 못 읽은 까닭). 키 값은 돌려주기만 하고 어디에도 찍지 않는다 — /health 에는 있음 · 읽음 · 권한만
    (경로도 싣지 않는다 — 박스 배치가 드러난다)."""
    info: dict = {"present": False, "read": False}
    if not (path or "").strip():
        return None, info, "CLAUDE_CHAT_API_KEY_FILE 이 비었다"
    p = Path(path.strip()).expanduser()
    try:
        st = p.stat()
    except OSError:
        return None, info, "키 파일이 없다(CLAUDE_CHAT_API_KEY_FILE)"
    info["present"] = True
    info["mode"] = format(st.st_mode & 0o777, "04o")
    try:
        key = p.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        return None, info, f"키 파일을 읽지 못했다({type(exc).__name__})"
    if not key:
        return None, info, "키 파일이 비었다"
    info["read"] = True
    if st.st_mode & 0o077:
        info["warning"] = "다른 사용자가 읽을 수 있다 — chmod 600"
    return key, info, None


def _anthropic_kwargs(kw: dict) -> dict:
    """호출 인자를 Anthropic 요청 꼴로 — OpenAI 전용 칸은 빼고, '생각 끔' 은 effort low 로, 강제 tool_choice 는 auto 로."""
    kw = dict(kw)
    eb = kw.get("extra_body")
    ctk = kw.get("chat_template_kwargs") or (eb.get("chat_template_kwargs") if isinstance(eb, dict) else None)
    for k in _OPENAI_ONLY:
        kw.pop(k, None)
    if "max_completion_tokens" in kw:
        kw["max_tokens"] = kw.pop("max_completion_tokens")
    if isinstance(ctk, dict) and ctk.get("enable_thinking") is False:
        kw["output_config"] = {**(kw.get("output_config") or {}), "effort": "low"}
    tc = kw.get("tool_choice")                # ChatAnthropic.bind_tools 가 이름 · 'any' 를 이미 사전으로 바꿔 둔다
    if isinstance(tc, dict) and tc.get("type") in ("any", "tool"):
        kw["tool_choice"] = {"type": "auto", **{k: v for k, v in tc.items() if k == "disable_parallel_tool_use"}}
    return kw


def _replay(m):
    """받아 둔 블록이 있는 AIMessage → 그 블록 목록을 content 로(받은 그대로). 서버 대체 모델의 경계(fallback 블록)가 있으면 마지막 경계 앞의
    생각 · 도구 호출은 뺀다(되돌리기 규칙). 도구 호출로 실행하지 않은 tool_use(거절 등)도 뺀다 — 짝 없는 tool_use 는 400 이다."""
    if not isinstance(m, AIMessage):
        return m
    blocks = (m.additional_kwargs or {}).get(BLOCKS)
    if not isinstance(blocks, list) or not blocks:
        return m
    order = sorted((b for b in blocks if isinstance(b, dict)), key=lambda b: b.get("index", 0))
    cut = max((i for i, b in enumerate(order) if b.get("type") == "fallback"), default=-1)
    called = {tc.get("id") for tc in m.tool_calls or []}
    out: list = []
    for i, b in enumerate(order):
        t = b.get("type")
        if i < cut and t in _DROP_BEFORE_FALLBACK:
            continue
        blk = {k: v for k, v in b.items() if k not in ("index", "partial_json") and v is not None}
        if t == "thinking":
            blk.setdefault("thinking", "")            # display omitted 이면 글이 빈 채로 시작 사건에만 있었다
        elif t == "text":
            if not str(blk.get("text") or "").strip():
                continue                              # 빈 글 블록은 API 가 받지 않는다(받을 때도 오지 않는다)
        elif t == "tool_use" and blk.get("id") not in called:
            continue
        out.append(blk)
    return m.model_copy(update={"content": out})


def _refusal_note(details: dict | None) -> str:
    d = details or {}
    cat = d.get("category") or "밝히지 않음"
    why = str(d.get("explanation") or "").strip()
    return (f"\n\n[Claude 가 이 요청에 답하지 않았습니다 — 거절(범주: {cat})"
            + (f": {why}" if why else "") + ". 질문을 바꾸어 다시 보내거나 관리자에게 알려 주세요.]")


class _Turn:
    """한 번의 스트림 호출 상태 — 도구 호출 조각을 메시지 끝까지 붙잡고 서버 대체 모델의 경계를 기억한다."""

    def __init__(self) -> None:
        self.calls: list = []
        self.cut = -1

    def chunk(self, gen: ChatGenerationChunk) -> ChatGenerationChunk:
        m = gen.message
        blocks = [dict(b) for b in m.content if isinstance(b, dict)] if isinstance(m.content, list) else []
        text = "".join(b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str))
        think = "".join(b["thinking"] for b in blocks if b.get("type") == "thinking" and isinstance(b.get("thinking"), str))
        for b in blocks:
            if b.get("type") == "fallback" and isinstance(b.get("index"), int):
                self.cut = max(self.cut, b["index"])
        ak = dict(m.additional_kwargs or {})
        if blocks:
            ak[BLOCKS] = blocks
        if think:
            ak["reasoning_content"] = think
        self.calls += list(getattr(m, "tool_call_chunks", None) or [])
        md = dict(m.response_metadata or {})
        calls: list = []
        stop = md.get("stop_reason")
        if stop:
            if stop == "refusal":
                text += _refusal_note(md.get("stop_details"))
            else:
                calls = _merged_calls([c for c in self.calls if (c.get("index") if isinstance(c.get("index"), int) else -1) > self.cut])
            self.calls = []
            if stop == "max_tokens":
                md["finish_reason"] = "length"
        out = AIMessageChunk(content=text, additional_kwargs=ak, tool_call_chunks=calls, response_metadata=md,
                             usage_metadata=m.usage_metadata, id=m.id, chunk_position=m.chunk_position)
        return ChatGenerationChunk(message=out, generation_info=gen.generation_info)


def _merged_calls(chunks: list) -> list:
    """같은 index 의 도구 호출 조각을 하나로(이름 · id 는 처음 것, 인자 글은 잇는다)."""
    by: dict = {}
    for c in chunks:
        k = c.get("index")
        if k not in by:
            by[k] = {"type": "tool_call_chunk", "index": k, "id": c.get("id"), "name": c.get("name"), "args": c.get("args") or ""}
        else:
            d = by[k]
            d["id"] = d["id"] or c.get("id")
            d["name"] = d["name"] or c.get("name")
            d["args"] += c.get("args") or ""
    return list(by.values())


class ClaudeChatLLM(ChatAnthropic):
    """Anthropic 공식 SDK(langchain-anthropic)의 ChatAnthropic — 앱이 읽는 꼴(content 글 · reasoning_content)로 맞추고 생각 블록을 되돌린다."""

    # httpx.Timeout — 연결은 LLM_CONNECT_TIMEOUT_S, 읽기는 LLM_TIMEOUT_S(챗 기본 LLM 과 같은 층, 바깥 한도보다 안쪽). 시험은 가짜 전송을 단
    # httpx.AsyncClient 를 준다(hwax_http_client) — 실제 API 에 닿지 않는다.
    hwax_timeout: Any = None
    hwax_http_client: Any = None

    @cached_property
    def _async_client(self) -> anthropic.AsyncClient:
        params = dict(self._client_params)
        if self.hwax_timeout is not None:
            params["timeout"] = self.hwax_timeout
        if self.hwax_http_client is not None:
            params["http_client"] = self.hwax_http_client
        return anthropic.AsyncClient(**params)

    def _get_request_payload(self, input_, *, stop=None, **kwargs) -> dict:
        messages = [_replay(m) for m in self._convert_input(input_).to_messages()]
        payload = super()._get_request_payload(messages, stop=stop, **_anthropic_kwargs(kwargs))
        # 캐시 표지 둘 — 최상위 자동(늘어나는 대화 꼬리 · 한 턴의 도구 왕복)과 마지막 시스템 블록(도구 목록 + 시스템 글 — 턴이 바뀌어도 같은
        # 앞부분이라 다음 턴도 읽는다). 표지를 달거나 옮겨도 생각 블록의 묶임은 깨지지 않는다.
        sysm = payload.get("system")
        if isinstance(sysm, str) and sysm:
            payload["system"] = [{"type": "text", "text": sysm, "cache_control": dict(CACHE)}]
        elif isinstance(sysm, list) and sysm and isinstance(sysm[-1], dict) and sysm[-1].get("type") == "text":
            payload["system"] = [*sysm[:-1], {**sysm[-1], "cache_control": dict(CACHE)}]
        return payload

    def _make_message_chunk_from_anthropic_event(self, event, *, stream_usage: bool = True, coerce_content_to_string: bool,
                                                 block_start_event=None):
        # 늘 블록 목록으로 받는다(글로 펴는 것은 _Turn 이 한다) · 끝 사건(message_delta)은 늘 받는다 — 도구 호출 조각을 거기서 낸다.
        msg, start = super()._make_message_chunk_from_anthropic_event(
            event, stream_usage=True, coerce_content_to_string=False, block_start_event=block_start_event)
        et = getattr(event, "type", "")
        if et == "content_block_start" and getattr(getattr(event, "content_block", None), "type", "") == "fallback":
            # 서버 대체 모델의 경계 — 상위 클래스는 버린다. 되돌릴 때 받은 자리 그대로 있어야 한다(그 둘레 생각 블록 검증에 쓰인다).
            blk = event.content_block.model_dump(mode="json", by_alias=True, exclude_none=True)
            blk["index"] = event.index
            msg = AIMessageChunk(content=[blk], response_metadata={"model_provider": "anthropic"})
        elif et == "message_delta" and msg is not None:
            sd = getattr(getattr(event, "delta", None), "stop_details", None)
            if sd is not None:
                msg.response_metadata["stop_details"] = sd.model_dump(
                    mode="json", include={"type", "category", "explanation", "recommended_model"}, exclude_none=True)
        return msg, start

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs) -> AsyncIterator[ChatGenerationChunk]:
        kwargs.pop("stream_usage", None)
        turn = _Turn()
        async for gen in super()._astream(messages, stop=stop, run_manager=None, stream_usage=True, **kwargs):
            yield turn.chunk(gen)

    def _stream(self, messages, stop=None, run_manager=None, **kwargs) -> Iterator[ChatGenerationChunk]:
        kwargs.pop("stream_usage", None)
        turn = _Turn()
        for gen in super()._stream(messages, stop=stop, run_manager=None, stream_usage=True, **kwargs):
            yield turn.chunk(gen)


def build(*, model: str, key: str, cfg: dict, timeout, read_s: float, callbacks: list | None = None,
          http_client: Any = None, effort: str | None = None) -> ClaudeChatLLM:
    """설정으로 Claude 챗 모델을 짓는다. temperature · top_p · top_k 는 싣지 않는다(이 모델들에서 400). 생각은 끌 수 없어(Opus 5.5) 늘 adaptive 이고
    깊이는 effort 하나다(effort 를 주면 그 값 — 사람이 고른 것, 없으면 박스 기본 CLAUDE_CHAT_EFFORT). 생각 요약을 받아 화면의 '생각 중' 칸에
    낸다(display summarized — 기본은 빈 생각 글). 생각 블록이 묶인 대화가 앱의 루프 맞추기 · 도구 그림 재배치로 바뀌면 400 대신 그 블록만 버리게
    한다(block_binding drop_block — beta 머리가 함께 가야 한다). 서버 대체 모델은 받는 모델에만 싣는다(server_fallback_ok)."""
    sfb = cfg["server_fallback"] and server_fallback_ok(model)
    betas = [THINKING_BETA] + ([FALLBACK_BETA] if sfb else [])
    extra: dict = {"cache_control": dict(CACHE)}
    if sfb:
        extra["fallbacks"] = "default"
    return ClaudeChatLLM(
        model=model, api_key=key, max_tokens=cfg["max_tokens"], streaming=True, stream_usage=True, max_retries=1,
        default_request_timeout=read_s if read_s > 0 else None,
        thinking={"type": "adaptive", "display": "summarized", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
        output_config={"effort": effort or cfg["effort"]}, betas=betas, model_kwargs=extra,
        hwax_timeout=timeout, hwax_http_client=http_client, callbacks=list(callbacks or []) or None)
