# MCP 심의 잡의 주제 — 시작 · 이어하기 · 목록 색인 · 주제로 거르는 목록(신원 요구 · 같은 소속 · 확인된 주제만)을 본다 — 과제 인사이트 그래프 M1
"""HWAXPortal docs/project-insight-graph PLAN §4.7 (a) · §9 M1 · 사용자 결정 11-02(PG-15 · PG-17).

  · deliberate_start(subject) — 꼴을 보고 SP 과제 key 를 서버가 다시 풀어 잡 원장 · 목록 색인에 싣는다. 엔진에는 넘기지 않는다(결정문 ·
    RA 보고서에 주제가 실리지 않는다). 주제가 없으면 원장은 종전 그대로다. 리스크 심사 계열은 주제를 받지 않는다.
  · deliberate_continue — 앞 잡의 주제를 물려받는다(앞 잡에 없거나 확인되지 않았으면 이어하기에서 단다 · 확인됐으면 같은 주제는 물려받고 다른
    주제는 거절 · 리스크 심사로 잇는 회차는 물려받지 않는다). 끊긴 잡의 이어하기(요약 길 · 제자리 길)도 같다.
  · deliberate_list(subject) — 신원이 있어야 하고, 볼 수 있는 잡(주인 · 같은 소속 · 관리자)만, 확인된 주제만, 과제 key 글자 그대로 · 차수 정확히.
    차수 모름은 그 칸에만 있다.
  풀이 가짜는 실제 StepForge 응답 꼴이다(tests/fixtures/subject/find_plm_task.json). 엔진은 test_delib_job_queue 의 가짜(문이 열릴 때까지 도는 생성기)다.

  실행:  .venv/bin/python -m pytest tests/test_delib_subject_jobs.py -q
"""
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import delib_resume  # noqa: E402
import delib_subject as s  # noqa: E402
import mcp_server as m  # noqa: E402
from test_delib_job_queue import _caps, _end, _on_disk, _play, _tick, eng  # noqa: E402, F401
from test_delib_same_aff import _ctx  # noqa: E402

_FIX = json.loads((Path(__file__).resolve().parent / "fixtures" / "subject" / "find_plm_task.json").read_text(encoding="utf-8"))
KEY, OTHER = "알파 개발_C2", "베타 개발_C1"
ME, MATE, OUTSIDER, BOSS = "me@example.com", "mate@example.com", "out@example.com", "boss@example.com"


def _body(query: str) -> dict:
    """허브 거울에 KEY · OTHER 두 과제가 있는 판 — 실제 응답(exact_and_partial)의 확인 행을 본떠 질의에 맞는 행만 싣는다(부분 일치)."""
    base = _FIX["exact_and_partial"]["body"]
    rows = []
    for key in (KEY, f"{KEY} 보강", OTHER):
        if query in key:
            row = copy.deepcopy(base["tasks"][0])
            row.update(key=key, label=key)
            rows.append(row)
    return {**copy.deepcopy(base), "query": query, "count": len(rows), "tasks": rows}


@pytest.fixture
def hub(monkeypatch):
    """풀이 호출 가짜 — 받은 자격 · 질의를 적는다."""
    calls = []

    async def call(_app, *, groups, user, user_pat, args):
        calls.append({"groups": list(groups), "user": user, "user_pat": user_pat, "query": args["query"]})
        return False, json.dumps(_body(args["query"]), ensure_ascii=False, indent=2)
    monkeypatch.setattr(s, "call_resolver", call)
    return calls


def _index(job_id: str) -> dict:
    return json.loads((delib_jobs.JOB_DIR / "list" / f"{job_id}.json").read_text(encoding="utf-8"))


# ── 시작 ─────────────────────────────────────────────────────────────────────────────────────
def test_주제를_단_MCP_심의는_원장_색인_조회에_확인된_주제가_남고_엔진에는_가지_않는다(eng, monkeypatch, hub):
    _caps(monkeypatch, 5)
    art = {"kind": "mcad", "app": "heax-step_forge", "id": "019a2b3c4d5e6f7a8b9c0d1", "rev": "1a2b3c4d5e6f7a8b"}

    async def scenario():
        out = await m.deliberate_start("알파 힌지 낙하 크랙", subject={"sp_task": KEY, "round": "DV1", "text": "알파 1차 낙하",
                                                                    "artifacts": [art]},
                                       ctx=_ctx(ME, "CAEG", groups="CAEG,plat:stepforge"))
        rec = delib_jobs.get(out["job_id"])
        subj = rec["subject"]
        assert subj["sp_state"] == s.CONFIRMED and subj["sp_task"] == KEY and subj["round"] == "dv1" and subj["artifacts"] == [art]
        assert out["subject"] == subj and "허브 거울 풀이로 확인" in out["note"], "시작 응답이 확인 상태를 말한다"
        assert hub == [{"groups": ["CAEG", "plat:stepforge"], "user": ME, "user_pat": "", "query": KEY}], \
            "MCP 잡은 사람 PAT 가 없다 — 그 호출의 신원 · 그룹으로 한 번 부른다"
        await _end(eng, rec)
        assert _on_disk(rec)["subject"] == subj and _index(rec["id"])["subject"] == subj, "원장과 목록 색인 둘 다"
        st = await m.deliberate_status(out["job_id"], ctx=_ctx(ME, "CAEG"))
        assert st["subject"] == subj
        opts = eng.seen["알파 힌지 낙하 크랙"].opts or {}
        assert "subject" not in opts and not any(KEY in str(v) for v in opts.values()), \
            "엔진에 주제를 넘기면 결정문 · RA 보고서에 실릴 길이 생긴다(PLAN §4.1 · PG-11)"
    _play(scenario)


def test_주제가_없는_심의는_종전_그대로다(eng, monkeypatch, hub):
    _caps(monkeypatch, 5)

    async def scenario():
        out = await m.deliberate_start("주제 없는 화두", ctx=_ctx(ME, "CAEG"))
        rec = delib_jobs.get(out["job_id"])
        await _end(eng, rec)
        assert "subject" not in rec and "subject" not in _on_disk(rec) and "subject" not in _index(rec["id"])
        assert "subject" not in out and "subject" not in await m.deliberate_status(out["job_id"], ctx=_ctx(ME, "CAEG"))
        empty = await m.deliberate_start("빈 주제", subject={"sp_task": " ", "text": ""}, ctx=_ctx(ME, "CAEG"))
        assert "subject" not in delib_jobs.get(empty["job_id"]), "빈 주제는 주제가 없는 것이다"
        assert hub == [], "주제가 없으면 풀이를 부르지 않는다"
    _play(scenario)


@pytest.mark.parametrize("kw", [{"job": "risk-review"}, {"job": "risk-review-sealed"}, {"job": "sealed"},
                                {"advanced": {"chair_template": "risk-review"}}, {"advanced": {"started_by": "app"}}])
def test_리스크_심사는_주제를_받지_않는다(eng, monkeypatch, hub, kw):
    _caps(monkeypatch, 5)

    async def scenario():
        with pytest.raises(ValueError, match="리스크 심사"):
            await m.deliberate_start("변경 심사", subject={"sp_task": KEY}, ctx=_ctx(ME, "CAEG"), **kw)
        assert delib_jobs._JOBS == {} and hub == [], "잡을 만들지도 풀이를 부르지도 않았다"
        ok = await m.deliberate_start("변경 심사", ctx=_ctx(ME, "CAEG"), **kw)
        assert "subject" not in delib_jobs.get(ok["job_id"]), "주제가 없으면 리스크 심사는 종전 그대로 열린다"
    _play(scenario)


def test_손잡이에_실은_주제는_버린다(eng, monkeypatch, hub):
    """MCP advanced 는 손잡이 사전에 통째로 합쳐진다(mcp_server._build_opts) — 거기 subject 를 실어도 원장의 주제가 되지 않고, 엔진 · 원장의
    손잡이 메아리에도 가지 않는다(확인되지 않은 주제가 결정문 · RA 보고서 길이나 원장에 남지 않게)."""
    _caps(monkeypatch, 5)

    async def scenario():
        out = await m.deliberate_start("손잡이로 실은 주제", advanced={"subject": {"sp_task": KEY, "sp_state": "confirmed"}},
                                       ctx=_ctx(ME, "CAEG"))
        rec = delib_jobs.get(out["job_id"])
        assert "subject" not in rec and "subject" not in rec["opts"] and "subject" not in out and hub == []
        await _tick()                          # 엔진 가짜가 첫 걸음을 떼어 받은 인자를 적는다
        assert "subject" not in (eng.seen["손잡이로 실은 주제"].opts or {}), "엔진에 가지 않는다"
        assert "subject" not in (delib_jobs._read_args(out["job_id"]) or {}).get("opts", {}), "시작 인자 파일에도 없다"
    _play(scenario)


@pytest.mark.parametrize("bad,word", [({"sp_task": KEY, "sp_state": "confirmed"}, "모르는 칸"),
                                      ({"sp_task": KEY, "artifacts": [{"kind": "mcad", "id": "xyz"}]}, "16진 23자"),
                                      ("알파 개발_C2", "객체다")])
def test_꼴이_틀린_주제는_잡을_만들기_전에_거절한다(eng, monkeypatch, hub, bad, word):
    _caps(monkeypatch, 5)

    async def scenario():
        with pytest.raises(ValueError, match=word):
            await m.deliberate_start("화두", subject=bad, ctx=_ctx(ME, "CAEG"))
        assert delib_jobs._JOBS == {} and hub == []
    _play(scenario)


def test_확인하지_못한_주제는_주장으로_남고_시작은_그대로다(eng, monkeypatch, hub):
    _caps(monkeypatch, 5)

    async def scenario():
        out = await m.deliberate_start("코드로 단 심의", subject={"sp_task": "TC-1001", "round": "2차"}, ctx=_ctx(ME, "CAEG"))
        subj = delib_jobs.get(out["job_id"])["subject"]
        assert subj["sp_state"] == s.CLAIMED and subj["sp_task"] == "TC-1001" and subj["round"] is None and subj["text"] == "2차"
        assert "확인하지 못했다" in out["note"] and out["status"] in ("running", "queued")
    _play(scenario)


# ── 이어하기 ─────────────────────────────────────────────────────────────────────────────────
def test_이어하기는_앞_잡의_주제를_물려받고_다시_풀지_않는다(eng, monkeypatch, hub):
    _caps(monkeypatch, 5)

    async def scenario():
        first = await m.deliberate_start("앞 회차", subject={"sp_task": KEY, "round": "pv1"}, ctx=_ctx(ME, "CAEG"))
        prev = delib_jobs.get(first["job_id"])
        await _end(eng, prev)
        n = len(hub)
        nxt = await m.deliberate_continue(first["job_id"], human_note="이어서", ctx=_ctx(ME, "CAEG"))
        assert delib_jobs.get(nxt["job_id"])["subject"] == prev["subject"] and nxt["subject"] == prev["subject"]
        assert len(hub) == n, "물려받은 주제는 다시 풀지 않는다(그 잡을 열 때 서버가 확인한 값이다)"
        await _end(eng, delib_jobs.get(nxt["job_id"]))
        same = await m.deliberate_continue(nxt["job_id"], human_note="같은 주제를 다시 실었다", subject={"sp_task": KEY, "round": "ＰＶ１"},
                                           ctx=_ctx(ME, "CAEG"))
        assert delib_jobs.get(same["job_id"])["subject"] == prev["subject"] and len(hub) == n, "같은 주제는 다시 풀지 않고 물려받는다"
        for other in ({"sp_task": OTHER}, {"sp_task": KEY, "round": "pv2"}, {"sp_task": KEY, "round": "pv1", "text": "덧붙인 글"}):
            with pytest.raises(ValueError, match="이미 확인됐다"):
                await m.deliberate_continue(first["job_id"], human_note="다른 주제로", subject=other, ctx=_ctx(ME, "CAEG"))
    _play(scenario)


def test_앞_잡의_주제가_주장이면_이어하기에서_고쳐_단다(eng, monkeypatch, hub):
    """과제코드로 단 주장(claimed)을 이어하기가 영영 물려받으면 그 과제의 목록에 끝내 못 든다 — 이어하기에서 key 를 고쳐 단다(반대 검토 11-02)."""
    _caps(monkeypatch, 5)

    async def scenario():
        first = await m.deliberate_start("코드로 단 심의", subject={"sp_task": "TC-1001", "round": "dv1"}, ctx=_ctx(ME, "CAEG"))
        prev = delib_jobs.get(first["job_id"])
        assert prev["subject"]["sp_state"] == s.CLAIMED
        await _end(eng, prev)
        kept = await m.deliberate_continue(first["job_id"], human_note="그대로", ctx=_ctx(ME, "CAEG"))
        assert delib_jobs.get(kept["job_id"])["subject"] == prev["subject"], "주지 않으면 주장도 그대로 물려받는다"
        nxt = await m.deliberate_continue(first["job_id"], human_note="key 를 고쳐", subject={"sp_task": KEY, "round": "dv1"},
                                          ctx=_ctx(ME, "CAEG"))
        subj = delib_jobs.get(nxt["job_id"])["subject"]
        assert subj["sp_state"] == s.CONFIRMED and subj["sp_task"] == KEY and "확인되지 않은 주제 대신" in nxt["note"]
        assert delib_jobs.get(first["job_id"])["subject"]["sp_state"] == s.CLAIMED, "앞 잡의 원장은 바꾸지 않는다"
    _play(scenario)


def test_앞_잡에_주제가_없으면_이어하기에서_단다(eng, monkeypatch, hub):
    _caps(monkeypatch, 5)

    async def scenario():
        first = await m.deliberate_start("주제 없이 연 심의", ctx=_ctx(ME, "CAEG"))
        await _end(eng, delib_jobs.get(first["job_id"]))
        nxt = await m.deliberate_continue(first["job_id"], human_note="과제를 달고 잇는다", subject={"sp_task": KEY, "round": "dv2"},
                                          ctx=_ctx(ME, "CAEG"))
        subj = delib_jobs.get(nxt["job_id"])["subject"]
        assert subj["sp_state"] == s.CONFIRMED and subj["round"] == "dv2" and len(hub) == 1 and "확인" in nxt["note"]
        assert "subject" not in delib_jobs.get(first["job_id"]), "앞 잡의 원장은 바꾸지 않는다"
    _play(scenario)


def test_리스크_심사로_잇는_회차는_주제를_물려받지_않는다(eng, monkeypatch, hub):
    _caps(monkeypatch, 5)

    async def scenario():
        first = await m.deliberate_start("일반 심의", subject={"sp_task": KEY}, ctx=_ctx(ME, "CAEG"))
        await _end(eng, delib_jobs.get(first["job_id"]))
        nxt = await m.deliberate_continue(first["job_id"], human_note="리스크로", job="risk-review", ctx=_ctx(ME, "CAEG"))
        assert "subject" not in delib_jobs.get(nxt["job_id"]) and "물려받지 않는다" in nxt["note"]
    _play(scenario)


def _interrupted(subject=None) -> dict:
    prev = {"id": "default-20261102-000000-cut000", "job": "default", "kind": "default", "label": "자유 심의", "status": "interrupted",
            "question": "끊긴 화두", "chair_template": None, "opts": {}, "seats": [], "turns": [], "round": 1, "total_rounds": 3,
            "user": ME, "owner_aff": "CAEG", "started_by": "human", "started_at": 1.0, "updated_at": 2.0, "finished_at": 2.0,
            "error": delib_jobs.INTERRUPTED_MSG}
    if subject is not None:
        prev["subject"] = subject
    delib_jobs._JOBS[prev["id"]] = prev
    delib_jobs._persist(prev)
    return prev


def test_끊긴_잡의_요약_길_이어하기도_주제를_물려받는다(eng, monkeypatch, hub):
    _caps(monkeypatch, 5)
    subj = {"sp_task": KEY, "sp_state": s.CONFIRMED, "sp_resolved_by": {"tool": s.RESOLVER_CALL}, "round": "dv3", "round_by": "person",
            "text": "", "artifacts": []}

    async def scenario():
        prev = _interrupted(subj)
        out = await m.deliberate_continue(prev["id"], ctx=_ctx(ME, "CAEG"))
        assert out["resumed"]["mode"] == "summary" and delib_jobs.get(out["job_id"])["subject"] == subj and hub == []
    _play(scenario)


def test_끊긴_잡의_제자리_이어하기는_같은_잡에_주제를_단다(eng, monkeypatch, hub):
    """제자리 길(HA-09 S1) — 같은 job_id 를 다시 연다. 주제는 그 잡 기록의 칸이라 바꾼 값으로 치지 않는다."""
    _caps(monkeypatch, 5)
    info = {"restored": 1, "total": 3, "round": 2, "sk": None, "spoken": [], "todo": [], "post": {}}

    async def scenario():
        prev = _interrupted()
        res = delib_resume.Resume(prev["id"], {}, [], info, [])
        monkeypatch.setattr(delib_jobs, "resume_load", lambda job, groups=None: (res, ""))
        out = await m.deliberate_continue(prev["id"], subject={"sp_task": KEY, "round": "mp"}, ctx=_ctx(ME, "CAEG"))
        assert out["resumed"]["mode"] == "in_place" and out["job_id"] == prev["id"]
        again = delib_jobs.get(prev["id"])
        assert again["subject"]["sp_state"] == s.CONFIRMED and again["subject"]["round"] == "mp" and _on_disk(again)["subject"]
    _play(scenario)


# ── 주제로 거르는 목록 ───────────────────────────────────────────────────────────────────────
def test_주제로_거르는_목록은_신원을_요구한다(eng, monkeypatch):
    async def scenario():
        with pytest.raises(ValueError, match="신원 없는 호출은 주제로 거르지 못한다"):
            await m.deliberate_list(subject={"sp_task": KEY})
        with pytest.raises(ValueError, match="차수가 아니다"):
            await m.deliberate_list(subject={"sp_task": KEY, "round": "C2"}, ctx=_ctx(ME, "CAEG"))
    _play(scenario)


def test_주제로_거르는_목록은_볼_수_있는_확인된_주제만_과제와_차수가_정확히_같은_것이다(eng, monkeypatch, hub):
    _caps(monkeypatch, 20)

    async def scenario():
        made = {}
        for q, who, aff, subj in (
                ("내 dv1", ME, "CAEG", {"sp_task": KEY, "round": "dv1"}),
                ("내 dv2", ME, "CAEG", {"sp_task": KEY, "round": "dv2"}),
                ("내 차수 모름", ME, "CAEG", {"sp_task": KEY, "round": "2차"}),
                ("내 주장", ME, "CAEG", {"sp_task": "TC-1001", "round": "dv1"}),
                ("내 주제 없음", ME, "CAEG", None),
                ("내 다른 과제", ME, "CAEG", {"sp_task": OTHER, "round": "dv1"}),
                ("동료 dv1", MATE, "CAEG", {"sp_task": KEY, "round": "dv1"}),
                ("다른 소속 dv1", OUTSIDER, "OTHER", {"sp_task": KEY, "round": "dv1"})):
            out = await m.deliberate_start(q, subject=subj, ctx=_ctx(who, aff))
            made[q] = delib_jobs.get(out["job_id"])
        for job in made.values():
            await _end(eng, job)
        monkeypatch.setattr(delib_jobs, "_JOBS", {})        # 메모리를 비운다 — 목록 색인(파일)만으로 거른다

        async def got(filt, who=ME, aff="CAEG", groups=""):
            r = await m.deliberate_list(subject=filt, ctx=_ctx(who, aff, groups=groups))
            return r, {row["question"] for row in r["jobs"]}

        r, qs = await got({"sp_task": KEY})
        assert qs == {"내 dv1", "내 dv2", "내 차수 모름", "동료 dv1"}, qs
        assert r["rounds"] == {"dv1": 2, "dv2": 1, s.ROUND_UNKNOWN: 1} and r["count"] == 4
        assert r["window"]["max"] == delib_jobs.LIST_SCAN_MAX and r["window"]["complete"] is True and "300" in r["window"]["note"]
        assert "scanned" not in r["window"], "훑은 수는 내지 않는다 — 이 박스의 원장 규모를 내보인다(반대 검토 11-02)"
        assert all(row["subject"]["sp_task"] == KEY for row in r["jobs"])
        mine = {row["question"]: row["mine"] for row in r["jobs"]}
        assert mine["동료 dv1"] is False and mine["내 dv1"] is True
        assert (await got({"sp_task": KEY, "round": "ＤＶ１"}))[1] == {"내 dv1", "동료 dv1"}
        assert (await got({"sp_task": KEY, "round": "unknown"}))[1] == {"내 차수 모름"}, "차수 모름은 그 칸에만"
        assert (await got({"sp_task": KEY, "round": "pv1"}))[1] == set()
        assert (await got({"sp_task": "TC-1001"}))[1] == set(), "확인하지 못한 주제(주장)는 맞대지 않는다"
        assert (await got({"sp_task": "알파 개발"}))[1] == set(), "과제 key 는 글자 그대로 — 부분 일치로 찾지 않는다"
        assert (await got({"sp_task": OTHER}))[1] == {"내 다른 과제"}
        assert (await got({"sp_task": KEY}, who=OUTSIDER, aff="OTHER"))[1] == {"다른 소속 dv1"}, "다른 소속은 제 것만"
        assert (await got({"sp_task": KEY}, who=BOSS, aff="", groups="portal-admin"))[1] == \
            {"내 dv1", "내 dv2", "내 차수 모름", "동료 dv1", "다른 소속 dv1"}, "관리자는 전부"
        plain = await m.deliberate_list(ctx=_ctx(ME, "CAEG"))
        assert len(plain["jobs"]) == 7 and "window" not in plain, "주제 없이 부르면 종전 목록 그대로"
    _play(scenario)


def test_창을_넘긴_원장이_있으면_다_봤다고_말하지_않는다(eng, monkeypatch, hub):
    _caps(monkeypatch, 20)

    async def scenario():
        for i in range(4):
            out = await m.deliberate_start(f"심의 {i}", subject={"sp_task": KEY, "round": "dv1"}, ctx=_ctx(ME, "CAEG"))
            await _end(eng, delib_jobs.get(out["job_id"]))
        monkeypatch.setattr(delib_jobs, "_JOBS", {})
        monkeypatch.setattr(delib_jobs, "LIST_SCAN_MAX", 3)
        r = await m.deliberate_list(limit=1, subject={"sp_task": KEY}, ctx=_ctx(ME, "CAEG"))
        assert r["window"]["max"] == 3 and r["window"]["complete"] is False and r["count"] == 1
        monkeypatch.setattr(delib_jobs, "LIST_SCAN_MAX", 300)
        assert (await m.deliberate_list(limit=1, subject={"sp_task": KEY}, ctx=_ctx(ME, "CAEG")))["window"]["complete"] is False, \
            "한 행만 청해 일찍 멈춘 훑기도 다 본 것이 아니다"
        assert (await m.deliberate_list(limit=20, subject={"sp_task": KEY}, ctx=_ctx(ME, "CAEG")))["window"]["complete"] is True

        def broken(*_a, **_k):
            raise FileNotFoundError("훑는 사이 지워진 원장")
        monkeypatch.setattr(delib_jobs.JOB_DIR.__class__, "glob", broken)
        assert (await m.deliberate_list(subject={"sp_task": KEY}, ctx=_ctx(ME, "CAEG")))["window"]["complete"] is False, \
            "원장 폴더를 다 읽지 못했으면 다 봤다고 말하지 않는다"
    _play(scenario)


def test_목록_색인에_주제가_실리고_주제가_없는_잡의_색인은_종전_꼴이다(monkeypatch, tmp_path):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    subj = {"sp_task": KEY, "sp_state": s.CONFIRMED, "round": "dv1"}
    with_subj = {"id": "default-a", "job": "default", "status": "done", "question": "q", "subject": subj, "turns": []}
    without = {"id": "default-b", "job": "default", "status": "done", "question": "q", "turns": []}
    assert delib_jobs._list_rec(with_subj)["subject"] == subj
    assert "subject" not in delib_jobs._list_rec(without)
    for j in (with_subj, without):
        delib_jobs._persist(j)
    assert delib_jobs._list_load(tmp_path / "default-a.json")["subject"] == subj
    assert "subject" not in delib_jobs.summary(without) and delib_jobs.summary(with_subj)["subject"] == subj
