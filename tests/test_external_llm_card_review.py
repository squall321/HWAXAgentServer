# 외부 응답자로 카드 대조 다섯 종류가 실제 라우트·실제 파서를 지나 GLM 길과 같은 결과를 내는지, 한도·보충 실패·취소·관문이 기존 오류 길로 나가는지 본다
#
# 일꾼은 가짜(tests/fakes/worker.py — 실제 MCP 도구 함수를 부르는 스레드)이고 답은 GLM 자리의 FakeLLM 과 같은 대본이다. 두 길의 창·
# 출력 상한을 같은 수(200,000 · 32,000)로 맞추면 프롬프트가 글자까지 같아야 하고, 같은 답이면 행·결측·품질이 같아야 한다 — 엔진이
# 외부 응답자의 글을 GLM 의 글과 똑같이 읽는다는 것을 그렇게 본다(X-D3).
#
#   실행:  .venv/bin/python -m pytest tests/test_external_llm_card_review.py -q
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as a  # noqa: E402
import card_review as cr  # noqa: E402
import delib_jobs  # noqa: E402
import external_llm as x  # noqa: E402
import mcp_server as m  # noqa: E402
from fakes.llm import FakeLLM, act, default_oracle, respond  # noqa: E402
from fakes.worker import FakeWorker  # noqa: E402
from test_card_review_contract import REQUESTS  # noqa: E402

OWNER = "owner@x.com"
IDENT = {"groups": ["g"], "user_email": OWNER, "user_pat": "user-pat", "entitlements": ["feat:deliberation"]}
CTX, MT = 200_000, 32_000
KIND_REQ = {"verdict": "verdict_fwd", "sweep": "sweep", "offcard": "offcard", "noinput": "noinput",
            "summary": "summary_domain"}


def scaled(k: float = 1000.0):
    """실제 1초를 k 초로 세는 시계 — 300초 기다림을 0.3초에 본다(설정값은 운영값 그대로라 안내 글도 그대로다)."""
    t0 = time.monotonic()
    return lambda: 1000.0 + (time.monotonic() - t0) * k


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    monkeypatch.setenv("DRAIN_STATE_PATH", str(tmp_path / "drain.json"))
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    monkeypatch.setattr(a, "VLLM_MODEL", "fake-glm")
    monkeypatch.setattr(a, "_ctx_cache", {"n": CTX, "src": "server"})
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setattr(a, "_delib_human", lambda: 0)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    monkeypatch.setattr(m, "_APP", a.app)

    def use(board):
        monkeypatch.setattr(a.app.state, "external_board", board, raising=False)
        return board

    use(x.Board(x.Settings(ctx_tokens=CTX, max_tokens=MT)))
    return SimpleNamespace(use=use, client=TestClient(a.app), monkeypatch=monkeypatch)


def _frames(r):
    return [delib_jobs._parse_sse(c.encode()) for c in r.text.split("\n\n") if c.strip()]


def _run(env, body, *, llm=None):
    """GLM(llm 없음 — FakeLLM)이나 외부 응답자 길로 run 하나 — (프레임, 마지막 result|error)."""
    if llm is not None:
        env.monkeypatch.setattr(a.app.state, "delib_llm", llm, raising=False)
    r = env.client.post("/card-review/run", json=body)
    assert r.status_code == 200, r.text[:300]
    frames = _frames(r)
    return frames, [(e, d) for e, d in frames if e in ("result", "error")][-1]


def _scripted(*behaviors):
    """시도마다 대본 거동을 바꿔 답하는 일꾼 머리 — FakeLLM(script) 와 같은 차례."""
    left = list(behaviors)

    def brain(_head, system, human):
        b = left.pop(0) if len(left) > 1 else left[0]
        return respond(act(b), system, human, default_oracle)[0]
    return brain


# ── 다섯 종류 — GLM 길과 같은 프롬프트·같은 결과 ─────────────────────────────────────
@pytest.mark.parametrize("kind", list(KIND_REQ))
def test_다섯_종류가_외부_응답자로_GLM_길과_같은_프롬프트와_결과를_낸다(env, kind):
    body = {**REQUESTS[KIND_REQ[kind]], **IDENT}
    glm = FakeLLM(["oracle"], max_tokens=MT)
    _, (ev_g, res_g) = _run(env, body, llm=glm)
    with FakeWorker(OWNER).thread() as w:
        _, (ev_x, res_x) = _run(env, {**body, "llm": "external"})
    assert (ev_g, ev_x) == ("result", "result"), (res_g, res_x)
    assert [(c["system"], c["human"]) for c in glm.calls] == [(s["system"], s["human"]) for s in w.seen], \
        "두 길의 프롬프트가 다르다"
    for k in ("kind", "variant", "rows", "missing", "extras", "quality"):
        assert res_x[k] == res_g[k], k
    mx, mg = res_x["meta"], res_g["meta"]
    assert (mx["llm"], mx["model"], mx["usage"], mx["finish_reason"]) == ("external", "external", None, "stop")
    assert "llm" not in mg and mg["usage"] is not None
    for k in ("input_sha", "prompt_chars", "basis", "aliases", "prompt_rev", "ctx_tokens"):
        assert mx[k] == mg[k], k
    lim = env.client.get("/card-review/limits", params={"llm": "external"}).json()
    assert mx["limits_rev_kind"] == lim["limits_rev_by_kind"][kind] != mg["limits_rev_kind"]
    head = w.seen[0]["head"]
    assert (head["kind"], head["max_tokens"], head["claim"]) == (kind, MT, 1)
    assert kind in head["label"] and head.get("target_key") == (body.get("target_key") or None)
    assert not w.errors, w.errors


def test_보충은_새_과제로_오고_결과가_GLM_길의_보충과_같다(env):
    body = {**REQUESTS["verdict_fwd"], **IDENT}
    glm = FakeLLM(["skip_middle", "oracle"], max_tokens=MT)
    _, (_, res_g) = _run(env, body, llm=glm)
    with FakeWorker(OWNER, brain=_scripted("skip_middle", "oracle")).thread() as w:
        _, (_, res_x) = _run(env, {**body, "llm": "external"})
    assert res_x["quality"]["attempts"] == res_g["quality"]["attempts"] == 2
    assert res_x["rows"] == res_g["rows"] and res_x["missing"] == res_g["missing"] == []
    assert [s["human"] for s in w.seen] == [c["human"] for c in glm.calls], "보충 프롬프트(지적 블록)가 다르다"
    assert len({s["head"]["task_id"] for s in w.seen}) == 2


# ── 한도 — 기존 오류 길로, 코드가 보이게 ─────────────────────────────────────────────
def test_일꾼이_없으면_첫_시도가_external_worker_absent_error_프레임으로_끝난다(env):
    board = env.use(x.Board(x.Settings(ctx_tokens=CTX, max_tokens=MT, wait_s=300), clock=scaled(), tick=0.02))
    frames, (ev, err) = _run(env, {**REQUESTS["verdict_one"], **IDENT, "llm": "external"})
    assert [e for e, _ in frames] == ["status", "error", "done"]
    assert ev == "error" and {k: err[k] for k in ("code", "infra", "retryable", "knob", "attempt")} == {
        "code": "external_worker_absent", "infra": True, "retryable": True, "knob": "EXTERNAL_LLM_WAIT_S", "attempt": 1}
    assert "300초" in err["message"]
    assert board.counts() == {"queued": 0, "claimed": 0}
    assert cr.gate_of(a.app).active == 0, "자리를 돌려주지 않았다"


def test_집고_답하지_않으면_한_번_되돌린_뒤_external_answer_timeout_이다(env):
    env.use(x.Board(x.Settings(ctx_tokens=CTX, max_tokens=MT, wait_s=3000, answer_s=1200), clock=scaled(), tick=0.02))
    with FakeWorker(OWNER, hold=True, poll_s=0.05).thread() as w:
        _, (ev, err) = _run(env, {**REQUESTS["verdict_one"], **IDENT, "llm": "external"})
    assert (ev, err["code"], err["infra"], err["retryable"], err["knob"]) == (
        "error", "external_answer_timeout", True, True, "EXTERNAL_LLM_ANSWER_S")
    assert [s["head"]["claim"] for s in w.seen] == [1, 2] and len({s["head"]["task_id"] for s in w.seen}) == 1


def test_보충_중의_외부_한도는_받은_행을_버리지_않고_repair_error_로_남는다(env):
    """보충(n ≥ 2) 중의 실패는 error 가 아니라 result 와 quality.repair_error 다(P-08) — 외부 응답자의 한도도 같은 길이다."""
    env.use(x.Board(x.Settings(ctx_tokens=CTX, max_tokens=MT, wait_s=300), clock=scaled(), tick=0.02))
    answered = []

    def once(head, system, human):
        answered.append(head["task_id"])
        return respond(act("skip_middle"), system, human, default_oracle)[0]

    w = FakeWorker(OWNER, brain=once, poll_s=0.05)
    real_handle = w.handle

    async def handle_then_quit(head):
        await real_handle(head)
        w._stop.set()                     # 첫 답만 내고 떠난다 — 보충 과제는 아무도 집지 않는다
    w.handle = handle_then_quit
    with w.thread():
        _, (ev, res) = _run(env, {**REQUESTS["verdict_fwd"], **IDENT, "llm": "external"})
    assert ev == "result" and res["rows"], res
    # GLM 길(계약 예제 verdict_repair_error)과 같은 모양 — 실패한 보충도 시도로 센다.
    assert {k: res["quality"]["repair_error"][k] for k in ("code", "attempt")} == {
        "code": "external_worker_absent", "attempt": 2}
    assert res["quality"]["attempts"] == 2 and res["quality"]["repaired"] is True
    assert len(answered) == 1


# ── 관문·취소 ────────────────────────────────────────────────────────────────
def test_외부_호출도_관문을_지나고_비우기면_과제를_만들지_않고_review_busy_다(env):
    board = a.app.state.external_board
    cr.gate_of(a.app).drain.set_on(scope="all", by="update-all", ttl_s=600)
    try:
        _, (ev, err) = _run(env, {**REQUESTS["verdict_one"], **IDENT, "llm": "external"})
    finally:
        cr.gate_of(a.app).drain.set_off(by="update-all")
    assert (ev, err["code"], err["reason"]) == ("error", "review_busy", "draining")
    assert board.counts() == {"queued": 0, "claimed": 0}


def test_스트림이_닫히면_과제가_줄에서_빠지고_자리를_돌려준다(env):
    """구독이 끊기면(포털·앱이 스트림을 놓으면) LLM 태스크가 취소된다 — 외부 응답자면 그 과제가 줄에서 빠진다."""
    board = a.app.state.external_board
    lim = cr.limits(CTX, "external", MT, model="external", max_tokens_source="external", llm="external")
    prep = cr.prepare({**REQUESTS["verdict_one"], **IDENT, "llm": "external"}, lim)

    async def go():
        gen = cr.run(a.app, prep)
        frames = []

        async def drain():
            async for c in gen:
                frames.append(delib_jobs._parse_sse(c))

        consumer = asyncio.ensure_future(drain())
        for _ in range(200):
            if board.counts()["queued"]:
                break
            await asyncio.sleep(0.01)
        queued = board.counts()
        active = cr.gate_of(a.app).active
        consumer.cancel()                                   # 끊김 — 스트리밍 응답 태스크가 취소된다
        await asyncio.gather(consumer, return_exceptions=True)
        await gen.aclose()
        await asyncio.sleep(0.05)
        return queued, active, board.counts(), cr.gate_of(a.app).active, frames

    queued, active, after, active_after, frames = asyncio.run(go())
    assert (queued, active) == ({"queued": 1, "claimed": 0}, 1)
    assert (after, active_after) == ({"queued": 0, "claimed": 0}, 0)
    assert [e for e, _ in frames] == ["status"]


# ── 라우트 — 꺼진 서버는 409, 켜진 서버의 limits·plan·거절 ─────────────────────────────
@pytest.fixture
def client(monkeypatch, tmp_path):
    """서버 모델 창은 1백만 토큰(GLM) — 외부 응답자 창(200,000)과 갈리게. 외부 응답자 줄은 기본 설정으로 처음 쓸 때 생긴다."""
    monkeypatch.setenv("DRAIN_STATE_PATH", str(tmp_path / "drain.json"))
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"]), raising=False)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    monkeypatch.setattr(a.app.state, "external_board", None, raising=False)
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setattr(a, "_delib_human", lambda: 0)
    return TestClient(a.app)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")


def _one():
    return {**REQUESTS["verdict_one"], **IDENT}


def test_꺼진_서버는_external_요청을_409_로_거절하고_GLM_길은_종전_그대로다(client):
    for r in (client.get("/card-review/limits", params={"llm": "external"}),
              client.post("/card-review/plan", json={"cards": [], "units": [], "llm": "external"}),
              client.post("/card-review/run", json={**_one(), "llm": "external"})):
        assert (r.status_code, r.json()["code"], r.json()["knob"]) == (409, "external_llm_disabled", "EXTERNAL_LLM_ENABLED")
    assert client.get("/card-review/limits").json() == client.get("/card-review/limits", params={"llm": "glm"}).json()


def test_켜진_서버의_limits_와_plan(client, on):
    lim = client.get("/card-review/limits", params={"llm": "external"}).json()
    assert (lim["llm"], lim["model"], lim["ctx_tokens"], lim["llm_max_tokens"], lim["ctx_source"],
            lim["max_tokens_source"]) == ("external", "external", 200_000, 32_000, "external", "external")
    same_numbers = cr.limits(200_000, "server", 32_000)
    assert lim["limits_rev"] != same_numbers["limits_rev"], "창·출력 상한이 같아도 외부 길의 판은 갈려야 한다"
    assert all(lim["limits_rev_by_kind"][k] != same_numbers["limits_rev_by_kind"][k] for k in cr.KINDS)
    glm = client.get("/card-review/limits").json()
    assert "llm" not in glm and glm["model"] != "external", "llm 을 안 주면 GLM 길 그대로다"
    plan = client.post("/card-review/plan", json={"cards": [], "units": [], "llm": "external"}).json()
    assert (plan["llm"], plan["limits_rev"]) == ("external", lim["limits_rev"])
    assert "llm" not in client.post("/card-review/plan", json={"cards": [], "units": []}).json()


def test_켜진_서버의_external_run_거절(client, on):
    one = _one()
    r = client.post("/card-review/run", json={**one, "llm": "external", "user_email": ""})
    assert (r.status_code, r.json()["code"]) == (422, "external_owner_missing")
    r = client.post("/card-review/run", json={**one, "llm": "gpt"})
    assert (r.status_code, r.json()["code"]) == (422, "bad_request")
    r = client.get("/card-review/limits", params={"llm": "gpt"})
    assert (r.status_code, r.json()["code"]) == (422, "bad_request")
    lim = client.get("/card-review/limits").json()          # GLM 판을 기대한 외부 호출 — 판이 달라 409
    r = client.post("/card-review/run", json={**one, "llm": "external",
                                              "expect": {"limits_rev_kind": lim["limits_rev_by_kind"]["verdict"]}})
    assert (r.status_code, r.json()["code"]) == (409, "rev_mismatch")
    r = client.post("/card-review/run", json={**one, "llm": "external", "entitlements": ["feat:thinking"]})
    assert (r.status_code, r.json()["code"]) == (403, "denied"), "권한 거절이 먼저다"


def test_GLM_길의_limits_는_llm_인자를_더해도_판이_같다():
    for ctx, mt in ((1_048_576, 0), (200_000, 32_000), (16_384, 0), (4_096, 0)):
        assert cr.limits(ctx, "server", mt) == cr.limits(ctx, "server", mt, llm="glm")


def test_카드_대조의_외부_손잡이_이름이_external_llm_과_같다():
    """limits() 안내 글은 예산 식이 읽는 전역을 늘리지 않으려고 이름을 글자로 적는다 — 그 글자가 손잡이 이름과 같은지 맞댄다."""
    lim = cr.limits(4_096, "external", 32_000, llm="external")
    msg = lim["ctx_too_small"]["message"]
    assert x.CTX_ENV in msg or x.MAX_TOKENS_ENV in msg, msg
    assert "DELIB_MAX_TOKENS" not in msg and "VLLM_MODEL" not in msg
