# 심의(쟁점 패널)를 외부 응답자로 — delib_opts.llm=external 이 evidence_only 를 강제하고 외부 창으로 셈하며 결과·잡 기록에 표지를 남기는지, 한도가 기존 오류 길(좌석 유실·의장 실패)로 cause 를 싣고 나가는지 본다
#
# 쟁점 패널은 리스크 앱이 포털 /agent/chat 으로 띄우는 심의다(free_tools=1 · tools=[] · voc=off · risk-review · save_report=0 ·
# started_by=app). 같은 모양으로 run_deliberation 을 **실제로** 돌리고 LLM 자리에는 아무것도 바꿔 끼우지 않는다 — 판단 호출은 전부
# 외부 응답자 과제로 가고 가짜 일꾼(tests/fakes/worker.py)이 실제 MCP 도구 함수로 집어 심의 대본으로 답한다.
#
#   실행:  .venv/bin/python -m pytest tests/test_external_llm_delib.py -q
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import app  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import external_llm as x  # noqa: E402
import mcp_server as m  # noqa: E402
from fakes.llm import delib_respond  # noqa: E402
from fakes.worker import FakeWorker, ctx_of  # noqa: E402

OWNER = "owner@x.com"
SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}, {"key": "pcb-c", "role": "보드"}]
TK = "diff:" + "ab" * 16
Q = f"[리스크심사 S26 Ultra {TK}] 쟁점 패널 — 보스 간극"
PANEL = {"personas": SEATS, "free_tools": 1, "tool_budget": 3, "tools": [], "voc": "off", "rescreen": 0,
         "chair_template": "risk-review", "save_report": 0, "started_by": "app", "rounds": 3, "llm": "external",
         "consent_external": True,
         "evidence": [{"key": "I1", "source": "쟁점 닻", "result": f"{TK} · mechanical.drop_stress · ck:0123456789ab"}]}


class _Tool:
    coroutine = None

    def __init__(self, name, answer='{"hits": []}'):
        self.name, self._answer, self.calls = name, answer, []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self._answer


def scaled(k: float = 1000.0):
    t0 = time.monotonic()
    return lambda: 1000.0 + (time.monotonic() - t0) * k


@pytest.fixture
def ctx_1m(monkeypatch):
    """서버 모델 창은 1백만 토큰(GLM) — 외부 응답자 창(200,000)과 갈리게."""
    monkeypatch.setattr(app, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    d._evid_cache.clear()
    yield
    d._evid_cache.clear()


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")


def _panel(monkeypatch, opts=None, *, user=OWNER, worker=True, board=None, entry=None, brain=None):
    """쟁점 패널 하나를 끝까지 — (사건, 일꾼, create_react_agent 에 묶인 도구, 도구 사전, 줄). brain 을 주면 일꾼이 그것으로 답한다."""
    import langgraph.prebuilt

    tools = {"agent_search": _Tool("agent_search"), "create_report_draft": _Tool("create_report_draft", '{"id": 7}')}

    async def fake_tools(*_a, **_k):
        return tools

    bound = []
    monkeypatch.setattr(d, "_tools_by_name", fake_tools)
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent",
                        lambda _llm, ts, **_k: bound.extend(t.name for t in ts) or object())
    board = board or x.Board(x.Settings())
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None, external_board=board))
    monkeypatch.setattr(m, "_APP", stub)

    async def go():
        w = FakeWorker(user or "nobody@x.com", poll_s=0.05, **({"brain": brain} if brain else {})) if worker else None
        serve = asyncio.ensure_future(w.serve()) if w else None
        out = []
        gen = (entry or d.run_deliberation)(stub, Q, [], {**PANEL, **(opts or {})}, user)
        try:
            async for chunk in gen:
                out.append(delib_jobs._parse_sse(chunk))
        finally:
            await gen.aclose()
            if w:
                w.stop()
                await serve
        return out, w

    events, w = asyncio.run(go())
    return events, w, bound, tools, board


def _cards(events):
    return [data for ev, data in events if ev == "delib" and data.get("kind") == "evidence"]


# ── 끝까지 — evidence_only · 표지 · 판정 ───────────────────────────────────────────
def test_외부_응답자로_쟁점_패널이_evidence_only_로_끝까지_돌고_결과에_표지가_남는다(monkeypatch, ctx_1m, on):
    events, w, bound, tools, board = _panel(monkeypatch)
    assert not [data for ev, data in events if ev == "error"], events[-3:]
    assert bound == [], "좌석에 도구를 묶었다 — evidence_only 가 아니다"
    steps = [data.get("step", "") for ev, data in events if ev == "status"]
    assert any(s.startswith("외부 응답자 실행 — ") for s in steps)
    notice = next(c for c in _cards(events) if c["source"] == "외부 응답자 실행 — evidence_only")
    assert notice["notice"] is True and notice["included"] is False and "free_tools=1 는 닫혔다" in notice["text"]
    assert "200,000 토큰" in notice["text"], "외부 응답자 창으로 셈했다고 밝혀야 한다"
    result = next(data for ev, data in events if ev == "result")
    assert (result["llm"], result["tool_mode"]) == ("external", "evidence_only")
    decision = next(data["text"] for ev, data in events if ev == "delib" and data.get("kind") == "decision")
    assert decision.startswith("■ 외부 응답자 실행 — ") and result["content"].startswith("■ 외부 응답자 실행 — ")
    spec = json.loads(re.findall(r"```json\s*(\{.*?\})\s*```", decision, re.S)[-1])
    assert (spec["schema"], spec["findings"][0]["judgement"], spec["scope"]["target_key"]) == ("risk_spec", "FAIL", TK)
    heads = [s["head"] for s in w.seen]
    assert heads and {h["kind"] for h in heads} == {"deliberation"}
    assert {h["target_key"] for h in heads} == {TK} and {h["label"] for h in heads} == {"risk-review"}
    assert len({h["run_id"] for h in heads}) == 1 and heads[0]["run_id"].startswith("delib-")
    seated = {re.search(r"'([^']+)' 전문가입니다", s["system"]).group(1) for s in w.seen if "전문가입니다" in s["system"]}
    assert seated == {"mech-a", "rel-b", "pcb-c", "delib-baseline-defender"}
    assert sum("전문가입니다" in s["system"] for s in w.seen) == len(seated) * 3, "좌석 발언이 전부 과제로 가지 않았다"
    assert any(s["system"].startswith("당신은 심의체 의장입니다") for s in w.seen), "의장도 외부 응답자다"
    assert all(s["answer"] == delib_respond(s["system"], s["human"]) for s in w.seen)
    assert not any("cause" in data for ev, data in events if ev in ("warning", "error"))
    assert board.counts() == {"queued": 0, "claimed": 0} and not w.errors
    assert tools["create_report_draft"].calls == [], "save_report=0 인데 저장했다"


def test_외부_창으로_셈해_1M_창이면_들어갈_큰_근거가_줄어_실리고_창은_그_심의_밖으로_새지_않는다(monkeypatch, ctx_1m, on):
    big = "보스 둘레 간극과 낙하 응력에 대한 큰 근거 본문. " * 9000          # 약 27만 자 — 1백만 토큰 창의 근거 몫 안
    assert len(big) < d._evid_budget(), "시험 전제 — 1백만 토큰 창에서는 통째로 들어간다"
    events, w, *_ = _panel(monkeypatch, {"evidence": [{"key": "I6", "source": "카드 절", "result": big}]})
    seat_humans = [s["human"] for s in w.seen if "전문가입니다" in s["system"]]
    assert seat_humans and all(big not in h for h in seat_humans), "외부 창(20만 토큰)인데 근거를 통째로 실었다"
    assert all(len(h) < x.CTX_DEFAULT * 1.05 for h in seat_humans), "좌석 프롬프트가 외부 창보다 크다"
    assert any(big[:2000] in h for h in seat_humans), "근거 앞부분조차 실리지 않았다"
    assert app._model_context_tokens() == 1_048_576, "외부 창이 그 심의 밖(이 문맥)으로 샜다"
    assert x.ctx_override.get() is None


# ── 한도 — 좌석 유실·의장 실패에 cause ─────────────────────────────────────────────
def test_일꾼이_없으면_좌석_유실과_의장_실패가_cause_를_싣고_빨리_끝난다(monkeypatch, ctx_1m, on):
    board = x.Board(x.Settings(wait_s=300), clock=scaled(), tick=0.02)
    t0 = time.monotonic()
    events, *_ = _panel(monkeypatch, worker=False, board=board)
    took = time.monotonic() - t0
    lost = [data for ev, data in events if ev == "warning" and data.get("code") == "seat_lost"]
    assert lost and all(data["cause"] == "external_worker_absent" for data in lost)
    assert "external_worker_absent" in lost[0]["message"] and lost[0]["knob"] == "EXTERNAL_LLM_WAIT_S"
    err = [data for ev, data in events if ev == "error"]
    assert len(err) == 1 and (err[0]["code"], err[0]["cause"]) == ("chair_failed", "external_worker_absent")
    result = next(data for ev, data in events if ev == "result")
    assert result["llm"] == "external", "결정문 없이 끝나도 외부 응답자로 돈 심의다"
    assert took < 10, f"없다고 판정한 뒤에도 과제마다 기다렸다 — {took:.1f}초"
    assert board.counts() == {"queued": 0, "claimed": 0}


def test_꺼진_서버와_신원_없는_호출은_심의를_열기_전에_거절한다(monkeypatch, ctx_1m):
    events, *_ = _panel(monkeypatch, worker=False)               # 시험 세션은 꺼진 서버다
    assert [ev for ev, _ in events] == ["error", "done"]
    assert (events[0][1]["code"], events[0][1]["knob"]) == ("external_llm_disabled", "EXTERNAL_LLM_ENABLED")
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    for entry in (d.run_deliberation, d.run_sim_deliberation, d.run_test_plan):
        events, *_ = _panel(monkeypatch, user="", worker=False, entry=entry)
        assert [ev for ev, _ in events] == ["error", "done"] and events[0][1]["code"] == "external_owner_missing", entry


def test_llm_을_안_주면_GLM_길_그대로다(monkeypatch, ctx_1m, on):
    seen = []

    async def fake_llm(_llm, system, human):
        seen.append(system)
        return delib_respond(system, human)

    monkeypatch.setattr(d, "_llm_text", fake_llm)
    events, w, bound, *_ = _panel(monkeypatch, {"llm": None, "free_tools": 0}, worker=False)
    result = next(data for ev, data in events if ev == "result")
    assert set(result) == {"type", "content"} and not result["content"].startswith("■ 외부 응답자")
    assert not any(c["source"].startswith("외부 응답자") for c in _cards(events))
    assert seen and not [data for ev, data in events if ev == "error"]


# ── MCP 잡 길(deliberate_start) ─────────────────────────────────────────────────
def _mcp(monkeypatch, tmp_path, *, advanced, ctx=ctx_of(OWNER)):
    import langgraph.prebuilt

    tools = {"agent_search": _Tool("agent_search"), "create_report_draft": _Tool("create_report_draft", '{"id": 7}')}

    async def fake_tools(*_a, **_k):
        return tools

    monkeypatch.setattr(d, "_tools_by_name", fake_tools)
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda *_a, **_k: pytest.fail("도구를 묶었다"))
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None, external_board=x.Board(x.Settings())))
    monkeypatch.setattr(m, "_APP", stub)

    async def go():
        w = FakeWorker(OWNER, poll_s=0.05)
        serve = asyncio.ensure_future(w.serve())
        try:
            out = await m.deliberate_start(Q, job="risk-review", rounds=2, personas=SEATS, save_report=False,
                                           advanced=advanced, ctx=ctx)
            await delib_jobs._TASKS[out["job_id"]]
            done = await m.deliberate_result(out["job_id"])
            tr = await m.deliberate_transcript(out["job_id"])
            return out, done, tr
        finally:
            w.stop()
            await serve

    return asyncio.run(go())


def test_MCP_심의_시작도_외부_응답자로_돌고_잡_기록에_llm_과_tool_mode_가_남는다(monkeypatch, tmp_path, ctx_1m, on):
    out, done, tr = _mcp(monkeypatch, tmp_path, advanced={"llm": "external", "consent_external": True, "voc": "off",
                                                          "free_tools": 1})
    assert (out["llm"], out["tool_mode"]) == ("external", "evidence_only")
    assert done["status"] == "done", done.get("error")
    # 손잡이 칸은 호출자가 보낸 값(1)이고 외부 응답자 길이 닫은 값은 따로 적힌다(GLM 이어하기가 닫힌 0 을 물려받지 않게 — L5).
    ao = done["applied_opts"]
    assert (done["llm"], ao["llm"], ao["free_tools"], ao["external_forced"]) == ("external", "external", 1, {"free_tools": 0})
    assert ao["consent_external"] is True, "반출 동의 표지가 잡 기록에 남아야 한다"
    assert done["decision"].startswith("■ 외부 응답자 실행 — ")
    assert (tr["llm"], tr["tool_mode"]) == ("external", "evidence_only") and tr["total"] > 0
    assert any(e["source"] == "외부 응답자 실행 — evidence_only" for e in done["evidence_omitted"])


def test_MCP_심의_시작은_꺼진_서버와_신원_없는_호출을_잡을_만들기_전에_거절한다(monkeypatch, tmp_path, ctx_1m):
    with pytest.raises(ValueError, match="^external_llm_disabled"):
        _mcp(monkeypatch, tmp_path, advanced={"llm": "external"})
    assert delib_jobs._JOBS == {}
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    with pytest.raises(ValueError, match="^external_owner_missing"):
        _mcp(monkeypatch, tmp_path, advanced={"llm": "external", "consent_external": True}, ctx=None)
    assert delib_jobs._JOBS == {}


# ── 요청 값 읽기 · 안내 ─────────────────────────────────────────────────────────
def test_심의_요청의_llm_읽기():
    o = d._resolve_opts({"llm": "external", "free_tools": 1})
    assert (o.llm, o.free_tools, o.external_closed) == ("external", 0, ["free_tools=1"])
    o = d._resolve_opts({"llm": "external"})
    assert (o.llm, o.free_tools, o.external_closed) == ("external", 0, []), "서버 기본값으로 켜진 것은 호출자가 청한 것이 아니다"
    o = d._resolve_opts({"llm": "gpt", "free_tools": 1})
    assert (o.llm, o.free_tools) == ("glm", 1) and "llm=gpt" in o.req_unread["llm"]
    assert d._resolve_opts({}).llm == "glm" and d._resolve_opts(None).external_closed == []
    assert json.dumps(vars(d._resolve_opts(None)), default=str)          # 직렬화가 깨지지 않는다


def test_꺼진_서버의_심의_옵션_안내에는_llm_이_없다():
    assert "llm(판단 LLM" not in m._ADV_DESC
