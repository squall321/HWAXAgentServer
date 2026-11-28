# 토큰 절감 2판(HWAXPortal docs/token-saving offload-v2) — 위임 잡 · 절 초안 · 단순 변환 · 결과에 묻기 창구의 에이전트 서버 몫 시험
#
# 가짜는 LLM(대본 챗 모델 — bind_tools · 도구 호출 메시지 · usage · 모델 이름을 진짜 꼴로)과 게이트웨이(사람의 도구 · /tools-map · 내부 ref
# 창구)뿐이다. 잡은 실제 원장 · 줄 · asyncio 태스크로 돈다(test_delib_job_queue 와 같은 꼴). 실제 LLM 은 부르지 않는다.
#
#   실행:  .venv/bin/python -m pytest tests/test_offload.py -q
import asyncio
import itertools
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from pydantic import Field  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402
import offload  # noqa: E402
import routine  # noqa: E402
import usage_tokens  # noqa: E402

from test_delib_job_queue import _Engine, _tick  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401

ME, YOU = "me@example.com", "you@example.com"
SECRET = "Bearer gw-secret-EXAMPLE"          # 게이트웨이 공유 비밀 자리에 넣는 지어낸 값
NAMES = ("delegate_start", "delegate_status", "delegate_result", "delegate_cancel", "draft_section", "llm_transform")
ALL_ON = {"delegate": True, "draft": True, "transform": True}


# ── 가짜: 대본 챗 모델 · 사람의 도구 · /tools-map ─────────────────────────────────────────────────────────────
_IDS = itertools.count(1)


def _call(name, **args) -> dict:
    return {"name": name, "args": args, "id": f"tc-{next(_IDS)}"}


def _ai(text="", *calls, model="qwen-test", usage=(10, 5)) -> AIMessage:
    return AIMessage(content=text, tool_calls=list(calls), response_metadata={"model_name": model},
                     usage_metadata={"input_tokens": usage[0], "output_tokens": usage[1], "total_tokens": sum(usage)})


class FakeChat(BaseChatModel):
    """대본대로 답하는 챗 모델 — 대본 항목은 AIMessage · 글 · 부를 함수(메시지 → 그것들, 기다릴 수 있다). 묶인 도구 이름과 받은 메시지를 적는다."""

    script: list = Field(default_factory=list)
    seen: list = Field(default_factory=list)
    bound: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "offload-fake"

    def bind_tools(self, tools, **_kw):
        self.bound.append(sorted(t.name for t in tools))
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        raise NotImplementedError("비동기 길만 쓴다")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kw):
        self.seen.append(list(messages))
        item = self.script.pop(0) if self.script else _ai("끝.")
        if callable(item):
            item = item(messages)
            if asyncio.iscoroutine(item):
                item = await item
        msg = item if isinstance(item, AIMessage) else _ai(str(item))
        return ChatResult(generations=[ChatGeneration(message=msg)])


CALLS: list = []


def _tool(name: str, out: str) -> StructuredTool:
    """사람의 게이트웨이 도구 대역 — MCP 어댑터 도구처럼 content_and_artifact 로 글 조각 목록을 낸다."""
    async def _co(**kw):
        CALLS.append((name, kw))
        return [{"type": "text", "text": out}], None
    return StructuredTool(name=name, description=f"{name} 설명", coroutine=_co, response_format="content_and_artifact",
                          args_schema={"type": "object", "properties": {"q": {"type": "string"}}})


VOC_OUT = "S26 VOC 불량 1,234건 · 화면 비율 12.5% · 기준일 2026-11-20"
TOOLS = {"voc_search": _tool("voc_search", VOC_OUT), "plm_get": _tool("plm_get", "모델 S26 과제 키 PLM-7781"),
         "voc_write_note": _tool("voc_write_note", "적었다"), "deliberate_start": _tool("deliberate_start", "심의를 열었다"),
         "invoke_tool": _tool("invoke_tool", "아무 도구나"), "delegate_status": _tool("delegate_status", "위임 상태")}
MAP = {"voc_search": "signalforge", "plm_get": "plm", "voc_write_note": "signalforge", "deliberate_start": "hwax-deliberation",
       "delegate_status": "hwax-deliberation", "invoke_tool": "_gateway"}
# 게이트웨이가 읽기로 확인한 것 — 이 서버 백엔드의 도구가 들어 있어도(이름 낱말) 위임은 묶지 않는다
RR = frozenset({"voc_search", "plm_get", "deliberate_start", "delegate_status", "invoke_tool"})


def _ctx(user=ME, aff="CAEG", groups="연구소"):
    hdr = {"x-hwax-groups": groups, "x-hwax-affiliation": aff}
    if user:
        hdr["x-hwax-user"] = user
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=hdr)))


def _app_ns(llm=None, glm=None):
    return SimpleNamespace(state=SimpleNamespace(
        connections={"gateway": {"url": "http://gw.test/mcp", "transport": "streamable_http", "headers": {"Authorization": SECRET}}},
        llm=glm or llm, delib_llm=glm, chat_llm_cfg=None))


@pytest.fixture
def kit(monkeypatch, tmp_path):
    """빈 원장 · 빈 줄 · 위임 종류를 올린 원장 · 이음매(사람의 도구 · /tools-map · 챗 LLM) — 실 원장 · 게이트웨이 · LLM 에 닿지 않는다."""
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    monkeypatch.setattr(delib_jobs, "_PENDING", {})
    monkeypatch.setattr(delib_jobs, "_CLOSING", False)
    monkeypatch.setattr(delib_jobs, "_PAUSE", {})
    monkeypatch.setattr(delib_jobs, "_KINDS", {})
    offload.arm()
    monkeypatch.setattr(routine, "ROUTINE_RESULT_CHARS", 5000)
    monkeypatch.setattr(offload, "DELEGATE_MAX_RUNNING", 2)
    llm = FakeChat()
    seen: dict = {}

    async def _pt(_app, groups, user):
        seen.setdefault("who", []).append((list(groups), user))
        return dict(TOOLS)

    async def _rm(_app):
        return dict(MAP), RR

    monkeypatch.setattr(offload, "_person_tools", _pt)
    monkeypatch.setattr(offload, "_read_map", _rm)
    monkeypatch.setattr(offload, "_chat_llm", lambda _app: (llm, None))
    ns = _app_ns(llm)
    monkeypatch.setattr(m, "_APP", ns)
    CALLS.clear()
    return SimpleNamespace(llm=llm, seen=seen, app=ns, mp=monkeypatch)


def _run(scenario):
    """한 이벤트 루프에서 돌리고, 끝나면 남은 잡을 접고 끝날 때까지 기다린다(루프가 닫힌 뒤 원장에 쓰지 않게)."""
    async def go():
        try:
            return await scenario()
        finally:
            for jid in list(delib_jobs._PENDING):
                delib_jobs.cancel(jid)
            live = [t for t in delib_jobs._TASKS.values() if not t.done()]
            for t in live:
                t.cancel()
            if live:
                await asyncio.wait(live)
            await _tick()
    return asyncio.run(go())


async def _done(job_id: str, timeout: float = 10.0) -> dict:
    t = delib_jobs._TASKS.get(job_id)
    if t is not None:
        done, _p = await asyncio.wait([t], timeout=timeout)       # 접힌 태스크도 기다린다(CancelledError 를 올리지 않게)
        assert done, f"{job_id} 가 {timeout}초 안에 끝나지 않았다"
    await _tick()
    return delib_jobs._JOBS[job_id]


# ── 스위치: 꺼지면 도구 · 창구가 없다(바이트까지 같다) ───────────────────────────────────────────────────────────────
def test_스위치는_값이_1_일_때만_켜진다():
    assert offload.switches({}) == {"delegate": False, "draft": False, "transform": False, "result_ask": False}
    assert offload.switches({"DELEGATE_ENABLED": "1", "DRAFT_ENABLED": "true", "TRANSFORM_ENABLED": "0", "RESULT_ASK_ENABLED": " 1 "}) == {
        "delegate": True, "draft": False, "transform": False, "result_ask": True}


def test_꺼진_서버에는_도구가_하나도_없고_원장에_위임_종류도_없다():
    names = {t.name for t in asyncio.run(m.mcp.list_tools())}
    assert not names & set(NAMES), "시험 세션은 스위치가 꺼진 서버다 — 그런데 위임 · 작성 · 변환 도구가 올라가 있다"
    assert m._OFFLOAD == {"delegate": False, "draft": False, "transform": False} and offload.ON == offload.switches({})
    off = FastMCP(name="off")
    assert m._register_offload(off, on=offload.switches({})) == {"delegate": False, "draft": False, "transform": False}
    assert asyncio.run(off.list_tools()) == []
    assert "delegate" not in delib_jobs._KINDS, "꺼진 서버의 원장에 위임 종류가 올라가 있다"


def test_꺼진_서버에는_묻기_창구가_없어_404_이고_health_는_넷_다_거짓(_pin_context):
    assert not any(getattr(r, "path", "") == "/llm/ask_over_text" for r in a.app.routes)
    c = TestClient(a.app)
    r = c.post("/llm/ask_over_text", json={"text": "x", "question": "y"}, headers={"Authorization": SECRET, "x-hwax-user": ME})
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}, "없는 경로와 같은 답이어야 한다"
    assert c.get("/health").json()["offload"] == {"delegate": False, "draft": False, "transform": False, "result_ask": False}


def test_켜면_도구_여섯이_오르고_인자는_계약대로다():
    on = FastMCP(name="on")
    assert m._register_offload(on, on=ALL_ON) == ALL_ON
    listed = {t.name: t for t in asyncio.run(on.list_tools())}
    assert set(listed) == set(NAMES)
    props = {n: set(t.inputSchema["properties"]) for n, t in listed.items()}
    assert props == {"delegate_start": {"task", "apps", "max_steps", "max_minutes"}, "delegate_status": {"job_id", "wait_s"},
                     "delegate_result": {"job_id", "view"}, "delegate_cancel": {"job_id"},
                     "draft_section": {"outline", "refs", "style", "target_chars"}, "llm_transform": {"instruction", "text", "ref"}}
    req = {n: set(t.inputSchema.get("required") or []) for n, t in listed.items()}
    assert req == {"delegate_start": {"task"}, "delegate_status": {"job_id"}, "delegate_result": {"job_id"}, "delegate_cancel": {"job_id"},
                   "draft_section": {"outline"}, "llm_transform": {"instruction"}}
    assert listed["delegate_result"].inputSchema["properties"]["view"]["enum"] == ["full", "summary"]
    assert listed["draft_section"].inputSchema["properties"]["style"]["enum"] == ["report", "memo", "minutes", "plain"]
    # 하나씩 켠다
    only = FastMCP(name="only")
    m._register_offload(only, on={"transform": True})
    assert [t.name for t in asyncio.run(only.list_tools())] == ["llm_transform"]


# ── D-18 · 이 서버의 에이전트는 묶지도 부르지도 않는다 ─────────────────────────────────────────────────────────────
def test_이_서버의_에이전트는_위임_도구를_묶지도_invoke_tool_로_부르지도_않는다():
    for n in NAMES:
        assert a._agent_denied(n) and a._agent_denied(n.upper()) and a._agent_denied("hwaxdeliberation_" + n), n
        t = SimpleNamespace(name=n, annotations=None, meta=None)
        assert not a._bindable(t) and not a._bindable(t, allow_panels=True), n
    # 다른 앱의 같은 끝말 도구는 막지 않는다 — 정확한 이름 · 이 서버 접두만
    assert not a._agent_denied("mxwp_draft_section") and not a._agent_denied("deliberate_start") and not a._agent_denied("delegate_list")
    # 좌석 자유 조회 접두(리스크 심사 좌석 포함)에 걸리지 않는다
    assert not [n for n in NAMES if any(n.startswith(p) for p in d._FREE_ALLOW)]


def test_도구_로더가_위임_도구를_목록에서_뺀다(monkeypatch):
    listed = [_tool(n, "x") for n in ("voc_search", *NAMES)]

    class _Client:
        def __init__(self, *_a, **_k):
            pass

        async def get_tools(self):
            return list(listed)
    monkeypatch.setattr(a, "MultiServerMCPClient", _Client)
    got = asyncio.run(a._get_tools_retry({"gateway": {}}, tries=1))
    assert [t.name for t in got] == ["voc_search"]


def test_invoke_tool_로_위임_도구를_부르면_보내지_않고_까닭을_준다():
    sent = []

    async def _co(**kw):
        sent.append(kw)
        return "보냈다"
    inv = a._cap_tool(StructuredTool(name="invoke_tool", description="안쪽 도구를 부른다", coroutine=_co,
                                     args_schema={"type": "object", "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}}}))
    out = asyncio.run(inv.coroutine(name="delegate_start", arguments={"task": "x"}))
    assert sent == [] and "서버에 일 맡기기" in str(out) and str(out).startswith(a._TOOL_FAIL_MARK)


# ── 울타리 ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_울타리는_게이트웨이가_읽기로_확인한_내_도구만_묶고_이_서버_도구와_로컬_도구는_뺀다(kit):
    allowed, why = asyncio.run(offload.read_fence(kit.app, ["연구소"], ME))
    assert sorted(allowed) == ["plm_get", "voc_search"] and why == ""
    assert kit.seen["who"] == [(["연구소"], ME)], "그 사람의 신원 · 그룹으로 도구를 받는다"
    only, _ = asyncio.run(offload.read_fence(kit.app, [], ME, apps=["plm"]))
    assert sorted(only) == ["plm_get"]
    none, why = asyncio.run(offload.read_fence(kit.app, [], ME, apps=["nope"]))
    assert none == {} and "nope" in why


def test_게이트웨이가_읽기_판정을_주지_않으면_아무것도_묶지_않는다(kit):
    async def _old(_app):
        return dict(MAP), None
    kit.mp.setattr(offload, "_read_map", _old)
    allowed, why = asyncio.run(offload.read_fence(kit.app, [], ME))
    assert allowed == {} and "routine_read" in why


def test_사람의_도구는_MCP_심의_잡과_같은_길로_받는다(monkeypatch):
    """게이트웨이 공유 비밀 + 신원 머리(deliberation._tools_by_name — 사람 PAT 없음). 새 신원 길을 만들지 않는다."""
    got = {}

    async def _tbn(app_, groups, result_max=None, desc_max=None, user="", user_pat="", svc_fallback=True, as_of=None):
        got.update(groups=groups, user=user, user_pat=user_pat)
        return {"voc_search": TOOLS["voc_search"]}
    monkeypatch.setattr(d, "_tools_by_name", _tbn)
    out = asyncio.run(offload._person_tools(object(), ["연구소"], ME))
    assert list(out) == ["voc_search"] and got == {"groups": ["연구소"], "user": ME, "user_pat": ""}


# ── A 위임 ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_위임은_그_사람_시야의_읽기_도구로_풀고_답_근거_숫자_대조를_돌려준다(kit):
    kit.llm.script = [_ai("", _call("voc_search", q="S26")),
                      _ai("", _call("voc_write_note", q="몰래 쓰기")),          # 묶이지 않은 쓰기 도구 — 부를 수 없다
                      _ai("S26 VOC 불량은 1,234건 [e1], 화면 비율 12.5% [e1], 지난달 대비 9876.5 늘었다.")]

    async def scenario():
        out = await m.delegate_start(task="S26 VOC 불량 수와 비율을 알아내라", ctx=_ctx())
        job = await _done(out["job_id"])
        st = await m.delegate_status(out["job_id"], ctx=_ctx())
        full = await m.delegate_result(out["job_id"], ctx=_ctx())
        summ = await m.delegate_result(out["job_id"], view="summary", ctx=_ctx())
        return out, job, st, full, summ
    out, job, st, full, summ = _run(scenario)
    assert out["status"] == "running" and out["job_id"].startswith("delegate-")
    assert job["status"] == "done" and st["status"] == "done" and st["stage"] == "answer"
    assert kit.llm.bound and kit.llm.bound[0] == ["plm_get", "voc_search"], "확인된 읽기 도구만 묶는다"
    assert CALLS == [("voc_search", {"q": "S26"})], "쓰기 도구는 불리지 않았다"
    assert full["answer"].startswith("S26 VOC 불량은 1,234건") and full["model"] == "qwen-test" and full["steps"] == 1
    assert full["evidence"] == [{"id": "e1", "tool": "voc_search", "args": '{"q": "S26"}', "chars": len(VOC_OUT), "ok": True}]
    assert full["numbers"] == {"checked": 3, "mismatched": ["9876.5"], "kinds": {"9876.5": "원문에 없음"}}, \
        "근거에 없는 수만 짚는다(고쳐 쓰지 않는다) — 어긋남마다 종류(HWAXPortal docs/request-1125 OF-3)"
    assert full["tokens"] == {"input": 30, "output": 15}
    assert summ["evidence_n"] == 1 and summ["evidence_tools"] == ["voc_search"] and "evidence" not in summ
    assert kit.seen["who"] == [(["연구소"], ME)]


def test_호출_예산을_다_쓰면_더_부르지_않고_받은_결과로_정리한다(kit):
    kit.llm.script = [_ai("", _call("voc_search", q="1")), _ai("", _call("voc_search", q="2")), _ai("", _call("voc_search", q="3")),
                      _ai("다 썼다 — 불량 1,234건 [e1].")]

    async def scenario():
        out = await m.delegate_start(task="세 번 찾아라", max_steps=2, ctx=_ctx())
        await _done(out["job_id"])
        return await m.delegate_result(out["job_id"], ctx=_ctx())
    res = _run(scenario)
    assert [c[1]["q"] for c in CALLS] == ["1", "2"] and res["steps"] == 2
    assert len(res["evidence"]) == 2


def test_시간_상자에_닿으면_조회를_멈추고_받은_결과로_정리_턴을_돈다(kit):
    gate = asyncio.Event()

    async def _stall(_msgs):
        await gate.wait()
        return _ai("오지 않는다")
    kit.mp.setattr(offload, "_MINUTE_S", 0.3)
    kit.llm.script = [_ai("", _call("voc_search", q="S26")), _stall, _ai("정리 — 불량 1,234건 [e1].", model="glm-fallback")]

    async def scenario():
        out = await m.delegate_start(task="S26 불량", max_minutes=1, ctx=_ctx())
        job = await _done(out["job_id"])
        return job, await m.delegate_result(out["job_id"], ctx=_ctx())
    job, res = _run(scenario)
    assert res["answer"].startswith("정리") and res["model"] == "glm-fallback"
    assert any("시간 예산" in w for w in job["warnings"]) and any("시간 예산" in x for x in res["notes"])
    wrap = kit.llm.seen[-1]
    assert "[받은 결과]" in wrap[1].content and VOC_OUT in wrap[1].content, "정리 턴은 받은 결과만으로 답한다"


def test_읽기_도구가_하나도_없으면_까닭을_붙여_오류로_끝난다(kit):
    async def _empty(_app):
        return dict(MAP), frozenset()
    kit.mp.setattr(offload, "_read_map", _empty)

    async def scenario():
        out = await m.delegate_start(task="아무거나", ctx=_ctx())
        job = await _done(out["job_id"])
        return job, await m.delegate_result(out["job_id"], ctx=_ctx())
    job, res = _run(scenario)
    assert job["status"] == "error" and "읽기로 확인된 도구가 하나도 없다" in job["error"]
    assert res["status"] == "error" and "answer" not in res and kit.llm.seen == []


def test_신원_없는_호출은_위임_작성_변환을_받지_않는다(kit):
    for coro in (m.delegate_start(task="x", ctx=_ctx(user="")), m.draft_section(outline="x", ctx=_ctx(user="")),
                 m.llm_transform(instruction="x", text="y", ctx=_ctx(user=""))):
        with pytest.raises(ValueError, match="신원 없는 호출"):
            asyncio.run(coro)
    assert delib_jobs._JOBS == {}


def test_위임은_주인만_본다_같은_소속도_남의_것은_없는_것과_같다(kit):
    gate = asyncio.Event()

    async def _wait(_msgs):
        await gate.wait()
        return _ai("끝 — 1,234건")
    kit.llm.script = [_wait]

    async def scenario():
        out = await m.delegate_start(task="내 일", ctx=_ctx(ME, aff="CAEG"))
        job = delib_jobs._JOBS[out["job_id"]]
        assert not delib_jobs.can_read(job, YOU, "CAEG") and not delib_jobs.can_read(job, "", "", True), "같은 소속 · 관리자 · 신원 없는 호출에도 닫힌다"
        for f in (m.delegate_status, m.delegate_result, m.delegate_cancel):
            with pytest.raises(ValueError, match="그런 위임이 없다"):
                await f(out["job_id"], ctx=_ctx(YOU, aff="CAEG"))
        with pytest.raises(ValueError, match="심의가 아니다"):
            await m.deliberate_status(out["job_id"], ctx=_ctx(ME))
        with pytest.raises(ValueError, match="심의가 아니다"):
            await m.deliberate_continue(out["job_id"], ctx=_ctx(ME))
        assert delib_jobs.list_jobs(viewer=ME) == [] and delib_jobs.list_jobs() == [], "심의 목록에 싣지 않는다"
        c = await m.delegate_cancel(out["job_id"], ctx=_ctx(ME))
        job = await _done(out["job_id"])
        return c, job
    c, job = _run(scenario)
    assert c["status"] == "cancelling" and job["status"] == "cancelled"


def test_위임은_제_상한으로_줄을_서고_심의_자리를_쓰지_않는다(kit, monkeypatch):
    eng = _Engine()
    monkeypatch.setattr(d, "run_deliberation", eng.entry)
    monkeypatch.setattr(delib_jobs, "MAX_RUNNING", 1)
    monkeypatch.setattr(delib_jobs, "MAX_RUNNING_PER_USER", 1)
    monkeypatch.setattr(offload, "DELEGATE_MAX_RUNNING", 2)
    gates: dict = {}

    def _hold(key):
        async def _w(_msgs):
            await gates.setdefault(key, asyncio.Event()).wait()
            return _ai(f"{key} 끝")
        return _w
    kit.llm.script = [_hold("a"), _hold("b"), _hold("c")]

    async def scenario():
        first = await m.delegate_start(task="A", ctx=_ctx(ME))
        mine2 = await m.delegate_start(task="A2", ctx=_ctx(ME))          # 사람마다 1 — 줄
        other = await m.delegate_start(task="B", ctx=_ctx(YOU))          # 전체 2 안 — 돈다
        third = await m.delegate_start(task="C", ctx=_ctx("third@example.com"))   # 전체 2 가 찼다 — 줄
        delib = delib_jobs.start(object(), "default", "심의 화두", user_email=ME)     # 심의 자리(전역 1)는 위임이 쓰지 않는다
        await _tick()
        snap = (first["status"], mine2["status"], other["status"], third["status"], delib["status"],
                mine2["queue"]["why"], third["queue"]["why"])
        assert delib_jobs._glm_running_count() == 1 and delib_jobs.running_human_count() == 1
        gates.setdefault("a", asyncio.Event()).set()
        await _done(first["job_id"])
        return snap, delib_jobs._JOBS[mine2["job_id"]]["status"], delib_jobs._JOBS[third["job_id"]]["status"]
    snap, after_mine, after_third = _run(scenario)
    assert snap[:5] == ("running", "queued", "running", "queued", "running")
    assert "사람마다 1건" in snap[5] and "전체 상한 2건" in snap[6]
    # 내 첫 위임이 끝났다 — 먼저 선 내 둘째가 들고(사람마다 1 이 풀렸다), 셋째는 전체 2 가 다시 차 기다린다
    assert (after_mine, after_third) == ("running", "queued")


def test_재기동_뒤_줄_선_위임은_같은_차례로_다시_서고_돌던_위임은_다시_맡기라고_닫힌다(kit, monkeypatch):
    monkeypatch.setattr(offload, "DELEGATE_MAX_RUNNING", 1)
    gate = asyncio.Event()

    async def _w(_msgs):
        await gate.wait()
        return _ai("끝")
    kit.llm.script = [_w]

    async def scenario():
        run = await m.delegate_start(task="도는 일", ctx=_ctx(ME))
        q = await m.delegate_start(task="줄 선 일", ctx=_ctx(YOU))
        await _tick()
        delib_jobs.closing(True)
        await delib_jobs.shutdown(wait_s=2)
        return run["job_id"], q["job_id"]
    run_id, q_id = asyncio.run(scenario())      # 줄 선 잡을 접지 않는다(_run 의 뒷정리를 쓰지 않는다 — 재기동이 줄을 넘기는지 본다)
    # 재기동 — 메모리가 비고 원장 파일만 남는다
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_PENDING", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    monkeypatch.setattr(delib_jobs, "_CLOSING", False)
    delib_jobs.reap_orphans(kit.app)
    ran, queued = delib_jobs.get(run_id), delib_jobs.get(q_id)
    assert ran["status"] == "interrupted" and "delegate_start 로 다시 맡기면" in ran["error"]
    assert queued["status"] == "queued" and q_id in delib_jobs._PENDING and queued["requeued"] == 1
    assert not delib_jobs._args_path(run_id).exists(), "잇지 않는 위임의 시작 인자 파일은 곧바로 치운다"


def test_위임_종류가_꺼진_서버의_재기동은_줄_선_위임을_닫는다(kit, monkeypatch):
    monkeypatch.setattr(offload, "DELEGATE_MAX_RUNNING", 1)
    gate = asyncio.Event()

    async def _w(_msgs):
        await gate.wait()
        return _ai("끝")
    kit.llm.script = [_w]

    async def scenario():
        await m.delegate_start(task="도는 일", ctx=_ctx(ME))
        q = await m.delegate_start(task="줄 선 일", ctx=_ctx(YOU))
        delib_jobs.closing(True)
        await delib_jobs.shutdown(wait_s=2)
        return q["job_id"]
    q_id = asyncio.run(scenario())
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_PENDING", {})
    monkeypatch.setattr(delib_jobs, "_CLOSING", False)
    monkeypatch.setattr(delib_jobs, "_KINDS", {})          # 스위치를 끄고 다시 띄웠다
    delib_jobs.reap_orphans(kit.app)
    j = delib_jobs.get(q_id)
    assert j["status"] == "interrupted" and "꺼져 있다" in j["error"] and q_id not in delib_jobs._PENDING


# ── C 절 초안 ───────────────────────────────────────────────────────────────────────────────────────────────────
class _GwRef:
    """게이트웨이 내부 ref 창구 대역(게이트웨이 가지 wt1124/offload-v2 의 꼴) — Authorization 한 번 · 사람 머리(없으면 400) · 없음 · 남의 것은 같은
    404 · 200 은 {ref, tool, chars, sha256, pages, created, expires, text, structured} 이고 text 가 원문 전부다(pages 는 result_get 의 쪽 수)."""

    def __init__(self, store: dict, owner: str = ME):
        self.store, self.owner, self.seen = store, owner, []

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.seen.append((str(req.url), req.headers.get_list("authorization"), req.headers.get("x-hwax-user"),
                          [k for k in req.headers if k.lower().startswith("x-forwarded")]))
        if req.method != "GET":
            return httpx.Response(405)
        if req.headers.get_list("authorization") != [SECRET]:
            return httpx.Response(401, json={"error": "unauthorized"})
        if not req.headers.get("x-hwax-user"):
            return httpx.Response(400, json={"error": "사람 머리가 없다"})
        ref = req.url.path.rsplit("/", 1)[-1]
        if ref not in self.store or req.headers.get("x-hwax-user") != self.owner:
            return httpx.Response(404, json={"error": "없다"})
        text = self.store[ref]
        return httpx.Response(200, json={"ref": ref, "tool": "voc_search", "chars": len(text), "sha256": "0" * 64,
                                         "pages": len(text) // 20000 + 1, "created": "2026-11-24T00:00:00Z",
                                         "expires": "2026-12-01T00:00:00Z", "text": text, "structured": None})


def test_절_초안은_ref_와_울타리_안_도구로_근거를_읽고_숫자를_대조한다(kit, _pin_context):
    gw = _GwRef({"r-1": "앞쪽 수율 97.5% 확인 · 뒤쪽 불량 1,234건"})
    kit.mp.setattr(offload, "GW_TRANSPORT", httpx.MockTransport(gw.handle))
    glm = FakeChat(script=[_ai("## 결과\n수율 97.5% [s1], 불량 1,234건 [s1], 과제 PLM-7781 [s2], 목표 88.8%.", model="glm-5.3")])
    kit.app.state.delib_llm = glm
    out = asyncio.run(m.draft_section(outline="수율과 불량을 정리", refs=["r-1", {"tool": "plm_get", "args": {"q": "S26"}},
                                                                    {"tool": "voc_write_note", "args": {"q": "x"}}],
                                      style="memo", ctx=_ctx()))
    assert out["model"] == "glm-5.3" and out["draft"].startswith("## 결과")
    assert out["tokens"] == {"input": 10, "output": 5}, "이 일이 쓴 서버 LLM 토큰(위임 delegate_result 와 같은 셈)"
    assert out["numbers"] == {"checked": 4, "mismatched": ["88.8"], "kinds": {"88.8": "원문에 없음"}}, "PLM-7781 의 7781 은 울타리 안 도구 결과에 있다"
    src = out["sources"]
    assert [s["ok"] for s in src] == [True, True, False] and "읽기 울타리 밖" in src[2]["error"]
    assert src[0] == {"n": 1, "kind": "ref", "ref": "r-1", "chars": len("앞쪽 수율 97.5% 확인 · 뒤쪽 불량 1,234건"), "ok": True}
    assert not any("text" in s for s in src), "근거 원문은 이 대화로 돌려주지 않는다"
    assert [c[0] for c in CALLS] == ["plm_get"], "울타리 밖 쓰기 도구는 부르지 않는다"
    assert gw.seen == [("http://gw.test/internal/result/r-1", [SECRET], ME, [])], \
        "게이트웨이 연결 주소에서 /mcp 를 뗀 자리로 한 번 · 공유 비밀 한 번 · 그 사람 이메일 머리 · X-Forwarded 없이 읽는다"
    human = glm.seen[0][1].content
    assert "앞쪽 수율 97.5% 확인 · 뒤쪽 불량 1,234건" in human and "memo" not in human and "메모" in glm.seen[0][0].content


def test_남의_ref_는_읽지_못하고_근거가_창을_넘으면_거절한다(kit, _pin_context):
    gw = _GwRef({"r-1": "x" * 9000}, owner=YOU)
    kit.mp.setattr(offload, "GW_TRANSPORT", httpx.MockTransport(gw.handle))
    kit.app.state.delib_llm = FakeChat(script=[_ai("초안")])
    out = asyncio.run(m.draft_section(outline="개요", refs=["r-1"], ctx=_ctx(ME)))
    assert out["sources"][0]["ok"] is False and "내 결과의 ref 가 아니다" in out["sources"][0]["error"], "남의 것은 없는 것과 같은 404 다"
    gw.owner = ME
    kit.mp.setitem(a._ctx_cache, "n", 20_000)
    with pytest.raises(ValueError, match="창에 실을 수 있는"):
        asyncio.run(m.draft_section(outline="개요", refs=["r-1"], ctx=_ctx(ME)))
    with pytest.raises(ValueError, match="ref 꼴"):
        asyncio.run(offload.read_ref(kit.app, "../etc/passwd", ME))


# ── D 변환 ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_변환은_글_하나만_받아_챗_LLM_으로_바꾸고_숫자를_대조한다(kit, _pin_context):
    kit.llm.script = [_ai("Defects: 1,234 · ratio 12.5% · new 4321.0", model="qwen-x")]
    out = asyncio.run(m.llm_transform(instruction="영어로", text=VOC_OUT, ctx=_ctx()))
    assert out == {"text": "Defects: 1,234 · ratio 12.5% · new 4321.0", "numbers": {"checked": 3, "mismatched": ["4321.0"], "kinds": {"4321.0": "원문에 없음"}}, "model": "qwen-x",
                   "tokens": {"input": 10, "output": 5}}
    for kw in ({}, {"text": "a", "ref": "r-1"}):
        with pytest.raises(ValueError, match="하나만"):
            asyncio.run(m.llm_transform(instruction="영어로", ctx=_ctx(), **kw))
    kit.mp.setattr(offload, "TRANSFORM_OUT_MAX", 10)
    kit.llm.script = [_ai("가" * 30)]
    out = asyncio.run(m.llm_transform(instruction="늘려라", text="원문", ctx=_ctx()))
    assert out["text"].startswith("가" * 10) and "잘랐다" in out["cut"]


def test_변환은_ref_를_서버가_읽고_원문이_창을_넘으면_거절한다(kit, _pin_context):
    gw = _GwRef({"r-9": "표 원문 3.14"})
    kit.mp.setattr(offload, "GW_TRANSPORT", httpx.MockTransport(gw.handle))
    kit.llm.script = [_ai("| 값 |\n| 3.14 |")]
    out = asyncio.run(m.llm_transform(instruction="마크다운 표로", ref="r-9", ctx=_ctx()))
    assert out["text"].endswith("3.14 |") and "표 원문 3.14" in kit.llm.seen[0][1].content
    kit.mp.setitem(a._ctx_cache, "n", 30_000)
    with pytest.raises(ValueError, match="챗 LLM 창"):
        asyncio.run(m.llm_transform(instruction="요약", text="나" * 9000, ctx=_ctx()))


# ── 결과에 묻기 창구 ────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def ask(kit, monkeypatch):
    """창구만 올린 앱(스위치를 켠 서버와 같은 경로 · 같은 함수) — 공유 비밀은 에이전트 앱의 게이트웨이 연결에서 읽는다."""
    monkeypatch.setattr(a.app.state, "connections", kit.app.state.connections, raising=False)
    fa = FastAPI()
    fa.add_api_route("/llm/ask_over_text", a.llm_ask_over_text, methods=["POST"])
    return SimpleNamespace(c=TestClient(fa), llm=kit.llm, mp=monkeypatch)


def _post(ask, body, user=ME, auth=SECRET):
    h = {"Authorization": auth, **({"x-hwax-user": user} if user else {})}
    return ask.c.post("/llm/ask_over_text", json=body, headers=h)


TEXT = "머리말 줄\n" + "가" * 30 + "\n불량 원인은 커넥터 접촉 저항 증가다.\n" + "나" * 30 + "\n수율은 97.5% 였다.\n"


def test_묻기_창구는_자격_신원_칸을_따진다(ask, _pin_context):
    assert _post(ask, {"text": "x", "question": "y"}, auth="Bearer wrong-secret").status_code == 401
    assert _post(ask, {"text": "x", "question": "y"}, user="").status_code == 400, "403 은 게이트웨이가 '공유 비밀 어긋남' 으로 읽는다"
    r = _post(ask, {"text": "x", "question": "y"}, auth="")
    assert r.status_code == 401
    r = _post(ask, {"text": "x", "question": "y", "ref": "r-1"})
    assert r.status_code == 422 and r.json()["code"] == "unknown_fields"
    assert _post(ask, {"text": "x", "question": "y", "max_answer_chars": "많이"}).status_code == 422


def test_한_쪽이면_한_번_묻고_원문에_없는_인용은_버리고_자리를_단다(ask, _pin_context):
    ask.llm.script = [_ai(json.dumps({"answer": "원인은 커넥터 접촉 저항 증가이고 수율은 97.5%, 목표 99.9%.",
                                      "quotes": [{"text": "불량 원인은 커넥터 접촉 저항 증가다."}, {"text": "지어낸 인용 문장입니다 정말로"},
                                                 {"text": "짧다"}, {"text": "«수율은 97.5% 였다.»"}]}, ensure_ascii=False), model="qwen-ask")]
    r = _post(ask, {"text": TEXT, "question": "불량 원인과 수율은?"})
    assert r.status_code == 200, r.text
    out = r.json()
    s1, s2 = TEXT.index("불량 원인은"), TEXT.index("수율은 97.5%")
    assert out["quotes"] == [{"page": 1, "start": s1, "end": s1 + len("불량 원인은 커넥터 접촉 저항 증가다."), "text": "불량 원인은 커넥터 접촉 저항 증가다."},
                             {"page": 1, "start": s2, "end": s2 + len("수율은 97.5% 였다."), "text": "수율은 97.5% 였다."}]
    assert out["numbers"] == {"checked": 2, "mismatched": ["99.9"], "kinds": {"99.9": "원문에 없음"}} and out["model"] == "qwen-ask"
    assert out["tokens"] == {"input": 10, "output": 5}
    assert len(ask.llm.seen) == 1 and TEXT in ask.llm.seen[0][1].content


def test_창이_모자라면_쪽마다_읽고_모으며_쪽_번호는_원문_전체의_자리로_센다(ask, _pin_context):
    ask.mp.setattr(offload, "ASK_PAGE_CHARS", 45)
    ask.mp.setattr(offload, "RESULT_PAGE_CHARS", 40)

    def _page(msgs):
        part = msgs[1].content.split("[이 쪽]\n", 1)[1]
        if "커넥터" in part:
            return _ai(json.dumps({"relevant": True, "notes": "원인 — 커넥터", "quotes": [{"text": "불량 원인은 커넥터 접촉 저항 증가다."}]}, ensure_ascii=False))
        if "수율" in part:
            return _ai(json.dumps({"relevant": True, "notes": "수율 97.5%", "quotes": [{"text": "수율은 97.5% 였다."}, {"text": "이 쪽에 없는 글을 지어냈다"}]},
                                  ensure_ascii=False))
        return _ai(json.dumps({"relevant": False, "notes": "", "quotes": []}))

    def _merge(msgs):
        assert "원인 — 커넥터" in msgs[1].content and "수율 97.5%" in msgs[1].content
        return _ai(json.dumps({"answer": "커넥터 접촉 저항 증가 · 수율 97.5%", "quotes": [{"text": "수율은 97.5% 였다."}]}, ensure_ascii=False))
    pages = len(offload._pages(TEXT, 45))
    assert pages >= 3
    ask.llm.script = [_page] * pages + [_merge]
    out = _post(ask, {"text": TEXT, "question": "원인과 수율?"}).json()
    s2 = TEXT.index("수율은 97.5%")
    assert out["answer"] == "커넥터 접촉 저항 증가 · 수율 97.5%"
    assert out["quotes"] == [{"page": s2 // 40 + 1, "start": s2, "end": s2 + len("수율은 97.5% 였다."), "text": "수율은 97.5% 였다."}]
    assert len(ask.llm.seen) == pages + 1
    assert out["tokens"] == {"input": 10 * (pages + 1), "output": 5 * (pages + 1)}, "쪽마다 부른 것과 모으기를 더한 합"


def test_쪽이_상한을_넘으면_413_으로_좁히라고_한다(ask, _pin_context):
    ask.mp.setattr(offload, "ASK_PAGE_CHARS", 10)
    r = _post(ask, {"text": TEXT, "question": "무엇?"})
    assert r.status_code == 413 and r.json()["code"] == "too_large" and ask.llm.seen == []


# ── 서버 LLM 토큰 · 사람별 사용량 종류(TS-16) ───────────────────────────────────────────────────────────────────────
def test_사용량이_없는_답뿐이면_tokens_는_null_이다_지어내지_않는다(ask, _pin_context):
    bare = AIMessage(content=json.dumps({"answer": "수율은 97.5%", "quotes": []}, ensure_ascii=False), response_metadata={"model_name": "q"})
    ask.llm.script = [bare]
    out = _post(ask, {"text": TEXT, "question": "수율?"}).json()
    assert "tokens" in out and out["tokens"] is None
    ask.llm.script = [AIMessage(content="Yield 97.5%")]
    assert asyncio.run(m.llm_transform(instruction="영어로", text=TEXT, ctx=_ctx()))["tokens"] is None


@pytest.fixture
def usage(monkeypatch, tmp_path):
    """사람별 LLM 사용량 기록기 — 임시 자리 · 더하는 스레드 없이. 가짜 LLM 에 진짜 기록기를 단다(앱의 LLM 공장과 같은 instrument)."""
    monkeypatch.setattr(usage_tokens, "STORE_DIR", tmp_path / "usage")
    monkeypatch.setattr(usage_tokens, "_ensure_thread", lambda: None)
    usage_tokens._reset_for_tests()
    yield lambda: sorted({(u, k) for (_d, u, k, _m), row in usage_tokens._ACC.items() for _ in range(row[0])})
    usage_tokens._reset_for_tests()


def test_넷의_LLM_사용량은_종류_offload_로_그_사람에게_단다(kit, ask, usage, _pin_context):
    assert "offload" in usage_tokens.KINDS and usage_tokens.KINDS[-1] == "other"
    usage_tokens.instrument(kit.llm)
    glm = usage_tokens.instrument(FakeChat(script=[_ai("## 초안 — 불량 1,234건 [s1]", model="glm-5.3")]))
    kit.app.state.delib_llm = glm
    # 위임 — 잡 태스크(사람은 잡 원장이, 종류는 실행기가 단다)
    kit.llm.script = [_ai("", _call("voc_search", q="S26")), _ai("불량 1,234건 [e1]")]

    async def scenario():
        out = await m.delegate_start(task="S26 불량 수", ctx=_ctx(ME))
        await _done(out["job_id"])
    _run(scenario)
    assert usage() == [(ME, "offload")], "위임의 LLM 은 offload — 잡 원장의 other 가 아니다"
    usage_tokens._reset_for_tests()
    asyncio.run(m.draft_section(outline="불량 정리", refs=[{"tool": "voc_search", "args": {"q": "S26"}}], ctx=_ctx(YOU)))
    kit.llm.script = [_ai("Defects 1,234")]
    asyncio.run(m.llm_transform(instruction="영어로", text=VOC_OUT, ctx=_ctx(ME)))
    ask.llm.script = [_ai(json.dumps({"answer": "97.5%", "quotes": []}))]
    assert _post(ask, {"text": TEXT, "question": "수율?"}, user="third%40example.com").status_code == 200
    assert usage() == sorted([(YOU, "offload"), (ME, "offload"), ("third@example.com", "offload")]), \
        "작성 · 변환 · 묻기도 그 사람(묻기는 신원 머리를 풀어) · offload 로"
