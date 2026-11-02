# 과제 인사이트의 카드 거리 — 엔진이 단마다 코드로 짓고(새 LLM 호출 0) 잡 원장 · 끝난 잡 창구 · 웹 무장 · 주제 다시 풀기 창구로 나가는지 본다
"""HWAXPortal docs/project-insight-graph M2 · contracts §1 ~ §3.

  · 주장은 엔진이 이미 지은 '■ 핵심 요약' 불릿, 없으면 '■ 쉬운 설명' 의 '### 한마디로' 줄이다(결정문 머리 · 코드 표지는 쓰지 않는다). 되살린 단은
    결정문에서 읽는다.
  · 무장(INSIGHT_ARM)한 심의만 카드 거리를 낸다 — 주제가 확인 · 주장이고 리스크 심사 계열이 아닐 때. 주제가 없으면 사건이 하나도 늘지 않는다.
  · 잡 원장에는 단마다 하나(job.insights), 끝난 잡 창구는 공유 비밀 · 끝남 · 주인 · 주제 · 리스크 심사 거름을 지난 잡만 낸다.
  · 주제 다시 풀기 창구는 사람 PAT 를 요구하고 M1 의 확인 규칙(delib_subject.confirm) 하나를 쓴다.

  실행:  .venv/bin/python -m pytest tests/test_insight_cards.py -q
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402
import delib_subject as s  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_job_queue import _caps, _end, _play, _tick, eng  # noqa: E402, F401
from test_delib_same_aff import _ctx  # noqa: E402
from test_delib_sealed import _SEAT_JSON, _Tripwire  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401

_FIX = json.loads((ROOT / "tests" / "fixtures" / "subject" / "find_plm_task.json").read_text(encoding="utf-8"))
KEY = "알파 개발_C2"
ME = "me@example.com"
SUMMARY = "- 리브를 1.2 mm 로 보강한다\n- 근거: 낙하 1.5 m 에서 응력 412 MPa\n- 소수 의견: 시험 먼저"
PLAIN = "### 한마디로\n리브를 보강하라는 결정이다\n### 왜 그런가\n- 응력이 높다"


@pytest.fixture
def hub(monkeypatch):
    async def call(_app, *, groups, user, user_pat, args):
        body = _FIX["exact_and_partial"]["body"] if args["query"] == KEY else _FIX["no_match"]["body"]
        return False, json.dumps(body, ensure_ascii=False)
    monkeypatch.setattr(s, "call_resolver", call)


# ── 주장 ─────────────────────────────────────────────────────────────────────────────────────
def test_주장은_핵심_요약_불릿이고_없으면_쉬운_설명의_한마디로다():
    assert d.insight_claim("결정문", SUMMARY, PLAIN) == (SUMMARY, "summary")
    assert d.insight_claim("결정문", "", PLAIN) == ("리브를 보강하라는 결정이다", "plain")
    assert d.insight_claim("결정 — 보강한다.", "", "") == ("", ""), "결정문 머리를 결론으로 짐작하지 않는다"
    assert d.insight_claim("결정문", "", "### 왜 그런가\n- 응력") == ("", ""), "한마디로 머리가 없으면 비운다"


def test_되살린_단은_결정문에서_코드_표지를_건너뛰고_요약을_읽는다():
    dec = (f"■ 외부 응답자 실행 — 외부\n\n■ 봉인 실행 — 봉인\n\n■ 기준일 — 2026-10-01\n\n■ 핵심 요약\n{SUMMARY}\n\n결정 — 보강한다."
           f"\n\n---\n\n■ 쉬운 설명\n{PLAIN}")
    assert d.insight_claim(dec) == (SUMMARY, "summary")
    gap = dec.replace("\n- 근거", "\n\n- 근거")
    assert d.insight_claim(gap)[0] == SUMMARY, "불릿 사이 빈 줄은 다음 줄이 불릿이면 넘는다"
    no_sum = "■ 봉인 실행 — 봉인\n\n결정 — 보강한다.\n\n---\n\n■ 쉬운 설명\n" + PLAIN
    assert d.insight_claim(no_sum) == ("리브를 보강하라는 결정이다", "plain")
    assert d.insight_claim("결정 ■ 핵심 요약\n- 가운데에 있는 줄") == ("", ""), "맨 위가 아니면 요약이 아니다"


def test_주장은_500자_안으로_줄_경계에서_자른다():
    long = "\n".join(f"- 근거 {i} " + "가" * 40 for i in range(30))
    got, src = d.insight_claim("", long)
    assert src == "summary" and len(got) <= d._INSIGHT_CLAIM_MAX and got.endswith("…")
    assert got[:-1].split("\n")[-1].startswith("- 근거"), "줄 가운데서 자르지 않는다"


# ── 칸 ───────────────────────────────────────────────────────────────────────────────────────
def _card(**kw):
    base = dict(stage="diagnosis", decision="결정", summary=SUMMARY, plain=PLAIN,
                tally={"agree": 2, "conditional": 1, "oppose": 0, "abstain": 1, "responded": 3, "total": 4}, rid=7,
                rurl="/w/a/reports/7", model="glm", pool=[(1, "mech-a", "find_part", '{"q": "힌지"}', "412 MPa"),
                                                        (2, "rel-b", "find_part", '{"q": "힌지"}', "412 MPa"),
                                                        (2, "rel-b", "vision_transcribe", "{}", "판독문")],
                evidence=[{"tool": "query_voc", "args": "", "result": "VOC 12건"}, {"source": "메모", "result": "도구 없음"}],
                designated=[("get_spec", {"id": 3}, "스펙 3")], pre_tools=["agent_search", "alert_check"],
                amap={"find_part": "heax-step_forge", "query_voc": "signalforge", "agent_search": "ai-data-hub", "get_spec": "_gateway"})
    return d.insight_card(**{**base, **kw})


def test_카드_칸은_늘_같고_근거_표지는_하나씩_지문과_앱을_단다():
    c = _card()
    assert set(c) == {"stage", "claim", "claim_from", "tally", "report_id", "report_url", "model", "calls", "tools", "apps",
                      "apps_unknown", "refs"}
    assert c["stage"] == "diagnosis" and c["claim"] == SUMMARY and c["tally"] == {"agree": 2, "conditional": 1, "oppose": 0, "abstain": 1}
    tools = [x["tool"] for x in c["calls"]]
    assert tools == ["find_part", "query_voc", "get_spec"], "같은 (도구, 인자, 결과)는 하나 · 판독문은 도구가 아니다 · 도구 없는 근거는 표지가 없다"
    one = c["calls"][0]
    assert one["ref"].startswith("call:") and len(one["ref"]) == 21 and len(one["sha256"]) == 64 and one["ok"] is True
    assert one["app"] == "heax-step_forge" and c["calls"][2]["args"] == '{"id": 3}'
    assert c["tools"] == ["agent_search", "alert_check", "find_part", "get_spec", "query_voc"]
    assert c["apps"] == ["_gateway", "ai-data-hub", "heax-step_forge", "signalforge"] and c["apps_unknown"] == ["alert_check"]
    assert c["refs"] == ["rpt:7"] + [x["ref"] for x in c["calls"]]
    bare = _card(rid=None, pool=[], evidence=[], designated=[], pre_tools=[], summary="", plain="")
    assert bare["refs"] == [] and bare["calls"] == [] and bare["claim"] == "" and bare["report_id"] is None


def test_인자는_엔진의_인자_표기_상한까지다():
    c = _card(pool=[(1, "s", "find_part", "가" * 5000, "x")], evidence=[], designated=[])
    assert len(c["calls"][0]["args"]) == d._EVID_ARGS_MAX


# ── 엔진 끝까지 — 무장한 심의만 카드 거리를 낸다 ─────────────────────────────────────────────────
def _run_engine(monkeypatch, tmp_path, *, subject=None, job="default"):
    """실제 MCP 도구(deliberate_start)로 열어 실제 엔진을 끝까지 돌린다(test_delib_sealed._run 과 같은 대역 — 요약 · 쉬운 설명만 정해 둔 글)."""
    import langgraph.prebuilt

    events: list = []
    real_apply = delib_jobs._apply

    def _apply(job_rec, event, data):
        events.append((event, data))
        return real_apply(job_rec, event, data)

    async def _fake_tools(*_a, **_k):
        return _Tripwire()

    async def _fake_llm(_llm, system, human):
        if "엔지니어링 톤" in system:
            return "결정 — 리브를 보강한다."
        if "군더더기 없이 핵심만" in system:
            return SUMMARY
        if "비전문가에게 설명" in system:
            return PLAIN
        return _SEAT_JSON

    async def _fake_gather(_agent, persona, *_a, **_k):
        return persona["key"], [], "", ""

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    monkeypatch.setattr(d, "_llm_text", _fake_llm)
    monkeypatch.setattr(d, "_free_gather_one", _fake_gather)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {"recommend_agents": "heax-hwax_deliberation"})
    monkeypatch.setattr(a, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda _llm, tools, **_k: object())
    monkeypatch.setattr(delib_jobs, "_apply", _apply)
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    monkeypatch.setattr(m, "_APP", SimpleNamespace(state=SimpleNamespace(llm=SimpleNamespace(model_name="glm-test"), delib_llm=None)))

    async def go():
        out = await m.deliberate_start("힌지 브래킷 리브를 보강할 것인가", job=job, rounds=2,
                                       personas=[{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}],
                                       subject=subject, ctx=_ctx(ME, "CAEG"))
        await delib_jobs._TASKS[out["job_id"]]
        return delib_jobs._JOBS[out["job_id"]]

    rec = asyncio.run(go())
    assert rec["status"] == "done", rec.get("error")
    return rec, [x for ev, x in events if ev == "delib" and x.get("kind") == "insight"], [ev for ev, _x in events]


def test_주제를_단_MCP_잡은_단마다_카드_거리를_원장에_남긴다(monkeypatch, tmp_path, hub):
    rec, ins, _evs = _run_engine(monkeypatch, tmp_path, subject={"sp_task": KEY, "round": "dv1"})
    assert rec["subject"]["sp_state"] == s.CONFIRMED and len(ins) == 1, ins
    card = rec["insights"]["default"]
    assert card["claim"] == SUMMARY and card["claim_from"] == "summary" and card["report_id"] == 7 and card["model"] == "glm-test"
    assert card["tally"]["agree"] + card["tally"]["abstain"] + card["tally"]["conditional"] + card["tally"]["oppose"] == 2
    assert "kind" not in card and KEY not in json.dumps(card, ensure_ascii=False), "카드 거리에는 주제가 없다(엔진은 주제를 모른다)"


def test_주장으로_남은_주제도_카드_거리를_짓는다(monkeypatch, tmp_path, hub):
    rec, ins, _evs = _run_engine(monkeypatch, tmp_path, subject={"sp_task": "없는 과제"})
    assert rec["subject"]["sp_state"] == s.CLAIMED and len(ins) == 1 and "default" in rec["insights"]


@pytest.mark.parametrize("subject", [None, {"text": "과제 없이 글만", "round": "pv1"}], ids=("no-subject", "none-state"))
def test_과제_대상이_아닌_심의는_카드_거리가_없고_사건도_종전_그대로다(monkeypatch, tmp_path, hub, subject):
    rec, ins, evs = _run_engine(monkeypatch, tmp_path, subject=subject)
    assert ins == [] and "insights" not in rec
    assert "insight" not in json.dumps(evs)


# ── 잡 원장 · 무장 ───────────────────────────────────────────────────────────────────────────
def test_원장은_단마다_하나를_쥐고_같은_단은_덮는다():
    job = {"id": "j"}
    delib_jobs._apply(job, "delib", {"kind": "insight", "stage": "mechanism", "claim": "앞"})
    delib_jobs._apply(job, "delib", {"kind": "insight", "stage": "sim-plan", "claim": "뒤"})
    delib_jobs._apply(job, "delib", {"kind": "insight", "stage": "mechanism", "claim": "다시 낸 앞"})
    assert job["insights"] == {"mechanism": {"stage": "mechanism", "claim": "다시 낸 앞"}, "sim-plan": {"stage": "sim-plan", "claim": "뒤"}}


@pytest.mark.parametrize("job,armed", [
    ({"subject": {"sp_state": "confirmed"}, "chair_template": "default"}, True),
    ({"subject": {"sp_state": "claimed"}, "chair_template": "diagnosis"}, True),
    ({"subject": {"sp_state": "none"}}, False),
    ({}, False),
    ({"subject": {"sp_state": "confirmed"}, "chair_template": "risk-review"}, False),
    ({"subject": {"sp_state": "confirmed"}, "started_by": "app"}, False),
])
def test_무장은_확인_주장_주제이고_리스크_심사가_아닐_때만(job, armed):
    assert delib_jobs.insight_armed(job) is armed


def test_잡_태스크_문맥에만_무장_표지가_걸린다(eng, monkeypatch, hub):
    _caps(monkeypatch, 5)
    seen = {}

    async def entry(_app, q, groups, opts, user_email, user_pat, _x):
        seen[q] = d.INSIGHT_ARM.get()
        yield d._sse("delib", {"kind": "decision", "text": "결정"})
        yield d._sse("done", {})
    monkeypatch.setattr(d, "run_deliberation", entry)

    async def scenario():
        a1 = await m.deliberate_start("주제 단 화두", subject={"sp_task": KEY}, ctx=_ctx(ME, "CAEG"))
        a2 = await m.deliberate_start("주제 없는 화두", ctx=_ctx(ME, "CAEG"))
        await asyncio.wait([delib_jobs._TASKS[a1["job_id"]], delib_jobs._TASKS[a2["job_id"]]])
        await _tick()
        assert d.INSIGHT_ARM.get() is False, "요청 문맥에는 걸지 않는다"
    _play(scenario)
    assert seen == {"주제 단 화두": True, "주제 없는 화두": False}


def test_보통_이어하기는_앞_잡을_continues_로_적는다(eng, monkeypatch, hub):
    _caps(monkeypatch, 5)

    async def scenario():
        first = await m.deliberate_start("앞 화두", subject={"sp_task": KEY}, ctx=_ctx(ME, "CAEG"))
        await _end(eng, delib_jobs.get(first["job_id"]))
        nxt = await m.deliberate_continue(first["job_id"], human_note="더 보자", ctx=_ctx(ME, "CAEG"))
        rec = delib_jobs.get(nxt["job_id"])
        assert rec["continues"] == first["job_id"]
        await _end(eng, rec)
        assert json.loads((delib_jobs.JOB_DIR / f"{rec['id']}.json").read_text(encoding="utf-8"))["continues"] == first["job_id"]
    _play(scenario)


# ── 웹 — 확인 · 주장인 주제만 엔진 앞에서 무장한다 ─────────────────────────────────────────────────
@pytest.mark.parametrize("subject,armed", [({"sp_task": KEY}, True), ({"sp_task": "없는 과제"}, True), ({"text": "글만"}, False),
                                           (None, False)])
def test_웹은_확인_주장_주제일_때만_엔진_앞에서_무장한다(monkeypatch, hub, subject, armed):
    seen = {}

    async def entry(_app, q, groups, opts, user_email, user_pat, _h=None, **kw):
        seen["arm"] = d.INSIGHT_ARM.get()
        yield a._sse("delib", {"kind": "decision", "text": "결정"})
        yield a._sse("done", {})
    monkeypatch.setattr(a, "run_deliberation", entry)
    streams = []
    monkeypatch.setattr(a, "_detach_stream", lambda stream, *_a, **_k: streams.append(stream) or _empty())
    opts = {"rounds": 3, **({"subject": subject} if subject is not None else {})}
    asyncio.run(a.chat(a.ChatRequest(message="/심의 알파 힌지", delib_opts=opts, groups=["CAEG"], user_email=ME, user_pat="pat-EXAMPLE")))

    async def drain():
        async for _c in streams[-1]:
            pass
        return d.INSIGHT_ARM.get()
    assert asyncio.run(drain()) is False, "엔진이 끝나면 표지를 되돌린다"
    assert seen["arm"] is armed


async def _empty():
    if False:
        yield b""
