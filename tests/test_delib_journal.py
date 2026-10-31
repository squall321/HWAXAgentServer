# 진행 일지(HA-09 S0) — 도는 잡의 사건이 한 줄씩 남고, 접으면 사건마다 죽기 직전의 메모리와 같고, 반쯤 쓴 줄 · 상한 · 권한 · 지우기 · 내려갈 때 접기가 설계대로인지 본다
#
# 원장은 띄울 때와 끝날 때만 쓴다. 재기동은 그 finally 를 돌리지 않아(uvicorn 이 정상 종료 뒤 신호를 다시 올린다 · KILL) 끊긴 잡의 원장이 늘
# 띄울 때 모양이었다. 이제 _drive 가 반영한 사건을 <잡 원장>/journal/<id>.jsonl 에 덧붙이고 재기동 정리가 같은 _apply 로 접는다
# (HWAXPortal docs/request-1027/design-ha09-ha20.md A5.1). **오라클은 끊기지 않은 실행의 메모리다** — 엔진이 사건 하나를 낼 때마다 그때의
# 메모리 · 일지 · 원장을 떠 두고, 원장 위에 일지를 접은 것이 그 메모리와 같아야 한다(모든 앞부분에서). 진짜 프로세스(KILL · TERM)는
# tests/test_ha09_restart_e2e.py 가 본다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_journal.py -q
import asyncio
import copy
import json
import os
import select
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_job_queue import ME, YOU, _caps, _end, _on_disk, _play, _start, _tick, eng  # noqa: E402, F401
from test_delib_ops import _ctx, _rounds_script  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401

SEATS = ("기구", "열", "보드")


def _stream(n_status=34, n_omit=36, n_warn=12) -> list:
    """엔진이 내는 꼴의 사건 — 단계 · 좌석 · 턴 · 근거(준 것 · 안 준 것) · 상태줄 · 경고 · 체크포인트 · 결정문 · 결과, 그리고 원장이 읽지 않는
    토큰 조각 · ping. 창 · 상한(상태줄 30 · 경고 10 · 안 준 근거 30+5 · 전사 TURN_MAX)을 넘기는 수만큼 낸다."""
    ev = [d._sse("status", {"step": "심의 시작 — 전문 페르소나 발굴 중", "tool": "recommend_agents"}),
          d._delib("stage", stage="discover"),
          d._delib("personas", totalRounds=3, personas=[{"key": k, "role": f"{k} 역할", "origin": "primary"} for k in SEATS])]
    ev += [d._sse("status", {"step": f"역할 복원 {i}", "tool": None, **({"knob": "DELIB_X"} if i % 5 == 0 else {})})
           for i in range(n_status)]
    ev.append(d._delib("evidence", source="준 근거", text="근거 본문 " * 400, included=True))
    ev += [d._delib("evidence", source=f"안 준 근거 {i}", text="빠진 근거 " * 90, included=False,
                    **({"knob": "DELIB_EVID"} if i % 7 == 0 else {})) for i in range(n_omit)]
    for r in (1, 2, 3):
        ev.append(d._delib("stage", stage=f"r{r}", n=len(SEATS)))
        for k in SEATS:
            ev.append(d._delib("turn", round=r, display_round=r, persona=k, say=f"{k} {r}R 줄임", say_full=f"{k} {r}R 입장 전문 " * 30,
                               say_full_chars=999, stance="동의"))
        ev.append(d._sse("token", {"delta": "흘린 조각"}))
        ev.append(d._sse("ping", {"idle_s": 15, "ts": 1}))
    ev += [d._sse("warning", {"message": f"경고 {i}", **({"knob": "W"} if i % 3 == 0 else {})}) for i in range(n_warn)]
    ev += [d._delib("checkpoint", stage="sim-1", decision_a="1단 결정문", personas_a=[{"key": "기구", "role": "역할"}], nn_a=["조항"]),
           d._delib("stage", stage="decide"), d._delib("decision", text="결정문 전문"), d._delib("stage", stage="explain"),
           d._delib("plain", text="쉬운 설명"), d._delib("stage", stage="report"), d._delib("outcome", report_id=7, title="보고서 제목"),
           d._sse("result", {"type": "text", "content": "결정문 전문"}), d._sse("error", {"message": "끝에 난 오류", "knob": "E"}),
           d._sse("done", {})]
    return ev


def _ledger_bytes(jid: str) -> bytes:
    return delib_jobs._path(jid).read_bytes()


def _journal_bytes(jid: str) -> bytes:
    p = delib_jobs._journal_path(jid)
    return p.read_bytes() if p.exists() else b""


def _run_snapshots(monkeypatch, chunks: list) -> tuple[dict, list]:
    """잡 하나를 끝까지 돌리며 사건마다 (메모리 · 일지 · 원장) 를 뜬다. 엔진이 다음 사건을 낼 차례가 되면 앞 사건은 _drive 가 반영 · 적기를
    끝낸 뒤다 — 그 자리에서 뜬다."""
    snaps = []

    async def entry(_app, q, groups, opts, user_email, user_pat, _x, **_k):
        jid = next(iter(delib_jobs._JOBS))
        for c in chunks:
            yield c
            job = delib_jobs._JOBS[jid]
            snaps.append((copy.deepcopy(job), _journal_bytes(jid), _ledger_bytes(jid)))

    monkeypatch.setattr(d, "run_deliberation", entry)

    async def go():
        job = delib_jobs.start(object(), "default", "힌지 크랙 원인", user_email=ME, delib_opts={"human_note": "현장 의견"})
        await asyncio.wait([delib_jobs._TASKS[job["id"]]])
        await _tick()
        return job
    return asyncio.run(go()), snaps


def _but_warnings(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("warnings")}


def _fold(ledger: bytes, journal: bytes, jid: str) -> tuple[dict, int]:
    """떠 둔 원장(띄울 때 모양) 위에 떠 둔 일지를 접는다 — 일지 자리에 잠깐 썼다가 지운다(시험이 남긴 파일이 판정을 흐리지 않게)."""
    led = json.loads(ledger)
    p = delib_jobs._journal_path(jid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(journal)
    try:
        return led, delib_jobs._fold_journal(led)
    finally:
        p.unlink()


@pytest.fixture
def jobdir(eng):
    return delib_jobs.JOB_DIR


# ── 접으면 메모리와 같다 ─────────────────────────────────────────────────────
def test_사건마다_접은_원장이_그때의_메모리와_같고_도는_동안_원장은_띄울_때_모양이다(jobdir, monkeypatch):
    monkeypatch.setattr(delib_jobs, "TURN_MAX", 5)               # 전사 상한을 넘는 턴의 줄인 꼴도 지난다
    job, snaps = _run_snapshots(monkeypatch, _stream())
    jid = job["id"]
    assert len(snaps) == len(_stream())
    assert _on_disk(job)["status"] == "error" and not delib_jobs._journal_path(jid).exists(), "끝난 잡의 일지가 남았다"
    seen_t = None
    for i, (mem, jbytes, ledger) in enumerate(snaps):
        assert json.loads(ledger)["stage"] == "start", f"{i}번째 사건 — 도는 동안 원장이 바뀌었다(시험 전제)"
        led, n = _fold(ledger, jbytes, jid)
        # 일지에 적지 않는 사건(토큰 조각 · ping · done)은 마지막 시각만 바꾼다 — 접은 원장의 시각은 마지막으로 **적은** 사건의 시각이다
        if i == 0 or jbytes.count(b"\n") > snaps[i - 1][1].count(b"\n"):
            seen_t = mem["updated_at"]
        want = {**mem, "updated_at": seen_t}
        assert led == want, f"{i}번째 사건 뒤 접은 원장이 메모리와 다르다 — " + json.dumps(
            {k: (led.get(k), want.get(k)) for k in set(led) | set(want) if led.get(k) != want.get(k)}, ensure_ascii=False)[:1500]
    final = snaps[-1][0]
    assert final["turns"][-1]["note"].startswith("전사 상한 5턴 초과") and final["evidence_omitted"][30]["note"].startswith("상한 30건 초과")
    assert final["steps_total"] > 30 and final["warnings_total"] > 10 and final["checkpoint"]["stage"] == "sim-1"
    text = snaps[-1][1].decode("utf-8")
    assert "근거 본문" not in text, "좌석에 준 근거 본문을 일지에 적었다(원장도 싣지 않는다)"
    assert "흘린 조각" not in text and '"k": "ping"' not in text and '"k": "done"' not in text, "원장이 읽지 않는 사건을 적었다"


def test_반쯤_쓴_마지막_줄은_버리고_그_앞까지_접는다(jobdir, monkeypatch):
    job, snaps = _run_snapshots(monkeypatch, _stream(n_status=3, n_omit=2, n_warn=2))
    jid = job["id"]
    grew = [i > 0 and snaps[i][1].count(b"\n") == snaps[i - 1][1].count(b"\n") + 1 for i in range(len(snaps))]
    k = next(i for i in range(len(snaps) // 2, len(snaps)) if grew[i] and grew[i - 1])     # 잇달아 일지에 적힌 두 사건의 뒤엣것
    (before, jb_before, ledger), (mem, jb, _) = snaps[k - 1], snaps[k]
    assert jb.startswith(jb_before) and jb[len(jb_before):].count(b"\n") == 1, "시험 전제 — 사건 하나가 한 줄이다"
    for cut in range(len(jb_before), len(jb)):                       # 마지막 줄의 모든 자리에서 끊는다(줄바꿈 앞까지)
        led, _n = _fold(ledger, jb[:cut], jid)
        assert led == before, f"{cut - len(jb_before)}바이트까지 쓴 줄을 읽었다"
    led, _n = _fold(ledger, jb, jid)
    assert led == mem
    led, _n = _fold(ledger, jb + b'{"t": 1.0, "k": "delib", "d": {"kind": "stage", "stage": "r9"', jid)
    assert led == mem, "줄바꿈 없는 꼬리를 읽었다"
    led, _n = _fold(ledger, jb_before + "{깨진 줄}\n".encode() + jb[len(jb_before):], jid)       # 가운데 깨진 줄 하나
    assert led["warnings"][-1] == "진행 일지의 깨진 줄 1개를 건너뛰고 접었다", led.get("warnings")
    assert _but_warnings(led) == _but_warnings(mem), "깨진 줄 뒤의 사건을 접지 않았다"


@pytest.mark.parametrize("head", [None, {"k": "j:head", "v": 2}, {"k": "j:head", "v": 1, "id": "다른-잡"}, {"k": "delib"}])
def test_머리가_없거나_다른_꼴_다른_잡의_일지는_접지_않는다(jobdir, monkeypatch, head):
    job, snaps = _run_snapshots(monkeypatch, _rounds_script())
    mem, jb, ledger = snaps[-1]
    lines = jb.split(b"\n")
    lines[0] = b"" if head is None else json.dumps({"t": 1.0, **head}).encode()
    if head is None:
        lines = lines[1:]
    led, n = _fold(ledger, b"\n".join(lines), job["id"])
    assert n == 0 and led == json.loads(ledger), "꼴이 다른 일지를 접었다"


def test_상한에_닿으면_한_줄_적고_멈추며_접은_원장은_그_앞까지의_메모리다(jobdir, monkeypatch):
    _job, snaps = _run_snapshots(monkeypatch, _rounds_script(done=3, total=4))
    cap = len(snaps[4][1]) + 10                                       # 다섯째 사건 언저리에서 닿는 크기
    monkeypatch.setattr(delib_jobs, "JOURNAL_MAX_BYTES", cap)
    delib_jobs._JOBS.clear()
    job, snaps = _run_snapshots(monkeypatch, _rounds_script(done=3, total=4))
    jb = snaps[-1][1]
    tail = json.loads(jb.splitlines()[-1])
    assert tail["k"] == "j:cap" and tail["max_mb"] == 0 and len(jb) - len(jb.splitlines()[-1]) - 1 <= cap, "상한을 넘겨 적었다"
    at = next(i for i, s in enumerate(snaps) if b'"j:cap"' in s[1])             # 상한에 닿은 사건 — 그 사건부터는 적지 않았다
    assert 3 <= at < len(snaps) - 1 and all(s[1] == jb for s in snaps[at:]), "상한 뒤에도 적었다"
    led, _n = _fold(snaps[-1][2], jb, job["id"])
    assert _but_warnings(led) == _but_warnings(snaps[at - 1][0]), "상한 뒤 접은 원장이 상한 앞의 메모리와 다르다(앞부분이 아니다)"
    assert "진행 일지가 상한" in led["warnings"][-1] and "DELIB_JOURNAL_MAX_MB" in led["warnings"][-1]


# ── 권한 · 지우기 ───────────────────────────────────────────────────────────
def test_일지는_폴더_0700_파일_0600_이고_기동_때_넓은_것을_좁힌다(monkeypatch, tmp_path, eng):
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o2775)
    monkeypatch.setattr(delib_jobs, "JOB_DIR", shared)
    _caps(monkeypatch, 1)
    eng.script["가"] = _rounds_script()
    old = os.umask(0o002)
    try:
        async def scenario():
            a = _start("가", ME)
            await _tick()
            p = delib_jobs._journal_path(a["id"])
            assert stat.S_IMODE(p.stat().st_mode) == 0o600 and stat.S_IMODE(p.parent.stat().st_mode) == 0o700
            return a
        _play(scenario)
    finally:
        os.umask(old)
    (shared / "journal").chmod(0o2775)
    wide = shared / "journal" / "옛-잡.jsonl"
    wide.write_text("{}\n")
    wide.chmod(0o664)
    delib_jobs._tighten_existing()
    assert stat.S_IMODE((shared / "journal").stat().st_mode) == 0o700 and stat.S_IMODE(wide.stat().st_mode) == 0o600


def test_끝난_잡은_일지를_지우고_끊긴_잡만_남기며_쓸기는_시작_인자_파일과_같은_규칙이다(eng, monkeypatch):
    _caps(monkeypatch, 4)
    for q in ("끝", "오류", "취소", "끊김"):
        eng.script[q] = _rounds_script()
    keep = {}

    async def scenario():
        jobs = {q: _start(q, ME) for q in ("끝", "오류", "취소", "끊김")}
        await _tick()
        assert all(delib_jobs._journal_path(j["id"]).exists() for j in jobs.values())
        await _end(eng, jobs["끝"])
        await _end(eng, jobs["오류"], fail=True)
        delib_jobs.cancel(jobs["취소"]["id"])
        await _tick()
        keep["shutdown"] = await delib_jobs.shutdown()
        return jobs
    try:
        jobs = _play(scenario)
    finally:
        delib_jobs.closing(False)
    left = {q for q, j in jobs.items() if delib_jobs._journal_path(j["id"]).exists()}
    assert left == {"끊김"} and keep["shutdown"] == 1, f"남은 일지 {left} — 끊긴 잡의 것만 남아야 한다(근거 · 발언)"
    assert [_on_disk(jobs[q])["status"] for q in ("끝", "오류", "취소", "끊김")] == ["done", "error", "cancelled", "interrupted"]
    jid = jobs["끊김"]["id"]
    delib_jobs._persist({**_on_disk(jobs["끊김"]), "finished_at": delib_jobs._now() - delib_jobs.ARGS_KEEP_S - 5})
    delib_jobs._args_path(jid).unlink()                    # 시작 인자 파일이 없는 잡(토큰을 쥔 잡 · 쓰기 실패)의 일지도 쓸기가 본다
    for store in (delib_jobs._JOBS, delib_jobs._TASKS, delib_jobs._PENDING):
        store.clear()
    m.bind(object())
    assert not delib_jobs._journal_path(jid).exists(), "7일 지난 끊긴 잡의 일지가 남았다"


_NO_PID = 4194304 + 7          # 커널의 PID 상한(PID_MAX_LIMIT 2^22) 밖 — 늘 죽은 프로세스다(재사용될 수 없다)


def test_고쳐_쓰다_남은_임시_파일은_기동_때_치우고_쓰던_프로세스가_살아_있으면_둔다(monkeypatch, tmp_path):
    """시작 인자 파일(_write_args)과 제자리 이어하기의 일지(_rewrite_journal)는 임시 파일(.<잡 id>.<pid>.tmp — 0600)에 다 쓴 뒤 바꿔 넣는다. 그 사이에
    죽으면 임시 파일이 남는데, 쓸기는 진짜 파일(*.json · *.jsonl)만 봐 근거 본문 · 지식카드 · 원 출력이 잡이 끝나도 7일이 지나도 남았다(검토
    10-31). 기동 정리가 치운다 — 쓰던 프로세스가 아직 살아 있으면(재기동이 겹쳤다) 곧 바꿔 넣으므로 둔다. 진짜 파일은 잡 규칙 그대로다(끊긴 지
    7일이 안 된 잡의 것은 남는다)."""
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(delib_jobs, "JOB_DIR", jobs)
    for name in ("_JOBS", "_TASKS", "_PENDING"):
        monkeypatch.setattr(delib_jobs, name, {})
    jid = "default-20261031-000000-abc123"
    delib_jobs._persist({"id": jid, "job": "default", "kind": "default", "status": "interrupted", "question": "q", "turns": [],
                         "finished_at": delib_jobs._now(), "updated_at": delib_jobs._now()})
    assert delib_jobs._write_args(jid, {"job_kind": "default", "question": "q", "opts": {}})
    jp = delib_jobs._journal_path(jid)
    delib_jobs._private_dir(jp.parent)
    jp.write_bytes(b'{"k": "j:head"}\n')
    live = subprocess.Popen(["sleep", "60"])
    try:
        made = {}
        for sub in ("args", "journal"):
            for who, pid in (("dead", _NO_PID), ("live", live.pid), ("me", os.getpid())):
                p = jobs / sub / f".{jid}.{pid}.tmp"
                p.write_bytes("카드 본문".encode("utf-8"))
                p.chmod(0o600)
                made[(sub, who)] = p
        m.bind(object())
        left = sorted(k for k, p in made.items() if p.exists())
    finally:
        live.kill()
        live.wait(timeout=10)
    assert left == [("args", "live"), ("journal", "live")], f"남은 임시 파일 {left} — 죽은 프로세스 · 이 프로세스의 것은 치워야 한다"
    assert jp.exists() and delib_jobs._args_path(jid).exists(), "끊긴 지 7일이 안 된 잡의 진짜 파일을 지웠다"


@pytest.mark.parametrize("what", ["journal", "args"])
def test_진짜_프로세스가_고쳐_쓰다_죽으면_다음_기동이_임시_파일을_치운다(monkeypatch, tmp_path, what):
    """진짜 프로세스 — 자식이 일지를 고쳐 쓰다(_rewrite_journal — 제자리 이어하기) · 시작 인자 파일을 쓰다(_write_args) 임시 파일을 다 쓰고 바꿔
    넣기 직전에 KILL 을 맞는다. 임시 파일(0600)이 남고 진짜 파일은 그대로다. 다음 기동(같은 원장 자리의 재기동 정리)이 임시 파일만 치운다. 자식은
    이 시험이 띄운 PID 하나이고 끝나면 내린다."""
    import signal

    jobs = tmp_path / "jobs"
    jid = "default-20261031-000000-fed987"
    child = (
        "import os, sys, time\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import delib_jobs\n"
        "def stuck(*_a, **_k):\n"
        "    print('AT', flush=True)\n"
        "    time.sleep(60)\n"
        "os.replace = stuck\n"
        "if sys.argv[3] == 'journal':\n"
        "    delib_jobs._rewrite_journal(sys.argv[2], [{'t': 1.0, 'k': 'e:pre', 'sk': 'main', 'knowledge': {'a': '카드 본문' * 2000}}])\n"
        "else:\n"
        "    delib_jobs._write_args(sys.argv[2], {'job_kind': 'default', 'question': 'q', 'opts': {'human_note': '사람 의견' * 2000}})\n")
    env = {**os.environ, "DELIB_JOB_DIR": str(jobs), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.Popen([sys.executable, "-c", child, str(ROOT), jid, what], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        ready, _w, _x = select.select([proc.stdout], [], [], 60)
        line = proc.stdout.readline().strip() if ready else ""
        assert line == "AT", f"자식이 바꿔 넣기 자리에 닿지 않았다 — {line!r} {proc.stderr.read()[-2000:] if proc.poll() is not None else ''}"
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    assert proc.returncode == -signal.SIGKILL
    sub = jobs / ("journal" if what == "journal" else "args")
    tmps = list(sub.glob(".*.tmp"))
    assert [p.name for p in tmps] == [f".{jid}.{proc.pid}.tmp"] and stat.S_IMODE(tmps[0].stat().st_mode) == 0o600, \
        "시험 전제 — 바꿔 넣기 전에 죽어 임시 파일이 남았다"
    monkeypatch.setattr(delib_jobs, "JOB_DIR", jobs)
    for name in ("_JOBS", "_TASKS", "_PENDING"):
        monkeypatch.setattr(delib_jobs, name, {})
    delib_jobs._persist({"id": jid, "job": "default", "kind": "default", "status": "interrupted", "question": "q", "turns": [],
                         "finished_at": delib_jobs._now(), "updated_at": delib_jobs._now()})
    m.bind(object())                                       # 다음 기동 — 재기동 정리
    assert not list(sub.glob(".*.tmp")), "죽은 프로세스가 남긴 임시 파일(재개 자료 · 근거 본문)을 치우지 않았다"


def test_남이_이으면_일지를_남기고_주인이_이으면_지운다(eng, monkeypatch):
    _caps(monkeypatch, 2)
    eng.script["가"] = _rounds_script()

    async def scenario():
        a = _start("가", ME)
        await _tick()
        await delib_jobs.shutdown()
        return a
    try:
        a = _play(scenario)
    finally:
        delib_jobs.closing(False)
    jp = delib_jobs._journal_path(a["id"])
    assert jp.exists()

    async def go(ctx):
        out = await m.deliberate_continue(previous_job_id=a["id"], ctx=ctx)
        await _tick()
        return out
    out = _play(lambda: go(_ctx(YOU, groups="portal-admin")))       # 관리자 — 남의 심의를 볼 수 있고 이을 수도 있다(HA-06)
    assert out["resumed"]["foreign"] is True and jp.exists(), "남의 이어하기가 주인의 일지를 지웠다"
    _play(lambda: go(_ctx(ME)))
    assert not jp.exists()


# ── 내려갈 때 접기 ──────────────────────────────────────────────────────────
def test_내려갈_때_도는_잡을_취소하고_원장에_진행을_쓴다(eng, monkeypatch):
    _caps(monkeypatch, 2)
    eng.script["가"] = _rounds_script()

    async def scenario():
        a = _start("가", ME)
        await _tick()
        assert _on_disk(a)["stage"] == "start", "시험 전제 — 도는 동안 원장은 띄울 때 모양이다"
        assert await delib_jobs.shutdown() == 1
        assert a["id"] not in delib_jobs._TASKS, "기다림 안에 접히지 않았다"
        return a
    try:
        a = _play(scenario)
    finally:
        delib_jobs.closing(False)
    rec = _on_disk(a)
    assert (rec["status"], rec["stage"], len(rec["turns"]), rec["total_rounds"]) == ("interrupted", "r3", 5, 4), rec
    assert rec["error"] == delib_jobs.INTERRUPTED_MSG and delib_jobs._args_path(a["id"]).exists()
    assert delib_jobs.resume_info(rec)["rounds_done"] == 2


def test_취소를_늦게_받는_잡도_상한_안에_메모리_상태로_남긴다(eng, monkeypatch):
    """엔진이 취소를 받고도 정리에 오래 걸리면(바깥 호출을 기다린다) 상한에서 메모리 상태 그대로 원장에 쓴다 — 내려가는 길을 붙잡지 않는다."""
    _caps(monkeypatch, 2)
    release = {}

    async def slow_entry(_app, q, groups, opts, user_email, user_pat, _x, **_k):
        for c in _rounds_script():
            yield c
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            release["ev"] = asyncio.Event()
            try:                                # 취소를 받고도 정리가 끝나지 않는다 — 시험이 먼저 깨져도 뒷정리가 매달리지 않게 5초까지만
                await asyncio.wait_for(release["ev"].wait(), 5)
            except asyncio.TimeoutError:
                pass
            raise
    monkeypatch.setattr(d, "run_deliberation", slow_entry)

    async def scenario():
        a = _start("가", ME)
        await _tick()
        t0 = asyncio.get_running_loop().time()
        assert await delib_jobs.shutdown(wait_s=0.05) == 1
        assert asyncio.get_running_loop().time() - t0 < 1.0
        rec = _on_disk(a)
        assert (rec["status"], rec["stage"], len(rec["turns"])) == ("interrupted", "r3", 5), rec
        release["ev"].set()
        await _tick()
        return a
    try:
        _play(scenario)
    finally:
        delib_jobs.closing(False)


def test_다른_루프의_잡은_건드리지_않는다(eng, monkeypatch):
    """시험이 남긴 다른 이벤트 루프의 태스크(닫힌 루프)를 취소하면 그 루프에 일을 싣다 터진다 — 지금 루프의 것만 본다."""
    class _Other:
        def done(self):
            return False

        def get_loop(self):
            return object()

        def cancel(self):
            raise AssertionError("다른 루프의 태스크를 취소했다")
    monkeypatch.setattr(delib_jobs, "_TASKS", {"x": _Other()})
    try:
        assert asyncio.run(delib_jobs.shutdown()) == 0
    finally:
        delib_jobs.closing(False)


# ── 재기동이 겹쳐도 남이 닫은 원장을 덮지 않는다 · 내려가기 시작하면 줄을 닫는다(HA-09 S0 반대 검토 10-31) ──────────────────────────────
# 옛 프로세스가 정상 종료를 기다리는 동안 새 프로세스가 같은 원장 자리에 떠서(종전 start.sh — 리스너만 보고 띄웠다) 재기동 정리가 도는 잡을
# interrupted 로 닫고, 그 위에 이어하기가 resumed_by 를 적었다. 옛 프로세스의 잡은 그 뒤에 끝나며 메모리 사본으로 원장을 덮어 표지를 지웠다 —
# 이어하기가 한 번 더 받아져 같은 잡이 두 번 이어졌다. start.sh 는 이제 옛 프로세스가 끝난 뒤에 띄우지만, 다른 길(손 기동 · 감시 크론)로 겹쳐도
# 끝 쓰기가 남이 닫은 원장을 덮지 않는다.
_OTHER = "default-20261031-000000-이은잡"


def _closed_by_other(job: dict) -> None:
    """다른 프로세스가 쓴 꼴 — 재기동 정리가 끊긴 잡으로 닫고 이어하기가 표지를 적은 원장(같은 자리). 이 프로세스의 _persist 를 거치지 않는다
    (다른 프로세스의 쓰기는 이 프로세스의 기억에 없다) — 원장과 목록 색인을 그 꼴로 바로 쓴다."""
    rec = {**_on_disk(job), "status": "interrupted", "error": delib_jobs.INTERRUPTED_MSG, "resumed_by": _OTHER,
           "finished_at": delib_jobs._now()}
    f = delib_jobs._path(job["id"])
    f.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    delib_jobs._write_list(rec, f.stat())


def test_다른_프로세스가_닫은_원장은_잡이_끝나도_덮지_않는다(eng, monkeypatch):
    _caps(monkeypatch, 2)

    async def scenario():
        a = _start("가", ME)
        await _tick()
        _closed_by_other(a)
        await _end(eng, a)
        assert a["status"] == "done", "시험 전제 — 이 프로세스에서는 잡이 끝까지 갔다"
        return a
    a = _play(scenario)
    rec = _on_disk(a)
    assert (rec["status"], rec.get("resumed_by")) == ("interrupted", _OTHER), f"남이 닫은 원장을 덮었다 — {rec['status']} · {rec.get('resumed_by')}"


def test_내려갈_때도_다른_프로세스가_닫은_원장은_덮지_않는다(eng, monkeypatch):
    """내려가는 길의 두 쓰기 — 상한 안에 접히지 않은 잡을 메모리 상태로 쓰는 것(shutdown)과 그 뒤 잡의 finally."""
    _caps(monkeypatch, 2)
    release = {}

    async def slow_entry(_app, q, groups, opts, user_email, user_pat, _x, **_k):
        for c in _rounds_script():
            yield c
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            release["ev"] = asyncio.Event()
            try:
                await asyncio.wait_for(release["ev"].wait(), 5)
            except asyncio.TimeoutError:
                pass
            raise
    monkeypatch.setattr(d, "run_deliberation", slow_entry)

    async def scenario():
        a = _start("가", ME)
        await _tick()
        _closed_by_other(a)
        assert await delib_jobs.shutdown(wait_s=0.05) == 1
        assert (_on_disk(a)["status"], _on_disk(a).get("resumed_by")) == ("interrupted", _OTHER), "상한 뒤 쓰기가 남의 원장을 덮었다"
        release["ev"].set()
        await _tick()
        return a
    try:
        a = _play(scenario)
    finally:
        delib_jobs.closing(False)
    rec = _on_disk(a)
    assert (rec["status"], rec.get("resumed_by")) == ("interrupted", _OTHER), "잡의 finally 가 남의 원장을 덮었다"


def test_내려가며_먼저_쓴_원장은_제_것이라_잡의_finally_가_마저_쓴다(eng, monkeypatch, caplog):
    """겹치지 않은 보통 재기동 — shutdown 의 늦은 길이 먼저 interrupted 를 쓰고, 그 뒤 잡의 finally 가 마지막 메모리 상태를 쓴다. 원장의 상태만
    보면 그 interrupted 를 남이 닫은 것으로 읽어 마지막 쓰기를 빼고 '다른 프로세스' 경고를 남긴다 — 이 프로세스가 쓴 도장으로 가른다."""
    _caps(monkeypatch, 2)
    release = {}

    async def slow_entry(_app, q, groups, opts, user_email, user_pat, _x, **_k):
        for c in _rounds_script():
            yield c
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            release["ev"] = asyncio.Event()
            try:
                await asyncio.wait_for(release["ev"].wait(), 5)
            except asyncio.TimeoutError:
                pass
            raise
    monkeypatch.setattr(d, "run_deliberation", slow_entry)
    seen = {}

    async def scenario():
        a = _start("가", ME)
        await _tick()
        assert await delib_jobs.shutdown(wait_s=0.05) == 1
        seen["late"] = delib_jobs._path(a["id"]).stat().st_mtime_ns
        await asyncio.sleep(0.01)
        release["ev"].set()
        await _tick()
        return a
    try:
        with caplog.at_level("WARNING", logger=delib_jobs.log.name):
            a = _play(scenario)
    finally:
        delib_jobs.closing(False)
    assert delib_jobs._path(a["id"]).stat().st_mtime_ns != seen["late"], "잡의 finally 가 제 원장을 마저 쓰지 않았다"
    assert _on_disk(a)["status"] == "interrupted"
    assert not [r for r in caplog.records if "다른 프로세스" in r.getMessage()], "제가 쓴 원장을 남의 것으로 읽었다"


def test_종료_신호를_받는_그_자리에서_줄을_닫는다(monkeypatch):
    """uvicorn 은 TERM 을 받으면 리스너를 닫고 열린 연결을 AGENT_GRACEFUL_S 까지 기다린 뒤에야 lifespan 종료(unbind — 줄 닫기)를 돈다. 그 사이 자리가
    나면(도는 잡이 끝나거나 주기 펌프) 내려가는 프로세스가 줄 선 잡을 띄웠고, 그 잡은 곧 interrupted 로 접혀 재기동 뒤 같은 차례로 다시 서지 못했다
    (R2). 기동 때 uvicorn 의 종료 처리기 앞에 줄 닫기를 끼운다 — 다른 처리기는 건드리지 않는다. 신호는 보내지 않고 처리기를 직접 부른다."""
    import signal

    seen = []

    class _Server:                      # uvicorn.Server 의 꼴 — 처리기는 바운드 메서드 handle_exit 이다
        def handle_exit(self, sig, frame):
            seen.append((sig, delib_jobs._CLOSING))

    srv = _Server()
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    monkeypatch.setattr(delib_jobs, "_CLOSING", False)
    try:
        signal.signal(signal.SIGTERM, srv.handle_exit)
        signal.signal(signal.SIGINT, signal.default_int_handler)     # uvicorn 의 것이 아니다 — 끼우지 않는다
        assert delib_jobs.hook_exit_signals() is True
        assert delib_jobs.hook_exit_signals() is False, "두 번 끼웠다"
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert seen == [(signal.SIGTERM, True)], f"줄을 닫기 전에 uvicorn 처리기로 갔거나 넘기지 않았다 — {seen}"
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
        delib_jobs.closing(False)
