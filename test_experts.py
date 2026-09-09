# /deliberate/experts 단위 테스트 — 추천 원 응답의 relevant_tools 가 expert_tools 로 실려 나가는지(스코프 회귀 방지)
import asyncio
import json

import pytest

import app as A


_RECD = {
    "agents": [
        {"agent_type": "sim-drop-impact", "name": "낙하 해석", "description": "d",
         "score": 3.1, "desc_match": 0.6, "why": "w", "low_confidence": False},
        {"agent_type": "mech-drop-impact", "name": "낙하 기구", "description": "d",
         "score": 3.0, "desc_match": 0.6, "why": "w", "low_confidence": False},
    ],
    "relevant_tools": [
        {"name": "submit_lsdyna_job", "description": "잡 제출", "score": 0.8,
         "compatible_agents": ["sim-drop-impact"]},
        {"name": "get_curve", "description": "곡선 조회", "score": 0.7, "compatible_agents": []},
    ],
}


@pytest.fixture
def stub(monkeypatch):
    """게이트웨이 없이 도는 /deliberate/experts — 도구 호출을 이름별 캔에 붙인다."""
    async def fake_tools(_app, _groups, *a, **kw):
        return {"recommend_agents": object(), "list_agents": object(),
                "list_agent_domains": object()}

    async def fake_call(_tools, name, _args):
        if name == "recommend_agents":
            return json.dumps(_RECD, ensure_ascii=False)
        if name == "list_agents":
            return json.dumps([{"agent_type": "sim-drop-impact", "name": "낙하 해석",
                                "common_tags": []}], ensure_ascii=False)
        return "[]"

    monkeypatch.setattr(A, "_tools_by_name", fake_tools)
    monkeypatch.setattr(A, "_call", fake_call)
    # lifespan 밖이라 app.state 가 비어 있다 — _seat_axes 인자로 읽히는 칸만 채운다.
    A.app.state.llm = None
    # 축 추출은 LLM 을 부른다 — 이 테스트의 관심 밖이라 끈다(history 가 비면 어차피 빈 목록).
    monkeypatch.setattr(A, "_seat_axes", lambda *a, **kw: _empty())
    return None


async def _empty():
    return []


def _run(message="낙하 충격 디스플레이 크랙"):
    return asyncio.run(A.deliberate_experts(A.ExpertsRequest(message=message, groups=[])))


def test_expert_tools_is_populated_from_recommendation_response(stub):
    """⚠ 회귀 방지 — 예전엔 _rank 의 지역변수 recd 를 바깥에서 참조해 매번 NameError 였고,
    try/except 가 그것을 삼켜 이 칸이 **항상 빈 배열**이었다(실측 2026-09-09)."""
    out = _run()
    tools = out["tools"]
    assert len(tools["expert_tools"]) == 2
    names = {t["name"] for t in tools["expert_tools"]}
    assert names == {"submit_lsdyna_job", "get_curve"}
    first = next(t for t in tools["expert_tools"] if t["name"] == "submit_lsdyna_job")
    assert first["agents"] == ["sim-drop-impact"]
    assert first["desc"] == "잡 제출"


def test_recommended_and_candidates_still_come_through(stub):
    out = _run()
    assert [r["key"] for r in out["recommended"]] == ["sim-drop-impact", "mech-drop-impact"]
    assert out["low_confidence"] is False


def test_gateway_down_returns_marker_not_crash(monkeypatch):
    async def no_tools(_app, _groups, *a, **kw):
        return {}

    monkeypatch.setattr(A, "_tools_by_name", no_tools)
    out = _run()
    assert out["error"] == "gateway_unavailable" and out["recommended"] == []
