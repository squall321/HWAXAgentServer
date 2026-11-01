# 심의 결과 요약(deliberate_result view="summary") — 결정문 · 표결 · 소수 의견 · 수치 대조 · 보고서 · full 의 쪽 수만, 원장에 있는 칸만 옮기고 없는 칸은 까닭을 적는다
"""HWAXPortal docs/token-saving PLAN §3 · §5 P4a · TS-4 · TS-5(사용자 결정 11-02).

왜 생겼나. deliberate_result 는 결정문 전문에 더해 화두 전문 · 쉬운 설명(결정문 끝에 이미 있다) · 상태줄 30줄 · 좌석 상세 · 손잡이 · 경고 · 빠진
근거를 한꺼번에 주어, 큰 심의는 여러 쪽(쪽마다 25,000자)을 다 읽어야 했다. summary 는 결정에 필요한 것만 준다 — 결정문 · 표결(집계와 좌석별 표) ·
소수 의견(표가 '동의' 가 아닌 좌석의 마지막 라운드 발언) · 결정문 수치 대조(evidence.unsourced_numbers) · RA 보고서 번호와 주소 · full 의 쪽 수.
**글을 짓지 않는다** — 엔진이 이미 낸 칸(결과 사건의 tally · 좌석별 표 · 보고서 주소, 결정문 사건의 수치 대조)과 원장 전사의 발언만 옮기고, 없으면
그 칸은 null 이고 까닭을 missing 에 적는다. **기본은 full 그대로다** — 루틴의 심의 단계가 결정문을 쪽마다 다 읽는다(routine.py _delib_step).

가짜는 바깥에만 있다(tests/test_ha09_s1_resume.py 의 world — LLM · 게이트웨이 도구 · 좌석 자유 조회). 엔진 · 잡 원장 · MCP 도구는 진짜다.

  실행:  .venv/bin/python -m pytest tests/test_delib_result_summary.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import mcp_pages  # noqa: E402
import mcp_server as m  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401 — 모델 창 고정(자동 픽스처)
from test_delib_ledger_fields import _DISSENT, _RA_URL, wd  # noqa: E402, F401 — wd 는 픽스처
from test_ha09_s1_resume import ROUNDS, _oracle, world  # noqa: E402, F401 — world 는 픽스처
from test_routine_executor import (ScriptLLM, _app_ns, _delib_gw, _env, _ev, _req, _run_bounded, _wire,  # noqa: E402
                                   kit)  # noqa: F401 — kit 은 픽스처


def _summary(job_id: str, part: int = 1) -> dict:
    return asyncio.run(m.deliberate_result(job_id, part=part, view="summary"))


def _final_turn(led: dict, seat: str) -> dict:
    rows = [t for t in led["turns"] if t.get("persona") == seat and t.get("round") == ROUNDS]
    assert len(rows) == 1, f"시험 전제 — {seat} 의 마지막 라운드 발언 하나"
    return rows[0]


# ── summary — 실제 엔진으로 끝난 잡 ─────────────────────────────────────────────────────────────────────────────
def test_요약은_결정문_표결_소수_의견_수치_대조_보고서_쪽_수만_준다(wd, monkeypatch):
    o = _oracle(wd, monkeypatch)
    led, jid = o["led"], o["job"]["id"]
    out = _summary(jid)
    full = asyncio.run(m.deliberate_result(jid))
    assert out["view"] == "summary" and out["job_id"] == jid and out["status"] == "done"
    assert out["decision"] == led["decision"]
    assert out["votes"] == {"tally": led["tally"], "unanimous": False, "seats": led["votes"]}, out["votes"]
    say = _final_turn(led, _DISSENT)
    want = {"seat": _DISSENT, "stance": "반대", "say": say["say"],
            **({"say_clipped": True, "say_full_chars": say["say_full_chars"]} if "say_full" in say else {})}
    assert out["minority"] == [want], out["minority"]
    assert out["numcheck"] == led["numcheck"]
    assert (out["report_id"], out["report_url"]) == (77, _RA_URL)
    assert out["full_parts"] == ((full.get("page") or {}).get("parts") or 1)
    assert out["missing"] == {}, out["missing"]
    assert set(out) == {"job_id", "status", "view", "full_parts", "report_id", "report_url", "votes", "minority", "numcheck",
                        "missing", "decision"}, sorted(out)
    for k in ("question", "plain", "steps", "seats_detail", "applied_opts", "warnings", "evidence_omitted"):
        assert k not in out, k
    assert len(mcp_pages.wire(out)) < len(mcp_pages.wire(full)), "요약이 full 보다 작지 않다"


# ── summary — 원장 꼴마다(합성 원장) ─────────────────────────────────────────────────────────────────────────────
_GONE = object()          # _led(칸=_GONE) — 그 칸을 원장에서 뺀다(옛 판 원장)


def _led(**kw) -> dict:
    """끝난 잡의 원장 — 마지막 라운드(3R) 좌석 다섯: a 동의 · b 반대(줄인 발언) · c 조건부 동의 · d 응답 못 함 · e 반대(이 단의 전사에 발언이
    없다 — 앞 단(turns_from 앞)의 3R 발언은 이 단의 것이 아니다)."""
    now = delib_jobs._now()
    job = {"id": "default-20261102-030303-cccccc", "job": "default", "kind": "default", "label": "자유 심의", "status": "done",
           "question": "화두", "chair_template": None, "opts": {"sealed": None}, "stage": "report", "step": "", "round": 3,
           "total_rounds": 3, "turns_from": 2,
           "seats": [{"key": k} for k in "abcde"],
           "turns": [{"persona": "e", "round": 3, "say": "앞 단의 e 3R — 이 단의 발언이 아니다", "stance": "반대"},  # 앞 단(turns_from 앞)
                     {"persona": "b", "round": 1, "say": "b 의 1라운드"},
                     {"persona": "a", "round": 3, "say": "a 최종 — 동의", "stance": "동의"},
                     {"persona": "b", "round": 3, "say": "b 최종(화면용)", "say_full": "b 최종(화면용) — 전문 " * 40,
                      "say_full_chars": 400, "stance": "반대"},
                     {"persona": "c", "round": 3, "say": "c 최종 — 조건부", "stance": "조건부 동의"}],
           "decision": "■ 핵심 요약\n- 결론\n\n## 결정\n본문", "plain": "쉬운 설명", "result_text": "■ 핵심 요약\n- 결론",
           "report_id": 41, "report_url": "/w/team-a/reports/41", "title": "심의 — 화두",
           "tally": {"agree": 1, "conditional": 1, "oppose": 2, "abstain": 1, "responded": 4, "total": 5}, "unanimous": False,
           "votes": [{"seat": "a", "stance": "동의", "responded": True}, {"seat": "b", "stance": "반대", "responded": True},
                     {"seat": "c", "stance": "조건부 동의", "responded": True}, {"seat": "d", "stance": "미표명", "responded": False},
                     {"seat": "e", "stance": "반대", "responded": True}],
           "numcheck": {"unsourced": ["4321.5"], "unsourced_count": 1, "checked": 3},
           "error": None, "warnings": [], "evidence_omitted": [], "steps": ["단계"], "checkpoint": None,
           "started_at": now - 60, "updated_at": now, "finished_at": now, "user": "", "owner_aff": ""}
    job.update(kw)
    for k in [k for k, v in kw.items() if v is _GONE]:
        del job[k]
    delib_jobs._JOBS[job["id"]] = job
    return job



@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    for name in ("_JOBS", "_TASKS", "_PENDING"):
        monkeypatch.setattr(delib_jobs, name, {})
    monkeypatch.setattr(m, "_APP", None)
    return _led


def _strings(v, skip=("missing",)) -> list:
    """응답의 글 잎 — 까닭(missing)은 뺀다."""
    if isinstance(v, dict):
        return [s for k, x in v.items() if k not in skip for s in _strings(x, skip)]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x, skip)]
    return [v] if isinstance(v, str) else []


def test_소수_의견은_동의가_아닌_좌석의_마지막_라운드_발언이고_없으면_그렇다고_말한다(ledger):
    job = ledger()
    out = _summary(job["id"])
    by = {r["seat"]: r for r in out["minority"]}
    assert list(by) == ["b", "c", "d", "e"], "동의 아닌 좌석을 표 차례대로"
    assert by["b"] == {"seat": "b", "stance": "반대", "say": "b 최종(화면용)", "say_clipped": True, "say_full_chars": 400}, by["b"]
    assert by["c"] == {"seat": "c", "stance": "조건부 동의", "say": "c 최종 — 조건부"}, by["c"]
    assert by["d"]["say"] is None and "응답하지 못했다" in by["d"]["missing"], by["d"]
    assert by["e"]["say"] is None and "DELIB_JOB_TURN_MAX" in by["e"]["missing"], by["e"]
    assert out["missing"] == {}, out["missing"]


def test_요약은_원장에_있는_글만_옮기고_글을_짓지_않는다(ledger):
    job = ledger()
    out = _summary(job["id"])
    have = set(_strings(job, skip=()))
    made = [s for s in _strings({k: v for k, v in out.items() if k != "view"}) if s not in have]
    assert made == [], f"원장에 없는 글을 지어냈다: {made}"
    assert out["view"] == "summary"


def test_옛_원장에는_없는_칸을_null_로_두고_까닭을_적는다(ledger):
    """이 판 이전에 돈 잡 — 원장에 표결 · 수치 대조 · 보고서 주소가 없다. 빈 값으로 말없이 두지 않는다."""
    job = ledger(tally=_GONE, unanimous=_GONE, votes=_GONE, numcheck=_GONE, report_url=_GONE)
    out = _summary(job["id"])
    assert out["decision"] == job["decision"] and out["report_id"] == 41
    for k in ("votes", "minority", "numcheck", "report_url"):
        assert out[k] is None and str(out["missing"].get(k) or "").strip(), (k, out["missing"])
    assert set(out["missing"]) == {"votes", "minority", "numcheck", "report_url"}, out["missing"]


@pytest.mark.parametrize("status", ["running", "queued"])
def test_끝나지_않은_잡의_요약은_아직이라고_말한다(ledger, status):
    job = ledger(status=status, decision=None, result_text=None, tally=_GONE, unanimous=_GONE, votes=_GONE, numcheck=_GONE,
                 report_id=None, report_url=_GONE, finished_at=None)
    out = _summary(job["id"])
    full = asyncio.run(m.deliberate_result(job["id"]))
    assert out["status"] == status and out["decision"] is None
    assert set(out["missing"]) == {"decision", "votes", "minority", "numcheck", "report_id", "report_url"}, out["missing"]
    assert all(status in why for why in out["missing"].values()), out["missing"]
    if status == "running":
        assert out["note"] == full["note"], "진행 중 안내가 full 과 다르다"


def test_보고서를_남기지_않는_심의는_그렇다고_말한다(ledger):
    job = ledger(opts={"sealed": None, "save_report": 0}, report_id=None, report_url=_GONE)
    out = _summary(job["id"])
    assert out["report_id"] is None and "save_report=0" in out["missing"]["report_id"], out["missing"]
    assert out["report_url"] is None and out["missing"]["report_url"], out["missing"]


def test_결정문_없이_끝난_잡은_까닭에_오류를_싣는다(ledger):
    job = ledger(status="error", decision=None, result_text=None, error="RuntimeError: 의장 실패", tally=_GONE, unanimous=_GONE,
                 votes=_GONE, numcheck=_GONE, report_id=None, report_url=_GONE)
    out = _summary(job["id"])
    assert out["decision"] is None and "error" in out["missing"]["decision"] and "의장 실패" in out["missing"]["decision"], out["missing"]


def test_요약의_full_parts_는_full_로_부를_때의_쪽_수이고_요약도_쪽으로_나뉜다(ledger):
    """화두 · 결정문이 아주 길다 — full 은 여러 쪽이고, 요약도 결정문 때문에 쪽으로 나뉜다. 요약의 다음 쪽은 view=summary 로 부르고, 잇으면
    원장의 결정문이 된다. 짧은 칸(표결 · 소수 의견 · 수치 대조 · 보고서 · 쪽 수)은 첫 쪽에 다 있다."""
    job = ledger(question="변경 요지 " * 4000, decision="결정 문단 — 근거와 판단 " * 3000)
    full = asyncio.run(m.deliberate_result(job["id"]))
    p1 = _summary(job["id"])
    assert p1["full_parts"] == full["page"]["parts"] >= 3, (p1.get("full_parts"), full["page"]["parts"])
    for k in ("votes", "minority", "numcheck", "report_id", "report_url", "full_parts", "missing"):
        assert k in p1, k
    assert p1["page"]["next"] == {"job_id": job["id"], "view": "summary", "part": 2}, p1["page"]
    text, part = p1.get("decision") or "", 2
    while part:
        page = _summary(job["id"], part)
        text += page.get("decision") or ""
        part = (page["page"]["next"] or {}).get("part")
    assert text == job["decision"]


def test_view_는_full_과_summary_가운데_하나다(ledger):
    job = ledger()
    with pytest.raises(ValueError, match="view"):
        asyncio.run(m.deliberate_result(job["id"], view="short"))


# ── 기본은 full 그대로 ───────────────────────────────────────────────────────────────────────────────────────────
async def _old_result(job_id: str, part: int = 1) -> dict:
    """종전(main 9f08507) deliberate_result 의 몸통 그대로 — 기본 호출(view 를 주지 않거나 full)이 이것과 바이트까지 같아야 한다."""
    job = delib_jobs.get(job_id)
    out = delib_jobs.summary(job, full=True)
    if job.get("status") == "running":
        out["note"] = "아직 진행 중이다. 잠시 뒤 다시 부르거나 deliberate_status 로 지켜봐라."
    elif out.get("queue"):
        out["note"] = "아직 시작 전이다 — " + m._queued_note(out["queue"])
    return mcp_pages.paged(out, part=part, ids=m._JOB_IDS, tail=m._JOB_TAIL, tail_page=True, again={"job_id": job_id})


def test_기본_view_는_full_이고_종전_응답과_바이트까지_같다(ledger):
    jobs = [ledger(), ledger(id="default-20261102-040404-dddddd", question="변경 요지 " * 4000, decision="결정 " * 9000),
            ledger(id="default-20261102-050505-eeeeee", status="running", decision=None, finished_at=None)]
    rows = 0
    for job in jobs:
        parts = (asyncio.run(_old_result(job["id"])).get("page") or {}).get("parts") or 1
        for p in range(1, parts + 1):
            old = mcp_pages.wire(asyncio.run(_old_result(job["id"], p)))
            assert mcp_pages.wire(asyncio.run(m.deliberate_result(job["id"], part=p))) == old, (job["id"], p)
            assert mcp_pages.wire(asyncio.run(m.deliberate_result(job["id"], part=p, view="full"))) == old, (job["id"], p)
            rows += 1
    assert rows > 3, "시험 전제 — 쪽으로 나뉜 응답도 본다"


def test_실제_엔진으로_끝난_잡도_기본_응답은_종전과_같다(wd, monkeypatch):
    o = _oracle(wd, monkeypatch)
    jid = o["job"]["id"]
    assert mcp_pages.wire(asyncio.run(m.deliberate_result(jid))) == mcp_pages.wire(asyncio.run(_old_result(jid)))


# ── 루틴은 요약을 쓰지 않는다 — 결정문을 쪽마다 다 읽는다 ────────────────────────────────────────────────────────────────
def test_루틴의_심의_단계는_요약_줄인_응답_기다림을_쓰지_않는다(kit):
    """routine.py 의 심의 단계 — 진행은 {job_id} 만으로 직접, 결과는 routine_call(deliberate_result) 의 {job_id} · {job_id, part} 로 쪽마다
    읽는다(TS-4). view · brief · wait_s 를 실으면 결정문 뒤쪽이 봉투에서 빠지거나(요약) 실행기가 상한까지 붙잡힌다(기다림)."""
    gw = _delib_gw(calls=1, statuses=[{"status": "running"}, {"status": "done"}])
    real = gw._inner

    async def _two_parts(bk, orig, args):
        if orig == "deliberate_result":
            part = int(args.get("part") or 1)
            return json.dumps({"job_id": args.get("job_id"), "status": "done", "decision": f"결정 {part}쪽 — 근거 4321.0 ppm",
                               "page": {"part": part, "parts": 2}}, ensure_ascii=False), False
        return await real(bk, orig, args)

    gw._inner = _two_parts
    llm = ScriptLLM(envelope={"delib": [_env(summary="결정 [delib.c2] [delib.c3]", findings=[{"text": "벤트", "refs": ["delib.c3"]}])]})
    _wire(kit, gw, llm)
    _ev(_run_bounded(_app_ns(llm), _req()))
    status = [x for n, x, _h in gw.log if n == "deliberate_status"]
    result = [x["arguments"] for x in gw.rcalls() if x.get("name") == "hwax-deliberation:deliberate_result"]
    assert status and all(set(x) == {"job_id"} for x in status), status
    assert [sorted(x) for x in result] == [["job_id"], ["job_id", "part"]], result


# ── 설명 — summary 를 먼저 ───────────────────────────────────────────────────────────────────────────────────────
def test_결과_회수_설명이_summary_를_먼저_부르라고_가르친다():
    t = next(x for x in asyncio.run(m.mcp.list_tools()) if x.name == "deliberate_result")
    v = t.inputSchema["properties"]["view"]
    assert v.get("default") == "full" and v.get("enum") == ["full", "summary"], v
    assert 'view="summary"' in t.description and "full" in t.description, t.description[:300]
    for k in ("full_parts", "missing"):
        assert k in v["description"], k
    assert t.inputSchema.get("required") == ["job_id"]

