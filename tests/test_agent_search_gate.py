# 전문가 검색 가로채기(is_agent_search)를 좁힌 것 — 빗금은 목록, 자연어는 다른 물음 · 고른 전문가가 없을 때만(HWAXPortal docs/request-1030 HA-31)
"""10-30 디지털 워커 QA DW-13 — 'PCB 내부 크랙을 잘 아는 전문가 에이전트를 추천하고, 추천 근거가 된 기록 수를 알려줘.' 가 80자 이하 +
'(전문가 · 에이전트) … (추천 · 알려 · 찾 …)' 에 걸려 LLM 0회로 추천 목록만 받았다('기록 수' 는 답이 없었다). AI 데이터 허브 운영자를 고른
대화에서도 가로챘다. 추천 질의는 질문 전체였다 — 요청 낱말이 겹침 점수를 받아 무관한 전문가가 올랐다(순위 몫은 AIDataHub AD-19).

    실행:  .venv/bin/python -m pytest tests/test_agent_search_gate.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as a  # noqa: E402

DW13 = "PCB 내부 크랙을 잘 아는 전문가 에이전트를 추천하고, 추천 근거가 된 기록 수를 알려줘."


def test_DW13_은_챗으로_가고_찾는_말만인_질문은_그대로_가로챈다():
    assert len(DW13) <= 80 and a._AGENT_SEARCH_RE.search(DW13), "종전 판정에는 걸리던 문장이다"
    assert a.is_agent_search(DW13) is False
    for m in ("낙하 해석 전문가 추천해 줘", "전문가 뭐 있어", "PCB 휨 전문가 있어?", "낙하 쪽 에이전트 목록 보여줘"):
        assert a.is_agent_search(m) is True, m


def test_다른_물음은_갈래마다_하나만_있어도_챗으로_간다():
    cases = {
        "수": ("낙하 전문가 알려줘, 전부 몇 명이야?", "낙하 전문가 목록, 지식카드 수도", "낙하 전문가 목록이랑 건수"),
        "근거": ("낙하 전문가 추천해 줘. 근거도 함께", "낙하 전문가 추천해 줘, 왜 그 사람인지도", "낙하 전문가 추천해 줘. 출처도",
                 "낙하 전문가 추천해 줘, 기록 많은 사람으로", "PCB 크랙 전문가 추천 이유"),
        "비교": ("낙하 전문가 추천해 줘. 누가 더 잘 아는지도", "낙하 전문가 추천해 줘, 어느 쪽이 나아?", "낙하 전문가 추천해 줘 비교해서"),
        "조건": ("낙하 높이가 2m 이상이면 어떤 전문가 있어?", "만약 낙하 시험이 실패하면 전문가 추천해 줘", "시험 조건별로 전문가 추천해 줘",
                 "충격이 큰 경우 전문가 추천해 줘"),
        "둘째 요청": ("낙하 전문가 추천하고 그 사람과 대화 시작해 줘", "낙하 전문가 추천해 주고 지식카드 요약해 줘", "낙하 전문가 알려주고 연락처도"),
    }
    for kind, msgs in cases.items():
        for m in msgs:
            assert a._AGENT_SEARCH_RE.search(m), (kind, m, "찾는 말은 있다 — 거르개가 막은 것이어야 한다")
            assert a.is_agent_search(m) is False, (kind, m)


def test_분야_낱말과_할_수_있는_은_다른_물음이_아니다():
    for m in ("낙하 해석을 할 수 있는 전문가 추천해 줘", "낙하 해석할 수도 있는 전문가 찾아줘", "왜곡 해석 전문가 추천해 줘",
              "근거리 통신 전문가 찾아줘", "수치 해석 전문가 추천해 줘", "다면체 메시 전문가 추천해 줘", "낙하 전문가 찾고 싶어",
              "낙하 전문가 찾고 있어"):
        assert a.is_agent_search(m) is True, m


def test_빗금_트리거는_다른_물음이_있어도_고른_대화에서도_목록이다():
    long_slash = "/전문가 " + "낙하 " * 40
    assert len(long_slash) > 80
    for m in ("/전문가", "/전문가 낙하 해석", "/전문가 낙하 해석 근거 몇 건", "/에이전트 PCB", "/agents drop", "/agent", long_slash):
        assert a.is_agent_search(m) is True, m
        assert a.is_agent_search(m, pinned=True) is True, m


def test_고른_전문가가_있으면_자연어는_가로채지_않고_80자_한도는_그대로다():
    assert a.is_agent_search("낙하 해석 전문가 추천해 줘", pinned=True) is False
    assert a.is_agent_search("전문가 뭐 있어", pinned=True) is False
    long_nl = "낙하 해석 " * 15 + "전문가 추천해 줘"
    assert len(long_nl) > 80 and a._AGENT_SEARCH_RE.search(long_nl)
    assert a.is_agent_search(long_nl) is False


def _route(monkeypatch, **kw):
    """chat() 이 어느 길로 보냈나 — 목록(run_agent_search)과 챗(_agent_stream)만 가짜로 바꿔 끼운다."""
    went = []

    async def fake_search(_app, query, _groups):
        went.append(("목록", query))
        yield a._sse("done", {})

    async def fake_agent(_app, req):
        went.append(("챗", req.message))
        yield a._sse("done", {})

    monkeypatch.setattr(a, "run_agent_search", fake_search)
    monkeypatch.setattr(a, "_agent_stream", fake_agent)
    resp = asyncio.run(a.chat(a.ChatRequest(**kw)))

    async def body():
        return b"".join([c async for c in resp.body_iterator]).decode("utf-8")

    assert "event: done" in asyncio.run(body())
    return went


def test_부르는_곳_고른_대화는_가로채지_않고_빗금은_목록이다(monkeypatch):
    m = "낙하 해석 전문가 추천해 줘"
    assert _route(monkeypatch, message=m) == [("목록", m)]
    assert _route(monkeypatch, message=m, pinned_agent="he-aidatahub-operator") == [("챗", m)]
    assert _route(monkeypatch, message=m, pinned_agents=["he-aidatahub-operator", "rel-drop-impact"]) == [("챗", m)], \
        "여러 명만 보낸 꼴(pinned_agents)도 고른 대화다 — _persona_keys 와 같다"
    assert _route(monkeypatch, message=DW13) == [("챗", DW13)]
    assert _route(monkeypatch, message="/전문가 낙하", pinned_agent="he-aidatahub-operator") == [("목록", "낙하")]


def test_권한_판정도_같은_길을_본다():
    """_denied_feature 는 chat() 과 같은 순서로 가른다 — 고른 대화의 자연어는 목록이 아니니 Thinking 이면 Thinking 권한을 본다."""
    m = "낙하 해석 전문가 추천해 줘"
    have = ["feat:chat", "feat:expert-chat"]
    assert a._denied_feature(a.ChatRequest(message=m, entitlements=["feat:chat"])) == "feat:expert-chat"
    assert a._denied_feature(a.ChatRequest(message=m, pinned_agent="x", thinking=True, entitlements=have)) == "feat:thinking"
    assert a._denied_feature(a.ChatRequest(message=m, pinned_agent="x", entitlements=have)) is None
    assert a._denied_feature(a.ChatRequest(message=DW13, entitlements=["feat:chat"])) is None, "챗으로 가는 질문에 전문가 권한을 묻지 않는다"


def test_추천_질의에서_찾는_말을_뺀다():
    q = a._agent_search_query
    assert q("낙하 해석 전문가 추천해 줘") == "낙하 해석"
    assert q("PCB 내부 크랙을 잘 아는 전문가 에이전트를 추천해 줘") == "PCB 내부 크랙을 잘 아는"
    assert q("낙하전문가 추천 부탁해요") == "낙하"
    assert q("왜곡 해석 전문가 찾아줘") == "왜곡 해석"
    assert q("reagent agentic 전문가 Agents 좀 알려줘") == "reagent agentic", "영어는 낱말째로만 뺀다"
    assert q("전문가 뭐 있어?") == "" and q("") == "" and q("낙하") == "낙하"


def test_run_agent_search_는_뺀_질의로_추천하고_비면_추천을_부르지_않는다(monkeypatch):
    calls = []

    async def fake_tools(*_a, **_k):
        return {"recommend_agents": object(), "list_agents": object()}

    async def fake_call(_tools, name, args):
        calls.append((name, args))
        if name == "recommend_agents":
            return json.dumps({"agents": [{"agent_type": "sim-drop-impact", "name": "낙하 해석", "description": "d"}]},
                              ensure_ascii=False)
        return json.dumps([{"agent_type": "sim-drop-impact", "name": "낙하 해석"}], ensure_ascii=False)

    monkeypatch.setattr(a, "_tools_by_name", fake_tools)
    monkeypatch.setattr(a, "_call", fake_call)

    async def run(text):
        return b"".join([c async for c in a.run_agent_search(a.app, text, [])]).decode("utf-8")

    out = asyncio.run(run("낙하 해석 전문가 추천해 줘"))
    assert [c for c in calls if c[0] == "recommend_agents"] == [("recommend_agents", {"q": "낙하 해석", "top_k": 10})]
    assert '"query": "낙하 해석"' in out and "'낙하 해석' 관련 추천 1명" in out
    calls.clear()
    out = asyncio.run(run("전문가 뭐 있어?"))
    assert [c[0] for c in calls] == ["list_agents"], "분야 낱말이 없으면 요청 낱말로 추천하지 않는다"
    assert "전문가 1명이 등록돼 있습니다" in out and "관련 추천" not in out
