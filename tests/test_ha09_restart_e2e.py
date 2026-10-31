# 재기동 넘기기(HA-09 S0) — 진짜 에이전트 서버를 빈 포트에 띄워 MCP 심의 잡을 돌리다 3라운드 한가운데서 KILL · TERM 으로 내리고 다시 띄워, 원장이 끊기기 전 진행을 싣고 deliberate_continue 가 끊긴 라운드부터 잇는지 본다
#
# 왜 생겼나. 원장은 잡을 띄울 때와 끝날 때만 썼다. 재기동(TERM)에서는 uvicorn 0.49 가 정상 종료를 마친 뒤 같은 신호를 다시 올려
# 프로세스가 그 자리에서 끝나므로 잡 태스크의 finally(원장 쓰기)가 돌지 않았고, 열린 SSE 가 하나라도 있으면 lifespan 종료에 닿기도
# 전에 start.sh 의 kill -9 를 맞았다. 그래서 끊긴 잡의 원장은 늘 띄울 때 모양(stage start · 턴 0)이었고, 끝난 라운드 뒤부터 잇는 길(R3)이
# 실제 재기동에서는 한 번도 걸리지 않고 처음부터 돌았다. 시험의 가짜(_crash)는 메모리 상태를 원장에 쓴 뒤 죽은 꼴이라 이것을 못 봤다
# (HWAXPortal docs/request-1027/design-ha09-ha20.md A2.3 · A5.1).
#
# 여기서는 가짜가 엔진 바깥에만 있다 — LLM(tests/fakes/llm_server.py)과 게이트웨이(tests/fakes/mcp_gateway.py)는 따로 뜬 가짜 서버이고,
# 에이전트 서버는 리포의 app:app 을 start.sh 와 같은 uvicorn 인자로 띄운다. 오라클은 둘이다 — 죽기 직전에 진행 조회로 읽은 메모리 상태
# (원장이 그것과 같아야 한다)와, 같은 화두를 끊지 않고 끝까지 돈 실행(끊긴 잡의 끝난 라운드 + 이어 돈 잡의 라운드가 그 칸과 같아야 한다).
# 모든 프로세스는 커널이 고른 빈 포트에 뜨고 원장 · 산출물은 임시 경로다. 끝나면 이 시험이 띄운 PID 만 내린다(pkill 을 쓰지 않는다).
#
#   실행:  .venv/bin/python -m pytest tests/test_ha09_restart_e2e.py -q
import json
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import httpx  # noqa: E402
import pytest  # noqa: E402

PY = sys.executable
ME = "me@example.com"
SEATS = [{"key": "mech-a", "role": "기구 구조 전문가 — 보스 · 리브"}, {"key": "rel-b", "role": "신뢰성 전문가 — 낙하"},
         {"key": "pcb-c", "role": "보드 전문가 — 휨"}]
Q = "보스 간극 축소가 낙하에 미치는 영향"
ROUNDS = 4
STEP_S = 1.0                     # 1 · 2라운드 좌석 호출 한 번 — 진행 조회가 2라운드를 놓치지 않을 만큼
HOLD_S = 6.0                     # 3라운드 좌석 호출 한 번 — 이 동안 이벤트가 없다. 그 틈에 읽고 내린다(내려가는 데 2초 남짓)
FAST_S = 0.05
START = {"question": Q, "job": "default", "rounds": ROUNDS, "personas": SEATS, "save_report": False,
         "advanced": {"voc": "off", "free_tools": 0, "persona_knowledge": 0, "rescreen": 0}}


def _start_sh() -> tuple[list, float]:
    """start.sh 가 uvicorn 에 주는 종료 손잡이와 TERM 뒤 KILL 까지의 기본 유예 — 시험은 start.sh 와 같은 인자로 띄운다(그 판에 손잡이가
    없으면 없이 — 고치기 전 판에서 이 시험이 무엇을 보는지가 그대로 결함이다)."""
    src = (ROOT / "start.sh").read_text(encoding="utf-8")
    grace = float(re.search(r'AGENT_STOP_GRACE_S:-([0-9.]+)', src).group(1))
    m = re.search(r'AGENT_GRACEFUL_S:-([0-9]+)', src)
    if m and "--timeout-graceful-shutdown" in src:
        return ["--timeout-graceful-shutdown", m.group(1)], grace
    return [], grace


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port != 9009
    return port


def _listener_pid(port: int):
    try:
        out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"pid=(\d+)", out)
    return int(m.group(1)) if m else None


def _wait_up(url: str, proc: subprocess.Popen, log: Path, timeout_s: float = 90.0) -> None:
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


def _stop(proc: subprocess.Popen) -> None:
    """이 시험이 띄운 PID 하나만 내린다."""
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


class _Stack:
    """가짜 LLM · 가짜 게이트웨이(시험 내내 하나) + 에이전트 서버(시험이 띄우고 내린다)."""

    def __init__(self, tmp: Path):
        self.tmp, self.procs, self.agents = tmp, [], []
        self.base_env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                         "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        self.pid9009 = _listener_pid(9009)
        lp, gp = _free_port(), _free_port()
        self.llm = f"http://127.0.0.1:{lp}"
        self._spawn([PY, str(ROOT / "tests" / "fakes" / "llm_server.py"), "--port", str(lp), "--ctx", "262144",
                     "--model", "fake-glm"], tmp / "fake-llm.log", f"{self.llm}/health")
        gw = self._spawn([PY, str(ROOT / "tests" / "fakes" / "mcp_gateway.py"), "--port", str(gp)], tmp / "fake-gw.log")
        self._wait_port(gp, gw, tmp / "fake-gw.log")
        (tmp / "mcp.json").write_text(json.dumps({"gateway": {"url": f"http://127.0.0.1:{gp}/mcp",
                                                              "transport": "streamable_http"}}), encoding="utf-8")
        self.ports = [lp, gp]
        self.uvicorn_extra, self.stop_grace = _start_sh()

    def _spawn(self, cmd, log: Path, health: str | None = None, env: dict | None = None, cwd: Path | None = None):
        with open(log, "a") as out:
            p = subprocess.Popen(cmd, cwd=str(cwd or self.tmp), env=env or self.base_env, stdout=out, stderr=subprocess.STDOUT)
        self.procs.append(p)
        if health:
            _wait_up(health, p, log)
        return p

    def _wait_port(self, port, proc, log, timeout_s=60.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            if proc.poll() is not None:
                pytest.fail(f"가짜 게이트웨이가 떠 있지 않다 — {log.read_text()[-2000:]}")
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return
            time.sleep(0.2)
        pytest.fail("가짜 게이트웨이가 뜨지 않았다")

    def agent(self, jobs: Path, name: str):
        """에이전트 서버 하나 — start.sh 와 같은 uvicorn 인자. (주소, 프로세스, 로그)."""
        port = _free_port()
        env = {**self.base_env, "VLLM_BASE_URL": f"{self.llm}/v1", "VLLM_MODEL": "fake-glm", "VLLM_API_KEY": "fake",
               "MCP_CONFIG": str(self.tmp / "mcp.json"), "MCP_SERVERS": "", "DELIB_JOB_DIR": str(jobs),
               "ARTIFACT_DIR": str(self.tmp / "artifacts")}
        log = self.tmp / f"agent-{name}.log"
        url = f"http://127.0.0.1:{port}"
        p = self._spawn([PY, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
                         *self.uvicorn_extra], log, f"{url}/health", env=env, cwd=ROOT)
        self.ports.append(port)
        self.agents.append(p)
        return url, p, log

    def llm_delay(self, s: float) -> None:
        with httpx.Client(timeout=10, trust_env=False) as c:
            assert c.post(f"{self.llm}/_script", json={"script": [{"behavior": "oracle", "delay": s}]}).status_code == 200

    def close(self):
        for p in reversed(self.procs):
            _stop(p)


@pytest.fixture
def stack(tmp_path):
    s = _Stack(tmp_path)
    try:
        yield s
    finally:
        s.close()
        assert 9009 not in s.ports and _listener_pid(9009) == s.pid9009, "하네스가 떠 있는 에이전트 서버(9009)를 바꿨다"


def _mcp(base: str, tool: str, args: dict, user: str = ME) -> dict:
    """게이트웨이가 하는 것처럼 /mcp 에 tools/call 한 번(신원 헤더 x-hwax-user). 도구 오류는 실패로 낸다."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    with httpx.Client(timeout=60, trust_env=False) as c:
        r = c.post(f"{base}/mcp/", json=body, headers={"Accept": "application/json, text/event-stream", "x-hwax-user": user})
    assert r.status_code == 200, r.text[:500]
    msg = None
    for line in r.text.splitlines():
        if line.startswith("data: "):
            msg = json.loads(line[6:])
    msg = msg or r.json()
    res = msg.get("result") or {}
    assert not res.get("isError"), f"{tool} 도구 오류 — {json.dumps(res.get('content'), ensure_ascii=False)[:800]}"
    sc = res.get("structuredContent")
    return sc.get("result", sc) if isinstance(sc, dict) else json.loads(res["content"][0]["text"])


def _wait(base: str, jid: str, *, stage: str | None = None, timeout_s: float = 90.0) -> dict:
    """그 단계에 들거나(stage) 끝날 때까지 진행 조회를 되풀이한다. 그 라운드를 건너뛰어 뒤 라운드에 있으면 곧바로 실패다(시험 전제가 깨졌다)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        st = _mcp(base, "deliberate_status", {"job_id": jid})
        if stage and st.get("stage") == stage:
            return st
        if stage and re.fullmatch(r"r\d+", str(st.get("stage"))) and int(st["stage"][1:]) > int(stage[1:]):
            pytest.fail(f"{stage} 를 진행 조회가 놓쳤다(이미 {st['stage']}) — 가짜 LLM 이 너무 빠르다(STEP_S)")
        if st.get("status") not in ("running", "queued"):
            assert not stage, f"{stage} 에 들기 전에 끝났다 — {st.get('status')} · {st.get('error')}"
            return st
        time.sleep(0.05)
    pytest.fail(f"{jid} 가 {timeout_s:.0f}초 안에 {stage or '끝'}에 닿지 않았다")


def _turns(base: str, jid: str) -> list:
    tr = _mcp(base, "deliberate_transcript", {"job_id": jid, "limit": 200, "full": True})
    assert tr.get("next_offset") is None
    return tr["turns"]


def _grid(turns: list) -> set:
    return {(t.get("display_round"), t.get("persona")) for t in turns if t.get("persona")}


def _ledger(jobs: Path, jid: str) -> dict:
    return json.loads((jobs / f"{jid}.json").read_text(encoding="utf-8"))


def _mid_round(stack: _Stack, base: str) -> tuple[str, dict, list]:
    """잡을 띄워 3라운드에 든 그 자리에서 진행 조회 · 전사를 읽는다. 2라운드에 들면 가짜 LLM 을 느리게 갈아 둔다 — 2라운드 좌석 호출은 이미
    나갔으니 그대로 STEP_S 에 돌아오고, 그 뒤에 나가는 3라운드 호출은 HOLD_S 를 기다린다(그 사이 이벤트가 없다 — 읽은 것이 죽을 때의 메모리다)."""
    stack.llm_delay(STEP_S)
    jid = _mcp(base, "deliberate_start", START)["job_id"]
    _wait(base, jid, stage="r2")
    stack.llm_delay(HOLD_S)
    st = _wait(base, jid, stage="r3")
    turns = _turns(base, jid)
    assert len(turns) == 2 * len(SEATS) and _grid(turns) == {(r, s["key"]) for r in (1, 2) for s in SEATS}, _grid(turns)
    return jid, st, turns


def _oracle(stack: _Stack, base: str) -> list:
    """같은 화두를 끊지 않고 끝까지 — 그 전사의 (라운드, 좌석) 칸이 끊긴 잡 + 이어 돈 잡의 칸과 같아야 한다."""
    stack.llm_delay(FAST_S)
    jid = _mcp(base, "deliberate_start", START)["job_id"]
    st = _wait(base, jid)
    assert st["status"] == "done", st
    return _turns(base, jid)


def _check_folded(base: str, jid: str, pre_turns: list) -> None:
    """재기동 뒤 — 원장이 죽기 직전의 진행(진행 조회로 읽은 메모리)을 싣고, 끝난 라운드 수를 이어하기 재료로 낸다."""
    st = _mcp(base, "deliberate_status", {"job_id": jid})
    assert st["status"] == "interrupted", st
    assert (st["stage"], st["turn_count"], st["total_rounds"]) == ("r3", 2 * len(SEATS), ROUNDS), \
        f"원장이 끊기기 전 진행을 싣지 않았다 — 띄울 때 모양이면 결함 그대로다: {(st['stage'], st['turn_count'], st['total_rounds'])}"
    assert st["seats"] == [s["key"] for s in SEATS]
    assert st["resume"] == {"rounds_done": 2, "total_rounds": ROUNDS, "rounds_left": 2, "args": True, "resumed_by": None,
                            "checkpoint": None}, st["resume"]
    assert _turns(base, jid) == pre_turns, "접은 원장의 전사가 죽기 직전의 메모리(진행 조회로 읽은 것)와 다르다"


def _continue_and_finish(stack: _Stack, base: str, jobs: Path, jid: str, pre_turns: list, oracle: list) -> None:
    """이어하기 한 번 — 끊긴 3라운드부터 돌아, 끊긴 잡의 끝난 라운드와 합쳐 끊지 않은 실행과 같은 칸을 채운다."""
    stack.llm_delay(FAST_S)
    out = _mcp(base, "deliberate_continue", {"previous_job_id": jid})
    assert out["resumed"]["rounds_done"] == 2 and out["resumed"]["rounds_start_at"] == 3 and out["resumed"]["args"] is True, out
    assert "끝난 2라운드 뒤부터" in out["note"], out["note"]
    new = out["job_id"]
    done = _wait(base, new)
    assert done["status"] == "done", done
    new_turns = _turns(base, new)
    assert {t["display_round"] for t in new_turns} == {3, 4}, "끊긴 라운드부터가 아니라 처음부터 돌았다"
    assert _grid(pre_turns) | _grid(new_turns) == _grid(oracle), "끊긴 잡 + 이어 돈 잡이 끊지 않은 실행의 칸을 채우지 않았다"
    assert not _grid(pre_turns) & _grid(new_turns), "끝난 라운드의 좌석을 다시 불렀다"
    cs = _mcp(base, "deliberate_result", {"job_id": new})["applied_opts"]["continue_summary"]
    assert "끝난 2라운드까지 좌석 3석" in cs, cs
    assert _ledger(jobs, jid)["resumed_by"] == new
    assert not (jobs / "journal" / f"{jid}.jsonl").exists() and not (jobs / "args" / f"{jid}.json").exists(), \
        "이어 돌린 잡의 일지 · 시작 인자 파일(근거 본문)이 남았다"


def test_KILL_로_3라운드_한가운데서_죽어도_다시_뜨면_원장에_끝난_라운드가_있고_끊긴_라운드부터_잇는다(stack):
    jobs = stack.tmp / "jobs"
    base, proc, log = stack.agent(jobs, "a")
    oracle = _oracle(stack, base)
    jid, _st, pre = _mid_round(stack, base)
    proc.kill()                                   # SIGKILL — 메모리 고갈 · kill -9 와 같은 꼴(아무 정리도 돌지 않는다)
    assert proc.wait(timeout=10) == -signal.SIGKILL
    disk = _ledger(jobs, jid)
    assert disk["status"] == "running" and disk["stage"] == "start", "KILL 인데 원장이 바뀌었다 — 시험 전제가 틀렸다"
    base2, _p2, _log2 = stack.agent(jobs, "b")
    _check_folded(base2, jid, pre)
    jr = jobs / "journal" / f"{jid}.jsonl"                 # KILL 에 남은 것은 일지뿐이다 — 이어 돌릴 때까지 둔다
    assert stat.S_IMODE(jr.stat().st_mode) == 0o600 and stat.S_IMODE(jr.parent.stat().st_mode) == 0o700
    _continue_and_finish(stack, base2, jobs, jid, pre, oracle)


def test_TERM_은_열린_SSE_가_있어도_유예_안에_내려가며_원장에_진행을_남기고_다시_뜨면_끊긴_라운드부터_잇는다(stack):
    jobs = stack.tmp / "jobs"
    base, proc, log = stack.agent(jobs, "a")
    oracle = _oracle(stack, base)
    jid, _st, pre = _mid_round(stack, base)
    # 열린 SSE — 포털 챗의 웹 심의 구독 하나를 읽는 채로 둔다(종전엔 이것이 닫히기를 상한 없이 기다리다 start.sh 의 kill -9 를 맞았다).
    # ⚠ 줄 읽개(iter_lines)를 붙잡아 둔다 — 버리면 httpx 가 그 응답의 연결을 닫아 '열린 SSE' 가 없어진다(시험 전제가 조용히 사라진다).
    got, ended = [], []

    def _read(it):
        try:
            for ln in it:
                got.append(ln)
        except httpx.HTTPError as exc:
            got.append(f"끊김 {type(exc).__name__}")
        ended.append(time.monotonic())

    with httpx.Client(timeout=httpx.Timeout(30, read=None), trust_env=False) as sse:
        with sse.stream("POST", f"{base}/chat", json={"message": f"/심의 {Q}", "delib_opts": {
                "personas": SEATS, "voc": "off", "free_tools": 0, "persona_knowledge": 0, "rescreen": 0, "save_report": 0}}) as r:
            assert r.status_code == 200
            it = r.iter_lines()
            assert next(it).startswith("event: ")
            reader = threading.Thread(target=_read, args=(it,), daemon=True)
            reader.start()
            time.sleep(0.3)
            assert not ended, f"웹 심의 구독이 TERM 전에 끝났다 — 시험 전제(열린 연결)가 없다: {got[-6:]}"
            t0 = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            try:
                rc = proc.wait(timeout=stack.stop_grace)
            except subprocess.TimeoutExpired:
                pytest.fail(f"TERM 뒤 {stack.stop_grace:.0f}초(start.sh 의 KILL 유예) 안에 내려가지 않았다 — start.sh 라면 여기서 kill -9"
                            f" 다(열린 SSE 를 기다린다). 로그 끝:\n{log.read_text()[-1500:]}")
            took = time.monotonic() - t0
            reader.join(timeout=5)
    assert ended and ended[0] - t0 < stack.stop_grace, "구독이 서버가 내려간 뒤에도 열려 있다"
    assert rc == -signal.SIGTERM, rc                     # uvicorn 은 정상 종료 뒤 받은 신호를 다시 올린다 — 그 자리에서 끝난다
    assert "timeout graceful shutdown exceeded" in log.read_text(), "열린 SSE 를 상한에서 끊지 않았다 — 시험 전제(열린 연결)가 없었다"
    disk = _ledger(jobs, jid)
    assert (disk["status"], disk["stage"], len(disk["turns"]), disk["total_rounds"]) == ("interrupted", "r3", 2 * len(SEATS), ROUNDS), \
        f"내려가며 원장에 진행을 남기지 않았다({took:.1f}초): {(disk['status'], disk['stage'], len(disk['turns']))}"
    assert disk["error"].startswith("서버 재기동으로 중단됨") and (jobs / "args" / f"{jid}.json").exists()
    base2, _p2, _log2 = stack.agent(jobs, "b")
    _check_folded(base2, jid, pre)
    _continue_and_finish(stack, base2, jobs, jid, pre, oracle)
