# 실패로 건너뛴 부가 단계가 보이는지, 조회가 실패한 웹 인용을 날조로 세지 않는지
#
# 핵심 요약·쉬운 설명·VOC 환기·정량 근거 선주입은 실패해도 심의를 죽이지 않는다(맞는 동작이다). 그런데 흔적이
# 서버 로그 한 줄뿐이라, LLM 호출 한도에 걸려 빠진 요약과 환기가 **처음부터 없던 것**과 똑같이 보였다.
# 웹 인용 대조는 더 나빴다 — 원장 조회가 시간 초과로 실패한 인용을 '날조' 로 세어 결정문에 '이 항목의 근거는
# 신뢰하지 마세요' 를 붙였다. 한도 만료가 틀린 판정으로 둔갑한 것이다.
#
# **스트림을 끝까지 돌려서** 본다 — 단계 하나의 LLM 호출만 골라 터뜨린다.
#
#   실행:  .venv/bin/python -m pytest tests/test_skipped_steps.py -q
import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import openai  # noqa: E402
import pytest  # noqa: E402

import deliberation as d  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _cards, _mcp_view, _pin_context, _steps, _stream, _Tool  # noqa: E402, F401

_REQ = httpx.Request("POST", "http://llm.invalid/v1/chat/completions")
_SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}]
_KNOB = "DELIB_TIMEOUT_S · 요청 timeout_s(상한 DELIB_TIMEOUT_MAX_S)"
_WHY = "LLM 호출이 1,800초 안에 끝나지 않았다(2회 시도 · APITimeoutError)"
# 단계 → 그 단계의 LLM 호출을 알아보는 시스템 프롬프트 조각
_STEP = {"summary": "군더더기 없이 핵심만", "plain": "비전문가에게 설명하는 사람",
         "voc": "반드시 유효한 JSON 하나만 출력하세요", "prepass": "주어진 검색 결과에서만 발췌"}
# 단계 → (경고 코드, 상태줄·경고에 나오는 구절, 경고 끝 문장)
_WANT = {"summary": ("summary_skipped", "핵심 요약을 만들지 못했다", "결정문은 그대로입니다."),
         "plain": ("plain_skipped", "쉬운 설명을 만들지 못했다", "결정문은 그대로입니다."),
         "voc": ("voc_recall_skipped", "최근 불량 이슈 환기(VOC)를 하지 못했다", "VOC 없이 질문만으로 진행합니다."),
         "prepass": ("evidence_prepass_skipped", "정량 근거 선주입을 하지 못했다", "사전 근거 없이 진행합니다.")}


def _timeout():
    try:
        try:
            raise httpx.ReadTimeout("가로챈 요청", request=_REQ)
        except httpx.ReadTimeout as low:
            raise openai.APITimeoutError(request=_REQ) from low
    except openai.APITimeoutError as exc:
        return exc


def _run(monkeypatch, fail=None, decision="결정문 본문", tools=None, **req):
    """심의를 끝까지 돌린다. fail 은 터뜨릴 단계(_STEP 의 키) — 그 단계의 LLM 호출만 시간 초과로 끝난다."""
    async def _text(_obj, system, _human):
        if fail and _STEP[fail] in system and "전문가입니다" not in system:
            raise _timeout()
        if re.search(r"당신은 '[^']+' 전문가", system):
            return json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["가", "나"],
                               "position_short": "요약", "final_position": "최종", "non_negotiable": "",
                               "vote": "진행", "stance": "동의"}, ensure_ascii=False)
        if "엔지니어링 톤" in system:
            return decision
        if _STEP["voc"] in system:
            return '{"relevant": true, "reason": "힌지 불만"}'
        return "- 한 줄"

    monkeypatch.setattr(d, "_llm_text", _text)
    llm = SimpleNamespace(request_timeout=httpx.Timeout(1800.0, connect=10.0), max_retries=1, max_tokens=None)
    base = {"agent_search": _Tool("agent_search"),
            "alert_check": _Tool("alert_check", '{"summary": "힌지 소음 불만 급증"}'),
            "daily_briefing": _Tool("daily_briefing", "힌지 관련 부정 VOC 12건"),
            "query_voc": _Tool("query_voc", '{"content": "힌지에서 소리가 난다", "product": "X"}'),
            "hybrid_search": _Tool("hybrid_search", "힌지 토크 0.42 N·m 사례")}
    return _stream(monkeypatch, {"personas": _SEATS, "rounds": 2, "save_report": 0, "persona_knowledge": 0, **req},
                   tools={**base, **(tools or {})}, until=lambda ev, _data: ev == "done", llm=llm)


def _warn(events, code):
    return [data for ev, data in events if ev == "warning" and data.get("code") == code]


def _decision(events):
    return next(data["text"] for ev, data in events if ev == "delib" and data.get("kind") == "decision")


# ── 부가 단계 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("step,req", [("summary", {}), ("plain", {}),
                                      ("voc", {"voc": "always"}), ("prepass", {"evidence_prepass": 1})])
def test_한도에_걸려_건너뛴_단계는_상태줄과_경고로_남는다(monkeypatch, step, req):
    code, what, after = _WANT[step]
    events = _run(monkeypatch, fail=step, **req)
    (warn,) = _warn(events, code)
    assert warn["message"] == f"{what} — {_WHY} — 호출당 타임아웃을 늘린다. {after}", warn["message"]
    assert warn["knob"] == _KNOB and "DELIB_" not in warn["message"]
    line = next(data for ev, data in events if ev == "status" and data.get("step", "").startswith(f"⚠ {what}"))
    assert _WHY in line["step"] and line["knob"] == _KNOB, line
    # 심의는 죽지 않는다 — 결정문까지 간다.
    assert "결정문 본문" in _decision(events) and events[-1][0] == "done"
    for name, view in _mcp_view(monkeypatch, events).items():
        assert any(what in w and w.endswith(f"(설정 {_KNOB})") for w in view["warnings"]), (name, view["warnings"])


def test_요약이_빠지면_결정문에_요약_머리가_없고_쉬운_설명은_그대로_만든다(monkeypatch):
    events = _run(monkeypatch, fail="summary")
    text = _decision(events)
    assert "■ 핵심 요약" not in text and "■ 쉬운 설명" in text, text[:200]
    assert not _warn(events, "plain_skipped")


def test_쉬운_설명이_빠지면_그_카드도_없다(monkeypatch):
    events = _run(monkeypatch, fail="plain")
    assert "■ 쉬운 설명" not in _decision(events) and "■ 핵심 요약" in _decision(events)
    assert not [1 for ev, data in events if ev == "delib" and data.get("kind") == "plain"]


def test_건너뛴_것이_없으면_그런_경고가_없다(monkeypatch):
    events = _run(monkeypatch, voc="always", evidence_prepass=1)
    for code, _what, _after in _WANT.values():
        assert not _warn(events, code), code
    assert not any(s_.startswith("⚠") for s_ in _steps(events)), [s_ for s_ in _steps(events) if s_.startswith("⚠")]
    assert "■ 핵심 요약" in _decision(events) and "■ 쉬운 설명" in _decision(events)


def test_시간과_무관한_실패는_설정을_가리키지_않는다(monkeypatch):
    async def _boom(*_a, **_k):
        raise KeyError("issues")

    monkeypatch.setattr(d, "_defect_briefing", _boom)
    events = _run(monkeypatch, voc="always")
    (warn,) = _warn(events, "voc_recall_skipped")
    assert "KeyError" in warn["message"] and "knob" not in warn, warn


# ── 웹 인용 대조 — 확인하지 못한 것은 날조가 아니다 ─────────────────────────────
_OK, _FAKE, _DOWN = "[W:d_0123456789ab#1]", "[W:d_0123456789ab#2]", "[W:d_0123456789ab#3]"
_CALL_FAILED = ("✖ 도구 get_quote 호출 실패: 900초 안에 답하지 않았다(MCP_CALL_TIMEOUT_S) — 게이트웨이 무응답이다.")


class _Ledger(_Tool):
    """웹 리서치 원장 대역 — 1번 문장은 있고, 2번은 없다고 답하고, 3번은 조회 자체가 실패한다."""

    def __init__(self):
        super().__init__("get_quote")

    async def ainvoke(self, args):
        self.calls.append(args)
        return {1: '{"ok": true, "data": {"text": "원문 문장"}}', 2: '{"ok": false, "error": "no such sentence"}',
                3: _CALL_FAILED}[args["index"]]


def _cite_run(monkeypatch, cites, **kw):
    return _run(monkeypatch, decision="결정: 문헌에 따르면 그렇다 " + " ".join(cites),
                search_sources=["web"], tools={"get_quote": _Ledger()}, **kw)


def test_원장_조회가_실패한_인용은_날조로_세지_않고_확인_못_했다고_적는다(monkeypatch):
    events = _cite_run(monkeypatch, [_DOWN])
    text = _decision(events)
    assert "확인하지 못했습니다" in text and _DOWN in text.split("확인하지 못했습니다")[1], text[-300:]
    assert "날조라는 뜻이 아닙니다" in text
    assert "날조 가능" not in text and "신뢰하지 마세요" not in text, "조회 실패를 날조로 적었다"
    line = next(s_ for s_ in _steps(events) if s_.startswith("웹 인용 대조"))
    assert line == "웹 인용 대조 — 실재 0건 / 날조 0건 / 확인 못 함 1건(원장 조회 실패)", line
    (warn,) = _warn(events, "web_cite_unverified")
    assert "1건" in warn["message"] and "MCP_CALL_TIMEOUT_S" in warn["knob"], warn


def test_있는_것_없는_것_못_물어본_것을_따로_센다(monkeypatch):
    events = _cite_run(monkeypatch, [_OK, _FAKE, _DOWN])
    line = next(s_ for s_ in _steps(events) if s_.startswith("웹 인용 대조"))
    assert line == "웹 인용 대조 — 실재 1건 / 날조 1건 / 확인 못 함 1건(원장 조회 실패)", line
    text = _decision(events)
    fake_note = text.split("원장에서 확인되지 않았습니다(날조 가능)")[1].split("\n")[0]
    assert _FAKE in fake_note and _DOWN not in fake_note and _OK not in fake_note, fake_note
    down_note = text.split("확인하지 못했습니다")[1].split("\n")[0]
    assert _DOWN in down_note and _FAKE not in down_note, down_note
    assert "원장 원문과 대조되었습니다" not in text, "못 물어본 인용이 있는데 전부 대조됐다고 적었다"


def test_전부_확인되면_종전_문구_그대로다(monkeypatch):
    events = _cite_run(monkeypatch, [_OK])
    assert "웹 인용 1건이 원장 원문과 대조되었습니다" in _decision(events)
    assert not _warn(events, "web_cite_unverified")
    assert next(s_ for s_ in _steps(events) if s_.startswith("웹 인용 대조")) == "웹 인용 대조 — 실재 1건 / 날조 0건"


def test_확인_못_한_인용의_표시가_저장되는_보고서에도_실린다(monkeypatch):
    saved = _Tool("create_report_draft", '{"report_id": 7}')
    _run(monkeypatch, decision=f"결정: 문헌에 따르면 그렇다 {_DOWN}", search_sources=["web"], save_report=1,
         tools={"get_quote": _Ledger(), "create_report_draft": saved})
    body = "\n\n".join(saved.calls[0]["blocks"]["recommendation"])
    assert "확인하지 못했습니다" in body and _DOWN in body, body[-300:]


def test_도구를_못_받으면_전부_확인_못_한_것으로_돌려준다(monkeypatch):
    """종전엔 (0, 0, []) 로 돌아와 대조를 안 한 것이 조용히 묻혔다."""
    async def _down(*_a, **_k):
        raise ConnectionError("gateway down")

    monkeypatch.setattr(d, "_tools_by_name", _down)
    got = asyncio.run(d._verify_web_citations(None, [], f"가 {_OK} 나 {_FAKE}"))
    assert got == (0, 0, [], [_OK, _FAKE]), got
    assert asyncio.run(d._verify_web_citations(None, [], "인용 없음")) == (0, 0, [], [])
