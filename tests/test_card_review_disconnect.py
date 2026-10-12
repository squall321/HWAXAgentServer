# 카드 대조 스트림의 구독을 실제 소켓으로 끊으면(uvicorn 의 http.disconnect 길) 도는 LLM 호출이 취소되고 관문 자리가 돌아오는지 본다
#
# TestClient 와 제너레이터 aclose() 는 uvicorn 의 끊김 감지 길을 지나지 않는다(P-17l). 그래서 빈 포트에 실제 uvicorn 을 띄우고
# httpx 로 스트림을 읽다가 연결을 닫는다. 떠 있는 9009 를 쓰지 않는다 — 커널이 고른 빈 포트를 쓰고, 서버는 이 프로세스의
# 스레드라 끝나면 함께 내려간다(남의 프로세스를 죽이지 않는다). lifespan 은 끈다(실 LLM·MCP 를 만들지 않는다).
#
#   실행:  .venv/bin/python -m pytest tests/test_card_review_disconnect.py -q
import random
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402

import app as a  # noqa: E402
import card_review as cr  # noqa: E402
from fakes.llm import Act, FakeLLM  # noqa: E402
from test_card_review_prompts import _digest, _pack, _req, _unit  # noqa: E402

PACK = _pack(random.Random(1), n=4, size=(300, 600))


@pytest.fixture
def server(monkeypatch):
    llm = FakeLLM([Act("slow", delay=30)])
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.05)
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setattr(a.app.state, "delib_llm", llm, raising=False)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port != 9009
    srv = uvicorn.Server(uvicorn.Config(a.app, host="127.0.0.1", port=port, log_level="error", lifespan="off"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started, "시험용 uvicorn 이 뜨지 않았다"
    yield f"http://127.0.0.1:{port}", llm
    srv.should_exit = True
    th.join(timeout=10)


def test_팩을_청한_호출자가_떠나면_빌드를_접고_자리를_돌려준다(server, monkeypatch):
    """RB-8 — 팩은 비스트리밍이라 바깥(포털 단발 한도)이 포기해도 엔진은 몰랐고, 버려진 팩을 끝까지 지으며 자리를 쥐었다.
    카드 한 장에 2초씩 걸리게 하고(첫 묶음 6장이 도는 중) 호출자가 0.8초에 떠나면, 엔진이 끊김을 보고 빌드를 취소한다 —
    도는 조회가 접히고 둘째 묶음은 나가지 않으며 팩 관문 자리가 돌아온다."""
    import asyncio

    from test_card_review_pack import Hub, KEY, _tools

    base, _llm = server
    hub = Hub()
    hub.get_sleep = 2.0
    calls, orig = [], hub.get_record

    async def get_record(args):
        calls.append(args["record_id"])
        return await orig(args)

    hub.get_record = get_record

    async def fake_load(_app, groups, *, user_email, user_pat):
        return _tools(hub)

    monkeypatch.setattr(cr, "load_pack_tools", fake_load)
    monkeypatch.setattr(cr, "PACK_POLL_S", 0.1)
    monkeypatch.setattr(a.app.state, "card_review_pack_sem", None, raising=False)
    assert len(hub.records) > cr._KN_CONC
    with httpx.Client(timeout=httpx.Timeout(5.0, read=0.8), trust_env=False) as c:
        with pytest.raises(httpx.ReadTimeout):
            c.post(f"{base}/card-review/pack", json={"agent_key": KEY, "user_pat": "pat"})
    for _ in range(60):
        sem = getattr(a.app.state, "card_review_pack_sem", None)
        if hub.active == 0 and sem is not None and sem._value == cr.PACK_CONC:
            break
        time.sleep(0.05)
    assert hub.active == 0, "호출자가 떠났는데 조회가 돈다"
    assert a.app.state.card_review_pack_sem._value == cr.PACK_CONC, "팩 관문 자리가 돌아오지 않았다"
    n = len(calls)
    time.sleep(hub.get_sleep + 0.5)
    assert len(calls) == n == cr._KN_CONC, f"버려진 팩의 조회가 더 나갔다({n} → {len(calls)}, 전체 {len(hub.records)})"
    assert isinstance(a.app.state.card_review_pack_sem, asyncio.Semaphore)


def test_구독을_끊으면_LLM_이_취소되고_자리가_돌아온다(server):
    base, llm = server
    cards = [c for c in PACK if c["checkable"]][:2]
    body = _req("verdict", cards, _digest(PACK, [c["alias"] for c in cards]), unit=_unit(0), variant="fwd")
    seen = []
    with httpx.Client(timeout=10, trust_env=False) as c:
        with c.stream("POST", f"{base}/card-review/run", json=body) as r:
            assert r.status_code == 200
            for line in r.iter_lines():
                if line.startswith("event: "):
                    seen.append(line[7:])
                if "ping" in seen:
                    break                       # LLM 이 도는 중이다 — 여기서 연결을 닫는다
        gate = cr.gate_of(a.app)
        assert seen[0] == "status"
        for _ in range(100):
            if llm.cancelled and gate.active == 0:
                break
            time.sleep(0.05)
        assert llm.cancelled == 1 and llm.active == 0, "끊긴 구독의 LLM 호출이 계속 돈다"
        assert gate.active == 0 and gate.totals["cancelled"] == 1
        # 끊긴 뒤에도 서버는 멀쩡하고 /health 가 0 으로 돌아왔다.
        h = c.get(f"{base}/health").json()
        assert h["review_active"] == 0 and h["card_review"]["totals"]["cancelled"] == 1
