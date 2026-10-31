# 웹 리서치의 차단 · 빈 결과는 근거로 세지 않는다 — 그런 봉투뿐인 심의는 근거 프로파일과 의장 지시에 '가설 단계' 로 남는다
#
# 웹 리서치 앱(WebResearchMCP)은 결과의 상태를 맨 위가 아니라 data.status 에 싣는다(ok · empty · blocked · degraded — 3상태 계약,
# 그 리포 app/errors.py). 차단(blocked) · 빈 결과(empty)도 맨 위는 ok:true · errors [] 라, 맨 위만 보는 판정(envelope_failed)은 그 봉투를
# 정상 결과로 읽는다 — 그래서 근거 프로파일의 '도구 조회' 가 올라 실측 데이터 0건인 심의에서 '가설 단계' 표지가 빠졌다
# (HWAXPortal docs/web-research checklist P6 '근거 0 심의에서 [가설 단계] 태깅').
#
# **실제 엔진을 의장 단계까지 돌려** 의장이 받은 프롬프트를 본다. 세는 자리가 셋(지정 도구 · 좌석 자유 조회 · 챗이 넘긴 근거)이라
# 판정 함수만 떼어 보면 '판정은 맞는데 그 자리에서 부르지 않는다' 를 못 잡는다.
#
#   실행:  .venv/bin/python -m pytest tests/test_web_envelope_evidence.py -q
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402

# 같은 하네스를 쓴다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _pin_context, _Tool  # noqa: E402, F401

_SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}]
_SEAT_JSON = json.dumps({"lens": "관점", "reads": ["해석"], "recommendation": "권장", "concerns": ["가", "나"],
                         "position_short": "입장", "final_position": "최종 입장", "non_negotiable": "",
                         "vote": "진행", "stance": "동의"}, ensure_ascii=False)
_PATHS = ("tools", "free", "evidence")        # 지정 도구 · 좌석 자유 조회 · 챗이 넘긴 근거
_HYPO_ASK = "제목 앞에 [가설 단계] 를 붙이고"     # 의장 지시 — 도구 조회 0건일 때만 붙는다
_HYPO_NOTE = "실측 데이터 0건(가설 단계)"         # 근거 프로파일(_evidence_note) — 의장 프롬프트에 그대로 실린다


def _env(status: str, n: int = 0, **data) -> str:
    """WebResearchMCP 의 envelope(search_data(…)) 꼴 그대로 — 맨 위 ok 는 errors 가 비면 참이다. n 은 결과 건수."""
    rows = [{"title": f"논문 {i}", "url": f"https://example.org/p{i}", "date": None, "authors": [], "doi": None,
             "source": "arxiv", "snippet": "요약"} for i in range(n)]
    return json.dumps({"ok": True,
                       "data": {"status": status, "results": rows, "note": "",
                                "egress": {"occurred": status != "blocked", "mode": "domain_only"}, **data},
                       "errors": [],
                       "warnings": ["모든 소스가 차단됐습니다. '그런 논문이 없다'는 뜻이 아닙니다."] if status == "blocked" else [],
                       "server": {"name": "web-research", "version": "0"}}, ensure_ascii=False)


# ── 판정 ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("out", [_env("blocked"), _env("empty"), _env("degraded"),
                                 _env("BLOCKED"),                                       # 대소문자는 가리지 않는다
                                 _env("blocked", provider="searxng", upstream_engines=[])],   # search_web 의 꼴
                         ids=["blocked", "empty", "degraded-0", "upper", "search_web"])
def test_차단_빈_결과_건수_0인_부분_실패는_근거가_아니다(out):
    assert d._envelope_no_evidence(out)
    assert not d.envelope_failed(out), "시험 전제 — 맨 위만 보는 실패 판정은 이 봉투를 실패로 보지 않는다(실패가 아니다)"


@pytest.mark.parametrize("out", [
    _env("ok", 2), _env("degraded", 1),                                  # 결과가 있다 — 일부 실패여도 근거다
    json.dumps({"ok": True, "data": {"status": "empty"}}),                # results 가 없다 — 3상태 봉투가 아니다
    json.dumps({"ok": True, "data": {"status": "blocked", "results": "없음"}}),
    json.dumps({"status": "empty", "results": []}),                        # 맨 위 칸 — 다른 앱의 꼴이다
    json.dumps([{"data": {"status": "empty", "results": []}}]),
    "검색 결과 없음", "", None, '{"ok": true, "data": ',                    # JSON 아님 · 잘린 글
], ids=["ok", "degraded-1", "no-results", "results-str", "top-level", "list", "text", "blank", "None", "cut-json"])
def test_그_밖의_꼴은_판정하지_않는다(out):
    assert not d._envelope_no_evidence(out)


# ── 실제 엔진 ────────────────────────────────────────────────────────────────
def _chair_prompt(monkeypatch, path: str, out: str) -> tuple[str, list]:
    """세는 자리 하나(path)로만 웹 리서치 봉투 out 을 흘려 실제 엔진을 의장 단계까지 돌리고 (의장이 받은 프롬프트, 이벤트) 를 돌려준다.

    좌석은 지정으로 주고(발굴 생략) VOC · 지식카드 · 재심사 · 보고서 저장을 끈다 — 망을 타는 단계다. 자유 조회는 그 자리를
    볼 때만 켜고, 좌석의 조회 턴(_free_gather_one)은 그 봉투를 받은 호출 하나로 갈음한다."""
    import langgraph.prebuilt

    scholar, seen, gathered = _Tool("search_scholar", answer=out), [], []

    async def _fake_tools(*_a, **_k):
        return {"search_scholar": scholar}

    async def _fake_llm(_llm, system, human, *_a):
        seen.append((system, human))
        if "엔지니어링 톤" in system:
            return "결정문 본문"
        if "도구 호출 계획자" in system:
            return '{"query": "hinge adhesive delamination"}'
        return _SEAT_JSON

    async def _fake_gather(_agent, persona, *_a, **_k):
        gathered.append(persona["key"])
        return persona["key"], [("search_scholar", '{"query": "x"}', out, f"call-{persona['key']}")], "", ""

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    monkeypatch.setattr(d, "_llm_text", _fake_llm)
    monkeypatch.setattr(d, "_free_gather_one", _fake_gather)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda *_a, **_k: object())
    opts = {"personas": _SEATS, "rounds": 2, "voc": "off", "rescreen": 0, "persona_knowledge": 0,
            "evidence_prepass": 0, "save_report": 0, "free_tools": int(path == "free")}
    if path == "tools":
        opts["tools"] = ["search_scholar"]
    if path == "evidence":
        opts["evidence"] = [{"source": "챗", "tool": "search_scholar", "args": '{"query": "x"}', "result": out}]
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None))

    async def go():
        return [delib_jobs._parse_sse(c) async for c in d.run_deliberation(stub, "힌지 크랙 원인", [], opts)]

    events = asyncio.run(go())
    errs = [x for ev, x in events if ev == "error"]
    assert not errs, f"스트림이 오류로 끝났다 — {errs}"
    # 시험 전제 — 그 자리가 실제로 돌았다. 안 돌았으면 '0건' 은 판정이 아니라 하네스가 눈이 먼 것이다.
    assert {"tools": bool(scholar.calls), "free": bool(gathered),
            "evidence": any(ev == "delib" and x.get("source") == "챗 정리 · 챗" for ev, x in events)}[path], \
        f"{path} 자리가 돌지 않았다 — 하네스가 낡았다"
    chair = [h for s, h in seen if "엔지니어링 톤" in s]
    assert chair, "의장 단계까지 가지 못했다"
    return chair[0], events


def _secured(events) -> str:
    """지정 도구 단계의 마지막 상태줄 — '지정 도구 근거 확보 — N건 …'."""
    return next(x["step"] for ev, x in events if ev == "status" and str(x.get("step", "")).startswith("지정 도구 근거 확보"))


@pytest.mark.parametrize("status", ["blocked", "empty", "degraded"])
@pytest.mark.parametrize("path", _PATHS)
def test_차단_빈_결과_봉투뿐인_심의는_가설_단계다(monkeypatch, path, status):
    chair, events = _chair_prompt(monkeypatch, path, _env(status))
    assert "도구 조회 0건" in chair and _HYPO_NOTE in chair, (
        f"{path} 자리의 웹 리서치 {status} 봉투를 근거로 셌다 — 근거 프로파일에 가설 단계가 없다")
    assert _HYPO_ASK in chair, f"{path} 자리의 {status} 봉투뿐인데 의장 지시에 [가설 단계] 가 없다"
    if path == "tools":
        # 화면의 셈도 같은 말을 한다 — '근거 확보 1건' 이면 결정문의 '도구 조회 0건' 과 어긋난다. 좌석에는 그대로 실린다.
        assert _secured(events).startswith("지정 도구 근거 확보 — 0건 · 웹 리서치 차단 · 빈 결과 1건(search_scholar)"), \
            _secured(events)
        assert "[사용자 지정 도구 정량 결과" in chair and '"status": "' + status + '"' in chair


@pytest.mark.parametrize("out", [_env("ok", 2), _env("degraded", 1)], ids=["ok", "degraded-1"])
@pytest.mark.parametrize("path", _PATHS)
def test_결과가_있는_봉투는_근거로_센다(monkeypatch, path, out):
    """시험 전제 — 같은 자리에 결과가 있는 봉투를 흘리면 센다. 이게 안 서면 위 시험은 그 자리를 안 거쳐 통과한 것일 수 있다."""
    chair, events = _chair_prompt(monkeypatch, path, out)
    n = len(_SEATS) if path == "free" else 1          # 자유 조회는 좌석마다 한 건이다
    assert f"도구 조회 {n}건" in chair and _HYPO_NOTE not in chair and _HYPO_ASK not in chair, chair[:600]
    if path == "tools":
        assert _secured(events) == "지정 도구 근거 확보 — 1건", _secured(events)
