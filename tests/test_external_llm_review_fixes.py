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
    """실제 앱의 카드 대조 라우트 — GLM 관문 상한 2(탐침 p02), 시계가 멈춘 줄(일꾼이 없어도 시험이 시계를
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
    board = x.Board(x.Settings(ctx_tokens=CTX, max_tokens=MT), clock=clock, tick=0.02)
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
