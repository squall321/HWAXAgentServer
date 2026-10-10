# deliberate_list 와 재기동 정리가 큰 잡 원장 파일을 통째로 읽지 않고 목록 색인만 읽는지 본다
"""검토 engine-contract:A-ledger-growth — 잡 원장 파일이 커졌고 목록 한 번이 원장 파일을 최대 300개 통째로 읽었다.

넓은 창에서 턴마다 say 와 say_full(무절단 — 1라운드 해석·우려, 3라운드 양보 불가 조항까지)을 함께 적어 원장 파일 하나가 수백
KB~수 MB 가 됐다. list_jobs 는 남의 잡을 세지 않고 지나가므로, 제 잡이 드문 사람이 목록을 한 번 부르면 mtime 상위 300개 파일을
전부 json.loads 했다(300 × 2~6MB). 재기동 정리(reap_orphans)는 도는 잡을 찾으려고 파일 전부를 읽었다. 목록 한 행에 쓰는 것은
상태·화두·좌석 키·턴 수 같은 몇 칸뿐이다.

읽은 양은 이 스레드가 read() 로 받은 바이트(/proc/thread-self/io 의 rchar)로 잰다 — 구현이 어떤 함수로 파일을 열든 같다.
원장은 생산자가 쓴다 — 심의 스트림을 실제로 돌린 잡(delib_jobs._drive 가 원장에 쓴다)과 그 잡을 _persist 로 쓴 남의 잡들.

  실행:  .venv/bin/python -m pytest tests/test_delib_list_index.py -q
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402

from test_delib_silent_drops import _pin_context  # noqa: E402, F401
from test_mcp_pages import _job  # noqa: E402

ME = "me@example.com"


def _rchar() -> int:
    """이 스레드가 지금까지 read() 로 받은 바이트."""
    for p in ("/proc/thread-self/io", "/proc/self/io"):
        try:
            with open(p, encoding="ascii") as f:
                for line in f:
                    if line.startswith("rchar:"):
                        return int(line.split()[1])
        except OSError:
            continue
    pytest.skip("/proc 의 io 회계가 없다 — 읽은 양을 잴 수 없다")


def _read_by(fn):
    before = _rchar()
    out = fn()
    return out, _rchar() - before


def _ledger(monkeypatch, tmp_path, others: int = 30) -> dict:
    """내 잡 하나(넓은 창 실주행)와 같은 크기의 남의 잡 others 개. 내 잡이 가장 오래돼 목록이 남의 잡을 다 훑고 지나가야 닿는다."""
    job = _job(monkeypatch, tmp_path, user=ME, job_id="risk-review-20261010-000000-mine00")
    for k in range(others):
        delib_jobs._persist({**job, "id": f"risk-review-20261010-0001{k:02d}-other{k:02d}",
                             "user": f"u{k}@example.com"})
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    return job


def test_목록은_큰_원장_파일을_통째로_읽지_않는다(monkeypatch, tmp_path):
    job = _ledger(monkeypatch, tmp_path)
    big = delib_jobs._path(job["id"]).stat().st_size
    assert big > 300_000, f"원장 파일이 {big:,}바이트뿐이다 — 시험 전제(큰 원장)가 깨졌다"
    rows, used = _read_by(lambda: delib_jobs.list_jobs(1, viewer=ME))
    assert [r["job_id"] for r in rows] == [job["id"]], rows
    assert used < big, (f"목록 한 번이 {used:,}바이트를 읽었다 — 원장 파일 하나({big:,}바이트)보다 많다. "
                        "남의 잡 30개를 지나가며 원장을 통째로 읽은 것이다")


def test_재기동_정리는_끝난_잡의_원장을_통째로_읽지_않는다(monkeypatch, tmp_path):
    job = _ledger(monkeypatch, tmp_path, others=10)
    big = delib_jobs._path(job["id"]).stat().st_size
    orphan = {**job, "id": "default-20261010-000001-orphan", "status": "running", "turns": [], "user": ""}
    delib_jobs._persist(orphan)
    n, used = _read_by(delib_jobs.reap_orphans)
    assert n == 1 and delib_jobs._load(orphan["id"])["status"] == "interrupted"
    assert delib_jobs._load(job["id"])["status"] == "done", "끝난 잡은 건드리지 않는다"
    assert used < big, f"재기동 정리가 {used:,}바이트를 읽었다 — 끝난 잡 11개의 원장({big:,}바이트씩)을 통째로 읽었다"


def _rows_from_ledger(viewer: str = "") -> list:
    """원장 파일 전부를 읽어 만든 목록 행 — 색인이 없던 때의 list_jobs 와 같은 행이다(비교 기준)."""
    jobs = [delib_jobs._load(f.stem) for f in delib_jobs.JOB_DIR.glob("*.json")]
    jobs = [j for j in jobs if j and not delib_jobs._foreign(j, viewer)]
    jobs.sort(key=lambda j: j.get("started_at") or j.get("queued_at") or 0, reverse=True)
    me = viewer.strip().lower()
    return [{**delib_jobs.summary(j), "mine": bool(me and delib_jobs._owner(j) == me)} for j in jobs]


def test_색인으로_만든_목록_행은_원장으로_만든_행과_같다(monkeypatch, tmp_path):
    job = _job(monkeypatch, tmp_path, seats=3, rounds=2, user=ME, job_id="default-20261010-000000-base00")
    t0 = job["started_at"]
    variants = [
        {"report_id": 7, "title": "보고서"},
        {"status": "error", "error": "LLM 시간 초과 (설정 DELIB_TIMEOUT_S)"},
        {"status": "cancelled", "error": "취소됨 — 줄을 선 채로 접었다(시작하지 않았다)", "started_at": None,
         "queued_at": t0 - 50},
        {"status": "interrupted", "error": "서버 재기동으로 중단됨 — 다시 시작해야 한다"},
        {"opts": {**(job.get("opts") or {}), "sealed": 1}, "user": "you@example.com"},
        {"checkpoint": {"round": 1, "positions": ["가"]}, "queued_at": t0 - 30},
        {"user": "", "seats": [], "turns": []},
    ]
    for k, v in enumerate(variants):
        delib_jobs._persist({**job, **v, "id": f"default-20261010-00000{k}-var{k:03d}",
                             "started_at": v.get("started_at", t0 - 100 * (k + 1))})
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    for viewer in ("", ME):
        got = delib_jobs.list_jobs(100, viewer=viewer)
        assert got == _rows_from_ledger(viewer), f"viewer={viewer!r}: 색인으로 만든 행이 원장으로 만든 행과 다르다"
    assert {r["turn_count"] for r in delib_jobs.list_jobs(100)} == {0, len(job["turns"])}


def test_색인이_원장보다_오래됐으면_원장을_읽고_색인을_다시_쓴다(monkeypatch, tmp_path):
    """색인 쓰기가 실패했거나 색인 없는 판이 원장만 고쳐 썼으면 색인은 원장과 어긋난다 — 그대로 믿으면 옛 상태가 목록에 뜬다.
    원장의 mtime 을 앞당겨 둔다(다른 박스에서 옮겨 온 원장처럼) — 시각을 견주면 다시 쓴 색인도 영영 '오래됐다' 로 읽힌다."""
    job = _ledger(monkeypatch, tmp_path, others=0)
    main = delib_jobs._path(job["id"])
    changed = {**job, "status": "error", "error": "다른 판이 고쳐 썼다"}
    main.write_text(json.dumps(changed, ensure_ascii=False, indent=1), encoding="utf-8")
    later = delib_jobs._list_path(job["id"]).stat().st_mtime_ns + 2_000_000_000
    os.utime(main, ns=(later, later))
    rows = delib_jobs.list_jobs(5, viewer=ME)
    assert [(r["status"], r["error"]) for r in rows] == [("error", "다른 판이 고쳐 썼다")], rows
    rec = json.loads(delib_jobs._list_path(job["id"]).read_text(encoding="utf-8"))
    assert rec["src"] == delib_jobs._stamp(main.stat()) and rec["status"] == "error", "색인을 다시 쓰지 않았다"
    _rows, used = _read_by(lambda: delib_jobs.list_jobs(5, viewer=ME))
    assert used < main.stat().st_size, "다시 쓴 색인을 두고 또 원장을 통째로 읽었다"


def test_색인_이전의_옛_원장도_목록에_뜨고_다음부터는_색인만_읽는다(monkeypatch, tmp_path):
    job = _ledger(monkeypatch, tmp_path, others=0)
    delib_jobs._list_path(job["id"]).unlink()                       # 색인을 쓰지 않던 판이 남긴 원장
    assert [r["job_id"] for r in delib_jobs.list_jobs(5, viewer=ME)] == [job["id"]]
    assert delib_jobs._list_path(job["id"]).exists(), "옛 원장을 읽고 색인을 만들지 않았다"
    _rows, used = _read_by(lambda: delib_jobs.list_jobs(5, viewer=ME))
    assert used < delib_jobs._path(job["id"]).stat().st_size


def test_색인_폴더는_원장_파일로_세지_않는다(monkeypatch, tmp_path):
    """색인은 원장 옆 하위 폴더에 있다 — 원장의 glob('*.json')(목록·재기동 정리)에 섞이면 색인을 잡으로 읽는다."""
    job = _ledger(monkeypatch, tmp_path, others=2)
    names = sorted(f.name for f in delib_jobs.JOB_DIR.glob("*.json"))
    assert len(names) == 3 and all(n.endswith(".json") for n in names)
    assert sorted(f.name for f in (delib_jobs.JOB_DIR / delib_jobs._LIST_DIR).glob("*.json")) == names
    assert delib_jobs.reap_orphans() == 0 and delib_jobs._load(job["id"])["status"] == "done"
