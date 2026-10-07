# 좌석 유실 알림이 왜 빠졌는지를 말하는지 — LLM 호출 한도인지 서버가 죽은 것인지, 한도라면 얼마이고 어느 값을 올리나
#
# 좌석 하나가 그 라운드에 발언하지 못하면 알림은 '(오류·시간초과)' 뿐이었다. 22석 패널이 공유 LLM 에 줄을 서서
# 좌석 셋이 호출 한도에 걸려 빠져도, 읽는 사람은 그것이 한도 때문인지 LLM 서버가 내려간 것인지 알 수 없었고
# 한도였다면 지금 값이 얼마인지·어느 설정을 올려야 하는지도 없었다.
#
# **스트림을 실제로 돌려서** 본다 — 좌석 발언에서 openai SDK 가 올리는 것과 같은 모양의 예외를 터뜨린다.
#
#   실행:  .venv/bin/python -m pytest tests/test_seat_loss_reason.py -q
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import openai  # noqa: E402

import deliberation as d  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _cards, _mcp_view, _pin_context, _steps, _stream  # noqa: E402, F401

_REQ = httpx.Request("POST", "http://llm.invalid/v1/chat/completions")
_SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}, {"key": "mat-c", "role": "재료"}]
_TIMEOUT_KNOB = "DELIB_TIMEOUT_S · 요청 timeout_s(상한 DELIB_TIMEOUT_MAX_S)"


def _sdk_error(cause, outer):
    """openai SDK 가 올리는 모양 그대로 — httpx 예외를 원인(__cause__)으로 문 SDK 예외."""
    try:
        try:
            raise cause("가로챈 요청", request=_REQ)
        except httpx.HTTPError as low:
            raise outer(request=_REQ) from low
    except openai.APIError as exc:
        return exc


def _read_timeout():
    return _sdk_error(httpx.ReadTimeout, openai.APITimeoutError)


def _connect_timeout():           # openai 는 연결 시간 초과도 APITimeoutError 로 올린다
    return _sdk_error(httpx.ConnectTimeout, openai.APITimeoutError)


def _refused():
    return _sdk_error(httpx.ConnectError, openai.APIConnectionError)


def _llm(read=1800.0, retries=1):
    """엔진이 한도와 시도 횟수를 읽는 자리만 갖춘 LLM — 호출은 시험이 가로챈다."""
    return SimpleNamespace(request_timeout=httpx.Timeout(read, connect=10.0), max_retries=retries, max_tokens=None)


def _run(monkeypatch, failing, llm=None):
    """1라운드에서 failing({좌석 키: 예외})의 좌석이 터지는 심의를 끝까지 돌려 이벤트를 받는다."""
    async def _text(_obj, system, _human):
        if "전문가입니다" not in system:
            return "결정문 본문"
        return json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["가", "나"],
                           "position_short": "요약", "final_position": "최종", "non_negotiable": "",
                           "vote": "진행", "stance": "동의"}, ensure_ascii=False)

    real_round = d._persona_round

    async def _round(llm_obj, p, prompt, required, validator, opts=None):
        if p["key"] in failing and tuple(required) == ("lens", "recommendation"):
            raise failing[p["key"]]
        return await real_round(llm_obj, p, prompt, required, validator, opts=opts)

    monkeypatch.setattr(d, "_llm_text", _text)
    monkeypatch.setattr(d, "_persona_round", _round)
    return _stream(monkeypatch, {"personas": _SEATS, "rounds": 2, "save_report": 0, "persona_knowledge": 0},
                   until=lambda ev, _data: ev == "done", llm=llm or _llm())


def _loss(events):
    """(상태줄 이벤트, 경고 이벤트, 카드) — 좌석 유실 알림 셋."""
    status = next(data for ev, data in events if ev == "status" and "좌석 유실" in data.get("step", ""))
    warning = next(data for ev, data in events if ev == "warning" and data.get("code") == "seat_lost")
    card = next(c for c in _cards(events, included=False) if c["source"] == "좌석 유실")
    return status, warning, card


def test_호출_한도에_걸린_좌석은_걸린_값과_시도_횟수를_말한다(monkeypatch):
    events = _run(monkeypatch, {"rel-b": _read_timeout()})
    status, warning, card = _loss(events)
    want = "rel-b · LLM 호출이 1,800초 안에 끝나지 않았다(2회 시도 · APITimeoutError)"
    for text in (status["step"], warning["message"], card["text"]):
        assert want in text and "호출당 타임아웃을 늘린다" in text, text
        assert "오류·시간초과" not in text, "사유를 뭉뚱그린 종전 문구가 남아 있다"
        # 화면에 뜨는 글에는 설정·필드 이름이 없다 — 이름은 knob 으로 간다.
        assert "DELIB_" not in text and "timeout_s" not in text, text
    assert status["knob"] == warning["knob"] == card["knob"] == _TIMEOUT_KNOB
    assert "mech-a" not in card["text"].split("못했다")[0], "빠지지 않은 좌석을 유실로 적었다"


def test_잡_원장에는_설정_이름이_붙어_남는다(monkeypatch):
    """MCP 호출자가 보는 것은 원장이다 — 상태줄·경고·카드 셋 다 어느 설정을 올릴지 말한다."""
    events = _run(monkeypatch, {"rel-b": _read_timeout()})
    for name, view in _mcp_view(monkeypatch, events).items():
        row = next(x for x in view["evidence_omitted"] if x.get("source") == "좌석 유실")
        assert row["text"].endswith(f"(설정 {_TIMEOUT_KNOB})"), (name, row)
        (warn,) = [w for w in view["warnings"] if "좌석 유실" in w]
        assert warn.endswith(f"(설정 {_TIMEOUT_KNOB})"), (name, warn)
    result = _mcp_view(monkeypatch, events)["deliberate_result"]
    assert any("좌석 유실" in s_ and s_.endswith(f"(설정 {_TIMEOUT_KNOB})") for s_ in result["steps"]), result["steps"]


def test_요청_단위_한도로_돈_심의는_그_값을_말한다(monkeypatch):
    """설정을 다시 읽지 않고 그 심의의 LLM 에 실제로 걸린 값을 읽는다 — 7,200초를 청해 돈 심의에 1,800초라고
    적으면 호출자는 엉뚱한 값을 올린다."""
    events = _run(monkeypatch, {"rel-b": _read_timeout()}, llm=_llm(read=7200.0, retries=0))
    assert "LLM 호출이 7,200초 안에 끝나지 않았다(1회 시도 · APITimeoutError)" in _loss(events)[2]["text"]


def test_연결하지_못한_것은_한도를_늘리라고_하지_않는다(monkeypatch):
    """느린 것이 아니라 닿지 않는 것이다 — 호출 한도를 올려도 풀리지 않는다."""
    # 연결 시간 초과는 걸린 연결 한도(10초)를 말하고, 거절은 한도와 무관하니 말하지 않는다.
    for exc, want in ((_connect_timeout(), "LLM 서버에 10초 안에 연결하지 못했다(APITimeoutError)"),
                      (_refused(), "LLM 서버에 연결하지 못했다(APIConnectionError)")):
        status, warning, card = _loss(_run(monkeypatch, {"mat-c": exc}))
        for text in (status["step"], warning["message"], card["text"]):
            assert f"mat-c · {want}" in text, text
            assert "늘린다" not in text and "1,800초" not in text, text
        assert "VLLM_BASE_URL" in card["knob"] and "LLM_CONNECT_TIMEOUT_S" in card["knob"], card["knob"]
        assert "DELIB_TIMEOUT_S" not in card["knob"], card["knob"]


def test_사유가_다른_좌석은_사유별로_묶어_적는다(monkeypatch):
    events = _run(monkeypatch, {"mech-a": _read_timeout(), "rel-b": _read_timeout(),
                                "mat-c": ValueError("모델이 빈 응답을 냈다")})
    # 좌석이 전부 빠져도 알림은 나간다(라운드는 빈 채로 넘어간다 — 그 판정은 이 시험의 몫이 아니다).
    card = _loss(events)[2]
    assert "mech-a, rel-b · LLM 호출이 1,800초 안에" in card["text"], card["text"]
    assert "mat-c · ValueError: 모델이 빈 응답을 냈다" in card["text"], card["text"]
    assert card["knob"] == _TIMEOUT_KNOB, "설정이 없는 사유에 설정 이름을 붙였다"


def test_설정과_무관한_실패에는_knob_이_없다(monkeypatch):
    status, warning, card = _loss(_run(monkeypatch, {"rel-b": ValueError("모델이 빈 응답을 냈다")}))
    assert "knob" not in status and "knob" not in warning and "knob" not in card
    assert "rel-b · ValueError: 모델이 빈 응답을 냈다" in card["text"]


def test_걸린_한도는_LLM_객체에서_읽는다():
    assert d._llm_limit(_llm(1800.0, 1)) == (1800.0, 2)
    assert d._llm_limit(SimpleNamespace(request_timeout=600.0, max_retries=2)) == (600.0, 3)
    assert d._llm_limit(SimpleNamespace(request_timeout=httpx.Timeout(None, connect=10.0), max_retries=0)) == (0.0, 1)
    assert d._llm_limit(object()) == (0.0, 0), "모르는 것을 지어내지 않는다"
    note, knob = d._llm_fail_note(_read_timeout(), object())
    assert "제한 시간 안에 끝나지 않았다(APITimeoutError)" in note and knob == _TIMEOUT_KNOB, note
