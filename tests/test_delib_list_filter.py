# deliberate_list 의 날짜 · 글 거름(HWAXPortal docs/request-1125 WD-08) — 최근 창 밖의 옛 심의를 날짜로 찾고, 글로 거른다
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_list_filter.py -q
import asyncio
import datetime as dt
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import mcp_server as m  # noqa: E402

ME = "me@example.com"


def _ctx(user=ME, aff="CAEG"):
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={
        "x-hwax-user": user, "x-hwax-affiliation": aff, "x-hwax-groups": "연구소"})))


def _ts(day: str) -> float:
    return dt.datetime.strptime(day, "%Y-%m-%d").timestamp() + 3600


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "LIST_SCAN_MAX", 3)          # 최근 창을 셋으로 — 옛 잡은 창 밖이다
    rows = [("2026-11-10", "S26 낙하 화면 깨짐 원인", "diagnosis"), ("2026-11-12", "커넥터 접촉 저항 신뢰 판정", "credibility")]
    rows += [(f"2026-11-2{k}", f"최근 심의 {k}", "default") for k in range(5)]
    for i, (day, q, job) in enumerate(rows):
        jid = f"{job}-{day.replace('-', '')}-1000{i:02d}-abc{i:03d}"
        rec = {"id": jid, "job": job, "status": "done", "question": q, "user": ME, "owner_aff": "CAEG",
               "started_at": _ts(day), "finished_at": _ts(day) + 600, "turns": [], "decision": "결론"}
        delib_jobs._persist(rec)
        p = tmp_path / f"{jid}.json"
        os.utime(p, (_ts(day) + 700, _ts(day) + 700))
    return rows


def test_WD08_날짜로_거르면_최근_창_밖의_옛_심의도_찾는다(ledger):
    plain = asyncio.run(m.deliberate_list(limit=1, ctx=_ctx()))          # 창 = max(limit × 3, LIST_SCAN_MAX) = 3
    assert not any("S26" in r["question"] for r in plain["jobs"]), "거름이 없으면 종전대로 최근 창만 본다"
    got = asyncio.run(m.deliberate_list(limit=1, since="2026-11-09", until="2026-11-12", ctx=_ctx()))
    assert got["truncated"] is True and len(got["jobs"]) == 1
    got = asyncio.run(m.deliberate_list(since="2026-11-09", until="2026-11-12", ctx=_ctx()))
    assert [r["question"] for r in got["jobs"]] == ["커넥터 접촉 저항 신뢰 판정", "S26 낙하 화면 깨짐 원인"]
    assert got["filter"] == {"since": "2026-11-09", "until": "2026-11-12"} and got["window"]["complete"] is True


def test_WD08_글로_거른다(ledger):
    got = asyncio.run(m.deliberate_list(query="화면 깨짐", ctx=_ctx()))
    assert [r["question"] for r in got["jobs"]] == ["S26 낙하 화면 깨짐 원인"]
    assert [r["question"] for r in asyncio.run(m.deliberate_list(query="CREDIBILITY", ctx=_ctx()))["jobs"]] == ["커넥터 접촉 저항 신뢰 판정"]


def test_WD08_잘못된_날짜는_거절한다(ledger):
    with pytest.raises(ValueError):
        asyncio.run(m.deliberate_list(since="11월 10일", ctx=_ctx()))
    with pytest.raises(ValueError):
        asyncio.run(m.deliberate_list(since="2026-11-20", until="2026-11-10", ctx=_ctx()))


def test_WD08_새_인자는_목록_도구_스키마에_있다():
    tools = {t.name: t for t in asyncio.run(m.mcp.list_tools())}
    props = tools["deliberate_list"].inputSchema["properties"]
    assert {"since", "until", "query"} <= set(props) and "깊이" in tools["deliberate_list"].description
