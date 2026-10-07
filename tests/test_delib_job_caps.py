# 심의 잡 동시 실행 상한 — 거절할 때 남의 잡을 보여 주지 않고, 어느 상한에 걸렸는지 말한다
#
# 종전 거절 문구는 진행 중인 잡 id 를 **전부** 찍었다. deliberate_cancel·deliberate_result 는 id 만
# 받으므로, 거절당한 사람이 남의 심의를 들여다보고 접을 수 있었다(S26U 피드백 1-9).
# 상한도 전역 하나뿐이라 한 사람이 자리를 다 차지하면 나머지는 기다릴 수밖에 없었다 — 사용자별
# 상한을 따로 둔다(기본은 전역과 같아 종전 동작 그대로다. HWAXPortal docs/delib-engine-feedback D-6).
#
# 지금은 상한에 걸리면 거절하지 않고 줄을 세운다(tests/test_delib_job_queue.py). 여기서는 **줄을 끈 박스**
# (DELIB_JOB_QUEUE_MAX=0)의 거절 문구를 본다 — 그 박스에서는 종전 문구가 그대로 나가야 한다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_job_caps.py -q
import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402

# 메뉴(deliberate_jobs)가 근거 예산을 재느라 모델 컨텍스트를 묻는다 — 고정해 둔다(이름째 가져와 이 파일에도 건다).
from test_delib_silent_drops import _pin_context  # noqa: E402, F401

ME, YOU, THIRD = "me@example.com", "you@example.com", "third@example.com"


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """빈 원장 — 실 원장(/data 쪽)에 시험 잡을 쓰지 않는다. 엔진은 곧바로 끝나는 가짜다."""
    async def _fake_entry(*_a):
        yield d._sse("done", {})

    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    monkeypatch.setattr(delib_jobs, "_PENDING", {})
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


def _caps(monkeypatch, total, per_user=None):
    monkeypatch.setattr(delib_jobs, "MAX_RUNNING", total)
    monkeypatch.setattr(delib_jobs, "MAX_RUNNING_PER_USER", total if per_user is None else per_user)
    monkeypatch.setattr(delib_jobs, "QUEUE_MAX", 0)       # 줄을 끈다 — 상한에 걸리면 거절이다


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
    assert "deliberate_cancel" not in msg, "제 것이 뭔지 모르는 호출자에게 접으라고 했다"


def test_자리가_있으면_시작한다(ledger, monkeypatch):
    _caps(monkeypatch, 2)
    _running(ledger, YOU)
    assert _start(ME)["user"] == ME


# ── 어느 상한인가 ────────────────────────────────────────────────────────────
def test_전역_상한에만_걸리면_전역이라고_말한다(ledger, monkeypatch):
    _caps(monkeypatch, 3, 2)
    _running(ledger, ME, YOU, THIRD)
    msg = _refused(ME)
    assert "전역 3건" in msg and "DELIB_JOB_MAX_RUNNING" in msg, msg
    assert "사용자별" not in msg, msg


def test_사용자별_상한에만_걸리면_사용자별이라고_말한다(ledger, monkeypatch):
    _caps(monkeypatch, 6, 2)
    mine = _running(ledger, ME, ME, YOU)
    msg = _refused(ME)
    assert "사용자별 2건" in msg and "DELIB_JOB_MAX_RUNNING_PER_USER" in msg, msg
    assert "전역" not in msg, msg
    assert all(j in msg for j in mine[:2]) and mine[2] not in msg, msg
    assert "내 진행 중 2건" in msg and "전체 3/6" in msg, msg


def test_둘_다_걸리면_둘_다_말한다(ledger, monkeypatch):
    _caps(monkeypatch, 2, 2)
    _running(ledger, ME, ME)
    msg = _refused(ME)
    assert "전역 2건" in msg and "사용자별 2건" in msg, msg


# ── 사용자별 상한 ────────────────────────────────────────────────────────────
def test_한_사람이_제_몫을_다_써도_다른_사람은_시작한다(ledger, monkeypatch):
    _caps(monkeypatch, 6, 2)
    _running(ledger, ME, ME)
    _refused(ME)
    assert _start(YOU)["user"] == YOU


def test_신원_없는_호출은_전역_상한만_받는다(ledger, monkeypatch):
    """신원 헤더 없이 온 호출(서비스 계정)을 빈 이름으로 묶으면 서로 다른 호출자가 한 사람이 된다."""
    _caps(monkeypatch, 4, 1)
    _running(ledger, "", "", "")
    assert _start("")["user"] == "", "신원 없는 호출을 사용자별 상한(1)으로 막았다"
    anon = _running(ledger, "", "", "", "")              # 4/4 — 전역
    msg = _refused("")
    assert "전역 4건" in msg and "사용자별" not in msg, msg
    assert not any(j in msg for j in anon), f"신원 없는 호출에 남의 잡 id 를 보여 줬다 — {msg}"


def test_신원_없는_잡은_누구의_것으로도_세지_않는다(ledger, monkeypatch):
    _caps(monkeypatch, 6, 1)
    _running(ledger, "", "")
    assert _start(ME)["user"] == ME


def test_끝난_잡은_세지_않는다(ledger, monkeypatch):
    _caps(monkeypatch, 2, 1)
    (jid,) = _running(ledger, ME)
    ledger[jid]["status"] = "done"
    assert _start(ME)["user"] == ME


# ── 기본값 — 종전 동작 그대로 ────────────────────────────────────────────────────
def _loaded(**env):
    code = "import delib_jobs as j; print(j.MAX_RUNNING, j.MAX_RUNNING_PER_USER)"
    clean = {k: v for k, v in os.environ.items() if not k.startswith("DELIB_JOB_MAX_RUNNING")}
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                       env={**clean, **env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert r.returncode == 0, r.stderr[-600:]
    return tuple(int(x) for x in r.stdout.split())


def test_사용자별_기본값은_전역_상한이다():
    """전역 2 는 LLM 큐 보호선이고 용량은 여기서 잴 수 없다 — 손잡이를 만들되 기본 동작은 안 바꾼다."""
    assert _loaded() == (2, 2)
    assert _loaded(DELIB_JOB_MAX_RUNNING="6") == (6, 6)
    assert _loaded(DELIB_JOB_MAX_RUNNING="6", DELIB_JOB_MAX_RUNNING_PER_USER="2") == (6, 2)
    assert _loaded(DELIB_JOB_MAX_RUNNING="6", DELIB_JOB_MAX_RUNNING_PER_USER="") == (6, 6)
    assert _loaded(DELIB_JOB_MAX_RUNNING="6", DELIB_JOB_MAX_RUNNING_PER_USER="0") == (6, 6)


def test_전역_상한_0_은_전부_막는_것이_아니라_기본값이다():
    """`int("0" or 2)` 는 0 이다 — 0 을 넣으면 모든 시작이 '상한 0건' 으로 거절됐다. 대기 큐가 있으면
    거절도 아니고 영영 대기다. 빈 값과 같은 뜻(기본 2)으로 읽는다 — 사용자별 상한의 0 과 같은 규칙이다."""
    assert _loaded(DELIB_JOB_MAX_RUNNING="0") == (2, 2)
    assert _loaded(DELIB_JOB_MAX_RUNNING="-3") == (2, 2)
    assert _loaded(DELIB_JOB_MAX_RUNNING="0", DELIB_JOB_MAX_RUNNING_PER_USER="1") == (2, 1)
    assert _loaded(DELIB_JOB_MAX_RUNNING="1") == (1, 1)


def test_메뉴와_목록이_두_상한을_다_알려_준다(ledger, monkeypatch):
    """전역만 적으면 사용자별 상한이 더 낮을 때 그만큼 돌릴 수 있다고 읽힌다."""
    import mcp_server

    _caps(monkeypatch, 6, 2)
    for out in (asyncio.run(mcp_server.deliberate_jobs()), asyncio.run(mcp_server.deliberate_list())):
        assert (out["running_max"], out["running_max_per_user"]) == (6, 2), out


@pytest.mark.parametrize("owners,ok", [((), True), ((YOU,), True), ((ME,), True),
                                        ((ME, YOU), False), ((YOU, THIRD), False), ((ME, ME), False)])
def test_기본값에서는_전체가_찼을_때만_거절한다(ledger, monkeypatch, owners, ok):
    _caps(monkeypatch, 2, 2)
    _running(ledger, *owners)
    if ok:
        assert _start(ME)["user"] == ME
    else:
        _refused(ME)


# ── 오타 값 — 심의 MCP 를 통째로 떼지 않는다 ─────────────────────────────────────
# 네 손잡이를 맨 int() 로 읽어서, 값 하나가 숫자가 아니면 delib_jobs 가 import 에서 죽었다. app.py 는
# `import mcp_server` 실패를 경고 한 줄로 넘기고 /mcp 없이 뜬다 — 서버는 살아 있고 /health 는 초록인데
# 게이트웨이에서 deliberate_* 가 전부 사라진다. env 키트의 줄을 설명째 옮겨 적으면 바로 그 모양이 된다
# (start.sh 는 줄 끝 설명을 떼지 않는다).
_KNOBS = {"DELIB_JOB_MAX_RUNNING": ("MAX_RUNNING", 2), "DELIB_JOB_MAX_RUNNING_PER_USER": ("MAX_RUNNING_PER_USER", 2),
          "DELIB_JOB_QUEUE_MAX": ("QUEUE_MAX", 20), "DELIB_JOB_TURN_MAX": ("TURN_MAX", 400)}
_BAD = ["abc", "2.5", "20             # 줄의 길이"]


def _import(code, tmp_path, **env):
    """다른 프로세스에서 모듈을 새로 읽는다 — 원장 경로는 임시 디렉터리로 못박는다(실 원장에 닿지 않게)."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("DELIB_JOB_")}
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                          env={**clean, **env, "DELIB_JOB_DIR": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"})


@pytest.mark.parametrize("knob", list(_KNOBS))
@pytest.mark.parametrize("bad", _BAD)
def test_숫자가_아닌_값은_기본값으로_읽고_어느_설정인지_남긴다(knob, bad, tmp_path):
    attr, default = _KNOBS[knob]
    r = _import(f"import delib_jobs as j; print(j.{attr})", tmp_path, **{knob: bad})
    assert r.returncode == 0, f"{knob}={bad!r} 가 모듈 로드를 죽였다 — {r.stderr[-300:]}"
    assert int(r.stdout.split()[-1]) == default, r.stdout
    assert knob in r.stderr, f"어느 설정이 틀렸는지 남기지 않았다 — {r.stderr[-300:]}"


def test_숫자가_아닌_값이_있어도_심의_MCP_가_붙은_채로_뜬다(tmp_path):
    r = _import("import app; print('mcp', app._DELIB_MCP is not None, "
                "any(getattr(x, 'path', '') == '/mcp' for x in app.app.routes))", tmp_path,
                DELIB_JOB_QUEUE_MAX=_BAD[2])
    assert r.returncode == 0, r.stderr[-600:]
    assert r.stdout.split()[-3:] == ["mcp", "True", "True"], (r.stdout[-200:], r.stderr[-300:])


def test_멀쩡한_값은_종전대로_읽는다(tmp_path):
    r = _import("import delib_jobs as j; print(j.MAX_RUNNING, j.MAX_RUNNING_PER_USER, j.QUEUE_MAX, j.TURN_MAX)",
                tmp_path, DELIB_JOB_MAX_RUNNING="6", DELIB_JOB_MAX_RUNNING_PER_USER="2",
                DELIB_JOB_QUEUE_MAX="-4", DELIB_JOB_TURN_MAX=" 50 ")
    assert r.returncode == 0, r.stderr[-300:]
    assert r.stdout.split() == ["6", "2", "0", "50"], r.stdout        # 줄 길이의 음수는 0(거절) — 종전 그대로
