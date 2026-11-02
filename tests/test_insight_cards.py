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
from fastapi.testclient import TestClient  # noqa: E402

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
                      "apps_unknown", "untracked", "refs"}
    assert c["untracked"] == ["evidence"], "도구 없는 근거(메모)는 표지가 없어 범위를 모르는 입력으로 적는다"
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
    assert bare["refs"] == [] and bare["calls"] == [] and bare["claim"] == "" and bare["report_id"] is None and bare["untracked"] == []


def test_범위를_모르는_입력은_untracked_에_적는다():
    """도구 없는 근거 글 · 사람 의견(보강 글) · 앞 회차 요약 · 그림은 근거 표지가 없어 그 범위(남의 보고서 · 다른 앱의 결과가 섞였나)를 모른다 —
    카드가 그 사실을 적어야 포털이 조직 공개를 멈춘다(반대 검토 11-02 의 2 — 표지가 비면 '근거 범위를 다 안다' 로 읽혀 조직에 열렸다)."""
    bare = dict(pool=[], evidence=[], designated=[], pre_tools=[])
    assert _card(**bare)["untracked"] == []
    assert _card(**{**bare, "evidence": [{"source": "RA 보고서 발췌", "result": "기밀 412 MPa"}]})["untracked"] == ["evidence"]
    assert _card(**{**bare, "evidence": [{"tool": "query_voc", "result": "VOC"}]})["untracked"] == [], "도구가 있는 근거는 표지로 센다"
    assert _card(**bare, human_note="현장 의견")["untracked"] == ["note"]
    assert _card(**bare, continue_summary="앞 회차 요약")["untracked"] == ["summary"]
    assert _card(**bare, images=True)["untracked"] == ["images"]
    assert _card(**bare, human_note="  ", continue_summary="")["untracked"] == [], "빈 글은 입력이 아니다"
    every = _card(**{**bare, "evidence": [{"source": "x", "result": "y"}]}, human_note="n", continue_summary="s", images=True)
    assert every["untracked"] == ["evidence", "note", "summary", "images"]


def test_되살린_단의_지정_도구_표지는_재개_자료에서_다시_싣는다():
    """제자리 이어하기는 지정 도구를 다시 부르지 않는다(재개 자료 e:pre) — 그 표지를 재개 자료에서 되살려 실어야 이어 돈 단의 카드가 끊지 않은 실행과
    같다(반대 검토 11-02 의 미확인 하나 — 도구 이름은 남고 표지 · refs 만 빠졌다)."""
    pre = d._insight_calls([], [], [("get_spec", {"id": 3}, "스펙 3")])
    c = _card(designated=[], pre_calls=pre)
    got = [x for x in c["calls"] if x["tool"] == "get_spec"]
    assert [x["ref"] for x in got] == [pre[0]["ref"]] and got[0]["app"] == "_gateway" and pre[0]["ref"] in c["refs"]
    assert [x["tool"] for x in _card(pre_calls=pre)["calls"]].count("get_spec") == 1, "이번 실행에도 있으면 하나다"


def test_인자는_엔진의_인자_표기_상한까지다():
    c = _card(pool=[(1, "s", "find_part", "가" * 5000, "x")], evidence=[], designated=[])
    assert len(c["calls"][0]["args"]) == d._EVID_ARGS_MAX


# ── 엔진 끝까지 — 무장한 심의만 카드 거리를 낸다 ─────────────────────────────────────────────────
def _run_engine(monkeypatch, tmp_path, *, subject=None, job="default", **start_kw):
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
                                       subject=subject, ctx=_ctx(ME, "CAEG"), **start_kw)
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


def test_엔진은_도구_없는_근거와_사람_의견을_카드에_범위_모름으로_적는다(monkeypatch, tmp_path, hub):
    rec, _ins, _evs = _run_engine(monkeypatch, tmp_path, subject={"sp_task": KEY}, human_note="현장 의견 — 리브부터",
                                  evidence=[{"source": "붙인 문서", "result": "낙하 1.5 m 에서 412 MPa"}])
    assert rec["insights"]["default"]["untracked"] == ["evidence", "note"]


@pytest.mark.parametrize("job,stage,snap", [("test-plan", "test-plan", {"database_summary", "coverage_gaps", "list_materials"}),
                                            ("sim", "sim-plan", {"list_tool_apps"})])
def test_단_준비의_현황_조회_도구는_그_단_카드의_근거_도구다(monkeypatch, tmp_path, hub, job, stage, snap):
    """시험 설계의 물성 근거 현황 · 해석 설계 2단의 사내 자산 현황은 엔진 밖(단 준비)에서 도구를 불러 사람 의견 칸에 붙인다 — 그 도구가 카드의 근거
    도구에 들어야 포털이 넓힐 때 그 앱의 plat 토큰을 단다(빠지면 그 앱 권한 없는 같은 소속이 그 결과로 지은 카드를 본다). 엔진이 채운 의견 칸
    (지시문 · 현황 글)은 '범위 모름' 이 아니다 — 호출자가 쓴 의견만 그렇다."""
    rec, _ins, _evs = _run_engine(monkeypatch, tmp_path, subject={"sp_task": KEY}, job=job)
    card = rec["insights"][stage]
    assert snap <= set(card["tools"]), card["tools"]
    assert card["untracked"] == [], card["untracked"]


def test_다단_심의의_뒤_단_카드는_앞_단_카드의_근거와_범위_모름을_담는다(monkeypatch, tmp_path, hub):
    """해석 설계 2단은 1단 결정문(앞 회차 요약 칸)에 기댄다 — 2단 카드는 1단 카드의 근거 도구 · 범위 모름 표지를 다 담는다(PLAN §6.2 최소 범위).
    호출자의 사람 의견은 1단의 입력이라 1단에서 표지가 서고 2단으로 이어진다."""
    rec, _ins, _evs = _run_engine(monkeypatch, tmp_path, subject={"sp_task": KEY}, job="sim", human_note="현장 의견",
                                  evidence=[{"tool": "find_part", "args": "{}", "result": "부품 12"}])
    one, two = rec["insights"]["mechanism"], rec["insights"]["sim-plan"]
    assert "find_part" in one["tools"] and set(one["tools"]) <= set(two["tools"])
    assert one["untracked"] == ["note"] and two["untracked"] == ["note"], (one["untracked"], two["untracked"])
    tp = _run_engine(monkeypatch, tmp_path / "tp", subject={"sp_task": KEY}, job="test-plan", human_note="현장 의견")[0]
    assert tp["insights"]["test-plan"]["untracked"] == ["note"], "시험 설계는 호출자가 쓴 의견만 표지가 선다"


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


# ── 끝난 잡 창구 ─────────────────────────────────────────────────────────────────────────────
def _put(tmp_path, jid, **kw):
    job = {"id": jid, "job": "default", "status": "done", "finished_at": 1000.0, "user": ME, "owner_aff": "CAEG",
           "chair_template": "default", "started_by": "human", "subject": {"sp_state": "confirmed", "sp_task": KEY},
           "insights": {"default": {"stage": "default", "claim": "결론"}}, **kw}
    delib_jobs._persist(job)
    return job


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    return tmp_path


def test_창구는_끝났고_주인과_주제가_있는_비리스크_잡만_낸다(ledger):
    _put(ledger, "ok-1")
    _put(ledger, "ok-claimed", subject={"sp_state": "claimed", "sp_task": "x"}, finished_at=1001.0, continues="ok-1")
    _put(ledger, "running", status="running")
    _put(ledger, "interrupted", status="interrupted")
    _put(ledger, "cancelled", status="cancelled")
    _put(ledger, "anon", user="")
    _put(ledger, "none", subject={"sp_state": "none"})
    _put(ledger, "nosubject", subject=None)
    _put(ledger, "risk", chair_template="risk-review")
    _put(ledger, "app", started_by="app")
    _put(ledger, "nocards", insights={})
    _put(ledger, "riskcard", insights={"risk-review": {"stage": "risk-review", "claim": "x"}})
    _put(ledger, "err", status="error", finished_at=1002.0)
    got = delib_jobs.insight_items(0.0, 50)
    assert got["contract"] == delib_jobs.INSIGHT_CONTRACT and got["complete"] is True
    assert [it["job_id"] for it in got["items"]] == ["ok-1", "ok-claimed", "err"]
    one = got["items"][1]
    assert one["continues"] == "ok-1" and one["subject"]["sp_state"] == "claimed" and one["cards"] == [{"stage": "default", "claim": "결론"}]
    assert got["next_since"] == 1002.0


def test_창구는_since_부터_상한까지_내고_다_냈는지_말한다(ledger):
    for i in range(5):
        _put(ledger, f"j{i}", finished_at=1000.0 + i)
    page = delib_jobs.insight_items(1001.0, 2)
    assert [it["job_id"] for it in page["items"]] == ["j1", "j2"] and page["complete"] is False and page["next_since"] == 1002.0
    rest = delib_jobs.insight_items(page["next_since"], 10)
    assert [it["job_id"] for it in rest["items"]] == ["j2", "j3", "j4"] and rest["complete"] is True, "경계는 다시 나온다"
    assert delib_jobs.insight_items(5000.0, 10)["items"] == []


def test_창구는_공유_비밀을_요구한다(ledger, monkeypatch):
    _put(ledger, "ok-1")
    import seat_box
    monkeypatch.setattr(seat_box, "secret", lambda _app: "Bearer gw-secret")
    c = TestClient(a.app)
    assert c.get("/insights/cards").status_code == 401
    assert c.get("/insights/cards", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.get("/insights/cards", params={"since": 0}, headers={"Authorization": "Bearer gw-secret"})
    assert r.status_code == 200 and [it["job_id"] for it in r.json()["items"]] == ["ok-1"]
    assert c.get("/insights/cards", params={"since": -1}, headers={"Authorization": "Bearer gw-secret"}).status_code == 422
    monkeypatch.setattr(seat_box, "secret", lambda _app: "")
    assert c.get("/insights/cards", headers={"Authorization": "Bearer gw-secret"}).status_code == 503


# ── 주제 다시 풀기 창구 ──────────────────────────────────────────────────────────────────────
def test_주제_다시_풀기는_사람_PAT_로_M1_규칙을_쓴다(monkeypatch):
    seen = []

    async def call(_app, *, groups, user, user_pat, args):
        seen.append((tuple(groups), user, user_pat))
        return False, json.dumps(_FIX["exact_and_partial"]["body"], ensure_ascii=False)
    monkeypatch.setattr(s, "call_resolver", call)
    c = TestClient(a.app)
    body = {"subject": {"sp_task": KEY, "round": "DV1", "text": "알파"}, "groups": ["CAEG"], "user_email": ME, "user_pat": "pat-EXAMPLE"}
    r = c.post("/deliberate/subject/confirm", json=body)
    assert r.status_code == 200 and r.json()["subject"]["sp_state"] == "confirmed" and r.json()["subject"]["round"] == "dv1"
    assert seen == [(("CAEG",), ME, "pat-EXAMPLE")]
    assert c.post("/deliberate/subject/confirm", json={**body, "user_pat": " "}).json()["code"] == "bad_request"
    r = c.post("/deliberate/subject/confirm", json={**body, "chair_template": "risk-review"})
    assert r.status_code == 422 and r.json()["code"] == "risk_family"
    r = c.post("/deliberate/subject/confirm", json={**body, "subject": {"sp_task": KEY, "sp_state": "confirmed"}})
    assert r.status_code == 422 and "모르는 칸" in r.json()["message"]
    assert c.post("/deliberate/subject/confirm", json={**body, "subject": {"sp_task": " "}}).json() == {"subject": None}
    assert len(seen) == 1, "꼴이 틀리거나 빈 주제는 풀이를 부르지 않는다"


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
