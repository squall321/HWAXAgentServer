# 리스크 심사 좌석에게 같은 과제의 앞 패널 판정이 닿지 않는다 — 사용자 결정 D-18 의 지정 도구 길과 RA 보고서 검색 길
#
# 자유 조회 후보는 tests/test_delib_seats.py 의 등록부 가드가 본다. 이 파일은 그 밖의 두 길이다.
# (1) 호출자가 고르는 지정 도구(delib_opts.tools) — 엔진이 그 도구를 주제 기반 인자로 불러 결과를 **전 좌석 프롬프트에**
#     싣는다. 리스크 심사에 risk_get_registry 를 지정하면 좌석이 앞 패널 판정을 읽고 같은 말을 해, 원장(D-23)의 지지 수가
#     독립 근거 없이 오른다.
# (2) 엔진이 패널마다 Report Archive 에 남기는 심의 보고서 — 러너가 save_report 를 보내지 않아 패널마다 결정문(판정)이 든
#     초안이 생기고, 같은 잡 자격으로 도는 다음 패널 좌석의 search_reports(리스크 심사 유지 도구)에 걸린다(RA 는 소유한
#     초안도 검색 범위에 넣고 저장 때 본문 색인을 채운다). engine-contract A-D18-ra-reports.
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


def _seat_pool(monkeypatch, chair, question, answer, apps=("heax-step_forge",)):
    """심의를 좌석 발굴 문턱까지 실제로 돌려, 자유 조회 준비가 좌석 에이전트에게 준 도구들을 받는다. apps 기본값은 러너처럼
    어댑터 앱이다(RA 도구는 리스크 심사 유지 도구로만 남는다)."""
    import langgraph.prebuilt

    given = []

    def _fake_agent(_llm, tools, **_k):
        given.append({t.name: t for t in tools})
        return SimpleNamespace()

    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", _fake_agent)
    monkeypatch.setattr(d, "_app_of_tools", lambda: {"search_reports": "reportarchive"})
    tools = {"agent_search": _Tool("agent_search"), "search_reports": _CoTool("search_reports", answer)}

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
    return given[0]["search_reports"]


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
