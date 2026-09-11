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


def _prime_tools_map(monkeypatch, mapping):
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "at", time.time())
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "map", mapping)


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
                             first=None, core_names=None):
        sink["pinned"], sink["first"], sink["core"] = list(pinned or []), list(first or []), core_names
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
