# REST 다리 쓰기의 확인 관문과 StepForge 운영자 챗의 rest_call 묶음(HWAXPortal docs/core-link §7 — SRC-15 챗 쪽)
#
# 게이트웨이가 StepForge 의 정해진 쓰기 경로(POST /api/onto-link · onto-put · onto-define)에 그 사람의 단언을 실어 보내게 된다(design §5).
# 챗에서 그 길은 rest_call 의 POST 인데, rest_call 은 운영자 정의의 writes · confirm 어디에도 없어 카드 없이 나갔다(PLAN §4 SRC-15).
#   A. 부류 — 메서드가 GET · HEAD 가 아니면(대소문자 · 앞뒤 공백 무시, 빠지면 게이트웨이처럼 GET) 운영자 목록 · 주문 말과 상관없이 늘 confirm 이다.
#      GET · HEAD 는 종전 판정 그대로이고, 운영자 목록 · 잡 바닥 목록의 판정도 그대로다.
#   B. 턴 — 직접 · invoke_tool · 별칭 모두 카드이고, 카드는 site · method · path 와 본문 앞 400자를 싣는다. 승인하면 처음 꼴 그대로 한 번 간다.
#   C. 묶음 — StepForge 운영자를 고르면 그 앱 도구가 캡(dev 40)을 다 채워 rest_call 이 묶이지 않았다. 입구 도구로 묶는다(챗만 — 좌석은 그대로).
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

import app as a  # noqa: E402
from test_confirm_gate import HE_TEAM, _card_turn, _GateAgent, _gw, _held, _result, _run, _turn  # noqa: E402
from test_confirm_gate import gate, maps, turn_ctx  # noqa: E402, F401 — 픽스처
from test_operator_persona import OP_SESSION, PLAIN_SESSION, _FakeAgent, _prime_tools_map, _run_stream, _stub_session, _tool  # noqa: E402

# 미리 보기(MCP onto_link dry_run=true)와 같은 인자를 본문에 싣고 적용한다 — 포털 he-team.json he-cad-stepforge 에 더할 줄(design §7)의 꼴
ONTO_BODY = {"project_id": "P1", "links": [{"from": "cad_part:BAT-1", "rel": "instance_of", "to": "part:battery"}], "dry_run": False}
ONTO = {"site": "step-forge", "method": "POST", "path": "/api/onto-link", "body": ONTO_BODY}
ONTO_TEXT = ('site=step-forge · method=POST · path=/api/onto-link · '
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
    assert json.loads(c["args"]) == {**ONTO, "method": "post"}, "화면이 펼칠 인자 전부(정규 JSON)는 그대로다"
    res = _result(evs)
    assert "**확인이 필요합니다** — 되돌리기 어려운 작업이라 보내기 전에 확인합니다." in res
    assert f"1. `rest_call` · {ONTO_TEXT}" in res, res
    assert reached == []
    ag = _GateAgent(tools, steps=[], say="선 1개를 적용했습니다.")
    _turn(monkeypatch, ag, tools, card["approve_text"], hist)
    assert reached == [("rest_call", {**ONTO, "method": "post"})], "승인하면 서버가 처음 꼴 그대로 한 번 보낸다(대문자로 고치지 않는다)"


def test_카드의_본문은_앞_400자이고_query_와_글_본문도_보인다():
    big = {"links": [{"from": f"cad_part:P{i:04d}", "to": "part:battery"} for i in range(40)]}
    txt = a._confirm_rest_text({"site": "step-forge", "method": "post", "path": "/api/onto-put", "body": big})
    body = json.dumps(big, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert txt == f"site=step-forge · method=POST · path=/api/onto-put · body={body[:400]}…({len(body):,}자)", txt
    q =a._confirm_rest_text({"site": "s", "method": "DELETE", "path": "/api/x", "query": {"id": 7}})
    assert q == 'site=s · method=DELETE · path=/api/x · query={"id":7}', "본문 없는 DELETE 도 무엇을 지우는지 보인다"
    s = a._confirm_rest_text({"site": "s", "method": "POST", "path": "/p", "body": '{"a":1}'})
    assert s.endswith('body="{\\"a\\":1}"'), "글 본문은 따옴표째 — 게이트웨이는 객체 · 목록 본문만 싣는다"
    nl = a._confirm_rest_text({"site": "s", "method": "POST", "path": "/p\n/q"})
    assert "\n" not in nl and "⏎" in nl


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
