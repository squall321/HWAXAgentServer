# REST 다리 쓰기의 확인 관문과 StepForge 운영자 챗의 rest_call 묶음(HWAXPortal docs/core-link §7 — SRC-15 챗 쪽)
#
# 게이트웨이가 StepForge 의 정해진 쓰기 경로(POST /api/onto-link · onto-put · onto-define)에 그 사람의 단언을 실어 보내게 된다(design §5).
# 챗에서 그 길은 rest_call 의 POST 인데, rest_call 은 운영자 정의의 writes · confirm 어디에도 없어 카드 없이 나갔다(PLAN §4 SRC-15).
#   A. 부류 — 메서드가 GET · HEAD 가 아니면(대소문자 · 앞뒤 공백 무시, 빠지면 게이트웨이처럼 GET) 운영자 목록 · 주문 말과 상관없이 늘 confirm 이다.
#      GET · HEAD 는 종전 판정 그대로이고, 운영자 목록 · 잡 바닥 목록의 판정도 그대로다.
#   B. 턴 — 직접 · invoke_tool · 별칭 모두 카드이고, 카드는 승인 때 보낼 인자 그대로의 site · method · path · query · body 를 칸마다 JSON 으로
#      싣는다(본문은 앞 400자). 승인하면 처음 꼴 그대로 한 번 간다.
#   C. 묶음 — StepForge 운영자를 고르면 그 앱 도구가 캡(dev 40)을 다 채워 rest_call 이 묶이지 않았다. 입구 도구로 묶는다(챗만 — 좌석은 그대로).
#   D. 엔진 — 심의(지정 도구 · 좌석 자유 조회 · 외부 응답자의 CALL 줄)에는 사람의 확인 카드가 없다. 래퍼(G2b)가 챗 턴 밖의 쓰기 메서드를 막고,
#      지정 도구 자리는 보내기 전에 거절해 상태줄 · 경고 사건 · 끝 카드로 알린다(일반 심의 · 리스크 심사 · MCP 로 띄운 잡이 같은 자리를 지난다).
#
#   실행:  .venv/bin/python -m pytest tests/test_core_link_rest_gate.py -q
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
from test_confirm_gate import HE_TEAM, _card_turn, _GateAgent, _gw, _held, _result, _run, _turn  # noqa: E402
from test_confirm_gate import gate, maps, turn_ctx  # noqa: E402, F401 — 픽스처
from test_delib_silent_drops import _cards, _pin_context, _steps, _stream, _Tool  # noqa: E402, F401 — _pin_context 는 픽스처
from test_operator_persona import OP_SESSION, PLAIN_SESSION, _FakeAgent, _prime_tools_map, _run_stream, _stub_session, _tool  # noqa: E402

# 미리 보기(MCP onto_link dry_run=true)와 같은 인자를 본문에 싣고 적용한다 — 포털 he-team.json he-cad-stepforge 에 더할 줄(design §7)의 꼴
ONTO_BODY = {"project_id": "P1", "links": [{"from": "cad_part:BAT-1", "rel": "instance_of", "to": "part:battery"}], "dry_run": False}
ONTO = {"site": "step-forge", "method": "POST", "path": "/api/onto-link", "body": ONTO_BODY}
ONTO_TEXT = ('site="step-forge" · method="POST" · path="/api/onto-link" · '
             'body={"dry_run":false,"links":[{"from":"cad_part:BAT-1","rel":"instance_of","to":"part:battery"}],"project_id":"P1"}')
ORDER = "BAT-1 을 배터리 부품으로 잇는 선을 적용해줘"        # 쓰기(write)라면 주문으로 통과할 말 — confirm 은 늘 카드다


# ── A. 부류 ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post", " Patch ", "delete\n"])
@pytest.mark.parametrize("name", ["rest_call", "REST_CALL", "gateway_rest_call"])
def test_쓰기_메서드의_REST_다리는_늘_confirm_이다(gate, name, method):
    args = {**ONTO, "method": method}
    assert a._confirm_class(name, args) == ("confirm", "rest_call")
    assert a._confirm_class(name, json.dumps(args)) == ("confirm", "rest_call"), "invoke_tool 의 arguments 는 JSON 글로도 온다"


@pytest.mark.parametrize("method", ["GET", "get", " HEAD ", "head", "", "   ", None, "missing"])
def test_GET_HEAD_와_빠진_메서드는_종전_판정_그대로다(gate, method):
    """게이트웨이 _rest_call 은 method 가 없거나 빈 글이면 GET 으로 보낸다 — 같은 읽기여야 읽기에 카드가 서지 않는다."""
    args = {k: v for k, v in ONTO.items() if k != "method"} if method == "missing" else {**ONTO, "method": method}
    assert a._rest_method(args) in ("GET", "HEAD")
    assert a._confirm_class("rest_call", args) == (None, ""), "rest_call 은 운영자 목록 어디에도 없다 — 종전처럼 목록 밖"
    assert a._confirm_class("rest_call") == (None, ""), "인자 없이 물으면 GET 이다(게이트웨이와 같다)"
    assert a._confirm_class("rest_call", "{깨진 글") == (None, ""), "못 읽는 인자는 칸이 없는 셈 — 게이트웨이도 객체가 아니면 받지 않는다"


def test_운영자_목록과_잡_바닥_목록의_판정은_그대로이고_REST_쓰기만_목록보다_엄하다(gate):
    assert a._confirm_class("delete_project") == ("confirm", "delete_project")
    assert a._confirm_class("heaxstep_forge_run_job") == ("job", "run_job")
    assert a._confirm_class("create_project") == ("write", "create_project")
    assert a._confirm_class("wiki_propose") == ("write", "wiki_propose")
    assert a._confirm_class("list_projects") == (None, "") and a._confirm_class("rest_catalog", {"method": "POST"}) == (None, "")
    assert a._confirm_class("not_rest_call_x", {"method": "POST"}) == (None, ""), "꼬리가 rest_call 인 이름만 REST 다리다"
    # rest_call 을 운영자 writes 에 적어도 — GET 은 목록대로 write, 쓰기 메서드는 목록보다 엄한 confirm
    he = {"personas": [*HE_TEAM["personas"], {"key": "he-x-rest", "apps": ["x"], "writes": ["rest_call"], "confirm": []}]}
    gate.write_text(json.dumps(he), encoding="utf-8")
    os.utime(gate, ns=(time.time_ns(), time.time_ns() + 1_000_000_000))
    assert a._confirm_class("rest_call", {"method": "GET"}) == ("write", "rest_call")
    assert a._confirm_class("rest_call", {"method": "POST"}) == ("confirm", "rest_call")


# ── B. 턴 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("form", ["direct", "alias", "invoke_dict", "invoke_json"])
def test_주문_말이어도_콕_집어도_쓰기_메서드의_REST_다리는_보내지_않고_confirm_으로_든다(turn_ctx, form):
    reached = []
    for pins in ((), ("rest_call",)):
        ctx = turn_ctx(ORDER, pinned_tools=list(pins))
        if form in ("direct", "alias"):
            out = _run(_gw("rest_call" if form == "direct" else "gateway_rest_call", reached), ONTO)
        else:
            out = _run(_gw("invoke_tool", reached), {"name": "rest_call", "arguments": json.dumps(ONTO) if form == "invoke_json" else ONTO})
        assert _held(out) and "되돌리기 어려운 작업이라" in out, (form, pins, out)
        assert [(c["kind"], c["base"], c["via"]) for c in ctx.pending] == [("confirm", "rest_call", form.startswith("invoke"))]
    assert reached == []


def test_GET_은_어떤_말에서도_그대로_간다(turn_ctx):
    turn_ctx(ORDER)
    reached = []
    get = {"site": "step-forge", "path": "/api/onto-candidates", "query": {"project_id": "P1"}}
    assert _run(_gw("rest_call", reached), get) == "gateway called rest_call"
    assert _run(_gw("rest_call", reached), {**get, "method": "head"}) == "gateway called rest_call"
    assert len(reached) == 2


def test_반출_동의_칸은_카드보다_먼저_거절한다(turn_ctx):
    """G2(외부 응답자 · 반출 동의) 판정은 그대로 G5 앞이다 — 사람이 승인해도 이 칸은 싣지 못한다."""
    ctx = turn_ctx(ORDER)
    reached = []
    out = _run(_gw("rest_call", reached), {**ONTO, "body": {**ONTO_BODY, "consent_external": True}})
    assert out.startswith(a._TOOL_FAIL_MARK) and "consent_external" in out and a._CONFIRM_MARK not in out
    assert ctx.pending == [] and reached == []


def test_카드는_site_method_path_와_본문을_싣고_승인하면_처음_꼴_그대로_한_번_간다(gate, monkeypatch):
    reached = []
    tools = {"rest_call": _gw("rest_call", reached, reply=lambda n, x: json.dumps({"applied": 1}))}
    evs, card, hist = _card_turn(monkeypatch, tools, [("rest_call", {**ONTO, "method": "post"})], message=ORDER)
    c = card["calls"][0]
    assert (c["tool"], c["kind"], c["again"]) == ("rest_call", "confirm", False) and "app" not in c, c
    assert c["args_text"] == ONTO_TEXT, c["args_text"]
    assert json.loads(c["args"]) == {**ONTO, "method": "post"}, "화면이 펼칠 인자 전부 — 승인 때 보낼 인자 그대로다"
    res = _result(evs)
    assert "**확인이 필요합니다** — 되돌리기 어려운 작업이라 보내기 전에 확인합니다." in res
    assert f"1. `rest_call` · {ONTO_TEXT}" in res, res
    assert reached == []
    ag = _GateAgent(tools, steps=[], say="선 1개를 적용했습니다.")
    _turn(monkeypatch, ag, tools, card["approve_text"], hist)
    assert reached == [("rest_call", {**ONTO, "method": "post"})], "승인하면 서버가 처음 꼴 그대로 한 번 보낸다(대문자로 고치지 않는다)"


def test_카드는_승인이_보낼_인자_그대로라_빈_칸도_보이고_보낸_것과_같다(gate, monkeypatch):
    """정규 인자는 빈 칸을 뺀다 — 카드가 그것으로 서면 승인이 그 칸째 보내는 query {"force": ""} · 본문 "note": "" 가 카드에 보이지 않았다
    (리뷰 11-09). 카드 글 · 카드 사건 둘 다 승인 때 보낼 인자 그대로다."""
    reached = []
    tools = {"rest_call": _gw("rest_call", reached)}
    sent = {"site": "step-forge", "method": "DELETE", "path": "/api/onto-link", "query": {"force": ""}, "body": {"note": "", "ids": []}}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("rest_call", sent)], message=ORDER)
    c = card["calls"][0]
    assert c["args_text"] == ('site="step-forge" · method="DELETE" · path="/api/onto-link" · query={"force":""} · '
                              'body={"ids":[],"note":""}'), c["args_text"]
    assert json.loads(c["args"]) == sent, "카드 사건의 인자도 빈 칸째"
    _turn(monkeypatch, _GateAgent(tools, steps=[], say="지웠습니다."), tools, card["approve_text"], hist)
    assert reached == [("rest_call", sent)], "카드에 보인 것과 승인이 보낸 것이 같다"


def test_값_안의_구분자와_따옴표는_칸을_꾸며_내지_못한다():
    """값은 칸마다 JSON 이다 — 경로에 ' · method=GET · path=/y' 를 넣어도 따옴표 안에 남고, 따옴표를 넣으면 이스케이프된다(리뷰 11-09)."""
    fake = a._confirm_rest_text({"site": "s", "method": "POST", "path": "/x · method=GET · path=/y"})
    assert fake == 'site="s" · method="POST" · path="/x · method=GET · path=/y"', fake
    quoted = a._confirm_rest_text({"site": 's" · method="GET', "method": "POST", "path": '/x" · path="/y'})
    assert quoted.count('method="') == 1 and quoted.count('path="') == 1, quoted
    assert quoted.startswith('site="s\\" · method=\\"GET" · method="POST" · path="/x\\" · path=\\"/y"'), quoted
    nl = a._confirm_rest_text({"site": "s", "method": "POST", "path": "/p\n/q"})
    assert "\n" not in nl and 'path="/p\\n/q"' in nl, "줄바꿈은 JSON 이스케이프 — 카드 글에 들지 않는다"


def test_칸마다_앞_400자이고_query_와_글_본문도_JSON_으로_보인다():
    big = {"links": [{"from": f"cad_part:P{i:04d}", "to": "part:battery"} for i in range(40)]}
    txt = a._confirm_rest_text({"site": "step-forge", "method": "post", "path": "/api/onto-put", "body": big})
    body = json.dumps(big, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert txt == f'site="step-forge" · method="POST" · path="/api/onto-put" · body={body[:400]}…({len(body):,}자)', txt
    q = a._confirm_rest_text({"site": "s", "method": "DELETE", "path": "/api/x", "query": {"id": 7}})
    assert q == 'site="s" · method="DELETE" · path="/api/x" · query={"id":7}', "본문 없는 DELETE 도 무엇을 지우는지 보인다"
    s = a._confirm_rest_text({"site": "s", "method": "POST", "path": "/p", "body": '{"a":1}'})
    assert s.endswith('body="{\\"a\\":1}"'), "글 본문은 따옴표째 — 게이트웨이는 객체 · 목록 본문만 싣는다"
    assert a._confirm_rest_text({"method": "POST"}) == 'site=null · method="POST" · path=null', "빠진 칸은 null — 지어내지 않는다"


# ── C. 묶음 ────────────────────────────────────────────────────────────────────────────────────────────────────────

SF_KEY = ["list_projects", "get_project_meta", "measurement_guide"]
SF_SESSION = {"agent_type": "he-cad-stepforge", "name": "StepForge 운영자", "description": "StepForge 운영자",
              "system_prompt": "# StepForge 운영자\n역할 본문",
              "response_config": {"persona_kind": "mcp_operator", "mcp_apps": ["heax-step_forge"], "key_tools": SF_KEY}}
# 게이트웨이 /tools-map 은 rest_call · rest_catalog 를 싣지 않는다(로컬 도구 중 REST 다리는 빠졌다) — 앱 핀으로는 닿지 않는다
SF_MAP = {**{f"sf_tool_{i:02d}": "heax-step_forge" for i in range(60)}, **{k: "heax-step_forge" for k in SF_KEY}, "query_voc": "signalforge"}


def test_StepForge_운영자를_고르면_rest_catalog_와_rest_call_이_입구_도구로_묶인다(monkeypatch):
    sink, _out, _ = _run_stream(monkeypatch, SF_SESSION, SF_MAP)
    assert sink["first"] == [*SF_KEY, "rest_catalog", "rest_call"], sink["first"]
    assert sink["core"] == a._GUIDE_TOOLS, "운영자의 상시 예약(안내대)은 그대로다"
    # 캡 경로(dev TOOL_MAX=40) — 그 앱 도구 63개가 핀이라 입구 도구가 아니면 rest_call 은 캡 밖이었다
    monkeypatch.setattr(a, "TOOL_MAX", 40)
    monkeypatch.setattr(a, "TOOL_SCHEMA_BUDGET", 0)
    monkeypatch.setattr(a, "_semantic_order", lambda q, tools: [])
    tools = [_tool(n, "stepforge 파트") for n in SF_MAP] + [_tool(n, "게이트웨이 도구") for n in (*a._GUIDE_TOOLS, "rest_catalog", "rest_call")]
    kept = [t.name for t in a._select_tools(tools, "선 적용", sink["pinned"], sink["first"], sink["core"])]
    assert {"rest_catalog", "rest_call"} <= set(kept) and set(a._GUIDE_TOOLS) <= set(kept), kept
    old_first = [n for n in sink["first"] if n not in ("rest_catalog", "rest_call")]
    old = [t.name for t in a._select_tools(tools, "선 적용", [n for n in sink["pinned"] if n not in ("rest_catalog", "rest_call")],
                                           old_first, sink["core"])]
    assert "rest_call" not in old, "입구 도구가 아니던 때는 앱 도구가 캡을 다 채웠다(고치기 전의 꼴)"


def test_다른_운영자와_일반_전문가는_종전_그대로다(monkeypatch):
    sink, _out, _ = _run_stream(monkeypatch, OP_SESSION, {"predict_sed": "heax-thermal_shock_mcp"})
    assert sink["first"] == ["predict_sed", "get_dataset_summary"]
    sink, _out, _ = _run_stream(monkeypatch, PLAIN_SESSION, {"predict_sed": "heax-thermal_shock_mcp"})
    assert sink["first"] == [] and sink["pinned"] == []


def test_보조로_고른_StepForge_운영자도_입구_도구로_빌려준다(monkeypatch):
    """보조 운영자는 입구 도구만 빌려준다(앱 통째 핀은 하지 않는다) — rest_call 도 그 입구 도구다."""
    sink: dict = {}
    _stub_session(monkeypatch, {"he-calc-thermalshock": OP_SESSION, "he-cad-stepforge": SF_SESSION})
    _prime_tools_map(monkeypatch, {**SF_MAP, "predict_sed": "heax-thermal_shock_mcp"})

    async def fake_agent_for(app, groups, pinned=None, query="", sources=None, user="", user_pat="", first=None, core_names=None, prefer=None):
        sink["first"] = list(first or [])
        return _FakeAgent(sink)

    async def no_knowledge(*_a, **_k):
        return ""
    monkeypatch.setattr(a, "_agent_for", fake_agent_for)
    monkeypatch.setattr(a, "_persona_knowledge", no_knowledge)
    a._persona_knowledge.last_note = ""
    req = a.ChatRequest(message="SED 예측해줘", groups=[], pinned_agents=["he-calc-thermalshock", "he-cad-stepforge"])

    async def consume():
        return [c async for c in a._agent_stream(NS(state=NS(tool_load_error={}, tool_degraded={}, llm_nostream=False)), req)]
    asyncio.run(consume())
    assert sink["first"] == ["predict_sed", "get_dataset_summary", *SF_KEY, "rest_catalog", "rest_call"], sink["first"]


def test_좌석이_읽는_운영자_설정에는_더하지_않는다(monkeypatch):
    """심의 좌석 · 좌석 탐침(seat_probe)은 _persona_meta 를 읽고 확인 관문이 없다 — 게이트웨이 도구는 챗의 입구 도구에서만 더한다."""
    _stub_session(monkeypatch, {"he-cad-stepforge": SF_SESSION})
    m = asyncio.run(a._persona_meta(NS(state=NS()), [], "he-cad-stepforge"))
    assert m["key_tools"] == SF_KEY and m["apps"] == ["heax-step_forge"]
    assert a._operator_entry(m) == [*SF_KEY, "rest_catalog", "rest_call"] and a._operator_entry({"operator": False, "apps": ["heax-step_forge"]}) == []


# ── D. 엔진 — 사람의 확인 카드가 서지 않는 곳(리뷰 11-09) ──────────────────────────────────────────────────────────────

def _chat_gw(name, reached):
    """챗에 묶는 도구(_agent_for — _prep_tool(chat=True))의 대역."""
    async def inner(**kw):
        reached.append((name, kw))
        return f"gateway called {name}"
    return a._prep_tool(StructuredTool(name=name, description="게이트웨이 도구 대역", args_schema={"type": "object", "properties": {}},
                                       coroutine=inner), chat=True)


def _g2b(out) -> bool:
    return isinstance(out, str) and out.startswith(a._TOOL_FAIL_MARK) and "REST 다리 쓰기(rest_call POST /api/onto-link)" in out


@pytest.mark.parametrize("knob", [False, True])
def test_G2b_챗_턴_밖에서는_REST_다리_쓰기를_보내지_않는다(monkeypatch, knob):
    """심의 지정 도구 · 좌석 자유 조회 · 외부 응답자의 CALL 줄 · 띵킹 · 카드 대조는 챗 턴 맥락이 없어 G5 가 서지 않는다 — 챗 관문 손잡이와
    상관없이 막는다(닫힌 쪽). 별칭 · invoke_tool 의 안쪽(dict · JSON 글)도 같다. 조회는 그대로 간다."""
    monkeypatch.setattr(a, "CHAT_CONFIRM_GATE", knob)
    assert a._turn_confirm.get() is None
    reached = []
    for out in (_run(_gw("rest_call", reached), ONTO), _run(_gw("gateway_rest_call", reached), ONTO),
                _run(_gw("invoke_tool", reached), {"name": "rest_call", "arguments": ONTO}),
                _run(_gw("invoke_tool", reached), {"name": "rest_call", "arguments": json.dumps(ONTO)})):
        assert _g2b(out) and a._result_ok(out) is False, out
    assert reached == []
    get = {"site": "step-forge", "path": "/api/onto-candidates"}
    assert _run(_gw("rest_call", reached), get) == "gateway called rest_call"
    assert _run(_gw("invoke_tool", reached), {"name": "rest_call", "arguments": {**get, "method": "HEAD"}}) == "gateway called rest_call"
    assert len(reached) == 2


def test_G2b_챗에_묶는_도구는_관문_손잡이를_따른다():
    """챗에 묶는 도구는 G5(확인 카드)가 맡고, 관문을 끈 박스는 종전처럼 간다. 승인 실행은 엔진 로더의 도구지만 챗 턴 맥락이 있어 G5 의 승인
    열쇠로 지난다(B 의 승인 시험이 그 길이다)."""
    reached = []
    _run(_chat_gw("rest_call", reached), ONTO)
    assert reached == [("rest_call", ONTO)]


def test_G2b_외부_응답자의_CALL_줄도_같은_래퍼에서_막힌다():
    """외부 응답자의 글 왕복(_TextReactAgent)은 좌석에 묶는 것과 같은 래퍼 도구를 부른다 — 자유 조회 후보에 rest_call 이 들 길이 생겨도 막힌다."""
    reached = []
    ag = d._TextReactAgent(None, [_gw("rest_call", reached)], 2)
    assert _g2b(asyncio.run(ag._run("rest_call", ONTO)))
    assert asyncio.run(ag._run("rest_call", {"site": "s", "path": "/p"})) == "gateway called rest_call"
    assert reached == [("rest_call", {"site": "s", "path": "/p"})]


def _after_tools(ev, data):
    """지정 도구 단계가 끝난 뒤(좌석 발굴 문턱)에서 멈춘다 — 거기까지는 좌석 LLM 이 필요 없다."""
    return ev == "delib" and data.get("kind") == "stage" and data.get("stage") == "discover"


def _designate(monkeypatch, name, plan, chair="default"):
    """지정 도구 하나로 심의를 발굴 문턱까지 실제로 돌린다 — 계획자는 plan 을 짓는다. 도구는 래퍼를 지나지 않은 대역이라 지정 도구 자리의
    거절만 본다(래퍼의 G2b 는 위 시험)."""
    planned = []

    async def _planner(_llm, system, human):
        if "도구 호출 계획자" in system:
            planned.append(human)
            return json.dumps(plan, ensure_ascii=False)
        return "{}"
    monkeypatch.setattr(d, "_llm_text", _planner)
    tools = {name: _Tool(name, '{"applied": 1}'), "agent_search": _Tool("agent_search")}
    events = _stream(monkeypatch, {"chair_template": chair, "tools": [name], "persona_knowledge": 0}, tools=tools, until=_after_tools)
    return events, tools[name].calls, planned


@pytest.mark.parametrize("chair,name,plan", [("default", "rest_call", ONTO),
                                             ("default", "invoke_tool", {"name": "rest_call", "arguments": ONTO}),
                                             ("risk-review", "rest_call", ONTO)],
                         ids=["심의-rest_call", "심의-invoke_tool", "리스크-rest_call"])
def test_지정_도구의_REST_다리_쓰기는_보내기_전에_거절하고_알린다(monkeypatch, chair, name, plan):
    """deliberate_start(tools=["rest_call"]) — 챗 모델이 사람이 시킨 심의에 지정 도구를 더할 수 있다(리뷰 11-09). 계획자가 지은 쓰기 메서드는
    보내지 않고 상태줄 · 경고 사건(rest_write_refused) · 끝 카드로 알린다. 계획자에게 다시 시키지 않는다. 리스크 심사도 같다(D-18 은 rest_call 을
    닫지 않는다 — 범용 실행기는 D-18 이 먼저 닫는다)."""
    events, calls, planned = _designate(monkeypatch, name, plan, chair)
    assert calls == [] and len(planned) == 1, (calls, len(planned))
    want = f"{name} POST /api/onto-link"
    assert any(s.startswith(f"지정 도구 거절: {name} — REST 다리 쓰기(POST /api/onto-link)") for s in _steps(events)), _steps(events)
    (w,) = [x for ev, x in events if ev == "warning" and x.get("code") == "rest_write_refused"]
    assert w["tools"] == [want] and "확인 카드" in w["message"], w
    card = next(c for c in _cards(events, included=False) if c["source"] == "지정 도구 미실행")
    assert f"REST 다리 쓰기라 보내지 않은 호출: {want}" in card["text"], card["text"]
    assert not any("지정 도구 실패/건너뜀" in s for s in _steps(events)), "실패로 두 번 적지 않는다"


def test_지정_도구의_REST_다리_조회는_그대로_간다(monkeypatch):
    get = {"site": "step-forge", "path": "/api/onto-candidates", "query": {"project_id": "P1"}}
    events, calls, _planned = _designate(monkeypatch, "rest_call", get)
    assert calls == [get]
    assert not [x for ev, x in events if ev == "warning" and x.get("code") == "rest_write_refused"]


def test_MCP_로_띄운_잡도_같은_자리에서_거절하고_잡_원장에_경고가_남는다(monkeypatch, tmp_path):
    """MCP deliberate_start 는 잡(delib_jobs)이 같은 엔진 생성기를 돌린다 — 끝까지 돌려 잡 원장의 경고로 남는지 본다."""
    async def _round(_llm, personas, _prompt_fn, _rnd, required=(), validator_fn=None, opts=None, fails=None):
        for p in personas:
            yield {"persona": p["key"], "lens": "관점", "recommendation": "권장", "position_short": "입장"}

    async def _planner(_llm, system, human):
        return json.dumps(ONTO) if "도구 호출 계획자" in system else "{}"
    tools = {"rest_call": _Tool("rest_call", '{"applied": 1}'), "agent_search": _Tool("agent_search")}

    async def _tools(*_a, **_k):
        return tools
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(d, "_tools_by_name", _tools)
    monkeypatch.setattr(d, "_round_live", _round)
    monkeypatch.setattr(d, "_llm_text", _planner)
    opts = {"personas": [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}], "free_tools": 0, "voc": "off", "rescreen": 0,
            "persona_knowledge": 0, "stop_after_round": 1, "save_report": 0, "tools": ["rest_call"]}
    j = {"id": "t-rest-write", "status": "running", "question": "q", "turns": [], "checkpoint": None, "started_at": delib_jobs._now()}
    monkeypatch.setitem(delib_jobs._JOBS, j["id"], j)
    asyncio.run(delib_jobs._drive(j, d.run_deliberation(NS(state=NS(llm=object(), delib_llm=None)), "선 적용", [], opts)))
    assert not j.get("error"), j.get("error")
    assert tools["rest_call"].calls == []
    assert any("REST 다리 쓰기라 보내지 않았습니다" in w for w in j.get("warnings") or []), j.get("warnings")
