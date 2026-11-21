# 이용 통계 창구(/usage-rollup) — MCP 심의 잡 원장을 KST 날마다 세어 내는지, 공유 비밀 뒤에 있는지 본다(HWAXPortal docs/ops-dashboard M2)
"""  · 잡은 줄을 서거나 돌기 시작한 KST 날에 든다. 종류 · 상태 · 판단 LLM 마다 수 · 보고서가 붙은 수 · 좌석 · 라운드 · 걸린 시간의 합.
  · 사람별 줄은 소문자 메일(퍼센트로 적힌 주인은 푼다)이고 그때의 소속을 함께 낸다. 주인이 없는 잡은 수만 센다.
  · 뺄 계정은 잡 수 · 사람 줄 모두에서 빠진다. 화두 · 결정문 · 라벨은 싣지 않는다.
  · 원장은 목록 색인으로 읽는다(창 앞에서 멈춘 원장은 열지 않는다).

  실행:  .venv/bin/python -m pytest tests/test_usage_rollup.py -q
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402
import usage_rollup as ur  # noqa: E402

KST = timezone(timedelta(hours=9))
ME = "me@example.com"
AUTH = {"Authorization": "Bearer gw-secret"}


def _kst(d, h=12, mi=0) -> float:
    return datetime(2026, 11, d, h, mi, tzinfo=KST).timestamp()


def _put(jid, *, started=None, finished=None, queued=None, **kw):
    job = {"id": jid, "job": "default", "kind": "default", "label": "자유 심의", "status": "done", "question": "비밀 화두 원문",
           "decision": "비밀 결정문", "started_at": started, "finished_at": finished, "queued_at": queued, "updated_at": finished or started or queued,
           "user": ME, "owner_aff": "CAEG", "seats": [{"key": "a"}, {"key": "b"}, {"key": "c"}], "round": 2, "total_rounds": 3,
           "report_id": None, "opts": {}, "turns": [], **kw}
    delib_jobs._persist(job)
    return job


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    return tmp_path


def _view(**kw):
    return ur.view({"since": "2026-11-01", "until": "2026-11-30", **kw})


def _jobs(v):
    return {(r["day"], r["kind"], r["status"], r["llm"]): r for r in v["jobs"]}


def test_잡은_시작한_KST_날에_들고_종류_상태마다_센다(ledger):
    _put("a1", started=_kst(2, 23, 59), finished=_kst(3, 0, 30), report_id=77)          # 11-02 에 시작해 11-03 에 끝났다
    _put("a2", started=_kst(3, 0, 5), finished=_kst(3, 0, 15), round=3)
    _put("a3", started=_kst(3, 9), status="running", finished=None)
    _put("a4", job="sim-plan", kind="sim-plan", started=_kst(3, 10), finished=_kst(3, 11), status="error")
    _put("a5", queued=_kst(4, 8), started=None, finished=_kst(4, 9), status="cancelled", seats=[], round=0)   # 줄만 서다 접혔다
    v = _view()
    got = _jobs(v)
    assert set(got) == {("2026-11-02", "default", "done", "glm"), ("2026-11-03", "default", "done", "glm"),
                        ("2026-11-03", "default", "running", "glm"), ("2026-11-03", "sim-plan", "error", "glm"),
                        ("2026-11-04", "default", "cancelled", "glm")}
    first = got[("2026-11-02", "default", "done", "glm")]
    assert (first["n"], first["with_report"], first["seats"], first["seated"], first["rounds"], first["dur_s"], first["dur_n"]) == \
        (1, 1, 3, 1, 2, 1860.0, 1)
    assert got[("2026-11-03", "default", "running", "glm")]["dur_n"] == 0, "아직 도는 잡은 걸린 시간이 없다"
    cut = got[("2026-11-04", "default", "cancelled", "glm")]
    assert (cut["n"], cut["seated"], cut["dur_n"]) == (1, 0, 0), "시작도 못 한 잡은 좌석 · 걸린 시간을 세지 않는다"
    assert v["contract"] == ur.CONTRACT and v["tz"] == "Asia/Seoul" and (v["since"], v["until"]) == ("2026-11-01", "2026-11-30")


def test_외부_응답자_잡은_판단_LLM_을_따로_적는다(ledger):
    _put("x1", started=_kst(3), finished=_kst(3, 13), opts={"llm": "external"}, tool_mode="tools")
    assert set(_jobs(_view())) == {("2026-11-03", "default", "done", "external")}


def test_사람별_줄은_소문자_메일과_그때의_소속이고_주인_없는_잡은_수만_센다(ledger):
    _put("p1", started=_kst(3), finished=_kst(3, 13), user="Me%40Example.com")           # 게이트웨이 머리의 원문(퍼센트 인코딩)
    _put("p2", started=_kst(3, 14), finished=_kst(3, 15))
    _put("p3", started=_kst(4), finished=_kst(4, 13), user="you@example.com", owner_aff="LAB")
    _put("p4", started=_kst(4), finished=_kst(4, 13), user="", owner_aff="")
    v = _view()
    assert v["actors"] == [{"day": "2026-11-03", "user": ME, "aff": "CAEG", "n": 2},
                           {"day": "2026-11-04", "user": "you@example.com", "aff": "LAB", "n": 1}]
    assert v["anon"] == [{"day": "2026-11-04", "n": 1}]
    assert sum(r["n"] for r in v["jobs"]) == 4, "주인 없는 잡도 잡 수에는 든다"


def test_뺄_계정은_잡_수와_사람_줄에서_빠진다(ledger):
    _put("e1", started=_kst(3), finished=_kst(3, 13), user="tester@example.com")
    _put("e2", started=_kst(3), finished=_kst(3, 13))
    v = _view(exclude=[" Tester@Example.com "])
    assert sum(r["n"] for r in v["jobs"]) == 1 and [x["user"] for x in v["actors"]] == [ME] and v["excluded"] == 1
    assert sum(r["n"] for r in _view()["jobs"]) == 2


def test_기간_밖의_잡은_세지_않고_화두_결정문_라벨은_싣지_않는다(ledger):
    _put("in", started=_kst(3), finished=_kst(3, 13))
    _put("early", started=_kst(1, 23, 59), finished=_kst(2, 1))
    _put("late", started=_kst(5, 0, 0), finished=_kst(5, 1))
    v = ur.view({"since": "2026-11-02", "until": "2026-11-04"})
    assert [(r["day"], r["n"]) for r in v["jobs"]] == [("2026-11-03", 1)]
    blob = json.dumps(v, ensure_ascii=False)
    for secret in ("비밀 화두", "비밀 결정문", "자유 심의"):
        assert secret not in blob, secret
    assert "웹" in v["note"], "웹 심의가 빠진다는 것을 답이 말한다"


def test_이상한_종류_상태는_한_칸으로_접는다(ledger):
    _put("w1", started=_kst(3), finished=_kst(3, 13), job="<script>", kind="<script>", status="Weird Status!")
    assert set(_jobs(_view())) == {("2026-11-03", "(other)", "(other)", "glm")}


def test_창_앞에서_멈춘_원장은_열지_않는다(ledger, monkeypatch):
    _put("old", started=_kst(1), finished=_kst(1, 13))
    _put("new", started=_kst(10), finished=_kst(10, 13))
    old = _kst(1, 14)
    os.utime(ledger / "old.json", (old, old))
    opened = []
    real = delib_jobs._list_load
    monkeypatch.setattr(delib_jobs, "_list_load", lambda f: opened.append(f.stem) or real(f))
    v = ur.view({"since": "2026-11-09", "until": "2026-11-11"})
    assert opened == ["new"] and [r["day"] for r in v["jobs"]] == ["2026-11-10"]


def test_메모리의_잡을_원장보다_먼저_읽는다(ledger):
    """도는 잡은 메모리가 가장 새것이다 — 원장 파일은 마지막으로 적은 때의 상태다."""
    job = _put("m1", started=_kst(3), finished=None, status="running")
    delib_jobs._JOBS["m1"] = {**job, "status": "done", "finished_at": _kst(3, 12, 10)}
    got = _jobs(_view())
    assert set(got) == {("2026-11-03", "default", "done", "glm")} and got[("2026-11-03", "default", "done", "glm")]["dur_s"] == 600.0


def test_원장의_첫_날은_잡_id_의_날짜에서_읽는다(ledger):
    assert _view()["ledger"] == {"first_day": None, "scanned": 0}
    _put("default-20261102-235900-abc123", started=_kst(2, 23, 59), finished=_kst(3))
    _put("sim-plan-20260914-101500-ffffff", started=datetime(2026, 9, 14, 10, 15, tzinfo=KST).timestamp(), finished=None, status="error")
    _put("odd-name", started=_kst(3), finished=_kst(3, 13))
    v = _view()
    assert v["ledger"]["first_day"] == "2026-09-14" and v["ledger"]["scanned"] == 3


def test_물음의_꼴이_틀리면_거절한다(ledger):
    for bad in ({"since": "2026-11-05", "until": "2026-11-01"}, {"since": "어제"}, {"since": "2026-11-01", "until": "2026-11-02", "exclude": "x"},
                {"since": "2025-01-01", "until": "2026-11-02"}):
        with pytest.raises(ValueError):
            ur.view(bad)
    v = ur.view({}, today="2026-11-21")
    assert (v["since"], v["until"]) == ("2026-11-19", "2026-11-21")


# ── 창구 ────────────────────────────────────────────────────────────────────
def test_창구는_공유_비밀만_받고_사람_신원을_실은_호출은_받지_않는다(ledger, monkeypatch):
    _put("ok", started=_kst(3), finished=_kst(3, 13))
    import seat_box
    monkeypatch.setattr(seat_box, "secret", lambda _app: "Bearer gw-secret")
    c = TestClient(a.app)
    q = {"since": "2026-11-01", "until": "2026-11-30"}
    assert c.get("/usage-rollup", params=q).status_code == 401
    assert c.post("/usage-rollup", json=q, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/usage-rollup", params=q, headers={**AUTH, "x-hwax-user": ME}).status_code == 403
    assert c.post("/usage-rollup", json=q, headers={**AUTH, "x-hwax-user": ME}).status_code == 403
    r = c.get("/usage-rollup", params=q, headers=AUTH)
    assert r.status_code == 200 and sum(x["n"] for x in r.json()["jobs"]) == 1
    r = c.post("/usage-rollup", json={**q, "exclude": [ME]}, headers=AUTH)
    assert r.status_code == 200 and r.json()["jobs"] == [] and r.json()["actors"] == []
    monkeypatch.setattr(seat_box, "secret", lambda _app: "")
    assert c.get("/usage-rollup", params=q, headers=AUTH).status_code == 503


def test_창구는_틀린_물음을_422_로_답한다(ledger, monkeypatch):
    import seat_box
    monkeypatch.setattr(seat_box, "secret", lambda _app: "Bearer gw-secret")
    c = TestClient(a.app)
    assert c.get("/usage-rollup", params={"since": "2026-11-05", "until": "2026-11-01"}, headers=AUTH).status_code == 422
    assert c.post("/usage-rollup", content=b"{", headers=AUTH).status_code == 422
    assert c.post("/usage-rollup", json=[1], headers=AUTH).status_code == 422
    r = c.post("/usage-rollup", json={"since": "2026-11-01", "until": "2026-11-02", "extra": 1}, headers=AUTH)
    assert r.status_code == 422 and "extra" in r.text, "모르는 칸은 조용히 버리지 않는다"
