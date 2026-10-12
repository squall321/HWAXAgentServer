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
