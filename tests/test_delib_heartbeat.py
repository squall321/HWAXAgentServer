# 심의 SSE heartbeat(ping) — 이벤트가 없는 동안에도 스트림이 살아 있고, 그 때문에 잃는 프레임이 없는지
#
# 심의는 LLM 호출 한 번(의장 결정문·좌석 발언·요약)이 도는 동안 스트림에 바이트가 0 이었다. 20석 넘는 패널이
# 공유 LLM 에 줄을 서면 그 침묵이 수십 분이라, 바깥의 침묵 한도(포털 릴레이 · nginx /agent/ 1시간 · 리스크 앱
# 읽기 1,860초 · 사내 프록시)가 **살아 있는** 심의의 구독을 끊었다. LLM 한도를 넉넉히 올리려면 먼저 이 침묵이
# 없어야 한다.
#
# **구독자를 실제로 돌려서** 본다 — 느린 생성기를 물리고, 나온 프레임을 센다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_heartbeat.py -q
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as a  # noqa: E402
import delib_jobs  # noqa: E402


def _frames(chunks):
    return [delib_jobs._parse_sse(c) for c in chunks]


def _real(chunks):
    return [(ev, data) for ev, data in _frames(chunks) if ev != "ping"]


def _play(gen):
    """생성기를 분리 스트림에 물려 끝까지 받는다 — 분리는 도는 이벤트 루프 안에서 해야 한다(태스크를 만든다)."""
    async def go():
        return [c async for c in a._detach_stream(gen, "t")]

    return asyncio.run(go())


def test_조용한_동안_ping_이_흐르고_프레임은_그대로_온다(monkeypatch):
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.02)

    async def slow():
        yield a._sse("status", {"step": "의사결정문 합성 중"})
        await asyncio.sleep(0.25)                      # LLM 호출 한 번 — 이 동안 종전엔 바이트가 0 이었다
        yield a._sse("result", {"type": "text", "content": "결정문"})
        yield a._sse("done", {})

    chunks = _play(slow())
    assert [ev for ev, _d in _real(chunks)] == ["status", "result", "done"]
    pings = [d for ev, d in _frames(chunks) if ev == "ping"]
    assert len(pings) >= 5, f"0.25초 침묵에 0.02초 간격인데 ping 이 {len(pings)}번뿐이다"
    # ping 은 침묵 구간에만 온다 — status 와 result 사이다.
    names = [ev for ev, _d in _frames(chunks)]
    assert names[0] == "status" and set(names[1:names.index("result")]) == {"ping"}, names
    # 내용 없는 프레임이다 — 경과(초, 늘어난다)와 시각(epoch 밀리초)뿐이다.
    assert all(set(p) == {"idle_s", "ts"} for p in pings), pings
    assert all(isinstance(p["idle_s"], int) and p["ts"] > 1_600_000_000_000 for p in pings), pings
    assert [p["idle_s"] for p in pings] == sorted(p["idle_s"] for p in pings)


def test_ping_프레임은_SSE_한_덩이다(monkeypatch):
    """`event: ping\\ndata: {...}\\n\\n` — 주석 줄(`: ping`)이 아니다. 이 스트림을 읽는 셋이 이벤트 이름으로
    가르고(모르는 이름은 버린다), 포털 화면은 이 프레임을 받아 '마지막 진행 N초 전' 을 그린다."""
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.01)

    async def slow():
        await asyncio.sleep(0.05)
        yield a._sse("done", {})

    raw = _play(slow())[0].decode()
    assert re.fullmatch(r'event: ping\ndata: \{"idle_s": \d+, "ts": \d+\}\n\n', raw), raw
    assert json.loads(raw.split("data: ", 1)[1])["idle_s"] == 0


def test_만료와_도착이_겹쳐도_프레임을_잃지_않는다(monkeypatch):
    """ping 을 내려고 기다림을 끊는 순간에 프레임이 도착해도 그 프레임은 온다 — 그 프레임이 결정문일 수 있다.
    간격과 같은 박자로 프레임을 흘려 겹치게 만들고, 하나도 안 빠지고 순서도 그대로인지 센다.
    (구현은 get 을 취소하지 않고 쥔 채 기다린다. 취소하는 쪽 — wait_for — 도 이 venv 에서는 이 시험을
    통과했다. 이 시험은 그 둘을 가르지 않고, 어느 구현이든 프레임을 잃으면 문다.)"""
    beat = 0.004
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", beat)
    n = 150

    async def racing():
        for i in range(n):
            await asyncio.sleep(beat if i % 3 else beat * 1.05)
            yield a._sse("status", {"step": f"프레임 {i}"})
        yield a._sse("done", {})

    chunks = _play(racing())
    got = [d["step"] for ev, d in _real(chunks) if ev == "status"]
    assert got == [f"프레임 {i}" for i in range(n)], f"{n}개 중 {len(got)}개만 왔다"
    assert _real(chunks)[-1][0] == "done"
    assert any(ev == "ping" for ev, _d in _frames(chunks)), "시험 전제 — 만료가 한 번도 안 났다"


def test_ping_은_진행_중인_심의를_건드리지_않는다(monkeypatch):
    """한도는 구독 쪽에만 건다 — 생성기를 감싸면 만료 때 진행 중인 LLM 호출이 취소된다."""
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.01)
    seen = {}

    async def one_long_call():
        try:
            await asyncio.sleep(0.15)                  # 간격의 15배짜리 호출 하나
            seen["끝까지"] = True
        except asyncio.CancelledError:
            seen["취소됨"] = True
            raise
        yield a._sse("done", {})

    chunks = _play(one_long_call())
    assert seen == {"끝까지": True}, seen
    assert [ev for ev, _d in _real(chunks)] == ["done"]


def test_간격을_0_으로_두면_ping_이_없다(monkeypatch):
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.0)

    async def slow():
        yield a._sse("status", {"step": "가"})
        await asyncio.sleep(0.05)
        yield a._sse("done", {})

    chunks = _play(slow())
    assert [ev for ev, _d in _frames(chunks)] == ["status", "done"]


def test_구독이_끊겨도_심의는_끝까지_돈다(monkeypatch):
    """종전 계약 그대로다 — ping 을 넣으면서 깨지지 않았는지 본다. 구독자가 ping 을 기다리는 중에 끊긴다."""
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.01)
    seen = {}

    async def saves_at_the_end():
        yield a._sse("status", {"step": "가"})
        await asyncio.sleep(0.08)
        seen["저장"] = True                             # 보고서·대화 저장은 생성기 꼬리에 있다
        yield a._sse("done", {})

    async def go():
        stream = a._detach_stream(saves_at_the_end(), "t")
        got = []
        async for c in stream:
            got.append(delib_jobs._parse_sse(c)[0])
            if got[-1] == "ping":
                break                                   # 브라우저가 닫혔다
        await stream.aclose()
        await asyncio.sleep(0.15)
        return got

    got = asyncio.run(go())
    assert got[0] == "status" and got[-1] == "ping", got
    assert seen == {"저장": True}, "구독이 끊기자 심의도 멈췄다"


def test_잡_원장은_ping_을_버린다():
    """이 리포 안의 소비자 — MCP 잡 원장은 생성기를 직접 구동해 ping 을 받을 일이 없지만, 받아도 상태가
    바뀌지 않아야 한다(모르는 이벤트는 버린다)."""
    job = {"id": "t", "status": "running", "steps": ["가"], "step": "가"}
    before = json.dumps(job, ensure_ascii=False, sort_keys=True)
    delib_jobs._apply(job, *delib_jobs._parse_sse(a._sse("ping", {"idle_s": 30, "ts": 1})))
    assert json.dumps(job, ensure_ascii=False, sort_keys=True) == before


def test_기본_간격은_15초다():
    """포털 화면이 ping 이 세 번(45초) 끊기면 '신호 없음' 으로 바꾼다 — 그 계산이 이 기본값 위에 있다.
    환경값이 아니라 **소스의 기본값**을 본다(박스 설정이 이 시험을 흔들지 않게)."""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert re.search(r'^DELIB_HEARTBEAT_S = _env_float\("DELIB_HEARTBEAT_S", 15\.0\)$', src, re.M)


def test_heartbeat_를_끄고_뜨면_기동_로그가_바깥_한도를_말한다(monkeypatch, capsys):
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    @asynccontextmanager
    async def _no_session_manager(_app):          # MCP 세션 매니저는 프로세스에 한 번만 열 수 있다 — 건너뛴다
        yield

    monkeypatch.setattr(a, "_load_mcp_config", dict)                 # 실 게이트웨이 설정을 읽지 않는다
    monkeypatch.setattr(a._DELIB_MCP.router, "lifespan_context", _no_session_manager)

    async def boot():
        async with a.lifespan(FastAPI()):
            pass

    try:
        monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 15.0)
        asyncio.run(boot())
        assert "DELIB_HEARTBEAT_S=0" not in capsys.readouterr().out
        monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.0)
        asyncio.run(boot())
        out = capsys.readouterr().out
        assert "DELIB_HEARTBEAT_S=0" in out and "2×DELIB_TIMEOUT_S" in out, out
        for knob in ("AGENT_STREAM_IDLE_TIMEOUT_S", "NGINX_AGENT_READ_TIMEOUT", "HWAXRISK_ENGINE_READ_TIMEOUT_S"):
            assert knob in out, f"바깥 침묵 한도 {knob} 를 말하지 않았다"
    finally:
        delib_jobs.closing(False)                 # 종료 절차가 '내려가는 중' 을 세워 두었다 — 다음 시험이 잡을 띄운다
