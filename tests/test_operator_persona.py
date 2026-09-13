# HE팀 MCP 운영자 페르소나 — 고르면 그 앱 도구가 묶이고, 입구 도구는 캡에 안 잘리고, 자동 발굴엔 안 섞인다
#
#   실행:  .venv/bin/python -m pytest tests/test_operator_persona.py -q
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as a  # noqa: E402
import deliberation as d  # noqa: E402
import thinking as th  # noqa: E402

OP_SESSION = {
    "agent_type": "he-calc-thermalshock", "name": "Thermal Shock SED 운영자",
    "description": "열충격 SED 운영자", "system_prompt": "# Thermal Shock SED 운영자\n역할 본문",
    "response_config": {"persona_kind": "mcp_operator", "mcp_apps": ["heax-thermal_shock_mcp"],
                        "key_tools": ["predict_sed", "get_dataset_summary"]},
}
PLAIN_SESSION = {"agent_type": "rel-drop-impact", "name": "낙하 전문가", "description": "낙하 충격",
                 "system_prompt": "낙하 전문가 역할", "response_config": {}}


def _prime_tools_map(monkeypatch, mapping, areas=None):
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "at", time.time())
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "map", mapping)
    # 영역도 같은 응답에 온다 — 안 채우면 _seat_tool_prefer 가 '분류 없음' 으로 보고 빈
    # 목록을 주는데, 실제 게이트웨이는 areas 를 함께 준다.
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "areas",
                        areas if areas is not None else {n: "sim" for n in mapping})
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "area_meta", {"sim": {"area": "sim", "label": "해석"}})


def _stub_session(monkeypatch, sessions, calls=None):
    async def fake_tools(*_a, **_k):
        return {"get_agent_session": object()}

    async def fake_call(tools, name, args):
        if calls is not None:
            calls.append((name, args))
        return json.dumps(sessions[args["agent_type"]], ensure_ascii=False)

    monkeypatch.setattr(a, "_tools_by_name", fake_tools)
    monkeypatch.setattr(a, "_call", fake_call)


# ── _persona_meta ────────────────────────────────────────────────────────────
def test_운영자_설정을_읽고_캐시한다(monkeypatch):
    calls: list = []
    _stub_session(monkeypatch, {"he-calc-thermalshock": OP_SESSION}, calls)
    fake = NS(state=NS())
    m = asyncio.run(a._persona_meta(fake, [], "he-calc-thermalshock"))
    assert m["operator"] is True
    assert m["apps"] == ["heax-thermal_shock_mcp"]
    assert m["key_tools"] == ["predict_sed", "get_dataset_summary"]
    assert m["role"].startswith("# Thermal Shock")
    asyncio.run(a._persona_meta(fake, [], "he-calc-thermalshock"))
    assert len(calls) == 1, "TTL 안에서는 다시 묻지 않는다"


def test_캐시는_TTL_이_지나면_다시_읽는다(monkeypatch):
    calls: list = []
    _stub_session(monkeypatch, {"he-calc-thermalshock": OP_SESSION}, calls)
    monkeypatch.setattr(a, "PERSONA_TTL_S", 0)
    fake = NS(state=NS())
    asyncio.run(a._persona_meta(fake, [], "he-calc-thermalshock"))
    asyncio.run(a._persona_meta(fake, [], "he-calc-thermalshock"))
    assert len(calls) == 2, "예전엔 재시작 전까지 영구 캐시라 동기화한 역할이 안 먹었다"


def test_일반_전문가는_운영자가_아니다(monkeypatch):
    _stub_session(monkeypatch, {"rel-drop-impact": PLAIN_SESSION})
    m = asyncio.run(a._persona_meta(NS(state=NS()), [], "rel-drop-impact"))
    assert m["operator"] is False and m["apps"] == [] and m["key_tools"] == []


# ── _select_tools: 입구 도구는 캡에 안 잘린다 ─────────────────────────────────
def _tool(name, desc="x"):
    return NS(name=name, description=desc, args_schema={})


def test_앱을_통째로_핀해도_콕_집은_도구가_먼저_산다(monkeypatch):
    monkeypatch.setattr(a, "TOOL_MAX", 5)
    monkeypatch.setattr(a, "TOOL_SCHEMA_BUDGET", 0)
    monkeypatch.setattr(a, "_TOOL_PRIORITY", ())
    monkeypatch.setattr(a, "_semantic_order", lambda q, tools: [])
    app_tools = [_tool(f"sf_{i:02d}", "stepforge 파트 간극 측정") for i in range(12)]
    entry = _tool("list_projects", "과제 목록")
    tools = app_tools + [entry, _tool("other", "무관")]
    pinned = [t.name for t in app_tools] + ["list_projects"]
    kept = [t.name for t in a._select_tools(tools, "파트 간극", pinned, ["list_projects"])]
    assert "list_projects" in kept, "입구 도구가 관련도에 밀려 캡 밖으로 나가면 운영자가 첫 걸음을 못 뗀다"
    assert kept[0] == "list_projects"
    assert "other" not in kept


def test_운영자는_안내대만_상시_예약한다(monkeypatch):
    monkeypatch.setattr(a, "TOOL_MAX", 5)
    monkeypatch.setattr(a, "TOOL_SCHEMA_BUDGET", 0)
    monkeypatch.setattr(a, "_TOOL_PRIORITY", ("query_voc", "invoke_tool"))
    monkeypatch.setattr(a, "_semantic_order", lambda q, tools: [])
    tools = [_tool(f"sf_{i:02d}", "stepforge") for i in range(8)] + [_tool("query_voc"), _tool("invoke_tool")]
    pinned = [f"sf_{i:02d}" for i in range(8)]
    lean = [t.name for t in a._select_tools(tools, "stepforge", pinned, [], ("invoke_tool",))]
    assert "invoke_tool" in lean and "query_voc" not in lean, "앱과 무관한 핵심 도구까지 덧붙이면 작은 컨텍스트가 넘친다"
    full = [t.name for t in a._select_tools(tools, "stepforge", pinned, [])]
    assert "query_voc" in full and "invoke_tool" in full, "일반 경로의 핵심 예약은 그대로다"


# ── _select_tools: 전문가 분야 도구는 핀보다 뒤, 질문 어휘보다 앞 ─────────────
def test_전문가_분야_도구가_질문_어휘보다_먼저_산다(monkeypatch):
    monkeypatch.setattr(a, "TOOL_MAX", 3)
    monkeypatch.setattr(a, "TOOL_SCHEMA_BUDGET", 0)
    monkeypatch.setattr(a, "_TOOL_PRIORITY", ())
    monkeypatch.setattr(a, "_semantic_order", lambda q, tools: [])
    tools = [_tool("list_materials", "물성 목록"), _tool("get_material_properties", "물성 값"),
             _tool("noise_a", "구리 구리 구리"), _tool("noise_b", "구리 구리 구리")]
    # 질문 어휘("구리")만 보면 noise 둘이 이긴다. 전문가를 앉혔으면 그 사람 도구가 먼저다.
    kept = [t.name for t in a._select_tools(tools, "구리", prefer=["list_materials",
                                                                  "get_material_properties"])]
    assert kept[:2] == ["list_materials", "get_material_properties"], \
        "전문가를 골라도 질문 어휘로만 고르면, 그 사람이 늘 쓰는 도구가 캡 밖으로 밀린다"


def test_사용자가_콕_집은_도구가_전문가_분야보다_먼저다(monkeypatch):
    monkeypatch.setattr(a, "TOOL_MAX", 2)
    monkeypatch.setattr(a, "TOOL_SCHEMA_BUDGET", 0)
    monkeypatch.setattr(a, "_TOOL_PRIORITY", ())
    monkeypatch.setattr(a, "_semantic_order", lambda q, tools: [])
    tools = [_tool("list_materials", "물성"), _tool("submit_job", "잡 제출"), _tool("x", "x")]
    kept = [t.name for t in a._select_tools(tools, "물성", pinned=["submit_job"],
                                            prefer=["list_materials"])]
    assert kept[0] == "submit_job", "일부러 고른 것은 언제나 전문가 짐작보다 먼저다"
    assert "list_materials" in kept


def test_분야_도구_없이는_종전_순서_그대로다(monkeypatch):
    monkeypatch.setattr(a, "TOOL_MAX", 2)
    monkeypatch.setattr(a, "TOOL_SCHEMA_BUDGET", 0)
    monkeypatch.setattr(a, "_TOOL_PRIORITY", ())
    monkeypatch.setattr(a, "_semantic_order", lambda q, tools: [])
    tools = [_tool("hit", "구리 물성"), _tool("miss", "무관")]
    assert [t.name for t in a._select_tools(tools, "구리 물성")][0] == "hit"


# ── 챗 스트림: 운영자를 고르면 그 앱이 묶인다 ─────────────────────────────────
class _FakeAgent:
    def __init__(self, sink):
        self.sink = sink

    async def astream_events(self, inputs, version=None, config=None):
        self.sink["messages"] = inputs["messages"]
        yield {"event": "on_chat_model_stream", "data": {"chunk": NS(content="답변")}}


def _run_stream(monkeypatch, session, mapping):
    sink: dict = {}
    _stub_session(monkeypatch, {session["agent_type"]: session})
    _prime_tools_map(monkeypatch, mapping)

    async def fake_agent_for(app, groups, pinned=None, query="", sources=None, user="", user_pat="",
                             first=None, core_names=None, prefer=None):
        sink["pinned"], sink["first"], sink["core"] = list(pinned or []), list(first or []), core_names
        sink["prefer"] = list(prefer or [])
        return _FakeAgent(sink)

    knowledge_calls: list = []

    async def fake_knowledge(*args, **kw):
        knowledge_calls.append(args)
        return ""

    monkeypatch.setattr(a, "_agent_for", fake_agent_for)
    monkeypatch.setattr(a, "_persona_knowledge", fake_knowledge)
    a._persona_knowledge.last_note = ""
    fake_app = NS(state=NS(tool_load_error={}, tool_degraded={}, llm_nostream=False))
    req = a.ChatRequest(message="SED 예측해줘", groups=[], pinned_agent=session["agent_type"])

    async def consume():
        return [chunk async for chunk in a._agent_stream(fake_app, req)]

    out = b"".join(asyncio.run(consume())).decode("utf-8")
    return sink, out, knowledge_calls


TS_MAP = {"predict_sed": "heax-thermal_shock_mcp", "get_dataset_summary": "heax-thermal_shock_mcp",
          "train_model": "heax-thermal_shock_mcp", "query_voc": "signalforge"}


def test_운영자를_고르면_앱_도구가_묶이고_지식카드_선조회는_안_한다(monkeypatch):
    sink, out, kcalls = _run_stream(monkeypatch, OP_SESSION, TS_MAP)
    assert set(sink["pinned"]) >= {"predict_sed", "get_dataset_summary", "train_model"}
    assert "query_voc" not in sink["pinned"], "다른 앱 도구까지 핀하지 않는다"
    assert sink["first"][:2] == ["predict_sed", "get_dataset_summary"], "입구 도구는 먼저 산다"
    sys_prompt = sink["messages"][0][1]
    assert "[HE팀 MCP 운영자" in sys_prompt and "역할 본문" in sys_prompt
    assert "[운영 앱 — 선택한 HE팀 운영자]" in sys_prompt
    assert "agent_search" not in sys_prompt.split("[HE팀 MCP 운영자")[1], "운영자에게 지식카드 우선을 지시하지 않는다"
    assert kcalls == [], "지식카드 0건을 '사내 지식 없음'으로 먼저 밝히게 하면 안 된다"
    assert sink["core"] == a._GUIDE_TOOLS, "운영자는 안내대만 상시 예약한다"
    assert "지정 앱 1개" in out


def test_운영자의_앱이_게이트웨이에_없으면_경고한다(monkeypatch):
    sess = dict(OP_SESSION, response_config={"persona_kind": "mcp_operator", "mcp_apps": ["arp"],
                                             "key_tools": []})
    sink, out, _ = _run_stream(monkeypatch, sess, TS_MAP)
    assert "operator_app_missing" in out
    assert "연결돼 있지 않다" in sink["messages"][0][1]


def test_일반_전문가는_종전대로_지식카드를_조회한다(monkeypatch):
    sink, _out, kcalls = _run_stream(monkeypatch, PLAIN_SESSION, TS_MAP)
    assert len(kcalls) == 1
    assert "agent_search" in sink["messages"][0][1]
    assert sink["pinned"] == [] and sink["first"] == []
    assert sink["core"] is None, "일반 전문가는 종전 핵심 예약 그대로"
    # 낙하 전문가 + 해석(sim) 영역 도구 → 분야 도구가 붙는다.
    assert sink["prefer"], "도메인 전문가를 골랐는데 분야 도구가 하나도 안 붙었다"
    assert len(sink["prefer"]) <= a._PERSONA_PREFER_N


def test_전문가_역할이_도구와_걸리면_분야_도구가_붙는다(monkeypatch):
    """전문가를 앉혔는데 도구가 질문 어휘로만 정해지면 그 사람을 고른 효과가 말투까지만 간다."""
    sess = {"agent_type": "he-calc-thermalshock", "name": "열충격 전문가",
            "description": "열충격", "system_prompt": "열충격 SED 담당", "response_config": {}}
    sink, _out, _ = _run_stream(monkeypatch, sess, TS_MAP)
    assert sink["prefer"], "역할이 도구 영역과 걸리는데도 분야 도구가 안 붙었다"
    assert len(sink["prefer"]) <= a._PERSONA_PREFER_N


# ── 자동 발굴에서 운영자 제외 ────────────────────────────────────────────────
REC = {"agents": [{"agent_type": "he-calc-laminate", "name": "Laminate 운영자"},
                  {"agent_type": "pcb-warpage", "name": "PCB 휨"},
                  {"agent_type": "rel-drop-impact", "name": "낙하"}]}


def test_is_operator():
    assert d.is_operator("he-cad-stepforge")
    assert not d.is_operator("rel-drop-impact")
    assert not d.is_operator("hexa-mesh")      # 접두사가 'he' 로 시작해도 도메인이 아니면 아니다


def test_심의_발굴은_운영자를_앉히지_않는다(monkeypatch):
    async def fake_call(tools, name, args):
        return json.dumps(REC)

    async def fake_role(tools, key, fallback=""):
        return ""

    monkeypatch.setattr(d, "_call", fake_call)
    monkeypatch.setattr(d, "_restore_role", fake_role)
    seats = asyncio.run(d._discover({}, "PCB 휨", limit=5))
    assert [s["key"] for s in seats] == ["pcb-warpage", "rel-drop-impact"]


def test_띵킹_소집도_운영자를_부르지_않는다(monkeypatch):
    async def fake_call(tools, name, args):
        return json.dumps(REC)

    monkeypatch.setattr(th, "_call", fake_call)
    seats = asyncio.run(th._summon({}, "PCB 휨", top_k=5, exclude=set()))
    assert [s["key"] for s in seats] == ["pcb-warpage", "rel-drop-impact"]


def test_좌석_추천_화면은_운영자를_추천하지_않지만_풀에는_남긴다(monkeypatch):
    async def fake_tools(*_a, **_k):
        return {"recommend_agents": NS(name="recommend_agents", description=""),
                "list_agents": NS(name="list_agents", description="")}

    async def fake_call(tools, name, args):
        if name == "recommend_agents":
            return json.dumps(REC)
        return "".join(json.dumps({"agent_type": k, "name": k}) for k in
                       ("he-calc-laminate", "pcb-warpage", "rel-drop-impact"))

    async def no_axes(*_a, **_k):
        return []

    monkeypatch.setattr(a, "_tools_by_name", fake_tools)
    monkeypatch.setattr(a, "_call", fake_call)
    monkeypatch.setattr(a, "_seat_axes", no_axes)
    monkeypatch.setattr(a.app.state, "llm", None, raising=False)   # 축 추출은 위에서 막았다 — 인자 평가용
    out = asyncio.run(a.deliberate_experts(a.ExpertsRequest(message="PCB 휨", groups=[])))
    assert "he-calc-laminate" not in [r["key"] for r in out["recommended"]]
    assert "he-calc-laminate" not in [r["key"] for r in out["candidates"]]
    assert "he-calc-laminate" in [p["key"] for p in out["pool"]], "조직도에서 사람이 고를 수는 있어야 한다"


def test_전문가_상세는_scope_아래_태그를_읽고_운영_앱을_준다(monkeypatch):
    sess = dict(OP_SESSION, scope={"common_tags": ["HE팀", "MCP 운영자"]})

    async def fake_tools(*_a, **_k):
        return {"get_agent_session": object(), "list_records": object()}

    async def fake_call(tools, name, args):
        return json.dumps(sess, ensure_ascii=False) if name == "get_agent_session" else json.dumps({"records": []})

    monkeypatch.setattr(a, "_tools_by_name", fake_tools)
    monkeypatch.setattr(a, "_call", fake_call)
    _prime_tools_map(monkeypatch, TS_MAP)
    out = asyncio.run(a.catalog_agent(a.AgentDetailRequest(key="he-calc-thermalshock")))
    assert out["tags"] == ["HE팀", "MCP 운영자"], "get_agent_session 은 태그를 scope 아래에 둔다"
    assert out["operator"] is True
    assert out["apps"] == [{"key": "heax-thermal_shock_mcp", "label": a._app_label("heax-thermal_shock_mcp"),
                            "tool_count": 3, "connected": True}]



# ── 전문가 분야 도구는 '짐작이 될 때만' 준다 ──────────────────────────────────
def test_역할이_비면_분야_도구를_주지_않는다(monkeypatch):
    """짐작이 안 되는데 순위를 바꾸면 질문 어휘로 고른 진짜 관련 도구를 밀어낸다.
    실측(2026-09-13): 역할 없는 키를 주니 get_guide·describe_* 입구 도구만 12종 올라왔다."""
    names = ["get_guide", "describe_template", "list_materials", "search_reports"]
    assert d._seat_tool_prefer(names, {}, "구리", 12) == []


def test_물성_전문가는_물성_도구를_받는다(monkeypatch):
    monkeypatch.setattr(a, "_area_of", lambda n: ("material", "물성·재료")
                        if "material" in n else ("report", "보고서"))
    monkeypatch.setattr(d, "_AREA_HINT", {"material": "물성 재료 구리", "report": "보고서"})
    names = ["get_guide", "describe_template", "list_materials", "get_material_properties"]
    got = d._seat_tool_prefer(names, {"key": "mat-cu", "role": "구리 물성 담당"}, "구리", 12)
    assert got and all("material" in g for g in got), f"물성 전문가가 받은 것: {got}"


# ── 역할 주입 실패를 사용자에게 말한다(during-F1) ─────────────────────────────
def _stream_with_persona(monkeypatch, meta):
    sink: dict = {}
    _prime_tools_map(monkeypatch, TS_MAP)

    async def fake_meta(*_a, **_k):
        return meta

    async def fake_agent_for(app, groups, pinned=None, query="", sources=None, user="", user_pat="",
                             first=None, core_names=None, prefer=None):
        return _FakeAgent(sink)

    async def fake_knowledge(*_a, **_k):
        return ""

    monkeypatch.setattr(a, "_persona_meta", fake_meta)
    monkeypatch.setattr(a, "_agent_for", fake_agent_for)
    monkeypatch.setattr(a, "_persona_knowledge", fake_knowledge)
    a._persona_knowledge.last_note = ""
    fake_app = NS(state=NS(tool_load_error={}, tool_degraded={}, llm_nostream=False))
    req = a.ChatRequest(message="휨 봐줘", groups=[], pinned_agent="sim-pcb-warpage")

    async def consume():
        return [c async for c in a._agent_stream(fake_app, req)]

    return sink, b"".join(asyncio.run(consume())).decode("utf-8")


def test_역할을_못_불러오면_사용자에게_말한다(monkeypatch):
    """전문가를 골라 놓고 일반 답을 받는데 화면에 아무 표시가 없으면 사용자는 모른다."""
    sink, out = _stream_with_persona(
        monkeypatch, {"role": "", "note": "RuntimeError: gateway down",
                      "operator": False, "apps": [], "key_tools": []})
    assert "persona_load_failed" in out
    assert "gateway down" in out
    assert "불러오지 못했다" in sink["messages"][0][1], "모델에게도 알려야 전문가인 척하지 않는다"


def test_역할이_비어_있으면_다르게_말한다(monkeypatch):
    """'못 물어봤다' 와 '원래 없다' 는 다른 사실이다 — 지식카드 경로와 같은 원칙."""
    _sink, out = _stream_with_persona(
        monkeypatch, {"role": "", "note": "", "operator": False, "apps": [], "key_tools": []})
    assert "persona_role_empty" in out and "persona_load_failed" not in out


def test_역할이_있으면_경고하지_않는다(monkeypatch):
    sink, out = _stream_with_persona(
        monkeypatch, {"role": "휨 해석 전문가다", "note": "", "operator": False,
                      "apps": [], "key_tools": []})
    assert "persona_load_failed" not in out and "persona_role_empty" not in out
    assert "휨 해석 전문가다" in sink["messages"][0][1]


# ── 캡이 없어도 전문가 도구가 앞에 선다(운영 cae00 은 TOOL_MAX=0) ──────────────
def test_캡이_없어도_전문가_도구가_앞에_선다(monkeypatch):
    """update-all.sh 가 원격 GLM 을 보면 TOOL_MAX=0 을 박는다. 조기 반환하면 이 기능이
    운영에서 100% 무효인데 상태줄은 '우선 바인딩' 이라고 말한다."""
    monkeypatch.setattr(a, "TOOL_MAX", 0)
    tools = [_tool("zzz"), _tool("list_materials"), _tool("aaa")]
    kept = [t.name for t in a._select_tools(tools, "물성", prefer=["list_materials"])]
    assert kept[0] == "list_materials"
    assert len(kept) == 3, "캡이 없으면 아무것도 떨어뜨리지 않는다"


def test_캡이_안_걸려도_마찬가지다(monkeypatch):
    monkeypatch.setattr(a, "TOOL_MAX", 200)
    tools = [_tool("zzz"), _tool("list_materials")]
    assert [t.name for t in a._select_tools(tools, "물성",
                                            prefer=["list_materials"])][0] == "list_materials"


def test_분야_도구가_없으면_순서를_안_건드린다(monkeypatch):
    monkeypatch.setattr(a, "TOOL_MAX", 0)
    tools = [_tool("zzz"), _tool("aaa")]
    assert [t.name for t in a._select_tools(tools, "물성")] == ["zzz", "aaa"]


def test_캡이_없어도_prefer_가_캐시_키에_들어간다(monkeypatch):
    """빠지면 전문가를 바꿔도 앞 전문가 순서로 만든 에이전트가 재사용된다(조용히)."""
    import inspect
    src = inspect.getsource(a._agent_for)
    assert "pin_key = (pin_key, tuple(prefer or [])) if prefer else pin_key" in src


# ── 챗과 심의가 정말 같은 순위를 낸다 ────────────────────────────────────────
def test_챗_페르소나에도_키와_이름이_있다(monkeypatch):
    """_seat_tool_rank 는 역할뿐 아니라 키·이름의 낱말도 쓴다. 챗이 안 넘기면 '같은 기준' 은
    절반만 참이고, 그 페르소나에 가장 맞는 도구가 조용히 빠진다."""
    _stub_session(monkeypatch, {"rel-drop-impact": PLAIN_SESSION})
    m = asyncio.run(a._persona_meta(NS(state=NS()), [], "rel-drop-impact"))
    assert m["key"] == "rel-drop-impact"
    assert m["name"] == "낙하 전문가"
    names = ["drop_impact_calc", "list_materials", "zzz"]
    assert d._seat_tool_rank(names, m, "낙하") == d._seat_tool_rank(
        names, {"key": m["key"], "name": m["name"], "role": m["role"]}, "낙하")


# ── 영역 분류를 못 받으면 짐작을 그만둔다 ────────────────────────────────────
def test_영역_분류가_없으면_분야_도구를_안_준다(monkeypatch):
    """_tools_map 은 실패를 print 한 줄로 삼키고 구 게이트웨이면 areas 를 빈 dict 로
    덮어쓴다. 그러면 점수가 이름 매칭만 남아 쓰레기가 되는데 상태줄은 계속 뜬다."""
    _prime_tools_map(monkeypatch, TS_MAP, areas={})
    assert d._seat_tool_prefer(list(TS_MAP), {"key": "rel-drop-impact",
                                              "role": "낙하 충격 전문가"}, "낙하", 12) == []


def test_세글자_역할어가_엉뚱한_도구에_안_걸린다(monkeypatch):
    """'str-laminate' 의 str 이 instruments·distribution·stress 에 6점씩 붙던 자리다."""
    assert not d._name_hit("str", "list_instruments")
    assert not d._name_hit("str", "chart_country_distribution")
    assert d._name_hit("pcb", "pcb_warpage_surrogate"), "낱말로 맞으면 3자라도 붙는다"
    assert d._name_hit("material", "list_materials"), "4자 이상은 접두 일치를 살린다"


# ── 상태줄은 실제로 묶인 것만 말한다 ────────────────────────────────────────
def test_안_묶인_도구를_우선_바인딩이라_말하지_않는다(monkeypatch):
    """prefer 는 그룹 필터 **전** 목록(/tools-map 465종)에서 뽑는다. 실제 바인딩은 그 사용자의
    그룹 헤더 뒤라 8종일 수도 있다 — 그대로 칩으로 그리면 못 쓰는 도구를 '우선 바인딩' 으로
    보여 준다. 조기 반환·예산 절단으로 안 붙는 경우도 같다."""
    sink: dict = {}
    _prime_tools_map(monkeypatch, TS_MAP)
    sess = {"agent_type": "he-calc-thermalshock", "name": "열충격 전문가",
            "description": "열충격", "system_prompt": "열충격 SED 담당", "response_config": {}}
    _stub_session(monkeypatch, {sess["agent_type"]: sess})

    async def fake_agent_for(app, groups, pinned=None, query="", sources=None, user="", user_pat="",
                             first=None, core_names=None, prefer=None):
        ag = _FakeAgent(sink)
        ag._hwax_tools = ("predict_sed",)      # 하나만 실제로 묶였다
        return ag

    async def fake_knowledge(*_a, **_k):
        return ""

    monkeypatch.setattr(a, "_agent_for", fake_agent_for)
    monkeypatch.setattr(a, "_persona_knowledge", fake_knowledge)
    a._persona_knowledge.last_note = ""
    fake_app = NS(state=NS(tool_load_error={}, tool_degraded={}, llm_nostream=False))
    req = a.ChatRequest(message="SED 예측", groups=[], pinned_agent=sess["agent_type"])

    async def consume():
        return [c async for c in a._agent_stream(fake_app, req)]

    out = b"".join(asyncio.run(consume())).decode("utf-8")
    assert "1종 우선 바인딩" in out, out[:400]
    assert "get_dataset_summary" not in out, "안 묶인 도구를 칩으로 그렸다"


def test_하나도_안_묶이면_그렇다고_말한다(monkeypatch):
    sink: dict = {}
    _prime_tools_map(monkeypatch, TS_MAP)
    sess = {"agent_type": "he-calc-thermalshock", "name": "열충격 전문가",
            "description": "열충격", "system_prompt": "열충격 SED 담당", "response_config": {}}
    _stub_session(monkeypatch, {sess["agent_type"]: sess})

    async def fake_agent_for(*_a, **_k):
        ag = _FakeAgent(sink)
        ag._hwax_tools = ("무관도구",)
        return ag

    async def fake_knowledge(*_a, **_k):
        return ""

    monkeypatch.setattr(a, "_agent_for", fake_agent_for)
    monkeypatch.setattr(a, "_persona_knowledge", fake_knowledge)
    a._persona_knowledge.last_note = ""
    fake_app = NS(state=NS(tool_load_error={}, tool_degraded={}, llm_nostream=False))
    req = a.ChatRequest(message="SED 예측", groups=[], pinned_agent=sess["agent_type"])

    async def consume():
        return [c async for c in a._agent_stream(fake_app, req)]

    out = b"".join(asyncio.run(consume())).decode("utf-8")
    assert "안 붙었습니다" in out, "조용히 넘기면 '전문가 도구로 답했다' 로 읽힌다"
