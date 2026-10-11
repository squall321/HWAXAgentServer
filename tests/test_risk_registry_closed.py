# 리스크 심사 좌석에게 같은 과제의 앞 패널 판정이 닿지 않는다 — 사용자 결정 D-18 의 지정 도구 길과 RA 보고서 검색 길
#
# 자유 조회 후보는 tests/test_delib_seats.py 의 등록부 가드가 본다. 이 파일은 그 밖의 길이다.
# (1) 호출자가 고르는 지정 도구(delib_opts.tools) — 엔진이 그 도구를 주제 기반 인자로 불러 결과를 **전 좌석 프롬프트에**
#     싣는다. 리스크 심사에 risk_get_registry 를 지정하면 좌석이 앞 패널 판정을 읽고 같은 말을 해, 원장(D-23)의 지지 수가
#     독립 근거 없이 오른다.
# (2) 엔진이 패널마다 Report Archive 에 남기는 심의 보고서 — 러너가 save_report 를 보내지 않아 패널마다 결정문(판정)이 든
#     초안이 생기고, 같은 잡 자격으로 도는 다음 패널 좌석의 search_reports(리스크 심사 유지 도구)에 걸린다(RA 는 소유한
#     초안도 검색 범위에 넣고 저장 때 본문 색인을 채운다). engine-contract A-D18-ra-reports.
# (3) 엔진이 직접 부르는 보고서 검색(지정 도구 · 정량 근거 선주입) — 결과가 전 좌석 프롬프트에 실린다.
# (4) 앞 패널 기록 도구(패널 목록 · 패널 전사)를 리스크 심사에 지정한 길.
# (5) 공백·한글이 든 과제 코드 — 질문 머리를 읽지 못하면 (2)·(3) 의 거름이 알림 없이 꺼졌다. 못 읽으면 알린다.
# (6) apps 없이 손수 돈 리스크 심사 — 좌석이 RA 보고서 읽기 도구로 앞 패널 초안을 읽었다. 이름으로 뺀다.
#
#   실행:  .venv/bin/python -m pytest tests/test_risk_registry_closed.py -q
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402

from test_delib_silent_drops import _cards, _pin_context, _steps, _stream, _Tool  # noqa: E402, F401

_REG = '{"target_key": "T-1", "claims": [{"ref": "hinge", "verdict": "FAIL", "panel": 1}]}'


def _after_tools(ev, data):
    """지정 도구 단계가 끝난 뒤(좌석 발굴 문턱)에서 멈춘다 — 거기까지는 좌석 LLM 이 필요 없다."""
    return ev == "delib" and data.get("kind") == "stage" and data.get("stage") == "discover"


def _run(monkeypatch, chair):
    planned = []

    async def _planner(_llm, _system, human):
        planned.append(human)
        return '{"target_key": "T-1"}'

    monkeypatch.setattr(d, "_llm_text", _planner)
    tools = {n: _Tool(n, _REG) for n in d._RISK_REGISTRY_TOOLS}
    tools["agent_search"] = _Tool("agent_search")
    events = _stream(monkeypatch, {"chair_template": chair, "tools": list(d._RISK_REGISTRY_TOOLS)},
                     tools=tools, until=_after_tools)
    return events, tools, planned


def test_리스크_심사는_지정한_등록부_도구를_부르지_않고_까닭을_남긴다(monkeypatch):
    events, tools, planned = _run(monkeypatch, "risk-review")
    assert all(not tools[n].calls for n in d._RISK_REGISTRY_TOOLS), {n: tools[n].calls for n in tools}
    assert not planned, "거절할 도구의 인자를 구성했다"
    assert not [c for c in _cards(events, included=True) if "risk_" in c.get("source", "")], "등록부 결과가 좌석에 갔다"
    card = next(c for c in _cards(events, included=False) if c["source"] == "지정 도구 미실행")
    assert "risk_get_registry" in card["text"] and "risk_claims_for_ref" in card["text"] and "D-18" in card["text"], card
    assert any("지정 도구 거절: risk_get_registry" in s for s in _steps(events)), _steps(events)


@pytest.mark.parametrize("chair", ("default", "diagnosis"))
def test_리스크_심사가_아니면_지정한_등록부_도구를_그대로_부른다(monkeypatch, chair):
    """D-18 은 리스크 심사 좌석의 결정이다 — 다른 심의에서 호출자가 직접 고른 도구는 종전대로 돈다."""
    events, tools, _planned = _run(monkeypatch, chair)
    assert all(tools[n].calls for n in d._RISK_REGISTRY_TOOLS), {n: tools[n].calls for n in tools}
    assert not [c for c in _cards(events, included=False) if c["source"] == "지정 도구 미실행"]


# ── (2) RA 보고서 검색 — 같은 과제의 앞 패널 심의 보고서를 리스크 심사 좌석의 검색 결과에서 뺀다 ─────────────────────
# 질문 머리는 리스크 앱 runner.panel_question 의 형식이다(`[리스크심사 <과제코드> <target_key>] …`).
_Q_NOW = "[리스크심사 P1 T-new] 기준 P0 대비 변경이 각 도메인에서 어떤 리스크와 개선을 낳는가를 도구 근거로 판정하라."
_Q_PREV = "[리스크심사 P1 T-old] 현황이 각 도메인에서 어떤 리스크와 개선을 낳는가를 도구 근거로 판정하라."
_Q_OTHER = "[리스크심사 P12 T-x] 현황이 각 도메인에서 어떤 리스크와 개선을 낳는가를 도구 근거로 판정하라."


def _ra_hits():
    """RA search_reports 의 응답 모양({reports:[{report_id,title,snippet,boards}], count, limit}). 제목은 엔진이 앞 패널
    보고서에 실제로 다는 것(생산자 함수)이다."""
    return {"reports": [
        {"report_id": 7, "title": d._ra_report_title(_Q_PREV), "snippet": "힌지 FAIL — 판정 근거 …", "boards": []},
        {"report_id": 8, "title": d._ra_report_title(_Q_OTHER), "snippet": "다른 과제의 판정", "boards": []},
        {"report_id": 9, "title": "힌지 낙하 시험 보고서", "snippet": "1.5m 낙하 …", "boards": []},
    ], "count": 3, "limit": 8}


class _CoTool:
    """자유 조회 준비가 감싸는 모양(coroutine) — 받은 인자를 적고 정해 둔 답을 준다."""

    def __init__(self, name, answer):
        self.name, self.description, self.args_schema, self.calls = name, "", {}, []
        self._answer = answer

        async def _co(*_a, **kw):
            self.calls.append(kw)
            return self._answer
        self.coroutine = _co

    async def ainvoke(self, args):
        return await self.coroutine(**args)


def _seat_given(monkeypatch, chair, question, answer, apps=("heax-step_forge",), extra=None):
    """심의를 좌석 발굴 문턱까지 실제로 돌려, 자유 조회 준비가 좌석 에이전트에게 준 도구들({이름: 도구})을 받는다. apps 기본값은
    러너처럼 어댑터 앱이다(RA 도구는 리스크 심사 유지 도구로만 남는다). extra 는 {도구 이름: 소속 앱} — 게이트웨이에 더 있는 도구."""
    import langgraph.prebuilt

    given = []

    def _fake_agent(_llm, tools, **_k):
        given.append({t.name: t for t in tools})
        return SimpleNamespace()

    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", _fake_agent)
    extra = extra or {}
    monkeypatch.setattr(d, "_app_of_tools", lambda: {"search_reports": "reportarchive", **extra})
    tools = {"agent_search": _Tool("agent_search"), "search_reports": _CoTool("search_reports", answer),
             **{n: _CoTool(n, "{}") for n in extra}}

    async def _fake_tools(*_a, **_k):
        return tools

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    opts = {"personas": [{"key": "mech-a", "role": "기구"}], "chair_template": chair, "free_tools": 1,
            "voc": "off", "rescreen": 0, "persona_knowledge": 0, "apps": list(apps)}

    async def go():
        gen = d.run_deliberation(SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None)), question, [], opts)
        try:
            async for chunk in gen:
                ev, data = delib_jobs._parse_sse(chunk)
                assert ev != "error", data
                if _after_tools(ev, data):
                    break
        finally:
            await gen.aclose()

    asyncio.run(go())
    assert given and "search_reports" in given[0], "시험 전제 — 좌석 자유 조회 후보에 search_reports 가 없다"
    return given[0]


def _seat_pool(monkeypatch, chair, question, answer, apps=("heax-step_forge",)):
    """좌석 에이전트에게 간 search_reports(_seat_given)."""
    return _seat_given(monkeypatch, chair, question, answer, apps)["search_reports"]


def _search(tool):
    out = asyncio.run(tool.coroutine(query="힌지 리스크"))
    return json.loads(out) if isinstance(out, str) else out


def test_리스크_심사_좌석의_보고서_검색은_같은_과제의_앞_패널_보고서를_뺀다(monkeypatch):
    got = _search(_seat_pool(monkeypatch, "risk-review", _Q_NOW, json.dumps(_ra_hits(), ensure_ascii=False)))
    assert [r["report_id"] for r in got["reports"]] == [8, 9], got
    assert got["count"] == 2 and "1건" in got.get("withheld", ""), got


def test_유니코드_이스케이프_응답에서도_뺀다(monkeypatch):
    """게이트웨이·MCP 직렬화가 한글을 \\uXXXX 로 보내도 제목을 풀어서 본다 — 글자열 대조만 하면 그대로 샌다."""
    got = _search(_seat_pool(monkeypatch, "risk-review", _Q_NOW, json.dumps(_ra_hits(), ensure_ascii=True)))
    assert [r["report_id"] for r in got["reports"]] == [8, 9], got


def test_읽지_못한_응답에_같은_과제의_보고서가_보이면_통째로_막는다(monkeypatch):
    raw = json.dumps(_ra_hits(), ensure_ascii=False)[:-40]      # 잘린 JSON — 행을 가려낼 수 없다
    out = asyncio.run(_seat_pool(monkeypatch, "risk-review", _Q_NOW, raw).coroutine(query="힌지"))
    assert "[리스크심사 P1 " not in out and "같은 과제" in out, out


@pytest.mark.parametrize("chair,question", [("default", _Q_NOW), ("risk-review", "힌지 크랙 원인")])
def test_리스크_심사가_아니거나_과제를_모르면_종전대로_준다(monkeypatch, chair, question):
    """머리 없는 자유 질문은 같은 과제를 가릴 수 없다 — 그대로 준다(남는 길은 열린 문제로 적었다)."""
    got = _search(_seat_pool(monkeypatch, chair, question, json.dumps(_ra_hits(), ensure_ascii=False), apps=()))
    assert [r["report_id"] for r in got["reports"]] == [7, 8, 9], got


def test_엔진이_남기는_보고서_제목에_과제_머리가_실린다():
    """거르는 열쇠가 이 제목이다 — 제목 앞 50자 안에 '[리스크심사 <과제코드> ' 가 들어야 한다."""
    assert d._ra_report_title(_Q_PREV).startswith("심의 — [리스크심사 P1 ")


# ── (3) 엔진 자신이 부르는 보고서 검색 — 지정 도구·정량 근거 선주입도 같은 과제의 앞 패널 보고서를 뺀다 ─────────────────
# 자유 조회 후보만 거르면 엔진이 직접 부르는 두 길이 남는다. 둘 다 결과를 **전 좌석 프롬프트에** 싣는다 — 호출자가
# search_reports 를 지정 도구로 고른 리스크 심사, 그리고 정량 근거 선주입(evidence_prepass — 박스 설정이나 MCP 인자로 켠다)이
# 질문 그대로 보고서를 찾는 것. 리스크 심사의 질문 머리에는 과제 코드가 실려 앞 패널 보고서 제목과 바로 겹친다.
_PRIOR_SNIPPET = "힌지 FAIL — 판정 근거"


def _engine_search(monkeypatch, chair, req):
    """심의를 좌석 발굴 문턱까지 돌려, 엔진 LLM 이 받은 글(인자 계획·근거 증류)과 이벤트를 받는다."""
    seen = []

    async def _llm(_llm, _system, human):
        seen.append(human)
        return '{"q": "힌지 리스크", "limit": 3}'

    monkeypatch.setattr(d, "_llm_text", _llm)
    ra = _CoTool("search_reports", json.dumps(_ra_hits(), ensure_ascii=False))
    tools = {"agent_search": _Tool("agent_search"), "search_reports": ra}

    async def _fake_tools(*_a, **_k):
        return tools

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    opts = {"personas": [{"key": "mech-a", "role": "기구"}], "chair_template": chair, "free_tools": 0,
            "voc": "off", "rescreen": 0, "persona_knowledge": 0, **req}

    async def go():
        out = []
        gen = d.run_deliberation(SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None)), _Q_NOW, [], opts)
        try:
            async for chunk in gen:
                ev, data = delib_jobs._parse_sse(chunk)
                assert ev != "error", data
                out.append((ev, data))
                if _after_tools(ev, data):
                    break
        finally:
            await gen.aclose()
        return out

    events = asyncio.run(go())
    assert ra.calls, "시험 전제 — 엔진이 search_reports 를 부르지 않았다"
    return events, seen


def test_리스크_심사의_지정_도구_보고서_검색은_같은_과제의_앞_패널_보고서를_뺀다(monkeypatch):
    events, _seen = _engine_search(monkeypatch, "risk-review", {"tools": ["search_reports"]})
    card = next(c for c in _cards(events, included=True) if c["source"] == "지정 도구 search_reports")
    assert _PRIOR_SNIPPET not in card["text"] and "1.5m 낙하" in card["text"], card["text"]
    assert "withheld" in card["text"], card["text"]


def test_리스크_심사의_정량_근거_선주입은_같은_과제의_앞_패널_보고서를_보지_않는다(monkeypatch):
    _events, seen = _engine_search(monkeypatch, "risk-review", {"evidence_prepass": 1})
    distill = [h for h in seen if "[도구 검색 결과]" in h]
    assert distill and all(_PRIOR_SNIPPET not in h for h in distill), distill
    assert "1.5m 낙하" in distill[0], distill[0][:400]


def test_리스크_심사가_아니면_지정_도구_보고서_검색은_종전대로다(monkeypatch):
    events, _seen = _engine_search(monkeypatch, "default", {"tools": ["search_reports"]})
    card = next(c for c in _cards(events, included=True) if c["source"] == "지정 도구 search_reports")
    assert _PRIOR_SNIPPET in card["text"], card["text"]


# ── (4) 앞 패널 기록 도구 — 패널 목록·패널 전사도 리스크 심사의 지정 도구로 부르지 않는다 ────────────────────────────────
# risk_get_panel_transcript 는 패널 한 건의 결정문·risk_spec 이고 risk_list_panels 는 그 id 의 출처다(HWAXRisk mcp_server).
# 등록부 둘만 거절하면 이 둘을 지정한 리스크 심사의 좌석이 앞 패널 판정을 통째로 읽는다(D-18 — 여는 것은 변경·스냅샷뿐).
_PRIOR = ("risk_list_panels", "risk_get_panel_transcript")


def _run_prior(monkeypatch, chair):
    async def _planner(_llm, _system, _human):
        return '{"target_key": "T-1", "panel_id": "p-1"}'

    monkeypatch.setattr(d, "_llm_text", _planner)
    tools = {n: _Tool(n, '{"decision_text": "힌지 FAIL", "risk_spec": {"verdict": "FAIL"}}') for n in _PRIOR}
    tools["agent_search"] = _Tool("agent_search")
    events = _stream(monkeypatch, {"chair_template": chair, "tools": list(_PRIOR)}, tools=tools, until=_after_tools)
    return events, tools


def test_리스크_심사는_지정한_앞_패널_기록_도구를_부르지_않는다(monkeypatch):
    events, tools = _run_prior(monkeypatch, "risk-review")
    assert all(not tools[n].calls for n in _PRIOR), {n: tools[n].calls for n in _PRIOR}
    card = next(c for c in _cards(events, included=False) if c["source"] == "지정 도구 미실행")
    assert all(n in card["text"] for n in _PRIOR) and "D-18" in card["text"], card


def test_리스크_심사가_아니면_앞_패널_기록_도구를_그대로_부른다(monkeypatch):
    _events, tools = _run_prior(monkeypatch, "default")
    assert all(tools[n].calls for n in _PRIOR), {n: tools[n].calls for n in _PRIOR}


# ── (5) 과제 코드는 자유 형식이다 — 공백·한글이 든 코드에서도 같은 과제의 앞 패널 보고서를 뺀다 ──────────────────────────
# 리스크 앱은 과제 코드에 형식 검사를 하지 않는다(HWAXRisk routes.ProjectBody — code: str, max_length=40 · 화면은 trim 만).
# 러너 질문 머리는 '[리스크심사 <과제코드> <target_key>]' 이고 target_key 는 '<kind>:<ref_id>' 라 공백이 없다. 종전 머리 식
# r"\[리스크심사 (\S+) \S+\]" 은 'S26 Ultra' 같은 코드의 머리를 읽지 못해, 거름이 카드도 경고도 없이 꺼졌다 — 다음 패널
# 좌석의 보고서 검색에 앞 패널 결정문이 든 초안이 그대로 실렸다(m1 2차 AF2-D18-code-space).
def _hits_for(code):
    prev = f"[리스크심사 {code} snap:old] 현황이 각 도메인에서 어떤 리스크와 개선을 낳는가를 도구 근거로 판정하라."
    return {"reports": [
        {"report_id": 7, "title": d._ra_report_title(prev), "snippet": "힌지 FAIL — 판정 근거 …", "boards": []},
        {"report_id": 8, "title": d._ra_report_title(_Q_OTHER), "snippet": "다른 과제의 판정", "boards": []},
        {"report_id": 9, "title": "힌지 낙하 시험 보고서", "snippet": "1.5m 낙하 …", "boards": []},
    ], "count": 3, "limit": 8}


@pytest.mark.parametrize("code", ("S26 Ultra", "힌지 개선 2차"))
def test_공백_한글이_든_과제_코드도_같은_과제의_앞_패널_보고서를_뺀다(monkeypatch, code):
    q = f"[리스크심사 {code} diff:new] 기준 P0 대비 변경이 각 도메인에서 어떤 리스크와 개선을 낳는가를 도구 근거로 판정하라."
    got = _search(_seat_pool(monkeypatch, "risk-review", q, json.dumps(_hits_for(code), ensure_ascii=False)))
    assert [r["report_id"] for r in got["reports"]] == [8, 9], got
    assert "1건" in got.get("withheld", "") and code in got["withheld"], got


@pytest.mark.parametrize("code", ("S26 Ultra", "힌지 개선 2차", "P1", "-"))
def test_질문_머리는_마지막_토막을_target_key_로_읽는다(code):
    assert d._risk_q_code(f"[리스크심사 {code} diff:3f2a] 기준 대비 변경이 …") == code


def _events_for(monkeypatch, question, chair="risk-review"):
    """심의를 좌석 발굴 문턱까지 돌려 이벤트를 받는다(도구는 agent_search 하나 — 거름이 걸릴 자리와 상관없이 알림을 본다)."""
    async def _fake_tools(*_a, **_k):
        return {"agent_search": _Tool("agent_search")}

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    opts = {"personas": [{"key": "mech-a", "role": "기구"}], "chair_template": chair, "free_tools": 0,
            "voc": "off", "rescreen": 0, "persona_knowledge": 0}

    async def go():
        out = []
        gen = d.run_deliberation(SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None)), question, [], opts)
        try:
            async for chunk in gen:
                ev, data = delib_jobs._parse_sse(chunk)
                assert ev != "error", data
                out.append((ev, data))
                if _after_tools(ev, data):
                    break
        finally:
            await gen.aclose()
        return out

    return asyncio.run(go())


def _head_notices(events):
    warn = [data for ev, data in events if ev == "warning" and data.get("code") == "risk_head_unread"]
    cards = [c for c in _cards(events, included=False) if c.get("source") == "앞 패널 보고서 거름 꺼짐"]
    return warn, cards


def test_리스크심사_머리를_읽지_못하면_조용히_넘기지_않고_알린다(monkeypatch):
    """질문에 '[리스크심사' 가 있는데 과제 코드를 못 읽으면 같은 과제를 가릴 수 없다 — 거름이 꺼진 채 도는 것을 경고와 카드로
    남긴다(카드는 좌석에 주지 않은 것이라 러너가 패널에 옮겨 적는다)."""
    warn, cards = _head_notices(_events_for(monkeypatch, "[리스크심사 S26U] 힌지 크랙이 각 도메인에서 어떤 리스크를 낳는가"))
    assert len(warn) == 1 and len(cards) == 1, (warn, cards)
    assert "D-18" in cards[0]["text"] and "[리스크심사 S26U]" in cards[0]["text"], cards[0]


@pytest.mark.parametrize("chair,question", [
    ("risk-review", "[리스크심사 S26 Ultra diff:new] 기준 대비 변경이 …"),     # 읽힌다
    ("risk-review", "힌지 크랙 원인"),                                       # 머리가 없는 손수 질문 — 알릴 머리가 없다
    ("default", "[리스크심사 S26U] 힌지 크랙"),                               # 리스크 심사가 아니다
])
def test_머리를_읽었거나_머리가_없거나_리스크_심사가_아니면_알리지_않는다(monkeypatch, chair, question):
    warn, cards = _head_notices(_events_for(monkeypatch, question, chair))
    assert not warn and not cards, (warn, cards)


# ── (6) apps 없이 손수 돈 리스크 심사 — RA 보고서 읽기 도구를 이름으로 뺀다 ───────────────────────────────────────────
# 실사용 팀은 리스크 앱을 거치지 않고 Claude 에서 deliberate_start(job='risk-review')를 apps 없이 부른다(HWAXPortal
# docs/risk-review-redesign D-20·D-21). 앱 제한(_narrow)은 apps 가 있을 때만 걸려, 이 길의 좌석은 접두사 화이트리스트 전체를
# 받았다 — RA 의 get_report·list_my_reports·get_reports_digest 가 열려 잡 주인이 남긴 앞 패널 '심의 — …' 초안을 목록에서 보고
# 본문을 읽었다. 러너 패널(apps = 어댑터 앱)의 좌석은 RA 를 유지 도구(search_reports 등)로만 본다 — 같은 시야로 맞춘다.
# 이름은 RA mcp_server/server.py 의 search·collab 묶음에서 보고서 본문·제목·요약을 내주는 읽기 도구다(2026-10-11 대조).
_RA_READERS = ("get_report", "get_report_outline", "get_reports_digest", "list_my_reports", "list_reports",
               "list_related_reports", "list_comments", "get_composite", "list_composites")
_OTHER_APP = {"describe_part": "heax-step_forge"}      # 리스크 심사와 무관한 앱의 읽기 도구 — 그대로 남아야 한다


def test_apps_없는_리스크_심사의_좌석은_RA_보고서_읽기_도구를_받지_않는다(monkeypatch):
    extra = {**{n: "reportarchive" for n in _RA_READERS}, **_OTHER_APP}
    given = _seat_given(monkeypatch, "risk-review", "힌지 크랙이 각 도메인에서 어떤 리스크를 낳는가",
                        json.dumps(_ra_hits(), ensure_ascii=False), apps=(), extra=extra)
    assert not [n for n in _RA_READERS if n in given], sorted(given)
    # 좁히지 않는다 — 앞 패널 판정에 닿는 이름만 뺀다(리스크 심사 유지 도구와 다른 앱의 읽기 도구는 그대로다).
    assert "search_reports" in given and "describe_part" in given and "agent_search" in given, sorted(given)


@pytest.mark.parametrize("chair", ("default", "diagnosis"))
def test_리스크_심사가_아니면_RA_보고서_읽기_도구를_종전대로_받는다(monkeypatch, chair):
    extra = {n: "reportarchive" for n in _RA_READERS}
    given = _seat_given(monkeypatch, chair, "힌지 크랙 원인", json.dumps(_ra_hits(), ensure_ascii=False),
                        apps=(), extra=extra)
    assert all(n in given for n in _RA_READERS), sorted(given)
