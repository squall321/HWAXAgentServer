# 전문가 검색 가로채기(is_agent_search)를 좁힌 것 — 빗금은 목록, 자연어는 다른 물음 · 고른 전문가가 없을 때만(HWAXPortal docs/request-1030 HA-31)
"""10-30 디지털 워커 QA DW-13 — 'PCB 내부 크랙을 잘 아는 전문가 에이전트를 추천하고, 추천 근거가 된 기록 수를 알려줘.' 가 80자 이하 +
'(전문가 · 에이전트) … (추천 · 알려 · 찾 …)' 에 걸려 LLM 0회로 추천 목록만 받았다('기록 수' 는 답이 없었다). AI 데이터 허브 운영자를 고른
대화에서도 가로챘다. 추천 질의는 질문 전체였다 — 요청 낱말이 겹침 점수를 받아 무관한 전문가가 올랐다(순위 몫은 AIDataHub AD-19).

반대 검토(2차)에서 둘째 요청을 다른 문장 · '-아서' · '그리고' · '-한 다음' 으로 이은 꼴, 조사가 붙은 '수'('수까지' · '카드수'), 동사의
'-면'(실패하면), 기준(제일 · 순으로)이 목록으로 새었고, 갈래 몇을 지워도 시험이 다 통과했다. 그래서 갈래마다 그 갈래 하나만 걸리는 문장을 둔다.
빗금 질의는 사람이 적은 그대로 넘긴다(정리하면 'he-agent-builder' 가 'he- -builder' 가 됐다).

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


# 갈래마다 그 갈래 하나만 걸리는 문장이다 — 한 갈래를 지우면 그 문장이 목록으로 새어 이 시험이 깨진다(1차 시험은 갈래 일곱을 지워도 통과했다).
ONE_BRANCH = {
    "수": ("낙하 전문가 추천해 줘 몇 명이야", "낙하 전문가 추천해 줘 개수만", "낙하 전문가 추천해 줘 갯수만", "낙하 전문가 목록이랑 건수",
          "낙하 전문가 추천해 줘 호출 횟수만", "낙하 전문가 추천해 줘 인원수만", "낙하 전문가 추천해 줘 명수만",
          "PCB 크랙 전문가 추천해 줘 점수만", "낙하 전문가 추천해 줘 얼마나 아는지", "낙하 전문가 추천해 줘 카드수만",
          "낙하 전문가 추천해 줘 3명만", "낙하 전문가 추천해 줘 카드 수까지", "낙하 전문가 추천해 줘 카드 수랑 이름",
          "낙하 전문가 추천해 줘 카드 수로 정렬", "낙하 전문가 추천해 줘 카드 수와 함께", "낙하 전문가 추천해 줘 카드 수의 합",
          "낙하 전문가 추천해 줘 분야별 카드 수"),
    "근거": ("낙하 전문가 추천해 줘 근거와 함께", "낙하 전문가 추천해 줘 기록 많은 사람으로", "낙하 전문가 추천해 줘 출처와 함께",
            "PCB 크랙 전문가 추천 이유", "낙하 전문가 추천해 줘 왜 그 사람인지"),
    "비교": ("낙하 전문가 추천해 줘 비교해서", "낙하 전문가 추천해 줘 차이점 위주로", "낙하 전문가 추천해 줘 누가 더 잘 아는지",
            "낙하 전문가 추천해 줘 어느 쪽이 나은지", "낙하 vs 열 전문가 추천해 줘"),
    "기준": ("낙하 전문가 추천해 줘 제일 잘 아는 사람으로", "PCB 크랙에 가장 적합한 전문가 추천해 줘", "낙하 해석 최고 전문가 추천해 줘",
            "낙하 전문가 추천해 줘 순위 매겨서", "낙하 전문가 추천해 줘 랭킹으로", "낙하 전문가 추천해 줘 카드 많은 순으로",
            "낙하 전문가 추천해 줘 잘 아는 순서대로", "낙하 전문가 추천해 줘 상위권만", "신뢰도 높은 PCB 크랙 전문가 추천해 줘"),
    "조건": ("시험 조건별로 전문가 추천해 줘", "충격이 큰 경우 전문가 추천해 줘", "만약을 대비해 낙하 전문가 추천해 줘",
            "만일을 대비해 낙하 전문가 추천해 줘", "온도가 높으면 어떤 전문가 추천해?", "낙하 시험이 실패하면 전문가 추천해 줘",
            "부품이 파손되면 어떤 전문가 있어?", "크랙이 나면 어떤 전문가 있어?", "충격이 크면 어떤 전문가 있어?",
            "물이 들어오면 어떤 전문가 있어?", "반대쪽으로 가면 어떤 전문가 있어?", "해석해 보면 어떤 전문가 있어?",
            "이 소재를 쓰면 어떤 전문가 있어?", "폰을 떨어뜨리면 어떤 전문가 있어?", "온도가 다르면 어떤 전문가 있어?",
            "크랙이 생기면 어떤 전문가 있어?", "두께가 두꺼우면 어떤 전문가 있어?", "높이가 2m 이상이면 어떤 전문가 있어?",
            "양산 전이라면 어떤 전문가 있어?", "양산 전이었다면 어떤 전문가 있어?", "폰이 떨어지면 어떤 전문가 있어?",
            "온도가 높아지면 어떤 전문가 있어?", "기기가 무거워지면 어떤 전문가 있어?", "응답이 느려지면 어떤 전문가 있어?",
            "속도가 빨라지면 어떤 전문가 있어?", "충격이 커지면 어떤 전문가 있어?", "화면이 꺼지면 어떤 전문가 있어?",
            "화면이 깨지면 어떤 전문가 있어?", "접합이 약해지면 어떤 전문가 있어?", "기판이 부서지면 어떤 전문가 있어?",
            "크랙이 있으면 어떤 전문가 있어?", "낙하 전문가가 없다면 열 전문가 추천해 줘"),
    "둘째 요청": ("원인 찾아보고 낙하 전문가 추천해 줘", "그리고 낙하 전문가 추천해 줘", "그러고 낙하 전문가 추천해 줘",
               "그다음 낙하 전문가 추천해 줘", "그런데 낙하 전문가 추천해 줘", "근데 낙하 전문가 추천해 줘", "이어서 낙하 전문가 추천해 줘",
               "아울러 낙하 전문가 추천해 줘", "또한 낙하 전문가 추천해 줘", "낙하 전문가 추천해 줘. 그 사람 카드 보여줘",
               "PCB 크랙 원인이 뭐야? 전문가 추천해 줘", "낙하 전문가 추천해 줘\n그 사람 카드 보여줘",
               "PCB 크랙 원인 알려줘 낙하 전문가도 추천해 줘", "크랙 원인 알려 주세요 낙하 전문가도 추천해 주세요",
               "크랙 원인 정리해 줄래 낙하 전문가도 추천해 줄래", "크랙 원인 정리해 주실래요 낙하 전문가 추천해 주실래요",
               "크랙 원인 설명하고 낙하 전문가 추천해 줘", "크랙 원인을 분석해서 전문가 추천해 줘",
               "PCB 크랙 전문가 추천해 줘, 그 사람한테 질문해 줘", "어떤 전문가가 있고 누가 잘 아는지 알려줘",
               "낙하 전문가 찾아서 연락처 알려줘", "낙하 전문가 추천하며 그 사람 카드 보여줘", "낙하 전문가 추천해 줘 그 사람 카드도",
               "낙하 전문가 추천한 다음 심의 열어줘", "낙하 전문가 추천한뒤 심의 열어줘", "낙하 전문가 찾은 후 심의 열어줘",
               "낙하 전문가 추천해 줘 또 다른 분야"),
}

# 1차 시험의 문장과 반대 검토(2차)가 목록으로 샌다고 보인 문장 — 실제 질문 꼴이다(갈래 여럿이 함께 걸려도 된다).
SEEN = (
    "낙하 전문가 알려줘, 전부 몇 명이야?", "낙하 전문가 목록, 지식카드 수도", "낙하 전문가 추천해 줘. 근거도 함께",
    "낙하 전문가 추천해 줘, 왜 그 사람인지도", "낙하 전문가 추천해 줘. 출처도", "낙하 전문가 추천해 줘, 기록 많은 사람으로",
    "낙하 전문가 추천해 줘. 누가 더 잘 아는지도", "낙하 전문가 추천해 줘, 어느 쪽이 나아?", "낙하 높이가 2m 이상이면 어떤 전문가 있어?",
    "만약 낙하 시험이 실패하면 전문가 추천해 줘", "낙하 전문가 추천하고 그 사람과 대화 시작해 줘", "낙하 전문가 추천해 주고 지식카드 요약해 줘",
    "낙하 전문가 알려주고 연락처도",
    "PCB 내부 크랙을 잘 아는 전문가 에이전트를 추천해 줘. 그 전문가의 대표 카드도 보여줘.", "낙하 전문가 추천해 줘. 그 사람 지식카드도 요약해 줘",
    "낙하 전문가 추천해서 바로 대화 연결해 줘", "PCB 크랙 전문가 추천해 줘, 그리고 그 사람한테 질문해 줘",
    "PCB 내부 크랙을 잘 아는 전문가 에이전트를 추천해 줘. 그리고 그 전문가에게 크랙 원인을 물어봐줘",
    "PCB 크랙 전문가 추천해 줘, 지식카드 수까지", "낙하 전문가 알려줘, 카드 수랑 이름도", "낙하 전문가 추천해 줘. 카드 수로 정렬해서",
    "PCB 크랙 전문가 추천해줘, 카드수도", "PCB 크랙 전문가 추천해줘, 점수도", "낙하 전문가수 알려줘", "낙하 시험에서 크랙이 나면 어떤 전문가 있어?",
    "낙하 시험이 실패하면 전문가 추천해 줘", "낙하 전문가 추천해 줘, 제일 잘 아는 사람으로", "낙하 전문가 추천해 줘, 카드 많은 순으로",
    "낙하 전문가 추천해 줘 순위도", "PCB 크랙 전문가 추천해줘, 신뢰도도 알려줘",
)


def test_다른_물음은_갈래마다_하나만_있어도_챗으로_간다():
    for kind, msgs in ONE_BRANCH.items():
        for m in msgs:
            assert len(m) <= 80 and a._AGENT_SEARCH_RE.search(m), (kind, m, "찾는 말은 있다 — 거르개가 막은 것이어야 한다")
            assert a.is_agent_search(m) is False, (kind, m)


def test_검토에서_새던_문장도_챗으로_간다():
    for m in SEEN:
        assert len(m) <= 80 and a._AGENT_SEARCH_RE.search(m), m
        assert a.is_agent_search(m) is False, m


def test_분야_낱말과_할_수_있는_은_다른_물음이_아니다():
    """'-면 · 수 · 이음 어미' 를 넓혀도 분야 낱말(표면 · 계면 · 접지면 · 변수 · 방수 · 센서 · 사고 …)과 부탁 꼴은 목록 그대로다."""
    for m in ("낙하 해석을 할 수 있는 전문가 추천해 줘", "낙하 해석할 수도 있는 전문가 찾아줘", "왜곡 해석 전문가 추천해 줘",
              "근거리 통신 전문가 찾아줘", "수치 해석 전문가 추천해 줘", "다면체 메시 전문가 추천해 줘", "낙하 전문가 찾고 싶어",
              "낙하 전문가 찾고 있어", "어떤 전문가가 있는지 알려줘", "낙하 전문가 알려주세요",
              "표면 처리 전문가 추천해 줘", "계면 박리 전문가 추천해 줘", "도면 검토 전문가 추천해 줘", "접지면 설계 전문가 추천해 줘",
              "지면 충돌 전문가 찾아줘", "파단면 분석 전문가 추천해 줘", "센서 전문가 추천해 줘", "사고 분석 전문가 추천해 줘",
              "보고서 작성 전문가 추천해 줘", "변수 최적화 전문가 추천해 줘", "방수 설계 전문가 추천해 줘", "주파수 해석 전문가 추천해 줘",
              "치수 공차 전문가 추천해 줘", "수명 예측 전문가 추천해 줘", "2.5D 패키지 전문가 추천해 줘", "낙하, 충격 전문가 추천해 줘",
              "PCB 크랙 전문가 있으면 알려줘", "낙하 전문가 있다면 추천해 줘", "낙하 전문가 추천해 주시면 감사하겠습니다",
              "낙하 해석 전문가를 추천해 주실 수 있나요?", "낙하 해석을 하고 있는 전문가 추천해 줘",
              "낙하 해석을 하려면 어떤 전문가가 있어?", "전문가 추천해 줘 낙하 해석 쪽으로", "낙하 전문가 추천해 줘!"):
        assert a.is_agent_search(m) is True, m


def test_빗금_트리거는_다른_물음이_있어도_고른_대화에서도_목록이다():
    long_slash = "/전문가 " + "낙하 " * 40
    assert len(long_slash) > 80
    for m in ("/전문가", "/전문가 낙하 해석", "/전문가 낙하 해석 근거 몇 건", "/에이전트 PCB", "/agents drop", "/agent", long_slash,
              "/전문가 낙하 추천해 줘. 그리고 카드 수도"):
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
    assert _route(monkeypatch, message=m) == [("목록", "낙하 해석")], "자연어는 찾는 말을 뺀 분야 낱말로 찾는다"
    assert _route(monkeypatch, message=m, pinned_agent="he-aidatahub-operator") == [("챗", m)]
    assert _route(monkeypatch, message=m, pinned_agents=["he-aidatahub-operator", "rel-drop-impact"]) == [("챗", m)], \
        "여러 명만 보낸 꼴(pinned_agents)도 고른 대화다 — _persona_keys 와 같다"
    assert _route(monkeypatch, message=DW13) == [("챗", DW13)]
    assert _route(monkeypatch, message="/전문가 he-agent-builder", pinned_agent="he-aidatahub-operator") == \
        [("목록", "he-agent-builder")], "빗금 뒤 글은 사람이 적은 그대로다"


def test_권한_판정도_같은_길을_본다():
    """_denied_feature 는 chat() 과 같은 순서로 가른다 — 고른 대화의 자연어는 목록이 아니니 Thinking 이면 Thinking 권한을 보고,
    /보고서 는 전문가 검색보다 먼저 저장으로 가니 결론 글에 '전문가 추천' 이 있어도 전문가 권한을 묻지 않는다."""
    m = "낙하 해석 전문가 추천해 줘"
    have = ["feat:chat", "feat:expert-chat"]
    assert a._denied_feature(a.ChatRequest(message=m, entitlements=["feat:chat"])) == "feat:expert-chat"
    assert a._denied_feature(a.ChatRequest(message=m, pinned_agent="x", thinking=True, entitlements=have)) == "feat:thinking"
    assert a._denied_feature(a.ChatRequest(message=m, pinned_agents=["x"], thinking=True, entitlements=have)) == "feat:thinking", \
        "여러 명만 보낸 꼴(pinned_agents)도 고른 대화다 — chat() 과 같다"
    assert a._denied_feature(a.ChatRequest(message=m, pinned_agent="x", entitlements=have)) is None
    assert a._denied_feature(a.ChatRequest(message=DW13, entitlements=["feat:chat"])) is None, "챗으로 가는 질문에 전문가 권한을 묻지 않는다"
    rep = "/보고서 낙하 전문가 추천 정리"
    assert a.is_report_save(rep) and a.is_agent_search(rep), "보고서 결론 글이 전문가 검색 꼴이기도 하다"
    assert a._denied_feature(a.ChatRequest(message=rep, entitlements=["feat:chat"])) is None


def test_추천_질의에서_찾는_말을_뺀다():
    q = a._agent_search_query
    assert q("낙하 해석 전문가 추천해 줘") == "낙하 해석"
    assert q("PCB 내부 크랙을 잘 아는 전문가 에이전트를 추천해 줘") == "PCB 내부 크랙을 잘 아는"
    assert q("낙하전문가 추천 부탁해요") == "낙하"
    assert q("왜곡 해석 전문가 찾아줘") == "왜곡 해석"
    assert q("reagent agentic 전문가 추천해 줘") == "reagent agentic", "영어 낱말 속 agent 는 그대로다"
    assert q("he-agent-builder 같은 에이전트 있어?") == "he-agent-builder 같은", "키 꼴의 agent 는 그대로다"
    assert q("전문가 뭐 있어?") == "" and q("어떤 전문가가 있는지 알려줘") == "" and q("") == "" and q("낙하") == "낙하"
    # 분야 낱말이 찾는 말로 시작하거나 그것을 품어도 남는다 — 찾는 말 앞은 끝에 붙은 전문가 · 에이전트와 '어떤' 만 뺀다
    assert q("검색 엔진 전문가 추천해 줘") == "검색 엔진"
    assert q("추천 시스템 전문가 알려줘") == "추천 시스템"
    assert q("체크리스트 관리 전문가 찾아줘") == "체크리스트 관리"
    assert q("데이터 애널리스트 전문가 추천해 줘") == "데이터 애널리스트"
    assert q("AI 에이전트 개발 전문가 추천해 줘") == "AI 에이전트 개발"
    assert q("어떤 낙하 전문가가 있어?") == "낙하"
    # 찾는 말 뒤는 활용 · 부탁 낱말만 빼고 덧붙인 분야 낱말은 남긴다
    assert q("PCB 크랙 전문가 있으면 알려줘") == "PCB 크랙"
    assert q("낙하 해석 전문가 추천해 주시면 감사하겠습니다") == "낙하 해석"
    assert q("낙하 해석 전문가를 추천해 주실 수 있나요?") == "낙하 해석"
    assert q("전문가 추천해 줘 낙하 해석 쪽으로") == "낙하 해석 쪽으로"
    assert q("낙하 전문가 찾고 싶어") == "낙하"


def test_빗금_질의는_사람이_적은_그대로다():
    s = a.strip_agent_trigger
    assert s("/전문가 he-agent-builder") == "he-agent-builder"
    assert s("/전문가 검색 엔진") == "검색 엔진"
    assert s("/에이전트 AI 에이전트 개발") == "AI 에이전트 개발"
    assert s("/전문가 ODB 체크리스트") == "ODB 체크리스트"
    assert s("/agents drop") == "drop" and s("/전문가") == ""
    assert s("/전문가 낙하 해석 전문가 추천해 줘") == "낙하 해석 전문가 추천해 줘", "빗금 뒤 글은 찾는 말이 들어 있어도 그대로다"
    assert s("낙하 해석 전문가 추천해 줘") == "낙하 해석", "자연어는 찾는 말을 뺀 분야 낱말이다"


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
        # chat() 과 같은 꼴로 부른다 — 질의는 strip_agent_trigger 가 만든다
        return b"".join([c async for c in a.run_agent_search(a.app, a.strip_agent_trigger(text), [])]).decode("utf-8")

    out = asyncio.run(run("낙하 해석 전문가 추천해 줘"))
    assert [c for c in calls if c[0] == "recommend_agents"] == [("recommend_agents", {"q": "낙하 해석", "top_k": 10})]
    assert '"query": "낙하 해석"' in out and "'낙하 해석' 관련 추천 1명" in out
    calls.clear()
    asyncio.run(run("/전문가 he-agent-builder"))
    assert [c for c in calls if c[0] == "recommend_agents"] == [("recommend_agents", {"q": "he-agent-builder", "top_k": 10})]
    calls.clear()
    out = asyncio.run(run("전문가 뭐 있어?"))
    assert [c[0] for c in calls] == ["list_agents"], "분야 낱말이 없으면 요청 낱말로 추천하지 않는다"
    assert "전문가 1명이 등록돼 있습니다" in out and "관련 추천" not in out
