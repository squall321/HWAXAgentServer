# 포털 챗 MCP Apps 패널(HWAXPortal docs/request-1027 HA-20 · 설계 B5.2 · B6 P1) — 진짜 mcp 1.28 FastMCP 가짜 패널 앱을 게이트웨이 꼴로 붙여 묶음 · 사건 · 상한을 본다
"""받아들이는 기준(design-ha09-ha20.md B6 P1).

  · 표지(ui_host="mcp-apps/1")가 있는 턴만 패널 도구가 묶이고, 부르면 ui_resource 사건이 정확히 하나(주소 · 입력 · structuredContent · 결과 _meta).
  · 표지가 없거나 손잡이 CHAT_UI_HOST=0 이면 HA-20 전 그대로다(HA-05) — 이름 집합만 빼고, 집합 밖의 resourceUri 도구는 종전처럼 묶이고
    invoke_tool 로도 간다(반대 검토 10-31 — 넓힌 규칙이 손잡이로 되돌릴 수 없는 길을 닫았다). 심의 · 루틴 · 카탈로그도 같다.
  · invoke_tool 경유는 표지 턴에서만 배운 패널 이름까지 막는다(직접 부르라고 한다). 자동 재시도(ainvoke) 안에서 부른 패널 도구의 사건도 결과 앞에 낸다.
  · 사건이 상한을 넘으면 result 를 빼고 cut. 자기검증을 켜도 사건은 턴 도중에 나간다. 모델에 가는 결과는 그대로다(결과 _meta 는 모델 ·
    자기검증 · 다음 턴에 가지 않는다).

가짜 패널 앱은 SimFlow 꼴의 이름 하나(open_workflow_panel — HA-05 이름 집합)와 집합 밖 이름(open_fake_panel)을 함께 낸다 — resourceUri 로 알아보는
규칙과 이름 집합이 같은 길로 가는지 본다. 빈 포트에 띄우고 끝나면 내린다(떠 있는 9110 과 겹치지 않는다).
"""
import asyncio
import json
import socket
import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult  # noqa: E402
from mcp import types as mt  # noqa: E402
from pydantic import Field  # noqa: E402

import app as a  # noqa: E402
import deliberation as d  # noqa: E402
import routine as rt  # noqa: E402
import ui_panels as ui  # noqa: E402

SECRET = "PANEL-ONLY-0f3a"            # 결과 _meta 에만 있는 값 — 모델 · 자기검증 · 상태줄 어디에도 보이면 안 된다
URI = "ui://fake/panel.html"
_REAL_AGENT_FOR = a._agent_for


@pytest.fixture(scope="module")
def panel_gw():
    """진짜 FastMCP(mcp 1.28) — 패널 도구 셋 · 패널 전용 업로드 도구 · 일반 도구 · invoke_tool 대역 · ui:// 리소스. 받은 호출을 남긴다."""
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("hwax-fake-panel", stateless_http=True, streamable_http_path="/")
    calls: list = []

    def _panel_result(case: str, big: bool = False) -> mt.CallToolResult:
        rows = [{"i": i, "v": "x" * 64} for i in range(6000)] if big else [{"i": 1}]
        return mt.CallToolResult(content=[mt.TextContent(type="text", text=f"패널 데이터 준비 — case={case} · 행 {len(rows)}")],
                                 structuredContent={"case": case, "rows": rows},
                                 _meta={"viewer": {"session": SECRET}})

    @mcp.tool(meta={"ui": {"resourceUri": URI}})
    async def open_fake_panel(case: str = "") -> mt.CallToolResult:
        """가짜 패널을 연다(집합 밖 이름)."""
        calls.append(("open_fake_panel", case))
        return _panel_result(case)

    @mcp.tool(meta={"ui/resourceUri": "ui://fake/workflow.html"})
    async def open_workflow_panel(case: str = "") -> mt.CallToolResult:
        """SimFlow 꼴 이름의 패널(옛 키 ui/resourceUri)."""
        calls.append(("open_workflow_panel", case))
        return _panel_result(case)

    @mcp.tool(meta={"ui": {"resourceUri": URI}})
    async def big_panel(case: str = "") -> mt.CallToolResult:
        """결과가 상한을 넘는 패널."""
        calls.append(("big_panel", case))
        return _panel_result(case, big=True)

    @mcp.tool(meta={"ui": {"visibility": ["app"]}})
    async def panel_upload_begin(name: str = "") -> str:
        """패널만 부르는 업로드 시작(HA-05 이름 · 주소 없음)."""
        calls.append(("panel_upload_begin", name))
        return "upload-1"

    @mcp.tool(meta={"ui": {"resourceUri": URI, "visibility": ["app"]}})
    async def open_app_only_panel(case: str = "") -> mt.CallToolResult:
        """패널 전용(모델에 숨김) 패널 도구."""
        calls.append(("open_app_only_panel", case))
        return _panel_result(case)

    @mcp.tool()
    async def plain_lookup(q: str = "") -> str:
        """일반 조회."""
        calls.append(("plain_lookup", q))
        return f"조회 결과 {q}"

    @mcp.tool()
    async def run_job(project_id: str = "") -> str:
        """잡 넣기 — 확인 관문 바닥 목록(_CONFIRM_JOB_TOOLS)의 이름이다(재시도 안에서 막히는 갈래를 본다)."""
        calls.append(("run_job", project_id))
        return f"job J-1 for {project_id}"

    @mcp.tool()
    async def invoke_tool(name: str, arguments: dict | None = None) -> str:
        """게이트웨이 invoke_tool 대역 — 받은 안쪽 이름을 남긴다."""
        calls.append(("invoke_tool", name))
        return f"called {name}"

    @mcp.resource(URI, mime_type="text/html;profile=mcp-app")
    def panel_html() -> str:
        return "<!doctype html><html><body>fake</body></html>"

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "가짜 패널 앱이 뜨지 않았다"
    yield {"gateway": {"url": f"http://127.0.0.1:{port}/", "transport": "streamable_http"}}, calls
    server.should_exit = True
    th.join(timeout=10)


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ui, "_LEARNED", set())
    monkeypatch.setattr(ui, "ON", True)
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    monkeypatch.setattr(a, "_TOOLS_MAP_CACHE", {"at": time.time(), "routine_read": None, "areas": {}, "area_meta": {}, "map": {
        n: "fake-panel" for n in ("open_fake_panel", "open_workflow_panel", "big_panel", "panel_upload_begin", "open_app_only_panel",
                                  "plain_lookup")}})
    monkeypatch.setenv("CHAT_AUTO_RETRY", "0")


class _Script(BaseChatModel):
    """대본대로 부르는 모델 — steps 원소는 [(도구, 인자) …] 이거나 글. 받은 메시지를 호출마다 적는다(tests/test_status_wait 의 꼴)."""

    steps: list = Field(default_factory=list)
    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "ui-host-script"

    def bind_tools(self, tools, **_kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **_kw):
        self.seen.append(list(messages))
        step = self.steps[min(len(self.seen), len(self.steps)) - 1]
        msg = (AIMessage(content=step) if isinstance(step, str) else
               AIMessage(content="", tool_calls=[{"name": n, "args": ar, "id": f"c{len(self.seen)}_{i}"} for i, (n, ar) in enumerate(step)]))
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(self, messages, stop=None, run_manager=None, **kw):
        msg = self._generate(messages, stop, run_manager, **kw).generations[0].message
        yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content, tool_call_chunks=[
            {"name": c["name"], "args": json.dumps(c["args"], ensure_ascii=False), "id": c["id"], "index": i}
            for i, c in enumerate(msg.tool_calls)]))


def _wire(monkeypatch, llm, conns) -> dict:
    """앱이 실제로 만드는 길(_agent_for — 진짜 어댑터 · 진짜 게이트웨이 꼴 서버)로 에이전트를 짓는다. 받은 allow_panels · 묶인 이름을 남긴다."""
    state = types.SimpleNamespace(llm=llm, connections=conns, agent_cache={}, tool_degraded={}, tool_load_error={}, tool_snapshot={})
    seen: dict = {"allow": [], "bound": []}

    async def built(_app, groups, *args, **kw):
        seen["allow"].append(kw.get("allow_panels", False))
        ag = await _REAL_AGENT_FOR(types.SimpleNamespace(state=state), groups, *args, **kw)
        seen["bound"].append(tuple(getattr(ag, "_hwax_tools", ())))
        return ag

    monkeypatch.setattr(a, "_agent_for", built)
    return seen


def _events(chunks) -> list:
    out = []
    for c in chunks:
        ev, data = a._sse_parse(c)
        if ev:
            out.append((ev, data))
    return out


def _turn(req) -> list:
    async def go():
        return [c async for c in a._agent_stream(a.app, req)]
    return _events(asyncio.run(go()))


def _req(**kw):
    return a.ChatRequest(message="가짜 패널 열어서 case A 보여줘", groups=["portal-admin"], history=[], **kw)


PANEL_NAMES = {"open_fake_panel", "open_workflow_panel", "big_panel", "open_app_only_panel", "panel_upload_begin"}
HA05_NAMES = {n for n in PANEL_NAMES if a._chat_panel_tool(n)}          # HA-05 이름 집합(_CHAT_PANEL_TOOLS)에 드는 것 — 표지 없는 턴은 이것만 뺀다
OUT_OF_SET = PANEL_NAMES - HA05_NAMES                                   # 주소(resourceUri)로만 패널인 것 — 표지 없는 턴은 HA-20 전처럼 묶는다


def _ha05(conns) -> set:
    """HA-20 전의 거르기 — 진짜 어댑터 목록에서 _agent_denied(외부 응답자 과제 · HA-05 이름 집합 · 루틴)만 뺀다. 표지 없는 길의 오라클이다."""
    return {t.name for t in asyncio.run(a.MultiServerMCPClient(conns).get_tools()) if not a._agent_denied(t.name)}


# ── 묶음 ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_P1_표지_턴만_패널_도구를_묶고_패널_전용은_모델에_안_묶는다(panel_gw):
    conns, _ = panel_gw
    on = {t.name for t in asyncio.run(a._get_tools_retry(conns, allow_panels=True))}
    off = {t.name for t in asyncio.run(a._get_tools_retry(conns))}
    assert {"open_fake_panel", "open_workflow_panel", "big_panel", "plain_lookup", "invoke_tool"} <= on, on
    assert not on & {"panel_upload_begin", "open_app_only_panel"}, "visibility 에 model 이 없는 도구는 표지 턴에도 모델에 안 묶는다"
    assert off == _ha05(conns), ("표지 없는 턴은 HA-20 전 그대로(HA-05 이름 집합만 뺀다)", off)
    assert not off & HA05_NAMES and OUT_OF_SET <= off, off
    assert ui.known("open_fake_panel") and ui.known("fake-panel_open_fake_panel") and not ui.known("plain_lookup")


def test_P1_심의_루틴_카탈로그는_HA20_전_그대로다(panel_gw, monkeypatch):
    """심의 좌석 · 루틴 · /도구 카탈로그는 표지를 받지 않는다 — HA-05 이름 집합만 빼고 나머지는 HA-20 전처럼 본다(B1 기준 3 · B5.6 — 집합 밖의
    resourceUri 도구를 어떻게 다룰지는 Q-A1 답 뒤 사람 결정이다)."""
    conns, _ = panel_gw
    app_ = types.SimpleNamespace(state=types.SimpleNamespace(connections=conns))
    seat = set(asyncio.run(d._tools_by_name(app_, [])))
    rtn = set(asyncio.run(rt._user_tools(app_, [], "u1@corp.example", "")))

    async def cat():
        return [a._sse_parse(c) async for c in a.run_tool_search(app_, "패널", [])]
    tools = [dd for ev, dd in asyncio.run(cat()) if ev == "tools"]
    catalog = {t["name"] for t in tools[0]["all"]} if tools else set()
    for name, got in (("심의", seat), ("루틴", rtn), ("카탈로그", catalog)):
        assert "plain_lookup" in got and not got & HA05_NAMES and OUT_OF_SET <= got, (name, got)


def test_P1_표지_턴의_에이전트와_스냅샷을_표지_없는_턴이_재사용하지_않는다(panel_gw, monkeypatch):
    """에이전트 캐시 · 도구 스냅샷(게이트웨이가 죽었을 때의 폴백)은 같은 사람 · 같은 그룹이면 다시 쓴다 — 표지가 키에 없으면 표지 턴이 묶은 패널
    도구를 표지 없는 턴이 받아 HA-05 가 풀린다."""
    conns, _ = panel_gw
    state = types.SimpleNamespace(llm=_Script(steps=["답"]), connections=conns, agent_cache={}, tool_degraded={}, tool_load_error={},
                                  tool_snapshot={})
    fake = types.SimpleNamespace(state=state)
    first = asyncio.run(_REAL_AGENT_FOR(fake, ["admin"]))
    with_panels = asyncio.run(_REAL_AGENT_FOR(fake, ["admin"], allow_panels=True))
    plain = asyncio.run(_REAL_AGENT_FOR(fake, ["admin"]))
    assert "open_workflow_panel" in with_panels._hwax_tools and plain is first and not set(plain._hwax_tools) & HA05_NAMES
    # 게이트웨이가 죽었다 — 표지 없는 턴의 스냅샷 폴백은 표지 없는 턴이 받은 목록이다(나중에 받은 표지 턴의 목록을 집지 않는다)
    state.agent_cache.clear()
    monkeypatch.setattr(a, "_get_tools_retry", _boom)
    down = asyncio.run(_REAL_AGENT_FOR(fake, ["admin"]))
    assert "plain_lookup" in down._hwax_tools and not set(down._hwax_tools) & HA05_NAMES, down._hwax_tools


async def _boom(*_a, **_k):
    raise RuntimeError("gateway down")


# ── 실제 턴 ────────────────────────────────────────────────────────────────────────────────────────────────────

def _direct(conns, name, args) -> ToolMessage:
    """턴 밖(표지 그릇 없음)에서 같은 도구를 같은 래퍼로 부른 결과 — '모델에 가는 결과는 그대로' 의 오라클."""
    tools = {t.name: t for t in asyncio.run(a._get_tools_retry(conns, allow_panels=True))}
    t = a._prep_tool(tools[name], arg_defaults=a._CHAT_ARG_DEFAULTS, chat=True)
    return asyncio.run(t.ainvoke({"name": name, "args": args, "id": "oracle", "type": "tool_call"}))


def test_P1_표지_턴에_부르면_사건_하나이고_모델은_그대로의_결과를_받는다(panel_gw, monkeypatch):
    conns, calls = panel_gw
    llm = _Script(steps=[[("open_fake_panel", {"case": "A"})], "패널을 열었습니다."])
    seen = _wire(monkeypatch, llm, conns)
    evs = _turn(_req(ui_host="mcp-apps/1"))
    assert seen["allow"] == [True] and "open_fake_panel" in seen["bound"][0]
    ui_evs = [dd for ev, dd in evs if ev == "ui_resource"]
    assert len(ui_evs) == 1, evs
    u = ui_evs[0]
    done = [dd for ev, dd in evs if ev == "status" and dd.get("step") == "도구 완료: open_fake_panel"]
    assert done and u["call"] == done[0]["call"] and u["call"], "사건의 call 이 상태줄의 call(도구 실행 id)과 같다"
    assert (u["tool"], u["app"], u["uri"], u["input"]) == ("open_fake_panel", "fake-panel", URI, {"case": "A"})
    assert u["result"]["structuredContent"] == {"case": "A", "rows": [{"i": 1}]}
    assert u["result"]["_meta"] == {"viewer": {"session": SECRET}} and "cut" not in u
    assert u["result"]["content"] == [{"type": "text", "text": "패널 데이터 준비 — case=A · 행 1"}]
    names = [ev for ev, _ in evs]
    assert names.index("ui_resource") < names.index("result") < names.index("done"), "결과 · 끝 앞에 낸다"
    # 모델에 가는 결과 — 표지 그릇 없는 직접 호출과 글자까지 같고 결과 _meta 는 없다
    tm = [m for m in llm.seen[1] if isinstance(m, ToolMessage)][-1]
    oracle = _direct(conns, "open_fake_panel", {"case": "A"})
    assert tm.content == oracle.content and SECRET not in str(tm.content) and SECRET not in str(tm.artifact)
    assert not any(SECRET in json.dumps(dd, ensure_ascii=False) for ev, dd in evs if ev != "ui_resource"), "_meta 는 패널 사건에만"
    assert calls.count(("open_fake_panel", "A")) == 2, "턴 한 번 · 오라클 한 번"


@pytest.mark.parametrize("why", ["no-flag", "knob-off", "other-flag"])
def test_P1_표지가_없거나_손잡이가_끄면_HA05_그대로다(panel_gw, monkeypatch, why):
    """HA-20 전과 같다 — HA-05 이름 집합만 묶지 않고 invoke_tool 로도 막는다. 집합 밖의 resourceUri 도구(가짜 앱 · ARP arp_upload_open 꼴)는 종전처럼
    묶이고 invoke_tool 로도 게이트웨이에 간다(반대 검토 10-31 — 손잡이 CHAT_UI_HOST=0 으로 종전 동작을 되돌릴 수 있어야 한다). 사건은 없다."""
    conns, calls = panel_gw
    before = len(calls)
    llm = _Script(steps=[[("invoke_tool", {"name": "fake-panel_open_fake_panel", "arguments": {"case": "K"}}),
                          ("invoke_tool", {"name": "open_workflow_panel", "arguments": {}})], "패널 없이 답합니다."])
    seen = _wire(monkeypatch, llm, conns)
    if why == "knob-off":
        monkeypatch.setattr(ui, "ON", False)
    req = _req(ui_host={"no-flag": "", "knob-off": "mcp-apps/1", "other-flag": "mcp-apps/2"}[why])
    evs = _turn(req)
    bound = set(seen["bound"][0])
    assert seen["allow"] == [False] and not bound & HA05_NAMES and OUT_OF_SET <= bound, seen
    assert not [ev for ev, _ in evs if ev == "ui_resource"]
    tms = {m.tool_call_id: m.content for m in llm.seen[1] if isinstance(m, ToolMessage)}
    assert ("invoke_tool", "fake-panel_open_fake_panel") in calls[before:], "집합 밖 이름은 invoke_tool 로도 종전처럼 간다"
    assert ("invoke_tool", "open_workflow_panel") not in calls[before:] and "패널을 그리는 클라이언트 전용 도구" in tms["c1_1"], tms


def test_P1_invoke_tool_경유는_표지_턴에도_막고_직접_부르라고_한다(panel_gw, monkeypatch):
    conns, calls = panel_gw
    before = len(calls)
    llm = _Script(steps=[[("invoke_tool", {"name": "open_fake_panel", "arguments": {"case": "B"}})], "끝"])
    _wire(monkeypatch, llm, conns)
    evs = _turn(_req(ui_host="mcp-apps/1"))
    tm = [m for m in llm.seen[1] if isinstance(m, ToolMessage)][-1]
    assert tm.content.startswith(a._TOOL_FAIL_MARK) and "직접 묶인 그 이름으로" in tm.content, tm.content
    assert ("invoke_tool", "open_fake_panel") not in calls[before:] and not [ev for ev, _ in evs if ev == "ui_resource"]
    # 표지 턴 — 이름 집합의 패널 도구도 경유로는 막는다(직접 묶여 있다)
    llm2 = _Script(steps=[[("invoke_tool", {"name": "fake-panel_open_workflow_panel", "arguments": {}})], "끝"])
    _wire(monkeypatch, llm2, conns)
    _turn(_req(ui_host="mcp-apps/1"))
    tm2 = [m for m in llm2.seen[1] if isinstance(m, ToolMessage)][-1]
    assert "직접 묶인 그 이름으로" in tm2.content and ("invoke_tool", "fake-panel_open_workflow_panel") not in calls[before:]


def test_P1_사건이_상한을_넘으면_결과를_빼고_cut_만_싣는다(panel_gw, monkeypatch):
    conns, _ = panel_gw
    llm = _Script(steps=[[("big_panel", {"case": "C"})], "끝"])
    _wire(monkeypatch, llm, conns)
    evs = _turn(_req(ui_host="mcp-apps/1"))
    (u,) = [dd for ev, dd in evs if ev == "ui_resource"]
    assert "result" not in u and u["cut"] > ui.MAX_BYTES and u["input"] == {"case": "C"} and u["uri"] == URI
    assert len(json.dumps(u, ensure_ascii=False).encode()) <= ui.MAX_BYTES


class _Reviewer:
    """자기검증 검토자 대역(app.state.chat_llm 자리) — 받은 메시지를 남기고 '고칠 것 없음' 으로 답한다(tests/test_chat_self_check 의 꼴)."""

    def __init__(self):
        self.calls: list = []

    async def astream(self, msgs, **kw):
        self.calls.append(msgs)
        for part in ("[판정] 고칠 것 없음\n", "[답]\n"):
            yield AIMessageChunk(content=part, response_metadata={"model_name": "qwen-x"})
        yield AIMessageChunk(content="", response_metadata={"finish_reason": "stop", "model_name": "qwen-x"})


def test_P1_자기검증을_켜도_사건은_턴_도중에_나가고_검토자는_결과_meta_를_못_본다(panel_gw, monkeypatch):
    conns, _ = panel_gw
    monkeypatch.setenv("CHAT_SELF_CHECK", "1")
    rv = _Reviewer()
    monkeypatch.setattr(a.app.state, "chat_llm", rv, raising=False)
    llm = _Script(steps=[[("open_fake_panel", {"case": "D"})], "case D 패널 데이터는 1행입니다."])
    _wire(monkeypatch, llm, conns)
    evs = _turn(_req(ui_host="mcp-apps/1"))
    names = [ev for ev, _ in evs]
    checking = next(i for i, (ev, dd) in enumerate(evs) if ev == "status" and "답을 검증하는 중" in str(dd.get("step")))
    assert names.count("ui_resource") == 1 and names.index("ui_resource") < checking < names.index("result"), names
    assert rv.calls and SECRET not in json.dumps([[getattr(m, "content", m) for m in c] for c in rv.calls], ensure_ascii=False, default=str)
    meta = [dd for ev, dd in evs if ev == "meta"][-1]
    assert meta["self_check"]["status"] in ("ok", "skipped"), meta


class _FailFirstStream(_Script):
    """스트리밍(astream_events)의 첫 모델 호출이 글 한 자 없이 상류 오류로 터진다 — 자동 재시도(ainvoke)는 대본대로 간다. fail_last 면 재시도의
    마지막 모델 호출(도구 결과를 받은 뒤)도 터진다."""

    failed: int = 0
    fail_last: bool = False

    def _stream(self, messages, stop=None, run_manager=None, **kw):
        if self.failed == 0:
            self.failed += 1
            raise RuntimeError("upstream 502 Bad Gateway — 일시 장애")
        yield from super()._stream(messages, stop, run_manager, **kw)

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        if self.fail_last and len(self.seen) >= 1:
            self.seen.append(list(messages))
            raise RuntimeError("upstream 502 Bad Gateway — 재시도도 끝에서 끊겼다")
        return super()._generate(messages, stop, run_manager, **kw)


@pytest.mark.parametrize("self_check", [False, True])
def test_P1_자동_재시도_안에서_부른_패널_도구의_사건도_결과_앞에_나간다(panel_gw, monkeypatch, self_check):
    """첫 스트리밍 호출이 글 없이 터지면 자동 재시도(agent.ainvoke)가 그래프를 다시 돈다 — 끝 사건(on_tool_end)이 없는 길이라 사건은 턴 그릇에
    남는다. 도구는 실행됐으니 사건이 결과 · 끝 앞에 하나 나가야 한다(반대 검토 10-31 — 예외 갈래가 그릇을 비우지 않고 끝나 패널이 조용히 사라졌다)."""
    conns, calls = panel_gw
    monkeypatch.setenv("CHAT_AUTO_RETRY", "1")
    monkeypatch.setenv("CHAT_SELF_CHECK", "1" if self_check else "0")
    monkeypatch.setattr(a.app.state, "chat_llm", _Reviewer(), raising=False)
    before = len(calls)
    llm = _FailFirstStream(steps=[[("open_fake_panel", {"case": "R"})], "패널을 열었습니다."])
    _wire(monkeypatch, llm, conns)
    evs = _turn(_req(ui_host="mcp-apps/1"))
    names = [ev for ev, _ in evs]
    ui_evs = [dd for ev, dd in evs if ev == "ui_resource"]
    assert calls[before:].count(("open_fake_panel", "R")) == 1 and len(ui_evs) == 1, names
    assert ui_evs[0]["call"] == "" and ui_evs[0]["result"]["structuredContent"]["case"] == "R" and ui_evs[0]["uri"] == URI
    assert names.index("ui_resource") < names.index("result") < names.index("done") and "error" not in names, names
    assert not any(SECRET in json.dumps(dd, ensure_ascii=False) for ev, dd in evs if ev != "ui_resource"), "_meta 는 패널 사건에만"


def test_P1_자동_재시도_안에서_확인_관문이_막아도_패널_사건은_카드_결과_앞에_나간다(panel_gw, monkeypatch):
    """재시도 안에서 확인 관문(HA-29)이 잡을 막으면 그 턴은 카드로 끝난다 — 같은 걸음에서 부른 패널 도구의 사건도 카드 · 결과 앞에 나간다."""
    conns, calls = panel_gw
    monkeypatch.setenv("CHAT_AUTO_RETRY", "1")
    monkeypatch.setattr(a, "CHAT_CONFIRM_GATE", True)
    monkeypatch.setattr(a, "_CONFIRM_STAT", a.Counter())
    before = len(calls)
    llm = _FailFirstStream(steps=[[("open_fake_panel", {"case": "G"}), ("run_job", {"project_id": "P1"})], "패널을 열고 잡을 걸었습니다."])
    _wire(monkeypatch, llm, conns)
    evs = _turn(_req(ui_host="mcp-apps/1", user_email="u@x.com", conversation_id="c-ha20"))
    names = [ev for ev, _ in evs]
    assert ("open_fake_panel", "G") in calls[before:] and ("run_job", "P1") not in calls[before:], calls[before:]
    assert names.count("confirm") == 1 and names.count("ui_resource") == 1, names
    assert names.index("ui_resource") < names.index("result") < names.index("done"), names


def test_P1_자동_재시도가_도구를_부른_뒤_끝내_실패해도_패널_사건은_오류_앞에_나간다(panel_gw, monkeypatch):
    """재시도가 패널 도구를 실행한 뒤 마지막 모델 호출에서 터지면 턴은 오류로 끝난다 — 도구는 실행됐으니 사건을 버리지 않는다(오류 · 끝 앞)."""
    conns, calls = panel_gw
    monkeypatch.setenv("CHAT_AUTO_RETRY", "1")
    before = len(calls)
    llm = _FailFirstStream(steps=[[("open_fake_panel", {"case": "E"})], "닿지 않는 답"], fail_last=True)
    _wire(monkeypatch, llm, conns)
    evs = _turn(_req(ui_host="mcp-apps/1"))
    names = [ev for ev, _ in evs]
    assert ("open_fake_panel", "E") in calls[before:] and "result" not in names, names
    assert names.count("ui_resource") == 1 and names.index("ui_resource") < names.index("error") < names.index("done"), names


# ── 단위 — 그릇 · 꼴 ───────────────────────────────────────────────────────────────────────────────────────────

def test_P1_끝_사건이_없던_사건은_턴_끝에_call_없이_나간다():
    t = ui.Turn()
    slot = {"content": [], "structuredContent": None, "_meta": None, "seen": True}
    t.add(ui.event(tool="p1", app="x", uri=URI, args={"runtime": object(), "k": 1}, slot=slot))
    t.add(ui.event(tool="p2", app="x", uri=URI, args={}, slot=slot))
    assert t.take("p2", "run-2")[0]["call"] == "run-2" and t.take("p2", "run-x") == []
    rest = t.rest()
    assert [(e["tool"], e["call"], e["input"]) for e in rest] == [("p1", "", {"k": 1})] and t.items == [] and t.sent == 2


def test_P1_도구_목록의_meta_로만_알아본다():
    def tool(meta):
        return types.SimpleNamespace(name="t", metadata={"_meta": meta} if meta is not None else None)
    assert ui.resource_uri(tool({"ui": {"resourceUri": URI}})) == URI
    assert ui.resource_uri(tool({"ui/resourceUri": URI})) == URI
    assert ui.resource_uri(tool({"ui": {"resourceUri": "https://evil/x.html"}})) is None, "ui:// 만"
    assert ui.resource_uri(tool(None)) is None and not ui.model_hidden(tool(None))
    assert ui.model_hidden(tool({"ui": {"visibility": ["app"]}})) and not ui.model_hidden(tool({"ui": {"visibility": ["model", "app"]}}))
    assert ui.HOST_TOKEN == "mcp-apps/1" and ui.EVENT == "ui_resource"
