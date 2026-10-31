# 웹 심의 멈춤 창구(POST /deliberate/cancel) — 대화 id 로 도는 분리 심의를 접고, 스트림은 멈춤 사건과 done 으로 닫히며, 거듭 · 끝난 · 모르는 · 남의 것을 가른다
"""HWAXPortal docs/leftovers-1017 triage A24 (나) — 사용자 결정 10-31.

웹 심의(포털 화면의 /심의 · /시뮬심의 · /시험계획)는 잡 원장이 없는 분리 태스크(app._detach_stream)라, 구독이 끊긴 뒤 서버에서 도는 것을
멈출 길이 없었다(HWAXPortal docs/change-request-8-10/server-setup §6 「끊긴 웹 심의의 결과 회수·취소」). 포털이 싣는 서버 대화 id 로 태스크를
적어 두고, 창구가 MCP 잡의 deliberate_cancel 처럼 태스크를 취소한다. 고정하는 것.
  · 엔진 생성기의 finally(남은 LLM 호출 접기)가 돈다 — 취소는 생성기를 닫는다.
  · 스트림은 멈춤 사건(delib kind=cancelled)과 done 으로 닫힌다 — 오류(error)가 아니다. 첫 걸음 전에 닿은 취소도 스트림을 닫는다(끝없이 기다리지 않는다).
  · 거듭 보내면 같은 답(이미 멈췄다) · 끝난 심의는 409 already_finished · 모르는 대화는 404 · user_email 을 주면 그 사람의 심의만.
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


async def _collect(stream, out: list) -> None:
    async for chunk in stream:
        ev, data = delib_jobs._parse_sse(chunk)
        if ev != "ping":
            out.append((ev, data))


async def _post(body) -> httpx.Response:
    """창구를 같은 이벤트 루프에서 부른다(태스크를 취소하는 쪽과 도는 쪽이 같은 루프여야 한다 — TestClient 는 다른 스레드의 루프다)."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=a.app), base_url="http://agent") as c:
        return await c.post("/deliberate/cancel", json=body)


async def _until(pred, timeout: float = 5.0) -> None:
    t0 = asyncio.get_running_loop().time()
    while not pred():
        assert asyncio.get_running_loop().time() - t0 < timeout, "기다린 일이 일어나지 않았다"
        await asyncio.sleep(0.01)


def _turned(frames) -> bool:
    return any(ev == "delib" and d.get("kind") == "turn" for ev, d in frames)


# ── 멈춤 ─────────────────────────────────────────────────────────────────────────────────────────
def test_도는_웹_심의를_대화_id_로_멈추면_finally_가_돌고_멈춤_사건과_done_으로_닫힌다():
    seen: list = []

    async def go():
        stream = a._detach_stream(_engine(seen), "delib", conv="c1", user=ME)
        frames: list = []
        reader = asyncio.create_task(_collect(stream, frames))
        await _until(lambda: _turned(frames))                     # 라운드 도중이다
        assert a._delib_load()[0] == 1
        r = await _post({"conversation_id": "c1", "by": ME, "user_email": ME})
        await asyncio.wait_for(reader, 5)
        again = await _post({"conversation_id": "c1", "by": ME, "user_email": ME})
        return r, frames, again, a._delib_load()[0], a._WEB_RUNS["c1"][-1]

    r, frames, again, active, run = asyncio.run(go())
    assert r.status_code == 200 and r.json()["status"] == "cancelling" and r.json()["runs"] == 1, r.text
    assert frames[-2:] == [("delib", {"kind": "cancelled", "by": ME, "message": a._WEB_CANCEL_MSG}), ("done", {})], frames[-3:]
    assert [ev for ev, _d in frames].count("done") == 1
    assert not [d for ev, d in frames if ev == "error"], "멈춤은 실패가 아니다 — error 로 내지 않는다"
    assert not [d for ev, d in frames if d.get("kind") == "decision"], "멈춘 심의는 결정문을 내지 않는다"
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
        return out, frames

    out, frames = asyncio.run(go())
    assert out["status"] == "cancelling" and out["runs"] == 1
    assert frames == [("delib", {"kind": "cancelled", "by": "운영", "message": a._WEB_CANCEL_MSG}), ("done", {})], frames
    assert seen == [], "생성기는 시작하지도 않았다 — 접을 것이 없다"


def test_한_대화에_나란히_도는_심의_둘을_한_번에_멈춘다():
    """끊긴 화면에서 다시 시도하면 같은 대화에 두 번째 심의가 나란히 돈다(server-setup §5 '끊긴 화면') — 멈춤은 둘 다 접는다."""
    seen: list = []

    async def go():
        s1 = a._detach_stream(_engine(seen), "delib", conv="c1", user=ME)
        s2 = a._detach_stream(_engine(seen), "delib", conv="c1", user=ME)
        f1, f2 = [], []
        readers = [asyncio.create_task(_collect(s1, f1)), asyncio.create_task(_collect(s2, f2))]
        await _until(lambda: _turned(f1) and _turned(f2))
        r = await _post({"conversation_id": "c1", "by": ME, "user_email": ME})
        await asyncio.wait_for(asyncio.gather(*readers), 5)
        return r, f1, f2

    r, f1, f2 = asyncio.run(go())
    assert r.status_code == 200 and r.json()["runs"] == 2, r.text
    for frames in (f1, f2):
        assert frames[-2][1]["kind"] == "cancelled" and frames[-1] == ("done", {})
    assert seen == ["finally", "finally"]


# ── 끝난 · 모르는 · 남의 것 ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("error_frame, state", [(False, "done"), (True, "error")])
def test_멈추기_전에_끝난_심의는_409_already_finished_로_어떻게_끝났는지_말한다(error_frame, state):
    seen: list = []

    async def go():
        frames: list = []
        await _collect(a._detach_stream(_engine(seen, n=2, step=0.0, error_frame=error_frame), "delib", conv="c1", user=ME),
                       frames)
        return await _post({"conversation_id": "c1", "by": ME, "user_email": ME}), frames

    r, frames = asyncio.run(go())
    assert r.status_code == 409, r.text
    body = r.json()
    assert (body["code"], body["status"]) == ("already_finished", state) and "이미 끝난 심의" in body["message"]
    assert frames[-1] == ("done", {}) and not [d for ev, d in frames if d.get("kind") == "cancelled"]


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


def test_user_email_을_주면_그_사람이_띄운_심의만_접고_운영의_호출은_전부_접는다():
    """MCP deliberate_cancel 과 같은 규칙 — 남의 심의는 접지 못한다(신원 없이 부르는 운영의 루프백 호출은 종전대로 전부)."""
    seen: list = []

    async def go():
        frames: list = []
        reader = asyncio.create_task(_collect(a._detach_stream(_engine(seen), "delib", conv="c1", user=ME), frames))
        await _until(lambda: _turned(frames))
        other = await _post({"conversation_id": "c1", "by": YOU, "user_email": YOU})
        still = a._WEB_RUNS["c1"][-1]["state"]
        upper = await _post({"conversation_id": "c1", "by": ME, "user_email": ME.upper()})   # 대소문자만 다른 같은 사람
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
])
def test_본문이_어긋나면_422_로_거절하고_아무것도_접지_않는다(body):
    c = TestClient(a.app)
    r = c.post("/deliberate/cancel", content=body) if isinstance(body, bytes) else c.post("/deliberate/cancel", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["code"] in ("bad_request", "unknown_fields")


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
