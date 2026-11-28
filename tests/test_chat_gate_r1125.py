# 운영 요청 모음 11-24(HWAXPortal docs/request-1125 1-3) — 웹 챗 관문 WD-02 · WD-06 · WD-07 · WD-13 시험(가짜는 test_confirm_gate 의 것)
#
#   실행:  .venv/bin/python -m pytest tests/test_chat_gate_r1125.py -q
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import app as a  # noqa: E402

from test_confirm_gate import _gw, _held, _run, gate, maps, turn_ctx  # noqa: E402, F401


# ── WD-07 RA 보고서 쓰기는 운영자와 상관없이 늘 카드 ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["create_report_draft", "update_report_draft", "add_report_tags", "publish_report",
                                  "create_report_from_format", "reportarchive_create_report_draft"])
def test_WD07_RA_쓰기는_운영자_정의_없이도_confirm_이다(gate, name):
    kind, hit = a._confirm_class(name)
    assert kind == "confirm" and name.endswith(hit)


def test_WD07_운영자를_고르지_않은_챗에서_RA_초안을_만들라고_해도_카드가_먼저다(turn_ctx):
    reached: list = []
    t = _gw("create_report_draft", reached, props={"title": {"type": "string"}})
    turn_ctx("RA 초안 만들어줘")
    out = _run(t, {"title": "S26 낙하 정리"})
    assert _held(out) and reached == [], "주문 말로도 통과하지 않는다(confirm)"


def test_WD07_RA_읽기는_그대로_간다(turn_ctx):
    reached: list = []
    t = _gw("get_report", reached, props={"report_id": {"type": "integer"}})
    turn_ctx("보고서 보여줘")
    assert not _held(_run(t, {"report_id": 7})) and reached == [("get_report", {"report_id": 7})]


# ── WD-06 심의 확인 카드의 요약 · 카드에서 바꾸기 ────────────────────────────────────────────────────────────────────
from test_confirm_gate import _GateAgent, _card_turn, _result, _turn  # noqa: E402

DELIB_ARGS = {"question": "S26 낙하 화면 깨짐의 원인", "job": "default", "rounds": 3,
              "personas": [{"name": "기구 구조"}, {"name": "재료"}, {"name": "신뢰성 시험"}],
              "evidence": [{"source": "VOC", "result": "화면 깨짐 1,234건"}, {"source": "시험", "result": "1.5 m 낙하 실패"}],
              "apps": ["signalforge"], "save_report": False}


def test_WD06_심의_카드_맨_위에_종류_라운드_좌석_저장_근거를_고정해_보인다(gate, monkeypatch):
    reached: list = []
    tools = {"deliberate_start": _gw("deliberate_start", reached)}
    evs, card, hist = _card_turn(monkeypatch, tools, [("deliberate_start", DELIB_ARGS)])
    sm = card["calls"][0]["summary"]
    assert sm["job"] == "default" and sm["rounds"] == 3 and sm["seats"] == ["기구 구조", "재료", "신뢰성 시험"] and sm["seats_n"] == 3
    assert sm["save_report"] is False and sm["evidence_n"] == 2 and sm["evidence_chars"] > 0 and sm["apps"] == ["signalforge"]
    assert sm["editable"] == ["save_report", "job", "rounds"]
    text = _result(evs)
    assert "심의 — 종류 **default** · 라운드 3 · 좌석 3(기구 구조, 재료, 신뢰성 시험) · RA 저장 끔 · 근거 2건" in text
    assert reached == []


def test_WD06_카드에서_저장을_켜고_종류를_바꿔_승인하면_고친_인자로_한_번_간다(gate, monkeypatch):
    reached: list = []
    tools = {"deliberate_start": _gw("deliberate_start", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("deliberate_start", DELIB_ARGS)])
    ag = _GateAgent(tools, steps=[], say="심의를 열었습니다.")
    edit = {"id": card["id"], "set": {"save_report": True, "job": "diagnosis", "bogus": 1}}
    evs, _seen = _turn(monkeypatch, ag, tools, card["approve_text"], hist, confirm_edit=edit)
    assert len(reached) == 1
    sent = reached[0][1]
    assert sent["save_report"] is True and sent["job"] == "diagnosis" and sent["question"] == DELIB_ARGS["question"]
    steps = [d.get("step", "") for e, d in evs if e == "status"]
    assert any("카드에서 바꿈" in s and "save_report" in s and "bogus" in s for s in steps)


def test_WD06_다른_카드의_고침_틀린_값은_버린다(gate):
    calls = [{"name": "deliberate_start", "base": "deliberate_start", "args": dict(DELIB_ARGS), "canon": a._confirm_canon(DELIB_ARGS)}]
    assert a._confirm_apply_edit("cf-000000000000", calls, {"id": "cf-111111111111", "set": {"save_report": True}}) == []
    notes = a._confirm_apply_edit("cf-000000000000", calls, {"id": "cf-000000000000", "set": {"job": "없는종류", "rounds": 99}})
    assert len(notes) == 2 and calls[0]["args"]["job"] == "default" and calls[0]["args"]["rounds"] == 3
    other = [{"name": "run_job", "base": "run_job", "args": {"kind": "graph"}, "canon": "{}"}]
    assert a._confirm_apply_edit("cf-0", other, {"id": "cf-0", "set": {"save_report": True}}) == ["이 카드에는 심의 호출이 없어 고친 칸을 버렸다"]


# ── WD-13 확인 뒤 진행 카드 ──────────────────────────────────────────────────────────────────────────────────────
import json  # noqa: E402

from test_confirm_gate import P1_GRAPH  # noqa: E402


def test_WD13_승인한_잡이_돌기_시작하면_진행_카드를_내고_LLM_에는_짧은_머리만(gate, monkeypatch):
    reached: list = []
    started = json.dumps({"job_id": "J-77", "status": "queued", "url": "/apps/step_forge/jobs/J-77", "log": "가" * 9000}, ensure_ascii=False)
    tools = {"run_job": _gw("run_job", reached, reply=lambda n, args: started)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    ag = _GateAgent(tools, steps=[], say="잡을 걸었습니다.")
    evs, _seen = _turn(monkeypatch, ag, tools, card["approve_text"], hist)
    cards = [d for e, d in evs if e == "job_card"]
    assert len(cards) == 1
    jc = cards[0]
    assert jc["job_ids"] == ["J-77"] and jc["state"] == "queued" and jc["phase"] == "run" and jc["kind"] == "job"
    assert jc["link"] == "/apps/step_forge/jobs/J-77" and jc["call"] == f"{card['id']}-1" and "status_call" not in jc
    human = ag.inputs[0]["messages"][-1][1]
    assert "진행 카드를 띄웠다" in human and "가" * 3000 not in human, "카드를 띄운 잡은 결과를 6,000자로 다시 주지 않는다"


def test_WD13_잡이_아닌_결과는_카드가_없고_종전처럼_6000자(gate, monkeypatch):
    reached: list = []
    tools = {"run_job": _gw("run_job", reached, reply=lambda n, args: "그냥 글 결과 " + "나" * 3000)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    ag = _GateAgent(tools, steps=[], say="끝.")
    evs, _seen = _turn(monkeypatch, ag, tools, card["approve_text"], hist)
    assert not [d for e, d in evs if e == "job_card"]
    assert "나" * 3000 in ag.inputs[0]["messages"][-1][1]


def test_WD13_심의_잡은_진행_조회_호출을_함께_싣는다():
    c = {"name": "deliberate_start", "base": "deliberate_start", "app": "심의"}
    jc = a._confirm_job_card(c, "cf-1-1", json.dumps({"job_id": "delib-20261128-101010-abc123", "status": "running", "stage": "seats"}))
    assert jc["kind"] == "deliberation" and jc["status_call"] == {"tool": "deliberate_status", "args": {"job_id": "delib-20261128-101010-abc123"}}
    assert a._confirm_job_card(c, "x", json.dumps({"job_id": "J", "status": "done"})) is None, "끝난 잡 결과는 카드가 아니다"
