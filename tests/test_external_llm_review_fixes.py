# 외부 응답자 반대 검토 1라운드의 결함을 못박는 회귀 시험 — 묵은 집기(임대)·주인 이름 디코딩·GLM 관문 자리·반출 동의 표지·에이전트 도구 거부·근거 몫 캐시·'없다' 판정의 수명·글자 단위와 max_chars·절단 손잡이·이어하기의 free_tools·설정 닫힘·주인 칸 정리·GLM 길 바이트 동일
#
# 검토자의 재현 탐침(p01 줄 · p02 카드 대조 · p05 근거 몫 경합 · p06 GLM 계약 · p07 심의 프레임 · p09 단위 · p10 이어하기)을 시험으로
# 옮겼다. 실제 Claude 는 부르지 않는다(X-D1) — 일꾼은 tests/fakes/worker.py 이거나 MCP 도구 함수를 바로 부른다. 한도는 시계를 바꿔
# 끼운 줄(Board(clock=…))로 본다 — 실제로 300초를 기다리지 않는다.
#
#   실행:  .venv/bin/python -m pytest tests/test_external_llm_review_fixes.py -q
import asyncio
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

import app as a  # noqa: E402
import card_review as cr  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import external_llm as x  # noqa: E402
import mcp_server as m  # noqa: E402
from fakes.llm import FakeLLM, delib_respond  # noqa: E402
from fakes.worker import FakeWorker, ctx_of  # noqa: E402
from test_card_review_contract import REQUESTS  # noqa: E402

OWNER = "owner@x.com"
IDENT = {"groups": ["g"], "user_email": OWNER, "user_pat": "user-pat", "entitlements": ["feat:deliberation"]}
EXT = {"llm": "external", "consent_external": True}
CTX, MT = 200_000, 32_000


class Clock:
    """바꿔 끼우는 단조 시계 — 시험이 앞으로 민다."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def gw(email: str):
    """게이트웨이가 실어 보낸 신원 — gateway 는 x-hwax-user 를 quote(user.strip().lower(), safe="@.") 로 싣는다."""
    return ctx_of(quote(email.strip().lower(), safe="@."))


async def _until(cond, timeout: float = 5.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("기다린 상태가 오지 않았다")


def _wait_until(cond, timeout: float = 10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("기다린 상태가 오지 않았다")


def _frames(r):
    return [delib_jobs._parse_sse(c.encode()) for c in r.text.split("\n\n") if c.strip()]


@pytest.fixture
def stub(monkeypatch):
    """외부 응답자가 켜진 앱 대역 — 과제 줄은 시계를 바꿔 끼운 것이다. MCP 도구도 이 앱을 본다."""
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    clock = Clock()
    board = x.Board(x.Settings(wait_s=300, answer_s=1200), clock=clock, tick=0.02)
    app = SimpleNamespace(state=SimpleNamespace(external_board=board, delib_llm=None, llm=object()))
    monkeypatch.setattr(m, "_APP", app)
    return SimpleNamespace(app=app, board=board, clock=clock)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """실제 앱의 카드 대조 라우트 — GLM 관문 상한 2(탐침 p02), 외부 응답자 사람별 상한 2, 시계가 멈춘 줄(일꾼이 없어도 시험이 시계를
    밀기 전에는 '없다' 로 끝나지 않는다)."""
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    monkeypatch.setenv("DRAIN_STATE_PATH", str(tmp_path / "drain.json"))
    monkeypatch.setenv("CARD_REVIEW_CONCURRENCY", "2")
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    monkeypatch.setattr(a, "VLLM_MODEL", "fake-glm")
    monkeypatch.setattr(a, "_ctx_cache", {"n": CTX, "src": "server"})
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setattr(a, "_delib_human", lambda: 0)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"], max_tokens=MT), raising=False)
    monkeypatch.setattr(m, "_APP", a.app)
    clock = Clock()
    board = x.Board(x.Settings(ctx_tokens=CTX, max_tokens=MT, max_inflight=2), clock=clock, tick=0.02)
    monkeypatch.setattr(a.app.state, "external_board", board, raising=False)
    return SimpleNamespace(client=TestClient(a.app), board=board, clock=clock)


def _call(app, owner=OWNER, kind="verdict", system="S", human="H", **bind):
    model = x.model_for(app, owner=owner, kind=kind)
    return asyncio.ensure_future((model.bind(**bind) if bind else model).ainvoke([("system", system), ("human", human)]))


# ── 1. 묵은 집기 — 집기마다 임대(lease) ───────────────────────────────────────────
def test_되돌려졌다_다시_집힌_과제에_묵은_임대로_낸_조각은_버퍼에_닿지_않는다(stub):
    """탐침 p01 stale_claim·stale_restart — A 가 반쯤 낸 뒤 답 한도가 지나 B 가 claim 2 로 다시 집었다. 종전엔 A 의 늦은 조각이
    B 의 앞 조각 뒤에 이어 붙거나(final) B 가 쓴 것을 지웠다(offset 0). 이제 A 의 읽기·내기·지우기는 not_claimed 이고 엔진이 받는
    답은 B 의 것뿐이다."""
    async def go():
        call = _call(stub.app)
        await _until(lambda: stub.board.counts()["queued"] == 1)
        A, B = ctx_of(OWNER), ctx_of(OWNER)                 # 같은 주인의 두 일꾼(워크플로의 병렬 에이전트)
        h1 = await m.review_task_next(ctx=A)
        a1 = await m.review_task_submit(h1["task_id"], h1["lease"], '{"ref":"c1","v":"OK"}\n{"ref":"c2",', offset=0, ctx=A)
        stub.clock.t += 1201                               # 답 한도가 지났다 — 한 번 줄로 되돌아간다
        await asyncio.sleep(0.1)
        h2 = await m.review_task_next(ctx=B)
        b1 = await m.review_task_submit(h2["task_id"], h2["lease"], '{"ref":"c1","v":"FAIL"}\n', offset=0, ctx=B)
        stale = [await m.review_task_read(h1["task_id"], h1["lease"], 0, ctx=A),
                 await m.review_task_submit(h1["task_id"], h1["lease"], '"v":"OK"}\n', final=True, ctx=A),
                 await m.review_task_submit(h1["task_id"], h1["lease"], "A 처음부터", offset=0, ctx=A)]
        b2 = await m.review_task_submit(h2["task_id"], h2["lease"], '{"ref":"c2","v":"OK"}\n', offset=b1["received_chars"],
                                        final=True, ctx=B)
        return h1, h2, a1, b2, stale, await call

    h1, h2, a1, b2, stale, msg = asyncio.run(go())
    assert h1["task_id"] == h2["task_id"] and (h1["claim"], h2["claim"]) == (1, 2) and h1["lease"] != h2["lease"]
    assert a1["ok"] is True and b2["done"] is True
    assert [r["code"] for r in stale] == ["not_claimed"] * 3, stale
    assert msg.content == '{"ref":"c1","v":"FAIL"}\n{"ref":"c2","v":"OK"}\n', "두 일꾼의 답이 한 답으로 섞였다"


def test_임대가_없거나_다르면_지금_집힌_과제라도_읽기_내기가_not_claimed_다(stub):
    async def go():
        call = _call(stub.app)
        await _until(lambda: stub.board.counts()["queued"] == 1)
        me = ctx_of(OWNER)
        h = await m.review_task_next(ctx=me)
        bad = [await m.review_task_read(h["task_id"], "", 0, ctx=me),
               await m.review_task_read(h["task_id"], h["lease"] + "x", 0, ctx=me),
               await m.review_task_submit(h["task_id"], "", "남의 답", final=True, ctx=me),
               await m.review_task_submit(h["task_id"], "ls-guess", "남의 답", offset=0, ctx=me)]
        ok = await m.review_task_submit(h["task_id"], h["lease"], "진짜 답", final=True, ctx=me)
        return bad, ok, await call

    bad, ok, msg = asyncio.run(go())
    assert [r["code"] for r in bad] == ["not_claimed"] * 4
    assert ok["done"] is True and msg.content == "진짜 답"


# ── 2. 주인 이름 — 원문은 풀지 않고 게이트웨이 헤더만 푼다 ──────────────────────────────
def test_원문_이메일의_퍼센트는_풀지_않아_남의_워크플로가_그_과제를_집지_못한다(stub):
    """탐침 p01 percent_owner — 카드 대조의 주인은 포털이 덮어쓴 원문 이메일이다. 종전엔 원문도 퍼센트를 풀어 'ev%41l@' 이
    'evAl@' 이 됐고 eval@ 의 워크플로가 그 과제를 집어 카드 글을 읽고 답을 냈다."""
    victim, other = "Ev%41l@Corp.com", "eval@corp.com"

    async def go():
        call = _call(stub.app, owner=victim, human="기밀 카드 글")
        await _until(lambda: stub.board.counts()["queued"] == 1)
        tid = next(iter(stub.board._tasks))
        theirs = await m.review_task_next(ctx=gw(other))
        theirs_read = await m.review_task_read(tid, "ls-x", 0, ctx=gw(other))
        mine = await m.review_task_next(ctx=gw(victim))     # 주인의 워크플로 — 헤더는 'ev%2541l@corp.com' 으로 온다
        theirs_submit = await m.review_task_submit(tid, mine["lease"], "남이 쓴 답", final=True, ctx=gw(other))
        await m.review_task_submit(tid, mine["lease"], "주인의 답", final=True, ctx=gw(victim))
        return theirs, theirs_read, mine, theirs_submit, await call

    theirs, theirs_read, mine, theirs_submit, msg = asyncio.run(go())
    assert theirs["empty"] is True and theirs_read["code"] == theirs_submit["code"] == "task_not_found"
    assert mine["task_id"] and msg.content == "주인의 답"
    assert x._norm_owner(" Ev%41l@Corp.com ") == "ev%41l@corp.com", "원문은 공백·대소문자만 맞춘다"
    assert x.header_owner(quote("first+tag@x.com", safe="@.")) == "first+tag@x.com"


def test_MCP_로_연_외부_심의는_게이트웨이_헤더를_풀어_과제_주인으로_삼고_잡_주인은_원문_그대로다(monkeypatch, tmp_path):
    """'+' 가 든 이메일은 헤더에 %2B 로 온다. 과제 주인은 푼 이름이라 같은 헤더로 붙은 워크플로가 그 과제를 집는다. 잡 기록의
    주인(user)은 헤더 원문 그대로다(접기·이어하기·목록이 같은 원문으로 견준다). 시계는 멈춰 둔다 — 주인이 어긋나면 아무도 못 집어
    심의가 끝나지 않고(시간 상한에서 실패), 맞으면 곧 끝난다."""
    import langgraph.prebuilt

    from test_external_llm_delib import Q, SEATS, _Tool

    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    tools = {"agent_search": _Tool("agent_search"), "create_report_draft": _Tool("create_report_draft", '{"id": 7}')}

    async def fake_tools(*_a, **_k):
        return tools

    monkeypatch.setattr(d, "_tools_by_name", fake_tools)
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda *_a, **_k: pytest.fail("도구를 묶었다"))
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    board = x.Board(x.Settings(), clock=Clock(), tick=0.02)
    owners = []
    real_open = board.open

    def open_(**kw):
        owners.append(kw["owner"])
        return real_open(**kw)

    board.open = open_
    monkeypatch.setattr(m, "_APP", SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None,
                                                                          external_board=board)))
    hdr = quote("first+tag@x.com", safe="@.")
    assert hdr == "first%2Btag@x.com"

    async def go():
        w = FakeWorker(hdr, poll_s=0.02)
        serve = asyncio.ensure_future(w.serve())
        try:
            out = await m.deliberate_start(Q, job="risk-review", rounds=2, personas=SEATS, save_report=False,
                                           advanced={**EXT, "voc": "off"}, ctx=ctx_of(hdr))
            await asyncio.wait_for(asyncio.shield(delib_jobs._TASKS[out["job_id"]]), 60)
            return delib_jobs._JOBS[out["job_id"]], w
        finally:
            w.stop()
            await serve

    job, w = asyncio.run(go())
    assert job["status"] == "done", job.get("error")
    assert job["user"] == hdr, "잡 주인은 헤더 원문 그대로다"
    assert owners and set(owners) == {"first+tag@x.com"} and w.seen


# ── 3. GLM 관문 — 외부 호출은 자리를 잡지 않고 사람별 상한·비우기·review_active ───────────────
def test_외부_호출은_GLM_관문_자리를_잡지_않고_사람별_상한과_비우기를_따르며_review_active_에_든다(env):
    """탐침 p02 — CARD_REVIEW_CONCURRENCY=2 에서 A 의 외부 호출 둘이 사람 속도의 일꾼을 기다리는 동안 B 의 GLM 카드 대조가
    review_busy 였다. 이제 외부 호출은 GLM 관문 밖이고 한 사람당 EXTERNAL_LLM_MAX_INFLIGHT(여기 2)로 묶인다. 비우기면 받지 않고,
    도는 수는 update-all 비우기가 기다리는 review_active 에 든다(재기동은 과제 줄을 잃는다)."""
    c, board = env.client, env.board
    results: dict = {}

    def ext(i, who):
        r = c.post("/card-review/run", json={**REQUESTS["verdict_one"], **IDENT, "user_email": who, **EXT})
        results[i] = [(e, dd.get("code")) for e, dd in _frames(r) if e in ("error", "result")]

    ths = [threading.Thread(target=ext, args=(i, "a@x.com")) for i in range(2)]
    for t in ths:
        t.start()
    try:
        _wait_until(lambda: board.counts()["queued"] == 2)
        gate = cr.gate_of(a.app)
        assert (gate.active, board.calls_active()) == (0, 2), "외부 호출이 GLM 관문 자리를 쥐었다"
        h = c.get("/health").json()
        assert (h["review_active"], h["card_review"]["active"], h["external_llm"]["inflight"],
                h["external_llm"]["max_inflight"]) == (2, 0, 2, 2)
        assert c.get("/drain").json()["review_active"] == 2
        # B 의 GLM 카드 대조는 막히지 않는다
        rb = _frames(c.post("/card-review/run", json={**REQUESTS["verdict_one"], **IDENT, "user_email": "b@x.com"}))
        assert [e for e, _ in rb][-2:] == ["result", "done"], rb[-2:]
        assert len(a.app.state.delib_llm.calls) == 1
        # A 의 셋째 외부 호출은 그 사람의 상한(2)에 걸린다 — 과제를 만들지 않는다
        r3 = _frames(c.post("/card-review/run", json={**REQUESTS["verdict_one"], **IDENT, "user_email": "a@x.com", **EXT}))
        err = next(dd for e, dd in r3 if e == "error")
        assert {k: err[k] for k in ("code", "reason", "knob", "limit", "active", "infra", "retryable")} == {
            "code": "review_busy", "reason": "external_per_owner", "knob": "EXTERNAL_LLM_MAX_INFLIGHT", "limit": 2,
            "active": 2, "infra": True, "retryable": True}
        assert board.counts()["queued"] == 2
        lim = c.get("/card-review/limits", params={"llm": "external"}).json()["gate"]
        assert lim == {"limit": 2, "limit_reason": "external_per_owner", "active": 2}
        # 비우기(scope all)면 다른 사람의 외부 호출도 받지 않는다
        gate.drain.set_on(scope="all", by="update-all", ttl_s=600)
        try:
            r4 = _frames(c.post("/card-review/run", json={**REQUESTS["verdict_one"], **IDENT, "user_email": "c@x.com",
                                                           **EXT}))
            lim = c.get("/card-review/limits", params={"llm": "external"}).json()["gate"]
        finally:
            gate.drain.set_off(by="update-all")
        err4 = next(dd for e, dd in r4 if e == "error")
        assert (err4["code"], err4["reason"], board.calls_active(), board.counts()["queued"]) == (
            "review_busy", "draining", 2, 2)
        assert (lim["limit"], lim["limit_reason"]) == (0, "draining")
    finally:
        env.clock.t += 301                                 # 일꾼이 없다 — A 의 둘은 '없다' 로 끝난다
        for t in ths:
            t.join(30)
    assert results == {0: [("error", "external_worker_absent")], 1: [("error", "external_worker_absent")]}
    assert (board.calls_active(), c.get("/health").json()["review_active"]) == (0, 0)


# ── 4. 반출 동의 표지(엔진의 겹 방어) ─────────────────────────────────────────────
def test_카드_대조_run_은_consent_external_true_가_없으면_과제를_만들기_전에_422_다(env):
    c, board = env.client, env.board
    one = {**REQUESTS["verdict_one"], **IDENT, "llm": "external"}
    for consent in (None, False, "true", 1):
        body = dict(one) if consent is None else {**one, "consent_external": consent}
        r = c.post("/card-review/run", json=body)
        assert (r.status_code, r.json()["code"]) == (422, "external_consent_required"), (consent, r.text[:200])
    r = c.post("/card-review/run", json={**one, "user_email": ""})
    assert r.json()["code"] == "external_consent_required", "동의 표지를 신원보다 먼저 본다"
    assert board.counts() == {"queued": 0, "claimed": 0} and board.calls_active() == 0
    # limits·plan 은 글이 나가지 않아 묻지 않는다 · GLM 길은 표지와 상관없다
    assert c.get("/card-review/limits", params={"llm": "external"}).status_code == 200
    assert c.post("/card-review/plan", json={"cards": [], "units": [], "llm": "external"}).status_code == 200
    rg = _frames(c.post("/card-review/run", json={**REQUESTS["verdict_one"], **IDENT}))
    assert [e for e, _ in rg][-2:] == ["result", "done"]


def test_심의_입구_셋과_MCP_시작은_반출_동의_표지가_없으면_과제_없이_거절한다(monkeypatch, tmp_path):
    from test_external_llm_delib import _mcp, _panel

    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    for entry in (d.run_deliberation, d.run_sim_deliberation, d.run_test_plan):
        for consent in (None, False, "true"):
            events, _w, _bound, _tools, board = _panel(monkeypatch, {"consent_external": consent}, worker=False,
                                                       entry=entry)
            assert [ev for ev, _ in events] == ["error", "done"], (entry, consent)
            assert events[0][1]["code"] == "external_consent_required" and board.counts() == {"queued": 0, "claimed": 0}
    with pytest.raises(ValueError, match="^external_consent_required"):
        _mcp(monkeypatch, tmp_path, advanced={"llm": "external", "voc": "off"})
    assert delib_jobs._JOBS == {}


def test_도구_설명은_서버가_표지_밖의_동의를_본다고_말하지_않고_의장_결정문의_꼴을_적는다():
    assert "서버가 보는 것은 그 표지 하나다" in m._TASK_EGRESS and "consent_external: true" in m._TASK_EGRESS
    assert "동의는 리스크 앱이 잡마다 받는다" not in m._TASK_EGRESS
    assert "마크다운 결정문" in m._TASK_FORMAT and "```json 코드 펜스로 감싼 risk_spec" in m._TASK_FORMAT
    on = FastMCP(name="on")
    assert m._register_external(on, env={"EXTERNAL_LLM_ENABLED": "1"}) is True
    for t in asyncio.run(on.list_tools()):
        assert "consent_external" in t.description and "유니코드 코드 포인트" in t.description, t.name


# ── 5. 이 서버의 에이전트는 과제 도구를 묶지도 부르지도 않는다 ────────────────────────────
def _named(name: str):
    @tool(name)
    async def _t(arg: str = "") -> str:
        """대역 도구."""
        return f"called {name}"
    return _t


def test_게이트웨이_도구_목록에서_과제_도구와_그_별칭을_뺀다(monkeypatch):
    """게이트웨이는 이 서버의 /mcp 를 사용자 신원으로 중계한다 — 챗·심의 에이전트가 과제 도구를 묶으면 주입된 지시를 받은 GLM 턴이
    그 사람의 기다리는 과제를 집거나 답을 낼 수 있다. 목록을 받는 곳(_get_tools_retry)에서 빼고, 별칭(<앱키>_<도구>)도 뺀다."""
    names = ["list_materials", "invoke_tool", "review_task_next", "review_task_read", "review_task_submit",
             "hwaxdeliberation_review_task_submit"]

    class _Gw:
        def __init__(self, connections):
            pass

        async def get_tools(self):
            return [_named(n) for n in names]

    monkeypatch.setattr(a, "MultiServerMCPClient", _Gw)
    got = asyncio.run(a._get_tools_retry({"gateway": {}}))
    assert {t.name for t in got} == {"list_materials", "invoke_tool"}
    conns = {"gateway": {"url": "http://gw/mcp", "transport": "streamable_http", "headers": {}}}
    stub = SimpleNamespace(state=SimpleNamespace(connections=conns, llm=object(), delib_llm=None))
    assert set(asyncio.run(d._tools_by_name(stub, ["g"]))) == {"list_materials", "invoke_tool"}, "심의 도구 사전도 같은 문이다"
    assert a._AGENT_DENY_TOOLS == frozenset(x.TASK_TOOLS)


def test_상시_묶이는_invoke_tool_로도_과제_도구를_부르지_못한다():
    calls = []

    @tool("invoke_tool")
    async def invoke(name: str, arguments: dict | None = None) -> str:
        """게이트웨이 invoke_tool 대역."""
        calls.append(name)
        return f"called {name}"

    t = a._prep_tool(invoke)

    async def go():
        return [await t.ainvoke({"name": n, "arguments": {}})
                for n in ("review_task_next", "HWAXDELIBERATION_review_task_submit", " review_task_read ", "list_materials")]

    out = asyncio.run(go())
    assert calls == ["list_materials"], "막아야 할 이름이 게이트웨이까지 갔다"
    assert all(o.startswith(a._TOOL_FAIL_MARK) and "Claude Code 워크플로만" in o for o in out[:3]), out
    assert out[3] == "called list_materials"


# ── L1. 근거 몫 캐시 — 창과 값을 한 덩이로 ──────────────────────────────────────────
def test_근거_몫_캐시는_창과_값을_한_덩이로_두어_다른_창의_값을_읽지_않는다(monkeypatch):
    """탐침 p05 — /health(스레드)가 외부 창으로 근거 몫을 셈하는 동안 이벤트 루프의 GLM 심의가 같은 캐시를 읽었다. 값과 창을 두 칸에
    나눠 적어 그 사이에 읽으면 한 창의 판정에 다른 창의 값을 받았다(1초에 수백 번)."""
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    d._evid_cache.clear()
    glm = d._evid_budget()
    assert set(d._evid_cache) == {"pre"} and d._evid_cache["pre"][0] == 1_048_576
    tok = x.ctx_override.set(200_000)
    try:
        ext = d._evid_budget()
    finally:
        x.ctx_override.reset(tok)
    assert ext < glm and d._evid_budget() == glm
    old, stop, bad = sys.getswitchinterval(), threading.Event(), []

    def health():
        while not stop.is_set():
            t = x.ctx_override.set(200_000)
            try:
                v = d._evid_budget()
            finally:
                x.ctx_override.reset(t)
            if v != ext:
                bad.append(("health", v))

    th = threading.Thread(target=health, daemon=True)
    sys.setswitchinterval(1e-6)
    try:
        th.start()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.0:
            v = d._evid_budget()
            if v != glm:
                bad.append(("glm", v))
    finally:
        stop.set()
        th.join(10)
        sys.setswitchinterval(old)
        d._evid_cache.clear()
    assert not bad, bad[:5]


# ── L2. '없다' 판정은 WAIT_S 뒤에 풀린다 ────────────────────────────────────────────
def test_없다_판정은_WAIT_S_동안만_서고_그_뒤의_새_과제는_다시_줄을_선다(stub):
    """탐침 p01 standing_absent — 종전엔 한 번 '없다' 로 판정된 주인은 사흘 뒤의 새 잡도 줄을 서지 못하고 곧바로 실패했다(워크플로를
    먼저 켜 두지 않았으면)."""
    async def go():
        first = _call(stub.app)
        await _until(lambda: stub.board.counts()["queued"] == 1)
        stub.clock.t += 301                                # 1301 — '없다' 판정
        with pytest.raises(x.ExternalLLMError):
            await first
        with pytest.raises(x.ExternalLLMError) as fast:     # 판정 뒤 WAIT_S 안 — 줄 세우지 않고 바로
            await _call(stub.app)
        stub.clock.t += 299                                # 1600 — 아직 안(299초)
        with pytest.raises(x.ExternalLLMError):
            await _call(stub.app)
        stub.clock.t += 1                                  # 1601 — 풀렸다
        later = _call(stub.app, human="사흘 뒤의 새 잡")
        await _until(lambda: stub.board.counts()["queued"] == 1)
        await asyncio.sleep(0.1)
        queued = not later.done()
        h = await m.review_task_next(ctx=ctx_of(OWNER))      # 그 뒤에 켠 워크플로가 집는다
        await m.review_task_submit(h["task_id"], h["lease"], "늦게 켠 워크플로의 답", final=True, ctx=ctx_of(OWNER))
        return fast.value, queued, await later

    fast, queued, msg = asyncio.run(go())
    assert fast.code == "external_worker_absent" and "300초가 지날 때까지" in fast.message
    assert queued, "판정이 풀린 뒤의 새 과제가 줄을 서지 못했다"
    assert msg.content == "늦게 켠 워크플로의 답"


# ── L3. 글자 단위(유니코드 코드 포인트)와 max_chars ──────────────────────────────────
def test_머리의_max_chars_는_어떤_글자로_써도_잘리지_않는_글자_수다(stub):
    for mt in (1, 50, 32_000, 128_000):
        mc = x.max_chars(mt)
        assert mc == int(mt * d._EVID_KO_CPT)
        for ch in ("가", "a", "𝜎", "한a\n"):
            s = (ch * mc)[:mc]
            assert x.fit_answer(s, mt) == (s, "stop"), (mt, ch)
    assert x.max_chars(None) is None and x.max_chars(0) is None

    async def go():
        call = _call(stub.app, max_tokens=50)
        await _until(lambda: stub.board.counts()["queued"] == 1)
        h = await m.review_task_next(ctx=ctx_of(OWNER))
        await m.review_task_submit(h["task_id"], h["lease"], "가" * h["max_chars"], final=True, ctx=ctx_of(OWNER))
        return h, await call

    h, msg = asyncio.run(go())
    assert (h["max_tokens"], h["max_chars"]) == (50, 52)
    assert msg.response_metadata["finish_reason"] == "stop" and len(msg.content) == 52


def test_길이와_자리는_유니코드_코드_포인트다_JS_의_UTF16_단위가_아니다(stub):
    """탐침 p09 — 수식 기호(𝜎 U+1D70E 등)는 BMP 밖이라 JS String.length 가 2로 센다. 서버는 코드 포인트로 센다(설명·README 가 그렇게
    말한다) — 코드 포인트로 낸 offset 은 받고 UTF-16 으로 센 offset 은 offset_mismatch 로 돌려보낸다."""
    def js_len(s):
        return len(s.encode("utf-16-le")) // 2

    system, human = "응력 𝜎_max 와 변형률 𝜀 를 본다.", "판정하라"
    ans1, ans2 = '{"ref":"c1","why":"𝜎 초과"}\n', '{"ref":"c2","why":"ok"}\n'

    async def go():
        call = _call(stub.app, system=system, human=human)
        await _until(lambda: stub.board.counts()["queued"] == 1)
        me = ctx_of(OWNER)
        h = await m.review_task_next(ctx=me)
        page = await m.review_task_read(h["task_id"], h["lease"], 0, ctx=me)
        r1 = await m.review_task_submit(h["task_id"], h["lease"], ans1, offset=0, ctx=me)
        r_js = await m.review_task_submit(h["task_id"], h["lease"], ans2, offset=js_len(ans1), final=True, ctx=me)
        r2 = await m.review_task_submit(h["task_id"], h["lease"], ans2, offset=len(ans1), final=True, ctx=me)
        return h, page, r1, r_js, r2, await call

    h, page, r1, r_js, r2, msg = asyncio.run(go())
    text = page["text"]
    assert h["chars"] == len(system) + len(human) == len(text) == js_len(text) - 2
    hp = next(p for p in h["parts"] if p["role"] == "human")
    assert text[hp["start"]:hp["start"] + hp["chars"]] == human
    assert r1["received_chars"] == len(ans1) and (r_js["code"], r_js["received_chars"]) == ("offset_mismatch", len(ans1))
    assert r2["done"] is True and msg.content == ans1 + ans2


# ── L4. 절단의 손잡이 — 외부 길은 EXTERNAL_LLM_MAX_TOKENS ─────────────────────────────
def test_외부_길의_truncated_empty_는_EXTERNAL_LLM_MAX_TOKENS_를_가리킨다(env):
    """탐침 p02 둘째 — 외부 답이 출력 상한에서 잘려 행이 하나도 없으면 손잡이가 DELIB_MAX_TOKENS 였다(외부 길에 걸리지 않는 값)."""
    prose = "이 단위는 다음과 같이 판단합니다. " * 6000        # 앞이 산문이라 잘린 앞부분(출력 상한 32,000 토큰)에 JSON 줄이 없다
    with FakeWorker(OWNER, brain=lambda *_: prose + '\n{"ref":"c1"}').thread() as w:
        r = env.client.post("/card-review/run", json={**REQUESTS["verdict_one"], **IDENT, **EXT})
    err = next(dd for e, dd in _frames(r) if e == "error")
    assert (err["code"], err["knob"]) == ("truncated_empty", "EXTERNAL_LLM_MAX_TOKENS")
    assert w.seen[0]["submits"][-1]["finish_reason"] == "length"


def test_외부_심의의_의장_결정문_절단_경고는_EXTERNAL_LLM_MAX_TOKENS_를_가리킨다(monkeypatch):
    from test_external_llm_delib import _panel

    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    d._evid_cache.clear()

    def brain(_head, system, human):              # 의장만 출력 상한(32,000 토큰)을 넘게 쓴다 — 엔진이 그 자리에서 자른다
        out = delib_respond(system, human)
        return out + "\n" + "덧붙인 긴 설명 " * 9000 if system.startswith("당신은 심의체 의장입니다") else out

    try:
        events, *_ = _panel(monkeypatch, brain=brain)
    finally:
        d._evid_cache.clear()
    cut = [dd for e, dd in events if e == "warning" and dd.get("code") == "chair_output_cut"]
    assert cut and cut[0]["knob"] == "EXTERNAL_LLM_MAX_TOKENS", [dd for e, dd in events if e == "warning"]


# ── L5. 외부 잡의 닫힌 free_tools 는 호출자의 선택과 따로 ─────────────────────────────
def test_외부_잡의_닫힌_free_tools_는_따로_적혀_GLM_이어하기가_물려받지_않는다(monkeypatch, tmp_path):
    """탐침 p10 — 외부 잡의 기록에 닫힌 free_tools=0 이 호출자의 값처럼 적혀, llm 을 이어받지 않는 이어하기(GLM)가 그 0 을
    물려받았다(호출자는 free_tools 를 보내지 않았거나 1 을 보냈다)."""
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    seen = []

    async def fake_run(app, q, groups, opts, user, pat, history, **kw):
        seen.append((dict(opts or {}), kw))
        yield d._sse("delib", {"kind": "decision", "text": "결정문"})
        yield d._sse("done", {})

    monkeypatch.setattr(d, "run_deliberation", fake_run)
    monkeypatch.setattr(m, "_APP", SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None,
                                                                          external_board=x.Board(x.Settings()))))
    hdr = ctx_of("first%2Btag@x.com")

    async def go(advanced):
        out = await m.deliberate_start("화두", job="risk-review", advanced=advanced, ctx=hdr)
        await delib_jobs._TASKS[out["job_id"]]
        cont = await m.deliberate_continue(out["job_id"], "사람 의견", ctx=hdr)
        await delib_jobs._TASKS[cont["job_id"]]
        return delib_jobs._JOBS[out["job_id"]], delib_jobs._JOBS[cont["job_id"]]

    prev, nxt = asyncio.run(go({**EXT, "free_tools": 1}))
    assert (prev["opts"]["llm"], prev["opts"]["free_tools"], prev["opts"]["external_forced"]) == (
        "external", 1, {"free_tools": 0})
    (first_opts, first_kw), (cont_opts, cont_kw) = seen
    assert first_kw == {"owner": "first+tag@x.com"} and d._resolve_opts(first_opts).free_tools == 0, "외부 잡은 닫혀 돈다"
    assert cont_kw == {} and "llm" not in cont_opts and cont_opts["free_tools"] == 1
    assert d._resolve_opts(cont_opts).free_tools == 1 and "external_forced" not in nxt["opts"]
    seen.clear()
    prev, _nxt = asyncio.run(go(dict(EXT)))                   # 호출자가 free_tools 를 보내지 않았다
    assert "free_tools" not in prev["opts"] and prev["opts"]["external_forced"] == {"free_tools": 0}
    assert "free_tools" not in seen[1][0], "보내지 않은 값을 이어하기가 지어냈다"


# ── L6. 켰는데 설정이 틀리면 닫힌다 ───────────────────────────────────────────────
@pytest.mark.parametrize("name,raw,why", [
    ("EXTERNAL_LLM_CTX_TOKENS", "많이", "정수로 읽지 못했다"),
    ("EXTERNAL_LLM_CTX_TOKENS", "200000.0", "정수로 읽지 못했다"),
    ("EXTERNAL_LLM_CTX_TOKENS", "512", "1024 미만"),
    ("EXTERNAL_LLM_MAX_TOKENS", "0", "1 미만"),
    ("EXTERNAL_LLM_WAIT_S", "0", "0.001 미만"),
    ("EXTERNAL_LLM_WAIT_S", "nan", "유한한 수가 아니다"),
    ("EXTERNAL_LLM_ANSWER_S", "inf", "유한한 수가 아니다"),
    ("EXTERNAL_LLM_ANSWER_S", "-1", "0.001 미만"),
    ("EXTERNAL_LLM_MAX_INFLIGHT", "0", "1 미만"),
    ("EXTERNAL_LLM_MAX_INFLIGHT", "여덟", "정수로 읽지 못했다"),
])
def test_켰는데_값_하나라도_못_받으면_기본값으로_메우지_않고_닫힌다(name, raw, why):
    cfg = x.config({"EXTERNAL_LLM_ENABLED": "1", name: raw})
    assert cfg.settings is None and cfg.knob == name, cfg
    assert why in cfg.error and "EXTERNAL_LLM_ENABLED=1 이지만" in cfg.error, cfg.error
    if raw in ("nan", "inf"):
        assert "미만" not in cfg.error, "nan·inf 를 '미만' 이라고 말했다"
    assert x.is_open({"EXTERNAL_LLM_ENABLED": "1", name: raw}) is False
    assert m._register_external(FastMCP(name="bad"), env={"EXTERNAL_LLM_ENABLED": "1", name: raw}) is False
    assert x.config({name: raw}) == x.Config(None), "꺼진 서버는 값을 보지 않는다"


def test_시간_한도_사슬이_깨지면_닫힌다():
    env = {"EXTERNAL_LLM_ENABLED": "1", "EXTERNAL_LLM_WAIT_S": "900"}      # 2×900 + 2×1200 = 4,200 ≥ 2×1800 + 8
    cfg = x.config(env)
    assert cfg.settings is None and cfg.knob == "EXTERNAL_LLM_WAIT_S · EXTERNAL_LLM_ANSWER_S"
    assert "4,200초" in cfg.error and "3,608초" in cfg.error
    assert x.config({**env, "DELIB_TIMEOUT_S": "3000"}).settings.wait_s == 900, "심의 한도가 넉넉한 박스는 열린다"
    assert x.config({**env, "DELIB_TIMEOUT_S": "0"}).settings is not None, "심의 한도를 끈 박스는 견줄 바깥 한도가 없다"
    assert x.config({"EXTERNAL_LLM_ENABLED": "1", "DELIB_TIMEOUT_S": "1000"}).settings is None, "기본값도 사슬을 본다"
    assert x.config({"EXTERNAL_LLM_ENABLED": "1"}).settings == x.Settings()


def test_설정_오류로_닫힌_서버는_꺼진_서버처럼_거절하고_까닭과_손잡이를_싣는다(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(x, "_warned", set())
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    monkeypatch.setenv("EXTERNAL_LLM_WAIT_S", "nan")
    monkeypatch.setenv("DRAIN_STATE_PATH", str(tmp_path / "drain.json"))
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"]), raising=False)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    monkeypatch.setattr(a.app.state, "external_board", None, raising=False)
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setattr(a, "_delib_human", lambda: 0)
    monkeypatch.setattr(m, "_APP", a.app)
    c = TestClient(a.app)
    h = c.get("/health").json()
    assert h["capabilities"] == ["card_review", "drain"] and "external_llm" not in h
    r = c.post("/card-review/run", json={**REQUESTS["verdict_one"], **IDENT, **EXT})
    body = r.json()
    assert (r.status_code, body["code"], body["knob"]) == (409, "external_llm_disabled", "EXTERNAL_LLM_WAIT_S")
    assert "유한한 수가 아니다" in body["message"]
    refused = d._external_refusal(a.app, dict(EXT), OWNER)
    assert (refused["code"], refused["knob"]) == ("external_llm_disabled", "EXTERNAL_LLM_WAIT_S")
    with pytest.raises(ValueError, match="EXTERNAL_LLM_ENABLED"):
        asyncio.run(m.review_task_next(ctx=ctx_of(OWNER)))
    assert getattr(a.app.state, "external_board", None) is None
    out = capsys.readouterr().out
    assert "⛔" in out and "EXTERNAL_LLM_WAIT_S" in out and out.count("⛔") == 1, "기동 오류 줄은 한 번만"


@pytest.mark.parametrize("delib_env", [{}, {"DELIB_TIMEOUT_S": "900", "DELIB_LLM_MAX_RETRIES": "2"},
                                       {"DELIB_TIMEOUT_S": "많이"}, {"DELIB_LLM_MAX_RETRIES": "-3"},
                                       {"DELIB_TIMEOUT_S": "0"}])
def test_사슬의_바깥_한도는_기동한_심의_LLM_에_실제로_걸린_한도와_같다(monkeypatch, delib_env):
    """config 는 기동 전(도구 등록 — import 때)에도 판정하므로 env 로 셈한다 — 기동 절차(app.lifespan)가 만드는 심의 LLM 의 한도와
    같아야 한다."""
    from test_llm_limits import _boot

    st = _boot(monkeypatch, **delib_env)
    read, tries = d._llm_limit(st.delib_llm)
    assert x.delib_call_worst_s() == (tries * read + 8 if read > 0 else None)


def test_기동_절차는_사슬이_깨진_설정이면_줄을_만들지_않고_오류_줄을_남긴다(monkeypatch, capsys):
    from test_llm_limits import _boot

    monkeypatch.setattr(x, "_warned", set())
    st = _boot(monkeypatch, EXTERNAL_LLM_ENABLED="1", DELIB_TIMEOUT_S="1000")
    assert getattr(st, "external_board", None) is None
    out = capsys.readouterr().out
    assert "⛔" in out and "외부 응답자 길을 열지 않는다" in out
    st = _boot(monkeypatch, EXTERNAL_LLM_ENABLED="1")
    assert isinstance(st.external_board, x.Board) and "켜짐" in capsys.readouterr().out


# ── L8. 주인 칸 정리 ─────────────────────────────────────────────────────────────
def test_과제도_없이_하루_넘게_조용한_주인_칸은_지운다(stub):
    """탐침 p01 owners_growth — 워크플로 쪽 호출마다 신원별 칸이 생기고 지워지지 않았다(2만 신원이면 2만 칸)."""
    for i in range(2000):
        stub.board.claim(f"u{i}@x.com")
    assert len(stub.board._owners) == 2000

    async def go():
        stub.clock.t += x.OWNER_IDLE_S - 1               # 아직 하루가 안 됐다
        stub.board.sweep()
        young = len(stub.board._owners)
        stub.clock.t += x._PRUNE_EVERY_S + 2
        call = _call(stub.app)
        await _until(lambda: stub.board.counts()["queued"] == 1)
        kept = set(stub.board._owners)
        h = await m.review_task_next(ctx=ctx_of(OWNER))
        await m.review_task_submit(h["task_id"], h["lease"], "답", final=True, ctx=ctx_of(OWNER))
        await call
        return young, kept

    young, kept = asyncio.run(go())
    assert young == 2000 and kept == {OWNER}
