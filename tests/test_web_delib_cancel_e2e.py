# 가짜 LLM · 가짜 게이트웨이와 임시 에이전트 서버를 실제 프로세스로 띄워, 도는 웹 심의를 라운드 도중에 멈추고(실행 id) 보고 단계의 멈춤은 거절하는지 스트림 · LLM 호출 · 다음 요청으로 본다
#
# HWAXPortal docs/leftovers-1017 triage A24 (나) — 포털 /agent/chat 이 싣는 서버 대화 id 로 도는 웹 심의(분리 태스크)를 POST /deliberate/cancel 이
# 접는다. 단위 시험(tests/test_web_delib_cancel.py)은 엔진 대역을 쓴다 — 여기서는 진짜 엔진(run_deliberation)이 진짜 HTTP 로 가짜 LLM 을
# 부르는 동안 멈춘다. 보는 것.
#   ① 1라운드의 LLM 호출이 가짜 LLM 에 걸려 있는 동안 첫 심의 사건에 실린 실행 id 로 멈추면 스트림이 멈춤 사건(delib cancelled)과 done 으로
#      닫힌다(error 없음 · 결정문 없음).
#   ② 멈춘 뒤 LLM 호출이 더 나가지 않는다(2라운드 · 의장이 돌지 않는다) · delib_active 0 · 로그에 멈춤 줄이 있고 '심의 태스크 오류' 는 없다.
#   ③ 거듭 멈추면 같은 답(이미 멈췄다) · 모르는 대화는 404 — 그리고 서버는 다음 심의를 끝까지 돌리고, 끝난 심의의 멈춤에는 409 로 답한다.
#   ④ 의장이 결정문을 낸 뒤의 마무리(보고서 저장이 걸린 동안)에 멈추면 409 finishing 이고, 심의는 결정문 · 보고서 번호 · done 까지 간다 —
#      엔진은 의장 호출이 돌아온 바로 뒤(의결과 쉬운 설명 사이)에 finishing 표지를 낸다(A24 검토 — 종전엔 다 만든 결정문이 버려지고
#      Report Archive 초안이 고아로 남았다).
# 세 프로세스 모두 커널이 고른 빈 포트에 뜨고(떠 있는 9009 · 실 vLLM · 게이트웨이를 건드리지 않는다 — AIDataHub · 게이트웨이 HTTP 주소도 닫힌
# 포트로 돌린다), 환경은 최소로 새로 짠다. 잡 원장 · 산출물 · 비우기 깃발은 임시 경로다. 끝나면 이 시험이 띄운 PID 만 내린다.
#
#   실행:  .venv/bin/python -m pytest tests/test_web_delib_cancel_e2e.py -q
import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

from test_card_review_e2e import PY, _free_port, _listener_pid, _stop, _wait  # noqa: E402

import app as a  # noqa: E402 — 멈춤 글(_WEB_CANCEL_MSG)의 정본


DELAY = 2.0                       # 가짜 LLM 이 호출 하나에 걸리는 시간(초) — 그 사이에 멈춘다
RA = {"delay": 0.0, "calls": 0}   # 가짜 Report Archive(create_report_draft) — 걸리는 시간과 받은 수
REPORT_ID = 4242
ME = "u1@corp.example"
SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}]
OPTS = {"personas": SEATS, "free_tools": 0, "voc": "off", "rescreen": 0, "save_report": 0, "rounds": 2}
TOPIC = "/심의 보스 둘레 간극을 줄인 변경이 낙하 때 크랙을 키우는지 판단해 달라"


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("web-delib-cancel")
    # 가짜 게이트웨이 — 심의는 게이트웨이 도구가 하나도 없으면 시작하지 않는다(gateway_unavailable). 역할 원문 · 지식카드 둘이면 된다.
    # 보고서 저장(create_report_draft)은 RA 의 지연을 흉내 낸다 — 받자마자 세고 RA["delay"] 초 뒤에 번호를 돌려준다.
    gw = FastMCP("a24-fake-gateway", stateless_http=True, streamable_http_path="/")

    @gw.tool()
    async def get_agent_session(agent_type: str = "") -> str:
        """역할 원문."""
        return "{}"

    @gw.tool()
    async def agent_search(agent_type: str = "", q: str = "", mode: str = "") -> str:
        """지식카드 조회."""
        return '{"hits": []}'

    @gw.tool()
    async def create_report_draft(template_id: str = "", template_version: int = 1, title: str = "",
                                  blocks: dict | list | None = None, tags: list | None = None) -> str:
        """보고서 초안(가짜 Report Archive)."""
        RA["calls"] += 1
        await asyncio.sleep(RA["delay"])
        return json.dumps({"report_id": REPORT_ID, "status": "draft"})

    gw_port = _free_port()
    server = uvicorn.Server(uvicorn.Config(gw.streamable_http_app(), host="127.0.0.1", port=gw_port, log_level="error",
                                           lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "가짜 게이트웨이가 뜨지 않았다"
    (tmp / "mcp.json").write_text(json.dumps({"gateway": {"url": f"http://127.0.0.1:{gw_port}/",
                                                          "transport": "streamable_http"}}), encoding="utf-8")
    base_env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    llm_port, agent_port, shut = _free_port(), _free_port(), _free_port()     # shut — 아무도 듣지 않는 포트(닫힌 주소)
    llm_log, agent_log = tmp / "fake-llm.log", tmp / "agent.log"
    procs = []
    pid9009 = _listener_pid(9009)
    try:
        with open(llm_log, "w") as out:
            procs.append(subprocess.Popen(
                [PY, str(ROOT / "tests" / "fakes" / "llm_server.py"), "--port", str(llm_port), "--model", "fake-glm",
                 "--script", "oracle"], cwd=str(tmp), env=base_env, stdout=out, stderr=subprocess.STDOUT))
        llm = f"http://127.0.0.1:{llm_port}"
        _wait(f"{llm}/health", procs[0], llm_log)
        agent_env = {**base_env, "VLLM_BASE_URL": f"{llm}/v1", "VLLM_MODEL": "fake-glm", "VLLM_API_KEY": "fake",
                     "MCP_CONFIG": str(tmp / "mcp.json"), "MCP_SERVERS": "", "DELIB_JOB_DIR": str(tmp / "jobs"),
                     "ARTIFACT_DIR": str(tmp / "artifacts"), "DRAIN_STATE_PATH": str(tmp / "review-drain.json"),
                     "AIDH_HTTP_BASE": f"http://127.0.0.1:{shut}", "GATEWAY_HTTP_BASE": f"http://127.0.0.1:{shut}",
                     "DELIB_HEARTBEAT_S": "1"}
        with open(agent_log, "w") as out:
            procs.append(subprocess.Popen(
                [PY, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(agent_port), "--log-level", "warning"],
                cwd=str(ROOT), env=agent_env, stdout=out, stderr=subprocess.STDOUT))
        agent = f"http://127.0.0.1:{agent_port}"
        _wait(f"{agent}/health", procs[1], agent_log)
        yield {"llm": llm, "agent": agent, "agent_log": agent_log, "ports": (llm_port, agent_port, gw_port),
               "pid9009": pid9009}
    finally:
        for p in reversed(procs):
            _stop(p)
        server.should_exit = True
        thread.join(timeout=10)


def _client():
    return httpx.Client(timeout=60, trust_env=False)


def _script(c, llm: str, delay: float) -> None:
    assert c.post(f"{llm}/_script", json={"script": [{"behavior": "oracle", "delay": delay}]}).status_code == 200


class _Stream(threading.Thread):
    """웹 심의 하나를 /chat 으로 열어 사건을 받는다(ping 은 버린다). 첫 심의 사건의 실행 id 를 쥐고, r1 · report 단계가 오면 그 깃발을 세운다."""

    def __init__(self, agent: str, conv: str, opts: dict = OPTS):
        super().__init__(daemon=True)
        self.agent, self.conv, self.opts = agent, conv, opts
        self.frames, self.r1, self.report, self.err, self.run_id = [], threading.Event(), threading.Event(), None, None

    def run(self):
        body = {"message": TOPIC, "conversation_id": self.conv, "user_email": ME, "delib_opts": self.opts}
        try:
            with httpx.Client(timeout=httpx.Timeout(30, read=120), trust_env=False) as c, \
                    c.stream("POST", f"{self.agent}/chat", json=body) as r:
                assert r.status_code == 200
                ev = None
                for line in r.iter_lines():
                    if line.startswith("event: "):
                        ev = line[7:]
                    elif line.startswith("data: ") and ev != "ping":
                        data = json.loads(line[6:])
                        self.frames.append((ev, data))
                        if ev == "delib" and self.run_id is None:
                            self.run_id = data.get("run_id")
                        if ev == "delib" and data.get("kind") == "stage" and data.get("stage") == "r1":
                            self.r1.set()
                        if ev == "delib" and data.get("kind") == "stage" and data.get("stage") == "report":
                            self.report.set()
        except Exception as exc:  # noqa: BLE001 — 시험이 본다
            self.err = exc

    def kinds(self, ev: str = "delib") -> list:
        return [d.get("kind") for e, d in self.frames if e == ev]

    def marks(self) -> list:
        """심의 사건의 차례 — 단계는 'stage:<이름>', 나머지는 kind."""
        return [f"stage:{d.get('stage')}" if d.get("kind") == "stage" else d.get("kind") for e, d in self.frames if e == "delib"]


def _mine(conv: str, rid) -> dict:
    return {"conversation_id": conv, "run_id": rid, "by": ME, "user_email": ME}


def test_라운드_도중에_멈추면_스트림이_멈춤_사건으로_닫히고_LLM_호출이_멈추며_서버는_다음_요청을_받는다(stack):
    agent, llm = stack["agent"], stack["llm"]
    with _client() as c:
        _script(c, llm, DELAY)
        s = _Stream(agent, "conv-a24-1")
        s.start()
        assert s.r1.wait(60), f"1라운드에 들어가지 않았다 — {s.frames[-5:]} {s.err}"
        t0 = time.monotonic()
        while c.get(f"{llm}/_stats").json()["active"] < 1:          # 1라운드 좌석 호출이 가짜 LLM 에 걸려 있다
            assert time.monotonic() - t0 < 30, "1라운드 LLM 호출이 나가지 않았다"
            time.sleep(0.05)
        calls = c.get(f"{llm}/_stats").json()["calls"]
        rid = s.run_id
        assert isinstance(rid, str) and rid, f"첫 심의 사건에 실행 id 가 없다 — {s.frames[:3]}"
        # 사람은 실행 id 없이 대화 전체를 접지 못한다
        bare = c.post(f"{agent}/deliberate/cancel", json={"conversation_id": "conv-a24-1", "by": ME, "user_email": ME})
        assert bare.status_code == 422 and bare.json()["code"] == "run_id_required", bare.text
        r = c.post(f"{agent}/deliberate/cancel", json=_mine("conv-a24-1", rid))
        assert r.status_code == 200 and r.json()["status"] == "cancelling" and r.json()["runs"] == 1, r.text
        s.join(30)
        assert not s.is_alive() and s.err is None, f"스트림이 닫히지 않았다 — {s.frames[-5:]} {s.err}"

        # ① 멈춤 사건과 done 으로 닫힌다 — 오류가 아니고 결정문이 없다
        assert s.frames[-2:] == [("delib", {"kind": "cancelled", "by": ME, "message": a._WEB_CANCEL_MSG, "run_id": rid}),
                                 ("done", {})], s.frames[-4:]
        assert not [d for e, d in s.frames if e == "error"] and "decision" not in s.kinds()
        # ② 2라운드 · 의장이 돌지 않는다 — 걸려 있던 호출이 끝날 시간이 두 번 지나도 호출 수가 그대로다
        time.sleep(2 * DELAY + 1)
        assert c.get(f"{llm}/_stats").json()["calls"] == calls, "멈춘 뒤에도 LLM 호출이 나갔다"
        h = c.get(f"{agent}/health").json()
        assert h["delib_active"] == 0, h["delib_active"]
        # ③ 거듭 멈춤 · 모르는 대화
        again = c.post(f"{agent}/deliberate/cancel", json=_mine("conv-a24-1", rid))
        assert again.status_code == 200 and again.json()["already"] is True and again.json()["status"] == "cancelled"
        unknown = c.post(f"{agent}/deliberate/cancel", json={"conversation_id": "conv-none", "by": ME})
        assert unknown.status_code == 404 and unknown.json()["code"] == "delib_not_found"

        # 서버는 다음 요청을 받는다 — 새 심의가 끝까지 돌고, 끝난 심의의 멈춤에는 409 로 답한다
        _script(c, llm, 0.0)
        s2 = _Stream(agent, "conv-a24-2")
        s2.start()
        s2.join(120)
        assert not s2.is_alive() and s2.err is None, s2.err
        assert "decision" in s2.kinds() and s2.frames[-1] == ("done", {}) and "cancelled" not in s2.kinds(), s2.frames[-4:]
        late = c.post(f"{agent}/deliberate/cancel", json=_mine("conv-a24-2", s2.run_id))
        assert late.status_code == 409 and late.json()["code"] == "already_finished" and late.json()["status"] == "done"

    log = stack["agent_log"].read_text(encoding="utf-8", errors="replace")
    assert "[delib-cancel] 대화 conv-a24-1 웹 심의 1건 멈춤 요청" in log, log[-2000:]
    assert "심의 태스크 오류" not in log and "Traceback" not in log, log[-3000:]


def test_보고서를_저장하는_동안의_멈춤은_409_finishing_이고_심의는_결정문과_보고서_번호까지_간다(stack):
    """의결 · 쉬운 설명이 끝나고 Report Archive 저장이 걸린 동안 '멈춤' 을 누른다(A24 검토의 재현 — 종전엔 200 으로 접혀 결정문이 버려지고
    RA 초안은 번호가 대화에 닿지 않은 채 남았다). 엔진은 finishing 표지를 의결 뒤 · 쉬운 설명 앞에 낸다."""
    agent, llm = stack["agent"], stack["llm"]
    RA.update(delay=4.0, calls=0)
    try:
        with _client() as c:
            _script(c, llm, 0.0)
            s = _Stream(agent, "conv-a24-3", {**OPTS, "save_report": 1})
            s.start()
            assert s.report.wait(120), f"보고 단계에 닿지 않았다 — {s.frames[-5:]} {s.err}"
            t0 = time.monotonic()
            while not RA["calls"]:                                     # 가짜 RA 가 저장 요청을 받았다(걸려 있다)
                assert time.monotonic() - t0 < 30, "create_report_draft 가 불리지 않았다"
                time.sleep(0.05)
            mine = c.post(f"{agent}/deliberate/cancel", json=_mine("conv-a24-3", s.run_id))
            ops = c.post(f"{agent}/deliberate/cancel", json={"conversation_id": "conv-a24-3", "by": "운영"})
            s.join(60)
            assert not s.is_alive() and s.err is None, f"스트림이 닫히지 않았다 — {s.frames[-5:]} {s.err}"
    finally:
        RA.update(delay=0.0)
    for r in (ops, mine):
        assert r.status_code == 409 and r.json()["code"] == "finishing", r.text
    marks = s.marks()
    assert "cancelled" not in marks and s.frames[-1] == ("done", {}), s.frames[-4:]
    order = [marks.index(x) for x in ("stage:decide", "finishing", "stage:explain", "stage:report", "decision", "outcome")]
    assert order == sorted(order), f"finishing 표지는 의결 뒤 · 쉬운 설명 앞이고 결정문까지 간다 — {marks}"
    assert marks.count("finishing") == 1
    outcome = [d for e, d in s.frames if e == "delib" and d.get("kind") == "outcome"][0]
    assert outcome["report_id"] == REPORT_ID, outcome
    with _client() as c:
        late = c.post(f"{agent}/deliberate/cancel", json=_mine("conv-a24-3", s.run_id))
    assert late.status_code == 409 and late.json()["code"] == "already_finished" and late.json()["status"] == "done"


def test_떠_있는_9009_를_건드리지_않았다(stack):
    assert 9009 not in stack["ports"]
    assert _listener_pid(9009) == stack["pid9009"], "떠 있는 에이전트 서버의 리스너가 바뀌었다"
