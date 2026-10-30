# 챗 턴의 잡 상태 조회를 코드가 기다리는지 — 상태 도구 알아보기 · 상태 읽기 · 이 턴에 건 잡 id · 고리 · 안내 · 진행 줄 · 꼬리 면제 · /health(HA-28)
"""HWAXPortal docs/request_261030.md HA-28 · 설계 docs/request-1030/design-ha28.md 7절(시험 1 ~ 18) · 맞물림 design-integration.md(C-3 · C-4 · C-5).

DW-02 후속 — run_checklist 를 건 뒤 LLM 이 get_task 를 26번 불렀고 끝을 못 보고 저장된 옛 결과로 답했다. 상태 도구가 '도는 중' 을 주면 이 턴에
건 잡이거나 같은 호출의 되풀이일 때만 도구 래퍼 안에서 코드가 다시 조회하며 기다리고, LLM 에는 마지막 결과와 '[코드 기다림 — …]' 한 줄만 간다.

가짜 상태 도구는 진짜 어댑터 꼴이다 — StructuredTool(response_format="content_and_artifact", args_schema 는 JSON 스키마 사전)이
([{"type": "text", "text": <앱이 실제로 주는 JSON>}], None) 을 돌려준다. 손잡이는 모듈 속성으로 줄인다(루트 conftest 가 기본으로 끈다).

  실행:  .venv/bin/python -m pytest tests/test_status_wait.py -q
"""
import asyncio
import io
import json
import sys
import time
import types
import urllib.request
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from langchain_core.callbacks import AsyncCallbackHandler  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402
from langchain_core.tools.base import _get_runnable_config_param  # noqa: E402
from pydantic import Field  # noqa: E402

import app as a  # noqa: E402
import status_wait as sw  # noqa: E402
from test_chat_loop_fit import _agent  # noqa: E402 — 앱이 실제로 만드는 길(_agent_for) + 게이트웨이 스텁
from test_health_contract import client  # noqa: E402,F401 — /health 고정물

_REAL_TOOLS_MAP = a._tools_map      # 자동 고정물이 막기 전의 것 — 지도를 받아 캐시에 싣는 길을 보는 시험만 쓴다
_BOOT_MAX_S = sw.MAX_S              # import 때 읽은 값 — 루트 conftest 가 끈 값이어야 한다(아래 고정물이 켜기 전)

# 앱이 실제로 주는 꼴(설계 2.6 · 받은 답 outbound.md)
ODB_START = {"task_id": "t-77", "kind": "checklist", "job_id": "j1", "status": "queued", "progress": 0.0}


def ODB_RUN(p=0.45, tid="t1"):
    return {"task_id": tid, "kind": "checklist", "status": "running", "progress": p, "message": "running rules"}


ODB_DONE = {"task_id": "t1", "kind": "checklist", "status": "done", "progress": 1.0, "message": "done", "result": {"violations": 3}}
STC_SHAPE = {"ok": True, "job_id": 3, "squeue": {"JOBID": "3", "ST": "R"}}       # SmartTwinMCP job_status — 맨 위 상태 칸이 없다


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    """기다림을 켜고 짧게 돈다 — 계수는 시험마다 0 에서, 도구 → 앱 캐시는 비운 채(게이트웨이에 묻지 않게)로 시작한다."""
    monkeypatch.setattr(sw, "MAX_S", 5.0)
    monkeypatch.setattr(sw, "POLL_S", 0.02)
    monkeypatch.setattr(sw, "TELL_S", 0.0)
    monkeypatch.setattr(sw, "EXTRA", frozenset())
    monkeypatch.setattr(sw, "_STAT", {"waits": 0, "ended": 0, "capped": 0, "failed": 0})
    _cache(monkeypatch, {})


def _cache(monkeypatch, mapping, routine_read=None, labels=None):
    """도구 → 앱 캐시와 routine_read 를 앉힌다 — 묻는 함수도 막는다(test_request_1029 의 _cache 꼴, 떠 있는 게이트웨이에 실제로 묻지 않게)."""
    apps = {k: {"app": k, "label": v} for k, v in (labels or {}).items()}
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "map", mapping)
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "routine_read", routine_read)
    monkeypatch.setitem(a._APPS_CACHE, "apps", apps)
    monkeypatch.setattr(a, "_tools_map", lambda: mapping)
    monkeypatch.setattr(a, "_gw_apps", lambda: apps)


def _fake(name, respond, props=None):
    """진짜 어댑터 꼴의 가짜 도구 — respond(인자, 몇 번째) 가 JSON 꼴 값 · 글 · 예외 · 코루틴을 준다. (도구, 받은 인자 목록)."""
    calls: list = []

    async def call(**kw):
        calls.append(dict(kw))
        r = respond(dict(kw), len(calls))
        if asyncio.iscoroutine(r):
            r = await r
        if isinstance(r, BaseException):
            raise r
        return ([{"type": "text", "text": r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)}], None)

    schema = {"type": "object", "properties": props or {"task_id": {"type": "string"}}}
    return StructuredTool(name=name, description="d", coroutine=call, args_schema=schema,
                          response_format="content_and_artifact"), calls


def _seq(*payloads):
    return lambda kw, n: payloads[min(n, len(payloads)) - 1]


def _chat(tool):
    return a._prep_tool(tool, arg_defaults=a._CHAT_ARG_DEFAULTS, chat=True)


@contextmanager
def _turn(born=(), ids=None):
    """챗 턴의 상태를 손으로 세운다 — 상태 기다림 · 도구별 호출 수(· 유령 ID 출처)."""
    st = sw.new_turn()
    st["born"] |= set(born)
    toks = [(a._turn_status_wait, a._turn_status_wait.set(st)), (a._turn_tool_calls, a._turn_tool_calls.set(Counter()))]
    if ids is not None:
        toks.append((a._turn_ids, a._turn_ids.set(set(ids))))
    try:
        yield st
    finally:
        for var, tok in reversed(toks):
            var.reset(tok)


def _run(coro):
    return asyncio.run(coro)


# ── 순수 판정 ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_1_상태_도구는_원래_이름_목록이고_routine_read_울타리를_지난다(monkeypatch):
    for name, key in (("heaxstep_forge_job_status", "heax-step_forge"), ("smarttwinmcp_job_status", "smart-twin-mcp"),
                      ("get_task", None), ("docforge_status", "heax-doc_forge"), ("get_job", "heax-kooremapper_mcp"),
                      ("get_job_status", "smarttwinexplorer"), ("doe_status", "abaqusmcp"), ("study_get", None)):
        assert sw.is_status_tool(name, key, None), name
    for name, key in (("job_log", None), ("list_jobs", None), ("heaxkooremapper_mcp_system_status", "heax-kooremapper_mcp"),
                      ("deliberate_status", "hwax-deliberation"), ("run_job", None), ("get_task_report", None),
                      ("heaxstep_forge_job_status", None)):
        assert not sw.is_status_tool(name, key, None), name
    assert not sw.is_status_tool("get_task", None, frozenset({"other"})), "게이트웨이가 읽기로 확인하지 않은 도구는 열지 않는다"
    assert sw.is_status_tool("get_task", None, frozenset({"get_task"}))
    monkeypatch.setattr(sw, "EXTRA", frozenset({"render_status"}))
    assert not sw.is_status_tool("render_status", None, None), "박스 덧붙임은 울타리(routine_read)가 있을 때만 먹는다"
    assert sw.is_status_tool("render_status", None, frozenset({"render_status"}))
    monkeypatch.setattr(sw, "MAX_S", 0.0)
    assert not sw.is_status_tool("get_task", None, None) and not sw.is_status_tool("render_status", None, frozenset({"render_status"}))


def test_2_결과에서_상태_낱말_진행률_한_줄을_읽는다():
    def js(v):
        return sw.job_state(v if isinstance(v, str) else json.dumps(v))
    assert js({"task_id": "t1", "status": "running", "progress": 0.45, "message": "running rules"}) == {
        "phase": "run", "word": "running", "progress": 45, "detail": "running rules"}
    assert js({"id": "j", "status": "pending", "progress": 0}) == {"phase": "run", "word": "pending", "progress": 0, "detail": ""}
    assert js({"status": "done", "progress": 100, "stage": "S7"})["phase"] == "end"
    for v in ({"status": "succeeded"}, {"status": "canceled"}, {"state": "COMPLETED"}, {"state": "HALTED"}, {"state": "PARTIAL"}):
        assert js(v)["phase"] == "end", v
    for v in ({"job_id": "x", "state": "RUNNING"}, {"state": "SUBMITTED"}, {"status": "ok", "state": "running"},
              {"job": {"status": "queued"}}, {"status": "running", "cancel_requested": True}, {"state": "license-wait"}):
        assert js(v)["phase"] == "run", v
    for v in ({"state": "GENERATED"}, {"ok": False, "error": "잡이 없다", "status": 404}, [{"status": "running"}],
              "✖ 도구 get_task 호출 실패: 502", STC_SHAPE, {"status": "unknown"}, {"status": "paused"}, "", "{깨진"):
        assert js(v) is None, v
    assert js({"ok": False, "error": "권한 없음", "status": 403, "state": "running"}) is None, \
        "숫자 status 는 도구 오류다(DocForge 답) — 다른 칸에 낱말이 있어도 기다리지 않는다"
    assert js({"status": "running", "progress": 37})["progress"] == 37 and js({"status": "running", "progress": None})["progress"] is None
    assert js({"status": "running", "progress": None, "pct": 12})["progress"] == 12 and js({"status": "running", "progress": 1.0})["progress"] == 100
    assert len(js({"status": "running", "message": "가 " * 100})["detail"]) == 80


def test_3_잡_시작_결과에서_이_턴에_건_잡의_id_를_배운다():
    assert sw.born_ids(json.dumps({"job_id": "j-1", "kind": "graph", "status": "pending"})) == {"j-1"}
    assert sw.born_ids(json.dumps({"task_id": "t1", "kind": "checklist", "job_id": "j1", "status": "queued", "progress": 0.0})) == {"t1", "j1"}
    assert sw.born_ids(json.dumps({"id": "01H", "status": "queued"})) == {"01H"}
    assert sw.born_ids(json.dumps({"job_id": 42, "status": "submitted", "flag_id": True})) == {"42"}, "정수는 글로, 불은 id 가 아니다"
    assert sw.born_ids(json.dumps({"jobs": [{"id": "x", "status": "running"}]})) == set(), "목록 속 잡은 이 턴에 건 잡이 아니다"
    assert sw.born_ids(json.dumps({"job_id": "z", "status": "done"})) == set(), "끝난 잡"
    big = json.dumps({"job_id": "j", "status": "queued", "pad": "x" * sw.BORN_TEXT_MAX})
    assert len(big) > sw.BORN_TEXT_MAX and sw.born_ids(big) == set()


def test_id_칸_꼴은_앱의_유령_ID_게이트와_같다():
    assert sw._ID_KEY.pattern == a._ID_ARG_RE.pattern, "두 군데에 따로 쓰면 한쪽만 고쳐질 때 조용히 어긋난다"


def test_루트_conftest_가_기다림을_끈다():
    """C-5 — 켜진 채면 상태 도구가 '도는 중' 을 준 남의 턴 시험이 10초씩 자고 꼬리 · 반복 경고 글이 바뀐다. 기다림을 보는 시험은 제 손으로 켠다."""
    assert _BOOT_MAX_S == 0.0


# ── 도구 래퍼 ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_4_이_턴에_건_잡의_상태는_끝날_때까지_코드가_기다려_끝난_결과만_넘긴다():
    tool, calls = _fake("get_task", _seq(ODB_RUN(0.3), ODB_RUN(0.6), ODB_DONE))
    tool = _chat(tool)
    with _turn(born={"t1"}) as st:
        out = _run(tool.ainvoke({"task_id": "t1"}))
    assert len(calls) == 3 and all(c == {"task_id": "t1"} for c in calls)
    assert '"status": "done"' in out and "[코드 기다림 — " in out and "끝난 상태(done)" in out, out
    assert "번째 불렀다" not in out
    assert 0 < st["spent_s"] < 1.0
    assert sw.health()["waits"] == 1 and sw.health()["ended"] == 1


def test_5_상태만_물은_첫_호출은_지금_상태로_같은_호출의_되풀이는_기다린다():
    per: Counter = Counter()

    def respond(kw, _n):
        per[kw.get("task_id")] += 1
        return ODB_RUN(0.3, kw.get("task_id")) if per[kw.get("task_id")] < 3 else ODB_DONE

    tool, calls = _fake("get_task", respond)
    tool = _chat(tool)
    with _turn():
        first = _run(tool.ainvoke({"task_id": "t5"}))
        assert len(calls) == 1 and "한 번 더 부르라" in first and '"status": "running"' in first, first
        assert "진행률 30%" in first and "최대 5초" in first, "남은 몫을 말한다"
        again = _run(tool.ainvoke({"task_id": "t5"}))
        assert len(calls) == 3 and "끝난 상태(done)" in again, "같은 원래 이름 · 같은 인자의 두 번째 호출부터 기다린다"
        other = _run(tool.ainvoke({"task_id": "t2"}))
    assert len(calls) == 4 and "한 번 더 부르라" in other, "인자가 다르면 첫 호출처럼 기다리지 않는다"


def test_6_턴의_기다림_몫을_다_쓰면_상한_안내를_달고_다음_호출은_기다리지_않는다(monkeypatch):
    monkeypatch.setattr(sw, "MAX_S", 0.1)
    tool, calls = _fake("get_task", _seq(ODB_RUN(0.8)))
    tool = _chat(tool)
    with _turn(born={"t1"}) as st:
        out = _run(tool.ainvoke({"task_id": "t1"}))
        n = len(calls)
        assert n >= 2 and "아직 끝나지 않았다" in out and "이 턴에는 더 기다리지 않는다" in out and "예전에 저장된 결과" in out, out
        assert "진행률 80%" in out and st["spent_s"] >= 0.1
        again = _run(tool.ainvoke({"task_id": "t1"}))
    assert len(calls) == n + 1 and "이미 다 써" in again and "예전 결과로 대신 답하지 말고" in again, again
    assert sw.health()["capped"] == 1 and sw.health()["waits"] == 1


def test_7_다시_조회가_실패하거나_답하지_않으면_마지막_결과로_멈춘다(monkeypatch):
    tool, calls = _fake("get_task", lambda kw, n: ODB_RUN(0.45) if n == 1 else RuntimeError("502"))
    tool = _chat(tool)
    with _turn(born={"t1"}):
        out = _run(tool.ainvoke({"task_id": "t1"}))
    assert len(calls) == 2 and '"status": "running"' in out and "다시 조회가 실패했다(RuntimeError: 502)" in out, out
    assert "끝난 결과가 아니다" in out and sw.health()["failed"] == 1

    async def slow():
        await asyncio.sleep(10)
        return ODB_DONE

    monkeypatch.setattr(sw, "MAX_S", 0.2)
    tool2, calls2 = _fake("get_task", lambda kw, n: ODB_RUN(0.45) if n == 1 else slow())
    tool2 = _chat(tool2)
    t0 = time.monotonic()
    with _turn(born={"t1"}):
        out2 = _run(tool2.ainvoke({"task_id": "t1"}))
    assert time.monotonic() - t0 < 0.5, "다시 조회 한 번의 한도는 남은 몫이다"
    assert len(calls2) == 2 and "다시 조회가 실패했다" in out2 and "답하지 않았다" in out2, out2


def test_8_턴_상태가_없거나_챗_래퍼가_아니면_기다리지_않는다():
    tool, calls = _fake("get_task", _seq(ODB_RUN(0.3), ODB_DONE))
    tool = _chat(tool)
    out = _run(tool.ainvoke({"task_id": "t1"}))
    assert len(calls) == 1 and "[코드 기다림" not in out, "턴 상태 없음(심의 · 카탈로그 길)"
    raw, calls2 = _fake("get_task", _seq(ODB_RUN(0.3), ODB_DONE))
    raw = a._prep_tool(raw)
    with _turn(born={"t1"}) as st:
        out2 = _run(raw.ainvoke({"task_id": "t1"}))
        out3 = _run(raw.ainvoke({"task_id": "t1"}))
    assert len(calls2) == 2 and "[코드 기다림" not in out2 + out3, "chat=False 래퍼는 결과를 코드가 읽는다 — 기다리지도 안내를 달지도 않는다"
    assert st["spent_s"] == 0.0


def test_C3_잡_시작은_래퍼_종류와_상관없이_배우고_그_잡의_상태는_첫_호출부터_기다린다():
    """확정 종결 · 새어 나온 호출 직접 실행 · 승인 실행은 chat=False 래퍼(deliberation._tools_by_name)로 잡을 건다(design-integration C-3)."""
    start, _ = _fake("run_job", _seq({"job_id": "j-1", "kind": "graph", "status": "pending"}), {"project_id": {"type": "string"}})
    start = a._prep_tool(start)
    status, calls = _fake("job_status", _seq({"job_id": "j-1", "run_id": "r-9", "status": "running", "progress": 40},
                                             {"job_id": "j-1", "status": "done", "progress": 100}), {"job_id": {"type": "string"}})
    status = _chat(status)
    with _turn() as st:
        _run(start.ainvoke({"project_id": "p"}))
        assert st["born"] == {"j-1"}
        out = _run(status.ainvoke({"job_id": "j-1"}))
    assert len(calls) == 2 and "끝난 상태(done)" in out
    assert st["born"] == {"j-1"}, "상태 도구의 결과에서는 배우지 않는다(r-9)"


def test_9_꼬리는_상태를_읽은_상태_도구만_면제하고_꺼져_있으면_종전_그대로(monkeypatch):
    done, _ = _fake("get_task", _seq(ODB_DONE))
    done = _chat(done)
    with _turn():
        outs = [_run(done.ainvoke({"task_id": "t1"})) for _ in range(5)]
    assert all("번째 불렀다" not in o for o in outs), "상태 도구의 결과는 다시 부르면 바뀐다 — '결과가 같다' 꼬리는 틀린 말이다"
    stc, _ = _fake("job_status", _seq(STC_SHAPE), {"job_id": {"type": "integer"}})
    stc = _chat(stc)
    with _turn():
        outs = [_run(stc.ainvoke({"job_id": 3})) for _ in range(4)]
    assert "4번째 불렀다" in outs[3] and "[코드 기다림" not in outs[3], "상태를 못 읽는 꼴은 코드가 기다려 줄 수 없다 — 꼬리가 루프를 막는다"
    monkeypatch.setattr(sw, "MAX_S", 0.0)
    run, calls = _fake("get_task", _seq(ODB_RUN(0.3)))
    run = _chat(run)
    with _turn(born={"t1"}):
        outs = [_run(run.ainvoke({"task_id": "t1"})) for _ in range(4)]
    assert len(calls) == 4 and all("[코드 기다림" not in o for o in outs), "꺼져 있으면 기다리지 않고 안내도 없다"
    assert "4번째 불렀다" in outs[3], "꺼져 있으면 꼬리도 종전 그대로"


@pytest.mark.parametrize("args", [{"task_id": "t1"}, json.dumps({"task_id": "t1"})])
def test_10_invoke_tool_경유도_안쪽_이름_인자로_기다리고_다시_조회는_같은_경유_호출이다(monkeypatch, args):
    _cache(monkeypatch, {"invoke_tool": "_gateway"})
    inv, calls = _fake("invoke_tool", lambda kw, n: ODB_RUN(0.2) if n < 3 else ODB_DONE,
                       {"name": {"type": "string"}, "arguments": {"type": "object"}})
    inv = _chat(inv)
    with _turn(born={"t1"}):
        out = _run(inv.ainvoke({"name": "get_task", "arguments": args}))
    assert len(calls) == 3 and all(c == {"name": "get_task", "arguments": args} for c in calls), calls
    assert "끝난 상태(done)" in out and "[코드 기다림 — get_task 를" in out


def test_11_유령_ID_게이트가_먼저다():
    tool, calls = _fake("get_task", _seq(ODB_RUN()), {"registry_id": {"type": "integer"}})
    tool = _chat(tool)
    with _turn(born={"98765"}, ids={1, 2}):
        out = _run(tool.ainvoke({"registry_id": 98765}))
    assert calls == [] and a._PHANTOM_ID_MARK in out and "[코드 기다림" not in out


def test_12_다시_조회는_관문을_다시_지나지_않고_실행_설정이_인자로_새지_않는다():
    def respond(kw, n):
        a._turn_ids.get().discard(77)        # 첫 호출 뒤 출처가 사라져도 다시 조회는 게이트를 다시 지나지 않는다
        return ODB_RUN(0.5) if n < 3 else ODB_DONE

    tool, calls = _fake("get_task", respond, {"task_id": {"type": "string"}, "registry_id": {"type": "integer"}})
    tool = _chat(tool)
    with _turn(born={"t1"}, ids={77}):
        out = _run(tool.ainvoke({"task_id": "t1", "registry_id": 77}))
    assert len(calls) == 3 and all(c == {"task_id": "t1", "registry_id": 77} for c in calls), calls
    assert a._PHANTOM_ID_MARK not in out and "끝난 상태(done)" in out


def test_13_부모_실행_없이_불러도_오류_없이_기다린다(capsys):
    class _Seen(AsyncCallbackHandler):
        def __init__(self):
            self.custom: list = []

        async def on_custom_event(self, name, data, **_kw):
            self.custom.append((name, data))

    h = _Seen()
    tool, calls = _fake("get_task", _seq(ODB_RUN(0.3), ODB_DONE))
    tool = _chat(tool)
    with _turn(born={"t1"}):
        out = _run(tool.ainvoke({"task_id": "t1"}, config={"callbacks": [h]}))
    assert len(calls) == 2 and "끝난 상태(done)" in out
    assert h.custom == [], "부모 실행이 없으면 사건을 낼 곳이 없다 — 기다림은 그대로 간다"
    log = capsys.readouterr().out
    assert "[status-wait] 진행 줄을 내지 못했다: RuntimeError" in log and "[status-wait] get_task: end" in log, log


def test_C4_멈춤_술어는_잠_앞과_다시_조회_앞에서_보고_참이면_접고_계수에_넣지_않는다(monkeypatch):
    """확인 카드(HA-29)가 선 턴에 같은 단계의 상태 도구가 기다리면 카드가 최대 600초 늦게 선다 — 술어가 참이면 다음 조회 앞에서 접는다."""
    monkeypatch.setattr(sw, "POLL_S", 0.05)
    frames: list = []

    async def emit(d):
        frames.append(sw.frame(d))

    def go(stop):
        calls: list = []

        async def again():
            calls.append(1)
            return ([{"type": "text", "text": json.dumps(ODB_RUN(0.5))}], None)

        turn = sw.new_turn()
        turn["born"].add("t1")
        first = ([{"type": "text", "text": json.dumps(ODB_RUN(0.4))}], None)
        return _run(sw.handle("get_task", None, None, {"task_id": "t1"}, first, again, turn, chat=True, emit=emit, stop=stop)), calls

    asked: Counter = Counter()

    def third():            # 잠 앞(거짓) → 다시 조회 앞(거짓) → 조회 한 번 → 다음 잠 앞(참)
        asked["n"] += 1
        return asked["n"] >= 3

    (last, note, polled), calls = go(third)
    assert polled and len(calls) == 1 and asked["n"] == 3, "두 자리 모두에서 본다 — 한 자리만 보면 한 번 더 조회한다"
    assert "기다리다 접었다" in note and "끝난 결과가 아니다" in note, note
    assert json.loads(last[0][0]["text"])["progress"] == 0.5, "마지막 조회 결과를 넘긴다"
    t0 = time.monotonic()
    (_, _, _), calls2 = go(lambda: time.monotonic() - t0 > 0.01)
    assert calls2 == [], "잠 동안 참이 되면 다시 조회하지 않는다"
    assert sw.health() == {"on": True, "poll_s": 0.05, "max_s": 5.0, "waits": 2, "ended": 0, "capped": 0, "failed": 0}, \
        "멈춤은 끝 · 상한 · 실패 어디에도 세지 않는다"
    assert [f["wait"]["phase"] for f in frames][-1] == "stop" and all(f["tool"] is None for f in frames)


def test_꼴이_바뀌거나_숫자_status_가_오면_멈춘다():
    tool, calls = _fake("docforge_status", _seq({"status": "running", "progress": 30, "stage": "S3"},
                                                {"ok": False, "error": "권한 없음", "status": 403}), {"job_id": {"type": "string"}})
    tool = _chat(tool)
    with _turn(born={"d1"}):
        out = _run(tool.ainvoke({"job_id": "d1"}))
    assert len(calls) == 2 and "진행 상태를 더 읽을 수 없어 멈췄다" in out and '"status": 403' in out, out
    assert sw.health()["failed"] == 1


def test_프레임은_tool_이_비고_진행_줄_한_줄이다():
    st = {"word": "running", "progress": 45, "detail": "running rules"}
    f = sw.frame(sw._data("wait", "get_task", st, 90, 600, 9))
    assert f["tool"] is None and f["step"] == "잡을 기다리는 중 — get_task · running 45% · running rules · 1분 30초 지남"
    assert set(f["wait"]) == {"phase", "tool", "state", "progress", "detail", "elapsed_s", "max_s", "polls"}
    assert sw.frame(sw._data("start", "get_task", {"word": "running", "progress": 30}, 0, 600, 0))["step"].endswith("최대 10분")
    assert sw.frame(sw._data("end", "get_task", {"word": "done"}, 130, 600, 13))["step"] == "잡이 끝났습니다 — get_task · done · 2분 10초 기다림"
    assert "다시 보지 못해" in sw.frame(sw._data("fail", "get_task", st, 3, 600, 1, "RuntimeError: 502"))["step"]


def test_도구_지도의_routine_read_를_캐시에_싣는다(monkeypatch):
    """옛 게이트웨이(그 칸 없음)는 None('모름'), 새 게이트웨이는 노출 이름의 frozenset 이다."""
    bodies = [{"map": {"get_task": "heax-odb"}, "routine_read": ["get_task", 3]}, {"map": {"get_task": "heax-odb"}}]

    def urlopen(url, timeout=None):
        return io.BytesIO(json.dumps(bodies.pop(0)).encode())

    monkeypatch.setattr(a, "_tools_map", _REAL_TOOLS_MAP)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(a, "_TOOLS_MAP_CACHE", {"at": 0.0, "map": {}, "areas": {}, "area_meta": {}, "routine_read": None})
    a._tools_map()
    assert a._TOOLS_MAP_CACHE["routine_read"] == frozenset({"get_task"}) and a._status_tool("get_task")
    a._TOOLS_MAP_CACHE["at"] = 0.0
    a._tools_map()
    assert a._TOOLS_MAP_CACHE["routine_read"] is None and a._status_tool("get_task")
    monkeypatch.setattr(a, "_TOOLS_MAP_CACHE", {"at": 0.0, "map": {}, "areas": {}, "area_meta": {}})
    assert a._status_tool("get_task"), "그 칸 없는 사전으로 통째로 바꿔 끼워도(tests/test_tools_map_retry) 읽는다"


@pytest.fixture(scope="module")
def mcp_gw():
    """진짜 MCP 서버(게이트웨이 대역) — get_task 가 잡마다 세 번째 조회에 끝난다. 잡 'slow' 의 두 번째 조회는 2초 뒤에 답한다
    (tests/test_mcp_call_timeout 의 꼴 — 빈 포트, 떠 있는 9110 과 겹치지 않는다)."""
    import socket
    import threading

    import uvicorn
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("hwax-status-wait-test", stateless_http=True, streamable_http_path="/")
    hits: Counter = Counter()

    @mcp.tool()
    async def get_task(task_id: str = "") -> str:
        """잡 상태(ODB 허브 꼴)."""
        hits[task_id] += 1
        if task_id == "slow" and hits[task_id] == 2:
            await asyncio.sleep(2.0)
        return json.dumps({"task_id": task_id, "status": "done" if hits[task_id] >= 3 else "running", "progress": 0.3 * hits[task_id]})

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "시험용 MCP 서버가 뜨지 않았다"
    yield {"gateway": {"url": f"http://127.0.0.1:{port}/", "transport": "streamable_http"}}, hits
    server.should_exit = True
    thread.join(timeout=10)


def test_진짜_MCP_어댑터로도_다시_조회하고_한도에_걸린_조회는_취소된다(monkeypatch, mcp_gw):
    """다시 조회는 어댑터의 호출(부를 때마다 세션을 연다)을 asyncio.wait_for 로 감싼다 — 한도에 걸려 취소돼도 anyio 의 취소 범위가 다른 태스크에서
    닫히는 오류 없이 '답하지 않았다' 로 끝나고, 같은 도구를 다음에 부르면 그대로 돈다."""
    from langchain_mcp_adapters.client import MultiServerMCPClient
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    conns, hits = mcp_gw
    tool = _chat(next(t for t in asyncio.run(MultiServerMCPClient(conns).get_tools()) if t.name == "get_task"))
    assert tool.response_format == "content_and_artifact"
    with _turn(born={"t1"}):
        out = _run(tool.ainvoke({"task_id": "t1"}))
    assert hits["t1"] == 3 and '"status": "done"' in out and "끝난 상태(done)" in out, out
    monkeypatch.setattr(sw, "MAX_S", 0.3)
    t0 = time.monotonic()
    with _turn(born={"slow"}):
        slow = _run(tool.ainvoke({"task_id": "slow"}))
    took = time.monotonic() - t0
    assert hits["slow"] == 2 and took < 1.5, took
    assert "다시 조회가 실패했다" in slow and "답하지 않았다" in slow and '"status": "running"' in slow, slow
    monkeypatch.setattr(sw, "MAX_S", 5.0)
    with _turn(born={"t2"}):
        after = _run(tool.ainvoke({"task_id": "t2"}))
    assert hits["t2"] == 3 and "끝난 상태(done)" in after, "취소된 조회 뒤에도 같은 도구가 그대로 돈다"


def test_18_실행_설정은_RunnableConfig_인자로_들어온다():
    tool, _ = _fake("get_task", _seq(ODB_DONE))
    assert _get_runnable_config_param(_chat(tool).coroutine) == "_hwax_rc", "기본값을 None 으로 두면 3.10 이 Optional 로 읽어 넣지 않는다"


def test_17_health_에_기다림_칸이_실리고_기다리면_는다(client):
    h = client.get("/health").json()["status_wait"]
    assert h == {"on": True, "poll_s": 0.02, "max_s": 5.0, "waits": 0, "ended": 0, "capped": 0, "failed": 0}
    tool, _ = _fake("get_task", _seq(ODB_RUN(0.3), ODB_DONE))
    tool = _chat(tool)
    with _turn(born={"t1"}):
        _run(tool.ainvoke({"task_id": "t1"}))
    h = client.get("/health").json()["status_wait"]
    assert (h["waits"], h["ended"], h["capped"], h["failed"]) == (1, 1, 0, 0)


# ── 실제 턴(_agent_stream) ───────────────────────────────────────────────────────────────────────────────────────────

class _Script(BaseChatModel):
    """대본대로 부르는 모델 — steps 의 원소는 [(도구, 인자) …](도구 호출) 이거나 글(답)이다. 받은 메시지를 호출마다 적는다."""

    steps: list = Field(default_factory=list)
    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "status-wait-script"

    def bind_tools(self, tools, **_kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **_kw):
        self.seen.append(list(messages))
        step = self.steps[min(len(self.seen), len(self.steps)) - 1]
        msg = (AIMessage(content=step) if isinstance(step, str) else
               AIMessage(content="", tool_calls=[{"name": n, "args": ar, "id": f"c{len(self.seen)}_{i}"} for i, (n, ar) in enumerate(step)]))
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(self, messages, stop=None, run_manager=None, **kw):
        """챗 스트림 길(on_chat_model_stream)로도 같은 답을 한 조각으로 낸다 — 운영 기본이 스트리밍이다(test_chat_loop_fit 의 _LoopLLM 꼴)."""
        msg = self._generate(messages, stop, run_manager, **kw).generations[0].message
        yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content, tool_call_chunks=[
            {"name": c["name"], "args": json.dumps(c["args"], ensure_ascii=False), "id": c["id"], "index": i}
            for i, c in enumerate(msg.tool_calls)]))


@pytest.fixture
def _ctx(monkeypatch):
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})


def _build(monkeypatch, llm, tools):
    agent = _agent(monkeypatch, llm, tools=tools)

    async def built(*_a, **_k):
        return agent

    monkeypatch.setattr(a, "_agent_for", built)
    return a.ChatRequest(message="ODB 체크리스트 돌려서 결과 알려줘", groups=["portal-admin"], history=[])


def _sse(chunks) -> list:
    out = []
    for c in chunks:
        ev, data = a._sse_parse(c)
        if ev:
            out.append((ev, data))
    return out


def _turn_events(req) -> list:
    async def go():
        return [c async for c in a._agent_stream(a.app, req)]
    return _sse(asyncio.run(go()))


def test_14_실제_턴_잡을_걸고_상태를_한_번_부르면_코드가_끝까지_기다리고_모델은_세_번만(monkeypatch, _ctx):
    start, _ = _fake("run_checklist", _seq(ODB_START), {"job_id": {"type": "string"}})
    status, calls = _fake("get_task", _seq(ODB_RUN(0.3, "t-77"), ODB_RUN(0.6, "t-77"), {**ODB_DONE, "task_id": "t-77"}))
    llm = _Script(steps=[[("run_checklist", {"job_id": "j1"})], [("get_task", {"task_id": "t-77"})], "체크리스트 위반은 3건입니다."])
    evs = _turn_events(_build(monkeypatch, llm, (start, status)))
    assert len(llm.seen) == 3 and len(calls) == 3, (len(llm.seen), calls)
    steps = [(e, d) for e, d in evs if e == "status"]
    i0 = next(i for i, (_, d) in enumerate(steps) if d.get("step") == "도구 호출: get_task")
    i1 = next(i for i, (_, d) in enumerate(steps) if d.get("step") == "도구 완료: get_task")
    assert sum(d.get("step") == "도구 호출: get_task" for _, d in steps) == 1 and sum(d.get("step") == "도구 완료: get_task" for _, d in steps) == 1
    waits = [d for _, d in steps[i0 + 1:i1] if "wait" in d]
    assert [w["wait"]["phase"] for w in waits][0] == "start" and waits[-1]["wait"]["phase"] == "end", waits
    assert {w["wait"]["phase"] for w in waits} <= {"start", "wait", "end"} and all(w["tool"] is None for w in waits)
    assert waits[-1]["step"].startswith("잡이 끝났습니다 — get_task · done")
    assert not [d for _, d in steps if "같은 호출 반복 감지" in str(d.get("step"))]
    last_tool = [m for m in llm.seen[2] if isinstance(m, ToolMessage)][-1].content
    assert '"status": "done"' in last_tool and "끝난 상태(done)" in last_tool, last_tool
    assert [e for e, _ in evs][-2:] == ["result", "done"]


def test_15_기다리는_중_턴을_끊으면_기다림도_멈춘다(monkeypatch, _ctx):
    monkeypatch.setattr(sw, "POLL_S", 0.05)
    start, _ = _fake("run_checklist", _seq(ODB_START), {"job_id": {"type": "string"}})
    status, calls = _fake("get_task", _seq(ODB_RUN(0.3, "t-77")))
    llm = _Script(steps=[[("run_checklist", {"job_id": "j1"})], [("get_task", {"task_id": "t-77"})], "답"])
    req = _build(monkeypatch, llm, (start, status))

    async def go():
        seen = asyncio.Event()

        async def consume():
            async for c in a._agent_stream(a.app, req):
                ev, data = a._sse_parse(c)
                if ev == "status" and (data.get("wait") or {}).get("phase") == "wait":
                    seen.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(seen.wait(), 10)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        n = len(calls)
        await asyncio.sleep(6 * sw.POLL_S)
        return n

    n = asyncio.run(go())
    assert n >= 2 and len(calls) == n, "중지는 기다림만 멈춘다(잡은 앱에서 계속 돈다)"
    assert len(llm.seen) == 2, "끊긴 턴은 모델을 더 부르지 않는다"


def test_16_상태만_물으면_한_번_조회하고_모델이_지금_상태로_답한다(monkeypatch, _ctx):
    status, calls = _fake("get_task", _seq(ODB_RUN(0.3, "t5")))
    llm = _Script(steps=[[("get_task", {"task_id": "t5"})], "아직 30% 진행 중입니다."])
    evs = _turn_events(_build(monkeypatch, llm, (status,)))
    assert len(llm.seen) == 2 and len(calls) == 1
    last_tool = [m for m in llm.seen[1] if isinstance(m, ToolMessage)][-1].content
    assert "한 번 더 부르라" in last_tool and "진행률 30%" in last_tool, last_tool
    assert not [d for e, d in evs if e == "status" and "wait" in d], "기다리지 않았으니 진행 줄도 없다"


@pytest.mark.parametrize("on", [True, False])
def test_상태_도구의_같은_호출_세_번은_반복_경고를_내지_않는다_꺼지면_종전_그대로(monkeypatch, _ctx, on):
    if not on:
        monkeypatch.setattr(sw, "MAX_S", 0.0)
    status, calls = _fake("get_task", _seq({**ODB_DONE, "task_id": "t9"}))
    step = [("get_task", {"task_id": "t9"})]
    llm = _Script(steps=[step, step, step, "끝났습니다."])
    evs = _turn_events(_build(monkeypatch, llm, (status,)))
    assert len(calls) == 3
    warned = [d for e, d in evs if e == "status" and "같은 호출 반복 감지" in str(d.get("step"))]
    assert (warned == []) is on, warned
