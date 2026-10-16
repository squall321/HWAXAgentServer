# 외부 응답자 X-2 반대 검토(X-D7 ④)의 회귀 — 이 서버의 챗·심의 에이전트는 리스크 앱 잡 시작(risk_start_job)과 REST 다리(rest_call)로도 Claude 선택·반출 동의 칸을 싣지 못한다
#
# 검토의 가설(지식카드에 숨은 지시 한 줄로 GLM 턴이 사용자 이름의 risk_start_job(llm=claude, consent_external=true)을 부른다)을 X-D6 의
# 거절과 같은 자리(_cap_tool — 게이트웨이에 닿기 전)에서 막는지 본다. 그 칸이 없는 호출은 종전 그대로 간다. 결정은 HWAXPortal
# docs/risk-claude-engine/context-notes.md X-D7. 실제 Claude 는 부르지 않는다(X-D1).
#
#   실행:  .venv/bin/python -m pytest tests/test_external_llm_risk_gate.py -q
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402

import app as a  # noqa: E402


def _gw(name: str, reached: list):
    """게이트웨이 도구 대역 — MCP 어댑터 도구처럼 인자 스키마가 JSON 스키마 dict 라 인자가 그대로 넘어오고, 챗·심의가 묶는 것과 같은
    래퍼(_prep_tool)를 지난다. 부르면 reached 에 (이름, 인자)를 적는다."""
    if name == "invoke_tool":
        schema = {"type": "object", "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}},
                  "required": ["name"]}

        async def inner(name: str = "", arguments=None):
            reached.append((name, arguments))
            return f"gateway called {name}"
    else:
        schema = {"type": "object", "properties": {}}

        async def inner(**kw):
            reached.append((name, kw))
            return f"gateway called {name}"
    return a._prep_tool(StructuredTool(name=name, description="게이트웨이 도구 대역", args_schema=schema, coroutine=inner))


def _refused(out) -> bool:
    return isinstance(out, str) and out.startswith(a._TOOL_FAIL_MARK) and "consent_external" in out and "보내지 않았다" in out


_RISK_FORGED = [
    {"target_key": "diff:ab", "mode": "reviews", "llm": "claude", "consent_external": True},
    {"target_key": "diff:ab", "mode": "reviews", "llm": "claude"},             # 동의 칸 없이도 — 고르는 것부터 사람의 몫이다
    {"target_key": "diff:ab", "llm": " Claude "},                              # 앞뒤 공백 · 대소문자를 맞춘다
    {"target_key": "diff:ab", "llm": "external"},                              # 엔진의 이름으로 적어도
    {"target_key": "diff:ab", "consent_external": True},                       # 동의 칸만 — 값과 상관없이 싣지 못한다
    {"target_key": "diff:ab", "consent_external": False, "llm": "glm"},
    {"target_key": "diff:ab", "Consent_External ": "true"},
    {"target_key": "diff:ab", "LLM": "CLAUDE"},
]


@pytest.mark.parametrize("args", _RISK_FORGED, ids=range(len(_RISK_FORGED)))
@pytest.mark.parametrize("inner", ["risk_start_job", "hwax_risk_risk_start_job", " HEAX-HWAX_RISK_risk_start_job "])
def test_invoke_tool_로_부르는_리스크_잡_시작에_Claude_선택이나_동의_칸이_있으면_게이트웨이에_보내지_않는다(inner, args):
    """X-D7 ④ — 게이트웨이는 risk_start_job 을 사용자 신원으로 중계하므로, 숨은 지시를 받은 GLM 턴이 llm=claude · consent_external=true 를
    스스로 달면 그 사람의 이름으로 Claude 잡이 서고 리스크 앱에 거짓 동의 기록(누가 · 언제 · mcp 입구)이 남는다. 게이트웨이 별칭 ·
    JSON 글로 온 인자 · 위치 dict 로 온 인자도 같다."""
    reached = []
    t = _gw("invoke_tool", reached)
    for form in (args, json.dumps(args)):
        assert _refused(asyncio.run(t.ainvoke({"name": inner, "arguments": form}))), form
    assert _refused(asyncio.run(t.coroutine({"name": inner, "arguments": args})))
    assert reached == [], "막아야 할 호출이 게이트웨이까지 갔다"


@pytest.mark.parametrize("args", _RISK_FORGED, ids=range(len(_RISK_FORGED)))
@pytest.mark.parametrize("name", ["risk_start_job", "hwax_risk_risk_start_job"])
def test_직접_묶인_리스크_잡_시작에_Claude_선택이나_동의_칸을_실어도_보내지_않는다(name, args):
    reached = []
    assert _refused(asyncio.run(_gw(name, reached).ainvoke(args)))
    assert reached == []


_REST_FORGED = [
    {"site": "hwax-risk", "path": "/api/targets/diff:ab/jobs", "method": "POST",
     "body": {"mode": "reviews", "llm": "claude", "consent_external": True}},
    {"site": "hwax-risk", "path": "/api/targets/diff:ab/jobs", "method": "POST", "body": {"mode": "reviews", "llm": "claude"}},
    {"site": "hwax-risk", "path": "/apps/hwax_risk/api/targets/diff:ab/jobs/", "method": "POST", "body": {"llm": " External "}},
    {"site": "hwax-risk", "path": "api/targets/diff:ab/jobs?mode=reviews", "method": "POST",
     "body": json.dumps({"llm": "claude"})},                                   # 앞 슬래시 없음 · 질의 · JSON 글 본문
    {"site": "hwax-risk", "path": "/api/jobs/j-1/resume", "method": "POST", "body": {"LLM": "claude"}},
    {"site": "anything", "path": "/agent/card-review/run", "method": "POST", "body": {"consent_external": True}},
    {"site": "anything", "path": "/v1/whatever", "method": "PUT", "body": {"Consent_External": False}},   # 동의 칸은 어디로든
]


@pytest.mark.parametrize("args", _REST_FORGED, ids=range(len(_REST_FORGED)))
@pytest.mark.parametrize("inner", ["rest_call", "gateway_rest_call"])
def test_REST_다리로_리스크_잡_경로에_Claude_선택이나_어디로든_동의_칸을_실으면_보내지_않는다(inner, args):
    """X-D7 ④ — 같은 일을 게이트웨이 REST 다리(rest_call)로 하면 리스크 앱 POST /api/targets/<key>/jobs 에 llm=claude ·
    consent_external=true 본문이 사용자 이름으로 간다. 본문의 반출 동의 칸은 사이트 · 경로와 상관없이, llm claude · external 은 리스크
    앱 잡 경로에서 막는다. invoke_tool 로도 · 직접 묶어도 같다."""
    reached = []
    t = _gw("invoke_tool", reached)
    for form in (args, json.dumps(args)):
        assert _refused(asyncio.run(t.ainvoke({"name": inner, "arguments": form}))), form
    assert _refused(asyncio.run(_gw(inner, reached).ainvoke(args)))
    assert reached == []


def test_그_칸이_없는_리스크_잡_시작과_REST_다리는_종전대로_게이트웨이에_간다():
    """막는 것은 위의 칸이 있을 때뿐이다 — GLM 잡 시작 · 다른 경로의 llm · 질의 · 목록 조회 · 비슷한 다른 이름은 그대로 간다."""
    reached = []
    inv = _gw("invoke_tool", reached)
    calls = [
        ("risk_start_job", {"target_key": "diff:ab", "mode": "reviews", "scope": {"agents": ["mech-a"]}}),
        ("risk_start_job", {"target_key": "diff:ab", "mode": "reviews", "llm": "glm"}),
        ("risk_start_job", {"target_key": "diff:ab", "llm": None}),
        ("risk_start_job", {"advanced": {"llm": "external", "consent_external": True}}),   # 그 도구의 칸이 아니다(X-D6 시험과 같다)
        ("rest_call", {"site": "hwax-risk", "path": "/api/targets/diff:ab/jobs", "method": "POST", "body": {"mode": "reviews"}}),
        ("rest_call", {"site": "hwax-risk", "path": "/api/targets/diff:ab/jobs", "method": "POST", "body": {"llm": "glm"}}),
        ("rest_call", {"site": "other", "path": "/v1/chat", "method": "POST", "body": {"llm": "claude"}}),
        ("rest_call", {"site": "hwax-risk", "path": "/api/targets/diff:ab/jobs", "method": "GET", "query": {"llm": "external"}}),
        ("rest_catalog", {"site": "hwax-risk", "q": "consent_external"}),
        ("risk_start_jobs", {"llm": "claude", "consent_external": True}),
        ("risk_job_action", {"job_id": "j-1", "action": "resume"}),
    ]
    outs = [asyncio.run(inv.ainvoke({"name": n, "arguments": args})) for n, args in calls]
    assert outs == [f"gateway called {n}" for n, _ in calls]
    assert reached == calls
    direct = []
    out = asyncio.run(_gw("risk_start_job", direct).ainvoke({"target_key": "diff:ab", "mode": "reviews"}))
    assert out == "gateway called risk_start_job" and direct == [("risk_start_job", {"target_key": "diff:ab", "mode": "reviews"})]


def test_막는_판정은_한_함수다():
    """리스크 잡 시작 · REST 다리의 판정은 심의 도구와 같은 함수(_agent_external_call)가 내린다 — 직접 묶인 도구와 invoke_tool 의 안쪽
    이름이 같은 판정을 받는다."""
    assert a._agent_external_call("risk_start_job", {"llm": "claude"})
    assert a._agent_external_call("hwax_risk_risk_start_job", json.dumps({"consent_external": False}))
    assert a._agent_external_call("rest_call", {"path": "/x/targets/snap:1/jobs", "body": {"llm": "external"}})
    assert not a._agent_external_call("rest_call", {"path": "/x/targets/snap:1/jobs", "body": "not json"})
    assert not a._agent_external_call("rest_call", {"path": "/x/targets/snap:1/units", "body": {"llm": "claude"}})
    assert not a._agent_external_call("risk_start_job", None)
    assert not a._agent_external_call("deliberate_status", {"consent_external": True})
