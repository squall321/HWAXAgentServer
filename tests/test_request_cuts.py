# 요청에 실린 값을 상한에서 줄였으면 알린다 — 이전 요약·양보 불가 조항·후보안·지정 도구·앱·좌석
#
# 근거(evidence)와 사람 의견(human_note)은 잘리면 카드가 나가게 됐는데, 같은 자리(_resolve_opts)의 나머지
# 값들은 여전히 말없이 잘렸다. 지정 도구를 8개 보낸 호출자는 6개만 돌았다는 것을, 이어하기를 부른 사람은
# 이전 결정문이 앞 8,000자만 실렸다는 것을 알 길이 없었다(1차 구현 결과의 후속 항목).
# 좌석 상한 초과는 상태줄로만 나가, MCP 호출자에게는 원장의 최근 30줄 창 밖으로 밀려나 사라졌다.
#
# 줄이는 값은 그대로다 — 여기서 보는 것은 **줄였다는 사실이 화면과 잡 원장에 남는가** 다.
#
#   실행:  .venv/bin/python -m pytest tests/test_request_cuts.py -q
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _cards, _mcp_view, _pin_context, _SEATS, _steps, _stream  # noqa: E402, F401

_CARD = "요청 값 상한 초과"


def _cut(req):
    return d._resolve_opts(req).req_cut


# ── 무엇을 줄였는지 적어 둔다 ────────────────────────────────────────────────────
def test_이전_요약을_줄였으면_몇_자_중_몇_자인지_적는다():
    note = _cut({"continue_summary": "가" * 9000})["continue_summary"]
    assert "9,000자 중 앞 8,000자만" in note, note


def test_지정_도구와_앱은_빠진_이름까지_적는다():
    """몇 개가 빠졌는지만 적으면 어느 도구가 안 돌았는지 다시 세어 봐야 한다."""
    cut = _cut({"tools": [f"tool_{i}" for i in range(8)], "apps": ["a", "b", "c", "d", "e"]})
    assert f"8개 중 앞 {d._TOOLS_MAX}개만" in cut["tools"] and "tool_6, tool_7" in cut["tools"], cut
    assert "tool_5" not in cut["tools"], "돌린 도구를 빠졌다고 적었다"
    assert f"5개 중 앞 {d._APPS_MAX}개만" in cut["apps"] and "d, e" in cut["apps"], cut


def test_후보안은_건수와_길이를_따로_적는다():
    cut = _cut({"options": [f"{i}안" for i in range(9)] + ["긴 안 " + "나" * 500]})
    assert f"10개 중 앞 {d._OPTIONS_MAX}개만" in cut["options"], cut
    assert "400자" not in cut["options"], "빠진 안의 길이를 줄였다고 적었다"
    cut = _cut({"options": ["1안", "긴 안 " + "나" * 500]})
    assert "1개" in cut["options"] and "400자" in cut["options"], cut


def test_양보_불가_조항도_건수와_길이를_적는다():
    cut = _cut({"non_negotiables": [f"조항 {i}" for i in range(13)] + ["다" * 1500]})
    assert "14개 중 앞 12개만" in cut["non_negotiables"], cut
    cut = _cut({"non_negotiables": ["다" * 1500, "짧은 조항"]})
    assert "1개" in cut["non_negotiables"] and "1,200자" in cut["non_negotiables"], cut


def test_좌석은_빠진_키와_설정_이름을_적고_긴_역할도_적는다(monkeypatch):
    monkeypatch.setattr(d, "MAX_REQ_SEATS", 3)
    seats = [{"key": f"mech-s{i}", "role": "역할"} for i in range(5)]
    seats[1]["role"] = "라" * 2500
    cut = _cut({"personas": seats})["personas"]
    assert "5석 중 앞 3석만" in cut and "mech-s3, mech-s4" in cut and "DELIB_MAX_SEATS" in cut, cut
    assert "역할" in cut and "2,000자" in cut, cut


def test_줄인_것이_없으면_비어_있다():
    assert _cut({"continue_summary": "가" * 8000, "tools": ["a"] * 6, "apps": ["a"] * 3,
                 "options": ["안"] * 8, "non_negotiables": ["조항"] * 12, "personas": _SEATS}) == {}
    assert _cut(None) == {} and d._DEFAULT_OPTS.req_cut == {}


def test_줄이는_값은_그대로다():
    """알리기만 한다 — 상한을 바꾼 것이 아니다."""
    o = d._resolve_opts({"continue_summary": "가" * 9000, "tools": [f"t{i}" for i in range(8)],
                         "apps": list("abcde"), "options": [f"{i}안" for i in range(10)],
                         "non_negotiables": [f"조항 {i}" for i in range(14)]})
    assert (len(o.continue_summary), len(o.delib_tools), len(o.delib_apps), len(o.options),
            len(o.continue_non_negotiables)) == (8000, 6, 3, 8, 12)


# ── 화면과 잡 원장에 남는다 ─────────────────────────────────────────────────────
def test_줄였으면_카드_하나로_알리고_MCP_호출자에게도_보인다(monkeypatch):
    req = {"continue_summary": "가" * 9000, "apps": list("abcde"), "options": [f"{i}안" for i in range(10)]}
    events = _stream(monkeypatch, req)
    out = [c for c in _cards(events, included=False) if c["source"] == _CARD]
    assert len(out) == 1, [c["source"] for c in _cards(events, included=False)]
    for want in ("9,000자 중 앞 8,000자만", "5개 중 앞 3개만", "10개 중 앞 8개만"):
        assert want in out[0]["text"], (want, out[0]["text"])
    for name, view in _mcp_view(monkeypatch, events).items():
        assert _CARD in [x["source"] for x in view["evidence_omitted"]], (name, view["evidence_omitted"])


def test_좌석_상한_초과가_원장에도_남는다(monkeypatch):
    """상태줄로만 나가면 MCP 호출자에게는 최근 30줄 창 밖으로 밀려나 사라진다."""
    monkeypatch.setattr(d, "MAX_REQ_SEATS", 2)
    events = _stream(monkeypatch, {"personas": _SEATS + [{"key": "therm-c", "role": "열"}]})
    assert any("좌석 상한 2석 초과" in s and "therm-c" in s for s in _steps(events)), "종전 상태줄이 사라졌다"
    for name, view in _mcp_view(monkeypatch, events).items():
        card = next((x for x in view["evidence_omitted"] if x["source"] == _CARD), None)
        assert card and "therm-c" in card["text"] and "3석 중 앞 2석만" in card["text"], (name, card)


# ── 지정 반대석은 좌석 상한에 세지 않는다 ───────────────────────────────────────
# 엔진은 지정 반대석을 상한 **밖에서** 얹는다(20석을 청하면 21석으로 돈다). 이어하기는 그 21석을 그대로
# 되넘기므로, 반대석까지 세면 꽉 찬 패널을 이어갈 때마다 '21석 중 앞 20석만 — 뺀 것: 반대석' 이 원장에
# 남았다. 그 좌석은 곧바로 다시 앉는다 — 앉아 있는 좌석을 뺐다고 적은 거짓 기록이고, 읽는 사람은 상한
# 설정(DELIB_MAX_SEATS)을 올리려 든다.
_ADV = "delib-baseline-defender"        # 리스크 심사의 지정 반대석


def _seated(events):
    return next(data["personas"] for ev, data in events if ev == "delib" and data.get("kind") == "personas")


def _three():
    return [{"key": f"sim-s{i}", "role": "역할"} for i in range(3)]


def test_꽉_찬_패널을_이어가도_앉아_있는_반대석을_뺐다고_적지_않는다(monkeypatch):
    monkeypatch.setattr(d, "MAX_REQ_SEATS", 3)
    first = _seated(_stream(monkeypatch, {"personas": _three(), "chair_template": "risk-review"}))
    assert [p["key"] for p in first] == ["sim-s0", "sim-s1", "sim-s2", _ADV], "시험 전제 — 상한 3석에 반대석이 얹힌다"
    # 이어하기는 이전 좌석을 그대로 넘긴다(mcp_server.deliberate_continue · 포털 continueDeliberation).
    assert d._resolve_opts({"personas": first, "chair_template": "risk-review"}).seats_clamped == []
    events = _stream(monkeypatch, {"personas": first, "chair_template": "risk-review"})
    again = _seated(events)
    assert [p["key"] for p in again] == [p["key"] for p in first], "이어간 패널의 좌석이 달라졌다"
    assert again[-1]["origin"] == "adversary", again[-1]
    assert not [s for s in _steps(events) if "좌석 상한" in s], [s for s in _steps(events) if "좌석" in s]
    assert not [c for c in _cards(events, included=False) if c["source"] == _CARD], (
        [c["text"] for c in _cards(events, included=False)])


def test_반대석을_빼고도_넘친_좌석은_종전대로_적는다(monkeypatch):
    monkeypatch.setattr(d, "MAX_REQ_SEATS", 3)
    seats = [{"key": f"sim-s{i}", "role": "역할"} for i in range(4)] + [{"key": _ADV, "role": "반대", "origin": "adversary"}]
    o = d._resolve_opts({"personas": seats, "chair_template": "risk-review"})
    assert o.seats_clamped == ["sim-s3"] and [p["key"] for p in o.continue_personas] == ["sim-s0", "sim-s1", "sim-s2"]
    assert "4석 중 앞 3석만" in o.req_cut["personas"] and _ADV not in o.req_cut["personas"], o.req_cut
    events = _stream(monkeypatch, {"personas": seats, "chair_template": "risk-review"})
    assert [p["key"] for p in _seated(events)] == ["sim-s0", "sim-s1", "sim-s2", _ADV]


@pytest.mark.parametrize("template", ["diagnosis", None])
def test_이번_심의의_반대석이_아니면_넘친_좌석으로_센다(monkeypatch, template):
    """다른 Job 으로 이어가면 옛 반대석은 엔진이 다시 앉히지 않는다 — 그때 '뺐다' 는 참이라 적어야 한다."""
    monkeypatch.setattr(d, "MAX_REQ_SEATS", 3)
    seats = _three() + [{"key": _ADV, "role": "반대", "origin": "adversary"}]
    o = d._resolve_opts({"personas": seats, **({"chair_template": template} if template else {})})
    assert o.seats_clamped == [_ADV] and _ADV in o.req_cut["personas"], (o.seats_clamped, o.req_cut)


def test_줄인_것이_없으면_카드도_없다(monkeypatch):
    events = _stream(monkeypatch, {"continue_summary": "짧은 요약", "options": ["1안", "2안"]})
    assert not [c for c in _cards(events, included=False) if c["source"] == _CARD]


# ── 다단 심의 — 엔진이 갈아 끼운 값에 대해서는 말하지 않는다 ────────────────────────────
def _stages(monkeypatch, run, req):
    """다단 진입 함수를 돌려 단마다 코어가 받은 opts 를 모은다(코어·조회는 가짜)."""
    got = []

    async def _core(_app, _q, _groups, opts, *_a, out=None, **_k):
        got.append(opts)
        if out is not None:
            out.update(decision="결정문", personas=[{"key": "mech-a", "role": "기구"}], non_negotiables=[])
        yield d._sse("status", {"step": "단 끝"})

    async def _none(*_a, **_k):
        return {}

    async def _text(*_a, **_k):
        return ""

    async def _empty(*_a, **_k):
        return []

    monkeypatch.setattr(d, "_deliberation_stream", _core)
    monkeypatch.setattr(d, "_tools_by_name", _none)
    monkeypatch.setattr(d, "_restore_role", _text)
    monkeypatch.setattr(d, "_discover", _empty)
    monkeypatch.setattr(d, "_asset_snapshot", _text)
    monkeypatch.setattr(d, "_material_evidence_snapshot", _text)
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None))

    async def go():
        return [chunk async for chunk in run(stub, "화두", [], req)]

    events = [delib_jobs._parse_sse(c) for c in asyncio.run(go())]
    assert not [data for ev, data in events if ev == "error"], events
    return got


_OVER = {"tools": [f"t{i}" for i in range(8)], "continue_summary": "가" * 9000,
         "personas": [{"key": "mech-a", "role": "라" * 2500}, {"key": "rel-b", "role": "신뢰성"}]}


def test_해석_설계는_첫_단에서만_알린다(monkeypatch):
    """2단부터는 요약·좌석·사람 의견을 엔진이 갈아 끼운다 — 거기서 또 '요청을 줄였다' 고 적으면
    같은 카드가 단마다 나가고, 갈아 끼운 값에 대해서는 거짓이다."""
    a, b, c = _stages(monkeypatch, d.run_sim_deliberation, {**_OVER, "build_plan": 1})
    assert set(a.req_cut) == {"tools", "continue_summary", "personas"}
    assert b.req_cut == {} and c.req_cut == {}


def test_해석_설계는_읽지_못한_값과_키_없는_좌석도_첫_단에서만_알린다(monkeypatch):
    """줄인 값과 같다 — 단마다 엔진이 옵션을 다시 읽으므로 지우지 않으면 같은 카드가 단마다 또 나간다.
    지정 좌석은 1단에 앉는다. 2단부터의 좌석은 엔진이 세운 것이라 '키가 없어 못 앉혔다' 가 거기엔 없는 말이다."""
    req = {**_OVER, "build_plan": 1, "rounds": "여러 번",
           "personas": _OVER["personas"] + [{"name": "키 없는 좌석"}]}
    a, b, c = _stages(monkeypatch, d.run_sim_deliberation, req)
    assert "rounds" in a.req_unread and a.seats_no_key and a.seats_no_key[:2] == (3, 1), (a.req_unread, a.seats_no_key)
    for later in (b, c):
        assert later.req_unread == {} and later.seats_no_key is None, (later.req_unread, later.seats_no_key)


def test_시험_설계는_키_없는_좌석을_못_앉혔다는_것을_그대로_알린다(monkeypatch):
    """시험 설계는 단이 하나이고 지정 좌석을 실제로 앉힌다 — 못 앉힌 좌석이 있으면 거기서 말해야 한다."""
    (o,) = _stages(monkeypatch, d.run_test_plan, {"personas": [{"key": "mech-a"}, {"name": "키 없는 좌석"}]})
    assert o.seats_no_key and o.seats_no_key[:2] == (2, 1), o.seats_no_key
    assert [p["key"] for p in o.continue_personas] == list(d._TEST_FIXED) + ["mech-a"]


def test_시험_설계는_지정_좌석을_고정_좌석_뒤에_앉히므로_줄인_것도_그대로_알린다(monkeypatch):
    """종전엔 호출자 좌석을 고정 좌석으로 갈아 끼웠다 — 그때는 '줄였다' 가 거짓이라 지웠다. 이제 앉히므로
    역할을 상한에서 줄였다는 것은 참이고, 알려야 한다."""
    (o,) = _stages(monkeypatch, d.run_test_plan, dict(_OVER))
    assert [p["key"] for p in o.continue_personas] == list(d._TEST_FIXED) + ["mech-a", "rel-b"]
    assert set(o.req_cut) == {"tools", "continue_summary", "personas"}, o.req_cut


# ── 이어하기 — 이전 결정문을 미리 자르지 않는다 ─────────────────────────────────────
def test_이어하기는_이전_결정문을_통째로_넘기고_줄인_사실은_엔진이_알린다(monkeypatch, tmp_path):
    """도구가 앞 8,000자만 떼어 넘기면 엔진은 줄어든 줄 모른다 — 알릴 수도 없다."""
    prev = {"id": "prev-1", "job": "default", "status": "done", "question": "힌지 크랙 원인",
            "decision": "마" * 9000, "seats": list(_SEATS), "opts": {}, "started_at": 0.0}
    got = {}

    async def _entry(_app, _q, _groups, opts, *_a):
        got.update(opts or {})
        yield d._sse("done", {})

    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {prev["id"]: prev})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    monkeypatch.setattr(delib_jobs, "_PENDING", {})
    monkeypatch.setattr(d, "run_deliberation", _entry)
    monkeypatch.setattr(m, "_APP", object())

    async def go():
        out = await m.deliberate_continue("prev-1", "두께를 다시 보라")
        await delib_jobs._TASKS[out["job_id"]]

    asyncio.run(go())
    assert len(got["continue_summary"]) == 9000
    o = d._resolve_opts(got)
    assert len(o.continue_summary) == 8000 and "9,000자 중 앞 8,000자만" in o.req_cut["continue_summary"]
