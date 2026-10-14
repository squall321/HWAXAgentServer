# 챗 첫 조각 지연(카드 대조가 돌 때와 아닐 때)을 두 고리에 재고, 평소의 2배를 넘으면 slow 와 로그 한 줄로 알리는지 — 가짜 LLM 서버의 --capacity 로 공유 LLM 이 밀리는 것을 흉내 내 본다
#
# D-34 — 낮 시간 카드 대조 동시 8, 챗 첫 응답 지연이 평소의 2배를 넘으면 알린다(자동으로 낮추지 않는다). 지연은 챗 턴의 첫
# on_chat_model_start 에서 첫 on_chat_model_stream 조각까지다(LLM 줄 대기·미리 채우기 — 도구 왕복은 들지 않는다, M3.md 4.7).
# 계측은 챗을 건드리지 않는다 — 실패해도 고리가 깨지지 않고 챗은 그대로 끝난다.
#
# 끝까지 보는 시험은 실제 챗 턴(_agent_stream)을 실제 ChatOpenAI(스트리밍)로 가짜 LLM 서버(빈 포트, --capacity 1)에 붙여 돈다.
# '카드 대조 8건이 자리를 쥔 동안' 은 관문 자리 8개와 가짜 서버의 /_hold(공유 LLM 의 자리를 쥔 카드 대조 호출의 대역)로 만든다.
# 탐침(scripts/llm_ttft_probe.py)은 --dry-run 으로 LLM 없이, 그리고 같은 가짜 서버로 실제로 돈다.
#
#   실행:  .venv/bin/python -m pytest tests/test_chat_ttft.py -q
import asyncio
import json
import signal
import socket
import subprocess
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import httpx  # noqa: E402
import pytest  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

import app  # noqa: E402
import card_review as cr  # noqa: E402
import review_gate as rg  # noqa: E402

PY = sys.executable


# ── ChatLoad(순수) ─────────────────────────────────────────────────────────────
def test_두_고리와_slow_판정():
    logs, clock = [], {"t": 1_000.0}
    load = rg.ChatLoad(clock=lambda: clock["t"], log=logs.append)
    for _ in range(9):
        load.record(100, 0)
        load.record(1_000, 8)
    assert load.slow is False, "고리마다 10턴이 차기 전에는 판정하지 않는다"
    load.record(100, 0)
    load.record(1_000, 8)
    snap = load.snapshot()
    assert snap["slow"] is True and snap["slow_since"] == 1_000.0 and snap["ratio_p50"] == 10.0
    assert snap["ttft_ms"] == {"idle": {"n": 10, "p50": 100, "p95": 100}, "review": {"n": 10, "p50": 1_000, "p95": 1_000}}
    assert len(logs) == 1 and "[load] ⚠ 챗 첫 조각 p50 1000ms 가 평소 100ms 의 10.0배다(카드 대조 8건 도는 중)" in logs[0]
    assert "CARD_REVIEW_CONCURRENCY" in logs[0]
    clock["t"] += 5
    load.record(1_000, 8)
    assert len(logs) == 1 and load.slow_since == 1_000.0, "이미 slow 면 다시 알리지 않는다"
    for _ in range(50):
        load.record(150, 8)
    assert load.slow is False and load.slow_since is None and len(load.rings["review"]) == 50, "고리는 최근 50턴"


def test_2배는_slow_가_아니다():
    load = rg.ChatLoad(log=lambda _m: None)
    for _ in range(10):
        load.record(100, 0)
        load.record(200, 3)
    assert load.snapshot()["slow"] is False and load.snapshot()["ratio_p50"] == 2.0


# ── _timed_events — 챗 스트림을 그대로 흘리며 한 번 잰다 ───────────────────────────
def _events(delay=0.05, fail_at=None):
    async def gen():
        yield {"event": "on_chat_model_start"}
        await asyncio.sleep(delay)
        yield {"event": "on_chat_model_stream", "data": {"chunk": types.SimpleNamespace(content="첫")}}
        yield {"event": "on_chat_model_stream", "data": {"chunk": types.SimpleNamespace(content="둘")}}
        yield {"event": "on_chat_model_start"}
        yield {"event": "on_chat_model_stream", "data": {"chunk": types.SimpleNamespace(content="셋")}}
    return gen()


def test_카드_대조가_돌면_review_고리에_한_턴에_한_번만_넣는다(monkeypatch):
    load = rg.ChatLoad(log=lambda _m: None)
    monkeypatch.setattr(app, "_CHAT_LOAD", load)
    monkeypatch.setattr(app.app.state, "review_gate", None, raising=False)
    gate = cr.gate_of(app.app)
    held = [gate.try_acquire("r") for _ in range(8)]

    async def drain(events):
        seen_active = []
        out = []
        async for ev in app._timed_events(events):
            seen_active.append(load.active)
            out.append(ev["event"])
        return out, seen_active

    out, active = asyncio.run(drain(_events()))
    for t in held:
        gate.release(t, "ok")
    assert out == ["on_chat_model_start", "on_chat_model_stream", "on_chat_model_stream", "on_chat_model_start",
                   "on_chat_model_stream"], "이벤트를 그대로 흘려야 한다"
    assert set(active) == {1} and load.active == 0
    assert len(load.rings["review"]) == 1 and len(load.rings["idle"]) == 0 and load.rings["review"][0] >= 40
    asyncio.run(drain(_events(delay=0)))
    assert len(load.rings["idle"]) == 1, "카드 대조가 0 이면 idle 고리다"


def test_계측이_실패해도_챗은_그대로_흐른다(monkeypatch, capsys):
    class Broken(rg.ChatLoad):
        def record(self, *_a, **_k):
            raise RuntimeError("고리 고장")

    monkeypatch.setattr(app, "_CHAT_LOAD", Broken())

    async def drain():
        return [ev["event"] async for ev in app._timed_events(_events(delay=0))]

    assert len(asyncio.run(drain())) == 5
    assert "계측 실패(무시)" in capsys.readouterr().out


def test_health_에_load_chat_이_실린다(monkeypatch):
    load = rg.ChatLoad(log=lambda _m: None)
    load.record(120, 0)
    monkeypatch.setattr(app, "_CHAT_LOAD", load)
    monkeypatch.setattr(app, "_model_context_tokens", lambda: 1_048_576)
    h = app.health()
    c = h["load"]["chat"]
    assert set(c) == {"active", "window", "ttft_ms", "ratio_p50", "slow", "slow_since"}
    assert c["ttft_ms"]["idle"] == {"n": 1, "p50": 120, "p95": 120} and c["window"] == 50


# ── 끝까지 — 실제 챗 턴 · 실제 ChatOpenAI 스트리밍 · 가짜 LLM 서버 --capacity 1 ─────────────────
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port != 9009
    return port


@pytest.fixture
def fake_llm(tmp_path):
    port, log = _free_port(), tmp_path / "fake-llm.log"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    with open(log, "w") as out:
        p = subprocess.Popen([PY, str(ROOT / "tests" / "fakes" / "llm_server.py"), "--port", str(port), "--ctx",
                              "1048576", "--model", "fake-glm", "--script", "oracle", "--capacity", "1"],
                             cwd=str(tmp_path), env=env, stdout=out, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    t0 = time.monotonic()
    with httpx.Client(timeout=2, trust_env=False) as c:
        while True:
            try:
                if c.get(f"{base}/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if p.poll() is not None or time.monotonic() - t0 > 60:
                pytest.fail(f"가짜 LLM 서버가 뜨지 않았다 — {log.read_text()[-1500:]}")
            time.sleep(0.1)
    yield base
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(timeout=10)


@tool
def noop(q: str) -> str:
    """아무것도 하지 않는 도구 — 에이전트를 앱이 만드는 길로 만들기 위한 자리."""
    return "ok"


def _agent(monkeypatch, llm):
    class _StubClient:
        def __init__(self, _connections):
            pass

        async def get_tools(self):
            return [noop]

    monkeypatch.setattr(app, "MultiServerMCPClient", _StubClient)
    state = types.SimpleNamespace(
        llm=llm, connections={"gateway": {"url": "http://gw/mcp", "headers": {"Authorization": "Bearer x"}}},
        agent_cache={}, tool_degraded={}, tool_load_error={}, tool_snapshot={})
    return asyncio.run(app._agent_for(types.SimpleNamespace(state=state), ["admin"]))


def test_카드_대조가_LLM_을_쥔_동안의_챗_첫_조각이_review_고리에_들고_slow_가_선다(monkeypatch, fake_llm, capsys):
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(base_url=f"{fake_llm}/v1", api_key="fake", model="fake-glm", temperature=0, max_retries=0,
                     timeout=30, http_client=httpx.Client(trust_env=False),
                     http_async_client=httpx.AsyncClient(trust_env=False))
    agent = _agent(monkeypatch, llm)

    async def built(*_a, **_k):
        return agent

    monkeypatch.setattr(app, "_agent_for", built)
    monkeypatch.setattr(app, "_CHAT_LOAD", rg.ChatLoad())
    monkeypatch.setattr(app.app.state, "review_gate", None, raising=False)
    monkeypatch.setattr(app.app.state, "llm_nostream", False, raising=False)
    monkeypatch.setenv("CHAT_AUTO_RETRY", "0")

    async def chat_turn():
        req = app.ChatRequest(message="안녕", groups=["portal-admin"], history=[])
        return [c async for c in app._agent_stream(app.app, req)]

    async def scenario():
        async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
            for _ in range(10):                                   # 평소 — 카드 대조 0
                await chat_turn()
            gate = cr.gate_of(app.app)
            held = [gate.try_acquire("card") for _ in range(8)]   # 카드 대조 8건이 자리를 쥐었다
            try:
                for _ in range(10):
                    hold = asyncio.ensure_future(c.post(f"{fake_llm}/_hold", params={"s": 0.4}))
                    while (await c.get(f"{fake_llm}/_stats")).json()["active"] < 1:
                        await asyncio.sleep(0.01)                 # 대역 호출이 LLM 자리를 쥘 때까지
                    await chat_turn()
                    await hold
            finally:
                for t in held:
                    gate.release(t, "ok")

    asyncio.run(scenario())
    snap = app._CHAT_LOAD.snapshot()
    assert snap["ttft_ms"]["idle"]["n"] == 10 and snap["ttft_ms"]["review"]["n"] == 10, snap
    assert snap["ttft_ms"]["review"]["p50"] > 2 * snap["ttft_ms"]["idle"]["p50"] and snap["slow"] is True, snap
    assert snap["ttft_ms"]["review"]["p50"] >= 250, "줄 선 시간(대역이 쥔 0.4초의 대부분)이 첫 조각 지연에 들어야 한다"
    assert "[load] ⚠ 챗 첫 조각" in capsys.readouterr().out


# ── 탐침 ─────────────────────────────────────────────────────────────────────
def test_탐침은_dry_run_으로_LLM_없이_돈다(tmp_path):
    r = subprocess.run([PY, str(ROOT / "scripts" / "llm_ttft_probe.py"), "--dry-run", "--count", "2", "--interval",
                        "0", "--env", str(tmp_path / "none.env")], capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "VLLM_BASE_URL": "http://u:p@h:1/v1"})
    assert r.returncode == 0, r.stderr
    assert "(dry-run)" in r.stdout and '"n": 2' in r.stdout and "u:p@" not in r.stdout, r.stdout


def test_탐침은_첫_조각까지의_초와_그때의_서버_수를_파일에_적고_비밀을_찍지_않는다(tmp_path, fake_llm):
    out = tmp_path / "ttft.jsonl"
    secret = "sk-secret-probe-9z8y7x"
    closed = f"http://127.0.0.1:{_free_port()}/health"
    r = subprocess.run([PY, str(ROOT / "scripts" / "llm_ttft_probe.py"), "--out", str(out), "--count", "2",
                        "--interval", "0.05", "--health", closed, "--env", str(tmp_path / "none.env")],
                       capture_output=True, text=True, timeout=120,
                       env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "VLLM_BASE_URL": f"{fake_llm}/v1",
                            "VLLM_MODEL": "fake-glm", "VLLM_API_KEY": secret, "NO_PROXY": "*"})
    assert r.returncode == 0, r.stderr
    rows = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    samples = [x for x in rows if x["t"] == "sample"]
    assert len(samples) == 2 and all(x["ok"] and x["ttft_s"] >= 0 for x in samples), samples
    assert all(x["review_active"] is None and x["health_error"] for x in samples), "못 읽은 /health 를 0 으로 적었다"
    assert rows[-1]["t"] == "summary" and rows[-1]["ok"] == 2
    assert secret not in r.stdout + r.stderr + out.read_text(encoding="utf-8")
    again = subprocess.run([PY, str(ROOT / "scripts" / "llm_ttft_probe.py"), "--out", str(out), "--count", "1"],
                           capture_output=True, text=True, timeout=60, env={"PATH": "/usr/bin:/bin"})
    assert again.returncode == 2 and "덮지 않는다" in again.stderr
