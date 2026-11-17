# 심의 기본값 묶음(DL-05) — deliberate_start(preset) 가 엔진의 진짜 인자 이름으로 깔고, 직접 준 인자가 이기며, 최종 값과 출처를 되돌려 준다 · 박스 덧씌우기
"""HWAXPortal docs/request-1117 (DL-05).

  · 첫 묶음 risk-review-batch — job risk-review · rounds 2 · modifiers premortem · advanced voc off · free_tools 0 · timeout_s 3600. 엔진(_resolve_opts)이
    그 이름 · 그 값으로 읽는지 본다('mode: credibility' 처럼 말없이 버려지는 이름을 받지 않는다).
  · 부른 쪽이 직접 준 인자(도구 기본값과 다른 값)가 이긴다 — advanced 는 손잡이마다. preset.applied 가 칸마다 {value, from}.
  · 추적 JSON + 박스 덧씌우기(merge patch · null 은 지운다 · 바뀐 시각으로 다시 읽는다 · 깨지면 버린다). deliberate_jobs 가 목록을 싣는다.

  실행:  .venv/bin/python -m pytest tests/test_dl05_presets.py -q
"""
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import delib_presets as P  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402
from test_delib_job_queue import _caps, _play, _tick, eng  # noqa: E402, F401
from test_delib_same_aff import _ctx  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401

ME = "me@example.com"
NAME = "risk-review-batch"


@pytest.fixture
def overlay(monkeypatch, tmp_path):
    p = tmp_path / "delib_presets.local.json"
    monkeypatch.setattr(P, "LOCAL_PATH", p)
    monkeypatch.setattr(P, "_CACHE", {"key": None, "presets": {}, "errors": [], "overlay": {"present": False}})

    def write(obj, bump=0):
        p.write_text(json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj, encoding="utf-8")
        if bump:
            st = p.stat()
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + bump))
    return write


# ── 첫 묶음 · 진짜 이름 ─────────────────────────────────────────────────────────────────────────────────
def test_첫_묶음은_엔진의_진짜_인자_이름이다():
    spec = P.presets()[NAME]
    assert spec["args"] == {"job": "risk-review", "rounds": 2, "modifiers": ["premortem"],
                            "advanced": {"voc": "off", "free_tools": 0, "timeout_s": 3600}}
    o = d._resolve_opts({**spec["args"]["advanced"], "rounds": 2, "modifiers": ["premortem"]})
    assert (o.voc, o.free_tools, o.timeout_s, o.rounds, o.modifiers) == ("off", 0, 3600.0, 2, ["premortem"])
    assert not o.req_unread and not o.timeout_clamped
    assert delib_jobs.JOBS[spec["args"]["job"]]["chair"] == "risk-review", "심의 종류는 job 이다 — mode 가 아니다"
    assert P.state() == {"errors": [], "overlay": {"present": False}}


@pytest.mark.parametrize("args,msg", [
    ({"mode": "credibility"}, "모르는 인자"),
    ({"job": "nope"}, "알 수 없는 심의 종류"),
    ({"rounds": 12}, "2~8"),
    ({"modifiers": ["nope"]}, "modifiers"),
    ({"advanced": {"vocc": "off"}}, "같은 이름으로 읽는 손잡이가 아니다"),
    ({"advanced": {"voc": "of"}}, "엔진이 그 값으로 읽지 않는다"),
    ({"advanced": {"timeout_s": 10 ** 9}}, "범위 밖"),
])
def test_엔진이_읽지_않는_묶음은_버리고_까닭을_남긴다(overlay, args, msg):
    overlay({"presets": {"bad": {"label": "x", "args": args}}})
    assert "bad" not in P.presets() and NAME in P.presets(), "틀린 묶음만 버린다"
    (err,) = P.state()["errors"]
    assert err.startswith("bad — ") and msg in err, err


# ── 적용 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_직접_준_인자가_이기고_칸마다_출처를_말한다():
    final, echo = P.apply(NAME, {"job": "default", "rounds": 3, "modifiers": None, "save_report": True,
                                 "advanced": {"voc": "always", "anchor": 1}})
    assert final == {"job": "risk-review", "rounds": 3, "modifiers": ["premortem"], "save_report": True,
                     "advanced": {"voc": "always", "anchor": 1, "free_tools": 0, "timeout_s": 3600}}
    assert echo == {"name": NAME, "applied": {
        "job": {"value": "risk-review", "from": "preset"}, "rounds": {"value": 3, "from": "caller"},
        "modifiers": {"value": ["premortem"], "from": "preset"}, "advanced.voc": {"value": "always", "from": "caller"},
        "advanced.free_tools": {"value": 0, "from": "preset"}, "advanced.timeout_s": {"value": 3600, "from": "preset"}}}
    final, echo = P.apply(NAME, {"job": "default", "rounds": 0, "modifiers": [], "save_report": True, "advanced": None})
    assert final["modifiers"] == [] and echo["applied"]["modifiers"]["from"] == "caller", "빈 목록은 '층 없음' 을 직접 준 것이다"
    with pytest.raises(ValueError, match="없다"):
        P.apply("nope", {})


def test_advanced_의_rounds_가_이기면_메아리도_그렇게_말한다(eng, monkeypatch):
    """반대 검토 L7a — advanced.rounds 는 엔진에 가는 손잡이에서 위 인자를 덮는다. 메아리가 preset 의 2 라고 말하면 거짓이다."""
    final, echo = P.apply(NAME, {"job": "default", "rounds": 0, "modifiers": None, "save_report": True, "advanced": {"rounds": 4}})
    assert echo["applied"]["rounds"] == {"value": 4, "from": "caller", "via": "advanced"}
    _caps(monkeypatch, 2)

    async def scenario():
        out = await m.deliberate_start("메아리", preset=NAME, advanced={"rounds": 4}, ctx=_ctx(ME))
        await _tick()
        assert eng.seen["메아리"].opts["rounds"] == 4 and out["preset"]["applied"]["rounds"]["value"] == 4

    _play(scenario)


def test_묶음의_advanced_에는_위_인자_이름을_두지_않는다(overlay):
    overlay({"presets": {"twice": {"args": {"rounds": 2, "advanced": {"rounds": 3}}}}})
    assert "twice" not in P.presets() and "위 인자로 준다" in P.state()["errors"][0]


def test_시작은_묶음을_깔아_엔진에_넘기고_응답에_되돌린다(eng, monkeypatch):
    _caps(monkeypatch, 2)

    async def scenario():
        out = await m.deliberate_start("묶음 화두", preset=NAME, advanced={"timeout_s": 1800}, ctx=_ctx(ME))
        await _tick()
        opts = eng.seen["묶음 화두"].opts
        assert opts["rounds"] == 2 and opts["modifiers"] == ["premortem"] and opts["chair_template"] == "risk-review"
        assert (opts["voc"], opts["free_tools"], opts["timeout_s"]) == ("off", 0, 1800)
        job = delib_jobs.get(out["job_id"])
        assert job["job"] == "risk-review" and job["opts"]["voc"] == "off" and job["opts"]["free_tools"] == 0
        assert out["preset"]["name"] == NAME and out["preset"]["applied"]["advanced.timeout_s"] == {"value": 1800, "from": "caller"}
        plain = await m.deliberate_start("묶음 없음", ctx=_ctx(ME))
        assert "preset" not in plain, "preset 을 안 주면 응답은 종전 그대로"
        with pytest.raises(ValueError):
            await m.deliberate_start("없는 묶음", preset="nope", ctx=_ctx(ME))
        await _tick()

    _play(scenario)


# ── 덧씌우기 ─────────────────────────────────────────────────────────────────────────────────────────────
def test_덧씌우기는_merge_patch_이고_바뀐_시각으로_다시_읽는다(overlay):
    overlay({"presets": {NAME: {"args": {"rounds": 3}}, "quick": {"label": "빠른", "args": {"rounds": 2}}}})
    ps = P.presets()
    assert ps[NAME]["args"]["rounds"] == 3 and ps[NAME]["args"]["modifiers"] == ["premortem"], "객체는 재귀로 합친다"
    assert ps["quick"]["args"] == {"rounds": 2} and P.state()["overlay"] == {"present": True}
    overlay({"presets": {NAME: None}}, bump=10_000_000)
    assert NAME not in P.presets(), "null 은 그 칸을 지운다 — 바뀐 시각으로 다시 읽었다"


def test_깨진_덧씌우기는_버리고_추적_표로_돈다(overlay):
    overlay("{ 깨짐")
    assert NAME in P.presets() and "덧씌우기를 버렸다" in P.state()["overlay"]["error"]


def test_메뉴가_묶음을_싣고_문제가_있을_때만_사정을_싣는다(overlay):
    menu = asyncio.run(m.deliberate_jobs(ctx=_ctx(ME)))
    assert [p["name"] for p in menu["presets"]] == [NAME] and "presets_state" not in menu
    assert menu["presets"][0]["args"]["advanced"] == {"voc": "off", "free_tools": 0, "timeout_s": 3600}
    overlay({"presets": {"bad": {"args": {"mode": "credibility"}}}})
    menu = asyncio.run(m.deliberate_jobs(ctx=_ctx(ME)))
    assert menu["presets_state"]["errors"] and "bad" not in [p["name"] for p in menu["presets"]]


def test_시작_설명이_묶음을_가리킨다():
    t = next(x for x in asyncio.run(m.mcp.list_tools()) if x.name == "deliberate_start")
    assert "preset" in t.inputSchema["properties"] and t.inputSchema["properties"]["preset"].get("default") is None
    assert "preset" in t.description and NAME in t.description
    assert "준 것으로 보지 않는다" in t.description, "기본값과 같은 인자는 묶음을 이기지 않는다는 규칙을 도구 설명에 적는다"


def test_advanced_의_job_은_위_인자를_이기지_않는다():
    """엔진이 advanced 로 읽는 것은 rounds · modifiers · save_report 뿐이다 — advanced.job 을 'caller via advanced' 로 말하면 거짓이다(2차 검토)."""
    final, echo = P.apply(NAME, {"job": "default", "rounds": 0, "modifiers": None, "save_report": True, "advanced": {"job": "diagnosis"}})
    assert final["job"] == "risk-review" and echo["applied"]["job"] == {"value": "risk-review", "from": "preset"}
