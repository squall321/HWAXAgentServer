# 가짜 LLM 서버(1,048,576 창)와 임시 에이전트 서버를 따로 띄워 /card-review/limits 와 run 을 실제 프로세스·HTTP·ChatOpenAI 로 끝까지 돌린다
#
# 단계 D 리허설(M2.md 4.6)의 바닥이다 — dev LLM 은 16K 창이라 1백만 토큰 예산을 실제 스택으로 볼 수 없다. 두 프로세스 모두
# 커널이 고른 빈 포트에 뜨고(떠 있는 9009·실 vLLM·게이트웨이를 건드리지 않는다), 환경은 최소로 새로 짠다(실 .env·프록시·
# LLM_CONTEXT_TOKENS 를 물려받지 않는다). 잡 원장·산출물 디렉터리는 임시 경로다 — 에이전트 서버는 기동 때 잡 원장의 도는
# 잡을 interrupted 로 정리하므로(reap_orphans), 실 원장을 가리키면 떠 있는 서버의 심의를 끊는다. 끝나면 이 시험이 띄운 PID 만
# 내린다(pkill 을 쓰지 않는다).
#
# 손으로 같은 배선을 띄우려면(리허설):
#   .venv/bin/python tests/fakes/llm_server.py --port <P1> --ctx 1048576 --model fake-glm --script oracle
#   env -i PATH=/usr/bin:/bin HOME=$HOME VLLM_BASE_URL=http://127.0.0.1:<P1>/v1 VLLM_MODEL=fake-glm VLLM_API_KEY=fake \
#       MCP_CONFIG=<빈 {} 파일> DELIB_JOB_DIR=<임시> ARTIFACT_DIR=<임시> NO_PROXY=127.0.0.1,localhost \
#       .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port <P2>        (리포 루트에서)
#
#   실행:  .venv/bin/python -m pytest tests/test_card_review_e2e.py -q
import json
import random
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

from test_card_review_prompts import _digest, _pack, _req, _unit  # noqa: E402

PY = sys.executable
WINDOW = 1_048_576
PACK = _pack(random.Random(1), n=4, size=(300, 600))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port != 9009
    return port


def _wait(url: str, proc: subprocess.Popen, log: Path, timeout_s: float = 90.0) -> None:
    t0 = time.monotonic()
    with httpx.Client(timeout=2, trust_env=False) as c:
        while time.monotonic() - t0 < timeout_s:
            if proc.poll() is not None:
                pytest.fail(f"{url} 프로세스가 떠 있지 않다(rc={proc.returncode}) — 로그 끝:\n{log.read_text()[-3000:]}")
            try:
                if c.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
    pytest.fail(f"{url} 가 {timeout_s:.0f}초 안에 뜨지 않았다 — 로그 끝:\n{log.read_text()[-3000:]}")


def _listener_pid(port: int):
    """그 포트에 리슨하는 PID(없거나 ss 가 없으면 None) — 떠 있는 9009 를 건드리지 않았는지 보는 데만 쓴다."""
    try:
        out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    import re
    m = re.search(r"pid=(\d+)", out)
    return int(m.group(1)) if m else None


def _stop(proc: subprocess.Popen) -> None:
    """이 시험이 띄운 PID 하나만 내린다."""
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("card-review-e2e")
    (tmp / "mcp.json").write_text("{}", encoding="utf-8")
    base_env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    llm_port, agent_port = _free_port(), _free_port()
    llm_log, agent_log = tmp / "fake-llm.log", tmp / "agent.log"
    procs = []
    pid9009 = _listener_pid(9009)
    try:
        with open(llm_log, "w") as out:
            procs.append(subprocess.Popen(
                [PY, str(ROOT / "tests" / "fakes" / "llm_server.py"), "--port", str(llm_port), "--ctx", str(WINDOW),
                 "--model", "fake-glm", "--script", "oracle"],
                cwd=str(tmp), env=base_env, stdout=out, stderr=subprocess.STDOUT))
        llm = f"http://127.0.0.1:{llm_port}"
        _wait(f"{llm}/health", procs[0], llm_log)
        agent_env = {**base_env, "VLLM_BASE_URL": f"{llm}/v1", "VLLM_MODEL": "fake-glm", "VLLM_API_KEY": "fake",
                     "MCP_CONFIG": str(tmp / "mcp.json"), "MCP_SERVERS": "", "DELIB_JOB_DIR": str(tmp / "jobs"),
                     "ARTIFACT_DIR": str(tmp / "artifacts")}
        with open(agent_log, "w") as out:
            procs.append(subprocess.Popen(
                [PY, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(agent_port),
                 "--log-level", "warning"],
                cwd=str(ROOT), env=agent_env, stdout=out, stderr=subprocess.STDOUT))
        agent = f"http://127.0.0.1:{agent_port}"
        _wait(f"{agent}/health", procs[1], agent_log)
        yield {"llm": llm, "agent": agent, "agent_log": agent_log, "ports": (llm_port, agent_port),
               "pids": [x.pid for x in procs], "pid9009": pid9009}
    finally:
        for p in reversed(procs):
            _stop(p)


def _client():
    return httpx.Client(timeout=120, trust_env=False)


def _run(c, agent: str, body: dict) -> list:
    frames = []
    with c.stream("POST", f"{agent}/card-review/run", json=body) as r:
        assert r.status_code == 200, r.read()
        event = None
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                frames.append((event, json.loads(line[6:])))
    return frames


def _verdict(n=3):
    cards = [c for c in PACK if c["checkable"]][:n]
    return _req("verdict", cards, _digest(PACK, [c["alias"] for c in cards]), unit=_unit(0), variant="fwd")


def test_limits_는_가짜_LLM_서버의_창을_서버_출처로_읽는다(stack):
    with _client() as c:
        lim = c.get(f"{stack['agent']}/card-review/limits").json()
    assert (lim["ctx_tokens"], lim["ctx_source"], lim["ctx_retrying"], lim["model"]) == (WINDOW, "server", False,
                                                                                         "fake-glm")
    assert all(lim["runnable"].values()) and "ctx_too_small" not in lim
    assert lim["verdict"]["k"] == 8 and lim["gate"] == {"limit": 8, "limit_reason": "normal", "active": 0}


def test_run_이_실제_LLM_호출을_지나_끝까지_돈다(stack):
    with _client() as c:
        c.post(f"{stack['llm']}/_script", json={"script": ["oracle"]})
        lim = c.get(f"{stack['agent']}/card-review/limits").json()
        body = {**_verdict(3), "expect": {"prompt_rev": lim["prompt_rev"]["verdict"], "limits_rev": lim["limits_rev"]},
                "groups": ["g"], "user_email": "u@x.com", "user_pat": "", "entitlements": ["feat:deliberation"]}
        frames = _run(c, stack["agent"], body)
        stats = c.get(f"{stack['llm']}/_stats").json()
        h = c.get(f"{stack['agent']}/health").json()
    names = [e for e, _ in frames if e != "ping"]
    assert names == ["status", "result", "done"], [f[1].get("message") for f in frames]
    res = frames[-2][1]
    q, m = res["quality"], res["meta"]
    assert (q["expected"], q["ok"], q["missing"], q["truncated"], q["attempts"]) == (3, 3, 0, False, 1)
    assert (m["ctx_tokens"], m["ctx_source"], m["model"], m["finish_reason"]) == (WINDOW, "server", "fake-glm", "stop")
    # 사용량은 가짜 서버의 usage 가 ChatOpenAI 를 지나 온 것이다(엔진이 0 으로 지어내지 않는다).
    assert m["usage"]["tokens_in"] > 0 and m["usage"]["llm_calls"] == 1
    assert stats["calls"] == 1 and stats["behaviors"] == ["oracle"]
    assert h["capabilities"] == ["card_review", "drain"] and h["review_active"] == 0 and h["delib_active"] == 0
    cr = h["card_review"]
    assert (cr["timeout_s"], cr["tries"], cr["request_worst_s"]) == (1800, 2, 10_884) and cr["totals"]["ok"] >= 1


def test_보충과_HTTP_오류가_실제_전송으로_갈린다(stack):
    with _client() as c:
        c.post(f"{stack['llm']}/_script", json={"script": ["skip_middle", "oracle"]})
        res = _run(c, stack["agent"], _verdict(6))[-2][1]
        assert res["quality"]["attempts"] == 2 and res["quality"]["missing"] == 0
        c.post(f"{stack['llm']}/_script", json={"script": ["http_400"]})
        err = _run(c, stack["agent"], _verdict(1))[-2][1]
        assert (err["code"], err["status"], err["infra"]) == ("llm_rejected", 400, False)
        c.post(f"{stack['llm']}/_script", json={"script": ["http_503"]})
        err = _run(c, stack["agent"], _verdict(1))[-2][1]
        assert (err["code"], err["status"], err["infra"]) == ("llm_error", 503, True)
        # openai SDK 재시도(DELIB_LLM_MAX_RETRIES 1)가 실제로 한 번 더 불렀다.
        assert c.get(f"{stack['llm']}/_stats").json()["calls"] == 2


def test_떠_있는_9009_를_건드리지_않았다(stack):
    assert 9009 not in stack["ports"]
    assert _listener_pid(9009) == stack["pid9009"], "하네스가 떠 있는 에이전트 서버(9009)를 바꿨다"
    assert all(_listener_pid(p) in stack["pids"] for p in stack["ports"])
