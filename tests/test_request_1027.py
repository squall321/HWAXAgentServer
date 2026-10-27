# 10-27 수정 요청(HWAXPortal docs/request-1027 03-HWAXAgentServer.md)의 에이전트 서버 항목 — 항목마다 고친 자리를 실제로 돌려 본다
"""항목 번호(HA-xx)는 요청서의 것이다. 판단의 까닭은 HWAXPortal docs/request-1027/context-notes.md 에 있다."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from langchain_core.tools import StructuredTool, tool  # noqa: E402

import app as a  # noqa: E402
import external_llm as x  # noqa: E402


# ── HA-05 — 패널을 그리지 못하는 챗 · 심의는 패널 전용 도구를 묶지도 부르지도 않는다 ─────────────────────────
def test_HA05_패널_전용_도구는_정확한_이름과_별칭만_막는다():
    for n in ("open_resin_panel", "simflow_open_resin_panel", "OPEN_WORKFLOW_PANEL", " panel_upload_commit ",
              "open_camfpcb_panel", "open_frc_panel", "open_gi_panel", "open_magnet_panel", "panel_upload_begin", "panel_upload_append"):
        assert a._agent_denied(n), n
    for n in ("risk_list_panels", "list_panels", "open_resin_panel_help", "prepare_run", "arp_upload_open", "panel"):
        assert not a._agent_denied(n), f"{n} 는 막을 이름이 아니다 — `*panel*` 무늬로 넓히면 조회 도구까지 걸린다"
    assert a._AGENT_DENY_TOOLS == frozenset(x.TASK_TOOLS), "외부 응답자 과제 도구 집합은 그대로다(따로 둔다)"


def test_HA05_invoke_tool_로도_패널_도구를_부르지_못하고_까닭을_말한다():
    calls = []

    @tool("invoke_tool")
    async def invoke(name: str, arguments: dict | None = None) -> str:
        """게이트웨이 invoke_tool 대역."""
        calls.append(name)
        return f"called {name}"

    t = a._prep_tool(invoke)

    async def go():
        return [await t.ainvoke({"name": n, "arguments": {}}) for n in ("open_resin_panel", "simflow_panel_upload_begin", "prepare_run")]

    out = asyncio.run(go())
    assert calls == ["prepare_run"], "막아야 할 이름이 게이트웨이까지 갔다"
    for o in out[:2]:
        assert o.startswith(a._TOOL_FAIL_MARK) and "패널" in o and "prepare_run" in o and "Claude Code" not in o, o
    assert out[2] == "called prepare_run"


# ── HA-08 — 위키가 꺼진 박스에서는 부르지 않고 한 줄만, 다단 심의는 한 번만 ─────────────────────────────────────
import deliberation as d  # noqa: E402

_KW = dict(question="q", decision="결정문", stage="default", personas=[{"key": "mech-a", "role": "기구"}], last_list=[],
           rid=None, evidence=[], pool=[])


class _WikiTool:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self.answer


def _two_stages(tools, bind=True):
    async def go():
        if bind:
            d._wiki_bind("CAEG", "job-1")
        first = await d._wiki_submit(tools, user="u1@corp.example", chair_failed=None, **_KW)
        second = await d._wiki_submit(tools, user="u1@corp.example", chair_failed=None, **{**_KW, "stage": "s2"})
        return first, second
    return asyncio.run(go())


def test_HA08_위키_도구가_없으면_부르지_않고_한_줄_다음_단은_조용하다():
    """MX 백서는 위키가 꺼지면 wiki_* 를 싣지 않는다(MW-01) — 종전엔 결정문 끝마다(다단이면 단마다) '⚠ 남기지 못했다 …' 가 붙었다."""
    first, second = _two_stages({})
    assert first == d._WIKI_OFF_NOTE and "꺼져 있거나" in first and "⚠" not in first
    assert second == "", "다단 심의의 다음 단은 같은 줄을 다시 붙이지 않는다"
    assert _two_stages({})[0] == d._WIKI_OFF_NOTE, "새 심의는 새로 본다(상태는 심의 하나의 것이다)"


def test_HA08_도구가_실려도_wiki_disabled_면_꺼짐으로_읽고_다시_부르지_않는다():
    """API 가 늦게 떠 mxwp-mcp 가 위키 도구를 실은 채 위키가 꺼진 경우 — 부른 결과가 wiki_disabled 다."""
    t = _WikiTool("(tool wiki_submit_source error: 404 wiki_disabled — MX 백서 AI 위키는 아직 열리지 않았다)")
    first, second = _two_stages({d._WIKI_TOOL: t})
    assert first == d._WIKI_OFF_NOTE and second == "" and len(t.calls) == 1


def test_HA08_켜진_위키의_다른_실패와_성공은_단마다_그대로다():
    bad = _WikiTool("(tool wiki_submit_source error: 보낸 사람이 갖지 않은 범위 토큰이다)")
    first, second = _two_stages({d._WIKI_TOOL: bad})
    assert "⚠ MX 백서 위키에 남기지 못했다" in first and "범위 토큰" in second and len(bad.calls) == 2, "꺼짐이 아닌 실패는 단마다 알린다"
    ok = _WikiTool('{"id": "4f1e", "short": "src_4f1e", "dedup": false, "ingest_state": "queued"}')
    first, second = _two_stages({d._WIKI_TOOL: ok})
    assert "src_4f1e" in first and "src_4f1e" in second and len(ok.calls) == 2, "원천은 단마다 남긴다(P8)"


# ── HA-24 — 챗 경로의 plm_defect_search 는 enrich 를 주지 않으면 0(사용자 결정 D-3 — 챗 쪽만 먼저) ──────────────────────
def _plm_tool(name="plm_defect_search"):
    """진짜 MCP 어댑터 도구와 같은 꼴 — args_schema 가 JSON 스키마 dict 이고 coroutine 이 받은 인자만 넘긴다(langchain_mcp_adapters).
    pydantic 스키마로 지으면 LangChain 이 기본값(5)을 먼저 채워 이 시험이 아무것도 못 본다(가짜는 진짜 꼴로)."""
    seen = []

    async def call_tool(**arguments):
        seen.append(arguments.get("enrich", "서버 기본(5)"))
        return "ok"
    schema = {"type": "object", "properties": {"query": {"type": "string"}, "enrich": {"type": "integer", "default": 5}},
              "required": ["query"]}
    return StructuredTool(name=name, description="PLM 불량 검색", args_schema=schema, coroutine=call_tool), seen


def test_HA24_챗_경로는_enrich_를_주지_않으면_0_이고_준_값은_그대로다():
    t, seen = _plm_tool()
    t = a._prep_tool(t, arg_defaults=a._CHAT_ARG_DEFAULTS)
    asyncio.run(t.ainvoke({"query": "힌지"}))
    asyncio.run(t.ainvoke({"query": "힌지", "enrich": 3}))
    assert seen == [0, 3], seen
    assert "실시간 상태를 요구할 때만" in t.description, "모델이 언제 올릴지 설명에서 읽는다"
    alias, seen2 = _plm_tool("plmdefect_plm_defect_search")       # 게이트웨이 충돌 접두
    asyncio.run(a._prep_tool(alias, arg_defaults=a._CHAT_ARG_DEFAULTS).ainvoke({"query": "x"}))
    assert seen2 == [0]


def test_HA24_심의_경로는_그대로다():
    t, seen = _plm_tool()
    t = a._prep_tool(t, 4000, 2400)                                 # 심의가 부르는 꼴(deliberation._prep_tool(t, result_max, desc_max))
    asyncio.run(t.ainvoke({"query": "힌지"}))
    assert seen == ["서버 기본(5)"] and "실시간 상태" not in t.description


def test_HA24_invoke_tool_로_불러도_같다():
    got = []

    async def call_tool(**kw):
        got.append((kw.get("name"), kw.get("arguments")))
        return "ok"
    invoke = StructuredTool(name="invoke_tool", description="게이트웨이 invoke_tool 대역", coroutine=call_tool,
                            args_schema={"type": "object", "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}},
                                         "required": ["name"]})
    t = a._prep_tool(invoke, arg_defaults=a._CHAT_ARG_DEFAULTS)
    asyncio.run(t.ainvoke({"name": "plm_defect_search", "arguments": {"query": "x"}}))
    asyncio.run(t.ainvoke({"name": "plm_defect_search", "arguments": '{"query": "y", "enrich": 2}'}))
    asyncio.run(t.ainvoke({"name": "list_materials", "arguments": {}}))
    assert got[0] == ("plm_defect_search", {"enrich": 0, "query": "x"})
    assert got[1][0] == "plm_defect_search" and __import__("json").loads(got[1][1]) == {"enrich": 2, "query": "y"}
    assert got[2] == ("list_materials", {}), "다른 도구의 인자는 건드리지 않는다"


def test_HA24_챗_도구_로드가_기본값을_싣는다():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "_raw = [_prep_tool(t, arg_defaults=_CHAT_ARG_DEFAULTS) for t in _got]" in src


# ── HA-01 — 무캡 경로가 예산 때문에 잘랐으면 턴마다 원본에서 다시 고른다(캐시 키에 질의 · 핀이 없다) ─────────────────────
import types  # noqa: E402


def _fake_tool(name: str, word: str):
    async def call_tool(**_kw):
        return "ok"
    return StructuredTool(name=name, description=f"{word} " * 120, coroutine=call_tool,
                          args_schema={"type": "object", "properties": {"q": {"type": "string"}}})


def _agent_env(monkeypatch, tools):
    loads = []

    class _Client:
        def __init__(self, _connections):
            pass

        async def get_tools(self):
            loads.append(1)
            return list(tools)

    built = []

    def _fake_react(_llm, tools_, prompt=None):
        built.append(tuple(getattr(t, "name", "") for t in tools_))
        return types.SimpleNamespace()

    monkeypatch.setattr(a, "MultiServerMCPClient", _Client)
    monkeypatch.setattr(a, "create_react_agent", _fake_react)
    monkeypatch.setattr(a, "_semantic_order", lambda _q, _t: [])      # 어휘 순위만 — 임베더를 부르지 않는다
    monkeypatch.setattr(a, "TOOL_MAX", 0)
    monkeypatch.setattr(a, "_model_context_tokens", lambda: 8000)    # 예산 max(4000, 3600) — 아래 도구 40개를 다 못 싣는다
    state = types.SimpleNamespace(llm="LLM", chat_llm=None, connections={"gateway": {"url": "http://gw/mcp", "headers": {}}},
                                  agent_cache={}, tool_degraded={}, tool_load_error={}, tool_snapshot={})
    app_ = types.SimpleNamespace(state=state)

    def run(**kw):
        return asyncio.run(a._agent_for(app_, ["CAEG"], user="u1@corp.example", user_pat="pat-1", **kw))
    return run, loads, built


def test_HA01_잘린_묶음은_다음_질문에_다시_고르고_게이트웨이는_다시_안_부른다(monkeypatch):
    tools = [_fake_tool(f"alpha_{i}", "alpha") for i in range(20)] + [_fake_tool(f"beta_{i}", "beta") for i in range(20)]
    run, loads, built = _agent_env(monkeypatch, tools)
    first = run(query="alpha 결과")
    assert any(n.startswith("alpha_") for n in first._hwax_tools) and not any(n.startswith("beta_") for n in first._hwax_tools)
    second = run(query="beta 결과")
    assert any(n.startswith("beta_") for n in second._hwax_tools), "앞 질문의 묶음을 그대로 썼다"
    assert loads == [1], "다시 고르는 데 게이트웨이를 다시 불렀다"
    same = run(query="beta 결과")
    assert same is second and len(built) == 2, "묶음이 같으면 새로 짓지 않는다"
    pinned = run(query="beta 결과", pinned=["alpha_7"])
    assert "alpha_7" in pinned._hwax_tools, "창 중간에 켠 핀이 묶음에 들지 않았다"


def test_HA01_안_잘렸으면_종전대로_재사용한다(monkeypatch):
    run, loads, built = _agent_env(monkeypatch, [_fake_tool(f"alpha_{i}", "alpha")
                                                 for i in range(3)])
    first = run(query="alpha")
    assert run(query="전혀 다른 질문") is first and loads == [1] and len(built) == 1


def test_HA21_캐시는_수명이_지나면_게이트웨이에서_다시_받는다(monkeypatch):
    """서비스 계정 키(PAT 없음)는 기한 없이 옛 카탈로그를 썼다 — 수명(AGENT_CACHE_TTL_S, 기본 30분)이 지나면 다시 받는다."""
    run, loads, _built = _agent_env(monkeypatch, [_fake_tool(f"alpha_{i}", "alpha") for i in range(3)])
    first = run(query="alpha")
    assert run(query="alpha") is first and loads == [1]
    first._hwax_at -= a.AGENT_CACHE_TTL_S + 1
    again = run(query="alpha")
    assert again is not first and loads == [1, 1], "수명이 지난 에이전트를 그대로 썼다"
    monkeypatch.setattr(a, "AGENT_CACHE_TTL_S", 0)
    again._hwax_at -= 10 ** 6
    assert run(query="alpha") is again, "0 은 기한 없음이다"


# ── HA-02 — 온톨로지 코어(허브 · PLM 과제 도구)는 예산 절단에서 빠지지 않는다 ─────────────────────────────────────
def test_HA02_무캡_절단에서도_보이는_코어는_남고_운영자는_넣지_않는다(monkeypatch):
    monkeypatch.setattr(a, "_semantic_order", lambda _q, _t: [])
    monkeypatch.setattr(a, "TOOL_MAX", 0)
    monkeypatch.setattr(a, "_model_context_tokens", lambda: 8000)
    noise = [_fake_tool(f"alpha_{i}", "alpha") for i in range(40)]
    core = [_fake_tool(n, "zzz") for n in ("rootdesigndatahub_object_resolve", "failuretrace_graph_neighbors", "find_plm_task")]
    kept = {t.name for t in a._select_tools(noise + core, "alpha 결과")}
    assert {"rootdesigndatahub_object_resolve", "failuretrace_graph_neighbors", "find_plm_task"} <= kept, kept
    assert len(kept) < len(noise) + len(core), "이 시험의 전제 — 절단이 일어나야 한다"
    op = {t.name for t in a._select_tools(noise + core, "alpha 결과", core_names=a._GUIDE_TOOLS)}
    assert "rootdesigndatahub_object_resolve" not in op, "HE팀 운영자에게까지 허브를 끼웠다"


def test_HA02_캡_경로도_코어를_예약한다(monkeypatch):
    monkeypatch.setattr(a, "_semantic_order", lambda _q, _t: [])
    monkeypatch.setattr(a, "TOOL_MAX", 10)
    monkeypatch.setattr(a, "TOOL_SCHEMA_BUDGET", 0)
    noise = [_fake_tool(f"alpha_{i}", "alpha") for i in range(30)]
    core = [_fake_tool("compare_task_revisions", "zzz")]
    assert "compare_task_revisions" in {t.name for t in a._select_tools(noise + core, "alpha 결과")}
