# 웹 심의 멈춤 창구(POST /deliberate/cancel) — 대화 id 와 실행 id(run_id)로 도는 분리 심의를 접고, 스트림은 멈춤 사건과 done 으로 닫히며, 의장이 결정문을 낸 뒤의 마무리 · 거듭 · 끝난 · 모르는 · 남의 것을 가른다
"""HWAXPortal docs/leftovers-1017 triage A24 (나) — 사용자 결정 10-31.

웹 심의(포털 화면의 /심의 · /시뮬심의 · /시험계획)는 잡 원장이 없는 분리 태스크(app._detach_stream)라, 구독이 끊긴 뒤 서버에서 도는 것을
멈출 길이 없었다(HWAXPortal docs/change-request-8-10/server-setup §6 「끊긴 웹 심의의 결과 회수·취소」). 포털이 싣는 서버 대화 id 로 태스크를
적어 두고, 창구가 MCP 잡의 deliberate_cancel 처럼 태스크를 취소한다. 고정하는 것.
  · 엔진 생성기의 finally(남은 LLM 호출 접기)가 돈다 — 취소는 생성기를 닫는다.
  · 스트림은 멈춤 사건(delib kind=cancelled)과 done 으로 닫힌다 — 오류(error)가 아니다. 첫 걸음 전에 닿은 취소도 스트림을 닫는다(끝없이 기다리지 않는다).
  · 심의마다 실행 id 가 있고 첫 심의 사건과 멈춤 사건에 실린다. 사람(user_email)을 실은 멈춤은 그 실행 id 로 그 심의 하나만 접는다 — 같은 대화의
    다른(새) 심의는 건드리지 않는다(A24 검토 — 낡은 화면의 '멈춤' 이 같은 대화에서 다른 기기가 띄운 새 심의를 접었다). 대화 전체 멈춤은 운영의
    루프백 호출(신원 없음)만이다.
  · 의장이 결정문을 낸 뒤의 마무리(요약 · 쉬운 설명 · 대조 · 보고서 저장 — 엔진의 finishing 표지부터 결정문까지)에는 멈추지 않고 409 finishing
    이다(A24 검토 — 보고 단계의 멈춤이 다 만든 결정문을 버리고 Report Archive 초안을 고아로 남겼다). 다단 심의는 앞 단의 결정문 뒤 다음 단에서 다시 멈출 수 있다.
  · 거듭 보내면 같은 답(이미 멈췄다) · 끝난 심의는 409 already_finished · 모르는 대화 · 실행은 404 · user_email 을 주면 그 사람의 심의만.
  · 서버가 내려가며 접힌 태스크(창구 밖의 취소)는 종전처럼 멈춤 사건 없이 닫히고 interrupted 로 적힌다.
실제 프로세스 · HTTP · 가짜 LLM 으로 끝까지 보는 시험은 tests/test_web_delib_cancel_e2e.py 다.

  실행:  .venv/bin/python -m pytest tests/test_web_delib_cancel.py -q
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402

ME, YOU = "u1@corp.example", "u2@corp.example"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(a, "_WEB_RUNS", {})
    monkeypatch.setattr(a, "_DETACHED_TASKS", set())
    monkeypatch.setattr(a, "_DETACHED_BY", {})
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.05)


def _engine(seen: list, *, n: int = 400, step: float = 0.02, error_frame: bool = False):
    """심의 엔진 대역 — 라운드 발언을 step 초마다 하나씩 낸다(LLM 호출 하나). finally 는 엔진의 '남은 LLM 호출 접기' 자리다."""
    async def gen():
        try:
            yield a._sse("delib", {"kind": "stage", "stage": "r1"})
            for i in range(n):
                await asyncio.sleep(step)
                yield a._sse("delib", {"kind": "turn", "round": 1, "persona": f"p{i}", "say": "초기 입장"})
            if error_frame:
                yield a._sse("error", {"code": "chair_failed", "message": "의장이 결정문을 내지 못했다"})
            yield a._sse("delib", {"kind": "decision", "text": "결정문"})
            yield a._sse("done", {})
        finally:
            seen.append("finally")
    return gen()


def _phases(seen: list, gates: list):
    """단(phase)이 여럿인 심의 대역 — 단마다 라운드(gates[i][0] 이 열릴 때까지 발언 하나 뒤에 걸린다) → 의장 → finishing 표지 → 쉬운 설명 →
    보고서 저장(gates[i][1] 이 열릴 때까지 걸린다) → 결정문 · 결과. 엔진(_deliberation_stream)은 의장 호출이 돌아온 바로 뒤에 finishing 을 낸다."""
    async def gen():
        try:
            for i, (rounds, report) in enumerate(gates):
                yield a._sse("delib", {"kind": "stage", "stage": "r1"})
                yield a._sse("delib", {"kind": "turn", "round": 1, "persona": f"p{i}", "say": f"{i + 1}단 입장"})
                await rounds.wait()
                yield a._sse("delib", {"kind": "stage", "stage": "decide"})
                await asyncio.sleep(0.01)                                  # 의장 호출
                yield a._sse("delib", {"kind": "finishing"})
                yield a._sse("delib", {"kind": "stage", "stage": "explain"})
                yield a._sse("delib", {"kind": "plain", "text": "쉬운 설명"})
                yield a._sse("delib", {"kind": "stage", "stage": "report"})
                await report.wait()                                        # Report Archive 저장(실측 3시간 심의에 1시간 반)
                yield a._sse("delib", {"kind": "decision", "text": f"{i + 1}단 결정문"})
                yield a._sse("delib", {"kind": "outcome", "report_id": 40 + i})
            yield a._sse("done", {})
        finally:
            seen.append("finally")
    return gen()


def _opened() -> asyncio.Event:
    e = asyncio.Event()
    e.set()
    return e


async def _collect(stream, out: list) -> None:
    async for chunk in stream:
        ev, data = delib_jobs._parse_sse(chunk)
        if ev != "ping":
            out.append((ev, data))


async def _post(body) -> httpx.Response:
    """창구를 같은 이벤트 루프에서 부른다(태스크를 취소하는 쪽과 도는 쪽이 같은 루프여야 한다 — TestClient 는 다른 스레드의 루프다)."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=a.app), base_url="http://agent") as c:
        return await c.post("/deliberate/cancel", json=body)


def _mine(rid: str, who: str = ME, cid: str = "c1") -> dict:
    """사람(포털)의 멈춤 — 그 사람이 띄운 그 실행 하나."""
    return {"conversation_id": cid, "run_id": rid, "by": who, "user_email": who}


def _rid(cid: str = "c1", i: int = -1) -> str:
    return a._WEB_RUNS[cid][i]["id"]


async def _until(pred, timeout: float = 5.0) -> None:
    t0 = asyncio.get_running_loop().time()
    while not pred():
        assert asyncio.get_running_loop().time() - t0 < timeout, "기다린 일이 일어나지 않았다"
        await asyncio.sleep(0.01)


def _turned(frames) -> bool:
    return any(ev == "delib" and d.get("kind") == "turn" for ev, d in frames)


def _kinds(frames) -> list:
    return [d.get("kind") for ev, d in frames if ev == "delib"]


# ── 멈춤 ─────────────────────────────────────────────────────────────────────────────────────────
def test_도는_웹_심의를_실행_id_로_멈추면_finally_가_돌고_멈춤_사건과_done_으로_닫힌다():
    seen: list = []

    async def go():
        stream = a._detach_stream(_engine(seen), "delib", conv="c1", user=ME)
        frames: list = []
        reader = asyncio.create_task(_collect(stream, frames))
        await _until(lambda: _turned(frames))                     # 라운드 도중이다
        assert a._delib_load()[0] == 1
        rid = _rid()
        r = await _post(_mine(rid))
        await asyncio.wait_for(reader, 5)
        again = await _post(_mine(rid))
        return rid, r, frames, again, a._delib_load()[0], a._WEB_RUNS["c1"][-1]

    rid, r, frames, again, active, run = asyncio.run(go())
    assert r.status_code == 200 and r.json()["status"] == "cancelling" and r.json()["runs"] == 1, r.text
    assert r.json()["run_id"] == rid
    assert frames[-2:] == [("delib", {"kind": "cancelled", "by": ME, "message": a._WEB_CANCEL_MSG, "run_id": rid}),
                           ("done", {})], frames[-3:]
    assert [ev for ev, _d in frames].count("done") == 1
    assert not [d for ev, d in frames if ev == "error"], "멈춤은 실패가 아니다 — error 로 내지 않는다"
    assert "decision" not in _kinds(frames), "멈춘 심의는 결정문을 내지 않는다"
    assert seen == ["finally"], "엔진 생성기의 finally(남은 LLM 호출 접기)가 돌지 않았다"
    assert again.status_code == 200 and again.json()["already"] is True and again.json()["status"] == "cancelled", again.text
    assert active == 0, "멈춘 태스크가 delib_active 에 남았다"
    assert (run["state"], run["by"], run["task"]) == ("cancelled", ME, None) and run["finished_at"] is not None


def test_멈춤_사건의_by_는_창구가_받은_값이고_첫_걸음_전에_닿은_취소도_스트림을_닫는다():
    """취소가 태스크의 첫 걸음보다 먼저 닿으면 _drive 의 몸(finally 포함)이 돌지 않는다 — 끝 표지를 거기에 두면 구독자가 끝없이 기다린다."""
    seen: list = []

    async def go():
        stream = a._detach_stream(_engine(seen), "delib", conv="c1", user=ME)
        out = a._web_cancel("c1", "운영", "")          # 같은 걸음 — 태스크는 아직 첫 걸음을 떼지 않았다
        frames: list = []
        await asyncio.wait_for(_collect(stream, frames), 5)
        return out, frames, _rid()

    out, frames, rid = asyncio.run(go())
    assert out["status"] == "cancelling" and out["runs"] == 1
    assert frames == [("delib", {"kind": "cancelled", "by": "운영", "message": a._WEB_CANCEL_MSG, "run_id": rid}),
                      ("done", {})], frames
    assert seen == [], "생성기는 시작하지도 않았다 — 접을 것이 없다"


def test_한_대화에_나란히_도는_심의_둘을_운영의_호출은_한_번에_멈추고_사람의_호출은_짚은_것만_멈춘다():
    """끊긴 화면에서 다시 시도하면 같은 대화에 두 번째 심의가 나란히 돈다(server-setup §5 '끊긴 화면'). 사람은 제 화면의 심의(실행 id)만
    접고, 신원 없이 부르는 운영의 루프백 호출은 그 대화의 웹 심의를 전부 접는다."""
    seen: list = []

    async def go():
        streams = [a._detach_stream(_engine(seen), "delib", conv="c1", user=ME) for _ in range(3)]
        fs: list = [[], [], []]
        readers = [asyncio.create_task(_collect(s, f)) for s, f in zip(streams, fs)]
        await _until(lambda: all(_turned(f) for f in fs))
        one = await _post(_mine(_rid(i=0)))
        await asyncio.wait_for(readers[0], 5)
        left = [r["state"] for r in a._WEB_RUNS["c1"]]
        ops = await _post({"conversation_id": "c1", "by": "운영"})
        await asyncio.wait_for(asyncio.gather(*readers[1:]), 5)
        return one, left, ops, fs

    one, left, ops, fs = asyncio.run(go())
    assert one.status_code == 200 and one.json()["runs"] == 1, one.text
    assert left == ["cancelled", "running", "running"], "사람의 멈춤이 짚지 않은 심의까지 접었다"
    assert ops.status_code == 200 and ops.json()["runs"] == 2, ops.text
    for frames in fs:
        assert frames[-2][1]["kind"] == "cancelled" and frames[-1] == ("done", {})
    assert seen == ["finally"] * 3


# ── 실행 id — 낡은 화면의 멈춤이 같은 대화의 새 심의를 접지 않는다(A24 검토) ───────────────────────────────────────────
def test_심의마다_실행_id_가_있고_첫_심의_사건에만_실린다():
    seen: list = []

    async def go():
        frames: list = []
        await _collect(a._detach_stream(_engine(seen, n=3, step=0.0), "delib", conv="c1", user=ME), frames)
        frames2: list = []
        await _collect(a._detach_stream(_engine(seen, n=1, step=0.0), "delib", conv="c1", user=ME), frames2)
        return frames, frames2, [r["id"] for r in a._WEB_RUNS["c1"]]

    frames, frames2, ids = asyncio.run(go())
    delib = [d for ev, d in frames if ev == "delib"]
    assert delib[0] == {"kind": "stage", "stage": "r1", "run_id": ids[0]}, delib[0]
    assert not [d for d in delib[1:] if "run_id" in d], "실행 id 는 첫 심의 사건 하나에만 싣는다(발언마다 싣지 않는다)"
    assert [d for ev, d in frames2 if ev == "delib"][0]["run_id"] == ids[1]
    assert len(set(ids)) == 2 and all(isinstance(x, str) and 8 <= len(x) <= 64 for x in ids)


def test_끝난_심의의_실행_id_로_멈추면_409_이고_같은_대화의_새_심의는_그대로_돈다():
    """기기 A 는 끝난 심의 #1 을 끊긴 채로 보고 있고, 기기 B 가 같은 대화에서 #2 를 띄웠다. A 의 '멈춤' 은 #1 을 짚는다 — #2 를 접지 않는다."""
    seen: list = []

    async def go():
        f1: list = []
        await _collect(a._detach_stream(_engine(seen, n=2, step=0.0), "delib", conv="c1", user=ME), f1)
        f2: list = []
        reader = asyncio.create_task(_collect(a._detach_stream(_engine(seen), "delib", conv="c1", user=ME), f2))
        await _until(lambda: _turned(f2))
        stale = await _post(_mine(_rid(i=0)))
        state2 = a._WEB_RUNS["c1"][1]["state"]
        unknown = await _post(_mine("0" * 16))
        mine2 = await _post(_mine(_rid(i=1)))
        await asyncio.wait_for(reader, 5)
        return stale, state2, unknown, mine2, f2

    stale, state2, unknown, mine2, f2 = asyncio.run(go())
    assert stale.status_code == 409 and (stale.json()["code"], stale.json()["status"]) == ("already_finished", "done"), stale.text
    assert state2 == "running", "끝난 심의의 멈춤이 같은 대화의 새 심의를 접었다"
    assert unknown.status_code == 404 and unknown.json()["code"] == "delib_not_found", unknown.text
    assert mine2.status_code == 200 and mine2.json()["runs"] == 1 and f2[-2][1]["kind"] == "cancelled"


def test_사람을_실은_멈춤은_실행_id_가_있어야_하고_대화_전체는_운영의_호출만이다():
    seen: list = []

    async def go():
        frames: list = []
        reader = asyncio.create_task(_collect(a._detach_stream(_engine(seen), "delib", conv="c1", user=ME), frames))
        await _until(lambda: _turned(frames))
        r = await _post({"conversation_id": "c1", "by": ME, "user_email": ME})
        state = a._WEB_RUNS["c1"][-1]["state"]
        a._web_cancel("c1", "운영", "")
        await asyncio.wait_for(reader, 5)
        return r, state

    r, state = asyncio.run(go())
    assert r.status_code == 422 and r.json()["code"] == "run_id_required", r.text
    assert state == "running", "실행 id 없이 사람이 대화 전체를 접었다"


# ── 의장이 결정문을 낸 뒤의 마무리 — 멈추지 않는다(A24 검토) ─────────────────────────────────────────────────────────────
def _at_report(seen: list, report: asyncio.Event, frames: list) -> asyncio.Task:
    """보고 단계(결정문은 이미 있고 Report Archive 저장이 걸린 동안)까지 간 심의 하나 — 읽는 태스크를 돌려준다."""
    return asyncio.create_task(_collect(a._detach_stream(_phases(seen, [(_opened(), report)]), "delib", conv="c1", user=ME),
                                        frames))


def test_보고서를_저장하는_동안에는_운영의_멈춤도_409_finishing_이고_심의는_결정문까지_간다():
    """보고 단계(Report Archive 저장 — 실측 3시간 심의에 1시간 반)에서 멈추면 다 만든 결정문이 버려지고 RA 초안은 대화에 번호 없이 남았다.
    finishing 표지(엔진이 의장 호출이 돌아온 바로 뒤에 낸다)부터 결정문까지는 접지 않는다 — 신원 없이 부르는 운영의 루프백 호출도 같다."""
    seen: list = []

    async def go():
        report, frames = asyncio.Event(), []
        reader = _at_report(seen, report, frames)
        await _until(lambda: any(d.get("stage") == "report" for _ev, d in frames))
        ops = await _post({"conversation_id": "c1", "by": "운영"})
        report.set()
        await asyncio.wait_for(reader, 5)
        late = await _post({"conversation_id": "c1", "by": "운영"})
        return ops, frames, late

    ops, frames, late = asyncio.run(go())
    assert ops.status_code == 409 and ops.json()["code"] == "finishing", ops.text
    assert "결정문" in ops.json()["message"] and "Report Archive" in ops.json()["message"]
    assert "cancelled" not in _kinds(frames) and _kinds(frames)[-2:] == ["decision", "outcome"], frames[-4:]
    assert frames[-1] == ("done", {}) and seen == ["finally"]
    assert late.status_code == 409 and late.json()["status"] == "done", "마무리를 마친 심의는 끝난 심의다"


def test_의장이_결정문을_낸_뒤의_마무리에는_사람의_멈춤도_409_finishing_이다():
    seen: list = []

    async def go():
        report, frames = asyncio.Event(), []
        reader = _at_report(seen, report, frames)
        await _until(lambda: any(d.get("stage") == "report" for _ev, d in frames))
        mine = await _post(_mine(_rid()))
        state = a._WEB_RUNS["c1"][-1]["state"]
        report.set()
        await asyncio.wait_for(reader, 5)
        return mine, state, frames

    mine, state, frames = asyncio.run(go())
    assert mine.status_code == 409 and mine.json()["code"] == "finishing" and mine.json()["run_id"] == _rid_of(frames), mine.text
    assert state == "running" and "cancelled" not in _kinds(frames) and "decision" in _kinds(frames)


def _rid_of(frames) -> str:
    return next(d["run_id"] for ev, d in frames if ev == "delib" and "run_id" in d)


def test_다단_심의는_앞_단의_결정문_뒤_다음_단에서_다시_멈출_수_있다():
    """/시뮬심의 는 1단 결정문 · 보고서를 낸 뒤 2단을 돈다 — 마무리 막음은 그 단의 결정문에서 풀린다(2단 라운드는 몇 시간이다)."""
    seen: list = []

    async def go():
        rounds2 = asyncio.Event()
        frames: list = []
        reader = asyncio.create_task(_collect(a._detach_stream(
            _phases(seen, [(_opened(), _opened()), (rounds2, _opened())]), "sim", conv="c1", user=ME), frames))
        await _until(lambda: any(d.get("say") == "2단 입장" for _ev, d in frames))
        r = await _post(_mine(_rid()))
        await asyncio.wait_for(reader, 5)
        return r, frames

    r, frames = asyncio.run(go())
    assert r.status_code == 200 and r.json()["status"] == "cancelling", r.text
    assert _kinds(frames).count("decision") == 1 and frames[-2][1]["kind"] == "cancelled" and frames[-1] == ("done", {})


def test_멈춤_글은_결정문_보고서가_없다고_단정하지_않고_RA_초안이_남을_수_있다고_말한다():
    """다단 심의의 앞 단 · 이어하기의 앞 회차는 결정문과 Report Archive 보고서를 이미 냈다 — '보고서는 만들지 않았다' 는 거짓이 된다."""
    assert "Report Archive" in a._WEB_CANCEL_MSG and "보고서는 만들지 않았다" not in a._WEB_CANCEL_MSG


# ── 끝난 · 모르는 · 남의 것 ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("error_frame, state", [(False, "done"), (True, "error")])
def test_멈추기_전에_끝난_심의는_409_already_finished_로_어떻게_끝났는지_말한다(error_frame, state):
    seen: list = []

    async def go():
        frames: list = []
        await _collect(a._detach_stream(_engine(seen, n=2, step=0.0, error_frame=error_frame), "delib", conv="c1", user=ME),
                       frames)
        return await _post(_mine(_rid())), frames

    r, frames = asyncio.run(go())
    assert r.status_code == 409, r.text
    body = r.json()
    assert (body["code"], body["status"]) == ("already_finished", state) and "이미 끝난 심의" in body["message"]
    assert frames[-1] == ("done", {}) and "cancelled" not in _kinds(frames)


def test_모르는_대화와_대화_id_없이_띄운_심의는_404_delib_not_found_다():
    seen: list = []

    async def go():
        frames: list = []
        stream = a._detach_stream(_engine(seen, n=30, step=0.01), "delib")       # 대화 id 가 없다 — 적지 않는다
        reader = asyncio.create_task(_collect(stream, frames))
        r1 = await _post({"conversation_id": "nope", "by": ME})
        await asyncio.wait_for(reader, 5)
        return r1, frames

    r1, frames = asyncio.run(go())
    assert r1.status_code == 404 and r1.json()["code"] == "delib_not_found", r1.text
    assert a._WEB_RUNS == {}, "대화 id 가 없는 심의를 적었다"
    assert frames[-1] == ("done", {}) and frames[-2][1]["kind"] == "decision", "멈추지 않은 심의는 끝까지 돈다"
    assert "run_id" not in frames[0][1], "멈출 열쇠가 없는 심의에 실행 id 를 싣지 않는다"


def test_user_email_을_주면_그_사람이_띄운_심의만_접고_운영의_호출은_전부_접는다():
    """MCP deliberate_cancel 과 같은 규칙 — 남의 심의는 접지 못한다(신원 없이 부르는 운영의 루프백 호출은 종전대로 전부)."""
    seen: list = []

    async def go():
        frames: list = []
        reader = asyncio.create_task(_collect(a._detach_stream(_engine(seen), "delib", conv="c1", user=ME), frames))
        await _until(lambda: _turned(frames))
        other = await _post(_mine(_rid(), who=YOU))
        still = a._WEB_RUNS["c1"][-1]["state"]
        upper = await _post(_mine(_rid(), who=ME.upper()))   # 대소문자만 다른 같은 사람
        await asyncio.wait_for(reader, 5)
        return other, still, upper, frames

    other, still, upper, frames = asyncio.run(go())
    assert other.status_code == 404 and other.json()["code"] == "delib_not_found", other.text
    assert still == "running", "남의 요청에 심의가 멈췄다"
    assert upper.status_code == 200 and upper.json()["status"] == "cancelling"
    assert frames[-2][1]["kind"] == "cancelled"


def test_서버가_내려가며_접힌_태스크는_멈춤_사건_없이_닫히고_interrupted_로_적힌다():
    """창구 밖의 취소(정상 종료 — 루프가 남은 태스크를 취소한다)는 사람이 멈춘 것이 아니다. 종전처럼 끝 표지만 낸다."""
    seen: list = []

    async def go():
        frames: list = []
        reader = asyncio.create_task(_collect(a._detach_stream(_engine(seen), "delib", conv="c1", user=ME), frames))
        await _until(lambda: _turned(frames))
        a._WEB_RUNS["c1"][-1]["task"].cancel()
        await asyncio.wait_for(reader, 5)
        r = await _post({"conversation_id": "c1", "by": ME})
        return frames, r

    frames, r = asyncio.run(go())
    assert not [d for ev, d in frames if ev in ("done", "error") or d.get("kind") == "cancelled"], frames[-3:]
    assert seen == ["finally"]
    assert r.status_code == 409 and r.json()["status"] == "interrupted", r.text


# ── 창구의 모양(/drain · /limits 와 같은 규칙 — 모르는 칸은 거절) ─────────────────────────────────────────────
@pytest.mark.parametrize("body", [
    b"not json", b"[1]",
    {"conversation_id": "c1", "by": "운영", "oops": 1},
    {"by": "운영"}, {"conversation_id": "  ", "by": "운영"}, {"conversation_id": "c" * 129, "by": "운영"},
    {"conversation_id": 7, "by": "운영"},
    {"conversation_id": "c1"}, {"conversation_id": "c1", "by": ""}, {"conversation_id": "c1", "by": "x" * 201},
    {"conversation_id": "c1", "by": "운영", "user_email": 3},
    {"conversation_id": "c1", "by": "운영", "run_id": 7}, {"conversation_id": "c1", "by": "운영", "run_id": "r" * 65},
    {"conversation_id": "c1", "by": ME, "user_email": ME}, {"conversation_id": "c1", "by": ME, "user_email": ME, "run_id": " "},
])
def test_본문이_어긋나면_422_로_거절하고_아무것도_접지_않는다(body):
    c = TestClient(a.app)
    r = c.post("/deliberate/cancel", content=body) if isinstance(body, bytes) else c.post("/deliberate/cancel", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["code"] in ("bad_request", "unknown_fields", "run_id_required")


def test_모르는_칸은_이름을_되비추며_거절한다():
    r = TestClient(a.app).post("/deliberate/cancel", json={"conversation_id": "c1", "by": "운영", "force": True})
    assert r.status_code == 422 and r.json()["code"] == "unknown_fields" and r.json()["fields"] == ["force"]


# ── 기록 ──────────────────────────────────────────────────────────────────────────────────────────
def test_끝난_기록은_대화마다_몇_건_대화는_상한까지만_두고_도는_것은_버리지_않는다(monkeypatch):
    monkeypatch.setattr(a, "_WEB_RUNS_KEEP", 2)
    monkeypatch.setattr(a, "_WEB_RUNS_PER_CONV", 1)
    done = SimpleNamespace(cancelled=lambda: False)
    running = a._web_run_open("live", ME, "delib")
    for cid in ("old", "mid", "new"):
        for _ in range(3):
            a._web_run_close(a._web_run_open(cid, ME, "delib"), done)
    assert set(a._WEB_RUNS) == {"live", "mid", "new"}, "끝난 기록만 남은 대화 가운데 가장 오래된 것부터 버린다"
    assert [len(a._WEB_RUNS[c]) for c in ("mid", "new")] == [1, 1]
    assert a._WEB_RUNS["live"] == [running] and running["state"] == "running"


def test_웹_세_트리거는_대화_id_와_띄운_사람을_분리_태스크에_넘긴다(monkeypatch):
    seen = []

    def capture(stream, label, started_by="human", conv="", user=""):
        seen.append((label, conv, user))

        async def empty():
            if False:
                yield b""

        return empty()

    monkeypatch.setattr(a, "_detach_stream", capture)
    for msg, label in (("/심의 화두", "delib"), ("/시뮬심의 현상", "sim"), ("/시험계획 목적", "test-plan")):
        asyncio.run(a.chat(a.ChatRequest(message=msg, conversation_id="conv-7", user_email=ME)))
        assert seen[-1] == (label, "conv-7", ME), msg
