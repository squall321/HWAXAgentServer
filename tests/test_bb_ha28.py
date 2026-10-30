# HA-28 잡 상태 조회를 코드가 기다리는지 설계서만 보고 재는 블랙박스 시험 — 이름 판정 · 상태 읽기 · born · 도구 래퍼 · 실제 챗 턴 · /health
"""HWAXPortal docs/request-1030/design-ha28.md 7절의 시험 1 ~ 18 과 design-integration.md 의 C-3(born 은 래퍼 종류와 상관없이) · C-5(각 행
시험은 제 손으로 켠다), outbound.md 의 앱 답(StepForge · DocForge 의 실제 상태 낱말, 숫자 status 는 도구 오류)을 구현 속을 보지 않고 잰다.
구현은 다른 손이 짓는다(wt1030/ha28) — 속을 읽고 맞춘 시험은 속과 함께 틀린다.

믿는 이음매는 설계가 이름을 박은 것뿐이다 — status_wait 의 is_status_tool(노출 이름, 앱 키, routine_read) · job_state(글) · born_ids(글) ·
frame(사건 자료) · health() · EVENT · MAX_S · POLL_S · TELL_S · POLL_CALL_MAX_S, app 의 _turn_status_wait · _TOOLS_MAP_CACHE["routine_read"] ·
_prep_tool · _turn_tool_calls · _turn_ids · /health 의 status_wait, 손잡이 CHAT_STATUS_WAIT_MAX_S · CHAT_STATUS_WAIT_POLL_S · CHAT_STATUS_TOOLS.
설계가 꼴을 열어 둔 곳은 맨 위 도우미 하나씩에 가뒀다 — 가정 ① job_state 는 모름이면 None 이고 그 밖의 반환 꼴은 보지 않는다(갈래는 래퍼가
기다리는지로, 진행률 · 설명은 사건 자료로 잰다), ② 턴 상태는 설계 7절이 손으로 세우라고 적은 사전(_state), ③ 사건 자료 · 화면 프레임의 칸
이름은 설계 4.6 그대로(_call_in_run · frame), ④ 실제 챗 턴은 앱이 만드는 길(test_chat_loop_fit 의 _agent)이다.

가짜는 진짜 꼴로 — 상태 도구는 MCP 어댑터가 만드는 꼴(StructuredTool · JSON 스키마 args_schema · content_and_artifact 의 ([글 블록], None),
글 블록은 어댑터가 쓰는 create_text_block)이고 결과 글은 앱이 실제로 내는 꼴(design-ha28 2.6 · outbound 의 답)이다. 손잡이는 모듈 속성으로
줄인다(POLL_S 0.02 · TELL_S 0) — 루트 conftest 가 CHAT_STATUS_WAIT_MAX_S=0 을 걸어도(design-integration C-5) 이 파일은 제 손으로 켠다.
매달리는 구현이 시험을 붙잡지 않게 코루틴은 모두 _run 으로 돌린다(따로 된 스레드 · 시간 한도 — 그 도우미의 설명).

  실행:  .venv/bin/python -m pytest tests/test_bb_ha28.py -q
"""
import asyncio
import contextlib
import contextvars
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Annotated

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.callbacks import AsyncCallbackHandler  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage  # noqa: E402
from langchain_core.messages.content import create_text_block  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult  # noqa: E402
from langchain_core.runnables import RunnableLambda  # noqa: E402
from langchain_core.tools import InjectedToolArg, StructuredTool  # noqa: E402
from langchain_core.tools.base import _get_runnable_config_param  # noqa: E402
from pydantic import Field  # noqa: E402

import app as a  # noqa: E402
from test_chat_loop_fit import _agent  # noqa: E402 — 앱의 _agent_for + 게이트웨이 스텁(설계 7절 머리)

_REAL_TOOLS_MAP = a._tools_map          # 자동 고정물이 스텁으로 바꾸기 전의 진짜 — 지도 캐시 시험이 부른다

# 설계 4.3 의 낱말표 그대로 — 도는 중 · 끝, 모름은 '그 밖 전부'(표에 적힌 예)
_RUN = ("queued pending running submitted started starting in_progress processing caching canary configuring completing "
        "cancelling canceling license_wait").split()
_END = ("done succeeded success completed complete finished ready failed failure error cancelled canceled timeout timed_out "
        "interrupted killed aborted partial halted node_fail preempted out_of_memory boot_fail deadline resubmitted stopped "
        "expired rejected").split()
_ODD = "created generated paused suspended held waiting unknown".split()


def _odb(status, progress=None, message=None, tid="t1"):
    """ODB 허브 get_task 의 꼴 — status queued · running · done · error, progress 0 ~ 1 실수, message(design-ha28 2.6)."""
    d = {"task_id": tid, "kind": "checklist", "status": status}
    if progress is not None:
        d["progress"] = progress
    if message is not None:
        d["message"] = message
    return d


RUN = _odb("running", 0.3, "running rules")
RUN2 = _odb("running", 0.6, "running rules")
DONE = _odb("done", 1.0, "finished")
QUEUED = {"task_id": "t1", "kind": "checklist", "job_id": "j1", "status": "queued", "progress": 0.0}   # ODB run_checklist 의 답


# ── 가짜 앱 도구 ───────────────────────────────────────────────────────────────────────────────────────────────
class _Slow:
    """대본 칸 — s 초 잠든 뒤 then 을 낸다(답하지 않는 다시 조회)."""

    def __init__(self, s, then):
        self.s, self.then = s, then


class _Fake:
    """앱 도구의 가짜 — 대본(칸 목록)을 부를 때마다 하나씩 내고 다 쓰면 마지막 칸을 되풀이한다. by 를 주면 그 인자 값마다 따로 대본이다.
    칸은 사전(JSON 글로) · 글(그대로) · 예외(던진다) · _Slow 다. 받은 인자는 부를 때마다 적는다."""

    def __init__(self, script, by=None):
        self.script, self.by, self.calls, self._n = script, by, [], Counter()

    async def __call__(self, **kw):
        self.calls.append(dict(kw))
        k = str(kw.get(self.by)) if self.by else ""
        seq = self.script[k] if self.by else self.script
        item = seq[min(self._n[k], len(seq) - 1)]
        self._n[k] += 1
        if isinstance(item, _Slow):
            await asyncio.sleep(item.s)
            item = item.then
        if isinstance(item, BaseException):
            raise item
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return [create_text_block(text=text)], None      # 어댑터의 (내용 블록 목록, 아티팩트) — 블록은 어댑터가 만드는 꼴 그대로


def _mcp_tool(name, fake, props=None):
    """MCP 어댑터가 만드는 꼴의 도구(langchain_mcp_adapters 0.3.0 — JSON 스키마 args_schema · content_and_artifact · call_tool 꼴 코루틴)."""
    async def call_tool(runtime: Annotated[object | None, InjectedToolArg()] = None, **arguments):
        return await fake(**arguments)
    return StructuredTool(name=name, description=f"{name} 가짜", coroutine=call_tool,
                          args_schema={"type": "object", "properties": dict(props or {"task_id": {"type": "string"}})},
                          response_format="content_and_artifact")


def _chat_tool(name, fake, props=None):
    """챗이 묶는 꼴로 감싼다 — _agent_for 가 쓰는 인자 그대로(arg_defaults=_CHAT_ARG_DEFAULTS · chat=True)."""
    return a._prep_tool(_mcp_tool(name, fake, props), arg_defaults=a._CHAT_ARG_DEFAULTS, chat=True)


def _run(coro, timeout=20.0):
    """코루틴을 새 이벤트 루프에서 돌린다 — 지금 문맥(손으로 세운 턴 상태)을 그대로 넘기고, 끝나지 않으면 timeout 초 뒤 붉게 끝낸다.
    파이썬 3.10 의 wait_for 는 안쪽이 막 끝난 때 온 취소를 삼킬 수 있어, 끝나지 않는 기다림 고리는 wait_for 로 묶어도 시험을 붙잡았다
    (변이 실험 — 상한 검사를 뺀 고리가 600초를 넘겼다). 그래서 따로 된 스레드에서 돌리고 기다리는 쪽만 끊는다."""
    box = {}

    def go():
        try:
            box["v"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 — 시험 스레드의 예외는 그대로 다시 올린다
            box["e"] = exc
    th = threading.Thread(target=contextvars.copy_context().run, args=(go,), daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        pytest.fail(f"{timeout:.0f}초 안에 끝나지 않았다 — 기다림이 매달렸다")
    if "e" in box:
        raise box["e"]
    return box["v"]


def _call(tool, args, timeout=20.0):
    """부모 실행 없이 부른다 — 사건을 받을 곳이 없다(설계 7절 13)."""
    return _run(tool.ainvoke(args), timeout)


class _Catch(AsyncCallbackHandler):
    """부모 실행의 사용자 사건 받개 — 챗 턴의 astream_events 자리(도구 노드의 부모만 세운다)."""

    def __init__(self):
        self.events = []

    async def on_custom_event(self, name, data, *, run_id, tags=None, metadata=None, **_kw):
        self.events.append((name, data))


def _call_in_run(tool, args, timeout=20.0):
    """가정 ③ 부모 실행 안에서 부른다 — 도구가 기다리며 낸 사건을 받는다. (결과 글, [(사건 이름, 자료)]). 자료의 칸은 설계 4.6 의
    {phase, tool, state, progress, detail, elapsed_s, max_s, polls} 다."""
    catch = _Catch()

    async def body(_x, config):
        return await tool.ainvoke(args, config=config)
    out = _run(RunnableLambda(body).ainvoke({}, config={"callbacks": [catch]}), timeout)
    return out, catch.events


def _norm(word) -> str:
    """설계 4.3 의 낱말 꼴 — 소문자, '-' · 빈칸은 '_'."""
    return re.sub(r"[-\s]+", "_", str(word).strip().lower())


# ── 턴 상태 · 도구 지도 ─────────────────────────────────────────────────────────────────────────────────────────
def _state(sw, born=()):
    """가정 ② 턴 상태 — 설계 7절 '도구 래퍼' 머리가 시험에서 손으로 세우라고 적은 꼴 {born, seen, spent_s}. 모듈이 new_turn() 을 주면
    (design-integration 3절) 그 사전에 같은 칸을 덮는다 — 구현이 칸을 더 두었어도 깨지지 않게."""
    base = sw.new_turn() if callable(getattr(sw, "new_turn", None)) else None
    st = base if isinstance(base, dict) else {}
    st.update({"born": {str(x) for x in born}, "seen": Counter(), "spent_s": 0.0})
    return st


@contextlib.contextmanager
def _turn(sw, born=(), ids=None):
    """챗 턴 하나를 손으로 세운다 — 상태 기다림의 턴 상태 · 도구 호출 수(되풀이 꼬리) · 출처 정수(ids 를 주면 유령 ID 게이트가 선다)."""
    st = _state(sw, born)
    toks = [(a._turn_status_wait, a._turn_status_wait.set(st)), (a._turn_tool_calls, a._turn_tool_calls.set(Counter()))]
    if ids is not None:
        toks.append((a._turn_ids, a._turn_ids.set(set(ids))))
    try:
        yield st
    finally:
        for var, tok in reversed(toks):
            var.reset(tok)


def _tools_map(monkeypatch, mapping=None, routine_read=None):
    """도구 → 앱 캐시를 앉힌다(test_request_1029 의 _cache 꼴 — 묻는 함수도 막는다). routine_read 는 게이트웨이의 읽기 확인 목록이고
    None 은 그 칸이 없는 옛 게이트웨이(44f4b8d)다(설계 4.1 · 4.2)."""
    mapping = dict(mapping or {})
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "map", mapping)
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "routine_read", routine_read)
    monkeypatch.setitem(a._APPS_CACHE, "apps", {})
    monkeypatch.setattr(a, "_tools_map", lambda: mapping)
    monkeypatch.setattr(a, "_gw_apps", lambda: {})


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _tools_map(monkeypatch)
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})     # 창을 박아 둔다 — LLM 서버에 묻지 않게


@pytest.fixture
def sw(monkeypatch):
    """상태 기다림 모듈(status_wait.py — 설계 4.1). HA-28 전에는 여기서 붉다. 손잡이는 시험 크기로 줄여 켠다(설계 7절 머리)."""
    import status_wait as mod
    monkeypatch.setattr(mod, "MAX_S", 5.0)
    monkeypatch.setattr(mod, "POLL_S", 0.02)
    monkeypatch.setattr(mod, "TELL_S", 0)
    return mod


# ── 순수 판정(설계 7절 1 ~ 3) ─────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name, app_key, rr, want", [
    ("heaxstep_forge_job_status", "heax-step_forge", None, True),       # 접두는 앱 키에서 '-' 를 뺀 꼴 — 벗기면 job_status
    ("smarttwinmcp_job_status", "smart-twin-mcp", None, True),
    ("get_task", None, None, True), ("docforge_status", None, None, True), ("get_job", None, None, True),
    ("get_job_status", None, None, True), ("doe_status", None, None, True), ("study_get", None, None, True),
    ("job_status", None, None, True), ("Get_Task", None, None, True),                      # 소문자로 견준다
    ("job_log", None, None, False), ("list_jobs", None, None, False),
    ("heaxkooremapper_mcp_system_status", "heax-kooremapper_mcp", None, False),
    ("deliberate_status", None, None, False), ("run_job", None, None, False), ("get_task_report", None, None, False),
    ("get_job_details", None, None, False),                              # 레지스트리 상세 — 확인 안 함(설계 4.2)
    ("heaxstep_forge_job_status", None, None, False),                    # 지도에 앱이 없으면 접두를 벗기지 않는다
    ("get_task", None, frozenset({"other"}), False),                     # 게이트웨이가 울타리를 주면 그 안의 노출 이름만
    ("get_task", None, frozenset({"get_task"}), True),
    ("heaxstep_forge_job_status", "heax-step_forge", frozenset({"heaxstep_forge_job_status"}), True),
    ("heaxstep_forge_job_status", "heax-step_forge", frozenset({"job_status"}), False),   # 울타리는 노출 이름으로 본다
])
def test_1_상태_도구는_원래_이름_목록이고_읽기_울타리가_있으면_그_안만이다(sw, name, app_key, rr, want):
    assert bool(sw.is_status_tool(name, app_key, rr)) is want


def test_1_끄면_어떤_이름도_상태_도구가_아니다(sw, monkeypatch):
    monkeypatch.setattr(sw, "MAX_S", 0)
    for name in ("get_task", "job_status", "docforge_status", "doe_status"):
        assert not sw.is_status_tool(name, None, None), name
        assert not sw.is_status_tool(name, None, frozenset({name})), name


@pytest.mark.parametrize("sample, known", [
    ({"task_id": "t1", "status": "running", "progress": 0.45, "message": "running rules"}, True),     # ODB get_task
    ({"id": "j", "status": "pending", "progress": 0}, True),                                         # StepForge job_status
    ({"job_id": "j", "status": "running", "progress": 10, "cancel_requested": True}, True),            # 취소 요청은 따로 칸(outbound)
    ({"status": "done", "progress": 100, "stage": "S7"}, True),                                      # DocForge docforge_status
    ({"status": "succeeded"}, True), ({"status": "canceled"}, True),                                 # DynaForge get_job
    ({"job_id": "x", "state": "RUNNING"}, True), ({"state": "COMPLETED"}, True),                     # ste get_job_status
    ({"state": "SUBMITTED"}, True), ({"state": "HALTED"}, True), ({"state": "PARTIAL"}, True),       # AbaqusMCP doe_status
    ({"state": "GENERATED"}, False),
    ({"ok": False, "error": "잡이 없다", "status": 404}, False),        # DocForge 도구 오류 — 숫자 status 는 낱말이 아니다
    ({"status": "ok", "state": "running"}, True),                       # 첫 낱말이 표 밖이면 다음 칸
    ({"status": 404, "state": "running"}, True),
    ({"job": {"status": "queued"}}, True), ({"data": {"job_state": "in-progress"}}, True),
    ([{"status": "running"}], False),                                   # 배열
    ("✖ 도구 get_task 호출 실패: 연결 거절", False),                     # 평문
    (json.dumps("running"), False),                                     # JSON 이지만 객체가 아니다
    ({"ok": True, "job_id": 3, "squeue": {"state": "RUNNING", "name": "drop"}}, False),   # SmartTwinMCP — 맨 위 상태 칸이 없다
    ({"status": "paused"}, False), ({"status": "unknown"}, False),
])
def test_2_결과의_상태_낱말을_읽고_모르면_None_이다(sw, sample, known):
    """가정 ① — 모름(기다리지 않는다)은 None 이다(설계 4.3 · 4.5 의 'st 가 None'). 도는 중 · 끝의 갈래는 아래 래퍼 시험이 잰다."""
    text = sample if isinstance(sample, str) else json.dumps(sample, ensure_ascii=False)
    assert (sw.job_state(text) is not None) is known


@pytest.mark.parametrize("sample, want", [
    ({"job_id": "j-1", "kind": "graph", "status": "pending"}, {"j-1"}),                              # StepForge run_job
    ({"task_id": "t1", "kind": "checklist", "job_id": "j1", "status": "queued", "progress": 0.0}, {"t1", "j1"}),   # ODB run_checklist
    ({"id": "01H", "status": "queued"}, {"01H"}),                                                  # DynaForge run_operation
    ({"job_id": "d-1", "status": "queued"}, {"d-1"}),                                              # DocForge docforge_convert
    ({"id": 42, "status": "queued"}, {"42"}),                                                      # 값은 글자로 넣는다
    ({"job": {"id": "n1", "status": "running"}}, {"n1"}),                                         # 4.3 과 같은 사전을 본다
    ({"jobs": [{"id": "x", "status": "running"}]}, set()),                                        # 목록 속 잡
    ({"job_id": "z", "status": "done"}, set()),                                                   # 끝난 잡
    ({"job_id": "u", "status": "created"}, set()),                                                # 모르는 낱말
    ("✖ 도구 run_job 호출 실패: 502", set()),
])
def test_3_잡을_건_결과에서_도는_중인_잡의_id_를_배운다(sw, sample, want):
    text = sample if isinstance(sample, str) else json.dumps(sample, ensure_ascii=False)
    assert set(sw.born_ids(text)) == want


def test_3_결과_글이_2만_자를_넘으면_배우지_않는다(sw):
    def sized(n):
        bare = json.dumps({"job_id": "big", "status": "running", "pad": ""})
        text = json.dumps({"job_id": "big", "status": "running", "pad": "x" * (n - len(bare))})
        assert len(text) == n
        return text
    assert set(sw.born_ids(sized(20_000))) == {"big"}
    assert set(sw.born_ids(sized(20_001))) == set()


# ── 손잡이(설계 4.11) — 기동 때 한 번 읽으므로 새 프로세스에서 본다 ────────────────────────────────────────────────────
_PROBE = r"""
import json
import status_wait as s
rr = frozenset
print(json.dumps({"max": s.MAX_S, "poll": s.POLL_S, "tell": s.TELL_S, "cap": s.POLL_CALL_MAX_S, "on": s.health()["on"],
                  "get_task": bool(s.is_status_tool("get_task", None, None)),
                  "render": bool(s.is_status_tool("render_status", None, None)),
                  "render_rr": bool(s.is_status_tool("render_status", None, rr({"render_status"}))),
                  "foo_rr": bool(s.is_status_tool("foo_status", None, rr({"foo_status"}))),
                  "bar_rr": bool(s.is_status_tool("bar_status", None, rr({"bar_status"}))),
                  "prefixed_rr": bool(s.is_status_tool("heaxrender_app_render_status", "heax-render_app",
                                                       rr({"heaxrender_app_render_status"}))),
                  "get_task_fenced": bool(s.is_status_tool("get_task", None, rr({"render_status"})))}))
"""


def _probe(**env):
    """셸의 CHAT_STATUS_* 는 지우고 준 값만 건 새 프로세스에서 모듈을 읽는다."""
    e = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_STATUS_")}
    e.update(env)
    r = subprocess.run([sys.executable, "-c", _PROBE], cwd=str(ROOT), env=e, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_손잡이는_기동_때_읽고_기본은_한_턴_600초_10초_간격이다():
    d = _probe()
    assert (d["max"], d["poll"], d["tell"], d["cap"], d["on"]) == (600, 10, 30, 120, True), d
    assert d["get_task"] and not d["render"] and not d["render_rr"], "덧붙임이 없으면 바닥 목록만이다"
    assert _probe(CHAT_STATUS_WAIT_POLL_S="0.5")["poll"] == 2, "2 보다 작은 간격은 2 로 읽는다"
    assert _probe(CHAT_STATUS_WAIT_POLL_S="7")["poll"] == 7
    off = _probe(CHAT_STATUS_WAIT_MAX_S="0")
    assert off["on"] is False and off["get_task"] is False, "0 이면 끈다 — 어떤 이름도 상태 도구가 아니다"
    ex = _probe(CHAT_STATUS_TOOLS="render_status, foo_status")
    assert ex["render"] is False, "덧붙임은 게이트웨이 울타리(routine_read)가 있을 때만 먹는다 — 잘못 적은 쓰기 도구를 10초마다 부르지 않게"
    assert ex["render_rr"] and ex["foo_rr"] and ex["prefixed_rr"] and not ex["bar_rr"] and not ex["get_task_fenced"], ex


# ── 화면 프레임(설계 4.6) ───────────────────────────────────────────────────────────────────────────────────────
_DATA = {"tool": "get_task", "state": "running", "progress": 30, "detail": "running rules", "elapsed_s": 130, "max_s": 600,
         "polls": 13}


@pytest.mark.parametrize("phase, head", [
    ("start", "코드가 잡이 끝나기를 기다립니다 — get_task · running 30%"),
    ("wait", "잡을 기다리는 중 — get_task · running 30%"),
    ("end", "잡이 끝났습니다 — get_task · done"),
    ("cap", "기다림 상한"),
    ("fail", "잡 상태를 다시 보지 못해 기다림을 멈춥니다 — get_task"),
    ("shape", "잡 상태를 더 읽을 수 없어 기다림을 멈춥니다 — get_task"),
])
def test_화면_한_줄은_tool_을_비우고_갈래마다_정해진_머리로_시작한다(sw, phase, head):
    """포털 중계는 tool 이 있는 status 만 서버 사본 활동에 남긴다 — 진행 줄은 tool 을 비워 다음 턴 근거에 섞이지 않는다(설계 4.6)."""
    data = {**_DATA, "phase": phase, **({"state": "done", "progress": 100} if phase == "end" else {})}
    f = sw.frame(dict(data))
    assert f.get("tool") is None and isinstance(f.get("step"), str), f
    assert f["step"].startswith(head), f["step"]
    assert {k: f["wait"].get(k) for k in data} == data, "사건 자료는 그대로 wait 칸에 간다"
    if phase in ("wait", "end"):
        assert "2분 10초" in f["step"], "시간은 N분 M초로 적는다"
    if phase == "cap":
        assert "get_task" in f["step"] and "running" in f["step"], f["step"]


# ── 도구 래퍼(설계 7절 4 ~ 13) ──────────────────────────────────────────────────────────────────────────────────
def test_4_13_이_턴에_건_잡이면_끝날_때까지_다시_보고_마지막_결과와_안내만_넘긴다(sw):
    """부모 실행 없이 부른다(사건을 낼 곳이 없다) — 오류 없이 기다린다(7절 13)."""
    fake = _Fake([RUN, RUN2, DONE])
    with _turn(sw, born={"t1"}) as st:
        out = _call(_chat_tool("get_task", fake), {"task_id": "t1"})
    assert len(fake.calls) == 3, fake.calls
    assert '"status": "done"' in out and '"status": "running"' not in out, "LLM 에는 마지막 결과 하나만 간다"
    assert "[코드 기다림 — " in out and "끝난 상태(done)" in out, out[-400:]
    assert out.index('"status": "done"') < out.index("[코드 기다림 — "), "머리 + 본문 + 기다림 안내 + 꼬리 차례다"
    assert "번째 불렀다" not in out and not out.lstrip().startswith(a._TOOL_FAIL_MARK), out[:200]
    assert st["spent_s"] > 0, "기다린 시간은 턴의 몫에서 뺀다"


def test_5_건_잡이_아니면_한_번_보고_같은_호출을_되풀이하면_그때부터_기다린다(sw):
    fake = _Fake({"t1": [RUN, RUN, DONE], "t2": [_odb("running", 0.3, tid="t2")]}, by="task_id")
    tool = _chat_tool("get_task", fake)
    with _turn(sw):
        first = _call(tool, {"task_id": "t1"})
        n_first = len(fake.calls)
        again = _call(tool, {"task_id": "t1"})
        n_again = len(fake.calls)
        other = _call(tool, {"task_id": "t2"})
        n_other = len(fake.calls)
    assert n_first == 1 and '"status": "running"' in first, first[-400:]
    assert "[코드 기다림 — " in first and "아직 끝나지 않았다" in first and "한 번 더 부르라" in first, "상태만 묻는 질문은 10분 붙잡지 않는다"
    assert n_again == 3 and "끝난 상태(done)" in again, (n_again, again[-400:])
    assert n_other == 4 and "한 번 더 부르라" in other, "인자가 다르면 되풀이가 아니다"


def test_6_상한에_닿으면_아직이라고_넘기고_몫을_다_쓰면_더_기다리지_않는다(sw, monkeypatch):
    monkeypatch.setattr(sw, "MAX_S", 0.1)
    fake = _Fake({"t1": [RUN], "t2": [_odb("running", 0.3, tid="t2")]}, by="task_id")       # 늘 도는 중
    tool = _chat_tool("get_task", fake)
    with _turn(sw, born={"t1"}) as st:
        capped = _call(tool, {"task_id": "t1"})
        spent, n = st["spent_s"], len(fake.calls)
        again = _call(tool, {"task_id": "t1"})
        n_again = len(fake.calls)
        other = _call(tool, {"task_id": "t2"})
        n_other = len(fake.calls)
    assert n >= 2, "상한 전에 다시 조회했다"
    for must in ("[코드 기다림 — ", "아직 끝나지 않았다", "이 턴에는 더 기다리지 않는다", "예전에 저장된 결과"):
        assert must in capped, (must, capped[-500:])
    assert spent >= 0.1, spent
    assert n_again == n + 1 and "이미 다 써" in again and "한 번 더 부르라" not in again, again[-400:]
    assert n_other == n_again + 1 and "이미 다 써" in other and "한 번 더 부르라" not in other, "몫이 0 이면 snapshot 대신 budget 안내"


def test_7_다시_조회가_터지면_멈추고_그_전_결과를_넘기며_되풀이하면_남은_몫_안에서_다시_기다린다(sw):
    fake = _Fake([RUN, RuntimeError("502"), RUN, DONE])
    tool = _chat_tool("get_task", fake)
    with _turn(sw, born={"t1"}):
        out = _call(tool, {"task_id": "t1"})
        n = len(fake.calls)
        again = _call(tool, {"task_id": "t1"})
    assert n == 2, "예외 한 번에 멈춘다"
    assert '"status": "running"' in out and "다시 조회가 실패했다(RuntimeError" in out and "끝난 결과가 아니다" in out, out[-400:]
    assert len(fake.calls) == 4 and "끝난 상태(done)" in again, again[-400:]


def test_7_다시_조회가_답하지_않으면_남은_몫에서_끊는다(sw, monkeypatch):
    monkeypatch.setattr(sw, "MAX_S", 0.2)
    fake = _Fake([RUN, _Slow(10, DONE)])
    t0 = time.monotonic()
    with _turn(sw, born={"t1"}):
        out = _call(_chat_tool("get_task", fake), {"task_id": "t1"})
    took = time.monotonic() - t0
    assert took < 3, f"{took:.1f}초 — 다시 조회 한 번의 한도가 남은 몫에 걸리지 않았다"
    assert '"status": "running"' in out and "다시 조회가 실패했다" in out and "답하지 않았다" in out, out[-400:]


def test_7_다시_조회_한_번은_POLL_CALL_MAX_S_를_넘지_않는다(sw, monkeypatch):
    """게이트웨이 600 · MCP_CALL_TIMEOUT_S 900 보다 먼저 걸려 무엇이 느린지 우리 문구로 말한다(설계 4.5 · 4.12)."""
    monkeypatch.setattr(sw, "POLL_CALL_MAX_S", 0.1)
    fake = _Fake([RUN, _Slow(10, DONE)])
    t0 = time.monotonic()
    with _turn(sw, born={"t1"}):
        out = _call(_chat_tool("get_task", fake), {"task_id": "t1"})
    took = time.monotonic() - t0
    assert took < 3, f"{took:.1f}초"
    assert "다시 조회가 실패했다" in out and "답하지 않았다" in out, out[-400:]


def test_8_턴_상태가_없거나_챗_래퍼가_아니거나_첫_호출이_터지면_기다리지_않는다(sw):
    fake = _Fake([RUN, DONE])
    out = _call(_chat_tool("get_task", fake), {"task_id": "t1"})                 # 문맥 변수 기본값 — 심의 · 카탈로그 길
    assert len(fake.calls) == 1 and "[코드 기다림" not in out, out[-300:]
    raw = _Fake([RUN, DONE])
    with _turn(sw, born={"t1"}):
        got = _call(a._prep_tool(_mcp_tool("get_task", raw)), {"task_id": "t1"})   # chat=False — 결과를 코드가 JSON 으로 읽는 길
    assert len(raw.calls) == 1 and json.loads(got) == RUN, got[-300:]
    boom = _Fake([RuntimeError("503 backend unavailable"), DONE])
    with _turn(sw, born={"t1"}):
        failed = _call(_chat_tool("get_task", boom), {"task_id": "t1"})
    assert len(boom.calls) == 1 and failed.lstrip().startswith(a._TOOL_FAIL_MARK) and "[코드 기다림" not in failed, failed[:300]


def test_9_끄면_종전대로_기다리지_않고_네_번째에_꼬리가_붙는다(sw, monkeypatch):
    monkeypatch.setattr(sw, "MAX_S", 0)
    fake = _Fake([RUN])
    tool = _chat_tool("get_task", fake)
    with _turn(sw, born={"t1"}):
        outs = [_call(tool, {"task_id": "t1"}) for _ in range(4)]
    assert len(fake.calls) == 4 and all("[코드 기다림" not in o for o in outs)
    assert all("번째 불렀다" not in o for o in outs[:3]) and "4번째 불렀다" in outs[3], outs[3][-300:]


def test_9_켜면_상태를_읽은_상태_도구만_꼬리를_면제한다(sw, monkeypatch):
    done = _Fake([DONE])
    tool = _chat_tool("get_task", done)
    with _turn(sw):
        outs = [_call(tool, {"task_id": "t1"}) for _ in range(5)]
    assert len(done.calls) == 5 and all("번째 불렀다" not in o for o in outs), outs[-1][-300:]
    _tools_map(monkeypatch, {"smarttwinmcp_job_status": "smart-twin-mcp"})
    blind = _Fake([{"ok": True, "job_id": 3, "squeue": {"state": "RUNNING", "name": "drop"}}])   # SmartTwinMCP — 상태를 못 읽는다
    tool = _chat_tool("smarttwinmcp_job_status", blind, {"job_id": {"type": "integer"}})
    with _turn(sw):
        outs = [_call(tool, {"job_id": 3}) for _ in range(4)]
    assert len(blind.calls) == 4 and all("[코드 기다림" not in o for o in outs)
    assert "4번째 불렀다" in outs[3], "코드가 기다려 줄 수 없는 꼴에서는 꼬리가 LLM 루프를 막는다"


def test_10_invoke_tool_경유도_안쪽_이름으로_기다리고_같은_인자로_다시_부른다(sw, monkeypatch):
    _tools_map(monkeypatch, {"invoke_tool": "_gateway"})
    for arguments in ({"task_id": "t1"}, json.dumps({"task_id": "t1"})):
        fake = _Fake([RUN, RUN2, DONE])
        tool = _chat_tool("invoke_tool", fake, {"name": {"type": "string"}, "arguments": {"type": "object"}})
        with _turn(sw, born={"t1"}):
            out = _call(tool, {"name": "get_task", "arguments": arguments})
        assert len(fake.calls) == 3, (arguments, fake.calls)
        assert all(c == {"name": "get_task", "arguments": arguments} for c in fake.calls), fake.calls
        assert "끝난 상태(done)" in out, out[-400:]


def test_11_유령_ID_게이트가_먼저다(sw):
    fake = _Fake([RUN, DONE])
    tool = _chat_tool("get_task", fake, {"registry_id": {"type": "integer"}})
    with _turn(sw, born={"98765"}, ids={1, 2, 3}):
        out = _call(tool, {"registry_id": 98765})
    assert fake.calls == [] and a._PHANTOM_ID_MARK in out and "[코드 기다림" not in out, out[-300:]


def test_12_다시_조회는_래퍼를_다시_지나지_않고_실행_설정이_도구_인자로_새지_않는다(sw):
    fake = _Fake([RUN, RUN2, DONE])
    with _turn(sw, born={"t1"}):
        _call(_chat_tool("get_task", fake), {"task_id": "t1"})
        cnt = a._turn_tool_calls.get()
    assert len(fake.calls) == 3 and all(c == {"task_id": "t1"} for c in fake.calls), fake.calls
    assert cnt["get_task"] == 1, "다시 조회가 래퍼(게이트 · 머리 · 꼬리 셈)를 다시 지났다"


def test_래퍼는_턴마다_그_턴의_상태를_읽고_몫도_턴마다_센다(sw, monkeypatch):
    """도구 래퍼는 에이전트 캐시(30분)와 함께 여러 턴에 다시 쓰인다 — born · 되풀이 · 기다림 몫은 문맥 변수에 든 이번 턴의 것이다(설계 2.1 · 4.1)."""
    monkeypatch.setattr(sw, "MAX_S", 0.1)
    tool = _chat_tool("get_task", _Fake([RUN]))                 # 늘 도는 중 — 한 래퍼를 세 턴이 함께 쓴다
    with _turn(sw, born={"t1"}):
        first = _call(tool, {"task_id": "t1"})                  # 이 턴의 몫을 다 쓴다
    with _turn(sw):
        fresh = _call(tool, {"task_id": "t1"})                  # 새 턴 — born 도 되풀이도 없다
    with _turn(sw, born={"t1"}):
        again = _call(tool, {"task_id": "t1"})
    assert "이 턴에는 더 기다리지 않는다" in first, first[-300:]
    assert "한 번 더 부르라" in fresh, "앞 턴의 born · 호출 수가 새 턴에 남았다"
    assert "이 턴에는 더 기다리지 않는다" in again and "이미 다 써" not in again, "기다림 몫은 턴마다 새로 센다"


def test_id_배우기는_마지막_결과_하나에만_걸린다(sw):
    """접기 · 자르기 · id 배우기는 기다림이 넘긴 마지막 결과에만 한 번 걸린다(설계 4.1) — 중간 조회의 정수는 이 턴의 출처가 아니다."""
    mid = {**RUN2, "queue_position": 7341}
    end = {**DONE, "violations": 5521}
    with _turn(sw, born={"t1"}, ids={1}):
        _call(_chat_tool("get_task", _Fake([RUN, mid, end])), {"task_id": "t1"})
        ids = a._turn_ids.get()
    assert 5521 in ids and 7341 not in ids, sorted(i for i in ids if i > 100)


# 설계 4.3 의 표 — 도는 중이면 기다리고, 끝 · 모름이면 첫 결과 그대로다(가정 ① — 갈래는 job_state 의 꼴이 아니라 래퍼가 기다리는지로 잰다)
@pytest.mark.parametrize("first, kind", [
    *(({"status": w}, "run") for w in _RUN), *(({"status": w}, "end") for w in _END), *(({"status": w}, "odd") for w in _ODD),
    ({"status": "RUNNING"}, "run"), ({"status": "In Progress"}, "run"), ({"status": "in-progress"}, "run"),
    ({"status": "License Wait"}, "run"), ({"status": "COMPLETED"}, "end"), ({"status": "timed-out"}, "end"),
    ({"status": "Out Of Memory"}, "end"),
    ({"job_id": "x", "state": "RUNNING"}, "run"), ({"job_id": "x", "state": "CANCELLING"}, "run"),        # ste — 취소 직후 CANCELLING
    ({"job_id": "x", "state": "COMPLETED"}, "end"),
    ({"state": "SUBMITTED"}, "run"), ({"state": "CANARY"}, "run"), ({"state": "GENERATED"}, "odd"),       # AbaqusMCP doe_status
    ({"state": "HALTED"}, "end"), ({"state": "PARTIAL"}, "end"),
    ({"job_id": "x", "status": "running", "progress": 10, "cancel_requested": True}, "run"),             # StepForge — 취소 요청은 따로 칸
    ({"id": "x", "status": "queued", "progress": None}, "run"),                                         # DynaForge get_job
    ({"status": "ok", "state": "running"}, "run"), ({"status": "done", "job": {"status": "running"}}, "end"),   # 맨 위가 먼저
    ({"job": {"status": "queued"}}, "run"), ({"data": {"state": "COMPLETED"}}, "end"),
    ({"ok": False, "error": "잡이 없다", "status": 404}, "odd"),                                         # DocForge 도구 오류
])
def test_낱말표_도는_중이면_기다리고_끝이나_모름이면_첫_결과_그대로다(sw, first, kind):
    fake = _Fake([first, DONE])
    with _turn(sw, born={"t1"}):
        out = _call(_chat_tool("get_task", fake), {"task_id": "t1"})
    if kind == "run":
        assert len(fake.calls) == 2 and "끝난 상태(done)" in out, (first, out[-300:])
    else:
        assert len(fake.calls) == 1 and "[코드 기다림" not in out, (first, out[-300:])


@pytest.mark.parametrize("name, first", [
    ("get_task", DONE),
    ("get_task", {"ok": True, "job_id": 3, "squeue": {"state": "RUNNING"}}),
    ("get_task", "잡 t1 — 진행 중(평문)"),
    ("list_jobs", _odb("running", 0.3)),                     # 상태 도구가 아니다 — 도는 중이어도 기다리지 않는다
])
def test_첫_결과가_끝_모름이거나_상태_도구가_아니면_안내가_없다(sw, name, first):
    fake = _Fake([first, DONE])
    with _turn(sw, born={"t1"}):
        out = _call(_chat_tool(name, fake), {"task_id": "t1"})
    assert len(fake.calls) == 1 and "[코드 기다림" not in out, out[-300:]


def test_다시_조회_결과를_더_읽을_수_없으면_멈추고_그_결과를_넘긴다_DocForge_숫자_status(sw):
    """DocForge 는 도구 오류를 {ok:false, error, status:403} 처럼 숫자 status 로 준다 — 숫자면 기다리기를 끝낸다(outbound 의 답)."""
    fake = _Fake([RUN, {"ok": False, "error": "권한이 없다", "status": 403}, DONE])
    with _turn(sw, born={"t1"}):
        out = _call(_chat_tool("get_task", fake), {"task_id": "t1"})
    assert len(fake.calls) == 2, fake.calls
    assert '"status": 403' in out and '"status": "running"' not in out, "마지막 조회 결과를 넘긴다"
    assert "[코드 기다림 — " in out and "더 읽을 수 없어" in out, out[-400:]


def test_앱_접두는_도구_지도의_앱_키로_벗긴다_StepForge(sw, monkeypatch):
    def sf(status, progress):
        return {"job_id": "j-1", "status": status, "progress": progress}
    _tools_map(monkeypatch, {"heaxstep_forge_job_status": "heax-step_forge"})
    fake = _Fake([sf("running", 10), sf("running", 40), sf("done", 100)])
    with _turn(sw, born={"j-1"}):
        out = _call(_chat_tool("heaxstep_forge_job_status", fake, {"job_id": {"type": "string"}}), {"job_id": "j-1"})
    assert len(fake.calls) == 3 and "끝난 상태(done)" in out, out[-400:]
    _tools_map(monkeypatch, {})
    bare = _Fake([sf("running", 10), sf("done", 100)])
    with _turn(sw, born={"j-1"}):
        out = _call(_chat_tool("heaxstep_forge_job_status", bare, {"job_id": {"type": "string"}}), {"job_id": "j-1"})
    assert len(bare.calls) == 1 and "[코드 기다림" not in out, "앱을 모르면 원래 이름을 모른다 — 상태 도구로 보지 않는다"


def test_게이트웨이_읽기_울타리가_막으면_기다리지_않고_칸이_없으면_바닥_목록이다(sw, monkeypatch):
    def once():
        fake = _Fake([RUN, DONE])
        with _turn(sw, born={"t1"}):
            out = _call(_chat_tool("get_task", fake), {"task_id": "t1"})
        return len(fake.calls), out
    _tools_map(monkeypatch, {}, routine_read=frozenset({"other_tool"}))
    n, out = once()
    assert n == 1 and "[코드 기다림" not in out, "읽기로 확인된 도구만 되풀이해 부른다"
    _tools_map(monkeypatch, {}, routine_read=frozenset({"get_task"}))
    n, out = once()
    assert n == 2 and "끝난 상태(done)" in out
    # 칸이 아예 없는 캐시(tests/test_tools_map_retry 가 통째로 바꿔 끼우는 꼴) — '모름' 으로 읽고 터지지 않는다(design-integration 10절 3)
    monkeypatch.setattr(a, "_TOOLS_MAP_CACHE", {"at": 0.0, "map": {}, "areas": {}, "area_meta": {}})
    n, out = once()
    assert n == 2 and "끝난 상태(done)" in out, out[-300:]


def test_상태_도구의_결과는_born_이_아니다(sw):
    """born 은 상태 도구가 아닌 도구의 결과에서만 배운다 — 상태 조회가 제 id 를 born 으로 만들면 상태만 묻는 질문이 10분 붙잡힌다(설계 4.4)."""
    fake = _Fake([_odb("running", 0.3, tid="t7")])
    tool = _chat_tool("get_task", fake, {"task_id": {"type": "string"}, "verbose": {"type": "boolean"}})
    with _turn(sw):
        first = _call(tool, {"task_id": "t7"})
        second = _call(tool, {"task_id": "t7", "verbose": True})          # 같은 id · 다른 인자 — 되풀이가 아니다
    assert len(fake.calls) == 2 and "한 번 더 부르라" in first and "한 번 더 부르라" in second, second[-300:]


def test_DW02_잡을_건_결과의_id_로_첫_조회부터_기다린다(sw):
    run = _Fake([QUEUED])
    fake = _Fake([RUN, DONE])
    with _turn(sw):
        _call(_chat_tool("run_checklist", run, {"job_id": {"type": "string"}}), {"job_id": "j1"})
        out = _call(_chat_tool("get_task", fake), {"task_id": "t1"})
    assert len(run.calls) == 1 and len(fake.calls) == 2 and "끝난 상태(done)" in out, out[-300:]


def test_invoke_tool_로_건_잡도_born_이다(sw, monkeypatch):
    """목록에 없는 도구는 invoke_tool 로 부른다 — 경유로 건 잡의 id 도 배운다(안쪽 이름이 상태 도구가 아니면 born 을 본다)."""
    _tools_map(monkeypatch, {"invoke_tool": "_gateway"})
    run = _Fake([QUEUED])
    fake = _Fake([RUN, DONE])
    via = _chat_tool("invoke_tool", run, {"name": {"type": "string"}, "arguments": {"type": "object"}})
    with _turn(sw):
        _call(via, {"name": "run_checklist", "arguments": {"job_id": "j1"}})
        out = _call(_chat_tool("get_task", fake), {"task_id": "t1"})
    assert len(fake.calls) == 2 and "끝난 상태(done)" in out, out[-300:]


def test_정수_id_인자도_글자로_맞춘다_DynaForge(sw):
    """born 은 글자로 담고(42 → '42'), 상태 조회의 정수 id 인자도 글자로 견준다(설계 4.4 — 문자열 · 정수, 불 아님)."""
    run = _Fake([{"id": 42, "status": "queued"}])
    fake = _Fake([{"id": 42, "status": "running", "progress": None}, {"id": 42, "status": "succeeded", "progress": 100}])
    with _turn(sw):
        _call(_chat_tool("run_operation", run, {"recipe": {"type": "string"}}), {"recipe": "remesh"})
        out = _call(_chat_tool("get_job", fake, {"id": {"type": "integer"}}), {"id": 42})
    assert len(fake.calls) == 2 and "끝난 상태(succeeded)" in out, out[-300:]


def test_C3_챗_래퍼가_아니어도_턴_상태가_있으면_born_을_배우고_결과는_그대로다(sw):
    """승인 실행 · 확정 종결 · 새어 나온 호출의 직접 실행은 chat=False 래퍼를 지난다 — 그 길로 건 잡도 born 이다(design-integration C-3)."""
    run = _Fake([{"job_id": "j-7", "kind": "graph", "status": "pending"}])
    starter = a._prep_tool(_mcp_tool("run_job", run, {"project_id": {"type": "string"}}))
    fake = _Fake([{"job_id": "j-7", "status": "running", "progress": 5}, {"job_id": "j-7", "status": "done", "progress": 100}])
    with _turn(sw):
        started = _call(starter, {"project_id": "p1"})
        out = _call(_chat_tool("job_status", fake, {"job_id": {"type": "string"}}), {"job_id": "j-7"})
    assert json.loads(started) == {"job_id": "j-7", "kind": "graph", "status": "pending"}, "chat=False 결과는 JSON 그대로다"
    assert len(fake.calls) == 2 and "끝난 상태(done)" in out, out[-300:]


def test_병렬_호출은_저마다_시작_때의_남은_몫으로_돌고_끝난_뒤_합에_더한다(sw, monkeypatch):
    monkeypatch.setattr(sw, "MAX_S", 0.1)
    f1, f2 = _Fake([RUN]), _Fake([_odb("running", 0.3, tid="t2")])
    t1, t2 = _chat_tool("get_task", f1), _chat_tool("get_task", f2)

    async def both():
        return await asyncio.gather(t1.ainvoke({"task_id": "t1"}), t2.ainvoke({"task_id": "t2"}))
    with _turn(sw, born={"t1", "t2"}) as st:
        o1, o2 = _run(both())
    assert all("이 턴에는 더 기다리지 않는다" in o for o in (o1, o2)), (o1[-300:], o2[-300:])
    assert st["spent_s"] >= 0.2 - 1e-6, st["spent_s"]


# ── 사건 자료(설계 4.5 · 4.6) — 부모 실행 안에서 부른다 ─────────────────────────────────────────────────────────────
def test_사건은_도구가_도는_동안_흐르고_자료에_화면_칸을_싣는다(sw):
    fake = _Fake([RUN, _odb("running", 0.6, "merging"), DONE])
    with _turn(sw, born={"t1"}):
        out, evs = _call_in_run(_chat_tool("get_task", fake), {"task_id": "t1"})
    assert evs and {n for n, _ in evs} == {sw.EVENT} == {"hwax_status_wait"}, evs
    data = [d for _, d in evs]
    keys = {"phase", "tool", "state", "progress", "detail", "elapsed_s", "max_s", "polls"}
    assert all(keys <= set(d) for d in data), data
    phases = [d["phase"] for d in data]
    assert phases[0] == "start" and phases[-1] == "end" and set(phases[1:-1]) <= {"wait"} and "wait" in phases, phases
    start, mid, end = data[0], data[1], data[-1]
    assert start["tool"] == "get_task" and _norm(start["state"]) == "running" and start["detail"] == "running rules"
    assert start["progress"] == pytest.approx(30, abs=0.5), "ODB 의 0 ~ 1 실수는 100배"
    assert mid["progress"] == pytest.approx(60, abs=0.5) and mid["detail"] == "merging", mid
    assert _norm(end["state"]) == "done" and end["polls"] == 2 and end["elapsed_s"] >= start["elapsed_s"], end
    assert "끝난 상태(done)" in out


@pytest.mark.parametrize("script, outcome", [
    ([RUN, DONE], "end"),
    ([RUN, RuntimeError("502")], "fail"),
    ([RUN, {"ok": False, "error": "권한이 없다", "status": 403}], "shape"),
    ([RUN], "cap"),
])
def test_끝_사건의_갈래는_어떻게_멈췄는지다(sw, monkeypatch, script, outcome):
    monkeypatch.setattr(sw, "MAX_S", 0.08)
    with _turn(sw, born={"t1"}):
        _out, evs = _call_in_run(_chat_tool("get_task", _Fake(script)), {"task_id": "t1"})
    phases = [d["phase"] for _, d in evs]
    assert phases[0] == "start" and phases[-1] == outcome and set(phases[1:-1]) <= {"wait"}, phases


def test_진행_줄은_낱말이_바뀌면_곧바로_같으면_TELL_S_에_한_번이다(sw, monkeypatch):
    monkeypatch.setattr(sw, "TELL_S", 100)
    fake = _Fake([_odb("queued", 0.0), _odb("queued", 0.0), _odb("running", 0.2), _odb("running", 0.4), DONE])
    with _turn(sw, born={"t1"}):
        _out, evs = _call_in_run(_chat_tool("get_task", fake), {"task_id": "t1"})
    data = [d for _, d in evs]
    assert len(fake.calls) == 5 and [d["phase"] for d in data] == ["start", "wait", "end"], data
    assert _norm(data[1]["state"]) == "running", data[1]


@pytest.mark.parametrize("first, state, progress, detail", [
    ({"task_id": "t1", "status": "running", "progress": 0.45, "message": "running rules"}, "running", 45, "running rules"),   # ODB
    ({"job_id": "x", "status": "pending", "progress": 0}, "pending", 0, None),            # StepForge — 0 ~ 100 정수
    ({"job_id": "x", "status": "running", "progress": 1}, "running", 1, None),            # 정수 1 은 1%
    ({"job_id": "x", "status": "running", "progress": 1.0}, "running", 100, None),        # 실수 1.0 은 100%
    ({"job_id": "x", "status": "running", "progress": 150}, "running", 100, None),        # 0 ~ 100 으로 자른다
    ({"id": "x", "status": "queued", "progress": None}, "queued", None, None),           # DynaForge — null 이면 없음
    ({"job_id": "x", "state": "RUNNING"}, "running", None, None),                        # ste — 진행률이 없다
    ({"status": "running", "progress_pct": 55, "stage": "S3"}, "running", 55, "S3"),
    ({"status": "running", "percent": 0.5, "step": "merge"}, "running", 50, "merge"),
    ({"status": "running", "message": "  a   b\n c "}, "running", None, "a b c"),        # 공백을 줄인다
    ({"job": {"status": "queued", "phase": "boot"}}, "queued", None, "boot"),            # 4.3 과 같은 사전
])
def test_진행률과_설명은_화면용으로_읽는다(sw, first, state, progress, detail):
    with _turn(sw, born={"t1"}):
        _out, evs = _call_in_run(_chat_tool("get_task", _Fake([first, DONE])), {"task_id": "t1"})
    start = next(d for _, d in evs if d.get("phase") == "start")
    assert _norm(start["state"]) == state, start
    assert start["progress"] is None if progress is None else start["progress"] == pytest.approx(progress, abs=0.5), start
    assert (start["detail"] or None) == detail, start


def test_설명은_80자로_줄인다(sw):
    with _turn(sw, born={"t1"}):
        _out, evs = _call_in_run(_chat_tool("get_task", _Fake([_odb("running", 0.1, "가" * 200), DONE])), {"task_id": "t1"})
    detail = next(d for _, d in evs if d.get("phase") == "start")["detail"]
    assert detail.startswith("가" * 79) and len(detail) <= 81, detail


# ── 흔적(설계 4.10) ─────────────────────────────────────────────────────────────────────────────────────────────
def test_기다림마다_로그_한_줄을_남긴다(sw, monkeypatch, capsys):
    """흔적이 없으면 cae00 재시험이 효과를 가리지 못한다(설계 4.10 — HA-34 의 교훈). 줄의 꼴은 '[status-wait] <도구>: <갈래> — …'."""
    def lines():
        got = capsys.readouterr()
        return (got.out + got.err).splitlines()
    with _turn(sw, born={"t1"}):
        _call(_chat_tool("get_task", _Fake([RUN, DONE])), {"task_id": "t1"})
    got = lines()
    assert len([ln for ln in got if "[status-wait] get_task: end" in ln]) == 1, got
    monkeypatch.setattr(sw, "MAX_S", 0.06)
    with _turn(sw, born={"t1"}):
        _call(_chat_tool("get_task", _Fake([RUN])), {"task_id": "t1"})
    got = lines()
    assert len([ln for ln in got if "[status-wait] get_task: cap" in ln]) == 1, got


def test_17_health_에_status_wait_칸이_있고_기다림을_센다(sw, monkeypatch):
    c = TestClient(a.app)
    h0 = c.get("/health").json()["status_wait"]
    assert {"on", "poll_s", "max_s", "waits", "ended", "capped", "failed"} <= set(h0), h0
    assert h0["on"] is True
    with _turn(sw, born={"t1"}):
        _call(_chat_tool("get_task", _Fake([RUN, RUN2, DONE])), {"task_id": "t1"})
    h1 = c.get("/health").json()["status_wait"]
    assert [h1[k] - h0[k] for k in ("waits", "ended", "capped", "failed")] == [1, 1, 0, 0], (h0, h1)
    monkeypatch.setattr(sw, "POLL_S", 3)
    monkeypatch.setattr(sw, "MAX_S", 9)
    h2 = c.get("/health").json()["status_wait"]
    assert (h2["poll_s"], h2["max_s"]) == (3, 9), h2
    monkeypatch.setattr(sw, "MAX_S", 0)
    assert c.get("/health").json()["status_wait"]["on"] is False, "끈 박스를 밖에서 가린다"


def test_계수는_시작한_기다림과_끝난_갈래이고_기다리지_않은_조회는_세지_않는다(sw, monkeypatch):
    """waits 는 시작한 기다림이고, 끝 · 상한 · 실패(결과를 못 읽어 멈춘 것도 실패)는 끝난 갈래다(설계 4.10)."""
    h0 = dict(sw.health())

    def once(script, born=("t1",)):
        with _turn(sw, born=born):
            return _call(_chat_tool("get_task", _Fake(script)), {"task_id": "t1"})
    once([RUN, DONE])                                                   # end
    once([RUN, RuntimeError("502")])                                    # fail
    once([RUN, {"ok": False, "error": "권한이 없다", "status": 403}])    # shape → failed
    assert "한 번 더 부르라" in once([RUN], born=())                     # snapshot — 기다리지 않는다
    with _turn(sw, born={"t1"}) as st:
        st["spent_s"] = sw.MAX_S                                        # 몫을 다 쓴 턴 — 기다리지 않는다
        spent = _call(_chat_tool("get_task", _Fake([RUN])), {"task_id": "t1"})
    assert "이미 다 써" in spent and "한 번 더 부르라" not in spent, spent[-300:]
    monkeypatch.setattr(sw, "MAX_S", 0.06)
    once([RUN])                                                         # cap
    h1 = sw.health()
    assert [h1[k] - h0[k] for k in ("waits", "ended", "capped", "failed")] == [4, 1, 1, 2], (h0, h1)


def test_18_실행_설정은_RunnableConfig_인자_이름으로_받는다():
    """기본값을 None 으로 두면 3.10 의 get_type_hints 가 Optional 로 읽어 LangChain 이 config 를 넣지 않는다 — 사건을 낼 곳이 사라진다(4.1)."""
    tool = _chat_tool("get_task", _Fake([DONE]))
    assert _get_runnable_config_param(tool.coroutine) == "_hwax_rc"


def test_지도_캐시는_routine_read_를_받고_칸이_없으면_None_이다(monkeypatch):
    """옛 게이트웨이(44f4b8d)는 그 칸이 없다 — '모름' 이라 바닥 목록만 연다(설계 4.1 · 4.2)."""
    for body, want in (({"map": {"get_task": "odb-hub"}, "routine_read": ["get_task", "list_jobs"]}, {"get_task", "list_jobs"}),
                       ({"map": {"get_task": "odb-hub"}}, None),
                       ({"map": {"get_task": "odb-hub"}, "routine_read": None}, None)):
        monkeypatch.setattr(a, "_TOOLS_MAP_CACHE", {"at": 0.0, "map": {}, "areas": {}, "area_meta": {}, "routine_read": None})
        monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None, _b=body: io.BytesIO(json.dumps(_b).encode()))
        assert _REAL_TOOLS_MAP() == body["map"]
        got = a._TOOLS_MAP_CACHE.get("routine_read")
        assert (None if got is None else set(got)) == want, (body, got)


# ── 실제 챗 턴(설계 7절 14 ~ 16) ─────────────────────────────────────────────────────────────────────────────────
class _Script(BaseChatModel):
    """대본 모델 — steps 의 n 번째 칸이 n 번째 호출의 답이다. 칸은 글(답) · (도구 이름, 인자) · 받은 메시지로 그 둘을 만드는 함수다.
    받은 메시지를 호출마다 적고, 챗 스트림 길(on_chat_model_stream)로도 같은 답을 한 조각으로 낸다 — 운영 기본이 스트리밍이다."""

    steps: list = Field(default_factory=list)
    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "ha28-script"

    def bind_tools(self, tools, **_kw):
        return self

    def _reply(self, messages):
        self.seen.append(list(messages))
        step = self.steps[min(len(self.seen), len(self.steps)) - 1]
        step = step(messages) if callable(step) else step
        if isinstance(step, str):
            return AIMessage(content=step)
        name, args = step
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{len(self.seen)}"}])

    def _generate(self, messages, stop=None, run_manager=None, **_kw):
        return ChatResult(generations=[ChatGeneration(message=self._reply(messages))])

    def _stream(self, messages, stop=None, run_manager=None, **_kw):
        msg = self._reply(messages)
        yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content, tool_call_chunks=[
            {"name": c["name"], "args": json.dumps(c["args"], ensure_ascii=False), "id": c["id"], "index": i}
            for i, c in enumerate(msg.tool_calls)]))


def _ask_task(messages):
    """앞 결과(run_checklist)가 준 task_id 로 get_task 를 한 번 부른다 — DW-02 의 둘째 걸음."""
    last = [m for m in messages if isinstance(m, ToolMessage)][-1].content
    return "get_task", {"task_id": re.search(r'"task_id": "([^"]+)"', last).group(1)}


def _req(message):
    return a.ChatRequest(message=message, groups=["portal-admin"], history=[])


def _use_agent(monkeypatch, llm, tools):
    """가정 ④ 앱이 만드는 길(_agent_for — 도구를 챗 래퍼로 감싼다)로 지은 에이전트를 이 턴에 쓴다. 자동 재시도는 닫는다 — 터진 턴이 같은
    도구를 처음부터 다시 불러 셈을 흐리지 않게."""
    agent = _agent(monkeypatch, llm, tools)

    async def built(*_a, **_k):
        return agent
    monkeypatch.setattr(a, "_agent_for", built)
    monkeypatch.setenv("CHAT_AUTO_RETRY", "0")


def _chat(monkeypatch, llm, tools, message):
    _use_agent(monkeypatch, llm, tools)

    async def run():
        return [c async for c in a._agent_stream(a.app, _req(message))]
    return [(e, d) for e, d in (a._sse_parse(c) for c in _run(run(), 60)) if e]


def _waits(evs):
    return [(i, d) for i, (e, d) in enumerate(evs) if e == "status" and isinstance(d.get("wait"), dict)]


def _at(evs, step):
    return [i for i, (e, d) in enumerate(evs) if e == "status" and d.get("step") == step]


def _checklist_tools(run, fake):
    return (_mcp_tool("run_checklist", run, {"job_id": {"type": "string"}}), _mcp_tool("get_task", fake))


def test_14_DW02_체크리스트를_걸고_한_번_조회하면_코드가_끝까지_기다리고_LLM_은_세_번이다(sw, monkeypatch):
    run, fake = _Fake([QUEUED]), _Fake([RUN, RUN2, DONE])
    llm = _Script(steps=[("run_checklist", {"job_id": "j1"}), _ask_task, "체크리스트가 끝났다 — 위반 0건."])
    evs = _chat(monkeypatch, llm, _checklist_tools(run, fake), "ODB 체크리스트 돌려서 결과 알려줘")
    assert (len(llm.seen), len(run.calls), len(fake.calls)) == (3, 1, 3), (len(llm.seen), run.calls, fake.calls)
    called, done = _at(evs, "도구 호출: get_task"), _at(evs, "도구 완료: get_task")
    assert len(called) == 1 and len(done) == 1, evs
    waits = _waits(evs)
    assert waits and all(called[0] < i < done[0] for i, _ in waits), "진행 줄은 도구가 도는 동안 흐른다"
    phases = [d["wait"]["phase"] for _, d in waits]
    assert phases[0] == "start" and phases[-1] == "end" and set(phases[1:-1]) <= {"wait"}, phases
    assert all(d.get("tool") is None for _, d in waits), "tool 을 비워야 중계가 활동 · 다음 턴 근거에 남기지 않는다"
    first, last = waits[0][1], waits[-1][1]
    assert first["step"].startswith("코드가 잡이 끝나기를 기다립니다 — get_task"), first["step"]
    assert last["step"].startswith("잡이 끝났습니다 — get_task"), last["step"]
    assert _norm(first["wait"]["state"]) == "running" and first["wait"]["progress"] == pytest.approx(30, abs=0.5), first
    assert _norm(last["wait"]["state"]) == "done" and last["wait"]["polls"] == 2, last
    assert not [d for e, d in evs if e == "status" and "같은 호출 반복 감지" in str(d.get("step"))]
    assert evs[done[0]][1].get("ok") is True, evs[done[0]]
    tm = [m for m in llm.seen[2] if isinstance(m, ToolMessage)][-1].content
    assert '"status": "done"' in tm and "끝난 상태(done)" in tm and '"status": "running"' not in tm, tm[-500:]


@pytest.mark.parametrize("where", ["잠", "다시 조회"])
def test_15_중지하면_기다림도_멈추고_어느_끝에도_세지_않는다(sw, monkeypatch, where):
    """■ 중지 · 탭 닫기는 기다림만 멈춘다(잡은 앱에서 계속 돈다 — 설계 4.7). 취소가 간격의 잠에 걸리든 도는 다시 조회에 걸리든 삼키지 않는다 —
    다시 조회의 실패로 삼키면 '실패' 로 세고 턴이 이어진다."""
    monkeypatch.setattr(sw, "MAX_S", 30.0)
    monkeypatch.setattr(sw, "POLL_S", 0.05)
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 15.0)         # 챗 라우트처럼 _pinged_stream 이 턴 태스크를 돌린다
    in_call = where == "다시 조회"
    run, fake = _Fake([QUEUED]), _Fake([RUN, _Slow(30, RUN)] if in_call else [RUN])     # 늘 도는 중
    llm = _Script(steps=[("run_checklist", {"job_id": "j1"}), _ask_task, "끝"])
    _use_agent(monkeypatch, llm, _checklist_tools(run, fake))
    h0 = dict(sw.health())

    async def go():
        stream = a._pinged_stream(a._agent_stream(a.app, _req("ODB 체크리스트 돌려서 결과 알려줘")))
        saw = False
        async for c in stream:
            e, d = a._sse_parse(c)
            if e == "status" and isinstance(d.get("wait"), dict):
                saw = True
                break
        while in_call and len(fake.calls) < 2:               # 느린 다시 조회가 도는 중에 끊는다
            await asyncio.sleep(0.01)
        await stream.aclose()                                 # ■ 중지 · 탭 닫기 — 포털 중계가 상류를 닫는다
        n = len(fake.calls)
        await asyncio.sleep(6 * sw.POLL_S)
        return saw, n, len(fake.calls)
    saw, n, later = _run(go(), 40)
    assert saw, "기다림의 진행 줄이 오지 않았다"
    assert later == n, f"중지 뒤에도 {later - n}번 더 다시 조회했다"
    h1 = sw.health()
    assert h1["waits"] - h0["waits"] == 1, (h0, h1)
    assert [h1[k] - h0[k] for k in ("ended", "capped", "failed")] == [0, 0, 0], "취소된 기다림은 어디에도 세지 않는다"


def test_16_건_잡이_없으면_한_번_보고_다시_부르는_길을_알려_준다(sw, monkeypatch):
    fake = _Fake([_odb("running", 0.3, "running rules", tid="t5")])
    llm = _Script(steps=[("get_task", {"task_id": "t5"}), "잡 t5 는 아직 도는 중이다(30%)."])
    evs = _chat(monkeypatch, llm, (_mcp_tool("get_task", fake),), "잡 t5 어떻게 돼 가?")
    assert len(llm.seen) == 2 and len(fake.calls) == 1, (len(llm.seen), fake.calls)
    tm = [m for m in llm.seen[1] if isinstance(m, ToolMessage)][-1].content
    assert "[코드 기다림 — " in tm and "한 번 더 부르라" in tm, tm[-400:]
    assert not _waits(evs), "기다리지 않았으면 진행 줄도 없다"


def test_재시도_길_ainvoke_도_같은_턴_상태로_기다린다(sw, monkeypatch):
    """자동 재시도 · 강제 호출 재시도는 같은 에이전트를 스트림 없이 부른다(agent.ainvoke) — 사건을 받을 곳이 없어도 기다리고, 몫은 같은 턴
    상태에서 쓴다(design-integration 7절)."""
    run, fake = _Fake([QUEUED]), _Fake([RUN, RUN2, DONE])
    llm = _Script(steps=[("run_checklist", {"job_id": "j1"}), _ask_task, "끝났다."])
    agent = _agent(monkeypatch, llm, _checklist_tools(run, fake))
    with _turn(sw) as st:
        res = _run(agent.ainvoke({"messages": [("user", "체크리스트 돌려서 결과 알려줘")]}, config={"recursion_limit": 25}), 60)
    assert res["messages"][-1].content == "끝났다." and len(llm.seen) == 3 and len(fake.calls) == 3, (len(llm.seen), fake.calls)
    tm = [m for m in llm.seen[2] if isinstance(m, ToolMessage)][-1].content
    assert "끝난 상태(done)" in tm and st["spent_s"] > 0, tm[-300:]


@pytest.mark.parametrize("on", [True, False])
def test_화면의_반복_경고는_상태_도구에_내지_않고_끄면_종전이다(sw, monkeypatch, on):
    if not on:
        monkeypatch.setattr(sw, "MAX_S", 0)
    monkeypatch.setattr(a, "TOOL_REPEAT_WARN", 3)
    fake = _Fake([DONE])
    llm = _Script(steps=[("get_task", {"task_id": "t9"})] * 3 + ["끝났다."])
    evs = _chat(monkeypatch, llm, (_mcp_tool("get_task", fake),), "잡 t9 상태를 세 번 봐 줘")
    rep = [d for e, d in evs if e == "status" and "같은 호출 반복 감지" in str(d.get("step"))]
    assert len(fake.calls) == 3 and bool(rep) is (not on), rep
