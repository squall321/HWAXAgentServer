# LLM·MCP 도구 호출을 본문 없이(서버·시간·토큰·id 만) Phoenix 로 보내는 호출 추적 — 기본 꺼짐, CALL_TRACE_URL 이 있을 때만 붙는다
"""호출 추적 — HWAXPortal `docs/improvement-1015/call-trace.md`(사용자 결정 IM-D6 ③ — 원문 없이 시간·토큰만).

켜기  `CALL_TRACE_URL=http://127.0.0.1:8750`(Phoenix 주소 — 뒤에 /v1/traces 를 붙여 보낸다). 없거나 비면 꺼짐이고, 꺼져 있으면 아무것도
      붙지 않는다 — LLM 에 콜백이 없고 · 도구를 감싸지 않고 · 스레드도 망 호출도 없다. 주소를 못 읽으면 꺼진 채 기동 줄 하나로 알린다.
무엇  호출 하나에 스팬 하나(따로 선 트레이스 — 묶음은 session.id 와 hwax.* id 로 한다).
      LLM — 어느 서버(설정 주소의 호스트:포트 · LLM 라우터를 지났으면 라우터가 응답 머리로 알려 준 실제 서버) · 시작과 끝 · 스트리밍이면
            첫 조각까지(큐 대기 + 프리필)와 그 뒤 생성 시간 · 서버가 준 토큰 수 · 모델 이름 · HTTP 상태와 시도 수 · 실패면 예외 이름.
      도구 — 이름 · 걸린 시간 · 성패(실패면 예외 이름과 HTTP 상태).
      둘 다 그 호출이 일어난 자리의 id(tag — 챗 턴 · 심의 잡 · 라운드 · 좌석 · 카드 대조 종류·단위·대상).
안 함  프롬프트·답·도구 인자·도구 결과·예외 문구는 읽지 않는다 — 콜백이 받는 메시지와 토큰 조각을 열지 않고, 끝난 결과에서는 사용량과
      모델 이름 칸만 꺼낸다. 이것을 켜는 손잡이는 없다(코드가 가린다 — 환경값으로 풀 수 없게). id 도 공백 없는 짧은 표지만 받는다.
보내기 스레드 하나가 줄(QUEUE_MAX)에서 꺼내 OTLP/HTTP protobuf(ExportTraceServiceRequest — Phoenix 20.19 의 /v1/traces 는
      application/x-protobuf 만 받는다)로 묶어 보낸다(TIMEOUT_S). 줄이 차면 새 기록을 버리고, 보내지 못한 묶음도 버린다 — 챗·심의는 기다리지
      않는다. 알림은 상태가 바뀔 때만 한 줄이다(못 보냄 · 다시 보냄 · 줄이 참).
이 모듈은 표준 라이브러리만 맨 위에서 읽는다 — deliberation 이 읽으므로, langchain 이 없는 가벼운 환경(포털 계약 시험)에서도 읽혀야 한다.
langchain_core 는 기록기를 만들 때 읽고, 보내기는 http.client 다(새 패키지가 없다).
"""
from __future__ import annotations

import contextvars
import functools
import http.client
import os
import queue
import re
import secrets
import struct
import threading
import time
from urllib.parse import urlsplit

URL_ENV = "CALL_TRACE_URL"
SERVICE = "hwax-agent-server"     # service.name 이자 Phoenix 프로젝트 이름(openinference.project.name)
QUEUE_MAX = 2000                  # 보내기 전 줄의 상한(스팬 수) — 넘치면 새 기록을 버린다
BATCH_MAX = 256                   # 한 번에 묶어 보내는 수
TIMEOUT_S = 2.0                   # 한 번 보내기의 한도(연결·읽기·쓰기) — 보내는 스레드만 기다린다
RETRY_S = 5.0                     # 보내지 못한 뒤 다음 묶음을 보내기까지 쉬는 시간(그사이 들어온 기록은 줄이 차면 버린다)
OPEN_MAX = 4096                   # 끝을 못 본 LLM 호출 기록의 상한 — 취소된 호출은 끝 콜백이 오지 않는다(가장 오래된 것부터 버린다)
UPSTREAM_HEADER = "x-hwax-llm-upstream"   # LLM 라우터(HWAXPortal infra/llm-router)가 실제로 보낸 서버를 알리는 응답 머리
_SCOPE = "hwax.call_trace"
_KIND_CLIENT = 3                  # OTLP SpanKind — 밖으로 부르는 호출
_STATUS_OK, _STATUS_ERROR = 1, 2

# id 자리에 받는 값 — 공백 없는 짧은 표지(잡 id · 좌석 키 · 모델 이름 · 호스트:포트). 글(질문·발언)이 id 자리로 새지 않게 이 꼴이 아니면 버린다.
_ID = re.compile(r"[\w.:@/+-]{1,160}")
# tag 가 받는 이름 → 스팬 속성 이름. 이 밖의 이름은 버린다.
_KEYS = {"kind": "hwax.kind", "turn": "hwax.turn_id", "job": "hwax.job_id", "round": "hwax.round", "seat": "hwax.seat",
         "unit": "hwax.unit", "target": "hwax.target"}

_IDS: contextvars.ContextVar = contextvars.ContextVar("hwax_call_trace_ids", default=None)
# 지금 이 문맥에서 도는 LLM 호출 — 기록기가 시작 콜백에서 세우고, 같은 문맥에서 나가는 HTTP 요청·응답 고리가 찾아 쓴다.
_CUR: contextvars.ContextVar = contextvars.ContextVar("hwax_call_trace_llm", default=None)


def _say(msg: str) -> None:
    print(f"[call-trace] {msg}", flush=True)


# ── 켜짐 ──────────────────────────────────────────────────────────────────────
@functools.lru_cache(maxsize=1)
def _endpoint() -> str | None:
    """보낼 곳(…/v1/traces) — CALL_TRACE_URL 이 없거나 비면 None(꺼짐). 처음 물을 때 한 번 읽는다(바꾸면 재기동)."""
    raw = (os.environ.get(URL_ENV) or "").strip()
    if not raw:
        return None
    try:
        u = urlsplit(raw)
        ok = (u.scheme in ("http", "https") and bool(u.hostname) and not (u.query or u.fragment or u.username or u.password)
              and (u.port is None or u.port > 0))
    except ValueError:
        ok = False
    if not ok:
        _say(f"⚠ {URL_ENV} 를 주소로 읽지 못해 호출 추적을 끈다 — http://호스트:포트 꼴로 적는다(기록하지 않는다)")
        return None
    path = u.path.rstrip("/")
    if not path.endswith("/v1/traces"):
        path += "/v1/traces"
    return f"{u.scheme}://{u.netloc}{path}"


def enabled() -> bool:
    return _endpoint() is not None


def startup_note() -> str:
    """기동 줄 — 켜졌을 때만 글이 있다(꺼져 있으면 빈 글)."""
    ep = _endpoint()
    if ep is None:
        return ""
    return (f"[agent] 호출 추적(CALL_TRACE_URL) 켜짐 — {urlsplit(ep).netloc} 로 LLM·도구 호출마다 서버·시간·토큰·id 만 보낸다"
            "(프롬프트·답·인자·결과는 싣지 않는다)")


def server_label(url: str | None) -> str:
    """설정 주소의 호스트:포트 — 경로·계정·질의는 버린다. 못 읽으면 '?'."""
    try:
        u = urlsplit(url or "")
        lab = (u.hostname or "") + (f":{u.port}" if u.port else "")
    except ValueError:
        return "?"
    return lab if _ID.fullmatch(lab) else "?"


def new_id() -> str:
    return secrets.token_hex(8)


# ── 자리 id ───────────────────────────────────────────────────────────────────
def tag(**ids) -> None:
    """지금 문맥(태스크)과 여기서 새로 만드는 태스크의 호출에 id 를 단다 — kind · turn · job · round · seat · unit · target.
    None·빈 글은 그 id 를 뗀다. 공백 없는 짧은 표지가 아니면 버린다(글이 새지 않게). 꺼져 있으면 아무것도 하지 않는다."""
    if not enabled():
        return
    cur = dict(_IDS.get() or {})
    for k, v in ids.items():
        if k not in _KEYS:
            continue
        if v is None or v == "":
            cur.pop(k, None)
        elif isinstance(v, int) and not isinstance(v, bool):
            cur[k] = v
        elif isinstance(v, str) and _ID.fullmatch(v):
            cur[k] = v
    _IDS.set(cur)


def _id_attrs(ids: dict) -> list:
    out = [(_KEYS[k], v) for k, v in ids.items() if k in _KEYS]
    sess = ids.get("job") or ids.get("turn")
    if sess:
        out.append(("session.id", sess))
    return out


# ── OTLP protobuf(ExportTraceServiceRequest 의 필요한 조각만) ──────────────────
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint(field << 3 | wire)


def _ld(field: int, payload: bytes) -> bytes:
    return _key(field, 2) + _varint(len(payload)) + payload


def _vi(field: int, n: int) -> bytes:
    return _key(field, 0) + _varint(n)


def _any(v) -> bytes:
    """AnyValue — 1 string · 2 bool · 3 int64 · 4 double."""
    if isinstance(v, bool):
        return _vi(2, 1 if v else 0)
    if isinstance(v, int):
        return _vi(3, v & 0xFFFFFFFFFFFFFFFF)       # int64 는 2의 보수 64비트 varint
    if isinstance(v, float):
        return _key(4, 1) + struct.pack("<d", v)
    return _ld(1, str(v).encode("utf-8"))


def _kv(k: str, v) -> bytes:
    return _ld(1, k.encode("utf-8")) + _ld(2, _any(v))


class _Span:
    __slots__ = ("trace_id", "span_id", "name", "start_ns", "end_ns", "attrs", "ok")

    def __init__(self, name: str, start_ns: int, end_ns: int, attrs: list, ok: bool):
        self.trace_id = _nonzero(16)
        self.span_id = _nonzero(8)
        self.name, self.start_ns, self.end_ns, self.attrs, self.ok = name, start_ns, max(end_ns, start_ns), attrs, ok


def _nonzero(n: int) -> bytes:
    while True:
        b = secrets.token_bytes(n)
        if any(b):
            return b


def _span_pb(s: _Span) -> bytes:
    """Span — 1 trace_id · 2 span_id · 5 name · 6 kind · 7·8 시각(fixed64 ns) · 9 attributes · 15 status{3 code}."""
    out = (_ld(1, s.trace_id) + _ld(2, s.span_id) + _ld(5, s.name.encode("utf-8")) + _vi(6, _KIND_CLIENT)
           + _key(7, 1) + struct.pack("<Q", s.start_ns) + _key(8, 1) + struct.pack("<Q", s.end_ns))
    out += b"".join(_ld(9, _kv(k, v)) for k, v in s.attrs)
    return out + _ld(15, _vi(3, _STATUS_OK if s.ok else _STATUS_ERROR))


def encode(spans: list) -> bytes:
    """ExportTraceServiceRequest{1 resource_spans{1 resource{1 attributes} · 2 scope_spans{1 scope{1 name} · 2 spans}}}."""
    resource = _ld(1, _kv("service.name", SERVICE)) + _ld(1, _kv("openinference.project.name", SERVICE))
    scope_spans = _ld(1, _ld(1, _SCOPE.encode("utf-8"))) + b"".join(_ld(2, _span_pb(s)) for s in spans)
    return _ld(1, _ld(1, resource) + _ld(2, scope_spans))


# ── 보내기 ────────────────────────────────────────────────────────────────────
class _Exporter:
    """줄 하나와 보내는 스레드 하나. put 은 기다리지 않는다(줄이 차면 버리고 센다)."""

    def __init__(self, url: str):
        self.url = url
        self.q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self.sent = self.failed = self.dropped = 0
        self._down = False          # 마지막 보내기가 실패했다
        self._full = False          # 줄이 찬 것을 알렸다(보내기가 다시 되면 푼다)
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="hwax-call-trace", daemon=True)
        self.thread.start()

    def put(self, span: _Span) -> None:
        try:
            self.q.put_nowait(span)
        except queue.Full:
            with self._lock:
                self.dropped += 1
                first, self._full = not self._full, True
            if first:
                _say(f"⚠ 보낼 줄이 찼다({QUEUE_MAX}건) — 넘치는 기록은 버린다(Phoenix 가 느리거나 내려갔다 · 다시 보내기까지 더 알리지 않는다)")

    def _post(self, body: bytes) -> int:
        """한 묶음을 보내고 상태 코드를 돌려준다 — 표준 라이브러리 http.client 다. httpx 를 쓰면 이 서버의 로그 설정이 보낼 때마다 INFO 한 줄
        (HTTP Request: POST …/v1/traces)을 남기고, 환경의 http(s)_proxy 도 따로 꺼야 한다(받는 곳은 루프백이다)."""
        u = urlsplit(self.url)
        conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(
            u.hostname, u.port, timeout=TIMEOUT_S)
        try:
            conn.request("POST", u.path, body=body, headers={"content-type": "application/x-protobuf"})
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()

    def _run(self) -> None:
        while True:
            batch = [self.q.get()]
            while len(batch) < BATCH_MAX:
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break
            why = None
            try:
                status = self._post(encode(batch))
                if status != 200:
                    why = f"HTTP {status}"
            except Exception as exc:  # noqa: BLE001 — 무엇이든 그 묶음을 못 보냈다는 뜻이다
                why = type(exc).__name__
            if why is None:
                with self._lock:
                    self.sent += len(batch)
                    back, self._down, self._full = self._down, False, False
                if back:
                    _say(f"✓ 다시 보낸다 — 그동안 못 보낸 기록 {self.failed}건 · 줄이 차 버린 기록 {self.dropped}건(누적)")
            else:
                with self._lock:
                    self.failed += len(batch)
                    first, self._down = not self._down, True
                if first:
                    _say(f"⚠ {urlsplit(self.url).netloc} 로 보내지 못했다({why}) — 그 기록은 버린다. 챗·심의는 그대로 돈다"
                         "(되살아나면 한 줄 · 그사이의 실패는 다시 알리지 않는다)")
                time.sleep(RETRY_S)


_EXP: _Exporter | None = None
_EXP_LOCK = threading.Lock()


def _emit(span: _Span) -> None:
    global _EXP
    ep = _endpoint()
    if ep is None:
        return
    exp = _EXP
    if exp is None:
        with _EXP_LOCK:
            if _EXP is None:
                _EXP = _Exporter(ep)
            exp = _EXP
    exp.put(span)


def _exc_name(exc: BaseException) -> str:
    """예외의 이름만(문구는 보지 않는다) — anyio TaskGroup 이 싼 것은 안쪽의 이름으로."""
    for _ in range(8):
        inner = getattr(exc, "exceptions", None)
        if not isinstance(inner, (list, tuple)) or not inner or not isinstance(inner[0], BaseException):
            break
        exc = inner[0]
    return type(exc).__name__


def _http_status(exc: BaseException) -> int | None:
    for v in (getattr(exc, "status_code", None), getattr(getattr(exc, "response", None), "status_code", None)):
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    return None


# ── 도구 ──────────────────────────────────────────────────────────────────────
def wrap_tools(tools: list) -> list:
    """게이트웨이에서 받은 도구 목록 — 꺼져 있으면 그 목록 그대로, 켜져 있으면 도구마다 걸린 시간·성패를 잰다."""
    if not enabled():
        return tools
    return [wrap_tool(t) for t in tools]


def wrap_tool(tool):
    """도구의 코루틴을 감싸 MCP 호출 하나를 스팬 하나로 남긴다 — 인자와 결과는 보지 않는다. 예외는 이름만 적고 그대로 올린다."""
    orig = getattr(tool, "coroutine", None)
    if not enabled() or orig is None or getattr(orig, "_hwax_call_trace", False):
        return tool
    name = getattr(tool, "name", "")
    name = name if isinstance(name, str) and _ID.fullmatch(name) else "tool"

    async def traced(*a, **kw):
        ids, start_ns, t0 = dict(_IDS.get() or {}), time.time_ns(), time.monotonic()
        try:
            out = await orig(*a, **kw)
        except BaseException as exc:
            _tool_span(name, ids, start_ns, t0, exc)
            raise
        _tool_span(name, ids, start_ns, t0, None)
        return out

    traced._hwax_call_trace = True
    tool.coroutine = traced
    return tool


def _tool_span(name: str, ids: dict, start_ns: int, t0: float, exc: BaseException | None) -> None:
    try:
        attrs = [("openinference.span.kind", "TOOL"), ("tool.name", name)]
        if exc is not None:
            attrs.append(("error.type", _exc_name(exc)))
            st = _http_status(exc)
            if st is not None:
                attrs.append(("hwax.http_status", st))
        attrs += _id_attrs(ids)
        _emit(_Span(name, start_ns, start_ns + int((time.monotonic() - t0) * 1e9), attrs, exc is None))
    except Exception:  # noqa: BLE001 — 기록 실패가 도구 호출을 건드리지 않는다
        pass


# ── LLM ───────────────────────────────────────────────────────────────────────
class _Call:
    __slots__ = ("server", "ids", "model", "start_ns", "t0", "first", "upstream", "status", "attempts", "done")

    def __init__(self, server: str, ids: dict, model: str | None):
        self.server, self.ids, self.model = server, ids, model
        self.start_ns, self.t0 = time.time_ns(), time.monotonic()
        self.first = None
        self.upstream = None
        self.status = None
        self.attempts = 0
        self.done = False


def _clean_id(v) -> str | None:
    return v if isinstance(v, str) and _ID.fullmatch(v) else None


def _count(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _tokens(src: dict, keys: tuple, details: tuple) -> dict:
    """사용량 칸에서 수만 — {prompt, completion, total, cache_read}. 수가 아닌 것은 버린다."""
    out = {name: _count(src.get(k)) for name, k in zip(("prompt", "completion", "total"), keys)}
    d = src.get(details[0])
    if isinstance(d, dict):
        out["cache_read"] = _count(d.get(details[1]))
    return {k: v for k, v in out.items() if v is not None}


def _usage(response) -> tuple[dict, str | None]:
    """끝난 결과에서 토큰 수와 모델 이름만 — 생성된 글(generations 의 text·message.content)은 열지 않는다."""
    tokens, model = {}, None
    lo = getattr(response, "llm_output", None)
    if isinstance(lo, dict):
        if isinstance(lo.get("token_usage"), dict):
            tokens = _tokens(lo["token_usage"], ("prompt_tokens", "completion_tokens", "total_tokens"),
                             ("prompt_tokens_details", "cached_tokens"))
        model = _clean_id(lo.get("model_name"))
    if not tokens or model is None:
        try:
            msg = getattr(response.generations[0][0], "message", None)
        except Exception:  # noqa: BLE001 — 결과 모양이 다르면 모른다
            msg = None
        um = getattr(msg, "usage_metadata", None)
        rm = getattr(msg, "response_metadata", None)
        if not tokens and isinstance(um, dict):
            tokens = _tokens(um, ("input_tokens", "output_tokens", "total_tokens"), ("input_token_details", "cache_read"))
        if isinstance(rm, dict):
            if not tokens and isinstance(rm.get("token_usage"), dict):
                tokens = _tokens(rm["token_usage"], ("prompt_tokens", "completion_tokens", "total_tokens"),
                                 ("prompt_tokens_details", "cached_tokens"))
            if model is None:
                model = _clean_id(rm.get("model_name"))
    return tokens, model


def _llm_span(c: _Call, *, tokens: dict | None = None, model: str | None = None, exc: BaseException | None = None) -> None:
    try:
        now = time.monotonic()
        attrs = [("openinference.span.kind", "LLM"), ("hwax.llm.server", c.server)]
        m = model or c.model
        if m:
            attrs.append(("llm.model_name", m))
        for k, v in (tokens or {}).items():
            attrs.append(("llm.token_count.prompt_details.cache_read" if k == "cache_read" else f"llm.token_count.{k}", v))
        if c.upstream:
            attrs.append(("hwax.llm.upstream", c.upstream))
        if c.first is not None:
            attrs.append(("hwax.llm.ttft_ms", round((c.first - c.t0) * 1000.0, 1)))
            attrs.append(("hwax.llm.gen_ms", round((now - c.first) * 1000.0, 1)))
        if c.status is not None:
            attrs.append(("hwax.llm.http_status", c.status))
        if c.attempts:
            attrs.append(("hwax.llm.attempts", c.attempts))
        if exc is not None:
            attrs.append(("error.type", _exc_name(exc)))
            st = _http_status(exc)
            if st is not None and c.status is None:
                attrs.append(("hwax.llm.http_status", st))
        attrs += _id_attrs(c.ids)
        _emit(_Span("llm", c.start_ns, c.start_ns + int((now - c.t0) * 1e9), attrs, exc is None))
    except Exception:  # noqa: BLE001 — 기록 실패가 LLM 호출을 건드리지 않는다
        pass


@functools.lru_cache(maxsize=None)
def _recorder_class():
    """기록기(LangChain 콜백) — 처음 쓸 때 짓는다(langchain_core 는 그때 읽는다). 스트리밍 콜백(_StreamingCallbackHandler)이 아니다 —
    그것이 붙으면 LangChain 이 비스트리밍 호출을 스트리밍으로 바꾼다. 붙여도 요청은 그대로다(시험이 본다)."""
    from langchain_core.callbacks import BaseCallbackHandler

    class CallTraceRecorder(BaseCallbackHandler):
        run_inline = True       # 부른 태스크 안에서 곧바로 — 스레드로 넘기면 문맥(자리 id · 지금 호출)이 끊긴다
        raise_error = False

        def __init__(self, server: str):
            self.server = server
            self._open: dict = {}

        def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, tags=None, metadata=None,
                                **kwargs):
            # messages(프롬프트)와 serialized(설정)는 열지 않는다 — 모델 이름만 호출 인자에서 꺼낸다.
            try:
                ip = kwargs.get("invocation_params")
                model = None
                if isinstance(ip, dict):
                    model = _clean_id(ip.get("model")) or _clean_id(ip.get("model_name"))
                if model is None and isinstance(metadata, dict):
                    model = _clean_id(metadata.get("ls_model_name"))
                c = _Call(self.server, dict(_IDS.get() or {}), model)
                if len(self._open) >= OPEN_MAX:
                    self._open.pop(next(iter(self._open)), None)
                self._open[run_id] = c
                _CUR.set(c)
            except Exception:  # noqa: BLE001
                pass

        def on_llm_new_token(self, token, *, run_id, **kwargs):
            # 조각의 글은 보지 않는다 — 첫 조각이 온 시각만.
            c = self._open.get(run_id)
            if c is not None and c.first is None:
                c.first = time.monotonic()

        def on_llm_end(self, response, *, run_id, **kwargs):
            c = self._open.pop(run_id, None)
            if c is None:
                return
            c.done = True
            try:
                tokens, model = _usage(response)
            except Exception:  # noqa: BLE001
                tokens, model = {}, None
            _llm_span(c, tokens=tokens, model=model)

        def on_llm_error(self, error, *, run_id, **kwargs):
            c = self._open.pop(run_id, None)
            if c is None:
                return
            c.done = True
            _llm_span(c, exc=error)

    return CallTraceRecorder


def _on_request(request) -> None:
    c = _CUR.get()
    if c is not None and not c.done:
        c.attempts += 1


def _on_response(response) -> None:
    c = _CUR.get()
    if c is None or c.done:
        return
    try:
        c.status = int(response.status_code)
        c.upstream = _clean_id(response.headers.get(UPSTREAM_HEADER)) or c.upstream
    except Exception:  # noqa: BLE001
        pass


async def _aon_request(request) -> None:
    _on_request(request)


async def _aon_response(response) -> None:
    _on_response(response)


def _hook_client(client, req, resp) -> None:
    hooks = getattr(client, "event_hooks", None)
    if not isinstance(hooks, dict) or resp in (hooks.get("response") or []):
        return
    client.event_hooks = {"request": [*(hooks.get("request") or []), req], "response": [*(hooks.get("response") or []), resp]}


def instrument_llm(llm, server: str):
    """켜져 있으면 그 LLM 에 기록기(콜백)를 붙이고, 그 LLM 의 HTTP 클라이언트에 응답 머리(라우터가 알린 실제 서버·상태)를 보는 고리를 단다.
    꺼져 있으면 아무것도 하지 않고 그대로 돌려준다. server 는 어느 서버를 불렀는지의 표지(설정 주소의 호스트:포트 · 'external')."""
    if llm is None or not enabled():
        return llm
    try:
        cbs = getattr(llm, "callbacks", None)
        if cbs is None or isinstance(cbs, list):
            llm.callbacks = [*(cbs or []), _recorder_class()(server)]
        else:
            _say(f"⚠ LLM 의 callbacks 가 목록이 아니라({type(cbs).__name__}) 기록기를 붙이지 않았다 — 그 LLM 의 호출은 기록되지 않는다")
            return llm
        # openai 클라이언트가 쓰는 httpx 클라이언트(_client) — LangChain 이 만든 것을 그대로 두고 고리만 단다(요청은 바뀌지 않는다).
        _hook_client(getattr(getattr(llm, "root_async_client", None), "_client", None), _aon_request, _aon_response)
        _hook_client(getattr(getattr(llm, "root_client", None), "_client", None), _on_request, _on_response)
    except Exception as exc:  # noqa: BLE001
        _say(f"⚠ LLM 에 기록기를 붙이지 못했다({type(exc).__name__}) — 그 LLM 의 호출은 기록되지 않는다")
    return llm


def _reset_for_tests() -> None:
    """시험 전용 — 환경값을 다시 읽게 하고 보내는 스레드를 잊는다(떠 있는 스레드는 데몬이라 시험이 끝나면 함께 내려간다)."""
    global _EXP
    _endpoint.cache_clear()
    _EXP = None
