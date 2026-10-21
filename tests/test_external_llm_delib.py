# 심의(쟁점 패널)를 외부 응답자로 — delib_opts.llm=external 이 좌석 자유 조회를 글 왕복(응답자가 CALL 줄을 적고 엔진이 실행)으로 돌리고 외부 창으로 셈하며 결과·잡 기록에 표지를 남기는지, 한도가 기존 오류 길(좌석 유실·의장 실패)로 cause 를 싣고 나가는지 본다
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
# 리스크 앱 쟁점 패널의 질문 꼴(issues.issue_question) — '[쟁점 …]' 로 시작해 위 머리가 없다. 대상 열쇠는 delib_opts.target_key 로 온다.
Q_ISSUE = ("[쟁점 iss-0123456789ab · held_fail] 부품 보스 의 치수 변경에서 메커니즘 낙하 응력(mechanical.drop_stress) 이 성립하는가."
           " 결정문의 finding 은 mechanism·mechanism_detail·subject·change_kind 네 필드를 [근거] I1 의 값 그대로 쓴다.")
PANEL = {"personas": SEATS, "free_tools": 1, "tool_budget": 3, "tools": [], "voc": "off", "rescreen": 0,
         "chair_template": "risk-review", "save_report": 0, "started_by": "app", "rounds": 3, "llm": "external",
         "consent_external": True, "target_key": TK,
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


def _panel(monkeypatch, opts=None, *, user=OWNER, worker=True, board=None, entry=None, brain=None, question=Q,
           search_answer='{"hits": []}', tools_loader=None):
    """쟁점 패널 하나를 끝까지 — (사건, 일꾼, create_react_agent 에 묶인 도구, 도구 사전, 줄). brain 을 주면 일꾼이 그것으로 답한다.
    opts 의 값이 None 이면 PANEL 에서 그 칸을 뺀다(보내지 않은 것과 같다). search_answer 는 agent_search 가 돌려줄 글이고,
    tools_loader 를 주면 도구 사전 대신 그것이 도구를 낸다."""
    import langgraph.prebuilt

    tools = {"agent_search": _Tool("agent_search", search_answer),
             "create_report_draft": _Tool("create_report_draft", '{"id": 7}')}

    async def fake_tools(*_a, **_k):
        return tools

    bound = []
    monkeypatch.setattr(d, "_tools_by_name", tools_loader or fake_tools)
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent",
                        lambda _llm, ts, **_k: bound.extend(t.name for t in ts) or object())
    board = board or x.Board(x.Settings())
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None, external_board=board))
    monkeypatch.setattr(m, "_APP", stub)

    async def go():
        w = FakeWorker(user or "nobody@x.com", poll_s=0.05, **({"brain": brain} if brain else {})) if worker else None
        serve = asyncio.ensure_future(w.serve()) if w else None
        out = []
        sent = {k: v for k, v in {**PANEL, **(opts or {})}.items() if not (k in (opts or {}) and v is None)}
        gen = (entry or d.run_deliberation)(stub, question, [], sent, user)
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


# ── 끝까지 — 글 왕복 조회 · 표지 · 판정 ───────────────────────────────────────────
def test_외부_응답자로_쟁점_패널이_끝까지_돌고_좌석_조회는_글_왕복이며_결과에_표지가_남는다(monkeypatch, ctx_1m, on):
    events, w, bound, tools, board = _panel(monkeypatch)
    assert not [data for ev, data in events if ev == "error"], events[-3:]
    assert bound == [], "외부 응답자 좌석을 create_react_agent 에 묶었다 — 외부 응답자에는 도구를 묶을 수 없다(글 왕복이어야 한다)"
    steps = [data.get("step", "") for ev, data in events if ev == "status"]
    assert any(s.startswith("외부 응답자 실행 — ") and "글 왕복" in s for s in steps)
    assert any(s.startswith("전문가 자유 조회 활성 — ") and "CALL 줄" in s for s in steps)
    notice = next(c for c in _cards(events) if c["source"] == "외부 응답자 실행 — 좌석 도구 글 왕복")
    assert notice["notice"] is True and notice["included"] is False and "엔진이 그 도구를 실행" in notice["text"]
    assert "200,000 토큰" in notice["text"], "외부 응답자 창으로 셈했다고 밝혀야 한다"
    result = next(data for ev, data in events if ev == "result")
    assert (result["llm"], result["tool_mode"]) == ("external", "tools")
    decision = next(data["text"] for ev, data in events if ev == "delib" and data.get("kind") == "decision")
    assert decision.startswith("■ 외부 응답자 실행 — ") and result["content"].startswith("■ 외부 응답자 실행 — ")
    assert "글 왕복으로 돌았다" in decision.split("\n", 1)[0]
    # 좌석마다 조회 과제(1 · 2 라운드 — 수렴은 조회하지 않는다)가 외부 응답자에게 갔다 — 도구 목록은 읽기 전용 후보만(쓰기 도구는 없다)
    lookups = [s for s in w.seen if "[글로 도구 부르기" in s["system"]]
    assert len(lookups) == 4 * 2, len(lookups)
    assert all("- agent_search — " in s["system"] and "create_report_draft" not in s["system"] for s in lookups)
    spec = json.loads(re.findall(r"```json\s*(\{.*?\})\s*```", decision, re.S)[-1])
    assert (spec["schema"], spec["findings"][0]["judgement"], spec["scope"]["target_key"]) == ("risk_spec", "FAIL", TK)
    heads = [s["head"] for s in w.seen]
    assert heads and {h["kind"] for h in heads} == {"deliberation"}
    assert {h["target_key"] for h in heads} == {TK} and {h["label"] for h in heads} == {"risk-review"}
    assert not any("job_id" in h for h in heads), "포털 /chat 심의(MCP 잡이 아니다)에 잡 id 가 실렸다"
    assert len({h["run_id"] for h in heads}) == 1 and heads[0]["run_id"].startswith("delib-")
    seated = {re.search(r"'([^']+)' 전문가입니다", s["system"]).group(1) for s in w.seen if "전문가입니다" in s["system"]}
    assert seated == {"mech-a", "rel-b", "pcb-c", "delib-baseline-defender"}
    assert sum("전문가입니다" in s["system"] for s in w.seen) == len(seated) * 3, "좌석 발언이 전부 과제로 가지 않았다"
    assert any(s["system"].startswith("당신은 심의체 의장입니다") for s in w.seen), "의장도 외부 응답자다"
    assert all(s["answer"] == delib_respond(s["system"], s["human"]) for s in w.seen)
    assert not any("cause" in data for ev, data in events if ev in ("warning", "error"))
    assert board.counts() == {"queued": 0, "claimed": 0} and not w.errors
    assert tools["create_report_draft"].calls == [], "save_report=0 인데 저장했다"


HIT = '{"hits": [{"id": "card-77", "text": "보스 둘레 간극 0.21 mm — 낙하 1.5 m 에서 접촉"}]}'
CALLS = ['CALL {"tool": "agent_search", "args": {"query": "보스 간극"}}',
         'CALL {"tool": "create_report_draft", "args": {"title": "몰래 저장"}}',      # 목록에 없는 도구(쓰기 — 자유 조회 후보가 아니다)
         'CALL {"tool": "agent_search", 깨진 줄}',                                    # JSON 이 아니다
         'CALL {"tool": "agent_search", "args": "보스"}',                              # args 가 객체가 아니다
         'CALL {"tool": "agent_search", "args": {"query": "낙하 응력"}}',
         'CALL {"tool": "agent_search", "args": {"query": "접착 계면"}}',
         'CALL {"tool": "agent_search", "args": {"query": "넷째 — 예산 밖"}}']        # tool_budget 3 을 넘는다


def _lookup_brain(head, system, human):
    """조회 과제면 첫 차례에 CALL 줄을 적고 다음 차례에 요약한다 — 그 밖은 심의 대본."""
    if "[글로 도구 부르기" not in system:
        return delib_respond(system, human)
    if "[이 조회 단계에서 부른 것" not in human:
        return "필요한 조회를 부른다.\n" + "\n".join(CALLS)
    return "조회 요약: 보스 둘레 간극 0.21 mm(card-77) — 낙하 1.5 m 에서 접촉."


def test_외부_응답자가_적은_CALL_줄을_엔진이_실행해_근거_공용_풀_발언_턴에_싣는다(monkeypatch, ctx_1m, on):
    events, w, bound, tools, _board = _panel(monkeypatch, brain=_lookup_brain, search_answer=HIT)
    assert not [data for ev, data in events if ev == "error"], events[-3:]
    assert bound == []
    # 엔진이 실행한 것은 목록에 있는 도구 · 예산 안의 줄뿐이다 — 좌석 넷 · 조회 라운드 둘 · 좌석마다 셋
    assert tools["create_report_draft"].calls == [], "목록에 없는 쓰기 도구를 실행했다"
    seat_calls = [c for c in tools["agent_search"].calls if "query" in c]     # 엔진이 스스로 부르는 지식 조회(q · agent_type)는 빼고
    assert len(seat_calls) == 4 * 2 * 3
    assert {c["query"] for c in seat_calls} == {"보스 간극", "낙하 응력", "접착 계면"}
    # 두 번째 차례 과제 — 실행한 호출과 결과, 실행하지 않은 줄과 까닭, 예산을 다 썼다는 말
    second = [s for s in w.seen if "[글로 도구 부르기" in s["system"] and "[이 조회 단계에서 부른 것" in s["human"]]
    assert len(second) == 4 * 2
    h = second[0]["human"]
    assert 'CALL {"tool": "agent_search", "args": {"query": "보스 간극"}}\n결과:\n' + HIT in h
    assert "목록에 없는 도구다(create_report_draft)" in h and "JSON 으로 읽지 못했다" in h
    assert "args 가 JSON 객체가 아니다" in h and "호출 예산(3회)을 넘었다" in h and "넷째 — 예산 밖" in h
    assert "호출 예산을 다 썼다" in h
    # 조회가 GLM 길과 같은 자리로 흐른다 — 상태줄(짝 키) · 근거 카드 · 좌석 발언 턴
    looked = [data for ev, data in events if ev == "status" and data.get("tool") == "agent_search" and " 조회: " in data["step"]]
    assert len(looked) == 4 * 2 * 3 and all(data["ok"] and data["call"].startswith("text-") for data in looked)
    cards = [c for c in _cards(events) if c["source"].endswith(" · agent_search")]
    assert len(cards) == 4 * 2 * 3 and all(c["included"] and c["text"] == HIT for c in cards)
    speech = [s for s in w.seen if "전문가입니다" in s["system"]]
    assert speech and any("보스 둘레 간극 0.21 mm" in s["human"] for s in speech), "조회 결과가 좌석 발언 턴에 실리지 않았다"
    assert any("(전문가 자체 요약) 조회 요약: 보스 둘레 간극 0.21 mm" in s["human"] for s in speech)
    result = next(data for ev, data in events if ev == "result")
    assert result["tool_mode"] == "tools"


def test_free_tools_0_이면_외부_응답자_심의는_evidence_only_다(monkeypatch, ctx_1m, on):
    events, w, bound, tools, _board = _panel(monkeypatch, {"free_tools": 0}, brain=_lookup_brain, search_answer=HIT)
    assert not [data for ev, data in events if ev == "error"], events[-3:]
    assert not [s for s in w.seen if "[글로 도구 부르기" in s["system"]]
    assert not [c for c in tools["agent_search"].calls if "query" in c], "좌석 자유 조회가 돌았다"
    notice = next(c for c in _cards(events) if c["source"] == "외부 응답자 실행 — evidence_only")
    assert "free_tools=0" in notice["text"] and "글 왕복" not in notice["text"]
    start = next(data["step"] for ev, data in events if ev == "status" and data.get("step", "").startswith("외부 응답자 실행 — "))
    assert "evidence_only 로 돈다" in start and "글 왕복" not in start
    result = next(data for ev, data in events if ev == "result")
    assert (result["llm"], result["tool_mode"]) == ("external", "evidence_only")
    assert "evidence_only 로 돌았다" in result["content"].split("\n", 1)[0]


async def _write_only(*_a, **_k):
    """쓰기 도구 하나뿐 — 엔진은 돌지만 좌석 자유 조회 후보(읽기 전용)는 하나도 없다."""
    return {"create_report_draft": _Tool("create_report_draft", '{"id": 7}')}


def test_쓸_도구가_하나도_없으면_요청이_켜도_결과는_evidence_only_다(monkeypatch, ctx_1m, on):
    events, w, *_ = _panel(monkeypatch, brain=_lookup_brain, tools_loader=_write_only)
    assert any(ev == "warning" and data.get("code") == "free_tools_unavailable" for ev, data in events)
    result = next(data for ev, data in events if ev == "result")
    assert result["tool_mode"] == "evidence_only" and "evidence_only 로 돌았다" in result["content"].split("\n", 1)[0]


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
def _mcp(monkeypatch, tmp_path, *, advanced, ctx=ctx_of(OWNER), tools_loader=None, question=Q, job="risk-review",
         with_worker=False):
    import langgraph.prebuilt

    tools = {"agent_search": _Tool("agent_search"), "create_report_draft": _Tool("create_report_draft", '{"id": 7}')}

    async def fake_tools(*_a, **_k):
        return tools

    monkeypatch.setattr(d, "_tools_by_name", tools_loader or fake_tools)
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda *_a, **_k: pytest.fail("도구를 묶었다"))
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None, external_board=x.Board(x.Settings())))
    monkeypatch.setattr(m, "_APP", stub)

    w = FakeWorker(OWNER, poll_s=0.05)

    async def go():
        serve = asyncio.ensure_future(w.serve())
        try:
            out = await m.deliberate_start(question, job=job, rounds=2, personas=SEATS, save_report=False,
                                           advanced=advanced, ctx=ctx)
            await delib_jobs._TASKS[out["job_id"]]
            done = await m.deliberate_result(out["job_id"])
            tr = await m.deliberate_transcript(out["job_id"])
            return out, done, tr
        finally:
            w.stop()
            await serve

    got = asyncio.run(go())
    return (*got, w) if with_worker else got


def test_MCP_심의_시작도_외부_응답자로_돌고_잡_기록에_llm_과_tool_mode_가_남는다(monkeypatch, tmp_path, ctx_1m, on):
    out, done, tr, w = _mcp(monkeypatch, tmp_path, advanced={"llm": "external", "consent_external": True, "voc": "off",
                                                             "free_tools": 1}, with_worker=True)
    # 과제마다 머리에 이 잡의 id 가 실린다(워크플로가 맡은 잡의 과제만 답한다 — 리스크 대상이 없는 일반 심의의 표지)
    assert w.seen and {s["head"].get("job_id") for s in w.seen} == {out["job_id"]}
    assert (out["llm"], out["tool_mode"]) == ("external", "tools")
    assert done["status"] == "done", done.get("error")
    # 손잡이 칸은 호출자가 보낸 값 그대로 돈다 — 외부 응답자 길이 닫는 값이 없다(좌석 자유 조회는 글 왕복이다).
    ao = done["applied_opts"]
    assert (done["llm"], ao["llm"], ao["free_tools"]) == ("external", "external", 1) and "external_forced" not in ao
    assert ao["consent_external"] is True, "반출 동의 표지가 잡 기록에 남아야 한다"
    assert done["decision"].startswith("■ 외부 응답자 실행 — ") and done["tool_mode"] == "tools"
    job = delib_jobs._JOBS[out["job_id"]]
    assert delib_jobs.summary(delib_jobs._list_rec(job))["tool_mode"] == "tools", "목록 색인이 tool_mode 를 잃었다"
    assert (tr["llm"], tr["tool_mode"]) == ("external", "tools") and tr["total"] > 0
    assert any(e["source"] == "외부 응답자 실행 — 좌석 도구 글 왕복" for e in done["evidence_omitted"])


def test_대상_열쇠가_없는_일반_MCP_외부_심의도_과제_머리의_잡_id_로_가려진다(monkeypatch, tmp_path, ctx_1m, on):
    out, done, _tr, w = _mcp(monkeypatch, tmp_path, question="보스 둘레 간극을 줄일 것인가", job="default",
                             advanced={"llm": "external", "consent_external": True, "voc": "off"}, with_worker=True)
    assert done["status"] == "done", done.get("error")
    heads = [s["head"] for s in w.seen]
    assert heads and {h.get("job_id") for h in heads} == {out["job_id"]} and not any(h.get("target_key") for h in heads)


def test_MCP_외부_잡의_tool_mode_는_엔진이_실제로_돈_길로_고쳐_적힌다(monkeypatch, tmp_path, ctx_1m, on):
    """시작 응답은 요청으로 적고(free_tools=1 → tools), 쓸 도구가 하나도 없어 evidence_only 로 돌았으면 result 프레임이 고쳐 적는다.
    tool_mode 칸이 없는 옛 잡 기록은 글 왕복 전에 돈 것이라 evidence_only 로 읽는다."""
    out, done, tr = _mcp(monkeypatch, tmp_path, advanced={"llm": "external", "consent_external": True, "voc": "off",
                                                          "free_tools": 1}, tools_loader=_write_only)
    assert out["tool_mode"] == "tools" and done["status"] == "done", done.get("error")
    assert (done["tool_mode"], tr["tool_mode"]) == ("evidence_only", "evidence_only")
    old = {**delib_jobs._JOBS[out["job_id"]]}
    old.pop("tool_mode")
    assert delib_jobs.summary(old)["tool_mode"] == "evidence_only"


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
    # 외부 응답자 길이 좌석 자유 조회를 닫지 않는다 — 호출자가 보낸 값 · 서버 기본값 그대로다(글 왕복)
    o = d._resolve_opts({"llm": "external", "free_tools": 1})
    assert (o.llm, o.free_tools) == ("external", 1) and not hasattr(o, "external_closed")
    assert d._resolve_opts({"llm": "external"}).free_tools == d._resolve_opts({}).free_tools
    assert d._resolve_opts({"llm": "external", "free_tools": 0}).free_tools == 0
    o = d._resolve_opts({"llm": "gpt", "free_tools": 1})
    assert (o.llm, o.free_tools) == ("glm", 1) and "llm=gpt" in o.req_unread["llm"]
    assert d._resolve_opts({}).llm == "glm"
    assert json.dumps(vars(d._resolve_opts(None)), default=str)          # 직렬화가 깨지지 않는다


def test_꺼진_서버의_심의_옵션_안내에는_llm_이_없다():
    assert "llm(판단 LLM" not in m._ADV_DESC


def test_심의_메뉴는_외부_응답자_창의_한도를_따로_싣는다(monkeypatch, ctx_1m):
    """외부 응답자 심의는 근거 · 사람 의견 상한을 외부 창(EXTERNAL_LLM_CTX_TOKENS)에서 유도한다 — limits(이 서버의 1M 창)만 보고 근거를
    꾸린 호출자는 외부 심의에서 뒤쪽이 빠진다. 길이 열린 서버의 deliberate_jobs 는 limits_external 을 싣고, 꺼진 서버는 종전 그대로다."""
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None, external_board=x.Board(x.Settings())))
    monkeypatch.setattr(m, "_APP", stub)
    off = asyncio.run(m.deliberate_jobs())
    assert "limits_external" not in off
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    on = asyncio.run(m.deliberate_jobs())
    ext, glm = on["limits_external"], on["limits"]
    assert ext["ctx_tokens"] == x.CTX_DEFAULT
    assert 0 < ext["evidence_total_chars"] < glm["evidence_total_chars"], (ext, glm)
    assert ext["evidence_items"] == glm["evidence_items"] and set(ext) == {"ctx_tokens", *glm} - {
        "seats", "tools", "apps", "options", "timeout_s_max", "timeout_s_default"}
    assert app._model_context_tokens() == 1_048_576 and x.ctx_override.get() is None, "외부 창이 메뉴 밖으로 샜다"
    assert m._external_limits() == ext and x.ctx_override.get() is None, "같은 문맥에서 부른 뒤에도 외부 창을 되돌려야 한다"


# ── 대상 열쇠(delib_opts.target_key — X-D8 ⑦) ───────────────────────────────────────
def test_쟁점_패널_꼴의_외부_심의는_delib_opts_의_대상_열쇠를_과제_머리에_싣는다(monkeypatch, ctx_1m, on):
    """리스크 앱 쟁점 패널의 질문은 '[쟁점 …]' 로 시작해 리스크심사 머리가 없다 — 종전엔 그 심의의 과제 머리에 target_key 가 없어 제 대상의
    과제만 답하는 워크플로가 그 과제를 알아보지 못했다. delib_opts.target_key 가 그 칸(과제 기록 · review_task_next 머리)을 채운다. 없으면
    종전 그대로 비고, 질문 머리와 둘 다 있으면 그 칸이 이긴다."""
    events, w, *_ = _panel(monkeypatch, question=Q_ISSUE)
    assert not [data for ev, data in events if ev == "error"], events[-3:]
    heads = [s["head"] for s in w.seen]
    assert heads and {h.get("target_key") for h in heads} == {TK} and {h["kind"] for h in heads} == {"deliberation"}
    events, w, *_ = _panel(monkeypatch, {"target_key": None}, question=Q_ISSUE)
    assert w.seen and {s["head"].get("target_key", "") for s in w.seen} == {""}, "그 칸 없이는 쟁점 패널 과제에 대상 열쇠가 없다"
    other = "snap:" + "cd" * 16
    events, w, *_ = _panel(monkeypatch, {"target_key": other})            # 질문 머리(Q 의 TK)와 칸이 다르면 칸이다
    assert w.seen and {s["head"].get("target_key") for s in w.seen} == {other}


_BAD_TARGET_KEYS = ["diff:", "ab" * 16, "diff:ab cd", "x:abc", "diff:ab\n", " diff:ab", "diff:" + "a" * 65, "diff:a]b", 123,
                    ["diff:ab"], {"k": "diff:ab"}, True]


@pytest.mark.parametrize("bad", _BAD_TARGET_KEYS, ids=range(len(_BAD_TARGET_KEYS)))
def test_꼴이_맞지_않는_대상_열쇠는_외부_심의를_열기_전에_bad_request_다(monkeypatch, ctx_1m, bad):
    """리스크 앱 대상 열쇠('diff:<id>' · 'snap:<id>')의 꼴이 아니면 과제를 만들기 전에 bad_request → done 이다(엔진의 다른 bad_request 와
    같은 꼴). 스위치를 끈 서버에서도 먼저 본다(모르는 llm 값과 같다). MCP 잡 시작도 잡을 만들기 전에 같은 코드로 거절한다."""
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    events, _w, _b, _t, board = _panel(monkeypatch, {"target_key": bad}, worker=False, question=Q_ISSUE)
    assert [ev for ev, _ in events] == ["error", "done"] and events[0][1]["code"] == "bad_request", events
    assert "target_key" in events[0][1]["message"] and board.counts() == {"queued": 0, "claimed": 0}
    monkeypatch.delenv("EXTERNAL_LLM_ENABLED")
    events, *_ = _panel(monkeypatch, {"target_key": bad}, worker=False, question=Q_ISSUE)
    assert [ev for ev, _ in events] == ["error", "done"] and events[0][1]["code"] == "bad_request", events


def test_MCP_심의_시작도_꼴이_맞지_않는_대상_열쇠면_잡을_만들기_전에_거절한다(monkeypatch, tmp_path, ctx_1m, on):
    with pytest.raises(ValueError, match="^bad_request: delib_opts.target_key"):
        _mcp(monkeypatch, tmp_path, advanced={"llm": "external", "consent_external": True, "target_key": "diff:a b"})
    assert delib_jobs._JOBS == {}


@pytest.mark.parametrize("tk", [TK, "꼴이 틀린 값 ]", 123], ids=["valid", "bad_shape", "not_str"])
def test_GLM_심의는_대상_열쇠를_읽지_않는다(monkeypatch, ctx_1m, on, tk):
    """GLM 심의(llm 없음)는 target_key 를 보내도 — 꼴이 틀려도 — 읽지 않는다: 같은 심의를 그 칸 없이 돌린 것과 사건 · LLM 이 받은 글이
    같다(거절도 없다)."""
    def run(extra: dict):
        seen = []

        async def fake_llm(_llm, system, human):
            seen.append((system, human))
            return delib_respond(system, human)

        monkeypatch.setattr(d, "_llm_text", fake_llm)
        events, *_ = _panel(monkeypatch, {"llm": None, "consent_external": None, "free_tools": 0, **extra}, worker=False,
                            question=Q_ISSUE)
        return events, seen

    base, base_seen = run({"target_key": None})
    got, got_seen = run({"target_key": tk})
    assert not [data for ev, data in got if ev == "error"]
    assert got == base and got_seen == base_seen
    assert d._resolve_opts({"target_key": tk}).target_key == "" and d._resolve_opts({"llm": "external", "target_key": TK}).target_key == TK
