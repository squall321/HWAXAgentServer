# 심의 잡 대기열 — 동시 실행 상한에 걸리면 거절하지 않고 줄을 세우고, 자리가 나면 스스로 시작한다
#
# 종전엔 자리가 없으면 거절했다. 실사용 팀은 패널 14개를 돌리는데, 누가 2건을 돌리고 있으면 시작조차
# 못 하고 사람이 빈 자리를 지켜보다 다시 불러야 했다(S26U 피드백 1-9 ③ — HWAXPortal
# docs/delib-engine-feedback D-12). 이제 줄을 선다(DELIB_JOB_QUEUE_MAX, 기본 20 · 0 이면 종전처럼 거절).
#
# **실제 asyncio 태스크를 돌려서** 본다 — 가짜는 엔진뿐이다(문이 열릴 때까지 '도는 중' 으로 남는 생성기).
# 자리 넘겨주기는 태스크가 끝나는 그 순간의 일이라, 상태 값만 바꿔 보는 시험으로는 '한 자리에 둘이 떴다'·
# '자리가 비었는데 줄이 그대로다' 를 못 잡는다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_job_queue.py -q
import asyncio
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

# 메뉴(deliberate_jobs)가 근거 예산을 재느라 모델 컨텍스트를 묻는다 — 고정해 둔다(이름째 가져와 이 파일에도 건다).
from test_delib_silent_drops import _pin_context  # noqa: E402, F401

ME, YOU, THIRD = "me@example.com", "you@example.com", "third@example.com"
_TOKEN = "pat-EXAMPLE-0000"                 # 호출자 토큰 자리에 넣는 지어낸 값 — 파일에 남으면 안 된다


class _Engine:
    """가짜 진입 함수 — 화두마다 문이 하나 있고, 문이 열릴 때까지 그 심의는 '도는 중' 이다."""

    def __init__(self):
        self.started, self.seen, self.fail = [], {}, set()   # 시작한 순서 · 화두별 받은 인자 · 터뜨릴 화두
        self.now = self.peak = 0                              # 지금·최대 동시에 도는 수
        self._gates = {}

    def gate(self, q):
        return self._gates.setdefault(q, asyncio.Event())

    async def entry(self, _app, q, groups, opts, user_email, user_pat, _x):
        self.started.append(q)
        self.seen[q] = SimpleNamespace(groups=groups, opts=opts, user=user_email, pat=user_pat)
        self.now += 1
        self.peak = max(self.peak, self.now)
        try:
            yield d._sse("status", {"step": f"{q} 시작"})
            await self.gate(q).wait()
            if q in self.fail:
                raise RuntimeError("엔진이 터졌다")
            yield d._sse("delib", {"kind": "decision", "text": f"{q} 결정문"})
            yield d._sse("done", {})
        finally:
            self.now -= 1


@pytest.fixture
def eng(monkeypatch, tmp_path):
    """빈 원장과 빈 줄 — 실 원장(/data 쪽)에 시험 잡을 쓰지 않는다."""
    e = _Engine()
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    monkeypatch.setattr(delib_jobs, "_PENDING", {})
    monkeypatch.setattr(d, "run_deliberation", e.entry)
    monkeypatch.setattr(m, "_APP", object())
    return e


def _caps(monkeypatch, total, per_user=None, queue=20):
    monkeypatch.setattr(delib_jobs, "MAX_RUNNING", total)
    monkeypatch.setattr(delib_jobs, "MAX_RUNNING_PER_USER", total if per_user is None else per_user)
    monkeypatch.setattr(delib_jobs, "QUEUE_MAX", queue)


def _play(scenario):
    """시나리오를 한 이벤트 루프에서 돌린다. 끝나면 남은 잡을 전부 접고 **끝날 때까지 기다린다** —
    루프가 닫힌 뒤에 태스크가 남으면, 원장 경로가 실 경로로 되돌아간 뒤에 그 잡이 기록을 쓴다."""
    async def go():
        try:
            return await scenario()
        finally:
            for jid in list(delib_jobs._PENDING):
                delib_jobs.cancel(jid)
            tasks = list(delib_jobs._TASKS.values())
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.wait(tasks)
            await _tick()

    return asyncio.run(go())


async def _tick(n=6):
    """이벤트 루프를 몇 바퀴 돌린다 — 막 띄운 태스크가 첫 걸음을 떼고, 끝난 태스크의 뒷정리가 돈다."""
    for _ in range(n):
        await asyncio.sleep(0)


def _start(q, user="", **kw):
    return delib_jobs.start(object(), "default", q, user_email=user, **kw)


async def _end(eng, job, *, fail=False):
    """도는 심의 하나를 끝낸다(fail 이면 엔진 예외로). 태스크가 끝나고 뒷정리가 돌 때까지 기다린다."""
    task = delib_jobs._TASKS[job["id"]]
    if fail:
        eng.fail.add(job["question"])
    eng.gate(job["question"]).set()
    await asyncio.wait([task])
    await _tick()


def _st(job):
    return job["status"]


def _on_disk(job):
    return json.loads((delib_jobs.JOB_DIR / f"{job['id']}.json").read_text(encoding="utf-8"))


# ── 줄을 서고, 자리가 나면 스스로 시작한다 ───────────────────────────────────────────
def test_상한을_넘겨_시작하면_줄을_서고_자리가_나면_스스로_시작한다(eng, monkeypatch):
    _caps(monkeypatch, 1)

    async def scenario():
        a, b = _start("가"), _start("나")
        await _tick()
        assert (_st(a), _st(b)) == ("running", "queued")
        assert eng.started == ["가"], "줄 선 심의가 엔진에 들어갔다"
        assert b["id"] not in delib_jobs._TASKS and _on_disk(b)["status"] == "queued"
        assert delib_jobs.summary(b)["queue"]["position"] == 1
        await _end(eng, a)
        assert (_st(a), _st(b)) == ("done", "running"), "자리가 났는데 줄 선 심의가 시작하지 않았다"
        assert eng.started == ["가", "나"] and _on_disk(b)["status"] == "running"
        assert "queue" not in delib_jobs.summary(b), "시작한 심의에 순번이 남아 있다"
        await _end(eng, b)
        assert _st(b) == "done" and b["decision"] == "나 결정문"
        assert delib_jobs.running_count() == 0 and not delib_jobs._PENDING

    _play(scenario)


def test_자리가_있으면_종전대로_곧바로_시작한다(eng, monkeypatch):
    _caps(monkeypatch, 2)

    async def scenario():
        a = _start("가", ME)
        assert _st(a) == "running" and a["id"] in delib_jobs._TASKS
        assert a["started_at"] and "queued_at" not in a
        out = delib_jobs.summary(a)
        assert "queue" not in out and "waited_s" not in out
        await _tick()
        await _end(eng, a)

    _play(scenario)


def test_먼저_선_순서대로_시작한다(eng, monkeypatch):
    _caps(monkeypatch, 1)

    async def scenario():
        jobs = [_start(q) for q in "가나다라"]
        await _tick()
        assert [delib_jobs.summary(j).get("queue", {}).get("position") for j in jobs] == [None, 1, 2, 3]
        assert [delib_jobs.summary(j).get("queue", {}).get("ahead") for j in jobs] == [None, 0, 1, 2]
        await _end(eng, jobs[0])
        assert [delib_jobs.summary(j).get("queue", {}).get("position") for j in jobs] == [None, None, 1, 2]
        for j in jobs[1:]:
            await _end(eng, j)
        assert eng.started == list("가나다라") and all(_st(j) == "done" for j in jobs)

    _play(scenario)


def test_자리_하나에_하나만_뜨고_빈_자리를_남기지_않는다(eng, monkeypatch):
    """끝날 때마다 정확히 하나가 뜬다 — 둘이 뜨면 상한이 깨지고, 안 뜨면 줄이 영영 그대로다."""
    _caps(monkeypatch, 2)

    async def scenario():
        jobs = [_start(f"화두{i}") for i in range(7)]
        await _tick()
        for done in range(7):
            left = 7 - done
            assert delib_jobs.running_count() == min(2, left), f"{done}건 끝난 뒤 도는 수가 어긋났다"
            assert len(delib_jobs._PENDING) == max(0, left - 2)
            await _end(eng, next(j for j in jobs if _st(j) == "running"))
        assert eng.peak == 2 and len(eng.started) == 7 == len(set(eng.started))
        assert all(_st(j) == "done" for j in jobs)

    _play(scenario)


def test_줄을_선_동안의_시간은_걸린_시간에_세지_않고_따로_적는다(eng, monkeypatch):
    _caps(monkeypatch, 1)

    async def scenario():
        a, b = _start("가"), _start("나")
        await _tick()
        b["queued_at"] -= 90                      # 90초 전에 줄을 섰다
        out = delib_jobs.summary(b)
        assert out["elapsed_s"] == 0 and 89 < out["queue"]["waited_s"] < 92, out
        await _end(eng, a)
        out = delib_jobs.summary(b)
        assert out["elapsed_s"] < 5 and 89 < out["waited_s"] < 92, out

    _play(scenario)


# ── 사용자별 상한에 걸린 잡은 건너뛴다 ───────────────────────────────────────────────
def test_제_몫을_다_쓴_사람의_잡은_건너뛰고_다음_사람이_먼저_시작한다(eng, monkeypatch):
    """줄 맨 앞이 사용자별 상한에 묶여 있다고 뒤 사람까지 못 가면, 한 사람이 전원을 세운다."""
    _caps(monkeypatch, 2, 1)

    async def scenario():
        me1, you1 = _start("내 첫째", ME), _start("남 첫째", YOU)
        me2, you2, third = _start("내 둘째", ME), _start("남 둘째", YOU), _start("셋째 사람", THIRD)
        await _tick()
        assert [_st(j) for j in (me1, you1, me2, you2, third)] == [
            "running", "running", "queued", "queued", "queued"]
        await _end(eng, you1)                     # 자리 하나 — 맨 앞(내 둘째)은 내 첫째가 돌고 있어 못 뜬다
        assert (_st(me2), _st(you2), _st(third)) == ("queued", "running", "queued"), (
            "맨 앞 잡이 사용자별 상한에 묶였다고 자리를 비워 두었거나, 줄 순서를 건너 셋째를 띄웠다")
        await _end(eng, me1)                      # 이제 내 것이 없다 — 맨 앞인 내 둘째가 뜬다
        assert (_st(me2), _st(third)) == ("running", "queued")
        await _end(eng, you2)
        assert _st(third) == "running"
        assert eng.started == ["내 첫째", "남 첫째", "남 둘째", "내 둘째", "셋째 사람"]

    _play(scenario)


def test_전역_자리가_남아도_제_몫을_넘으면_줄을_서고_남은_곧바로_시작한다(eng, monkeypatch):
    _caps(monkeypatch, 4, 1)

    async def scenario():
        me1, me2 = _start("내 첫째", ME), _start("내 둘째", ME)
        assert (_st(me1), _st(me2)) == ("running", "queued")
        assert "사용자별" in delib_jobs.summary(me2)["queue"]["why"]
        you1 = _start("남 첫째", YOU)             # 줄에 내 둘째가 서 있어도 남은 기다리지 않는다
        assert _st(you1) == "running"
        await _tick()
        await _end(eng, me1)
        assert _st(me2) == "running"

    _play(scenario)


def test_신원_없는_호출은_사용자별_상한에_묶이지_않는다(eng, monkeypatch):
    _caps(monkeypatch, 3, 1)

    async def scenario():
        jobs = [_start(f"익명{i}") for i in range(4)]
        assert [_st(j) for j in jobs] == ["running", "running", "running", "queued"]
        assert "전역" in delib_jobs.summary(jobs[3])["queue"]["why"]

    _play(scenario)


# ── 접기 ─────────────────────────────────────────────────────────────────────
def test_줄_선_심의를_접으면_시작하지_않고_뒤가_당겨진다(eng, monkeypatch):
    _caps(monkeypatch, 1)

    async def scenario():
        a, b, c = _start("가"), _start("나"), _start("다")
        await _tick()
        out = delib_jobs.cancel(b["id"])
        assert out["status"] == "cancelled" and _st(b) == "cancelled" and _on_disk(b)["status"] == "cancelled"
        assert b["finished_at"] and delib_jobs.summary(b)["elapsed_s"] == 0
        assert delib_jobs.summary(c)["queue"]["position"] == 1
        assert "이미 끝난" in delib_jobs.cancel(b["id"])["note"]
        await _end(eng, a)
        assert (_st(b), _st(c)) == ("cancelled", "running") and eng.started == ["가", "다"]

    _play(scenario)


def test_도는_심의를_접으면_줄_선_심의가_시작한다(eng, monkeypatch):
    _caps(monkeypatch, 1)

    async def scenario():
        a, b = _start("가"), _start("나")
        await _tick()
        assert delib_jobs.cancel(a["id"])["status"] == "cancelling"
        await asyncio.wait([delib_jobs._TASKS[a["id"]]])
        await _tick()
        assert (_st(a), _st(b)) == ("cancelled", "running")

    _play(scenario)


def test_엔진이_터져도_자리가_돌아온다(eng, monkeypatch):
    _caps(monkeypatch, 1)

    async def scenario():
        a, b = _start("가"), _start("나")
        await _tick()
        await _end(eng, a, fail=True)
        assert _st(a) == "error" and "엔진이 터졌다" in a["error"]
        assert _st(b) == "running"

    _play(scenario)


def test_첫_걸음도_떼기_전에_접힌_심의도_자리를_돌려준다(eng, monkeypatch):
    """막 띄운 태스크가 첫 걸음을 떼기 전에 취소되면 코루틴 본문에 들어가 보지도 못한다 — 뒷정리
    (finally)가 돌지 않아 잡은 영영 running 이고 자리는 안 돌아온다. 줄을 선 잡은 job_id 가 이미
    호출자 손에 있어, 띄운 바로 그 틈에 취소가 들어올 수 있다."""
    _caps(monkeypatch, 1)

    async def scenario():
        a = _start("가")
        delib_jobs.cancel(a["id"])                # 같은 걸음에서 — 태스크는 아직 한 발도 못 뗐다
        b = _start("나")
        assert _st(b) == "queued"
        await _tick()
        assert _st(a) == "cancelled" and a["finished_at"], "본문에 못 들어간 태스크가 running 으로 남았다"
        assert a["id"] not in delib_jobs._TASKS
        assert _st(b) == "running" and eng.started == ["나"]
        assert _on_disk(a)["status"] == "cancelled"

    _play(scenario)


# ── 줄이 찼을 때 ─────────────────────────────────────────────────────────────────
def _refused(q, user=""):
    with pytest.raises(RuntimeError) as e:
        _start(q, user)
    return str(e.value)


def test_줄까지_차면_그렇다고_말하고_거절한다(eng, monkeypatch):
    _caps(monkeypatch, 1, queue=2)

    async def scenario():
        a, b, c = _start("가", YOU), _start("나", ME), _start("다", THIRD)
        await _tick()
        msg = _refused("라", ME)
        assert "대기열" in msg and "2/2" in msg and "DELIB_JOB_QUEUE_MAX" in msg, msg
        assert "전역 1건" in msg, msg
        assert b["id"] in msg and "내 대기 1건" in msg and "내 진행 중 0건" in msg, msg
        assert "deliberate_cancel" in msg
        assert len(delib_jobs._JOBS) == 3 and len(list(delib_jobs.JOB_DIR.iterdir())) == 3, "거절했는데 잡이 생겼다"
        delib_jobs.cancel(b["id"])                # 내 것을 접으면 줄에 자리가 난다
        d2 = _start("라", ME)
        assert _st(d2) == "queued" and delib_jobs.summary(d2)["queue"]["position"] == 2
        return a, c

    _play(scenario)


def test_줄이_찼어도_자리가_있는_사람은_시작한다(eng, monkeypatch):
    """줄이 찬 것은 기다려야 하는 사람의 사정이다 — 전역 자리가 남고 제 몫도 남은 사람까지 막지 않는다."""
    _caps(monkeypatch, 3, 1, queue=1)

    async def scenario():
        _start("내 첫째", ME)
        assert _st(_start("내 둘째", ME)) == "queued"
        assert "대기열" in _refused("내 셋째", ME)
        assert _st(_start("남 첫째", YOU)) == "running"

    _play(scenario)


def test_0_이면_종전처럼_거절하고_아무것도_남기지_않는다(eng, monkeypatch):
    _caps(monkeypatch, 1, queue=0)

    async def scenario():
        a = _start("가", ME)
        msg = _refused("나", ME)
        assert msg.startswith("동시 실행 상한에 걸렸다(전역 1건 — DELIB_JOB_MAX_RUNNING"), msg
        assert a["id"] in msg and "대기열" not in msg, msg
        assert list(delib_jobs._JOBS) == [a["id"]] and not delib_jobs._PENDING

    _play(scenario)


# ── 남의 것을 보여 주지 않는다 ───────────────────────────────────────────────────────
def _ctx(user):
    """게이트웨이가 신원 헤더를 실어 보낸 호출 — mcp_server._caller 가 읽는 모양."""
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(headers={"x-hwax-user": user})))


def test_순번_안내_어디에도_남의_잡_id_와_화두가_없다(eng, monkeypatch):
    """줄을 선 사람에게는 '몇 번째인가' 만 필요하다. 앞에 선 잡의 id 를 주면 그걸로 남의 심의를
    들여다보고 접을 수 있다 — 접으면 제 순번이 당겨지니 그럴 까닭도 생긴다."""
    _caps(monkeypatch, 1, queue=3)

    async def scenario():
        theirs = [_start("남의 화두 — 힌지 크랙", YOU), _start("남의 화두 — 배터리 스웰링", THIRD)]
        mine = await m.deliberate_start("내 화두", ctx=_ctx(ME))
        assert mine["status"] == "queued" and mine["queue"]["position"] == 2 and mine["queue"]["ahead"] == 1
        await m.deliberate_start("내 화두 둘", ctx=_ctx(ME))
        views = {
            "시작 응답": mine,
            "진행 조회": await m.deliberate_status(mine["job_id"]),
            "결과 회수": await m.deliberate_result(mine["job_id"]),
            "메뉴": await m.deliberate_jobs(ctx=_ctx(ME)),
            "줄이 찬 거절": _refused("내 화두 셋", ME),
            "접기": await m.deliberate_cancel(mine["job_id"]),
        }
        for name, view in views.items():
            text = view if isinstance(view, str) else json.dumps(view, ensure_ascii=False)
            for job in theirs:
                assert job["id"] not in text, f"{name} 에 남의 잡 id 가 실렸다"
                assert job["question"] not in text, f"{name} 에 남의 화두가 실렸다"
            for other in (YOU, THIRD):
                assert other not in text, f"{name} 에 남의 계정이 실렸다"
        assert mine["job_id"] in views["줄이 찬 거절"], "제 잡 id 는 보여 줘야 접을 수 있다"

    _play(scenario)


# ── 재기동 ───────────────────────────────────────────────────────────────────
def test_줄_선_잡은_재기동을_못_넘기고_다시_시작하라고_남는다(eng, monkeypatch):
    """띄울 때 쓸 인자(호출자 토큰·근거 본문)는 메모리에만 있다 — 재기동 뒤에는 띄울 수가 없다.
    queued 로 남겨 두면 호출자는 영영 차례를 기다린다."""
    _caps(monkeypatch, 1)

    async def scenario():
        a, b = _start("가", YOU), _start("나", ME)
        await _tick()
        return a, b

    a, b = _play(scenario)                        # 여기까지가 죽기 전 — 아래는 파일만 남은 새 프로세스
    for job, status in ((a, "running"), (b, "queued")):
        job["status"] = status
        delib_jobs._persist(job)
    for store in (delib_jobs._JOBS, delib_jobs._TASKS, delib_jobs._PENDING):
        store.clear()
    m.bind(object())                              # 기동 때 도는 정리
    for job, was in ((a, "도는 중"), (b, "대기 중")):
        rec = asyncio.run(m.deliberate_status(job["id"]))
        assert rec["status"] == "interrupted", f"{was}이던 잡이 {rec['status']} 로 남았다"
        assert "재기동" in rec["error"] and "다시 시작" in rec["error"], rec["error"]
        assert "queue" not in rec
    assert "시작하지 못했다" in asyncio.run(m.deliberate_status(b["id"]))["error"]
    listed = asyncio.run(m.deliberate_list())
    assert (listed["running"], listed["queued"]) == (0, 0), listed
    assert delib_jobs.reap_orphans() == 0, "정리한 잡을 또 정리했다"


def test_호출자_토큰과_근거_본문은_파일에_남지_않고_띄울_때_그대로_간다(eng, monkeypatch):
    _caps(monkeypatch, 1)
    opts = {"evidence": [{"source": "E1", "result": "근거 본문 — 스프링백 0.42mm"}], "rounds": 2}

    async def scenario():
        a = _start("가")
        b = _start("나", ME, user_pat=_TOKEN, groups=["feat:delib"], delib_opts=opts)
        await _tick()
        raw = (delib_jobs.JOB_DIR / f"{b['id']}.json").read_text(encoding="utf-8")
        assert _on_disk(b)["status"] == "queued" and _on_disk(b)["opts"]["evidence"] == 1
        for secret in (_TOKEN, "스프링백 0.42mm"):
            assert secret not in raw, f"줄 선 잡의 파일에 '{secret}' 이 적혔다"
            assert secret not in json.dumps(b, ensure_ascii=False), "잡 기록(메모리)에 실렸다"
        await _end(eng, a)
        got = eng.seen["나"]
        assert (got.user, got.pat, got.groups) == (ME, _TOKEN, ["feat:delib"])
        assert got.opts["evidence"] == opts["evidence"] and got.opts["rounds"] == 2

    _play(scenario)


def test_줄_섰다_뜬_심의는_제_요청의_컨텍스트에서_돈다(eng, monkeypatch):
    """태스크는 만든 자리의 컨텍스트 변수를 물려받는다. 줄 선 잡은 **앞 심의가 끝나는 자리**에서 뜨므로,
    그대로 만들면 앞 심의가 세운 요청 단위 표식(자격 강등·유령 ID 출처 — 엔진의 ContextVar 들)을 달고 돈다."""
    import contextvars

    who = contextvars.ContextVar("who", default="없음")
    seen = {}
    real_entry = eng.entry

    async def _entry(app, q, *rest):
        seen[q] = who.get()
        who.set(f"{q} 가 돌면서 세운 값")           # 엔진이 도는 동안 세우는 요청 단위 표식
        async for chunk in real_entry(app, q, *rest):
            yield chunk

    monkeypatch.setattr(d, "run_deliberation", _entry)
    _caps(monkeypatch, 1)

    async def _request(q):                          # 요청마다 태스크가 다르다 — 컨텍스트도 다르다
        who.set(f"{q} 요청")
        return _start(q)

    async def scenario():
        a = await asyncio.create_task(_request("가"))
        b = await asyncio.create_task(_request("나"))
        await _tick()
        assert (_st(a), _st(b)) == ("running", "queued")
        await _end(eng, a)                          # 나 는 가 의 뒷정리 안에서 뜬다
        assert _st(b) == "running"
        assert seen == {"가": "가 요청", "나": "나 요청"}, f"줄 섰던 심의가 남의 컨텍스트에서 돌았다 — {seen}"

    _play(scenario)


# ── 원장 ─────────────────────────────────────────────────────────────────────
def test_메모리_원장을_비울_때_줄_선_잡은_버리지_않는다(eng, monkeypatch):
    """끝난 시각이 없는 잡이 '가장 오래 전에 끝난 잡' 으로 정렬돼 맨 먼저 버려진다 — 그러면 그 잡은
    줄에는 있는데 원장에는 없다."""
    _caps(monkeypatch, 1)
    monkeypatch.setattr(delib_jobs, "KEEP_IN_MEM", 2)

    async def scenario():
        a, b, c = _start("가"), _start("나"), _start("다")
        await _tick()
        for i in range(4):
            delib_jobs._JOBS[f"old-{i}"] = {"id": f"old-{i}", "status": "done", "finished_at": 10.0 + i}
        await _end(eng, a)                        # 끝나면서 원장을 비운다
        assert b["id"] in delib_jobs._JOBS and c["id"] in delib_jobs._JOBS
        assert (_st(b), _st(c)) == ("running", "queued")
        await _end(eng, b)
        assert _st(c) == "running"

    _play(scenario)


def test_목록은_줄_선_잡을_선_시각으로_정렬하고_수를_센다(eng, monkeypatch):
    _caps(monkeypatch, 1)

    async def scenario():
        a, b, c = _start("가", ME), _start("나", ME), _start("다", ME)
        await _tick()
        a["started_at"], b["queued_at"], c["queued_at"] = 100.0, 200.0, 300.0
        out = await m.deliberate_list()
        assert [r["question"] for r in out["jobs"]] == ["다", "나", "가"]
        assert (out["running"], out["queued"], out["queue_max"]) == (1, 2, 20)
        assert [r.get("queue", {}).get("position") for r in out["jobs"]] == [2, 1, None]

    _play(scenario)


# ── MCP 도구 ─────────────────────────────────────────────────────────────────
def test_시작_응답이_줄을_섰다는_것과_순번을_말한다(eng, monkeypatch):
    _caps(monkeypatch, 1)

    async def scenario():
        first = await m.deliberate_start("가", ctx=_ctx(YOU))
        assert first["status"] == "running" and "queue" not in first and "시작했다" in first["note"]
        out = await m.deliberate_start("나", ctx=_ctx(ME))
        assert out["status"] == "queued" and out["caller"] == ME
        assert (out["queue"]["position"], out["queue"]["ahead"], out["queue"]["length"]) == (1, 0, 1)
        for want in ("대기열", "1번째", "스스로 시작", "다시 시작하지 마라", "deliberate_status", "deliberate_cancel"):
            assert want in out["note"], (want, out["note"])
        res = await m.deliberate_result(out["job_id"])
        assert res["status"] == "queued" and "대기" in res["note"] and res["decision"] is None
        menu = await m.deliberate_jobs(ctx=_ctx(ME))
        assert menu["queue"] == {"length": 1, "max": 20,
                                 "mine": [{"job_id": out["job_id"], "position": 1, "ahead": 0}]}
        assert (await m.deliberate_jobs())["queue"]["mine"] == [], "신원 없는 호출에 누군가의 순번을 보여 줬다"
        assert (await m.deliberate_jobs(ctx=_ctx(THIRD)))["queue"] == {"length": 1, "max": 20, "mine": []}

    _play(scenario)


def test_이어하기도_줄을_선다(eng, monkeypatch):
    _caps(monkeypatch, 1)

    async def scenario():
        a = _start("가", ME)
        await _tick()
        await _end(eng, a)
        _start("나", YOU)
        out = await m.deliberate_continue(a["id"], "두께를 다시 보라", ctx=_ctx(ME))
        assert out["status"] == "queued" and out["queue"]["position"] == 1
        assert out["continued_from"] == a["id"] and "대기열" in out["note"]

    _play(scenario)


def _listed(name):
    return next(t.description for t in asyncio.run(m.mcp.list_tools()) if t.name == name)


def test_도구_설명이_세_상태를_알려_준다():
    """설명이 '즉시 시작한다' 고만 하면, queued 를 받은 호출자는 실패로 읽고 다시 시작한다 — 같은 심의가
    두 번 줄을 선다."""
    start = _listed("deliberate_start")
    for want in ("queued", "running", "queue.position", "다시 시작하지 마라", "DELIB_JOB_QUEUE_MAX"):
        assert want in start, f"deliberate_start 설명에 '{want}' 가 없다"
    status = _listed("deliberate_status")
    for want in ("queued", "running", "done", "queue"):
        assert want in status, f"deliberate_status 설명에 '{want}' 가 없다"
    assert "queued" in _listed("deliberate_result")
    assert "대기" in _listed("deliberate_cancel") and "대기" in _listed("deliberate_list")
    assert "queued" in m._INSTRUCTIONS


# ── 설정 ─────────────────────────────────────────────────────────────────────
def test_기본은_20_이고_0_은_거절로_되돌린다():
    def loaded(value):
        env = {k: v for k, v in os.environ.items() if k != "DELIB_JOB_QUEUE_MAX"}
        if value is not None:
            env["DELIB_JOB_QUEUE_MAX"] = value
        r = subprocess.run([sys.executable, "-c", "import delib_jobs as j; print(j.QUEUE_MAX)"], cwd=ROOT,
                           capture_output=True, text=True, timeout=60,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
        assert r.returncode == 0, r.stderr[-600:]
        return int(r.stdout.split()[-1])

    assert (loaded(None), loaded(""), loaded("5"), loaded("0"), loaded("-4")) == (20, 20, 5, 0, 0)


# ── 아무 순서로나 ────────────────────────────────────────────────────────────────
def _owner(job):
    return str(job.get("user") or "").strip().lower()


def _check(jobs):
    """어느 순간에도 서야 하는 것들 — 상한을 넘지 않고, 뜰 수 있는 잡이 있는데 자리를 비워 두지 않는다."""
    running = [j for j in jobs if _st(j) == "running"]
    queued = [j for j in jobs if _st(j) == "queued"]
    assert len(running) <= delib_jobs.MAX_RUNNING, "전역 상한을 넘겨 돌고 있다"
    per = {}
    for j in running:
        if _owner(j):
            per[_owner(j)] = per.get(_owner(j), 0) + 1
    assert all(n <= delib_jobs.MAX_RUNNING_PER_USER for n in per.values()), f"사용자별 상한을 넘겼다 — {per}"
    assert [j["id"] for j in queued] == list(delib_jobs._PENDING), "줄과 잡 상태가 어긋났다"
    assert len(queued) <= delib_jobs.QUEUE_MAX
    assert all(j["id"] in delib_jobs._TASKS for j in running), "도는 잡에 태스크가 없다"
    if len(running) < delib_jobs.MAX_RUNNING:
        free = [j["question"] for j in queued
                if not _owner(j) or per.get(_owner(j), 0) < delib_jobs.MAX_RUNNING_PER_USER]
        assert not free, f"자리가 비었는데 뜰 수 있는 잡이 줄에 있다 — {free}"


@pytest.mark.parametrize("seed", [1, 7, 20261007])
def test_아무_순서로_시작하고_끝내고_접어도_자리와_줄이_어긋나지_않는다(eng, monkeypatch, seed):
    _caps(monkeypatch, 3, 2, queue=6)
    rng = random.Random(seed)

    async def scenario():
        jobs = []
        for step in range(400):
            running = [j for j in jobs if _st(j) == "running"]
            queued = [j for j in jobs if _st(j) == "queued"]
            op = rng.choice(["start", "start", "start", "end", "end", "fail", "cancel", "unqueue"])
            if op == "start":
                try:
                    jobs.append(_start(f"화두{step}", rng.choice([ME, YOU, THIRD, ""])))
                except RuntimeError as exc:
                    assert "대기열" in str(exc) and len(queued) == 6, exc
            elif op in ("end", "fail") and running:
                await _end(eng, rng.choice(running), fail=(op == "fail"))
            elif op == "cancel" and running:
                delib_jobs.cancel(rng.choice(running)["id"])     # 막 띄운 것(첫 걸음 전)도 걸린다
            elif op == "unqueue" and queued:
                delib_jobs.cancel(rng.choice(queued)["id"])
            if rng.random() < 0.6:
                await _tick(rng.randint(1, 4))
            _check(jobs)
        while delib_jobs._TASKS or delib_jobs._PENDING:          # 남은 것을 전부 끝까지 돌린다
            for j in jobs:
                eng.gate(j["question"]).set()
            await _tick()
            _check(jobs)
        return jobs

    jobs = _play(scenario)
    assert len(jobs) > 100, "시험이 잡을 거의 못 만들었다"
    assert {_st(j) for j in jobs} <= {"done", "error", "cancelled"}, {_st(j) for j in jobs}
    assert len(eng.started) == len(set(eng.started)), "같은 잡이 두 번 떴다"
    assert eng.peak <= 3 and eng.now == 0
    done = [j["question"] for j in jobs if _st(j) == "done"]
    assert done and set(done) <= set(eng.started)
    for j in jobs:
        assert _on_disk(j)["status"] == _st(j), "파일에 남은 상태가 다르다"
