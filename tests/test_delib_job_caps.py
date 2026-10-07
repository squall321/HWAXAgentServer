# 심의 잡 동시 실행 상한 — 거절할 때 남의 잡을 보여 주지 않는다
#
# 종전 거절 문구는 진행 중인 잡 id 를 **전부** 찍었다. deliberate_cancel·deliberate_result 는 id 만
# 받으므로, 거절당한 사람이 남의 심의를 들여다보고 접을 수 있었다(S26U 피드백 1-9).
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_job_caps.py -q
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402

ME, YOU, THIRD = "me@example.com", "you@example.com", "third@example.com"


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """빈 원장 — 실 원장(/data 쪽)에 시험 잡을 쓰지 않는다. 엔진은 곧바로 끝나는 가짜다."""
    async def _fake_entry(*_a):
        yield d._sse("done", {})

    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    monkeypatch.setattr(d, "run_deliberation", _fake_entry)
    return delib_jobs._JOBS


def _running(ledger, *owners):
    """owners 순서대로 진행 중인 잡을 깔고 id 목록을 돌려준다."""
    ids = []
    for i, owner in enumerate(owners):
        jid = f"default-20261007-0000{i:02d}-{'abcdef'[i % 6] * 6}"
        ledger[jid] = {"id": jid, "status": "running", "user": owner, "question": "q"}
        ids.append(jid)
    return ids


def _start(user=""):
    async def go():
        job = delib_jobs.start(object(), "default", "화두", user_email=user)
        await delib_jobs._TASKS[job["id"]]
        return job

    return asyncio.run(go())


def _refused(user=""):
    with pytest.raises(RuntimeError) as e:
        _start(user)
    return str(e.value)


def _caps(monkeypatch, total):
    monkeypatch.setattr(delib_jobs, "MAX_RUNNING", total)


# ── 누출 ─────────────────────────────────────────────────────────────────────
def test_거절_문구에_남의_잡_id_가_없다(ledger, monkeypatch):
    _caps(monkeypatch, 2)
    theirs = _running(ledger, YOU, THIRD)
    msg = _refused(ME)
    for jid in theirs:
        assert jid not in msg, f"남의 잡 id 가 거절 문구에 실렸다 — {msg}"
    assert "내 진행 중 0건" in msg and "전체 2/2" in msg, msg


def test_내_잡_id_는_보여_준다(ledger, monkeypatch):
    """접을 수 있는 것은 제 것뿐이다 — 그 id 는 알려 줘야 deliberate_cancel 을 쓸 수 있다."""
    _caps(monkeypatch, 2)
    mine, theirs = _running(ledger, ME, YOU)
    msg = _refused(ME)
    assert mine in msg and theirs not in msg, msg
    assert "내 진행 중 1건" in msg and "전체 2/2" in msg, msg
    assert "deliberate_cancel" in msg


def test_내_것이_없으면_취소를_권하지_않는다(ledger, monkeypatch):
    """종전 문구는 누구에게나 '하나를 접어라' 고 했다 — 접을 수 있는 것이 남의 것뿐일 때도."""
    _caps(monkeypatch, 2)
    _running(ledger, YOU, THIRD)
    assert "deliberate_cancel" not in _refused(ME)


def test_이메일_대소문자가_달라도_같은_사람이다(ledger, monkeypatch):
    _caps(monkeypatch, 2)
    mine, _ = _running(ledger, "Me@Example.com", YOU)
    assert mine in _refused(ME)


def test_신원_없는_호출에도_남의_잡_id_를_보여_주지_않는다(ledger, monkeypatch):
    """신원 헤더 없이 온 호출끼리도 서로 남이다 — 빈 이름이 같다고 한 사람으로 묶지 않는다."""
    _caps(monkeypatch, 2)
    anon = _running(ledger, "", "")
    msg = _refused("")
    assert not any(j in msg for j in anon), msg
    assert "전체 2/2" in msg and "신원 없는 호출" in msg, msg


def test_자리가_있으면_시작한다(ledger, monkeypatch):
    _caps(monkeypatch, 2)
    _running(ledger, YOU)
    assert _start(ME)["user"] == ME
