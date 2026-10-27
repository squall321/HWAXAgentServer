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


# ── HA-03 — 코어 데이터 규칙 일곱 줄 ───────────────────────────────────────────────────────────────────
def test_HA03_시스템_프롬프트에_코어_규칙_일곱이_있고_시험_앱_문구는_없다():
    sp = a.SYSTEM_PROMPT
    assert "[코어 데이터 규칙]" in sp
    for i, must in enumerate(("object_resolve", "과제명", "objects_summary", "graph_neighbors", "다시 resolve",
                              "조회하지 못했다", "ComponentCore"), 1):
        mark = "①②③④⑤⑥⑦"[i - 1]
        line = sp[sp.index(mark):]
        assert must in line[:line.find("⑧") if "⑧" in line else 260], (mark, must)
    assert "(시험)" not in sp, "D-1 — 라벨을 바로잡는다(시험 앱 문구는 넣지 않는다)"
    assert sp.index("[코어 데이터 규칙]") > sp.index("search_tools 로 찾아보세요"), "search_tools 안내 뒤에 둔다"


# ── HA-04 — 허브의 큰 응답을 접는다(group_options · ref_labels · properties_schema 걷기 · related 는 관계별 수 + 앞 몇 개) ─────────
import json as _json  # noqa: E402


def test_HA04_세_꼴을_접고_무엇을_접었는지_한_줄을_남긴다():
    summ = _json.dumps({"total": 412, "groups": [{"k": "A", "n": 3}], "group_options": [{"v": i} for i in range(300)]})
    out = a._fold_hub_result("rootdesigndatahub_objects_summary", summ)
    head, body = out.split("\n", 1)
    assert head.startswith("[포털 챗이 허브 응답을 접었다") and "group_options 1곳" in head and f"{len(summ):,}자" in head
    assert _json.loads(body) == {"total": 412, "groups": [{"k": "A", "n": 3}]}
    lst = _json.dumps({"total": 2, "ref_labels": {"x": "y" * 900}, "properties_schema": {"p": 1},
                       "items": [{"id": 1, "ref_labels": {"a": 1}}, {"id": 2}]})
    body = _json.loads(a._fold_hub_result("failuretrace_objects_list", lst).split("\n", 1)[1])
    assert body == {"total": 2, "items": [{"id": 1}, {"id": 2}]}
    get = _json.dumps({"id": "P-1", "related": {"uses": [{"id": f"m{i}"} for i in range(12)], "owner": [{"id": "o1"}],
                                                "note": "기타"}})
    out = a._fold_hub_result("object_get", get)
    body = _json.loads(out.split("\n", 1)[1])
    assert body["related"]["uses"] == {"count": 12, "head": [{"id": f"m{i}"} for i in range(a._HUB_RELATED_HEAD)]}
    assert body["related"]["owner"] == {"count": 1, "head": [{"id": "o1"}]} and body["related"]["note"] == "기타"
    assert "관계 항목 7건" in out.split("\n", 1)[0]
    rel_list = _json.dumps({"id": "P-2", "related": [{"relation": "uses", "id": f"m{i}"} for i in range(8)] + [{"id": "x"}]})
    body = _json.loads(a._fold_hub_result("rootdesigndatahub_object_get", rel_list).split("\n", 1)[1])
    assert body["related"]["uses"]["count"] == 8 and len(body["related"]["uses"]["head"]) == a._HUB_RELATED_HEAD
    assert body["related"]["관계 미상"]["count"] == 1


def test_HA04_접을_것이_없거나_다른_도구면_글자_하나_바꾸지_않는다():
    small = _json.dumps({"id": "P-1", "related": {"uses": [{"id": "m1"}]}})
    assert a._fold_hub_result("object_get", small) == small, "앞 몇 개 안이면 그대로다"
    for name, text in (("objects_summary", "표 형태의 글 — JSON 이 아니다"), ("objects_summary", '{"total": 3}'),
                       ("list_materials", _json.dumps({"group_options": [1, 2]})), ("objects_list", "{깨진")):
        assert a._fold_hub_result(name, text) == text, (name, text)


def test_HA04_도구_래퍼를_지나면_접힌다():
    async def call_tool(**_kw):
        return _json.dumps({"total": 1, "group_options": ["x" * 5000]})
    t = a._prep_tool(StructuredTool(name="rootdesigndatahub_objects_summary", description="요약", coroutine=call_tool,
                                    args_schema={"type": "object", "properties": {}}))
    out = asyncio.run(t.ainvoke({}))
    assert "group_options" not in out.split("\n", 1)[1] and "x" * 100 not in out


# ── HA-15 — 질문의 언어로 답한다(기본 한국어 · 중국어로 새지 않는다) ─────────────────────────────────────────
def test_HA15_질문의_언어로_답하고_중국어로_새지_않는다():
    head = a.SYSTEM_PROMPT[:200]
    assert "사용자 질문의 언어로 답하세요" in head and "기본은 한국어" in head and "중국어로 절대 전환하지 마세요" in head
    assert "한국어로만" not in a.SYSTEM_PROMPT, "영어 질문에도 한국어로만 답하라는 줄이 남았다"


# ── HA-10 — 잡 폴더 0700 · 원장 · 색인 0640 · 상태 0600(공유 그룹 상위를 따라 넓게 만들어지던 것) ──────────────────────
import os  # noqa: E402
import stat  # noqa: E402

import delib_jobs  # noqa: E402


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_HA10_새로_쓰는_폴더와_파일은_좁다(monkeypatch, tmp_path):
    shared = tmp_path / "shared"; shared.mkdir(); os.chmod(shared, 0o2775)
    jobs = shared / "delib-jobs"; jobs.mkdir(); os.chmod(jobs, 0o2775)
    monkeypatch.setattr(delib_jobs, "JOB_DIR", jobs)
    monkeypatch.setattr(delib_jobs, "_PAUSE", {})
    old = os.umask(0o002)
    try:
        delib_jobs._persist({"id": "j-1", "status": "done", "question": "화두"})
        assert delib_jobs._write_args("j-1", {"question": "화두", "opts": {"human_note": "사람 의견"}})
        delib_jobs.set_limits({"max_running": 2}, by="시험")
        assert delib_jobs.pause_info() is None
        delib_jobs.pause_state().set_on(scope="delib", by="시험", reason="권한", ttl_s=60)
    finally:
        os.umask(old)
    assert _mode(jobs / "j-1.json") == 0o640 and _mode(jobs / "list" / "j-1.json") == 0o640
    for sub in ("args", "list", "state"):
        assert _mode(jobs / sub) == 0o700, sub
    assert _mode(delib_jobs._args_path("j-1")) == 0o600 and _mode(delib_jobs.limits_path()) == 0o600
    assert _mode(delib_jobs.pause_path()) == 0o600
    assert _mode(jobs) == 0o2775, "운영이 정한 잡 폴더 자체는 건드리지 않는다"


def test_HA10_기동_때_이_판_전의_넓은_것을_좁힌다(monkeypatch, tmp_path):
    jobs = tmp_path / "delib-jobs"
    for sub in ("args", "list", "state"):
        (jobs / sub).mkdir(parents=True); os.chmod(jobs / sub, 0o2775)
    for f, m in ((jobs / "j-old.json", 0o664), (jobs / "list" / "j-old.json", 0o664), (jobs / "state" / "limits.json", 0o664),
                 (jobs / "j-tight.json", 0o600)):
        f.write_text("{}"); os.chmod(f, m)
    monkeypatch.setattr(delib_jobs, "JOB_DIR", jobs)
    delib_jobs._tighten_existing()
    assert all(_mode(jobs / s) == 0o700 for s in ("args", "list", "state"))
    assert _mode(jobs / "j-old.json") == 0o640 and _mode(jobs / "list" / "j-old.json") == 0o640
    assert _mode(jobs / "state" / "limits.json") == 0o600
    assert _mode(jobs / "j-tight.json") == 0o600, "이미 좁은 것을 넓히지 않는다"


def test_HA10_읽기는_폴더를_만들지_않는다(monkeypatch, tmp_path):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_PAUSE", {})
    assert delib_jobs.pause_info() is None and list(tmp_path.iterdir()) == [], "멈춤을 읽기만 했는데 폴더가 생겼다"


# ── HA-17 — 좌석 조회 그림 판독만 끄는 손잡이(DELIB_SEAT_VISION_MAX)와 /health ───────────────────────────────────
def test_HA17_좌석_판독만_따로_끄고_health_에_보인다(monkeypatch):
    import vision as v
    monkeypatch.delenv("DELIB_SEAT_VISION_MAX", raising=False)
    assert a._SeatImageReader(a.app).limit == v.TRANSCRIBE_MAX, "없으면 종전 값이다"
    h = a.health()["seat_vision"]
    assert h["knob"] == "VISION_TRANSCRIBE_MAX" and h["max"] == v.TRANSCRIBE_MAX
    monkeypatch.setenv("DELIB_SEAT_VISION_MAX", "0")
    assert a._SeatImageReader(a.app).limit == 0 and v.TRANSCRIBE_MAX > 0, "챗 · 띵킹 판독은 그대로다"
    h = a.health()["seat_vision"]
    assert h == {"max": 0, "on": False, "knob": "DELIB_SEAT_VISION_MAX", "reader": h["reader"]}
    monkeypatch.setenv("DELIB_SEAT_VISION_MAX", "셋")
    assert a._SeatImageReader(a.app).limit == v.TRANSCRIBE_MAX, "모양이 틀린 값은 종전 값이다"
