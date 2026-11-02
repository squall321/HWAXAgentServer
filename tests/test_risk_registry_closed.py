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
# (7) 질문 머리 없이(또는 머리를 읽지 못한 채) 돈 리스크 심사 — 같은 과제를 가릴 수 없어 search_reports 거름이 없었다. 엔진이
#     남긴 심의 보고서('심의 — …')를 전부 빼고 알린다.
# (8) 지정 도구 — 리스크 앱은 변경 · 스냅샷만 열고, 그 밖의 리스크 앱 도구 · RA 보고서 읽기 · 범용 실행기를 부르지 않는다(이름 꼴 셋).
# (9) RA 온톨로지 읽기 — 같은 과제의 리스크 판정 객체(risk_finding)를 뺀다.
# (10) 포털 대화 검색 · 심의 잡 기록 — 같은 과제의 앞 패널 대화를 빼고, 잡 기록 읽기는 지정 도구로 부르지 않는다.
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


@pytest.mark.parametrize("chair,question", [("default", _Q_NOW), ("diagnosis", "힌지 크랙 원인")])
def test_리스크_심사가_아니면_종전대로_준다(monkeypatch, chair, question):
    """D-18 은 리스크 심사 좌석의 결정이다 — 다른 심의의 보고서 검색은 머리가 있든 없든 거르지 않는다. 머리 없는 리스크 심사는
    (7) 이 본다."""
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


def _engine_search(monkeypatch, chair, req, question=_Q_NOW):
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


_HEAD_CODES = ("risk_head_unread", "risk_head_missing")


def _head_notices(events, code="risk_head_unread"):
    """과제를 가릴 수 없다는 알림 — 경고(code)와 좌석에 주지 않은 카드('앞 패널 보고서 거름 …')."""
    warn = [data for ev, data in events if ev == "warning" and data.get("code") == code]
    cards = [c for c in _cards(events, included=False) if str(c.get("source", "")).startswith("앞 패널 보고서 거름")]
    return warn, cards


def test_리스크심사_머리를_읽지_못하면_조용히_넘기지_않고_알린다(monkeypatch):
    """질문에 '[리스크심사' 가 있는데 과제 코드를 못 읽으면 같은 과제를 가릴 수 없다 — 심의 보고서를 전부 빼고 그것을 경고와
    카드로 남긴다(카드는 좌석에 주지 않은 것이라 러너가 패널에 옮겨 적는다)."""
    warn, cards = _head_notices(_events_for(monkeypatch, "[리스크심사 S26U] 힌지 크랙이 각 도메인에서 어떤 리스크를 낳는가"))
    assert len(warn) == 1 and len(cards) == 1, (warn, cards)
    assert "D-18" in cards[0]["text"] and "[리스크심사 S26U]" in cards[0]["text"], cards[0]
    # 거름이 꺼진 것이 아니라 넓어졌다 — 알림이 그렇게 말해야 한다(종전 문구는 '빼지 못했다' 였다).
    assert "전부" in cards[0]["source"] and "전부" in warn[0]["message"], (cards[0], warn[0])
    assert "빼지 못" not in cards[0]["text"] and "빼지 못" not in warn[0]["message"], (cards[0], warn[0])


@pytest.mark.parametrize("chair,question", [
    ("risk-review", "[리스크심사 S26 Ultra diff:new] 기준 대비 변경이 …"),     # 읽힌다
    ("default", "[리스크심사 S26U] 힌지 크랙"),                               # 리스크 심사가 아니다
    ("default", "힌지 크랙 원인"),                                           # 리스크 심사가 아니다(머리도 없다)
])
def test_머리를_읽었거나_리스크_심사가_아니면_알리지_않는다(monkeypatch, chair, question):
    events = _events_for(monkeypatch, question, chair)
    for code in _HEAD_CODES:
        warn, cards = _head_notices(events, code)
        assert not warn and not cards, (code, warn, cards)


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


# ── (7) 머리 없는 손수 리스크 심사 — 같은 과제를 가릴 수 없으니 엔진이 남긴 심의 보고서를 전부 뺀다 ────────────────────────────
# 실사용 팀은 apps 도 질문 머리('[리스크심사 …]')도 없이 deliberate_start(job='risk-review')를 부른다. 이 길에서 거름은 과제
# 코드를 못 읽어 감싸지 않았고, 좌석의 search_reports 가 잡 주인의 앞 패널 초안('심의 — …' — 엔진의 _ra_report_title 이 붙이는
# 머리)을 스니펫과 함께 돌려줬다(m1 3차 발견 5 — 사용자 결정 D-18). 머리를 읽지 못한 길도 같다. 같은 과제를 가릴 수 없으니
# 엔진이 남긴 심의 보고서를 전부 뺀다 — 다른 과제의 선례도 빠지지만, 앞 패널 판정을 따라 말하는 쪽보다 낫다.
_Q_MANUAL = "힌지 크랙이 각 도메인에서 어떤 리스크와 개선을 낳는가를 도구 근거로 판정하라."
_Q_UNREAD = "[리스크심사 S26U] 힌지 크랙이 각 도메인에서 어떤 리스크를 낳는가"


@pytest.mark.parametrize("ascii_", (False, True), ids=("utf8", "escaped"))
@pytest.mark.parametrize("question", (_Q_MANUAL, _Q_UNREAD), ids=("headless", "unread"))
def test_과제를_모르는_리스크_심사_좌석의_보고서_검색은_심의_보고서를_전부_뺀다(monkeypatch, question, ascii_):
    got = _search(_seat_pool(monkeypatch, "risk-review", question, json.dumps(_ra_hits(), ensure_ascii=ascii_), apps=()))
    assert [r["report_id"] for r in got["reports"]] == [9], got
    assert got["count"] == 1 and "2건" in got.get("withheld", "") and "D-18" not in got["withheld"], got
    assert "가릴 수 없어" in got["withheld"], got


def test_머리_없는_리스크_심사도_읽지_못한_응답에_심의_보고서가_보이면_통째로_막는다(monkeypatch):
    raw = json.dumps(_ra_hits(), ensure_ascii=False)[:-40]      # 잘린 JSON — 행을 가려낼 수 없다
    out = asyncio.run(_seat_pool(monkeypatch, "risk-review", _Q_MANUAL, raw, apps=()).coroutine(query="힌지"))
    assert _PRIOR_SNIPPET not in out and "가려낼 수 없다" in out, out


def test_머리_없는_리스크_심사의_지정_도구_보고서_검색도_심의_보고서를_뺀다(monkeypatch):
    """엔진이 직접 부르는 길(지정 도구) — 결과가 전 좌석 프롬프트에 실린다."""
    events, _seen = _engine_search(monkeypatch, "risk-review", {"tools": ["search_reports"]}, question=_Q_MANUAL)
    card = next(c for c in _cards(events, included=True) if c["source"] == "지정 도구 search_reports")
    assert _PRIOR_SNIPPET not in card["text"] and "다른 과제의 판정" not in card["text"], card["text"]
    assert "1.5m 낙하" in card["text"], card["text"]


def test_머리_없는_리스크_심사는_심의_보고서를_전부_뺐다고_경고와_카드로_알린다(monkeypatch):
    warn, cards = _head_notices(_events_for(monkeypatch, _Q_MANUAL), "risk_head_missing")
    assert len(warn) == 1 and len(cards) == 1, (warn, cards)
    assert cards[0]["source"] == "앞 패널 보고서 거름 — 머리 없는 손수 길이라 심의 초안 전부를 뺐다", cards[0]
    assert "D-18" in cards[0]["text"] and "심의 — " in cards[0]["text"], cards[0]
    assert not _head_notices(_events_for(monkeypatch, _Q_MANUAL), "risk_head_unread")[0], "머리가 없는데 '못 읽었다' 고 했다"


def test_리스크_심사가_아니면_머리가_없어도_보고서_검색을_거르지_않는다(monkeypatch):
    events, _seen = _engine_search(monkeypatch, "default", {"tools": ["search_reports"]}, question=_Q_MANUAL)
    card = next(c for c in _cards(events, included=True) if c["source"] == "지정 도구 search_reports")
    assert _PRIOR_SNIPPET in card["text"], card["text"]



# ── (8) 지정 도구 — 리스크 앱에서 여는 것은 변경 · 스냅샷뿐이다. 그 밖의 리스크 앱 도구 · RA 보고서 읽기 · 범용 실행기도 부르지 않는다 ──
# 등록부 · 앞 패널 기록 넷만 거절하던 동안, 선례(risk_get_precedents — 이 대상 자신의 클러스터와 주장 · 등급 · 상태) · 종합 보고서 ·
# 쟁점 · 전문가 보고서 · 셀을 지정한 리스크 심사는 그 결과를 전 좌석 프롬프트에 실었다. RA 보고서 읽기는 자유 조회에서만 뺐다
# (D-18 누수 셋 가운데 하나, 2026-11-02). 이름은 게이트웨이가 싣는 꼴 그대로다 — 맨 이름과 겹침 접두(heaxhwax_risk_<도구> ·
# reportarchive_<도구>). 호출 별칭(목록에 없는 같은 꼴)은 invoke_tool 로만 닿는다.
# 리스크 앱 도구 33종 — HWAXRisk backend/app/mcp_server.py(d30bef6)와 dev 게이트웨이 /tools-map(2026-11-02)이 같다.
_RISK_APP_TOOLS = (
    "risk_ack_gate", "risk_add_finding", "risk_add_source", "risk_capture_snapshot", "risk_claims_for_ref", "risk_create_diff",
    "risk_create_project", "risk_decide_sameas", "risk_find_ecad_boards", "risk_get_brief", "risk_get_cells", "risk_get_coverage",
    "risk_get_diff", "risk_get_expert_report", "risk_get_issues", "risk_get_panel_transcript", "risk_get_precedents",
    "risk_get_registry", "risk_get_report", "risk_get_sameas", "risk_get_snapshot", "risk_get_units", "risk_job_action",
    "risk_list_panels", "risk_list_projects", "risk_next_step", "risk_open_target", "risk_repersist_panel", "risk_set_ecad_board",
    "risk_similar_projects", "risk_start_job", "risk_submit_panel_result", "risk_taxonomy")
_RISK_OPEN = ("risk_get_snapshot", "risk_get_diff")
_RISK_CLOSED = tuple(n for n in _RISK_APP_TOOLS if n not in _RISK_OPEN)
_RISK_MAP = {n: "heax-hwax_risk" for n in _RISK_APP_TOOLS}
# RA 보고서 읽기 — 자유 조회에서 빼던 아홉(위 (6))에 지정 도구 길에서만 닿던 넷(위임 답 · 집계 · 저장 검색 · 이름 판별)을 더한 것.
_RA_DESIGNATED = _RA_READERS + ("ask_ontology", "aggregate_reports", "run_saved_search", "resolve")


def _designate(monkeypatch, chair, names, amap=None):
    """names 를 지정 도구로 준 심의를 좌석 발굴 문턱까지 돌린다 — 지정 도구는 요청 하나에 6개까지라(_TOOLS_MAX) 그만큼씩 나눠 여러 번
    돌린다. 돌린 회차의 이벤트 목록과 도구마다 받은 호출을 돌려준다. amap 은 도구-앱 매핑이다 — 없으면 빈 매핑(게이트웨이에 못 물은
    것과 같다)."""
    async def _planner(_llm, _system, _human):
        return '{"target_key": "T-1", "diff_id": "d-1"}'

    monkeypatch.setattr(d, "_llm_text", _planner)
    monkeypatch.setattr(d, "_app_of_tools", lambda: dict(amap or {}))
    tools, runs = {n: _Tool(n, _REG) for n in names}, []
    for i in range(0, len(names), d._TOOLS_MAX):
        part = list(names[i:i + d._TOOLS_MAX])
        given = {n: tools[n] for n in part} | {"agent_search": _Tool("agent_search")}
        runs.append(_stream(monkeypatch, {"chair_template": chair, "tools": part}, tools=given, until=_after_tools))
    return runs, tools


def _refused(runs, names):
    """이름마다 거절 상태줄이 섰고, 그 회차의 미실행 카드가 그 이름과 D-18 을 적었는가. 거절한 도구의 결과 카드는 없다."""
    for n in names:
        events = next((ev for ev in runs if any(s.startswith(f"지정 도구 거절: {n} — ") for s in _steps(ev))), None)
        assert events is not None, (n, [_steps(ev) for ev in runs])
        card = next(c for c in _cards(events, included=False) if c["source"] == "지정 도구 미실행")
        assert n in card["text"] and "D-18" in card["text"], (n, card["text"])
        assert not [c for c in _cards(events, included=True) if c["source"] == f"지정 도구 {n}"], n


@pytest.mark.parametrize("amap", (_RISK_MAP, {}), ids=("mapped", "map-unavailable"))
def test_리스크_심사는_변경_스냅샷_밖의_리스크_앱_도구를_지정해도_부르지_않는다(monkeypatch, amap):
    """D-18 — 리스크 앱에서 좌석에게 여는 것은 변경 전체와 스냅샷 둘뿐이다. 매핑을 못 받아도(빈 매핑) 닫힌 쪽으로 간다. 다른 심의는
    같은 지정을 종전대로 부른다."""
    runs, tools = _designate(monkeypatch, "risk-review", _RISK_APP_TOOLS, amap)
    assert sorted(n for n in _RISK_APP_TOOLS if tools[n].calls) == sorted(_RISK_OPEN)
    _refused(runs, _RISK_CLOSED)
    _runs, tools = _designate(monkeypatch, "default", _RISK_APP_TOOLS, amap)
    assert all(tools[n].calls for n in _RISK_APP_TOOLS), sorted(n for n in _RISK_APP_TOOLS if not tools[n].calls)


def test_리스크_앱_도구는_겹침_접두_꼴로_와도_같다(monkeypatch):
    """게이트웨이가 이름 겹침으로 'heaxhwax_risk_<도구>' 로 실은 꼴 — 변경 · 스냅샷은 열고 나머지는 닫는다(매핑과 상관없다)."""
    names = tuple(f"heaxhwax_risk_{n}" for n in _RISK_APP_TOOLS)
    runs, tools = _designate(monkeypatch, "risk-review", names)
    assert sorted(n for n in names if tools[n].calls) == sorted(f"heaxhwax_risk_{n}" for n in _RISK_OPEN)
    _refused(runs, [f"heaxhwax_risk_{n}" for n in _RISK_CLOSED])


def test_다른_앱의_risk_이름은_매핑이_그_앱이라고_할_때만_부른다(monkeypatch):
    """StepForge 의 risk_screen 은 그 앱의 읽기 도구다(리스크 앱의 앞 판정이 아니다) — 매핑이 StepForge 라고 하면 부르고, 매핑을
    못 받았으면 리스크 앱 도구인지 가릴 수 없어 닫는다(틀릴 거면 닫힌 쪽으로)."""
    _runs, tools = _designate(monkeypatch, "risk-review", ("risk_screen",), {"risk_screen": "heax-step_forge"})
    assert tools["risk_screen"].calls
    runs, tools = _designate(monkeypatch, "risk-review", ("risk_screen",), {})
    assert not tools["risk_screen"].calls
    _refused(runs, ["risk_screen"])


def test_리스크_심사는_RA_보고서_읽기를_지정해도_부르지_않는다(monkeypatch):
    """RA 보고서 읽기는 자유 조회에서만 뺐다 — 지정 도구로 고르면 잡 주인이 남긴 앞 패널 '심의 — …' 초안이 전 좌석에 실렸다. 맨 이름과
    RA 의 겹침 접두 꼴(KooRemapper 와 함께 뜬 박스의 reportarchive_list_reports) 둘 다 닫는다. 다른 심의는 종전대로다."""
    names = _RA_DESIGNATED + tuple(f"reportarchive_{n}" for n in _RA_DESIGNATED)
    runs, tools = _designate(monkeypatch, "risk-review", names)
    assert not [n for n in names if tools[n].calls], sorted(n for n in names if tools[n].calls)
    _refused(runs, names)
    _runs, tools = _designate(monkeypatch, "default", names)
    assert all(tools[n].calls for n in names), sorted(n for n in names if not tools[n].calls)


def test_리스크_심사는_범용_실행기를_지정해도_부르지_않는다(monkeypatch):
    """invoke_tool 은 부를 도구를 호출할 때 정한다 — 목록에 없는 호출 별칭(heaxhwax_risk_risk_get_registry)으로도 등록부에 닿는다.
    리스크 앱의 변경 원장은 그대로 연다. 다른 심의는 종전대로다."""
    runs, tools = _designate(monkeypatch, "risk-review", ("invoke_tool", "risk_get_diff"))
    assert not tools["invoke_tool"].calls and tools["risk_get_diff"].calls
    _refused(runs, ["invoke_tool"])
    _runs, tools = _designate(monkeypatch, "default", ("invoke_tool",))
    assert tools["invoke_tool"].calls


# ── (9) RA 온톨로지 읽기 — 같은 과제의 리스크 판정 객체(risk_finding)를 리스크 심사 좌석에게 주지 않는다 ─────────────────────────
# 리스크 앱의 RA 송신(RK-01 — HWAXRisk ra_client._registry_object)이 켜지면 RA 에 등록부 클러스터마다 risk_finding 이 생긴다 — value 는
# '<과제 코드>@<메커니즘>.<세부>@<target_key>#<클러스터 키>' 이고 속성에 사람의 판정(status)이 실린다. 좌석은 온톨로지 읽기 셋
# (search_objects · get_object · get_subgraph — 리스크 심사 유지 도구)을 받으므로 같은 과제의 앞 판정을 읽을 수 있었다(D-18 누수 셋 가운데
# 하나, 2026-11-02). 응답 꼴은 RA mcp_server 그대로다 — search_objects · get_object 는 /api/ai/ontology/tool 의 data, get_subgraph 는
# /api/entities/{id}/graph 의 data. 다른 과제의 판정 객체는 선례라 남긴다. 'P1@' 와 'P12@' 는 다른 과제다.
_F_MINE = {"id": 501, "value": "P1@mech.hinge_crack@diff:new#a1b2c3d4e5f6", "type": "risk_finding",
           "properties": {"status": "confirmed", "cluster_key": "a1b2c3d4e5f6"}}
_F_MINE_OLD = {"id": 502, "value": "P1@sim.drop_peak@snap:old#0f0e0d0c0b0a", "type": "risk_finding",
               "properties": {"status": "dismissed"}}
_F_OTHER = {"id": 601, "value": "P12@mech.hinge_crack@diff:x#ffeeddccbbaa", "type": "risk_finding",
            "properties": {"status": "confirmed"}}
_F_RETRACTED = {"id": 701, "value": "비공개@diff:z#123456789abc", "type": "risk_finding", "properties": {"visibility": "private"}}
_INCIDENT = {"id": 301, "value": "INC-0412 힌지 크랙", "type": "incident", "properties": {}}


def _objs_hits(*rows):
    """search_objects 의 응답 모양 — content(items · total · shown · truncated)와 라우트가 덧붙이는 근거 객체(objects)."""
    return {"items": [dict(r) for r in rows], "total": len(rows) + 3, "shown": len(rows), "truncated": True,
            "objects": [{"type": r["type"], "id": str(r["id"]), "label": r["value"]} for r in rows]}


# get_object — 사고 객체의 관계에 판정 객체가 둘 걸려 있다(FK 관계 꼴 {id, value, type} 과 수동 연결 꼴 {type, id, label}).
_OBJ_INCIDENT = {"type": "incident", "id": "301", "label": "INC-0412 힌지 크랙", "kind": "record", "properties": {},
                 "reports": [{"report_id": 9, "title": "힌지 낙하 시험 보고서", "workspace_slug": "w"}],
                 "relations": [
                     {"relation": "verified_by", "direction": "in",
                      "object": {"id": 501, "value": _F_MINE["value"], "type": "risk_finding"}},
                     {"relation": "verified_by", "direction": "in",
                      "object": {"type": "risk_finding", "id": "601", "label": _F_OTHER["value"]}},
                     {"relation": "occurs_in", "direction": "out", "object": {"id": 41, "value": "힌지 브래킷", "type": "part"}}],
                 "objects": [{"type": "incident", "id": "301", "label": "INC-0412 힌지 크랙"}]}
_OBJ_MINE = {"type": "risk_finding", "id": "501", "label": _F_MINE["value"], "kind": "record",
             "properties": {"status": "confirmed"}, "reports": [],
             "relations": [{"relation": "verified_by", "direction": "out",
                            "object": {"id": 301, "value": "INC-0412 힌지 크랙", "type": "incident"}}],
             "objects": [{"type": "risk_finding", "id": "501", "label": _F_MINE["value"]}]}
# get_subgraph — 라우트 꼴(노드는 type_slug).
_GRAPH = {"nodes": [{"id": 301, "value": "INC-0412 힌지 크랙", "type_id": 3, "type_slug": "incident", "degree": 3},
                    {"id": 501, "value": _F_MINE["value"], "type_id": 9, "type_slug": "risk_finding", "degree": 1},
                    {"id": 601, "value": _F_OTHER["value"], "type_id": 9, "type_slug": "risk_finding", "degree": 1},
                    {"id": 41, "value": "힌지 브래킷", "type_id": 2, "type_slug": "part", "degree": 1}],
          "edges": [{"src": 501, "dst": 301, "relation": "verified_by"},
                    {"src": 601, "dst": 301, "relation": "verified_by"},
                    {"src": 301, "dst": 41, "relation": "occurs_in"}]}


def _onto_given(monkeypatch, chair, question, answers, apps=("heax-step_forge",), amap=None):
    """심의를 좌석 발굴 문턱까지 돌려 좌석 에이전트가 받은 도구({이름: 도구})를 돌려준다 — answers 는 {도구 이름: 정해 둔 답}, amap 은
    도구-앱 매핑(없으면 answers 를 모두 RA 도구로 본다)."""
    import langgraph.prebuilt

    given = []

    def _fake_agent(_llm, tools, **_k):
        given.append({t.name: t for t in tools})
        return SimpleNamespace()

    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", _fake_agent)
    monkeypatch.setattr(d, "_app_of_tools", lambda: dict({n: "reportarchive" for n in answers} if amap is None else amap))
    tools = {"agent_search": _Tool("agent_search"), **{n: _CoTool(n, a) for n, a in answers.items()}}

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
    assert given and all(n in given[0] for n in answers), f"시험 전제 — 좌석 자유 조회 후보에 {sorted(answers)} 가 없다"
    return given[0]


def _ask(tool, **args):
    """도구를 부른 답 — JSON 이면 풀고, 풀리지 않으면(거른 뒤의 알림 · 잘린 JSON) 글 그대로."""
    out = asyncio.run(tool.coroutine(**args))
    try:
        return json.loads(out)
    except (TypeError, ValueError):
        return out


def _ids(rows, key="id"):
    return [int(r[key]) for r in rows]


@pytest.mark.parametrize("apps", ((), ("heax-step_forge", "heax-kooremapper_mcp")), ids=("manual", "runner"))
def test_리스크_심사_좌석의_온톨로지_검색은_같은_과제의_판정_객체를_뺀다(monkeypatch, apps):
    hits = json.dumps(_objs_hits(_F_MINE, _F_MINE_OLD, _F_OTHER, _F_RETRACTED, _INCIDENT), ensure_ascii=False)
    got = _ask(_onto_given(monkeypatch, "risk-review", _Q_NOW, {"search_objects": hits}, apps)["search_objects"],
               type="risk_finding", q="힌지")
    assert _ids(got["items"]) == [601, 701, 301] and _ids(got["objects"]) == [601, 701, 301], got
    assert got["shown"] == 3 and got["total"] == 6, got
    assert "2건" in got["withheld"] and "P1" in got["withheld"] and "confirmed" not in json.dumps(got["items"][2:]), got
    # 다른 심의는 종전대로 준다(apps 없이 — 접두사로 열린다).
    raw = _ask(_onto_given(monkeypatch, "default", _Q_NOW, {"search_objects": hits}, ())["search_objects"], q="힌지")
    assert _ids(raw["items"]) == [501, 502, 601, 701, 301] and "withheld" not in raw, raw


def test_리스크_심사_좌석의_객체_보기는_같은_과제의_판정_객체를_가린다(monkeypatch):
    """관계의 상대가 같은 과제의 판정 객체면 그 관계를 뺀다(두 관계 꼴 모두) — 객체 자신이 그것이면 통째로 주지 않는다(그 둘레가 곧
    판정의 근거다). 다른 심의는 종전대로다."""
    answers = {"get_object": json.dumps(_OBJ_INCIDENT, ensure_ascii=False)}
    got = _ask(_onto_given(monkeypatch, "risk-review", _Q_NOW, answers)["get_object"], type="incident", id="301")
    assert [r["object"].get("value") or r["object"].get("label") for r in got["relations"]] == [_F_OTHER["value"], "힌지 브래킷"]
    assert got["reports"] == _OBJ_INCIDENT["reports"] and "1건" in got["withheld"], got
    answers = {"get_object": json.dumps(_OBJ_MINE, ensure_ascii=False)}
    got = _ask(_onto_given(monkeypatch, "risk-review", _Q_NOW, answers)["get_object"], type="risk_finding", id="501")
    assert set(got) == {"withheld"} and "P1" in got["withheld"], got
    raw = _ask(_onto_given(monkeypatch, "default", _Q_NOW, answers, ())["get_object"], type="risk_finding", id="501")
    assert raw == _OBJ_MINE, raw


def test_리스크_심사_좌석의_그래프는_같은_과제의_판정_노드와_엣지를_뺀다(monkeypatch):
    answers = {"get_subgraph": json.dumps(_GRAPH, ensure_ascii=False)}
    tool = _onto_given(monkeypatch, "risk-review", _Q_NOW, answers)["get_subgraph"]
    got = _ask(tool, entity_id=301)
    assert _ids(got["nodes"]) == [301, 601, 41], got
    assert got["edges"] == [{"src": 601, "dst": 301, "relation": "verified_by"}, {"src": 301, "dst": 41, "relation": "occurs_in"}]
    assert "1건" in got["withheld"], got
    seeded = _ask(tool, entity_id=501)                     # 씨앗이 같은 과제의 판정 객체다
    assert set(seeded) == {"withheld"}, seeded
    raw = _ask(_onto_given(monkeypatch, "default", _Q_NOW, answers, ())["get_subgraph"], entity_id=501)
    assert raw == _GRAPH, raw


@pytest.mark.parametrize("ascii_", (False, True), ids=("utf8", "escaped"))
@pytest.mark.parametrize("question", (_Q_MANUAL, _Q_UNREAD), ids=("headless", "unread"))
def test_과제를_모르는_리스크_심사는_판정_객체를_전부_뺀다(monkeypatch, question, ascii_):
    """머리 없는 손수 길 · 머리를 읽지 못한 길 — 같은 과제를 가릴 수 없어 판정 객체를 전부 뺀다(다른 과제 · 거둔 꼴도). 판정이 아닌
    객체는 남긴다."""
    hits = json.dumps(_objs_hits(_F_MINE, _F_OTHER, _F_RETRACTED, _INCIDENT), ensure_ascii=ascii_)
    got = _ask(_onto_given(monkeypatch, "risk-review", question, {"search_objects": hits}, ())["search_objects"], q="힌지")
    assert _ids(got["items"]) == [301], got
    assert "3건" in got["withheld"] and "가릴 수 없어" in got["withheld"], got


def test_읽지_못한_온톨로지_응답에_같은_과제의_판정이_보이면_통째로_막는다(monkeypatch):
    """잘린 JSON 은 항목을 가려낼 수 없다 — 같은 과제의 머리가 보이면 결과를 주지 않고, 다른 과제의 판정만 보이면 그대로 준다."""
    mine = json.dumps(_objs_hits(_INCIDENT, _F_MINE), ensure_ascii=False)[:-30]
    other = json.dumps(_objs_hits(_INCIDENT, _F_OTHER), ensure_ascii=False)[:-30]
    tool = _onto_given(monkeypatch, "risk-review", _Q_NOW, {"search_objects": mine})["search_objects"]
    out = _ask(tool, q="힌지")
    assert isinstance(out, str) and "P1@" not in out and "가려낼 수 없다" in out, out
    tool = _onto_given(monkeypatch, "risk-review", _Q_NOW, {"search_objects": other})["search_objects"]
    assert _ask(tool, q="힌지") == other


def test_한글_과제_코드는_유니코드_이스케이프_응답에서도_뺀다(monkeypatch):
    code = "힌지 개선 2차"
    mine = {**_F_MINE, "value": f"{code}@mech.hinge_crack@diff:new#a1b2c3d4e5f6"}
    other = {**_F_OTHER, "value": f"{code} B@mech.hinge_crack@diff:x#ffeeddccbbaa"}
    q = f"[리스크심사 {code} diff:new] 기준 P0 대비 변경이 각 도메인에서 어떤 리스크와 개선을 낳는가를 도구 근거로 판정하라."
    hits = json.dumps(_objs_hits(mine, other, _INCIDENT), ensure_ascii=True)
    got = _ask(_onto_given(monkeypatch, "risk-review", q, {"search_objects": hits})["search_objects"], q="힌지")
    assert _ids(got["items"]) == [601, 301] and code in got["withheld"], got
    truncated = hits[:-30]
    out = _ask(_onto_given(monkeypatch, "risk-review", q, {"search_objects": truncated})["search_objects"], q="힌지")
    assert isinstance(out, str) and "가려낼 수 없다" in out, out


def _engine_with(monkeypatch, chair, req, tools, question=_Q_NOW):
    """심의를 좌석 발굴 문턱까지 돌려 이벤트를 받는다 — 엔진이 직접 부르는 길(지정 도구)을 본다."""
    async def _llm(_llm, _system, _human):
        return '{"q": "힌지", "type": "risk_finding", "limit": 5}'

    monkeypatch.setattr(d, "_llm_text", _llm)

    async def _fake_tools(*_a, **_k):
        return tools

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    opts = {"personas": [{"key": "mech-a", "role": "기구"}], "chair_template": chair, "free_tools": 0,
            "voc": "off", "rescreen": 0, "persona_knowledge": 0, **req}

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


@pytest.mark.parametrize("name", ("search_objects", "reportarchive_search_objects"))
def test_리스크_심사의_지정_도구_온톨로지_검색도_같은_과제의_판정_객체를_뺀다(monkeypatch, name):
    """엔진이 직접 부르는 길 — 지정 도구 결과는 전 좌석 프롬프트에 실린다. RA 의 겹침 접두 꼴로 실린 이름도 같다. 다른 심의는 종전대로다."""
    hits = json.dumps(_objs_hits(_F_MINE, _F_OTHER, _INCIDENT), ensure_ascii=False)

    def card(chair):
        events = _engine_with(monkeypatch, chair, {"tools": [name]}, {"agent_search": _Tool("agent_search"),
                                                                      name: _CoTool(name, hits)})
        return next(c for c in _cards(events, included=True) if c["source"] == f"지정 도구 {name}")["text"]

    text = card("risk-review")
    assert _F_MINE["value"] not in text and _F_OTHER["value"] in text and "withheld" in text, text
    assert _F_MINE["value"] in card("default")


def test_리스크_심사의_지정_도구_보고서_검색은_RA_겹침_접두_꼴도_거른다(monkeypatch):
    """RA 의 search_reports 가 다른 앱과 이름이 겹쳐 reportarchive_search_reports 로만 실린 박스 — 맨 이름만 감싸던 동안 그 꼴을 지정하면
    앞 패널 초안이 전 좌석에 실렸다. 다른 심의는 종전대로다."""
    name = "reportarchive_search_reports"
    hits = json.dumps(_ra_hits(), ensure_ascii=False)

    def card(chair):
        events = _engine_with(monkeypatch, chair, {"tools": [name]}, {"agent_search": _Tool("agent_search"),
                                                                      name: _CoTool(name, hits)})
        return next(c for c in _cards(events, included=True) if c["source"] == f"지정 도구 {name}")["text"]

    text = card("risk-review")
    assert _PRIOR_SNIPPET not in text and "1.5m 낙하" in text and "withheld" in text, text
    assert _PRIOR_SNIPPET in card("default")


def test_과제를_모르는_리스크_심사는_판정_객체도_전부_뺐다고_알린다(monkeypatch):
    """머리 없는 · 머리를 읽지 못한 리스크 심사의 경고와 카드가 보고서뿐 아니라 판정 객체도 전부 뺐다고 말한다(감추면 보고서만 넓어진
    줄 안다)."""
    for question, code in ((_Q_MANUAL, "risk_head_missing"), (_Q_UNREAD, "risk_head_unread")):
        warn, cards = _head_notices(_events_for(monkeypatch, question), code)
        assert len(warn) == 1 and len(cards) == 1, (code, warn, cards)
        assert "risk_finding" in warn[0]["message"] and "risk_finding" in cards[0]["text"], (code, warn[0], cards[0])


# ── (10) 포털 대화 검색 · 심의 잡 기록 — 패널의 대화와 잡 기록도 앞 패널이다 ─────────────────────────────────────────────
# 리스크 앱은 패널마다 잡 주인 이름으로 포털 대화를 만들고(HWAXRisk engine_client — 제목 '[리스크심사] <패널 질문 앞 160자>') 거기에 좌석
# 발언과 결정문이 쌓인다. 게이트웨이 search_conversations(접두사 search_ 라 apps 없는 리스크 심사의 자유 조회 후보에 든다 · 지정 도구 ·
# 외부 응답자의 CALL 줄)가 그 제목과 발언 조각을 돌려줘, 좌석이 같은 과제의 앞 패널을 읽었다(D-18 누수 넷째, 2026-11-02). 응답 꼴은 포털
# agent/conv_search 그대로다. 심의 엔진의 잡 기록 읽기(deliberate_list · status · result · transcript)는 지정 도구로만 닿고 거기서 닫는다.
def _conv_hit(cid, title, text, mid):
    return {"score": 0.91, "conversation_id": cid, "title": title, "role": "persona", "persona": "mech-a",
            "ts": 1793500000, "text": text, "message_id": mid, "chunk_ix": 0}


def _conv_hits(*rows):
    """search_conversations 의 응답 모양 — 포털 /agent/conversations/search 의 본문을 게이트웨이가 그대로 싣는다."""
    return {"query": "힌지 리스크", "results": [dict(r) for r in rows], "low_confidence": False, "note": "",
            "index": {"messages": 40, "indexed": 40, "embed_space": "bge-m3", "just_indexed": 0, "too_short": 0,
                      "not_indexed_yet": 0, "budget_hit": False, "in_background": 0, "partial": False}}


_C_MINE = _conv_hit("c-p1", f"[리스크심사] {_Q_PREV}"[:200], "힌지 FAIL — 판정 근거 …", 11)       # 같은 과제의 앞 패널 대화
_C_MINE_2 = _conv_hit("c-p1", f"[리스크심사] {_Q_PREV}"[:200], "결정: 힌지 브래킷 FAIL 유지", 12)    # 같은 대화의 둘째 조각
_C_OTHER = _conv_hit("c-p12", f"[리스크심사] {_Q_OTHER}"[:200], "다른 과제의 판정", 21)
_C_ISSUE = _conv_hit("c-issue", "[리스크심사] [쟁점 I-3 · 갈림] 부품 힌지 브래킷 의 판정이 갈린다", "쟁점 판정 …", 31)
# 쟁점 패널 대화의 조각이 다른 과제의 머리를 인용한다 — 그 대화의 과제는 제목으로 가린다(조각의 머리는 그 대화의 과제가 아니다).
_C_ISSUE_Q = _conv_hit("c-issue2", "[리스크심사] [쟁점 I-4 · 갈림] 부품 힌지 의 판정이 갈린다",
                       "선례로 심의 — [리스크심사 P12 diff:x] 의 판정을 든다", 35)
_C_WF = _conv_hit("c-wf", "심의 — " + _Q_PREV[:50], "워크플로가 저장한 심의 …", 41)              # 머리가 실린 워크플로 대화
_C_NOTE = _conv_hit("c-note", "주간 메모", "[리스크심사 P1 T-old] 판정 메모 …", 51)                 # 조각에 같은 과제의 머리
_C_HEADLESS = _conv_hit("c-headless", "심의 — 힌지 크랙이 각 도메인에서 어떤 리스크를 낳는가", "머리 없는 심의 …", 61)
_C_CHAT = _conv_hit("c-chat", "힌지 낙하 시험 정리", "1.5m 낙하 …", 71)
_CONV_ALL = (_C_MINE, _C_MINE_2, _C_OTHER, _C_ISSUE, _C_ISSUE_Q, _C_WF, _C_NOTE, _C_HEADLESS, _C_CHAT)


def _cids(got):
    return [r["conversation_id"] for r in got["results"]]


@pytest.mark.parametrize("apps,amap", (((), {"search_conversations": "_gateway"}),
                                       (("heax-step_forge", "heax-kooremapper_mcp"), {})),
                         ids=("manual", "runner-map-unavailable"))
def test_리스크_심사_좌석의_대화_검색은_같은_과제의_앞_패널_대화를_뺀다(monkeypatch, apps, amap):
    """같은 과제의 패널 대화(제목 · 조각의 머리)와 과제를 읽을 수 없는 리스크 심사 대화(쟁점 패널)를 뺀다 — 다른 과제의 패널 대화 · 머리
    없는 심의 대화 · 일반 대화는 남긴다. 러너 apps 라도 매핑을 못 받으면 좁히지 않아 대화 검색이 좌석에 간다. 다른 심의는 종전대로다."""
    hits = json.dumps(_conv_hits(*_CONV_ALL), ensure_ascii=False)
    got = _ask(_onto_given(monkeypatch, "risk-review", _Q_NOW, {"search_conversations": hits}, apps, amap)["search_conversations"],
               query="힌지 리스크")
    assert _cids(got) == ["c-p12", "c-headless", "c-chat"], got
    assert "5건" in got["withheld"] and "P1" in got["withheld"] and got["index"]["messages"] == 40, got
    raw = _ask(_onto_given(monkeypatch, "default", _Q_NOW, {"search_conversations": hits}, (),
                           {"search_conversations": "_gateway"})["search_conversations"], query="힌지 리스크")
    assert raw == json.loads(hits), raw


@pytest.mark.parametrize("ascii_", (False, True), ids=("utf8", "escaped"))
@pytest.mark.parametrize("question", (_Q_MANUAL, _Q_UNREAD), ids=("headless", "unread"))
def test_과제를_모르는_리스크_심사의_대화_검색은_리스크_심사_심의_대화를_전부_뺀다(monkeypatch, question, ascii_):
    """같은 과제를 가릴 수 없다 — '[리스크심사' 가 보이는 대화와 심의 대화(제목 '심의 — …')를 전부 뺀다(다른 과제의 것도). 일반 대화는 남긴다."""
    hits = json.dumps(_conv_hits(*_CONV_ALL), ensure_ascii=ascii_)
    got = _ask(_onto_given(monkeypatch, "risk-review", question, {"search_conversations": hits}, (),
                           {"search_conversations": "_gateway"})["search_conversations"], query="힌지 리스크")
    assert _cids(got) == ["c-chat"], got
    assert "7건" in got["withheld"] and "가릴 수 없어" in got["withheld"], got


def test_읽지_못한_대화_검색_응답에_리스크_심사_대화가_보이면_통째로_막는다(monkeypatch):
    """자유 조회의 결과 상한에서 잘린 JSON 은 조각을 가려낼 수 없다 — 리스크 심사 대화가 보이면(다른 과제의 것이라도) 결과를 주지 않고,
    보이지 않으면 그대로 준다. 과제를 모르면 심의 대화의 제목 머리도 막는다."""
    amap = {"search_conversations": "_gateway"}

    def ask(question, rows):
        raw = json.dumps(_conv_hits(*rows), ensure_ascii=False)[:-30]
        tool = _onto_given(monkeypatch, "risk-review", question, {"search_conversations": raw}, (), amap)["search_conversations"]
        return raw, _ask(tool, query="힌지 리스크")

    for question, rows in ((_Q_NOW, (_C_CHAT, _C_OTHER)), (_Q_MANUAL, (_C_CHAT, _C_HEADLESS))):
        raw, out = ask(question, rows)
        assert isinstance(out, str) and out != raw and "가려낼 수 없다" in out and "다른 과제의 판정" not in out, out
    raw, out = ask(_Q_NOW, (_C_CHAT, _C_HEADLESS))
    assert out == raw


def test_리스크_심사의_지정_도구_대화_검색도_같은_과제의_앞_패널_대화를_뺀다(monkeypatch):
    """엔진이 직접 부르는 길 — 지정 도구 결과는 전 좌석 프롬프트에 실린다. 다른 심의는 종전대로다."""
    hits = json.dumps(_conv_hits(*_CONV_ALL), ensure_ascii=False)

    def card(chair):
        events = _engine_with(monkeypatch, chair, {"tools": ["search_conversations"]},
                              {"agent_search": _Tool("agent_search"),
                               "search_conversations": _CoTool("search_conversations", hits)})
        return next(c for c in _cards(events, included=True) if c["source"] == "지정 도구 search_conversations")["text"]

    text = card("risk-review")
    assert "힌지 FAIL — 판정 근거" not in text and "쟁점 판정" not in text and "1.5m 낙하" in text, text
    assert "다른 과제의 판정" in text and "withheld" in text, text
    text = card("default")
    assert "힌지 FAIL — 판정 근거" in text and "withheld" not in text, text


def test_리스크_심사는_심의_잡_기록_읽기를_지정해도_부르지_않는다(monkeypatch):
    """심의 엔진의 잡 기록(목록은 행마다 화두 · 셋은 잡 id 로 상태 · 결정문 · 좌석 발언)도 앞 패널이다 — 맨 이름과 겹침 접두 꼴
    (hwaxdeliberation_<도구>) 둘 다 닫는다. 심의 메뉴(deliberate_jobs)는 잡 기록이 아니라 그대로 부른다. 다른 심의는 종전대로다."""
    reads = ("deliberate_list", "deliberate_status", "deliberate_result", "deliberate_transcript")
    names = reads + tuple(f"hwaxdeliberation_{n}" for n in reads)
    runs, tools = _designate(monkeypatch, "risk-review", names + ("deliberate_jobs",))
    assert sorted(n for n in tools if tools[n].calls) == ["deliberate_jobs"], sorted(n for n in tools if tools[n].calls)
    _refused(runs, names)
    _runs, tools = _designate(monkeypatch, "default", names)
    assert all(tools[n].calls for n in names), sorted(n for n in names if not tools[n].calls)


def test_과제를_모르는_리스크_심사는_대화도_전부_뺐다고_알린다(monkeypatch):
    """머리 없는 · 머리를 읽지 못한 리스크 심사의 경고와 카드가 대화 검색도 전부로 넓어졌다고 말한다."""
    for question, code in ((_Q_MANUAL, "risk_head_missing"), (_Q_UNREAD, "risk_head_unread")):
        warn, cards = _head_notices(_events_for(monkeypatch, question), code)
        assert len(warn) == 1 and len(cards) == 1, (code, warn, cards)
        assert "대화 검색" in warn[0]["message"] and "search_conversations" in cards[0]["text"], (code, warn[0], cards[0])
