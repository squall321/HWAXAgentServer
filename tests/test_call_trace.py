# 호출 추적(call_trace.py) — 꺼지면 아무것도 붙지 않는가 · 본문이 어디에도 실리지 않는가 · 공식 OTLP 정의로 읽히는가 · 받는 곳이 죽어도 기다리지 않는가
"""왜 — 호출 추적은 사용자 결정(HWAXPortal docs/improvement-1015 IM-D6 ③)으로 **원문 없이** 서버·시간·토큰·id 만 남긴다.
가리는 것은 환경값이 아니라 코드다 — 그래서 시험이 그 약속을 실물 경로로 본다.

  · 꺼짐(CALL_TRACE_URL 없음) — 기동 절차가 만든 LLM 에 콜백도 HTTP 고리도 없고, 도구 목록은 같은 목록이고, 스레드도 없다.
  · 켜도 LLM 요청은 한 바이트도 바뀌지 않는다(기록기가 스트리밍 콜백이면 LangChain 이 비스트리밍 호출을 스트리밍으로 바꾼다).
  · 표식(canary)을 프롬프트·답·스트림 조각·오류 본문·도구 인자·결과·예외 문구·id 자리에 심고, 실제로 보낸 바이트에 없는지 본다.
  · 인코더는 공식 정의(opentelemetry-proto)로 읽어 맞댄다 — 없으면 건너뛴다(requirements-dev.txt).
  · 받는 곳이 죽었거나(연결 거절) 느리면(받고 답하지 않는다) 호출은 기다리지 않고 기록만 버리며, 알림은 상태가 바뀔 때 한 줄이다.

망은 이 시험이 루프백 빈 포트에 띄운 가짜 서버(OpenAI 꼴 · 수집기 꼴)뿐이다.

  실행:  .venv/bin/python -m pytest tests/test_call_trace.py -q
"""
import asyncio
import http.server
import json
import logging
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402
from langchain_core.language_models.fake_chat_models import FakeListChatModel  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

import call_trace as ct  # noqa: E402

CANARY = "CANARY-5b1e7c0d"          # 본문 자리에만 심는 표식 — 내보낸 바이트에 있으면 실패다
UPSTREAM = "203.0.113.7:8000"       # 라우터가 알려 주는 실제 서버(문서용 대역의 지어낸 주소 — 기록되는 값의 꼴만 본다)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(handler) -> tuple[http.server.ThreadingHTTPServer, int]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


class _OpenAI(http.server.BaseHTTPRequestHandler):
    """가짜 OpenAI 호환 서버 — 답·스트림 조각·오류 본문에 표식을 싣고, 라우터처럼 실제 서버 머리를 단다."""
    error = False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        if type(self).error:
            raw = json.dumps({"error": {"message": f"서버 오류 {CANARY}", "type": "server_error"}}).encode()
            self.send_response(500)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if body.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header(ct.UPSTREAM_HEADER, UPSTREAM)
            self.end_headers()
            for piece, fin in ((f"조각 {CANARY}", None), (" 끝", None), ("", "stop")):
                ch = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "fake-model",
                      "choices": [{"index": 0, "delta": {"content": piece} if piece else {}, "finish_reason": fin}]}
                self.wfile.write(f"data: {json.dumps(ch)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.02)
            self.wfile.write(b"data: [DONE]\n\n")
            return
        raw = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": "fake-model",
                          "choices": [{"index": 0, "message": {"role": "assistant", "content": f"답 {CANARY}"},
                                       "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
                                    "prompt_tokens_details": {"cached_tokens": 3}}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header(ct.UPSTREAM_HEADER, UPSTREAM)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class _Collector(http.server.BaseHTTPRequestHandler):
    """가짜 수집기 — 받은 본문을 모은다. codes 가 남아 있으면 그 상태 코드를 차례로 낸다(되살아남 시험)."""
    bodies: list = []
    codes: list = []

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        code = type(self).codes.pop(0) if type(self).codes else 200
        if code == 200:
            type(self).bodies.append((self.headers.get("content-type"), raw))
        self.send_response(code)
        self.send_header("content-type", "application/x-protobuf")
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def trace_on(monkeypatch):
    """CALL_TRACE_URL 을 켠다 — 시험이 준 주소로. 끝나면 끄고 다시 읽게 한다."""
    def on(url: str):
        monkeypatch.setenv(ct.URL_ENV, url)
        ct._reset_for_tests()
    yield on
    monkeypatch.delenv(ct.URL_ENV, raising=False)
    ct._reset_for_tests()


@pytest.fixture
def collector(trace_on):
    _Collector.bodies, _Collector.codes = [], []
    srv, port = _serve(_Collector)
    trace_on(f"http://127.0.0.1:{port}")
    yield _Collector
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def captured(monkeypatch):
    """보내기 직전의 스팬을 모은다(인코딩 전 — 속성 단위로 본다)."""
    spans = []
    monkeypatch.setattr(ct, "_emit", spans.append)
    return spans


def _llm(port: int, **kw) -> ChatOpenAI:
    return ChatOpenAI(base_url=f"http://127.0.0.1:{port}/v1", api_key="EMPTY", model="fake-model",
                      timeout=httpx.Timeout(30, connect=5), max_retries=0, **kw)


def _wait_sent(n: int, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if ct._EXP is not None and ct._EXP.sent >= n:
            return
        time.sleep(0.02)
    raise AssertionError(f"{n}건을 보내지 못했다 — {ct._EXP and (ct._EXP.sent, ct._EXP.failed, ct._EXP.dropped)}")


def _attrs(span) -> dict:
    return dict(span.attrs)


def _threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name == "hwax-call-trace"]


# ── 꺼짐 ─────────────────────────────────────────────────────────────────────────────────────────────
def test_꺼져_있으면_아무것도_붙지_않는다(monkeypatch):
    monkeypatch.delenv(ct.URL_ENV, raising=False)
    ct._reset_for_tests()
    before = _threads()
    assert not ct.enabled() and ct.startup_note() == ""
    llm = _llm(_free_port())
    hooks = dict(llm.root_async_client._client.event_hooks)
    assert ct.instrument_llm(llm, "x") is llm and llm.callbacks is None
    assert llm.root_async_client._client.event_hooks == hooks == {"request": [], "response": []}
    tool = StructuredTool.from_function(coroutine=_tool_ok, name="search_reports", description="d")
    co = tool.coroutine
    tools = [tool]
    assert ct.wrap_tools(tools) is tools and ct.wrap_tool(tool) is tool and tool.coroutine is co
    ct.tag(kind="chat", turn="t1", seat="s")
    assert ct._IDS.get() is None, "꺼져 있으면 문맥에 아무것도 세우지 않는다"
    assert ct._EXP is None and _threads() == before


def test_기동_절차가_꺼진_서버에서는_LLM_에_아무것도_붙이지_않는다(monkeypatch, capsys):
    from test_llm_limits import _boot  # noqa: PLC0415 — 기동 절차를 실제로 연다(그 시험의 도우미)
    monkeypatch.delenv(ct.URL_ENV, raising=False)
    ct._reset_for_tests()
    st = _boot(monkeypatch)
    for llm in (st.llm, st.delib_llm, st.mk_delib_llm(7200.0)):
        assert llm.callbacks is None
        assert llm.root_async_client._client.event_hooks == {"request": [], "response": []}
    assert "호출 추적" not in capsys.readouterr().out and ct._EXP is None


def test_켜도_LLM_요청은_한_바이트도_바뀌지_않는다(monkeypatch, trace_on, capsys):
    """기록기가 스트리밍 콜백이면 LangChain 이 ainvoke 를 스트리밍 요청으로 바꾼다(_should_stream) — 요청 본문·머리를 맞댄다."""
    from test_llm_limits import _boot  # noqa: PLC0415

    def sent(llm) -> list:
        seen = []

        async def _send(_self, request, **_kw):
            seen.append((request.method, str(request.url), request.content,
                         sorted((k, v) for k, v in request.headers.items() if k not in ("x-stainless-retry-count",))))
            return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                                             "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                                          "finish_reason": "stop"}]}, request=request)
        monkeypatch.setattr(httpx.AsyncClient, "send", _send)
        asyncio.run(llm.ainvoke([("system", "s"), ("human", "안녕")]))
        return seen

    monkeypatch.delenv(ct.URL_ENV, raising=False)
    ct._reset_for_tests()
    off = sent(_boot(monkeypatch).delib_llm)
    trace_on(f"http://127.0.0.1:{_free_port()}")       # 닫힌 포트 — 보내기는 실패해도 된다
    st = _boot(monkeypatch)
    assert "호출 추적(CALL_TRACE_URL) 켜짐" in capsys.readouterr().out
    assert [type(c).__name__ for c in st.delib_llm.callbacks] == ["CallTraceRecorder"]
    on = sent(st.delib_llm)
    assert on == off and json.loads(off[0][2]).get("stream") in (None, False), (off, on)


# ── 본문이 실리지 않는다 ─────────────────────────────────────────────────────────────────────────────
async def _tool_ok(query: str) -> str:
    return f"결과 {CANARY} ({query})"


async def _tool_bad(query: str) -> str:
    raise RuntimeError(f"도구가 터졌다 {CANARY} {query}")


def test_프롬프트_답_인자_결과_오류문구는_어디에도_실리지_않는다(collector, monkeypatch):
    emitted = []
    real = ct._emit
    monkeypatch.setattr(ct, "_emit", lambda sp: (emitted.append(sp), real(sp)))   # 인코딩 전 스팬도 본다(그대로 보낸다)
    srv, port = _serve(_OpenAI)
    try:
        llm = ct.instrument_llm(_llm(port), ct.server_label(f"http://127.0.0.1:{port}/v1"))
        ok = ct.wrap_tool(StructuredTool.from_function(coroutine=_tool_ok, name="search_reports", description="d"))
        bad = ct.wrap_tool(StructuredTool.from_function(coroutine=_tool_bad, name="get_material", description="d"))

        async def go():
            ct.tag(kind="delib", job="delib-20261016-000000-abc123", round=2, seat="rel-chemical-corrosion",
                   unit=f"질문 전체 {CANARY}", target=f"{CANARY} 공백이 든 글")
            r = await llm.ainvoke([("system", f"시스템 {CANARY}"), ("human", f"질문 {CANARY}")])
            assert CANARY in r.content
            got = [c.content async for c in llm.astream([("human", f"스트림 {CANARY}")])]
            assert CANARY in "".join(got)
            _OpenAI.error = True
            try:
                with pytest.raises(Exception) as ei:
                    await llm.ainvoke([("human", f"오류 {CANARY}")])
                assert CANARY in str(ei.value)
            finally:
                _OpenAI.error = False
            assert CANARY in await ok.ainvoke({"query": f"인자 {CANARY}"})
            with pytest.raises(RuntimeError):
                await bad.ainvoke({"query": f"인자 {CANARY}"})
        asyncio.run(go())
        _wait_sent(5)
    finally:
        srv.shutdown()
        srv.server_close()
    # 실제로 보낸 바이트 — 표식도, 표식 곁에 심은 글도 없다
    raw = b"".join(b for _ct, b in collector.bodies)
    assert {c for c, _b in collector.bodies} == {"application/x-protobuf"}
    assert CANARY.encode() not in raw, "본문 표식이 내보낸 바이트에 있다"
    for word in ("시스템", "질문", "스트림", "조각", "결과", "인자", "도구가 터졌다", "서버 오류", "공백이 든 글"):
        assert word.encode() not in raw, word
    # 무엇이 실렸나 — 서버 · 실제 서버 · 토큰 · 모델 · id · 도구 이름 · 예외 이름
    got = {}
    for sp in emitted:
        a = _attrs(sp)
        assert not any(CANARY in str(v) for v in a.values()), a
        got.setdefault((sp.name, a.get("error.type")), []).append((a, sp.ok))
    (ns, ok0), (sm, ok1) = got[("llm", None)]
    assert ns["llm.token_count.prompt"] == 11 and ns["llm.token_count.completion"] == 7 and ns["llm.token_count.total"] == 18
    assert ns["llm.token_count.prompt_details.cache_read"] == 3 and ns["llm.model_name"] == "fake-model"
    assert ns["hwax.llm.server"] == f"127.0.0.1:{port}" and ns["hwax.llm.upstream"] == UPSTREAM and ns["hwax.llm.http_status"] == 200
    assert ns["hwax.kind"] == "delib" and ns["hwax.round"] == 2 and ns["hwax.seat"] == "rel-chemical-corrosion"
    assert ns["session.id"] == ns["hwax.job_id"] == "delib-20261016-000000-abc123"
    assert "hwax.unit" not in ns and "hwax.target" not in ns, "글은 id 자리에 실리지 않는다"
    assert "hwax.llm.ttft_ms" not in ns, "비스트리밍 호출은 첫 조각을 모른다(지어내지 않는다)"
    assert sm["hwax.llm.ttft_ms"] >= 0 and sm["hwax.llm.gen_ms"] >= 0 and sm["hwax.llm.upstream"] == UPSTREAM, "스트리밍은 첫 조각까지"
    assert ok0 and ok1
    ((ne, oke),) = got[("llm", "InternalServerError")]
    assert ne["hwax.llm.http_status"] == 500 and not oke
    ((to, oko),) = got[("search_reports", None)]
    ((tb, okb),) = got[("get_material", "RuntimeError")]
    assert to["openinference.span.kind"] == tb["openinference.span.kind"] == "TOOL" and oko and not okb
    assert to["tool.name"] == "search_reports" and to["hwax.seat"] == "rel-chemical-corrosion"
    # 공식 정의가 있으면 보낸 바이트를 다시 읽어 인코딩 전 스팬과 맞댄다(없으면 이 대조만 건너뛴다 — 위 단언은 그대로다)
    try:
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as pb  # noqa: PLC0415
    except ImportError:
        # 건너뜀으로 바꾸지 않는다 — 이 시험의 본뜻(표식이 어디에도 없다)은 위에서 이미 통과했고, 인코더는 정의 없이도 고정 바이트
        # 시험(test_인코더는_고정_바이트와_같다)이 늘 본다. 건너뜀이면 본문 가림 시험 전체가 안 돈 것처럼 읽힌다.
        return
    decoded = [s for _c, b in collector.bodies for rs in pb.ExportTraceServiceRequest.FromString(b).resource_spans
               for ss in rs.scope_spans for s in ss.spans]
    assert sorted((s.name, s.status.code, tuple((kv.key, getattr(kv.value, kv.value.WhichOneof("value"))) for kv in s.attributes))
                  for s in decoded) == sorted((sp.name, 1 if sp.ok else 2, tuple(sp.attrs)) for sp in emitted)


def test_id_자리는_공백_없는_짧은_표지만_받는다(trace_on):
    trace_on(f"http://127.0.0.1:{_free_port()}")

    async def go():
        ct.tag(kind="chat", turn="t-1", round=3, seat="he-team.stepforge", unknown="x")
        first = dict(ct._IDS.get())
        ct.tag(seat="이 좌석의 역할 설명 문장", job="a" * 161)
        ids = dict(ct._IDS.get())
        out = {}
        for v in ("배터리팩_스웰링_원인을_찾아줘", "koo.park@example.com", "C-12/리브두께/감소", "rel-chemical-corrosion"):
            ct.tag(seat=v)
            out[v] = ct._IDS.get().get("seat")
        ct.tag(round=None, seat="")
        return first, ids, out, dict(ct._IDS.get())
    first, ids, out, after = asyncio.run(go())
    assert first == {"kind": "chat", "turn": "t-1", "round": 3, "seat": "he-team.stepforge"}, first
    # 거부된 값은 버리고 그 id 를 뗀다 — 앞 좌석이 남아 엉뚱한 호출에 붙지 않게(반대 검토 LOW-2)
    assert ids == {"kind": "chat", "turn": "t-1", "round": 3}, ids
    assert out == {"배터리팩_스웰링_원인을_찾아줘": None, "koo.park@example.com": None, "C-12/리브두께/감소": None,
                   "rel-chemical-corrosion": "rel-chemical-corrosion"}, "한글 낱말 · 메일 주소는 id 자리에 실리지 않는다"
    assert after == {"kind": "chat", "turn": "t-1"}


# ── 공식 정의로 읽기 ─────────────────────────────────────────────────────────────────────────────────
def test_OTLP_protobuf_를_공식_정의로_읽으면_보낸_값_그대로다():
    pb = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    attrs = [("openinference.span.kind", "LLM"), ("한글 키", "값 ✓ 𝄞"), ("i", 18), ("neg", -5), ("big", 2 ** 62),
             ("f", 12.5), ("t", True), ("f0", False), ("empty", "")]
    a = ct._Span("llm", 1_760_000_000_123_456_789, 1_760_000_001_000_000_000, attrs, True)
    b = ct._Span("search_reports", 5, 4, [("error.type", "RuntimeError")], False)
    req = pb.ExportTraceServiceRequest.FromString(ct.encode([a, b]))
    (rs,) = req.resource_spans
    assert [(kv.key, kv.value.string_value) for kv in rs.resource.attributes] == [
        ("service.name", ct.SERVICE), ("openinference.project.name", ct.SERVICE)]
    (ss,) = rs.scope_spans
    assert ss.scope.name == "hwax.call_trace"
    s1, s2 = ss.spans
    assert (s1.trace_id, s1.span_id, s1.name, s1.kind) == (a.trace_id, a.span_id, "llm", 3)
    assert len(s1.trace_id) == 16 and len(s1.span_id) == 8 and any(s1.trace_id) and any(s1.span_id)
    assert (s1.start_time_unix_nano, s1.end_time_unix_nano, s1.status.code, s1.status.message) == (
        1_760_000_000_123_456_789, 1_760_000_001_000_000_000, 1, "")
    got = [(kv.key, kv.value.WhichOneof("value"), getattr(kv.value, kv.value.WhichOneof("value"))) for kv in s1.attributes]
    assert got == [("openinference.span.kind", "string_value", "LLM"), ("한글 키", "string_value", "값 ✓ 𝄞"),
                   ("i", "int_value", 18), ("neg", "int_value", -5), ("big", "int_value", 2 ** 62),
                   ("f", "double_value", 12.5), ("t", "bool_value", True), ("f0", "bool_value", False),
                   ("empty", "string_value", "")]
    assert s2.status.code == 2 and s2.end_time_unix_nano == s2.start_time_unix_nano == 5, "끝이 시작보다 앞서지 않는다"
    assert not s1.parent_span_id and not s2.parent_span_id and s1.trace_id != s2.trace_id


# ── 받는 곳이 죽거나 느릴 때 ─────────────────────────────────────────────────────────────────────────
def _blackhole() -> tuple[socket.socket, int, list]:
    """연결은 받되 답하지 않는 수집기 — 보내기가 읽기 한도까지 매달린다."""
    lsn = socket.socket()
    lsn.bind(("127.0.0.1", 0))
    lsn.listen(64)
    held = []

    def loop():
        while True:
            try:
                c, _ = lsn.accept()
            except OSError:
                return
            held.append(c)
    threading.Thread(target=loop, daemon=True).start()
    return lsn, lsn.getsockname()[1], held


@pytest.mark.parametrize("dead", ["refused", "blackhole"])
def test_받는_곳이_죽었거나_느려도_호출은_기다리지_않고_알림은_한_번이다(trace_on, monkeypatch, capsys, dead):
    monkeypatch.setattr(ct, "TIMEOUT_S", 0.3)
    monkeypatch.setattr(ct, "RETRY_S", 0.05)
    monkeypatch.setattr(ct, "QUEUE_MAX", 20)
    lsn = None
    if dead == "refused":
        port = _free_port()
    else:
        lsn, port, held = _blackhole()
    trace_on(f"http://127.0.0.1:{port}")
    llm_srv, llm_port = _serve(_OpenAI)
    try:
        llm = ct.instrument_llm(_llm(llm_port), "x")
        tool = ct.wrap_tool(StructuredTool.from_function(coroutine=_tool_ok, name="search_reports", description="d"))

        async def go():
            t0 = time.monotonic()
            for _ in range(300):
                await tool.ainvoke({"query": "q"})
            t1 = time.monotonic()
            await llm.ainvoke([("human", "q")])
            return t1 - t0, time.monotonic() - t1
        tools_s, llm_s = asyncio.run(go())
        time.sleep(1.0)        # 보내는 스레드가 실패를 몇 번 겪을 시간
    finally:
        llm_srv.shutdown()
        llm_srv.server_close()
        if lsn is not None:
            lsn.close()
            for c in held:
                c.close()
    assert tools_s < 2.0 and llm_s < 2.0, (tools_s, llm_s)
    exp = ct._EXP
    assert exp.dropped > 0 and exp.sent == 0 and exp.failed > 0, (exp.dropped, exp.sent, exp.failed)
    out = capsys.readouterr().out
    assert out.count("보낼 줄이 찼다") == 1 and out.count("보내지 못했다") == 1, out
    assert CANARY not in out


def test_되살아나면_한_줄로_알리고_다시_보낸다(collector, monkeypatch, capsys, caplog):
    """보내기마다 로그를 남기지 않는다 — 이 서버의 로그 설정은 httpx 요청마다 INFO 한 줄을 찍는다(보내기는 http.client 다)."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(ct, "RETRY_S", 0.05)
    collector.codes = [503, 503, 503]

    async def go():
        tool = ct.wrap_tool(StructuredTool.from_function(coroutine=_tool_ok, name="search_reports", description="d"))
        for _ in range(5):
            await tool.ainvoke({"query": "q"})
            await asyncio.sleep(0.15)
    asyncio.run(go())
    _wait_sent(2)                     # 되살아난 뒤 두 번째 묶음까지 — 되살아남 알림은 그 앞에 찍힌다
    out = capsys.readouterr().out
    assert out.count("보내지 못했다(HTTP 503)") == 1 and out.count("✓ 다시 보낸다") == 1, out
    assert not [r for r in caplog.records if "/v1/traces" in r.getMessage()], "보내기마다 로그 줄이 남는다"


def test_주소를_못_읽으면_끄고_한_줄로_알린다(trace_on, capsys):
    trace_on("127.0.0.1:8750")
    assert not ct.enabled() and "주소로 읽지 못해" in capsys.readouterr().out
    trace_on("http://127.0.0.1:8750")
    assert ct._endpoint() == "http://127.0.0.1:8750/v1/traces"
    trace_on("http://127.0.0.1:8750/v1/traces/")
    assert ct._endpoint() == "http://127.0.0.1:8750/v1/traces"


# ── 엔진의 자리 ──────────────────────────────────────────────────────────────────────────────────────
def test_좌석_태스크의_id_는_그_좌석의_호출에만_붙는다(trace_on, captured):
    """_round_live 는 좌석마다 태스크를 만든다 — 좌석 id 는 그 태스크 안에만 있고, 라운드를 부른 쪽에는 남지 않는다."""
    import deliberation as d  # noqa: PLC0415
    trace_on(f"http://127.0.0.1:{_free_port()}")
    llm = ct.instrument_llm(FakeListChatModel(responses=['{"say": "a"}']), "fake")
    personas = [{"key": "seat-a", "role": "r"}, {"key": "seat-b", "role": "r"}]

    async def go():
        ct.tag(kind="delib", job="delib-job-1")
        outs = [o async for o in d._round_live(llm, personas, lambda p: "말하라", 2)]
        await llm.ainvoke([("human", "의장")])
        return outs, dict(ct._IDS.get())
    outs, after = asyncio.run(go())
    assert sorted(o["persona"] for o in outs) == ["seat-a", "seat-b"]
    seats = sorted((_attrs(s).get("hwax.seat", ""), _attrs(s).get("hwax.round", 0)) for s in captured)
    assert seats == [("", 0), ("seat-a", 2), ("seat-b", 2)], seats
    assert all(_attrs(s)["hwax.job_id"] == "delib-job-1" and _attrs(s)["hwax.llm.server"] == "fake" for s in captured)
    assert after == {"kind": "delib", "job": "delib-job-1"}, "좌석·라운드가 부른 쪽 문맥으로 새지 않는다"


def test_게이트웨이_도구는_켜져_있을_때만_감싼다(trace_on, captured, monkeypatch):
    import app as a  # noqa: PLC0415
    made = []

    class _Client:
        def __init__(self, _scoped):
            pass

        async def get_tools(self):
            t = StructuredTool.from_function(coroutine=_tool_ok, name="search_reports", description="d")
            made.append((t, t.coroutine))
            return [t]
    monkeypatch.setattr(a, "MultiServerMCPClient", _Client)
    monkeypatch.delenv(ct.URL_ENV, raising=False)
    ct._reset_for_tests()
    (t0,) = asyncio.run(a._get_tools_retry({}))
    assert t0 is made[0][0] and t0.coroutine is made[0][1], "꺼져 있으면 도구를 건드리지 않는다"
    trace_on(f"http://127.0.0.1:{_free_port()}")
    (t1,) = asyncio.run(a._get_tools_retry({}))
    assert t1.coroutine is not made[1][1]
    capped = a._cap_tool(t1)          # 챗·심의가 그 위에 얹는 래퍼(_prep_tool 의 바깥 껍질) — 그 아래에서 잰다
    asyncio.run(capped.ainvoke({"query": "q"}))
    (s,) = captured
    assert s.name == "search_reports" and s.ok and _attrs(s)["tool.name"] == "search_reports"


def test_챗_턴과_심의_잡이_자리_id_를_단다(trace_on, monkeypatch, tmp_path):
    import app as a  # noqa: PLC0415
    import delib_jobs  # noqa: PLC0415
    import deliberation as d  # noqa: PLC0415
    trace_on(f"http://127.0.0.1:{_free_port()}")

    async def turn():
        await a.chat(a.ChatRequest(message="안녕"))
        return dict(ct._IDS.get())
    ids = asyncio.run(turn())
    assert ids["kind"] == "chat" and len(ids["turn"]) == 16, ids

    seen = {}

    async def entry(_app, q, groups, opts, user_email, user_pat, _x):
        seen["ids"] = dict(ct._IDS.get() or {})
        yield d._sse("done", {})
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    monkeypatch.setattr(delib_jobs, "_PENDING", {})
    monkeypatch.setattr(delib_jobs, "_CLOSING", False)
    monkeypatch.setattr(d, "run_deliberation", entry)

    async def job():
        rec = delib_jobs.start(SimpleNamespace(state=SimpleNamespace()), "general", "화두", user_email="me@example.com")
        for _ in range(100):
            if "ids" in seen:
                break
            await asyncio.sleep(0.02)
        return rec
    rec = asyncio.run(job())
    assert seen["ids"].get("job") == rec["id"], seen


def test_외부_응답자_모델도_켜져_있을_때만_기록기를_단다(trace_on, monkeypatch):
    import external_llm  # noqa: PLC0415
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.delenv(ct.URL_ENV, raising=False)
    ct._reset_for_tests()
    assert external_llm.model_for(app, owner="me@example.com", kind="verdict").callbacks is None
    trace_on(f"http://127.0.0.1:{_free_port()}")
    m = external_llm.model_for(app, owner="me@example.com", kind="verdict")
    assert [c.server for c in m.callbacks] == ["external"]


# ── 반대 검토(10-16) 뒤 — 취소된 호출 · 알림 되풀이 · 싸인 오류 · 고정 바이트 ─────────────────────────────────────────
GOLDEN = bytes.fromhex(
    "0a87030a580a230a0c736572766963652e6e616d6512130a11687761782d6167656e742d7365727665720a310a1a6f70656e696e666572656e63652e70726f6a6563742e6e616d6512130a11687761782d6167656e742d73657276657212aa020a110a0f687761782e63616c6c5f747261636512b0010a100102030405060708090a0b0c0d0e0f10120811121314151617182a036c6c6d30033915cd0bdcacc66c184100ca4a10adc66c184a200a176f70656e696e666572656e63652e7370616e2e6b696e6412050a034c4c4d4a140a09687761782e7365617412070a0572656c2d614a070a0169120218124a120a036e6567120b18fbffffffffffffffff014a0e0a016612092100000000000029404a070a0174120210014a070a016512020a007a02180112620a101f202122232425262728292a2b2c2d2e12082f303132333435362a0e7365617263685f7265706f72747330033905000000000000004105000000000000004a1c0a0a6572726f722e74797065120e0a0c52756e74696d654572726f727a021802")


def test_인코더는_고정_바이트와_같다():
    """공식 정의(opentelemetry-proto)가 없는 venv 에서도 인코더를 본다 — 아래 값은 그 정의로 읽어 이름 · id · 시각 · 종류 · 상태 · 속성
    (음수 · 실수 · 참거짓 · 빈 글)이 그대로인 것을 확인하고 박았다(반대 검토 LOW-4 — 공유 venv 에서는 대조가 늘 건너뛰어졌다)."""
    a = ct._Span("llm", 1_760_000_000_123_456_789, 1_760_000_001_000_000_000,
                 [("openinference.span.kind", "LLM"), ("hwax.seat", "rel-a"), ("i", 18), ("neg", -5), ("f", 12.5), ("t", True), ("e", "")],
                 True)
    b = ct._Span("search_reports", 5, 4, [("error.type", "RuntimeError")], False)
    a.trace_id, a.span_id = bytes(range(1, 17)), bytes(range(17, 25))
    b.trace_id, b.span_id = bytes(range(31, 47)), bytes(range(47, 55))
    assert ct.encode([a, b]) == GOLDEN


class _Slow(http.server.BaseHTTPRequestHandler):
    """답하지 않고 매달리는 OpenAI 꼴 서버 — 호출이 바깥에서 취소되게 한다."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        time.sleep(3)

    def log_message(self, *a):
        pass


def test_취소된_비스트리밍_호출도_스팬을_남긴다(trace_on, captured):
    """LangChain 의 agenerate 는 바깥에서 취소되면 끝 · 오류 콜백을 부르지 않는다 — 시간 초과 · 잡 취소 · 연결 끊김의 가장 긴 대기가
    기록에서 빠졌다(반대 검토 MEDIUM-1). 그 호출을 돌린 태스크가 끝날 때 CancelledError 로 닫는다."""
    trace_on(f"http://127.0.0.1:{_free_port()}")
    srv, port = _serve(_Slow)
    try:
        llm = ct.instrument_llm(_llm(port), "203.0.113.9:8000")
        (rec,) = [cb for cb in llm.callbacks if type(cb).__name__ == "CallTraceRecorder"]

        async def go():
            ct.tag(kind="thinking", seat="s-slow")
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(llm.ainvoke("느린 질문"), 0.3)
        asyncio.run(go())
    finally:
        srv.shutdown()
        srv.server_close()
    (span,) = captured
    a = _attrs(span)
    assert a["error.type"] == "CancelledError" and not span.ok, a
    assert a["hwax.kind"] == "thinking" and a["hwax.seat"] == "s-slow" and a["hwax.llm.server"] == "203.0.113.9:8000"
    assert 0.2e9 <= span.end_ns - span.start_ns < 3e9, "끝 시각은 끊긴 때다(서버가 답한 때가 아니다)"
    assert not rec._open, "끝을 못 본 기록이 남지 않는다"


def test_같은_문맥에서_다음_호출이_시작되면_앞_호출을_닫고_끝난_호출은_두_번_닫지_않는다(trace_on, captured):
    trace_on(f"http://127.0.0.1:{_free_port()}")
    rec = ct._recorder_class()("203.0.113.9:8000")
    done = SimpleNamespace(llm_output={"token_usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}, generations=[])

    async def go():
        ct.tag(seat="s-1")
        rec.on_chat_model_start({}, [[]], run_id="r1", invocation_params={"model": "m"})
        rec.on_chat_model_start({}, [[]], run_id="r2", invocation_params={"model": "m"})     # r1 은 끝 콜백 없이 버려졌다
        rec.on_llm_end(done, run_id="r2")
        last = ct._CUR.get()
        assert last.done and last.task is None and last.cb is None, "끝난 호출은 태스크 끝 콜백을 놓는다(오래 사는 태스크에 쌓이지 않게)"
        rec.on_llm_end(done, run_id="r1")                                                     # 늦게 온 끝 — 이미 닫았다
    asyncio.run(go())                     # 태스크가 끝나도(끝 콜백) 더 내지 않는다
    got = [(_attrs(s).get("error.type"), s.ok, _attrs(s).get("llm.token_count.total")) for s in captured]
    assert got == [("CancelledError", False, None), (None, True, 3)], got
    assert not rec._open


def test_끝난_호출은_태스크가_끝나도_다시_내지_않는다(trace_on, captured):
    trace_on(f"http://127.0.0.1:{_free_port()}")
    srv, port = _serve(_OpenAI)
    try:
        llm = ct.instrument_llm(_llm(port), "203.0.113.9:8000")
        asyncio.run(llm.ainvoke("질문"))
    finally:
        srv.shutdown()
        srv.server_close()
    assert [(_attrs(s).get("error.type"), s.ok) for s in captured] == [(None, True)]


def test_줄이_찼다는_알림은_반_아래로_빠지기_전에는_되풀이되지_않는다(monkeypatch):
    """받는 곳이 살아 있지만 느리면 보낼 때마다 알림이 풀려 묶음마다 한 줄씩 찍혔다(반대 검토 LOW-1, 실측 4초에 19줄)."""
    said = []
    monkeypatch.setattr(ct, "_say", said.append)
    monkeypatch.setattr(ct, "QUEUE_MAX", 10)
    monkeypatch.setattr(ct, "BATCH_MAX", 2)
    gate = threading.Semaphore(0)

    def post(self, body):
        gate.acquire()                    # 한 묶음씩 풀어 준다 — 살아 있지만 느린 받는 곳
        return 200
    monkeypatch.setattr(ct._Exporter, "_post", post)
    exp = ct._Exporter("http://127.0.0.1:1/v1/traces")

    def until(cond, what):
        end = time.monotonic() + 10
        while not cond():
            assert time.monotonic() < end, what
            time.sleep(0.01)

    def fill(n):
        for _ in range(n):
            exp.put(ct._Span("t", 1, 2, [], True))

    def full():
        return [m for m in said if "줄이 찼다" in m]
    try:
        fill(1)
        until(lambda: exp.q.qsize() == 0, "보내는 스레드가 첫 묶음을 쥐지 않았다")
        fill(12)                          # 줄 10 이 차고 둘이 넘친다
        assert len(full()) == 1, said
        gate.release()                    # 한 묶음 성공 — 줄은 아직 반 위다
        until(lambda: exp.sent == 1 and exp.q.qsize() == 8, "다음 묶음을 쥐지 않았다")
        fill(5)                           # 다시 넘친다 — 알리지 않는다
        assert len(full()) == 1, said
        for _ in range(4):
            gate.release()
        until(lambda: exp.sent == 9 and exp.q.qsize() == 2, "줄이 반 아래로 빠지지 않았다")
        fill(9)                           # 반 아래로 빠진 뒤 다시 넘친다 — 한 줄 더
        assert len(full()) == 2, said
    finally:
        for _ in range(64):
            gate.release()
        # 시험 안에서 줄을 다 비운다 — 남은 묶음을 시험 뒤에 원래 _post 로 보내면 빈 포트로 가 경고 한 줄이 시험 밖에 찍힌다
        until(lambda: exp.q.qsize() == 0, "줄을 비우지 못했다")
        time.sleep(0.2)


class _Group(Exception):
    """anyio TaskGroup 이 싼 예외의 꼴 — .exceptions 의 첫 예외가 안쪽이다."""

    def __init__(self, excs):
        super().__init__("group")
        self.exceptions = excs


def test_싸인_HTTP_오류도_안쪽에서_이름과_상태를_함께_읽는다(trace_on, captured):
    trace_on(f"http://127.0.0.1:{_free_port()}")
    req = httpx.Request("POST", "http://127.0.0.1/x")
    err = httpx.HTTPStatusError("x", request=req, response=httpx.Response(502, request=req))
    assert ct._exc_name(_Group([err])) == "HTTPStatusError" and ct._http_status(_Group([err])) == 502
    ct._tool_span("get_material", {}, 1, time.monotonic(), _Group([err]))
    (span,) = captured
    assert _attrs(span)["error.type"] == "HTTPStatusError" and _attrs(span)["hwax.http_status"] == 502, _attrs(span)
