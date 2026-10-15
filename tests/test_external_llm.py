# 외부 응답자(external_llm) — 과제 줄·주인만·기다림/답 한도·취소·max_tokens·쪽 읽기·나눠 내기·켜짐/꺼짐(도구 등록·거절·/health)을 가짜 일꾼으로 본다
#
# 실제 Claude 는 부르지 않는다(사용자 결정 X-D1). 일꾼은 tests/fakes/worker.py — 실제 MCP 도구 함수(review_task_next·read·submit)를
# 부른다. 한도는 시계를 바꿔 끼운 줄(Board(clock=…))로 본다 — 실제로 300초를 기다리지 않는다.
#
#   실행:  .venv/bin/python -m pytest tests/test_external_llm.py -q
import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

import app as a  # noqa: E402
import external_llm as x  # noqa: E402
import mcp_pages  # noqa: E402
import mcp_server as m  # noqa: E402
from fakes.llm import FakeLLM  # noqa: E402
from fakes.worker import FakeWorker, ctx_of  # noqa: E402

OWNER, OTHER = "owner@x.com", "other@x.com"


class Clock:
    """바꿔 끼우는 단조 시계 — 시험이 앞으로 민다."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")


@pytest.fixture
def stub(on, monkeypatch):
    """외부 응답자가 켜진 앱 대역 — 과제 줄은 시계를 바꿔 끼운 것이다. MCP 도구도 이 앱을 본다."""
    clock = Clock()
    board = x.Board(x.Settings(wait_s=300, answer_s=1200), clock=clock, tick=0.02)
    app = SimpleNamespace(state=SimpleNamespace(external_board=board, delib_llm=None, llm=object()))
    monkeypatch.setattr(m, "_APP", app)
    return SimpleNamespace(app=app, board=board, clock=clock)


def _model(app, owner=OWNER, kind="verdict"):
    return x.model_for(app, owner=owner, kind=kind)


async def _until(cond, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("기다린 상태가 오지 않았다")


# ── 한 과제가 집기·읽기·내기로 답이 된다 ───────────────────────────────────────────
def test_과제_하나가_집기_읽기_내기로_모델의_답이_된다(stub):
    def brain(head, system, human):
        return f"답:{system}|{human}"

    async def go():
        w = FakeWorker(OWNER, brain=brain, chunk=5)
        serve = asyncio.ensure_future(w.serve())
        msg = await _model(stub.app).ainvoke([("system", "시스템 글"), ("human", "사람 글 «데이터»")])
        w.stop()
        await serve
        return msg, w

    msg, w = asyncio.run(go())
    assert msg.content == "답:시스템 글|사람 글 «데이터»"
    assert msg.response_metadata["finish_reason"] == "stop"
    assert msg.response_metadata.get("token_usage") is None and msg.usage_metadata is None, "사용량을 지어내지 않는다"
    head = w.seen[0]["head"]
    n_sys, n_hum = len("시스템 글"), len("사람 글 «데이터»")
    assert (head["kind"], head["claim"], head["chars"]) == ("verdict", 1, n_sys + n_hum)
    assert head["parts"] == [{"role": "system", "start": 0, "chars": n_sys},
                             {"role": "human", "start": n_sys, "chars": n_hum}]
    assert head["max_tokens"] == x.MAX_TOKENS_DEFAULT and head["answer_s"] == 1200
    assert len(w.seen[0]["submits"]) > 1 and w.seen[0]["submits"][-1]["done"] is True, "답을 조각내 냈다"
    assert stub.board.counts() == {"queued": 0, "claimed": 0}


def test_bind_의_max_tokens_가_과제에_실리고_넘는_답은_그_자리에서_잘린다(stub):
    """max_tokens 를 지킨다 — 묶은 값이 과제 머리에 가고(일꾼이 본다), 엔진 토큰 어림으로 넘는 답은 잘려 length 로 온다."""
    long = "가" * 200                                   # 한글 200자 ≈ 190토큰(1.05자/토큰)

    async def go():
        w = FakeWorker(OWNER, brain=lambda *_: long)
        serve = asyncio.ensure_future(w.serve())
        cut = await _model(stub.app).bind(max_tokens=50).ainvoke([("system", "s"), ("human", "h")])
        full = await _model(stub.app).ainvoke([("system", "s"), ("human", "h")])
        w.stop()
        await serve
        return cut, full, w

    cut, full, w = asyncio.run(go())
    assert [r["head"]["max_tokens"] for r in w.seen] == [50, x.MAX_TOKENS_DEFAULT]
    assert cut.response_metadata["finish_reason"] == "length" and 0 < len(cut.content) < len(long)
    assert a._est_tokens_all(cut.content) <= 50 < a._est_tokens_all(cut.content + "가")
    assert w.seen[0]["submits"][-1]["cut_chars"] == len(long) - len(cut.content)
    assert (full.content, full.response_metadata["finish_reason"]) == (long, "stop")


def test_도구를_묶을_수_없다_evidence_only():
    with pytest.raises(NotImplementedError, match="evidence_only"):
        x.ExternalChatModel(board=x.Board()).bind_tools([])


# ── 주인만 ────────────────────────────────────────────────────────────────────
def test_남의_과제는_보이지도_집히지도_읽히지도_내지지도_않고_없는_id_와_같은_답이다(stub):
    other = ctx_of(OTHER)

    async def go():
        call = asyncio.ensure_future(_model(stub.app).ainvoke([("system", "s"), ("human", "h")]))
        await _until(lambda: stub.board.counts()["queued"] == 1)
        tid = next(iter(stub.board._tasks))
        empty = await m.review_task_next(ctx=other)
        mine = await m.review_task_next(ctx=ctx_of(OWNER))           # 주인이 집은 뒤에도 남은 남의 손을 막는다
        lease = mine["lease"]                                        # 주인의 임대를 알아도 남은 못 쓴다
        got = {}
        for name, fn in (("read", lambda t: m.review_task_read(t, lease, 0, ctx=other)),
                         ("submit", lambda t: m.review_task_submit(t, lease, "가로챈 답", offset=0, final=True, ctx=other))):
            got[name] = (await fn(tid), await fn(f"xt-{uuid.uuid4().hex[:16]}"))
        done = await m.review_task_submit(tid, lease, "주인의 답", offset=0, final=True, ctx=ctx_of(OWNER))
        return empty, mine, got, done, await call

    empty, mine, got, done, msg = asyncio.run(go())
    assert empty["empty"] is True and empty["claimed"] == 0, "남의 과제가 보였다"
    assert mine["task_id"] and mine["claim"] == 1
    for name, (real, missing) in got.items():
        assert real == missing, f"{name}: 남의 과제와 없는 과제의 답이 다르다 — {real} · {missing}"
        assert real["code"] == "task_not_found"
    assert done["done"] is True and msg.content == "주인의 답", "남의 내기가 답을 바꿨다"


def test_게이트웨이가_퍼센트_인코딩한_신원도_같은_주인이다(stub):
    """게이트웨이는 x-hwax-user 를 quote(user, safe="@.") 로 싣는다 — '+' 가 든 이메일은 %2B 로 오고 대소문자는 섞일 수 있다."""
    async def go():
        call = asyncio.ensure_future(x.model_for(stub.app, owner="First+Tag@X.com", kind="verdict")
                                     .ainvoke([("system", "s"), ("human", "h")]))
        await _until(lambda: stub.board.counts()["queued"] == 1)
        enc = ctx_of("first%2Btag@x.com")
        head = await m.review_task_next(ctx=enc)
        await m.review_task_submit(head["task_id"], head["lease"], "인코딩된 신원의 답", final=True, ctx=enc)
        return await call

    assert asyncio.run(go()).content == "인코딩된 신원의 답"


def test_신원_없는_호출과_꺼진_서버는_도구_오류다(stub, monkeypatch):
    anon = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={})))
    with pytest.raises(ValueError, match="신원 없는 호출"):
        asyncio.run(m.review_task_next(ctx=anon))
    monkeypatch.delenv("EXTERNAL_LLM_ENABLED")
    with pytest.raises(ValueError, match="EXTERNAL_LLM_ENABLED"):
        asyncio.run(m.review_task_next(ctx=ctx_of(OWNER)))


# ── 기다림 한도(external_worker_absent) ─────────────────────────────────────────
def test_아무도_집지_않으면_WAIT_S_뒤에_absent_이고_그_뒤로는_바로_absent_다가_일꾼이_오면_풀린다(stub):
    async def go():
        call = asyncio.ensure_future(_model(stub.app).ainvoke([("system", "s"), ("human", "h")]))
        await _until(lambda: stub.board.counts()["queued"] == 1)
        stub.clock.t += 299
        await asyncio.sleep(0.1)
        assert not call.done(), "WAIT_S 전에 끝났다"
        stub.clock.t += 2
        with pytest.raises(x.ExternalLLMError) as first:
            await call
        with pytest.raises(x.ExternalLLMError) as fast:                    # 줄 세우지 않고 바로
            await _model(stub.app).ainvoke([("system", "s"), ("human", "h")])
        assert stub.board.counts()["queued"] == 0
        await m.review_task_next(ctx=ctx_of(OWNER))                       # 워크플로가 다시 붙었다
        again = asyncio.ensure_future(_model(stub.app).ainvoke([("system", "s"), ("human", "h")]))
        await _until(lambda: stub.board.counts()["queued"] == 1)
        head = await m.review_task_next(ctx=ctx_of(OWNER))
        await m.review_task_submit(head["task_id"], head["lease"], "다시 붙은 답", final=True, ctx=ctx_of(OWNER))
        return first.value, fast.value, await again

    first, fast, msg = asyncio.run(go())
    assert (first.code, first.knob, fast.code) == ("external_worker_absent", "EXTERNAL_LLM_WAIT_S", "external_worker_absent")
    assert "300초" in first.message and "다시 붙을 때까지" in fast.message
    assert msg.content == "다시 붙은 답"


def test_주인의_워크플로가_다른_과제를_쓰는_동안에는_기다리는_과제가_absent_로_끝나지_않는다(stub):
    """바쁜 것은 없는 것이 아니다 — 집힌 과제가 있는 동안 기다리는 과제의 '집히지 않은 시간' 을 세지 않는다."""
    async def go():
        busy = asyncio.ensure_future(_model(stub.app).ainvoke([("system", "1"), ("human", "첫 과제")]))
        await _until(lambda: stub.board.counts()["queued"] == 1)
        head = await m.review_task_next(ctx=ctx_of(OWNER))
        waiting = asyncio.ensure_future(_model(stub.app).ainvoke([("system", "2"), ("human", "둘째 과제")]))
        await _until(lambda: stub.board.counts()["queued"] == 1)
        stub.clock.t += 1000                                 # WAIT_S(300)를 훨씬 넘겼지만 첫 과제를 쓰는 중이다(답 한도 1200 안)
        await asyncio.sleep(0.1)
        alive = not waiting.done()
        await m.review_task_submit(head["task_id"], head["lease"], "첫 답", final=True, ctx=ctx_of(OWNER))
        h2 = await m.review_task_next(ctx=ctx_of(OWNER))
        await m.review_task_submit(h2["task_id"], h2["lease"], "둘째 답", final=True, ctx=ctx_of(OWNER))
        return alive, (await busy).content, (await waiting).content

    assert asyncio.run(go()) == (True, "첫 답", "둘째 답")


# ── 답 한도(external_answer_timeout) — 한 번 되돌리고 그다음 실패 ───────────────────────
def test_집고_답이_없으면_한_번_줄로_되돌리고_두_번째는_answer_timeout_이다(stub):
    async def go():
        call = asyncio.ensure_future(_model(stub.app).ainvoke([("system", "s"), ("human", "h")]))
        await _until(lambda: stub.board.counts()["queued"] == 1)
        h1 = await m.review_task_next(ctx=ctx_of(OWNER))
        part = await m.review_task_submit(h1["task_id"], h1["lease"], "반쯤", offset=0, ctx=ctx_of(OWNER))
        stub.clock.t += 1201
        await asyncio.sleep(0.1)
        stale = await m.review_task_submit(h1["task_id"], h1["lease"], "나머지", offset=part["received_chars"], final=True,
                                           ctx=ctx_of(OWNER))
        h2 = await m.review_task_next(ctx=ctx_of(OWNER))
        stub.clock.t += 1201
        with pytest.raises(x.ExternalLLMError) as err:
            await call
        gone = await m.review_task_read(h1["task_id"], h2["lease"], 0, ctx=ctx_of(OWNER))
        return part, stale, h1, h2, err.value, gone

    part, stale, h1, h2, err, gone = asyncio.run(go())
    assert part == {"ok": True, "task_id": h1["task_id"], "done": False, "received_chars": 2}
    assert stale["code"] == "not_claimed", "되돌린 과제의 늦은 답을 받았다"
    assert (h2["task_id"], h2["claim"]) == (h1["task_id"], 2), "같은 과제가 claim 2 로 다시 집혀야 한다"
    assert (err.code, err.knob) == ("external_answer_timeout", "EXTERNAL_LLM_ANSWER_S") and "1,200초" in err.message
    assert gone["code"] == "task_not_found"


# ── 취소 ──────────────────────────────────────────────────────────────────────
def test_부른_쪽이_취소하면_과제가_줄에서_빠지고_일꾼은_없는_과제의_답을_받는다(stub):
    async def go():
        call = asyncio.ensure_future(_model(stub.app).ainvoke([("system", "s"), ("human", "h")]))
        await _until(lambda: stub.board.counts()["queued"] == 1)
        head = await m.review_task_next(ctx=ctx_of(OWNER))
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        return head, stub.board.counts(), await m.review_task_submit(head["task_id"], head["lease"], "늦은 답", final=True,
                                                                    ctx=ctx_of(OWNER))

    head, counts, late = asyncio.run(go())
    assert counts == {"queued": 0, "claimed": 0}
    assert late["code"] == "task_not_found"


# ── 나눠 내기 · 쪽 읽기 ─────────────────────────────────────────────────────────
def test_나눠_내기의_offset_규칙(stub):
    async def go():
        call = asyncio.ensure_future(_model(stub.app).ainvoke([("system", "s"), ("human", "h")]))
        await _until(lambda: stub.board.counts()["queued"] == 1)
        head = await m.review_task_next(ctx=ctx_of(OWNER))
        tid, lease = head["task_id"], head["lease"]
        sub = lambda text, **k: m.review_task_submit(tid, lease, text, ctx=ctx_of(OWNER), **k)  # noqa: E731
        out = [await sub("가나", offset=0), await sub("다라"),               # 안 주면 끝에 붙인다
               await sub("마", offset=2),                                    # 받은 길이(4)가 아니다 — 맞추라고 알린다
               await sub("가나다라", offset=0),                               # 0 이면 처음부터(같은 조각을 다시 낸 것도 이 길)
               await sub("x" * (x.ANSWER_HARD_MAX + 1)),                    # 메모리 상한
               await sub("마바", offset=4, final=True)]
        return out, await call

    out, msg = asyncio.run(go())
    assert [r.get("received_chars") for r in out[:4]] == [2, 4, 4, 4]
    assert out[2]["code"] == "offset_mismatch" and out[4]["code"] == "answer_too_large"
    assert out[5]["done"] is True and msg.content == "가나다라마바"


def test_긴_프롬프트는_20000자_안의_쪽으로_나뉘고_이어_붙이면_그대로다(stub):
    """훑기 프롬프트는 12만 자쯤이다. 줄바꿈·따옴표·역슬래시는 JSON 에서 두 자가 되므로 잰 글 길이로 맞춘다."""
    human = ("가나다 \"인용\" 줄\n" * 6000 + "\\경로\\" * 5000 + "끝«»" * 15000)[:130_000]
    assert len(human) == 130_000
    system = "시스템\n" * 500

    async def go():
        w = FakeWorker(OWNER, brain=lambda h, s, u: "ok")
        serve = asyncio.ensure_future(w.serve())
        await _model(stub.app, kind="sweep").ainvoke([("system", system), ("human", human)])
        w.stop()
        await serve
        return w.seen[0]

    rec = asyncio.run(go())
    assert (rec["system"], rec["human"]) == (system, human)
    assert len(rec["pages"]) >= 6
    page_max = min(x.READ_PAGE_MAX, mcp_pages.PAGE_CHARS)

    async def sizes():
        call = asyncio.ensure_future(_model(stub.app, kind="sweep").ainvoke([("system", system), ("human", human)]))
        await _until(lambda: stub.board.counts()["queued"] == 1)
        head = await m.review_task_next(ctx=ctx_of(OWNER))
        out, off = [], 0
        while off is not None:
            p = await m.review_task_read(head["task_id"], head["lease"], off, ctx=ctx_of(OWNER))
            out.append(len(mcp_pages.wire(p)))
            off = p["next_offset"]
        await m.review_task_submit(head["task_id"], head["lease"], "", final=True, ctx=ctx_of(OWNER))
        await call
        return out

    got = asyncio.run(sizes())
    assert max(got) <= page_max and min(got[:-1]) > page_max * 0.5, got
    bad = asyncio.run(m.review_task_read("xt-none", "lease", -1, ctx=ctx_of(OWNER)))
    assert bad["code"] == "task_not_found"


# ── 설정 ──────────────────────────────────────────────────────────────────────
def test_설정과_스위치():
    s = x.Settings.from_env({})
    assert (s.ctx_tokens, s.max_tokens, s.wait_s, s.answer_s) == (200_000, 32_000, 300.0, 1200.0)
    assert s.call_worst_s == 3000.0
    bad = x.Settings.from_env({"EXTERNAL_LLM_CTX_TOKENS": "많이", "EXTERNAL_LLM_WAIT_S": "0", "EXTERNAL_LLM_ANSWER_S": "-1",
                               "EXTERNAL_LLM_MAX_TOKENS": "64000"})
    assert (bad.ctx_tokens, bad.wait_s, bad.answer_s, bad.max_tokens) == (200_000, 300.0, 1200.0, 64_000), \
        "한도를 끄는 값은 없다 — 0·음수·못 읽는 값은 기본값"
    for v, want in (("1", True), ("on", True), ("TRUE", True), ("", False), ("0", False), ("off", False),
                    ("1 # 켬", False), ("yes please", False)):
        assert x.enabled({"EXTERNAL_LLM_ENABLED": v}) is want, v
    assert x.enabled({}) is False, "기본은 꺼짐"


def test_기본값의_과제_하나_최악은_심의_LLM_논리_호출_1회보다_작다_안쪽이_먼저():
    """바깥 한도(리스크 앱 호출 벽시계·패널 벽시계, 포털·nginx)는 LLM 논리 호출 1회의 최악((1+1)×1800+8)에 맞춰져 있다."""
    s = x.Settings()
    assert s.call_worst_s < 2 * 1800 + 8
    assert "⚠" not in x.startup_note(s, 1800.0, 2)
    assert "⚠" in x.startup_note(x.Settings(wait_s=900, answer_s=1200), 1800.0, 2)


# ── 도구 등록 ─────────────────────────────────────────────────────────────────
TOOLS = {"review_task_next", "review_task_read", "review_task_submit"}


def test_꺼진_서버에는_과제_도구가_없고_켜진_서버에만_셋이_올라간다():
    names = {t.name for t in asyncio.run(m.mcp.list_tools())}
    assert not names & TOOLS, "시험 세션은 꺼진 서버다 — 그런데 과제 도구가 올라가 있다"
    off = FastMCP(name="off")
    assert m._register_external(off, env={}) is False and not asyncio.run(off.list_tools())
    on = FastMCP(name="on")
    assert m._register_external(on, env={"EXTERNAL_LLM_ENABLED": "1"}) is True
    listed = {t.name: t for t in asyncio.run(on.list_tools())}
    assert set(listed) == TOOLS
    props = {n: set(t.inputSchema["properties"]) for n, t in listed.items()}
    assert props == {"review_task_next": {"wait_s"}, "review_task_read": {"task_id", "lease", "offset"},
                     "review_task_submit": {"task_id", "lease", "text", "offset", "final"}}
    assert {n: set(t.inputSchema.get("required") or []) for n, t in listed.items()} == {
        "review_task_next": set(), "review_task_read": {"task_id", "lease"},
        "review_task_submit": {"task_id", "lease", "text"}}, "lease 는 빼고 부를 수 없다"
    desc = " ".join(t.description for t in listed.values())
    for must in ("JSON Lines", "max_tokens", "lease", "consent_external", "risk_spec",
                 "반출", "동의", "external_worker_absent", "external_answer_timeout",
                 "task_not_found", "offset_mismatch"):
        assert must in desc, must


# ── /health — 꺼짐은 종전 그대로, 켜짐은 계약대로 ─────────────────────────────────
@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("DRAIN_STATE_PATH", str(tmp_path / "drain.json"))
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"]), raising=False)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    monkeypatch.setattr(a.app.state, "external_board", None, raising=False)
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setattr(a, "_delib_human", lambda: 0)
    return TestClient(a.app)


def test_꺼진_서버의_health_는_종전_그대로다(client):
    h = client.get("/health").json()
    assert h["capabilities"] == ["card_review", "drain"] and "external_llm" not in h


def test_켜진_서버의_health_는_capabilities_와_external_llm_칸을_싣는다(client, on):
    h = client.get("/health").json()
    assert h["capabilities"] == ["card_review", "drain", "external_llm"]
    xh = h["external_llm"]
    assert set(xh) == {"ctx_tokens", "max_tokens", "wait_s", "answer_s", "call_worst_s", "queued", "claimed", "evid_budget"}
    assert (xh["ctx_tokens"], xh["max_tokens"], xh["queued"], xh["claimed"]) == (200_000, 32_000, 0, 0)
    assert (xh["wait_s"], xh["answer_s"], xh["call_worst_s"]) == (300.0, 1200.0, 3000.0)
    assert 2_000 <= xh["evid_budget"] < h["evid_budget"], "외부 창으로 셈한 근거 몫은 1백만 토큰 창의 몫보다 작다"
    assert x.ctx_override.get() is None, "/health 가 셈한 외부 창이 밖으로 샜다"
