# 웹 심의의 주제(delib_opts.subject) — 엔진 앞에서 그 사람의 자격으로 다시 풀고 delib 사건(kind=subject)으로 먼저 낸 뒤, 엔진에는 넘기지 않는지 본다
"""HWAXPortal docs/project-insight-graph PLAN §4.1 · §4.3(웹) — 브라우저가 보낸 주제는 믿지 않는다. 에이전트 서버가 MCP 길과 같은 함수
(delib_subject.confirm)로 다시 풀고, 확인한 주제를 사건으로 낸다(포털 중계가 답 meta.subject 로 남긴다). 새 LLM 호출이 없다. 세 트리거
(/심의 · /시뮬심의 · /시험계획)가 다 지난다. 리스크 심사 계열은 달지 않는다. 엔진에는 subject 가 가지 않는다 — 결정문 · RA 보고서에 실릴 길이 없다.

  실행:  .venv/bin/python -m pytest tests/test_delib_subject_web.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402
import delib_subject as s  # noqa: E402

_FIX = json.loads((Path(__file__).resolve().parent / "fixtures" / "subject" / "find_plm_task.json").read_text(encoding="utf-8"))
KEY = "알파 개발_C2"
PAT = "pat-EXAMPLE-0000"


@pytest.fixture
def hub(monkeypatch):
    calls = []

    async def call(_app, *, groups, user, user_pat, args):
        calls.append({"groups": list(groups), "user": user, "user_pat": user_pat, "query": args["query"]})
        return False, json.dumps(_FIX["exact_and_partial"]["body"], ensure_ascii=False)
    monkeypatch.setattr(s, "call_resolver", call)
    return calls


@pytest.fixture
def engines(monkeypatch):
    """세 진입 함수의 가짜 — 받은 opts 를 적고 결정문 하나를 낸다. 늘 엔진에 무엇이 갔는지 본다."""
    seen = {}

    def make(name):
        async def entry(_app, q, groups, opts, user_email, user_pat, _h=None, **kw):
            seen[name] = {"q": q, "opts": opts}
            yield a._sse("delib", {"kind": "decision", "text": f"{name} 결정문"})
            yield a._sse("done", {})
        return entry
    for name in ("run_deliberation", "run_sim_deliberation", "run_test_plan"):
        monkeypatch.setattr(a, name, make(name))

    def capture(stream, label, started_by="human", conv="", user=""):
        seen.setdefault("_streams", []).append(stream)

        async def empty():
            if False:
                yield b""
        return empty()
    monkeypatch.setattr(a, "_detach_stream", capture)       # 분리 태스크 대신 감싼 생성기를 손으로 돈다
    return seen


def _drain(stream) -> list:
    async def go():
        return [delib_jobs._parse_sse(c) async for c in stream]
    return asyncio.run(go())


def _chat(message, opts, **kw):
    return asyncio.run(a.chat(a.ChatRequest(message=message, delib_opts=opts, groups=["CAEG"], user_email="me@example.com",
                                            user_pat=PAT, **kw)))


@pytest.mark.parametrize("message,entry", [("/심의 알파 힌지 크랙", "run_deliberation"), ("/시뮬심의 알파 낙하 재현", "run_sim_deliberation"),
                                           ("/시험계획 알파 접착 물성", "run_test_plan")])
def test_세_트리거_모두_주제를_그_사람의_자격으로_풀어_먼저_내고_엔진에는_넘기지_않는다(hub, engines, message, entry):
    subj = {"sp_task": KEY, "round": "dv1", "text": "알파 1차"}
    _chat(message, {"subject": subj, "rounds": 3, "wiki": 0})
    events = _drain(engines["_streams"][-1])
    kinds = [(ev, d.get("kind")) for ev, d in events]
    first_delib = next(d for ev, d in events if ev == "delib")
    assert first_delib["kind"] == "subject", f"주제 사건이 엔진 사건보다 먼저다: {kinds}"
    rec = first_delib["subject"]
    assert rec["sp_state"] == s.CONFIRMED and rec["sp_task"] == KEY and rec["round"] == "dv1" and rec["text"] == "알파 1차"
    steps = [d.get("step") for ev, d in events if ev == "status"]
    assert any("확인하는 중" in (x or "") for x in steps) and any("허브 거울 풀이로 확인" in (x or "") for x in steps)
    assert hub == [{"groups": ["CAEG"], "user": "me@example.com", "user_pat": PAT, "query": KEY}], "웹은 그 사람의 PAT 로 한 번"
    got = engines[entry]["opts"]
    assert "subject" not in got and got.get("rounds") == 3, "엔진에는 subject 를 빼고 나머지 손잡이는 그대로 넘긴다"
    assert ("delib", "decision") in kinds


def test_브라우저가_확인_상태를_지어_보내면_달지_않고_말한다(hub, engines):
    _chat("/심의 화두", {"subject": {"sp_task": KEY, "sp_state": "confirmed", "sp_resolved_by": {"tool": "x"}}})
    events = _drain(engines["_streams"][-1])
    assert not [d for ev, d in events if ev == "delib" and d.get("kind") == "subject"], "지어낸 확인 상태로 주제를 달았다"
    assert any("주제를 달지 않았다" in (d.get("step") or "") and "모르는 칸" in (d.get("step") or "") for ev, d in events if ev == "status")
    assert hub == [] and "subject" not in engines["run_deliberation"]["opts"]


@pytest.mark.parametrize("opts", [{"chair_template": "risk-review"}, {"started_by": "app"}])
def test_리스크_심사_계열은_주제를_달지_않는다(hub, engines, opts):
    _chat("/심의 변경 심사", {**opts, "subject": {"sp_task": KEY}})
    events = _drain(engines["_streams"][-1])
    assert not [d for ev, d in events if ev == "delib" and d.get("kind") == "subject"] and hub == []
    assert any("리스크 심사는 주제를 달지 않는다" in (d.get("step") or "") for ev, d in events if ev == "status")
    assert "subject" not in engines["run_deliberation"]["opts"] and engines["run_deliberation"]["opts"].get(
        next(iter(opts))) == next(iter(opts.values())), "주제만 빼고 손잡이는 그대로"


def test_과제가_없는_주제는_풀지_않고_없음으로_낸다(hub, engines):
    _chat("/심의 화두", {"subject": {"text": "알파 관련", "round": "pv2"}})
    events = _drain(engines["_streams"][-1])
    rec = next(d["subject"] for ev, d in events if ev == "delib" and d.get("kind") == "subject")
    assert rec["sp_state"] == s.NONE and rec["sp_task"] is None and rec["round"] == "pv2" and hub == []


def test_주제가_없는_웹_심의는_종전_그대로다(hub, engines):
    _chat("/심의 화두", {"rounds": 4})
    events = _drain(engines["_streams"][-1])
    assert [ev for ev, _d in events] == ["delib", "done"] and hub == []
    assert engines["run_deliberation"]["opts"] == {"rounds": 4}
    _chat("/심의 화두", None)
    assert [ev for ev, _d in _drain(engines["_streams"][-1])] == ["delib", "done"]
