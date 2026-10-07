# MCP 심의 잡의 진행 조회가 '살아 있는지' 를 보이는지 — 마지막 이벤트 뒤 경과(idle_s)와 그 이벤트(last_step)
#
# 진행 조회는 단계·상태줄·걸린 시간만 줬다. 22석 패널이 공유 LLM 에 줄을 서면 같은 단계가 30분씩 이어지는데,
# 그것이 LLM 을 기다리는 것인지 멈춘 것인지 호출자는 알 길이 없었다 — 그래서 다시 시작했고, 같은 심의가 둘 돌았다.
# 시작 안내는 '보통 수 분~수십 분' 이라고 했다.
#
# **실제 잡 태스크를 돌려서** 본다 — 시계만 손으로 돌린다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_job_idle.py -q
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

# 같은 하네스를 쓴다 — 문이 열릴 때까지 '도는 중' 으로 남는 엔진과 빈 원장(eng 는 이름째 가져와 이 파일에도 건다).
from test_delib_job_queue import _end, _play, _start, _tick, eng  # noqa: E402, F401
from test_delib_silent_drops import _pin_context  # noqa: E402, F401


class _Clock:
    """손으로 돌리는 시계 — 벽시계에 기대면 바쁜 박스에서 흔들린다."""

    def __init__(self, monkeypatch):
        self.t = 1_000_000.0
        monkeypatch.setattr(delib_jobs, "_now", lambda: self.t)


def _llm(read=1800.0, retries=1):
    return SimpleNamespace(request_timeout=httpx.Timeout(read if read else None, connect=10.0), max_retries=retries)


def test_도는_잡은_마지막_이벤트_뒤_경과와_그_이벤트를_보인다(eng, monkeypatch):
    clock = _Clock(monkeypatch)
    monkeypatch.setattr(m, "_APP", SimpleNamespace(state=SimpleNamespace(delib_llm=_llm())))

    async def scenario():
        job = _start("힌지 크랙")
        await _tick()                                   # 엔진이 '힌지 크랙 시작' 을 내고 문 앞에서 기다린다
        first = await m.deliberate_status(job["id"])
        clock.t += 1500                                 # LLM 호출 한 번을 25분째 기다리는 중
        waiting = await m.deliberate_status(job["id"])
        await _end(eng, job)
        return first, waiting, await m.deliberate_status(job["id"])

    first, waiting, done = _play(scenario)
    assert first["status"] == "running" and first["idle_s"] == 0.0 and first["last_step"] == "힌지 크랙 시작", first
    assert waiting["idle_s"] == 1500.0 and waiting["elapsed_s"] == 1500.0, waiting
    # 무엇과 견줄지 같이 준다 — 호출 한도 1,800초 × 2회 시도 + 재시도 대기. 그 안이면 기다리는 중이다.
    assert waiting["quiet_ok_s"] == 3608.0 and waiting["idle_s"] < waiting["quiet_ok_s"]
    assert done["status"] == "done" and "idle_s" not in done and "quiet_ok_s" not in done, done


def test_좌석_발언과_근거도_마지막_이벤트로_적힌다(monkeypatch):
    """상태줄(step)은 라운드가 도는 동안 그대로다 — 그것만 보면 좌석이 하나씩 발언하는 것과 멈춘 것이 같아 보인다."""
    _Clock(monkeypatch)
    job = {"id": "t", "status": "running", "question": "q", "started_at": delib_jobs._now(),
           "updated_at": delib_jobs._now()}

    def feed(event, data):
        delib_jobs._apply(job, *delib_jobs._parse_sse(d._sse(event, data)))
        return delib_jobs.summary(job)

    assert feed("status", {"step": "2라운드 — 상호 반박·수치 심화"})["last_step"] == "2라운드 — 상호 반박·수치 심화"
    out = feed("delib", {"kind": "turn", "round": 2, "display_round": 5, "persona": "mech-a", "say": "발언"})
    assert out["last_step"] == "좌석 발언 — mech-a (5R)" and out["step"] == "2라운드 — 상호 반박·수치 심화", out
    assert feed("delib", {"kind": "evidence", "source": "rel-b · get_material", "text": "값",
                          "included": True})["last_step"] == "근거 — rel-b · get_material"
    assert feed("delib", {"kind": "stage", "stage": "decide"})["last_step"] == "단계 — decide"
    assert feed("warning", {"code": "seat_lost", "message": "3라운드 좌석 유실"})["last_step"].startswith("경고 — 3라운드")
    # 진행이 아닌 이벤트는 마지막 이벤트를 덮지 않는다.
    for event, data in (("token", {"delta": "가"}), ("ping", {"idle_s": 15, "ts": 1}), ("done", {})):
        assert feed(event, data)["last_step"].startswith("경고 — 3라운드"), event


def test_조용해도_되는_시간은_그_잡에_걸린_한도로_잰다(eng, monkeypatch):
    """요청 timeout_s 로 돈 잡은 그 값이다 — 서버 기본값으로 재면 7,200초를 청한 잡이 멀쩡히 기다리는 중에
    '넘었다' 로 읽힌다."""
    _Clock(monkeypatch)
    asked = iter(range(100))

    async def scenario(state, **start_kw):
        monkeypatch.setattr(m, "_APP", SimpleNamespace(state=state))
        job = _start(f"화두 {next(asked)}", **start_kw)      # 화두마다 문이 하나다 — 같은 화두를 또 쓰면 열린 문으로 지나간다
        await _tick()
        out = await m.deliberate_status(job["id"])
        await _end(eng, job)
        return out["quiet_ok_s"]

    assert _play(lambda: scenario(SimpleNamespace(delib_llm=_llm(1800.0, 1)))) == 3608.0
    assert _play(lambda: scenario(SimpleNamespace(delib_llm=_llm(600.0, 2)))) == 1808.0
    assert _play(lambda: scenario(SimpleNamespace(delib_llm=_llm(1800.0, 1)),
                                  delib_opts={"timeout_s": 7200})) == 14408.0
    assert _play(lambda: scenario(SimpleNamespace(delib_llm=_llm(0, 1)))) is None, "한도가 꺼진 박스에 수를 지어냈다"
    assert _play(lambda: scenario(SimpleNamespace(delib_llm=None))) is None


def test_줄_선_잡과_끝난_잡에는_경과가_없다(eng, monkeypatch):
    from test_delib_job_queue import _caps

    _Clock(monkeypatch)
    _caps(monkeypatch, 1)

    async def scenario():
        a, b = _start("가"), _start("나")
        await _tick()
        return await m.deliberate_status(a["id"]), await m.deliberate_status(b["id"])

    running, queued = _play(scenario)
    assert "idle_s" in running and queued["status"] == "queued"
    assert "idle_s" not in queued and "last_step" not in queued and "quiet_ok_s" not in queued, queued


def test_안내가_수_시간과_idle_s_를_말한다(eng, monkeypatch):
    async def scenario():
        out = await m.deliberate_start("화두")
        await _tick()
        return out["note"], {t.name: t.description for t in await m.mcp.list_tools()}

    note, listed = _play(scenario)
    assert "수 시간" in note and "idle_s" in note and "quiet_ok_s" in note and "다시 시작하지 마라" in note, note
    assert "수십 분" not in note, "대형 패널은 수 시간이다 — 종전 안내가 남아 있다"
    for name in ("deliberate_start", "deliberate_status"):
        for want in ("idle_s", "last_step", "quiet_ok_s", "수 시간", "DELIB_TIMEOUT_S"):
            assert want in listed[name], (name, want)
