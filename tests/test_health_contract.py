# /health 에 더한 칸(review_active·draining·drain·capabilities·card_review)이 셸과 포털의 기존 판정을 흔들지 않고, /drain 이 계약대로 깃발을 세우고 내리며 재기동을 넘기는지 본다
#
# 기존 delib_active·delib_queued 는 이름도 뜻도 그대로다 — start.sh 는 `"delib_active": *[0-9]+` 를 grep 하고, 포털
# lib/delib-busy.sh(hwax_delib_busy)는 파이썬으로 읽는다. 둘이 실제 /health 응답 글(starlette 직렬화 그대로)에서 종전과 같은
# 수를 읽어야 한다. 셸 함수는 start.sh 에서 떼어 그대로 돌리고, 포털 판정은 형제 리포의 파이썬 조각을 그대로 돌린다(손으로
# 베끼지 않는다 — 한쪽만 바뀌면 붉다). 형제 리포가 곁에 없으면 그 시험만 건너뛰고, 곁에 있는데 조각을 못 찾으면 실패다(P-22).
#
# /drain 의 규칙(수명·고정·주체)은 review_gate.DrainState 시험이 본다. 여기서는 라우트 모양과, 실제 uvicorn 을 내렸다 올려도
# 깃발이 남는지 본다(임시 포트·임시 ARTIFACT_DIR — 떠 있는 9009 와 실 /data 를 건드리지 않는다).
#
#   실행:  .venv/bin/python -m pytest tests/test_health_contract.py -q
import json
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as a  # noqa: E402
import card_review as cr  # noqa: E402
from fakes.llm import FakeLLM  # noqa: E402

PORTAL_LIB = ROOT.parent / "HWAXPortal" / "infra" / "scripts" / "lib" / "delib-busy.sh"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("DRAIN_STATE_PATH", str(tmp_path / "drain.json"))
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"]), raising=False)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setattr(a, "_delib_human", lambda: 0)
    return TestClient(a.app)


# ── /health 칸 ────────────────────────────────────────────────────────────────
def test_health_의_M3_칸(client):
    h = client.get("/health").json()
    assert (h["delib_active"], h["delib_queued"], h["review_active"], h["draining"]) == (0, 0, 0, False)
    assert h["drain"] == {"on": False, "scope": None, "until": None, "until_idle": None, "hold": False, "by": None,
                          "reason": None, "set_at": None}
    assert h["capabilities"] == ["card_review", "drain"]
    c = h["card_review"]
    assert c["kinds"] == ["sweep", "verdict", "offcard", "noinput", "summary"]
    assert (c["limit"], c["limit_reason"], c["delib_human"]) == (8, "normal", 0)


def test_비우기가_서면_health_가_draining_과_limit_0_을_싣는다(client):
    client.post("/drain", json={"on": True, "scope": "all", "ttl_s": 600, "by": "update-all", "reason": "§2"})
    h = client.get("/health").json()
    assert h["draining"] is True and h["drain"]["on"] is True and h["drain"]["scope"] == "all"
    assert (h["card_review"]["limit"], h["card_review"]["limit_reason"]) == (0, "draining")
    client.post("/drain", json={"on": True, "scope": "panels", "until_idle": {"grace_s": 60, "max_s": 600},
                                "by": "update-all"})
    h = client.get("/health").json()
    assert h["draining"] is False and h["drain"]["on"] is True, "panels 는 카드 대조를 막지 않지만 패널은 막는다"
    assert h["drain"]["until_idle"]["grace_s"] == 60 and h["card_review"]["limit"] == 8


# ── 셸·포털이 읽는 수는 종전과 같다 ─────────────────────────────────────────────
def _fn(src: str, name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}\n", src, re.S | re.M)
    assert m, f"start.sh 에서 {name}() 를 못 찾았다 — 모양이 바뀌었으면 이 시험도 고친다"
    return m.group(0)


def _sh(fn_src: str, call: str, body: str) -> str:
    r = subprocess.run(["bash", "-c", f"set -euo pipefail\n{fn_src}\n{call}"], env={"PATH": "/usr/bin:/bin", "B": body},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _portal_busy(body: str) -> tuple[int, str]:
    src = PORTAL_LIB.read_text(encoding="utf-8")
    m = re.search(r"python3 - <<'PY'\n(.*?)\nPY\n", src, re.S)
    assert m, "포털 delib-busy.sh 의 파이썬 판정 조각을 못 찾았다 — 모양이 바뀌었으면 이 시험도 고친다"
    r = subprocess.run([sys.executable, "-c", m.group(1)], env={"B": body, "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip()


@pytest.mark.parametrize("load,review,drain", [((0, 0), 0, False), ((2, 1), 3, True), ((0, 4), 0, True)])
def test_셸과_포털이_실제_health_글에서_종전과_같은_수를_읽는다(client, monkeypatch, load, review, drain):
    monkeypatch.setattr(a, "_delib_load", lambda: load)
    gate = cr.gate_of(a.app)
    held = [gate.try_acquire("x") for _ in range(review)]
    if drain:
        client.post("/drain", json={"on": True, "scope": "all", "ttl_s": 600, "by": "human:koo", "reason": "점검"})
    body = client.get("/health").text                     # starlette 가 직렬화한 글 그대로
    for t in held:
        gate.release(t, "ok")
    start = (ROOT / "start.sh").read_text(encoding="utf-8")
    got = _sh(_fn(start, "delib_load"), 'delib_load "$B"', body).strip()
    assert got == f"{load[0]} {load[1]}", (got, body[:200])
    notes = _sh(_fn(start, "review_notes"), 'review_notes "$B"', body)
    assert (f"카드 대조 {review}건이 돈다" in notes) == (review > 0), notes
    assert ("리스크 검토 비우기 중(by human:koo)" in notes) == drain, notes
    if not PORTAL_LIB.parent.parent.parent.parent.is_dir():
        pytest.skip("형제 리포 HWAXPortal 이 곁에 없다")
    rc, out = _portal_busy(body)
    assert (rc, out) == ((1, "") if sum(load) == 0 else (0, f"{load[0]} {load[1]}"))


# ── /drain 라우트 ─────────────────────────────────────────────────────────────
def test_drain_의_모양과_거절(client):
    r = client.post("/drain", json={"on": True, "scope": "all", "ttl_s": 600, "by": "update-all", "reason": "§2"})
    v = r.json()
    assert r.status_code == 200 and set(v) == {"on", "scope", "until", "until_idle", "hold", "by", "reason", "set_at",
                                               "review_active", "delib_active", "delib_queued"}
    assert (v["on"], v["scope"], v["by"], v["hold"]) == (True, "all", "update-all", False)
    assert client.get("/drain").json() == v
    r = client.post("/drain", json={"on": False, "by": "someone-else"})
    assert r.status_code == 409 and r.json()["code"] == "drain_held" and r.json()["drain"]["by"] == "update-all"
    assert client.post("/drain", json={"on": False, "by": "update-all"}).json()["on"] is False
    for bad, code in (({"on": "yes"}, "bad_request"), ({"on": True, "scope": "all", "by": "x"}, "bad_request"),
                      ({"on": True, "scope": "x", "ttl_s": 1, "by": "x"}, "bad_request"),
                      ({"on": True, "scope": "all", "ttl_s": 1, "by": "x", "until": 5}, "unknown_fields"),
                      ({"on": True, "scope": "all", "until_idle": 5, "by": "x"}, "bad_request"),
                      ({"on": False, "by": "x", "force": "yes"}, "bad_request")):
        r = client.post("/drain", json=bad)
        assert (r.status_code, r.json().get("code")) == (422, code), (bad, r.status_code, r.json())
    assert client.post("/drain", content=b"{not json").status_code == 422


def test_drain_거절은_호출자_값을_80자_안으로_되비추고_쓰기_실패는_503_이다(client, monkeypatch):
    """RB3-15 — 모르는 칸 이름을 길이·개수 제한 없이 되비췄다. RB3-8 — 깃발 파일을 못 쓰면 500 이었다."""
    body = {"on": True, "scope": "all", "ttl_s": 60, "by": "x", **{("칸" * 5_000) + str(i): 1 for i in range(50)}}
    r = client.post("/drain", json=body)
    v = r.json()
    assert r.status_code == 422 and v["code"] == "unknown_fields" and v["fields_n"] == 50, (r.status_code, str(v)[:200])
    assert len(v["fields"]) == 20 and all(len(f) <= 100 for f in v["fields"]) and len(v["message"]) < 3_000
    r = client.post("/drain", json={"on": True, "scope": "all", "ttl_s": 60, "by": "u" * 10_000})
    assert r.status_code == 200 and len(r.json()["by"]) == 120 and len(client.get("/health").json()["drain"]["by"]) == 120

    def deny(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cr.gate_of(a.app).drain, "_write", deny)
    try:
        r = client.post("/drain", json={"on": True, "scope": "all", "ttl_s": 600, "by": "u" * 10_000})
    except PermissionError:                # 라우트 밖으로 샌 예외(500) — 사유가 없다
        r = None
    assert r is not None and (r.status_code, r.json().get("code")) == (503, "drain_unavailable"), (
        r and (r.status_code, r.text[:200]))


def test_사람의_고정은_update_all_이_풀지_못한다(client):
    client.post("/drain", json={"on": True, "scope": "all", "hold": True, "by": "human:koo", "reason": "점검"})
    r = client.post("/drain", json={"on": True, "scope": "all", "ttl_s": 1800, "by": "update-all"})
    assert r.status_code == 409 and r.json()["code"] == "drain_held"
    assert client.post("/drain", json={"on": False, "by": "update-all"}).status_code == 409
    r = client.post("/drain", json={"on": False, "by": "human:koo", "force": True})
    assert r.status_code == 200 and r.json()["on"] is False


def test_깃발을_세운_서버의_run_은_1초_안에_review_busy_draining_이다(client):
    from test_card_review_prompts import _pack, _req, _unit

    llm = a.app.state.delib_llm
    client.post("/drain", json={"on": True, "scope": "all", "ttl_s": 600, "by": "update-all"})
    pack = _pack()
    req = _req("verdict", [c for c in pack if c["checkable"]][:1], [], unit=_unit(0), variant="fwd")
    t0 = time.monotonic()
    r = client.post("/card-review/run", json=req)
    frames = [ln for ln in r.text.split("\n\n") if ln.strip()]
    assert time.monotonic() - t0 < 1.0 and llm.calls == []
    assert len(frames) == 2 and '"reason": "draining"' in frames[0] and frames[1].startswith("event: done")


# ── 재기동을 넘긴다(실제 uvicorn) ───────────────────────────────────────────────
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port != 9009
    return port


def _serve(tmp: Path, port: int, log: Path) -> subprocess.Popen:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
           "VLLM_BASE_URL": f"http://127.0.0.1:{_free_port()}/v1", "VLLM_MODEL": "fake", "VLLM_API_KEY": "fake",
           "MCP_CONFIG": str(tmp / "mcp.json"), "MCP_SERVERS": "", "DELIB_JOB_DIR": str(tmp / "jobs"),
           "ARTIFACT_DIR": str(tmp / "artifacts")}
    out = open(log, "a")
    return subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port),
                             "--log-level", "warning"], cwd=str(ROOT), env=env, stdout=out, stderr=subprocess.STDOUT)


def _wait(url: str, proc, log: Path):
    t0 = time.monotonic()
    with httpx.Client(timeout=2, trust_env=False) as c:
        while time.monotonic() - t0 < 90:
            if proc.poll() is not None:
                pytest.fail(f"서버가 떠 있지 않다 — {log.read_text()[-2000:]}")
            try:
                if c.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
    pytest.fail(f"{url} 가 뜨지 않았다 — {log.read_text()[-2000:]}")


def _stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_깃발은_엔진을_내렸다_올려도_남는다(tmp_path):
    (tmp_path / "mcp.json").write_text("{}", encoding="utf-8")
    port, log = _free_port(), tmp_path / "agent.log"
    base = f"http://127.0.0.1:{port}"
    p = _serve(tmp_path, port, log)
    try:
        _wait(f"{base}/health", p, log)
        with httpx.Client(timeout=10, trust_env=False) as c:
            r = c.post(f"{base}/drain", json={"on": True, "scope": "all", "ttl_s": 900, "by": "update-all",
                                               "reason": "재기동 시험"})
            assert r.status_code == 200 and r.json()["on"] is True
    finally:
        _stop(p)
    assert json.loads((tmp_path / "artifacts" / "review-drain.json").read_text(encoding="utf-8"))["by"] == "update-all"
    p = _serve(tmp_path, port, log)
    try:
        _wait(f"{base}/health", p, log)
        with httpx.Client(timeout=10, trust_env=False) as c:
            d = c.get(f"{base}/drain").json()
            h = c.get(f"{base}/health").json()
        assert (d["on"], d["scope"], d["by"], d["reason"]) == (True, "all", "update-all", "재기동 시험")
        assert h["draining"] is True and h["card_review"]["limit_reason"] == "draining"
    finally:
        _stop(p)
