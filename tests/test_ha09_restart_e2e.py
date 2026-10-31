# 재기동 넘기기(HA-09 S0 · S1 · S2 · S3) — 진짜 에이전트 서버를 빈 포트에 띄워 MCP 심의 잡을 돌리다 3라운드 한가운데 · 보고서 단계에서 KILL · TERM 으로 내리고 다시 띄워, 원장이 끊기기 전 진행을 싣고 deliberate_continue 가 같은 잡을 끊긴 자리부터 잇는지(이미 말한 좌석 · 저장한 보고서는 다시 하지 않는지) 본다
#
# 왜 생겼나. 원장은 잡을 띄울 때와 끝날 때만 썼다. 재기동(TERM)에서는 uvicorn 0.49 가 정상 종료를 마친 뒤 같은 신호를 다시 올려
# 프로세스가 그 자리에서 끝나므로 잡 태스크의 finally(원장 쓰기)가 돌지 않았고, 열린 SSE 가 하나라도 있으면 그 연결이 닫힐 때까지(상한
# 없음) lifespan 종료에 닿지 않은 채 옛 프로세스가 남았다 — 그동안 start.sh 는 리스너만 보고 새 프로세스를 같은 원장 자리에 띄웠다(반대 검토
# 10-31 이 바로잡았다 — 설계서 A2.3 은 '2초 뒤 kill -9' 로 적었다). 그래서 끊긴 잡의 원장은 늘 띄울 때 모양(stage start · 턴 0)이었고, 끝난
# 라운드 뒤부터 잇는 길(R3)이 실제 재기동에서는 한 번도 걸리지 않고 처음부터 돌았다. 시험의 가짜(_crash)는 메모리 상태를 원장에 쓴 뒤
# 죽은 꼴이라 이것을 못 봤다(HWAXPortal docs/request-1027/design-ha09-ha20.md A2.3 · A5.1).
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

    def __init__(self, tmp: Path, ra: bool = False):
        self.tmp, self.procs, self.agents = tmp, [], []
        # ra — 가짜 게이트웨이에 보고서 · 위키 도구를 싣고 받은 호출을 calls 에 적는다. hold 파일({도구: 초})의 도구는 적은 뒤 붙잡힌다(HA-09 S3)
        self.calls, self.hold = tmp / "gw-calls.jsonl", tmp / "gw-hold.json"
        self.base_env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                         "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        self.pid9009 = _listener_pid(9009)
        lp, gp = _free_port(), _free_port()
        self.llm = f"http://127.0.0.1:{lp}"
        self._spawn([PY, str(ROOT / "tests" / "fakes" / "llm_server.py"), "--port", str(lp), "--ctx", "262144",
                     "--model", "fake-glm"], tmp / "fake-llm.log", f"{self.llm}/health")
        gw = self._spawn([PY, str(ROOT / "tests" / "fakes" / "mcp_gateway.py"), "--port", str(gp),
                          *(["--record", str(self.calls), "--hold", str(self.hold)] if ra else [])], tmp / "fake-gw.log")
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
        self.llm_script([s])

    def llm_script(self, delays: list) -> None:
        """가짜 LLM 의 대본 — 호출 차례마다 기다림(초). 다 떨어지면 마지막 것을 되풀이한다. 호출 수(calls)를 0 으로 돌린다(최근 요청 기록
        systems · humans 는 요청마다 굴러가는 최근 20건이라 그대로다)."""
        with httpx.Client(timeout=10, trust_env=False) as c:
            assert c.post(f"{self.llm}/_script", json={"script": [{"behavior": "oracle", "delay": s} for s in delays]}).status_code == 200

    def llm_stats(self) -> dict:
        with httpx.Client(timeout=10, trust_env=False) as c:
            return c.get(f"{self.llm}/_stats").json()

    def close(self):
        for p in reversed(self.procs):
            _stop(p)


def _stack_of(tmp_path, **kw):
    s = _Stack(tmp_path, **kw)
    try:
        yield s
    finally:
        s.close()
        assert 9009 not in s.ports and _listener_pid(9009) == s.pid9009, "하네스가 떠 있는 에이전트 서버(9009)를 바꿨다"


@pytest.fixture
def stack(tmp_path):
    yield from _stack_of(tmp_path)


@pytest.fixture
def stack_ra(tmp_path):
    """보고서 · 위키 도구가 실린 가짜 게이트웨이(받은 호출을 적고, hold 파일의 도구는 붙잡는다)."""
    yield from _stack_of(tmp_path, ra=True)


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
    sr = st["resume"].pop("seat_resume")
    assert st["resume"] == {"rounds_done": 2, "total_rounds": ROUNDS, "rounds_left": 2, "args": True, "resumed_by": None,
                            "checkpoint": None}, st["resume"]
    # 재개 자료(HA-09 S1) — 3라운드의 좌석 호출은 붙잡혀 아무도 말하지 않았다. 이으면 같은 잡이 3라운드부터 세 좌석을 부른다
    assert (sr["ok"], sr["sk"], sr["round"], sr["spoken"], sr["todo"]) == (True, "main", 3, [], [s["key"] for s in SEATS]), sr
    assert "같은 잡에서 이어 돈다" in st["error"], st["error"]
    assert _turns(base, jid) == pre_turns, "접은 원장의 전사가 죽기 직전의 메모리(진행 조회로 읽은 것)와 다르다"


def _continue_and_finish(stack: _Stack, base: str, jobs: Path, jid: str, pre_turns: list, oracle: list) -> None:
    """이어하기 한 번 — 같은 잡(제자리 — HA-09 S1 · 사용자 결정 10-31 A-2 (가))이 끊긴 3라운드부터 돌아, 끊기기 전에 끝난 라운드와 합쳐 끊지 않은
    실행과 같은 칸을 좌석마다 한 번 채운다. 끝난 라운드의 턴은 그대로다(다시 부르지 않았다)."""
    stack.llm_delay(FAST_S)
    out = _mcp(base, "deliberate_continue", {"previous_job_id": jid})
    r = out["resumed"]
    assert out["job_id"] == jid and r["mode"] == "in_place" and (r["round"], r["rounds_done"], r["attempt"]) == (3, 2, 1), out
    assert "같은 잡(같은 job_id)에서 잇는다" in out["note"] and "3라운드부터" in out["note"], out["note"]
    done = _wait(base, jid)
    assert done["status"] == "done" and len(done["attempts"]) == 1 and done["attempts"][0]["round"] == 3, done
    turns = _turns(base, jid)
    assert _grid(turns) == _grid(oracle) and len(turns) == len(oracle), "끊긴 잡이 이어 돌아 끊지 않은 실행의 칸을 좌석마다 한 번 채우지 않았다"
    assert [t for t in turns if t["display_round"] <= 2] == pre_turns, "끝난 라운드의 턴이 바뀌었다(좌석을 다시 불렀다)"
    led = _ledger(jobs, jid)
    assert not led.get("resumed_by") and "continue_summary" not in led["opts"], "요약 길(새 잡)로 이었다"
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
    # 열린 SSE — 포털 챗의 웹 심의 구독 하나를 읽는 채로 둔다(종전엔 이것이 닫히기를 상한 없이 기다려 lifespan 종료에 닿지 않았다).
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
                pytest.fail(f"TERM 뒤 {stack.stop_grace:.0f}초(start.sh 의 KILL 유예) 안에 내려가지 않았다 — start.sh 는 여기서 옛 PID 를"
                            f" kill -9 한다(열린 SSE 를 기다린다). 로그 끝:\n{log.read_text()[-1500:]}")
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


def _wait_turns(base: str, jid: str, n: int, timeout_s: float = 60.0) -> dict:
    """원장의 턴이 n 개가 될 때까지 진행 조회를 되풀이한다(넘기거나 끝나면 곧바로 실패 — 시험 전제가 깨졌다)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        st = _mcp(base, "deliberate_status", {"job_id": jid})
        if st.get("turn_count") == n:
            return st
        assert st.get("status") == "running" and (st.get("turn_count") or 0) < n, f"턴 {n}개 자리를 놓쳤다 — {st.get('status')} · {st.get('turn_count')}"
        time.sleep(0.05)
    pytest.fail(f"{jid} 의 턴이 {timeout_s:.0f}초 안에 {n}개가 되지 않았다")


def test_KILL_로_3라운드에서_한_석이_말한_뒤_죽어도_이으면_그_좌석은_다시_부르지_않고_나머지_두_석만_부른다(stack):
    """좌석 단위(HA-09 S2) — 3라운드에서 한 석이 말하고 두 석의 호출이 붙잡힌 채 KILL. 다시 뜨면 재개 요약이 말한 한 석과 부를 두 석을 싣고,
    이으면 같은 잡이 그 한 석을 다시 부르지 않고(가짜 LLM 이 받은 3라운드 좌석 호출이 정확히 나머지 두 석이다) 그 턴은 끊기기 전과 같게 다시
    낸다 — 전사는 끊지 않은 실행의 칸을 좌석마다 한 번 채운다."""
    jobs = stack.tmp / "jobs"
    base, proc, _log = stack.agent(jobs, "a")
    oracle = _oracle(stack, base)
    stack.llm_delay(STEP_S)
    jid = _mcp(base, "deliberate_start", START)["job_id"]
    _wait(base, jid, stage="r2")
    time.sleep(0.3)                  # 2라운드 좌석 호출은 이미 가짜 LLM 에 닿아 STEP_S 를 받았다 — 다음 호출(3라운드)부터 새 대본이다
    stack.llm_script([FAST_S, HOLD_S])                  # 3라운드의 첫 호출만 곧바로, 나머지는 붙잡힌다
    _wait_turns(base, jid, 2 * len(SEATS) + 1)
    pre = _turns(base, jid)
    spoke = [t["persona"] for t in pre if t["display_round"] == 3]
    assert len(spoke) == 1 and _grid(pre) == {(r, s["key"]) for r in (1, 2) for s in SEATS} | {(3, spoke[0])}, _grid(pre)
    proc.kill()
    assert proc.wait(timeout=10) == -signal.SIGKILL
    base2, _p2, _log2 = stack.agent(jobs, "b")
    st = _mcp(base2, "deliberate_status", {"job_id": jid})
    sr = st["resume"]["seat_resume"]
    todo = [s["key"] for s in SEATS if s["key"] != spoke[0]]
    assert (st["status"], sr["ok"], sr["round"], sr["spoken"], sr["todo"]) == ("interrupted", True, 3, spoke, todo), sr
    assert _turns(base2, jid) == pre, "접은 원장의 전사가 죽기 직전과 다르다"
    stack.llm_delay(FAST_S)
    out = _mcp(base2, "deliberate_continue", {"previous_job_id": jid})
    r = out["resumed"]
    assert out["job_id"] == jid and (r["mode"], r["round"], r["spoken"], r["todo"]) == ("in_place", 3, spoke, todo), out
    assert "이미 말한 1석은 다시 부르지 않고 남은 2석만 부른다" in out["note"], out["note"]
    done = _wait(base2, jid)
    assert done["status"] == "done" and len(done["attempts"]) == 1, done
    stats = stack.llm_stats()
    assert 0 < stats["calls"] <= 20, stats["calls"]           # 최근 요청 기록(20건) 안에 이은 실행의 호출이 다 있다
    recent = list(zip(stats["systems"], stats["humans"]))[-stats["calls"]:]
    called = sorted(re.search(r"당신은 '([^']+)' 전문가", sy).group(1) for sy, hu in recent if "전문가입니다" in sy and "수용(concede)" in hu)
    assert called == sorted(todo), f"3라운드에서 부른 좌석이 정확히 나머지 두 석이 아니다 — {called}"
    turns = _turns(base2, jid)
    assert _grid(turns) == _grid(oracle) and len(turns) == len(oracle), "끊긴 잡이 이어 돌아 끊지 않은 실행의 칸을 좌석마다 한 번 채우지 않았다"
    assert [t for t in turns if t["display_round"] <= 2] == [t for t in pre if t["display_round"] <= 2], "끝난 라운드의 턴이 바뀌었다"
    assert [t for t in turns if (t["display_round"], t["persona"]) == (3, spoke[0])] == [t for t in pre if t["display_round"] == 3], \
        "끊기기 전에 말한 좌석의 턴이 원 출력과 다르게 다시 났다"
    assert not (jobs / "journal" / f"{jid}.jsonl").exists() and not (jobs / "args" / f"{jid}.json").exists()


def test_내려가는_동안_자리가_나도_줄_선_잡은_띄우지_않고_다시_뜨면_같은_차례로_다시_선다(stack):
    """R2 — 줄 선 잡은 재기동 뒤 같은 차례로 다시 선다. uvicorn 은 TERM 뒤 열린 연결을 AGENT_GRACEFUL_S 까지 기다린 **뒤에야** lifespan 종료(줄
    닫기)를 돈다 — 그 사이 도는 잡이 끝나 자리가 나면 내려가는 프로세스가 줄 선 잡을 띄웠고, 그 잡은 곧 interrupted 로 접혀 다시 서지 못했다
    (HA-09 S0 반대 검토 10-31 — 운영이 웹 구독을 더 기다리려고 상한을 올린 꼴). 신호를 받는 그 자리에서 줄을 닫는다(delib_jobs.hook_exit_signals)."""
    jobs = stack.tmp / "jobs"
    grace = 8
    stack.base_env = {**stack.base_env, "DELIB_JOB_MAX_RUNNING": "1"}
    stack.uvicorn_extra = ["--timeout-graceful-shutdown", str(grace)]
    base, proc, log = stack.agent(jobs, "a")
    stack.llm_delay(STEP_S)
    ja = _mcp(base, "deliberate_start", {**START, "question": Q + " — 가", "rounds": 2})["job_id"]
    rb = _mcp(base, "deliberate_start", {**START, "question": Q + " — 나", "rounds": 2})
    jb = rb["job_id"]
    assert rb["status"] == "queued", rb
    _wait(base, ja, stage="r1")
    got = []

    def _read(it):
        try:
            for ln in it:
                got.append(ln)
        except httpx.HTTPError:
            pass

    with httpx.Client(timeout=httpx.Timeout(30, read=None), trust_env=False) as sse:
        with sse.stream("POST", f"{base}/chat", json={"message": f"/심의 {Q} — 다", "delib_opts": {
                "personas": SEATS, "rounds": 6, "voc": "off", "free_tools": 0, "persona_knowledge": 0, "rescreen": 0,
                "save_report": 0}}) as r:
            assert r.status_code == 200
            it = r.iter_lines()                  # ⚠ 줄 읽개를 붙잡아 둔다 — 버리면 열린 연결이 없어진다(시험 전제)
            assert next(it).startswith("event: ")
            reader = threading.Thread(target=_read, args=(it,), daemon=True)
            reader.start()
            time.sleep(0.3)
            proc.send_signal(signal.SIGTERM)
            stack.llm_delay(FAST_S)               # 내려가는 동안 도는 잡(가)이 끝나 자리가 난다
            t0 = time.monotonic()
            while _ledger(jobs, ja)["status"] == "running" and time.monotonic() - t0 < grace - 1:
                time.sleep(0.1)
            assert _ledger(jobs, ja)["status"] == "done" and proc.poll() is None, \
                "시험 전제 — 정상 종료를 기다리는 동안 도는 잡이 끝나 자리가 났다"
            assert proc.wait(timeout=grace + 15) == -signal.SIGTERM
            reader.join(timeout=5)
    led = _ledger(jobs, jb)
    assert (led["status"], led.get("started_at")) == ("queued", None), \
        f"내려가는 프로세스가 줄 선 잡을 띄웠다 — {(led['status'], led.get('started_at'), led.get('error'))}"
    base2, _p2, _log2 = stack.agent(jobs, "b")
    led = _ledger(jobs, jb)
    assert (led["status"], led.get("requeued")) == ("queued", 1), f"재기동 뒤 같은 차례로 다시 서지 않았다 — {led.get('status')}"
    assert _mcp(base2, "deliberate_status", {"job_id": jb})["status"] == "queued"


# ── 결정문 뒤(HA-09 S3) — 보고서를 저장한 뒤 · 저장하던 중에 끊겼으면 ─────────────────────────────────────────
def _calls(stack: _Stack, tool: str) -> list:
    try:
        rows = [json.loads(x) for x in stack.calls.read_text(encoding="utf-8").splitlines() if x]
    except FileNotFoundError:
        return []
    return [r for r in rows if r["tool"] == tool]


def _wait_call(stack: _Stack, tool: str, n: int = 1, timeout_s: float = 90.0) -> None:
    """가짜 게이트웨이가 그 도구 호출을 n 번 받을 때까지 — 받은 호출은 답하기 전에 적힌다(붙잡힌 호출도 적힌다)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if len(_calls(stack, tool)) >= n:
            return
        time.sleep(0.05)
    pytest.fail(f"{tool} 호출이 {timeout_s:.0f}초 안에 {n}번 오지 않았다")


_POST_START = {**START, "save_report": True, "wiki": True}


def test_KILL_로_보고서를_저장한_뒤_위키를_보내던_중에_죽어도_이으면_다시_저장하지_않고_같은_번호로_맺는다(stack_ra):
    """결정문 뒤 멱등(HA-09 S3 — 설계 A6 S3) — 보고서를 저장한 뒤 · 결과 사건 전(위키 호출이 붙잡힌 채)에 KILL. 다시 뜨면 이어하기가 같은 잡을
    LLM 호출 0 · 보고서 저장 0 으로 맺는다 — 번호는 끊기기 전의 것이고, 위키는 같은 원천 열쇠로 한 번 더 간다(보내던 중이었다 — MX 백서가 앞의
    원천으로 받는다)."""
    stack = stack_ra
    jobs = stack.tmp / "jobs"
    base, proc, _log = stack.agent(jobs, "a")
    stack.hold.write_text(json.dumps({"wiki_submit_source": 120}), encoding="utf-8")
    stack.llm_delay(FAST_S)
    jid = _mcp(base, "deliberate_start", _POST_START)["job_id"]
    _wait_call(stack, "wiki_submit_source")
    assert [c["rid"] for c in _calls(stack, "create_report_draft")] == [77], "시험 전제 — 보고서를 한 번 저장한 뒤 위키에서 붙잡혔다"
    proc.kill()
    assert proc.wait(timeout=10) == -signal.SIGKILL
    stack.hold.unlink()
    base2, _p2, _log2 = stack.agent(jobs, "b")
    st = _mcp(base2, "deliberate_status", {"job_id": jid})
    sr = st["resume"]["seat_resume"]
    assert (st["status"], st["stage"], sr["ok"], sr["round"], sr["todo"]) == ("interrupted", "report", True, ROUNDS + 1, []), st
    stack.llm_delay(FAST_S)                      # 호출 수를 0 으로 — 이은 실행이 LLM 을 부르면 센다
    out = _mcp(base2, "deliberate_continue", {"previous_job_id": jid})
    assert out["job_id"] == jid and out["resumed"]["mode"] == "in_place", out
    done = _wait(base2, jid)
    assert [c["rid"] for c in _calls(stack, "create_report_draft")] == [77], "이미 저장한 보고서를 다시 저장했다"
    assert (done["status"], done["report_id"], len(done["attempts"])) == ("done", 77, 1), done
    assert stack.llm_stats()["calls"] == 0, "끝난 의장 · 요약 · 쉬운 설명을 다시 불렀다"
    assert [c["ref"] for c in _calls(stack, "wiki_submit_source")] == [f"delib:{jid}:default"] * 2, "위키를 다른 열쇠로 보냈다"
    assert "보고서는 끊기기 전에 저장돼(#77) 다시 저장하지 않는다" in out["note"], out["note"]
    led = _ledger(jobs, jid)
    assert "보고서 #77 로 저장됨" in led["decision"] and not [w for w in led.get("warnings") or [] if "끝났는지 알 수 없어" in w]
    assert not (jobs / "journal" / f"{jid}.jsonl").exists() and not (jobs / "args" / f"{jid}.json").exists()


def test_TERM_으로_보고서를_저장하던_중에_내려가면_이을_때_경고_한_번과_함께_새로_저장한다(stack_ra):
    """보고서 저장 호출이 붙잡힌 채 TERM — 내려가며 잡을 접는다(취소는 저장의 예외 그물에 걸리지 않는다 — 저장을 마쳤다는 줄이 없다). 이으면
    RA 가 받았는지 알 길이 없어 경고 한 번과 함께 새로 저장한다(새 번호). 의장 · 요약 · 쉬운 설명은 다시 부르지 않는다."""
    stack = stack_ra
    jobs = stack.tmp / "jobs"
    base, proc, _log = stack.agent(jobs, "a")
    stack.hold.write_text(json.dumps({"create_report_draft": 120}), encoding="utf-8")
    stack.llm_delay(FAST_S)
    jid = _mcp(base, "deliberate_start", _POST_START)["job_id"]
    _wait_call(stack, "create_report_draft")
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=stack.stop_grace) == -signal.SIGTERM
    disk = _ledger(jobs, jid)
    assert (disk["status"], disk["stage"], disk["seat_resume"]["round"]) == ("interrupted", "report", ROUNDS + 1), disk.get("error")
    stack.hold.unlink()
    base2, _p2, _log2 = stack.agent(jobs, "b")
    stack.llm_delay(FAST_S)
    out = _mcp(base2, "deliberate_continue", {"previous_job_id": jid})
    assert out["job_id"] == jid and out["resumed"]["mode"] == "in_place", out
    done = _wait(base2, jid)
    assert stack.llm_stats()["calls"] == 0, "끝난 의장 · 요약 · 쉬운 설명을 다시 불렀다"
    led = _ledger(jobs, jid)
    assert len([w for w in led.get("warnings") or [] if "끝났는지 알 수 없어" in w]) == 1, led.get("warnings")
    assert (done["status"], done["report_id"]) == ("done", 78), done
    assert [c["rid"] for c in _calls(stack, "create_report_draft")] == [77, 78]
    assert "이미 저장됐을 수 있다" in out["note"], out["note"]
    assert "보고서 #78 로 저장됨" in led["decision"] and len(_calls(stack, "wiki_submit_source")) == 1


# ── 다단 심의(HA-09 S4) — 해석 설계 2단 한가운데서 KILL ─────────────────────────────────────────────
_SIM_START = {**START, "job": "sim-plan"}
_SEAT_SWITCH = ("recommend_agents", "get_agent_session", "list_tool_apps", "database_summary")


def _all_calls(stack: _Stack) -> list:
    try:
        return [json.loads(x) for x in stack.calls.read_text(encoding="utf-8").splitlines() if x]
    except FileNotFoundError:
        return []


def _wait_sim2(base: str, jid: str, stage: str, timeout_s: float = 120.0) -> dict:
    """2단(1단 체크포인트 뒤)의 그 단계에 들 때까지 — 1단의 같은 이름 단계는 넘긴다. 2단에서 그 단계를 건너뛰었으면 곧바로 실패다."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        st = _mcp(base, "deliberate_status", {"job_id": jid})
        if st.get("checkpoint") and st.get("stage") == stage:
            return st
        if st.get("checkpoint") and re.fullmatch(r"r\d+", str(st.get("stage"))) and int(st["stage"][1:]) > int(stage[1:]):
            pytest.fail(f"2단 {stage} 를 진행 조회가 놓쳤다(이미 {st['stage']}) — 가짜 LLM 이 너무 빠르다(STEP_S)")
        if st.get("status") not in ("running", "queued"):
            pytest.fail(f"2단 {stage} 에 들기 전에 끝났다 — {st.get('status')} · {st.get('error')}")
        time.sleep(0.05)
    pytest.fail(f"{jid} 가 {timeout_s:.0f}초 안에 2단 {stage} 에 닿지 않았다")


def test_KILL_로_해석_설계_2단_한가운데서_죽어도_이으면_1단_좌석_전환_자산_현황을_다시_하지_않고_2단_안에서_잇는다(stack_ra):
    """다단 심의(HA-09 S4 — 설계 A6 S4) — 해석 설계 2단 3라운드의 좌석 호출이 붙잡힌 채 KILL. 다시 뜨면 재개 요약이 끊긴 단(sim-2)과 라운드를
    싣고, 이으면 같은 잡이 2단 3라운드부터 돈다 — 가짜 LLM 이 받은 호출은 2단 3 · 4라운드 좌석과 2단 의장 · 요약 · 쉬운 설명뿐이고(1단 호출 0),
    가짜 게이트웨이는 좌석 전환(발굴 · 역할 복원)과 사내 자산 현황 호출을 받지 않는다. 전사는 끊지 않은 실행의 칸을 단마다 좌석마다 한 번
    채우고 1단의 턴은 그대로다(단마다 라운드를 1부터 센다 — 앞 단의 턴을 라운드 번호로 빼지 않는다)."""
    stack = stack_ra
    stack.base_env = {**stack.base_env, "DELIB_SIM_SPINE": "0"}          # 2단 고정 CAE 좌석을 둘로(시험 시간 — 좌석 수는 이 시험이 보지 않는다)
    jobs = stack.tmp / "jobs"
    base, proc, _log = stack.agent(jobs, "a")
    stack.llm_delay(FAST_S)
    jo = _mcp(base, "deliberate_start", _SIM_START)["job_id"]
    assert _wait(base, jo, timeout_s=180)["status"] == "done"
    oracle = _turns(base, jo)
    assert {c["tool"] for c in _all_calls(stack)} >= set(_SEAT_SWITCH), "시험 전제 — 끊지 않은 실행이 좌석 전환 · 사내 자산 현황을 불렀다"
    stack.llm_delay(STEP_S)
    jid = _mcp(base, "deliberate_start", _SIM_START)["job_id"]
    _wait_sim2(base, jid, "r2")
    stack.llm_delay(HOLD_S)                        # 2단 2라운드 호출은 이미 나갔다 — 그 뒤 3라운드 호출이 붙잡힌다
    _wait_sim2(base, jid, "r3")
    pre = _turns(base, jid)
    n1 = len(SEATS) * ROUNDS
    proc.kill()
    assert proc.wait(timeout=10) == -signal.SIGKILL
    seen = len(_all_calls(stack))
    base2, _p2, _log2 = stack.agent(jobs, "b")
    st = _mcp(base2, "deliberate_status", {"job_id": jid})
    sr = st["resume"]["seat_resume"]
    seats2 = st["seats"]
    assert len(pre) == n1 + 2 * len(seats2), f"시험 전제 — 1단 전부와 2단 1 · 2라운드의 턴: {len(pre)}"
    assert (st["status"], sr["ok"], sr["sk"], sr["round"], sr["spoken"], sr["todo"]) == ("interrupted", True, "sim-2", 3, [], seats2), st
    assert (st["resume"]["rounds_done"], st["resume"]["checkpoint"]) == (0, "sim-1"), st["resume"]
    assert _turns(base2, jid) == pre, "접은 원장의 전사가 죽기 직전과 다르다"
    stack.llm_delay(FAST_S)                        # 호출 수를 0 으로 — 이은 실행의 LLM 호출만 센다
    out = _mcp(base2, "deliberate_continue", {"previous_job_id": jid})
    r = out["resumed"]
    assert out["job_id"] == jid and (r["mode"], r["sk"], r["round"], r["todo"]) == ("in_place", "sim-2", 3, seats2), out
    assert "2단(해석 설계) 안에서" in out["note"], out["note"]
    done = _wait(base2, jid, timeout_s=180)
    assert done["status"] == "done" and len(done["attempts"]) == 1 and done["attempts"][0]["sk"] == "sim-2", done
    stats = stack.llm_stats()
    assert stats["calls"] == 2 * len(seats2) + 3, f"이은 실행의 LLM 호출이 2단 3 · 4라운드 좌석 + 의장 · 요약 · 쉬운 설명이 아니다: {stats['calls']}"
    recent = list(zip(stats["systems"], stats["humans"]))[-stats["calls"]:]
    who = lambda mark: sorted(re.search(r"당신은 '([^']+)' 전문가", sy).group(1) for sy, hu in recent  # noqa: E731
                              if "전문가입니다" in sy and mark in hu)
    assert who("수용(concede)") == sorted(seats2) and who("최종 입장(final_position") == sorted(seats2), "2단 3 · 4라운드 좌석이 아니다"
    assert not who("당신의 관점(lens"), "1라운드(1단이든 2단이든)를 다시 불렀다"
    later = [c["tool"] for c in _all_calls(stack)[seen:]]
    assert not [t for t in later if t in _SEAT_SWITCH], f"좌석 전환 · 사내 자산 현황을 다시 불렀다 — {later}"
    turns = _turns(base2, jid)
    key = lambda ts: sorted((t["display_round"], t["persona"]) for t in ts)  # noqa: E731
    assert key(turns) == key(oracle) and len(turns) == len(oracle), "끊긴 잡이 끊지 않은 실행의 칸을 단마다 좌석마다 한 번 채우지 않았다"
    assert turns[:len(pre)] == pre, "끊기기 전의 턴(1단 전부 · 2단 1 · 2라운드)이 바뀌었거나 빠졌다"
    assert not (jobs / "journal" / f"{jid}.jsonl").exists() and not (jobs / "args" / f"{jid}.json").exists()
