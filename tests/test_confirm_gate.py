# 확인 관문(HWAXPortal docs/request-1030 HA-29) — 사람이 이번 말로 시키지 않은 잡 · 쓰기는 코드가 확인 카드로 멈추고, 승인은 서버가 보내는지 본다
"""DW-03(10-30 QA) — StepForge run_job 을 사람에게 묻지 않고 두 번 걸었다(판 전체 drop_check 는 1,812초를 돌다 취소). 설계는
HWAXPortal docs/request-1030/design-ha29.md 이고 맞물림은 design-integration.md(C-5 · C-10 · C-11 · C-12 · C-14)다.

  A. 관문 단위 — 목록(바닥 · 운영자 정의 · 접두 별칭) · 부류(confirm > job > write) · 주문 판정(원문만) · 거절 차례 · 정규 인자 · 손잡이.
  B. 턴 — 카드 턴(끊기 · 사건 · 본문 끝 카드 글) · 승인(단추 · 글) · 거절 · 지난 카드 · 다른 사람 · 기한 · 나란한 승인 · 자기검증 · 확정 종결 ·
     띵킹 · 작업량(StepForge estimate_cost · 시간 상한) · 재촉 · 붙인 문서, 그리고 진짜 langgraph 로 끊기.

  실행:  .venv/bin/python -m pytest tests/test_confirm_gate.py -q
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402

import app as a  # noqa: E402

PORTAL_HE_TEAM = ROOT.parent / "HWAXPortal" / "infra" / "personas" / "he-team.json"

# 작은 운영자 정의 — 접두 별칭(heaxstep_forge_cancel_job) · 선택 앱(optional[]) · 같은 이름이 둘 다에(rename_part) 있다.
HE_TEAM = {"personas": [
    {"key": "he-cad-stepforge", "apps": ["heax-step_forge"], "writes": ["run_job", "create_project", "rename_part"],
     "confirm": ["delete_project", "heaxstep_forge_cancel_job", "rename_part"]},
    {"key": "he-doc-mxwhitepaper", "apps": ["mx-white-paper"], "writes": ["insert_block"], "confirm": ["delete_block"],
     "optional": [{"app": "mx-white-paper", "writes": ["wiki_propose"]}]},
    {"key": "he-sim-smarttwin", "apps": ["smart-twin-mcp"], "writes": ["smarttwinmcp_submit_job"], "confirm": [],
     "optional": [{"app": "smart-twin-cluster", "writes": ["smarttwin_submit"], "confirm": ["slurm_job_control"]}]},
]}
USER, CONV = "u@x.com", "c1"
P1_GRAPH = {"project_id": "P1", "kind": "graph"}
P1_DROP = {"project_id": "P1", "kind": "measure", "params": {"tool": "drop_check"}}


def _events(chunks) -> list:
    return [x for x in (a._sse_parse(c) for c in chunks) if x[0]]


def _steps(evs) -> list:
    return [d.get("step", "") for e, d in evs if e == "status"]


def _result(evs) -> str:
    return [d for e, d in evs if e == "result"][-1]["content"]


def _gw(name: str, reached: list, *, props: dict | None = None, reply=None):
    """게이트웨이 도구 대역 — JSON 스키마 dict 인자 그대로, 챗 · 심의가 묶는 래퍼(_prep_tool)를 지난다. 부르면 reached 에 (이름, 인자)를 적고
    reply(이름, 인자)가 있으면 그 답(코루틴이면 기다린다)을 돌려준다."""
    async def answer(n, args):
        got = reply(n, args) if reply else f"gateway called {n}"
        return await got if asyncio.iscoroutine(got) else got
    if name == "invoke_tool":
        schema = {"type": "object", "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["name"]}

        async def inner(name: str = "", arguments=None):
            reached.append((name, arguments))
            return await answer(name, arguments)
    else:
        schema = {"type": "object", "properties": dict(props or {})}

        async def inner(**kw):
            reached.append((name, kw))
            return await answer(name, kw)
    return a._prep_tool(StructuredTool(name=name, description="게이트웨이 도구 대역", args_schema=schema, coroutine=inner))


@pytest.fixture
def maps(monkeypatch):
    """도구 → 앱 맵 · 앱 라벨 캐시를 고정한다 — 비어 있으면 _tools_map · _gw_apps 가 게이트웨이에 물어 시험이 박스에 따라 달라진다."""
    def put(m=None, apps=None):
        monkeypatch.setattr(a, "_TOOLS_MAP_CACHE", {"at": time.time(), "map": dict(m or {"_none": "_none"}), "areas": {}, "area_meta": {}})
        monkeypatch.setattr(a, "_APPS_CACHE", {"apps": dict(apps or {"_none": {"label": "없음"}}), "at": time.time()})
    put()
    return put


@pytest.fixture
def gate(monkeypatch, tmp_path, maps):
    """관문을 켜고(루트 conftest 가 끈다) 작은 운영자 정의를 읽게 한다. 셈은 시험마다 새로."""
    monkeypatch.setattr(a, "CHAT_CONFIRM_GATE", True)
    p = tmp_path / "he-team.json"
    p.write_text(json.dumps(HE_TEAM), encoding="utf-8")
    monkeypatch.setattr(a, "CONFIRM_LISTS_PATH", p)
    monkeypatch.setattr(a, "_CONFIRM_STAT", a.Counter())
    return p


@pytest.fixture
def turn_ctx(gate):
    """관문 단위 시험의 턴 맥락 — 이번 말로 _confirm_ctx 를 짓고 _turn_confirm(필요하면 _turn_ids 도)에 건다. 끝나면 되돌린다."""
    toks = []

    def make(message, ids=None, **kw):
        req = a.ChatRequest(message=message, groups=[], user_email=USER, conversation_id=CONV, **kw)
        ctx = a._confirm_ctx(req)
        toks.append((a._turn_confirm, a._turn_confirm.set(ctx)))
        if ids is not None:
            toks.append((a._turn_ids, a._turn_ids.set(set(ids))))
        return ctx
    yield make
    for var, tok in reversed(toks):
        var.reset(tok)


def _run(tool, args):
    return asyncio.run(tool.ainvoke(args))


def _held(out) -> bool:
    return isinstance(out, str) and out.startswith(a._TOOL_FAIL_MARK) and a._CONFIRM_MARK in out


# ── A. 관문 단위 ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_A1_맥락이_없으면_챗_밖이라_그대로_간다(gate):
    reached = []
    assert a._turn_confirm.get() is None
    assert _run(_gw("run_job", reached), P1_GRAPH) == "gateway called run_job"
    assert reached == [("run_job", P1_GRAPH)], "심의 좌석 · 띵킹 · 루틴 · 카탈로그는 이 맥락이 없다"


def test_A2_시키지_않은_잡은_보내지_않고_확인_대기_글을_돌려준다(turn_ctx):
    ctx = turn_ctx("이 판의 낙하에 취약한 부품을 알려줘")
    reached = []
    out = _run(_gw("run_job", reached), P1_GRAPH)
    assert _held(out) and "이번 말로 시킨 작업이 아니라" in out and "다시 부르지 말고" in out, out
    assert a._result_ok(out) is False, "활동 · 절차 원장이 성공으로 접지 않는다"
    assert reached == []
    assert len(ctx.pending) == 1 and ctx.pending[0]["base"] == "run_job" and ctx.pending[0]["kind"] == "job"
    assert ctx.pending[0]["args"] == P1_GRAPH and ctx.pending[0]["via"] is False and ctx.pending[0]["again"] is False


@pytest.mark.parametrize("form", ["invoke_json", "invoke_dict", "alias", "invoke_alias"])
def test_A3_경유_invoke_tool_과_접두_별칭도_같다(turn_ctx, form):
    ctx = turn_ctx("이 판의 낙하에 취약한 부품을 알려줘")
    reached = []
    if form == "alias":
        out = _run(_gw("heaxstep_forge_run_job", reached), P1_GRAPH)
    else:
        inner = "heaxstep_forge_run_job" if form == "invoke_alias" else "run_job"
        args = json.dumps(P1_GRAPH) if form == "invoke_json" else P1_GRAPH
        out = _run(_gw("invoke_tool", reached), {"name": inner, "arguments": args})
    assert _held(out) and reached == []
    assert ctx.pending[0]["base"] == "run_job" and ctx.pending[0]["canon"] == a._confirm_canon(P1_GRAPH)
    assert ctx.pending[0]["via"] is form.startswith("invoke")


# 설계 4.1 의 판정 표(시험 벡터) — 통과면 게이트웨이에 닿고 카드면 닿지 않는다.
VECTORS = [
    ("drop_check 돌려줘", "run_job", P1_DROP, True),
    ("drop_check 돌려줘", "run_job", P1_GRAPH, False),                       # graph 는 시키지 않았다(DW-03 의 첫 잡)
    ("판 전체 낙하 점검 돌려줘", "run_job", P1_DROP, False),                  # 그 일을 글자로 부르지 않았다
    ("P1 돌려줘", "run_job", P1_GRAPH, False),                               # 식별자는 대상이 아니다
    ("drop_check 결과 보여줘", "run_job", P1_DROP, False),                   # 주문 동사 없음
    ("drop_check 실행 결과는?", "run_job", P1_DROP, False),                  # 질문
    ("drop_check 돌려도 돼?", "run_job", P1_DROP, False),
    ("drop_check 는 돌리지 마", "run_job", P1_DROP, False),                  # 부정
    ("run drop_check now", "run_job", P1_DROP, True),                        # 영어 주문
    ("don't run drop_check", "run_job", P1_DROP, False),
    ("x.pptx 변환해 주세요", "docforge_convert", {"path": "/stage/x.pptx"}, True),   # 파일 이름
    ("y.pptx 변환해 주세요", "docforge_convert", {"path": "/stage/x.pptx"}, False),
    ("이 표를 문서에 블록으로 추가해줘", "insert_block", {"doc_id": "D1", "text": "표"}, True),   # write 는 주문 동사면 된다
    ("이 문서 요약해줘", "insert_block", {"doc_id": "D1", "text": "요약"}, False),
    ("블록 추가해도 될까", "insert_block", {"doc_id": "D1"}, False),
    ("이 표를 블록으로 추가해 줄래", "insert_block", {"doc_id": "D1"}, True),
    ("이 표를 블록으로 추가해 줄래?", "insert_block", {"doc_id": "D1"}, False),   # 주문 동사가 있어도 질문이면 카드
    ("drop_check 돌려줄래?", "run_job", P1_DROP, False),
    ("delete_project 로 P1 지워줘", "delete_project", {"project_id": "P1"}, False),     # confirm 은 늘 카드
    ("이 판 질량 알려줘", "list_projects", {}, True),                         # 목록 밖 — 관문과 무관
    ("이 판 질량 알려줘", "mass_estimate", {"project_id": "P1"}, True),
]


@pytest.mark.parametrize("msg,tool,args,passes", VECTORS, ids=[f"{i}-{v[1]}" for i, v in enumerate(VECTORS)])
def test_A4_주문_판정_표(turn_ctx, msg, tool, args, passes):
    ctx = turn_ctx(msg)
    reached = []
    out = _run(_gw(tool, reached), args)
    assert bool(reached) is passes, (msg, tool, out)
    assert _held(out) is (not passes) and bool(ctx.pending) is (not passes)


def test_A5_주문으로_통과한_호출을_같은_턴에_또_부르면_카드고_again_이다(turn_ctx):
    ctx = turn_ctx("drop_check 돌려줘")
    reached = []
    t = _gw("run_job", reached)
    assert _run(t, P1_DROP) == "gateway called run_job"
    out = _run(t, P1_DROP)
    assert _held(out) and "이미 보냈다" in out
    assert len(reached) == 1 and ctx.pending[0]["again"] is True
    assert a._CONFIRM_STAT["ordered"] == 1


def test_A6_콕_집은_도구는_이름을_부른_셈이다(turn_ctx):
    reached = []
    turn_ctx("돌려줘", pinned_tools=["run_job"])
    assert _run(_gw("heaxstep_forge_run_job", reached), P1_GRAPH) == "gateway called heaxstep_forge_run_job"
    turn_ctx("돌려줘", pinned_tools=["heaxstep_forge_run_job"])
    assert _run(_gw("run_job", reached), P1_GRAPH) == "gateway called run_job", "핀이 별칭이고 부른 것이 맨 이름이어도 같다"
    ctx = turn_ctx("돌려줘")
    assert _held(_run(_gw("run_job", reached), P1_GRAPH)) and len(ctx.pending) == 1, "핀이 없으면 카드"
    ctx = turn_ctx("돌려줘", pinned_apps=["heax-step_forge"])
    assert _held(_run(_gw("run_job", reached), P1_GRAPH)), "앱 핀은 치지 않는다 — 앱 하나에 잡이 여럿이다"


def test_A7_거절_관문이_먼저다(turn_ctx):
    ctx = turn_ctx("전부 실행해줘", ids=[])
    reached = []
    inv = _gw("invoke_tool", reached)
    out = _run(inv, {"name": "open_resin_panel", "arguments": {}})
    assert out.startswith(a._TOOL_FAIL_MARK) and "패널" in out and a._CONFIRM_MARK not in out
    out = _run(_gw("deliberate_start", reached), {"question": "q", "advanced": {"llm": "external"}})
    assert "consent_external" in out and a._CONFIRM_MARK not in out
    out = _run(_gw("heaxstep_forge_cancel_job", reached), {"job_id": 987654})
    assert a._PHANTOM_ID_MARK in out and a._CONFIRM_MARK not in out, "지어낸 ID 에 카드를 띄우면 사람이 틀린 대상을 승인한다"
    assert ctx.pending == [] and reached == []


def test_A8_읽기는_어떤_말에서도_간다(turn_ctx):
    turn_ctx("전부 삭제해줘")
    reached = []
    for name, args in (("list_projects", {}), ("heaxstep_forge_job_status", {"job_id": "J-1"}),
                       ("estimate_cost", {"project_id": "P1", "job": "graph"})):
        assert _run(_gw(name, reached), args) == f"gateway called {name}"
    assert len(reached) == 3


def test_A9_목록_파일이_없으면_바닥_목록만_돈다(turn_ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(a, "CONFIRM_LISTS_PATH", tmp_path / "없는" / "he-team.json")
    ctx = turn_ctx("문서를 정리해줘")
    reached = []
    for name, args in (("run_job", P1_GRAPH), ("docforge_convert", {"path": "/s/a.pptx"}), ("ste_submit_job", {"deck": "a.k"})):
        assert _held(_run(_gw(name, reached), args)), name
    assert _run(_gw("insert_block", reached), {"doc_id": "D1"}) == "gateway called insert_block", "운영자 정의가 없으면 쓰기 목록도 없다"
    assert [c["kind"] for c in ctx.pending] == ["job", "job", "job"]
    lists = a._confirm_health()["lists"]
    assert lists["loaded"] is False and lists["error"] == "없음" and lists["jobs"] == len(a._CONFIRM_JOB_TOOLS) == 24
    assert (lists["writes"], lists["confirm"]) == (0, 0)


def test_A9b_꼴이_틀린_정본은_통째로_버리고_까닭을_적는다(gate):
    gate.write_text(json.dumps({"personas": [{"key": "x", "apps": ["a"], "writes": [1]}]}), encoding="utf-8")
    lists = a._confirm_health()["lists"]
    assert lists["loaded"] is False and lists["error"].startswith("못 읽음(") and lists["writes"] == 0
    gate.write_text(json.dumps(HE_TEAM), encoding="utf-8")
    os.utime(gate, ns=(time.time_ns(), time.time_ns() + 1_000_000_000))
    assert a._confirm_health()["lists"]["loaded"] is True, "바뀐 시각이 다르면 재기동 없이 다시 읽는다"


def test_A10_접두_별칭만_적어도_맨_이름까지_막고_겹치면_confirm_이다(turn_ctx):
    assert a._confirm_class("cancel_job") == ("confirm", "cancel_job")
    assert a._confirm_class("heaxstep_forge_cancel_job") == ("confirm", "heaxstep_forge_cancel_job")
    assert a._confirm_class("rename_part")[0] == "confirm", "writes 와 confirm 에 함께 걸린 이름은 더 엄한 쪽"
    assert a._confirm_class("smarttwinmcp_submit_job") == ("job", "submit_job"), "바닥 목록(job)이 운영자 writes 보다 엄하다"
    assert a._confirm_class("wiki_propose") == ("write", "wiki_propose"), "선택 앱(optional[])의 목록도 읽는다"
    assert a._confirm_class("slurm_job_control") == ("confirm", "slurm_job_control")
    assert a._confirm_class("smarttwincluster_slurm_job_control") == ("confirm", "slurm_job_control"), "게이트웨이 충돌 접두를 단 꼴"
    assert a._confirm_class("job_control") == (None, ""), "앱 접두가 아닌 slurm_ 은 떼지 않는다(맨 이름 job_control 은 다른 도구다)"
    ctx = turn_ctx("cancel_job 실행해줘")
    reached = []
    for name in ("cancel_job", "heaxstep_forge_cancel_job"):
        assert _held(_run(_gw(name, reached), {"job_id": "J-1"})), name
    assert [c["kind"] for c in ctx.pending] == ["confirm", "confirm"] and reached == []
    assert "되돌리기 어려운 작업이라" in a._confirm_refusal("cancel_job", "confirm", False)


def test_A11_손잡이를_끄면_막지_않고_넷째_규칙이_종전_글이다(turn_ctx, monkeypatch, tmp_path):
    turn_ctx("이 판의 낙하에 취약한 부품을 알려줘")
    monkeypatch.setattr(a, "CHAT_CONFIRM_GATE", False)
    reached = []
    assert _run(_gw("run_job", reached), P1_GRAPH) == "gateway called run_job"
    assert a._confirm_reply(a.ChatRequest(message="실행", history=[{"role": "assistant", "content": "확인 번호 `cf-0123456789ab`"}])) is None
    # 넷째 규칙은 기동 때 한 번 읽는다 — 새 인터프리터로 다시 읽는다(실 경로를 건드리지 않게 ARTIFACT_DIR 은 임시로)
    got = {}
    for v in ("0", "1", ""):
        env = {**os.environ, "CHAT_CONFIRM_GATE": v, "ARTIFACT_DIR": str(tmp_path / "art"), "PYTHONDONTWRITEBYTECODE": "1"}
        code = ("import app; print(app.CHAT_CONFIRM_GATE, app._SP_CONFIRM_OLD in app._SP_HEAD, app._SP_CONFIRM_GATE in app._SP_HEAD, "
                "app._SP_CONFIRM_GATE in app.SYSTEM_PROMPT)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-1500:]
        got[v] = r.stdout.strip().splitlines()[-1]
    assert got["0"] == "False True False False", got
    assert got["1"] == got[""] == "True False True True", ("기본은 켬", got)


def test_A12_정규_인자는_빈_칸과_키_차례를_무시한다():
    assert a._confirm_canon({"b": 1, "a": {"y": None, "x": 2}, "c": ""}) == a._confirm_canon('{"a":{"x":2},"b":1}') == '{"a":{"x":2},"b":1}'
    assert a._confirm_canon(None) == a._confirm_canon({}) == a._confirm_canon("") == "{}"
    assert a._confirm_canon({"a": {"y": [], "z": {}}}) == "{}", "비운 dict 도 빈 칸이다"
    assert a._confirm_canon("{깨진 글") == "{깨진 글", "못 읽는 글은 그대로(같은 글일 때만 같다)"
    assert a._confirm_canon({"a": [1, None, {"k": ""}]}) == '{"a":[1,null,{}]}', "목록의 자리는 남긴다"


def test_A13_카드의_답_판정(gate):
    card = {"role": "assistant", "content": "…(확인 번호 `cf-0123456789ab` · 30분)"}

    def reply(msg, hist=(card,)):
        return a._confirm_reply(a.ChatRequest(message=msg, history=list(hist)))
    assert reply("실행 — 확인 번호 cf-0123456789ab") == ("approve", "cf-0123456789ab")
    assert reply("취소 — 확인 번호 cf-0123456789ab 는 실행하지 않음") == ("decline", "cf-0123456789ab")
    for m in ("응", "네 실행해 주세요", "실행", "ok", "ㅇㅇ", "진행해", "Yes!"):
        assert reply(m) == ("approve", "cf-0123456789ab"), m
    for m in ("아니", "취소", "no", "하지 마"):
        assert reply(m) == ("decline", "cf-0123456789ab"), m
    for m in ("응 근데 kind 는 mesh 로", "계속", "drop_check 돌려줘", "", "실행 cf-ffffffffffff"):
        assert reply(m) is None, m
    assert reply("응", hist=(card, {"role": "user", "content": "다른 질문"})) is None, "확인은 바로 다음 말로만"
    assert reply("응", hist=({"role": "assistant", "content": "카드 없는 답"},)) is None


# ── B. 턴(대역 에이전트 · _agent_stream · SSE) ──────────────────────────────────────────────────────────────────────

class _GateAgent:
    """astream_events 안에서 감싼 도구를 실제로 부르는 대역 — 모델 호출 시작 → 단계마다 (도구 시작 · 부르기 · 끝) → 다음 모델 호출 시작 → 끝 글.
    끊긴 뒤 더 진행되면 after_cut 이 참이다. ainvoke(구제 · 강제 재시도)는 센다."""

    def __init__(self, tools, steps=(), say="잡을 걸었습니다 J-1", pre="", on_start=None, boom_on_close=False, boom_after_tool=False):
        self.tools, self.steps, self.say, self.pre = tools, list(steps), say, pre
        self.on_start, self.boom_on_close, self.boom_after_tool = on_start, boom_on_close, boom_after_tool
        self.inputs, self.ainvoked, self.after_cut = [], 0, False

    @staticmethod
    def _start(i):
        return {"event": "on_chat_model_start", "name": "fake", "metadata": {"ls_model_name": "fake-llm"}, "run_id": f"m{i}"}

    async def astream_events(self, inputs, version=None, config=None):
        self.inputs.append(inputs)
        if self.on_start:
            self.on_start()
        yield self._start(0)
        if self.pre:
            yield {"event": "on_chat_model_stream", "data": {"chunk": types.SimpleNamespace(content=self.pre)}, "run_id": "m0"}
        for i, step in enumerate(self.steps):
            ids = [(f"t{i}-{j}", name, args) for j, (name, args) in enumerate(step)]
            for rid, name, args in ids:
                yield {"event": "on_tool_start", "name": name, "data": {"input": args}, "run_id": rid}
            for rid, name, args in ids:
                out = await self.tools[name].ainvoke(args)
                yield {"event": "on_tool_end", "name": name, "data": {"input": args, "output": out}, "run_id": rid}
            if self.boom_after_tool:
                raise RuntimeError("흐름이 끊기며 난 오류(재귀 한도 같은)")
            try:
                yield self._start(i + 1)
            except GeneratorExit:
                if self.boom_on_close:
                    raise RuntimeError("닫다가 난 오류")
                raise
        self.after_cut = True
        yield {"event": "on_chat_model_stream", "data": {"chunk": types.SimpleNamespace(content=self.say)}, "run_id": "mz"}

    async def ainvoke(self, payload, config=None):
        self.ainvoked += 1
        return {"messages": [types.SimpleNamespace(type="ai", content=self.again_say)]}

    again_say = "run_job 을 호출하겠습니다"      # 구제 · 재시도 호출의 답


def _turn(monkeypatch, agent, tools, message, history=(), *, user=USER, conv=CONV, via_chat=False, tbn_boom=None, **kw):
    """한 챗 턴 — 에이전트는 대역, 도구 목록(승인 실행 · 작업량 묻기 · 확정 종결이 받는 것)은 tools 사전. → (사건들, 목록 받기 기록)."""
    async def built(*_a, **_k):
        return agent
    monkeypatch.setattr(a, "_agent_for", built)
    seen: list = []

    async def tbn(app_, groups, result_max=None, desc_max=None, user="", user_pat="", svc_fallback=True):
        seen.append({"user": user, "svc_fallback": svc_fallback})
        if tbn_boom is not None:
            raise tbn_boom
        return tools
    monkeypatch.setattr(a, "_tools_by_name", tbn)
    req = a.ChatRequest(message=message, groups=["portal-admin"], history=list(history), user_email=user, conversation_id=conv, **kw)

    async def go():
        if via_chat:
            resp = await a.chat(req)
            return [c async for c in resp.body_iterator]
        return [c async for c in a._agent_stream(a.app, req)]
    return _events(asyncio.run(go())), seen


def _card_turn(monkeypatch, tools, step, message="이 판의 낙하에 취약한 부품을 알려줘", **kw):
    """카드 턴 하나 → (사건들, 카드 사건, 이력 두 항목)."""
    ag = _GateAgent(tools, steps=[step])
    evs, _ = _turn(monkeypatch, ag, tools, message, **kw)
    cards = [d for e, d in evs if e == "confirm"]
    assert len(cards) == 1, _steps(evs)
    return evs, cards[0], [{"role": "user", "content": message}, {"role": "assistant", "content": _result(evs)}]


def test_B13_카드_턴은_보내지_않고_끊고_카드를_낸다(gate, monkeypatch):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    # 예고 글('호출하겠습니다')이라 구제 길(강제 재시도 · 확정 종결)이 설 자리다 — 막은 호출이 있으면 건너뛰어야 한다
    ag = _GateAgent(tools, steps=[[("run_job", P1_GRAPH)]], pre="낙하 그래프 잡 run_job 을 호출하겠습니다. ")
    evs, seen = _turn(monkeypatch, ag, tools, "이 판의 낙하에 취약한 부품을 알려줘")
    assert seen == [] and "확인 카드 — 예상 작업량을 묻는 중" not in _steps(evs), "StepForge 가 아닌 잡은 묻지 않는다(목록도 받지 않는다)"
    assert [d for e, d in evs if e == "confirm"][0]["calls"][0]["workload"] == "이 도구는 작업량을 미리 알려 주지 않는다"
    held = [d for e, d in evs if e == "status" and d.get("step", "").startswith("확인 대기: run_job")]
    assert len(held) == 1 and held[0]["ok"] is False and held[0]["call"] == "t0-0" and a._CONFIRM_MARK in held[0]["result_preview"]
    assert not any(s.startswith("도구 완료: run_job") for s in _steps(evs)), "'도는 중' 으로 남지 않게 완료 대신 확인 대기"
    card = [d for e, d in evs if e == "confirm"][0]
    assert re.fullmatch(r"cf-[0-9a-f]{12}", card["id"]) and card["calls"][0]["tool"] == "run_job" and card["calls"][0]["kind"] == "job"
    assert card["calls"][0]["args_text"] == "kind=graph · project_id=P1" and card["calls"][0]["again"] is False
    assert card["approve_text"] == f"실행 — 확인 번호 {card['id']}" and card["decline_text"] == f"취소 — 확인 번호 {card['id']} 는 실행하지 않음"
    assert 0 < card["expires_at"] / 1000 - time.time() <= a._CONFIRM_TTL_S
    res = _result(evs)
    assert res.startswith("낙하 그래프 잡 run_job 을 호출하겠습니다.") and res.count(card["id"]) == 1 and "**확인이 필요합니다**" in res
    assert res.endswith(f"(확인 번호 `{card['id']}` · 30분)"), "끝이 확인 번호 — 끊긴 턴 표지에 걸리지 않는다"
    assert not a._INTERRUPTED_RE.search(res)
    assert "잡을 걸었습니다" not in res and ag.after_cut is False, "끊은 뒤의 모델 글은 없다"
    assert reached == [] and ag.ainvoked == 0, "구제 길(강제 재시도 · 확정 종결 · 빈 답 구제)이 돌지 않는다"
    assert a._EVIDENCE_MARK not in res, "막은 호출은 조회한 것이 아니다"
    kinds = [e for e, _ in evs]
    assert kinds.index("confirm") < max(i for i, (e, d) in enumerate(evs) if e == "token" and card["id"] in d.get("delta", "")) < kinds.index("result")
    assert kinds[-2:] == ["result", "done"]
    stored = a._CONFIRM_PENDING[card["id"]]
    assert (stored["user"], stored["conv"]) == (USER, CONV) and stored["calls"][0]["args"] == P1_GRAPH
    assert a._confirm_health()["pending"] == 1 and a._CONFIRM_STAT["cards"] == 1 and a._CONFIRM_STAT["held_calls"] == 1


def test_B14_단추로_승인하면_첫_모델_호출_전에_서버가_그대로_한_번_보낸다(gate, monkeypatch):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    at_start: list = []
    # 끝 글에 예고('확인하겠습니다')가 있어도 보낸 수를 호출로 세므로 '도구 0회' 구제가 서지 않는다
    ag = _GateAgent(tools, steps=[], say="잡 J-1 을 걸었습니다. 진행은 상태 조회로 확인하겠습니다.",
                    on_start=lambda: at_start.append(list(reached)))
    evs, seen = _turn(monkeypatch, ag, tools, card["approve_text"], hist)
    assert reached == [("run_job", P1_GRAPH)] and at_start == [[("run_job", P1_GRAPH)]], "첫 모델 호출 전에 보낸다"
    steps = [(e, d) for e, d in evs if e == "status"]
    call = [d for e, d in steps if d["step"] == "도구 호출: run_job"][0]
    done = [d for e, d in steps if d["step"] == "도구 완료: run_job"][0]
    assert call["call"] == done["call"] == f"{card['id']}-1" and done["ok"] is True and "gateway called" in done["result_preview"]
    assert any(d["step"].startswith(f"확인 카드 {card['id']} 승인") for e, d in steps)
    human = ag.inputs[0]["messages"][-1][1]
    head = human.split(a._TURN_NOTES_TAIL)[0]
    assert f"[확인 카드 {card['id']} 승인 — 서버가 사람의 승인으로 이 호출을 보냈다]" in head and "gateway called run_job" in head
    assert ag.ainvoked == 0, "모델이 결과만 알리고 끝나도 강제 재시도 · 구제를 하지 않는다(보낸 수를 호출로 센다)"
    assert {"user": USER, "svc_fallback": False} in seen, "승인 실행은 사용자 자격만 — 서비스 계정으로 넘기지 않는다(C-11)"
    res = _result(evs)
    assert res.startswith("잡 J-1 을 걸었습니다.") and a._EVIDENCE_MARK in res and "`run_job`" in res, "보낸 호출은 근거 블록에 남는다"
    assert card["id"] not in a._CONFIRM_PENDING and a._CONFIRM_STAT["approved"] == 1 and a._CONFIRM_STAT["executed"] == 1


def test_B14b_승인된_턴에_모델이_같은_호출을_또_부르면_again_카드다(gate, monkeypatch):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    ag = _GateAgent(tools, steps=[[("run_job", P1_GRAPH)]])
    evs, _ = _turn(monkeypatch, ag, tools, card["approve_text"], hist)
    assert reached == [("run_job", P1_GRAPH)], "서버가 보낸 한 번뿐"
    again = [d for e, d in evs if e == "confirm"]
    assert len(again) == 1 and again[0]["calls"][0]["again"] is True and again[0]["id"] != card["id"]
    assert "이미 보냈다" in _result(evs)


def test_B14c_invoke_tool_로_막은_호출은_승인도_invoke_tool_로_처음_꼴_그대로(gate, monkeypatch):
    reached = []
    tools = {"invoke_tool": _gw("invoke_tool", reached)}
    raw = json.dumps(P1_GRAPH)
    _evs, card, hist = _card_turn(monkeypatch, tools, [("invoke_tool", {"name": "run_job", "arguments": raw})])
    assert card["calls"][0]["tool"] == "run_job"
    evs, _ = _turn(monkeypatch, _GateAgent(tools, steps=[], say="걸었습니다."), tools, card["approve_text"], hist)
    assert reached == [("run_job", raw)], "안쪽 인자 글 그대로"
    assert [d.get("via") for e, d in evs if e == "status" and d.get("step") == "도구 호출: run_job"] == ["invoke_tool"]


@pytest.mark.parametrize("answer,sent", [("응", True), ("네 실행해 주세요", True), ("실행", True), ("ok", True),
                                         ("응 근데 kind 는 mesh 로", False)])
def test_B15_글로_승인(gate, monkeypatch, answer, sent):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    _turn(monkeypatch, _GateAgent(tools, steps=[], say="알겠습니다."), tools, answer, hist)
    assert reached == ([("run_job", P1_GRAPH)] if sent else []), answer
    assert card["id"] not in a._CONFIRM_PENDING, "승인이면 꺼냈고, 답이 아니면 지웠다(다른 말을 하면 보내지 않는다)"


def test_B16_지난_카드에는_답할_수_없다(gate, monkeypatch):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    hist += [{"role": "user", "content": "다른 질문"}, {"role": "assistant", "content": "다른 답"}]
    for msg in ("응", card["approve_text"]):
        evs, _ = _turn(monkeypatch, _GateAgent(tools, steps=[], say="네."), tools, msg, hist)
        assert not any("확인 카드" in s or "확인 번호" in s for s in _steps(evs)), msg
    assert reached == []


@pytest.mark.parametrize("answer", ["decline", "아니", "취소"])
def test_B17_거절하면_보내지_않고_저장분이_사라지며_같은_호출은_다시_카드다(gate, monkeypatch, answer):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    msg = card["decline_text"] if answer == "decline" else answer
    ag = _GateAgent(tools, steps=[[("run_job", P1_GRAPH)]])
    evs, _ = _turn(monkeypatch, ag, tools, msg, hist)
    assert reached == [] and card["id"] not in a._CONFIRM_PENDING
    assert any(s.startswith(f"확인 카드 {card['id']} 거절") for s in _steps(evs))
    assert f"[확인 카드 {card['id']} 거절" in ag.inputs[0]["messages"][-1][1]
    assert len([d for e, d in evs if e == "confirm"]) == 1, "거절한 턴에 같은 호출을 부르면 다시 카드다(확인 답은 주문이 아니다)"
    assert a._CONFIRM_STAT["declined"] == 1


@pytest.mark.parametrize("who", ["user", "conv"])
def test_B18_다른_사람_다른_대화는_승인할_수_없다(gate, monkeypatch, who):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    other = {"user": "v@x.com"} if who == "user" else {"conv": "c2"}
    evs, _ = _turn(monkeypatch, _GateAgent(tools, steps=[], say="네."), tools, card["approve_text"], hist, **other)
    assert reached == [] and any("확인 번호를 찾지 못했다" in s for s in _steps(evs))
    assert card["id"] in a._CONFIRM_PENDING, "주인의 카드는 남는다"
    _turn(monkeypatch, _GateAgent(tools, steps=[], say="네."), tools, card["approve_text"], hist)
    assert reached == [("run_job", P1_GRAPH)], "주인은 그 뒤에도 승인할 수 있다"


@pytest.mark.parametrize("how", ["expired", "restart"])
def test_B19_기한이_지났거나_서버가_다시_떴으면_찾지_못했다고_하고_보내지_않는다(gate, monkeypatch, how):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    if how == "expired":
        a._CONFIRM_PENDING[card["id"]]["exp"] = time.time() - 1
    else:
        a._CONFIRM_PENDING.clear()
    evs, _ = _turn(monkeypatch, _GateAgent(tools, steps=[], say="네."), tools, card["approve_text"], hist)
    assert reached == [] and any(s.startswith("확인 번호를 찾지 못했다(30분이 지났거나 서버가 다시 떴다)") for s in _steps(evs))
    assert a._CONFIRM_STAT["unknown_reply"] == 1 and card["id"] not in a._CONFIRM_PENDING


def test_B20_같은_카드를_나란히_승인해도_한쪽만_보낸다(gate, monkeypatch):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])

    async def built(*_a, **_k):
        return _GateAgent(tools, steps=[], say="네.")
    monkeypatch.setattr(a, "_agent_for", built)

    async def tbn(*_a, **_k):
        await asyncio.sleep(0.05)        # 두 턴이 겹치게
        return tools
    monkeypatch.setattr(a, "_tools_by_name", tbn)
    req = a.ChatRequest(message=card["approve_text"], groups=["portal-admin"], history=hist, user_email=USER, conversation_id=CONV)

    async def one():
        return [c async for c in a._agent_stream(a.app, req)]

    async def both():
        return await asyncio.gather(one(), one())
    outs = asyncio.run(both())
    assert reached == [("run_job", P1_GRAPH)]
    assert sum(any("찾지 못했다" in s for s in _steps(_events(o))) for o in outs) == 1


class _Reviewer:
    def __init__(self):
        self.calls = []

    async def astream(self, msgs, **kw):
        self.calls.append(msgs)
        yield types.SimpleNamespace(content="[판정] 고칠 것 없음", additional_kwargs={}, response_metadata={"finish_reason": "stop"})


def _sf_tools(reached, est=None, delay=0.0):
    """StepForge run_job · list_projects · invoke_tool(estimate_cost 답) 대역."""
    async def reply(n, args):
        if n == "heaxstep_forge_estimate_cost":
            if delay:
                await asyncio.sleep(delay)
            return json.dumps(est or {})
        return f"gateway called {n}"
    return {"run_job": _gw("run_job", reached), "list_projects": _gw("list_projects", reached, reply=lambda n, x: "P1 판 · 파트 12개"),
            "invoke_tool": _gw("invoke_tool", reached, reply=reply)}


EST = {"expected_s": 1812, "low_s": 1500, "high_s": 2400, "cost_basis": "history", "gateway_verdict": "async_job",
       "basis_note": "이 과제에서 끝난 3번의 중앙값이다", "advice": None}


@pytest.mark.parametrize("self_check", [False, True])
def test_B21_자기검증_켬이면_카드_턴은_검토하지_않고_근거_블록이_카드의_작업량을_짚지_않는다(gate, monkeypatch, maps, self_check):
    maps({"run_job": "heax-step_forge"}, {"heax-step_forge": {"label": "StepForge"}})
    monkeypatch.setenv("CHAT_SELF_CHECK", "1" if self_check else "0")
    rv = _Reviewer()
    monkeypatch.setattr(a.app.state, "chat_llm", rv, raising=False)
    reached = []
    tools = _sf_tools(reached, EST)
    ag = _GateAgent(tools, steps=[[("list_projects", {}), ("run_job", P1_GRAPH)]], pre="판을 보고 잡을 걸겠습니다. ")
    evs, seen = _turn(monkeypatch, ag, tools, "이 판의 낙하에 취약한 부품을 알려줘")
    res = _result(evs)
    card = [d for e, d in evs if e == "confirm"][0]
    assert res.count(card["id"]) == 1 and "StepForge `run_job`" in res and "약 1,812초(1,500 ~ 2,400초 · 근거 history)" in res
    assert a._EVIDENCE_MARK in res and "`list_projects`" in res and "`run_job`" not in res.split(a._EVIDENCE_MARK)[1], \
        "나란한 읽기는 근거에 남고 막은 잡은 아니다(설계 9절 23)"
    assert "1,812" not in res.split(a._EVIDENCE_MARK)[1], "카드의 예상 작업량은 '출처 없는 수' 가 아니다(C-14)"
    assert ("list_projects", {}) in reached and not any(n == "run_job" for n, _ in reached)
    assert ag.ainvoked == 0, "조회 뒤 끊긴 턴이라도 빈 답 구제가 서지 않는다(막은 호출이 있다)"
    assert ("heaxstep_forge_estimate_cost", {"project_id": "P1", "job": "graph", "args": {}}) in reached
    assert {"user": USER, "svc_fallback": True} in seen, "작업량 묻기(읽기)는 기본 자격"
    if self_check:
        meta = [d for e, d in evs if e == "meta"][-1]
        assert rv.calls == [] and meta["self_check"]["status"] == "skipped" and "확인 카드" in meta["self_check"]["reason"]
        assert card["id"] not in "".join(d.get("delta", "") for e, d in evs if e == "reasoning"), "카드 글은 '생각' 칸에 흘리지 않는다"
    assert "".join(d.get("delta", "") for e, d in evs if e == "token") == res, "본문에 카드 글이 한 번"


def test_B22_끊긴_흐름이_닫히며_오류를_내도_오류_재시도_없이_카드로_끝난다(gate, monkeypatch):
    """닫다가 난 오류(_confirm_close) · 막은 뒤 흐름의 오류(_confirm_events)는 삼켜 정상 끝맺음을 탄다 — 나란한 읽기의 근거 블록과 meta 가 남는다
    (턴 except 의 카드 끝맺음은 그 밖에서 난 오류의 그물이라 근거 블록 · meta 가 없다)."""
    for kw in ({"boom_on_close": True}, {"boom_after_tool": True}):
        reached = []
        tools = {"run_job": _gw("run_job", reached), "list_projects": _gw("list_projects", reached)}
        ag = _GateAgent(tools, steps=[[("list_projects", {}), ("run_job", P1_GRAPH)]], **kw)
        evs, _ = _turn(monkeypatch, ag, tools, "이 판의 낙하에 취약한 부품을 알려줘")
        assert [e for e, _ in evs if e == "error"] == [] and not any("자동 재시도" in s for s in _steps(evs)), kw
        assert len([d for e, d in evs if e == "confirm"]) == 1 and ag.ainvoked == 0 and reached == [("list_projects", {})], kw
        res = _result(evs)
        assert "내부 오류" not in res and [e for e, _ in evs][-1] == "done"
        assert a._EVIDENCE_MARK in res and "`list_projects`" in res and "meta" in [e for e, _ in evs], kw


def test_B22b_흐름_밖의_오류도_카드가_섰으면_재시도_대신_카드로_끝낸다(gate, monkeypatch):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    ag = _GateAgent(tools, steps=[[("run_job", P1_GRAPH)]])
    real, seen = a._inner_tool, []

    def boom(name, inp):        # 막은 호출의 끝 사건을 처리하다 난 오류(흐름이 아니라 턴 쪽) — 첫 번째는 시작 사건이다
        if name == "run_job" and isinstance(inp, dict) and inp.get("kind") == "graph":
            seen.append(1)
            if len(seen) == 2:
                raise RuntimeError("턴 쪽 오류")
        return real(name, inp)
    monkeypatch.setattr(a, "_inner_tool", boom)
    evs, _ = _turn(monkeypatch, ag, tools, "이 판의 낙하에 취약한 부품을 알려줘")
    assert ag.ainvoked == 0 and not any("자동 재시도" in s for s in _steps(evs)) and [e for e, _ in evs if e == "error"] == []
    card = [d for e, d in evs if e == "confirm"]
    assert len(card) == 1 and card[0]["id"] in _result(evs)


def test_B24_확정_종결의_직접_실행도_막히면_요약하지_않고_카드로_끝난다(gate, monkeypatch):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    llm = []

    async def fake_llm_text(_llm, system, user, *a_, **k_):
        llm.append(system)
        return json.dumps(P1_GRAPH) if "도구 호출 계획자" in system else "요약 — 잡을 걸었습니다"
    monkeypatch.setattr(a, "_llm_text", fake_llm_text)
    monkeypatch.setattr(a.app.state, "chat_llm", object(), raising=False)      # _chat_llm_of 가 고르는 자리(대역 _llm_text 는 쓰지 않는다)
    ag = _GateAgent(tools, steps=[], say="run_job 을 호출하겠습니다")
    evs, _ = _turn(monkeypatch, ag, tools, "이 판의 낙하에 취약한 부품을 알려줘")
    assert reached == [] and len(llm) == 1, "인자 계획만 — 막은 결과를 요약하지 않는다"
    assert any(s.startswith("확인 대기: run_job") for s in _steps(evs))
    res = _result(evs)
    assert "요약 — 잡을 걸었습니다" not in res and len([d for e, d in evs if e == "confirm"]) == 1


def test_B24b_새어_나온_호출의_직접_실행도_막히면_요약하지_않고_카드로_끝난다(gate, monkeypatch):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    llm = []

    async def fake_llm_text(_llm, system, user, *a_, **k_):
        llm.append(system)
        return "요약 — 잡을 걸었습니다"
    monkeypatch.setattr(a, "_llm_text", fake_llm_text)
    monkeypatch.setattr(a.app.state, "chat_llm", object(), raising=False)
    leak = '<tool_call>{"name": "run_job", "arguments": {"project_id": "P1", "kind": "graph"}}</tool_call>'
    ag = _GateAgent(tools, steps=[], say=leak)
    ag.again_say = leak                  # 비스트리밍 재시도도 또 샌다 — 코드가 직접 실행하는 마지막 길
    evs, _ = _turn(monkeypatch, ag, tools, "이 판의 낙하에 취약한 부품을 알려줘")
    assert reached == [] and llm == [] and ag.ainvoked == 1, "막은 결과를 요약하지 않는다"
    assert any(s.startswith("확인 대기: run_job") for s in _steps(evs)) and len([d for e, d in evs if e == "confirm"]) == 1


def test_B25_띵킹이_켜져_있어도_카드의_답은_일반_챗으로_가_승인된다(gate, monkeypatch):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    thought = []

    async def fake_thinking(*_a, **_k):
        thought.append(1)
        yield a._sse("done", {})
    monkeypatch.setattr(a, "run_thinking", fake_thinking)
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    _turn(monkeypatch, _GateAgent(tools, steps=[], say="걸었습니다."), tools, card["approve_text"], hist, via_chat=True, thinking=True)
    assert thought == [] and reached == [("run_job", P1_GRAPH)]
    _turn(monkeypatch, _GateAgent(tools, steps=[], say="네."), tools, "다른 질문", via_chat=True, thinking=True)
    assert thought == [1], "카드의 답이 아니면 띵킹 그대로"


def test_I7_권한_판정도_카드의_답을_일반_챗으로_본다(gate, monkeypatch):
    hist = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "… (확인 번호 `cf-0123456789ab` · 30분)"}]

    def denied(msg, h=hist):
        return a._denied_feature(a.ChatRequest(message=msg, history=h, thinking=True, entitlements=["feat:chat"]))
    assert denied("실행 — 확인 번호 cf-0123456789ab") is None and denied("응") is None, "C-10 — chat() 과 같은 판정"
    assert denied("다른 질문") == "feat:thinking" and denied("응", h=hist[:1]) == "feat:thinking"
    monkeypatch.setattr(a, "CHAT_CONFIRM_GATE", False)
    assert denied("응") == "feat:thinking", "관문이 꺼져 있으면 카드의 답도 없다"


def test_B26_작업량(gate, monkeypatch, maps):
    maps({"run_job": "heax-step_forge"})
    reached = []
    _evs, card, _h = _card_turn(monkeypatch, _sf_tools(reached, {"expected_s": 60, "low_s": 40, "high_s": 90, "cost_basis": "history"}),
                                [("run_job", P1_DROP)])
    w = card["calls"][0]["workload"]
    assert w == "약 60초(40 ~ 90초 · 근거 history) — 추정이며 하한일 수 있다", w
    assert ("heaxstep_forge_estimate_cost", {"project_id": "P1", "job": "measure", "args": {"tool": "drop_check"}}) in reached
    assert any(s == "확인 카드 — 예상 작업량을 묻는 중" for s in _steps(_evs)) and f"예상 작업량: {w}" in _h[1]["content"]
    nb = {"expected_s": None, "cost_basis": None, "gateway_verdict": "async_job", "basis_note": "**근거가 없다** — 이 과제의 이 잡 이력도 없다",
          "advice": "잡으로 돌린다"}
    _e, card, _h = _card_turn(monkeypatch, _sf_tools([], nb), [("run_job", P1_GRAPH)])
    assert card["calls"][0]["workload"] == "예상 시간 없음 — **근거가 없다** — 이 과제의 이 잡 이력도 없다 · 잡으로 돌린다"
    monkeypatch.setattr(a, "_CONFIRM_WORKLOAD_S", 0.2)
    _e, card, _h = _card_turn(monkeypatch, _sf_tools([], EST, delay=2.0), [("run_job", P1_GRAPH)])
    assert card["calls"][0]["workload"] == "예상 작업량을 0.2초 안에 받지 못했다", "넘으면 그렇게 적고 카드는 선다"
    _e, card, _h = _card_turn(monkeypatch, {"run_job": _gw("run_job", [], reply=lambda n, x: "x"),
                                            "invoke_tool": _gw("invoke_tool", [], reply=lambda n, x: "✖ 도구 estimate_cost 호출 실패: 403")},
                              [("run_job", P1_GRAPH)])
    assert card["calls"][0]["workload"].startswith("예상 작업량을 받지 못했다 — ✖")


def test_B26b_시간_상한과_기다림_상한은_인자나_스키마_기본값으로(gate, monkeypatch):
    reached = []
    drop = _gw("fullangle_drop_simulation", reached, props={"time_limit": {"type": "string", "default": "12:00:00"}})
    inner_schema = _gw("single_drop_simulation", reached, props={"time_limit": {"type": "string", "default": "01:00:00"}})
    tools = {"fullangle_drop_simulation": drop, "single_drop_simulation": inner_schema, "invoke_tool": _gw("invoke_tool", reached),
             "recipe_run": _gw("recipe_run", reached), "docforge_convert": _gw("docforge_convert", reached)}
    step = [("fullangle_drop_simulation", {"deck": "a.k"}), ("invoke_tool", {"name": "single_drop_simulation", "arguments": {"deck": "b.k"}}),
            ("recipe_run", {"recipe": "r1", "wait_sec": 120}), ("docforge_convert", {"path": "/s/a.pptx"})]
    _evs, card, _h = _card_turn(monkeypatch, tools, step)
    got = [c["workload"] for c in card["calls"]]
    assert got == ["Slurm 시간 상한 12:00:00(기본값)", "Slurm 시간 상한 01:00:00(기본값)", "결과까지 최대 120초 기다린다",
                   "이 도구는 작업량을 미리 알려 주지 않는다"], got
    assert reached == [], "묻기에 dry_run 같은 부수효과 있는 호출을 쓰지 않는다"


def test_B27_카드_뒤의_재촉은_이어하기도_승인도_아니다(gate, monkeypatch):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    ag = _GateAgent(tools, steps=[], say="무엇을 계속할까요?")
    evs, _ = _turn(monkeypatch, ag, tools, "계속", hist)
    assert "직전 요청 이어서 진행" not in _steps(evs) and reached == [] and card["id"] not in a._CONFIRM_PENDING
    assert ag.inputs[0]["messages"][-1][1].endswith("계속")


def test_B27c_다른_대화의_말은_이_대화의_카드를_지우지_않는다(gate, monkeypatch):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    _turn(monkeypatch, _GateAgent(tools, steps=[], say="네."), tools, "다른 대화의 질문", conv="c2")
    _turn(monkeypatch, _GateAgent(tools, steps=[], say="네."), tools, "다른 사람의 질문", user="v@x.com")
    assert card["id"] in a._CONFIRM_PENDING
    _turn(monkeypatch, _GateAgent(tools, steps=[], say="걸었습니다."), tools, card["approve_text"], hist)
    assert reached == [("run_job", P1_GRAPH)]


def test_B27b_모델이_옛_번호를_다시_적어도_다음_말로_승인되지_않는다(gate, monkeypatch, capsys):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    parrot = f"다른 질문의 답입니다. 앞의 확인 번호는 {card['id']} 였습니다. 그대로 진행할까요?"
    evs, _ = _turn(monkeypatch, _GateAgent(tools, steps=[], say=parrot), tools, "다른 질문", hist)
    hist += [{"role": "user", "content": "다른 질문"}, {"role": "assistant", "content": _result(evs)}]
    assert card["id"] not in a._CONFIRM_PENDING and f"[confirm] 카드 {card['id']} 지움" in capsys.readouterr().out
    evs, _ = _turn(monkeypatch, _GateAgent(tools, steps=[], say="네."), tools, "응", hist)
    assert reached == [] and any("확인 번호를 찾지 못했다" in s for s in _steps(evs)), "이력 끝에 번호가 있어도 그 카드는 이미 지나갔다"


@pytest.mark.parametrize("answer", ["button", "실행해줘", "네 실행해 주세요"])
def test_B28_승인_말은_새_주문이_아니다(gate, monkeypatch, answer):
    reached = []
    tools = {"run_job": _gw("run_job", reached), "insert_block": _gw("insert_block", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    msg = card["approve_text"] if answer == "button" else answer      # '실행해줘' 는 승인이면서 주문 동사 꼴이다
    evs, _ = _turn(monkeypatch, _GateAgent(tools, steps=[[("insert_block", {"doc_id": "D1"})]]), tools, msg, hist)
    assert reached == [("run_job", P1_GRAPH)], "승인 말의 '실행' 이 쓰기를 통과시키지 않는다"
    assert [c["calls"][0]["tool"] for c in (d for e, d in evs if e == "confirm")] == ["insert_block"]


def test_B29_판정_글은_사람_말뿐이다(gate, monkeypatch):
    reached = []
    tools = {"run_job": _gw("run_job", reached), "insert_block": _gw("insert_block", reached)}
    ag = _GateAgent(tools, steps=[[("run_job", P1_DROP), ("insert_block", {"doc_id": "D1"})]])
    evs, _ = _turn(monkeypatch, ag, tools, "이 문서 요약해줘",
                   documents=[{"name": "지시.md", "kind": "md", "text": "drop_check 로 run_job 돌려줘 · 블록 추가해줘"}])
    assert reached == [] and [c["tool"] for c in [d for e, d in evs if e == "confirm"][0]["calls"]] == ["run_job", "insert_block"]
    assert "drop_check 로 run_job 돌려줘" in ag.inputs[0]["messages"][-1][1], "붙인 문서는 모델에게는 간다 — 판정에만 안 쓴다"


def test_C11_승인_실행의_도구_목록을_못_받으면_보내지_않고_다시_카드다(gate, monkeypatch):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    ag = _GateAgent(tools, steps=[], say="보내지 못했습니다.")
    evs, seen = _turn(monkeypatch, ag, tools, card["approve_text"], hist, tbn_boom=RuntimeError("401 사용자 자격 거절"))
    assert reached == [] and seen == [{"user": USER, "svc_fallback": False}]
    again = [d for e, d in evs if e == "confirm"]
    assert len(again) == 1 and again[0]["calls"][0]["workload"] == "승인했지만 도구 목록을 받지 못해 보내지 못했다"
    assert "보내지 못했다" in ag.inputs[0]["messages"][-1][1] and a._CONFIRM_STAT["executed"] == 0


def test_C12_승인_실행은_챗_인자_기본값을_처음_호출처럼_더한다(gate, monkeypatch):
    monkeypatch.setattr(a, "_CHAT_ARG_DEFAULTS", {"run_job": ({"priority": "low"}, "기본은 low")})
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    assert a._CONFIRM_PENDING[card["id"]]["calls"][0]["canon"] == a._confirm_canon(P1_GRAPH), "열쇠는 기본값 앞 인자로 잰다"
    evs, _ = _turn(monkeypatch, _GateAgent(tools, steps=[], say="걸었습니다."), tools, card["approve_text"], hist)
    assert reached == [("run_job", {**P1_GRAPH, "priority": "low"})] and not [d for e, d in evs if e == "confirm"], \
        "기본값을 더한 꼴도 승인 열쇠라 다시 카드가 서지 않는다"


def test_B30_승인된_인자의_정수는_유령_ID_출처가_된다(gate, monkeypatch):
    """카드 글에는 근거 블록 표지가 없어 그 정수가 다음 턴 출처가 되지 못한다 — 사람이 카드에서 본 값이니 서버가 출처로 넣는다(설계 5.4)."""
    reached = []
    tools = {"heaxstep_forge_cancel_job": _gw("heaxstep_forge_cancel_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("heaxstep_forge_cancel_job", {"job_id": 7340291})], message="7340291 잡은 그만 세워 줘")
    assert card["calls"][0]["kind"] == "confirm" and "job_id=7340291" in hist[1]["content"]
    evs, _ = _turn(monkeypatch, _GateAgent(tools, steps=[], say="세웠습니다."), tools, card["approve_text"], hist[1:])
    assert reached == [("heaxstep_forge_cancel_job", {"job_id": 7340291})], _steps(evs)


def test_루트_conftest_가_관문을_끄고_목록을_없음으로_돌린다():
    """다른 챗 턴 시험이 잡 꼴 도구 대역을 부르고 호출 수를 센다 — 이 격리가 빠지면 그 시험들이 카드로 끝난다(design-integration C-5)."""
    assert os.environ.get("CHAT_CONFIRM_GATE") == "0" and a.CHAT_CONFIRM_GATE is False
    assert a.CONFIRM_LISTS_PATH.name == "no-he-team.json" and not a.CONFIRM_LISTS_PATH.exists()
    assert a._confirm_health()["lists"]["error"] == "없음"
    assert a._SP_CONFIRM_OLD in a._SP_HEAD, "꺼진 관문의 시스템 프롬프트는 종전 글"


def test_health_에_confirm_gate_가_실린다(gate, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from fakes.llm import FakeLLM      # tests/test_health_contract.py 의 client 와 같은 꾸밈 — 떠 있는 서비스에 묻지 않게
    monkeypatch.setenv("DRAIN_STATE_PATH", str(tmp_path / "drain.json"))
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"]), raising=False)
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setattr(a, "_delib_human", lambda: 0)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    reached = []
    _card_turn(monkeypatch, {"run_job": _gw("run_job", reached)}, [("run_job", P1_GRAPH)])
    h = TestClient(a.app).get("/health").json()["confirm_gate"]
    assert h == {"on": True, "lists": {"loaded": True, "error": None, "jobs": 24, "writes": len(a._confirm_lists()["writes"]),
                                       "confirm": len(a._confirm_lists()["confirm"])},
                 "pending": 1, "counts": {"cards": 1, "held_calls": 1, "approved": 0, "executed": 0, "declined": 0,
                                          "unknown_reply": 0, "ordered": 0}}
    assert str(gate) not in json.dumps(h), "경로는 싣지 않는다"


def test_카드_저장은_같은_대화의_옛_카드를_지우고_상한을_지킨다(gate, monkeypatch):
    monkeypatch.setattr(a, "_CONFIRM_MAX", 2)
    req = a.ChatRequest(message="x", user_email="U@X.com", conversation_id="c1")
    first, _ = a._confirm_store(req, [])
    second, _ = a._confirm_store(req, [])
    assert list(a._CONFIRM_PENDING) == [second] and a._CONFIRM_PENDING[second]["user"] == "u@x.com"
    for c in ("c2", "c3"):
        a._confirm_store(a.ChatRequest(message="x", user_email=USER, conversation_id=c), [])
    assert len(a._CONFIRM_PENDING) == 2 and second not in a._CONFIRM_PENDING, "넘으면 오래된 것부터"


def test_로그에_사람_메일을_싣지_않는다(gate, monkeypatch, capsys):
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    _turn(monkeypatch, _GateAgent(tools, steps=[], say="네."), tools, card["approve_text"], hist)
    out = capsys.readouterr().out
    assert f"[confirm] 카드 {card['id']} — run_job(job)" in out and f"[confirm] 승인 {card['id']} — 1건 보냄" in out
    assert USER not in out


@pytest.mark.skipif(not PORTAL_HE_TEAM.exists(), reason="형제 포털 리포가 곁에 없다")
def test_형제_포털의_운영자_정의를_읽는다(gate, monkeypatch):
    monkeypatch.setattr(a, "CONFIRM_LISTS_PATH", PORTAL_HE_TEAM)
    lists = a._confirm_health()["lists"]
    assert lists["loaded"] is True and lists["writes"] > 0 and lists["confirm"] > 0, lists
    assert a._confirm_class("heaxstep_forge_run_job")[0] == "job", "DW-03 의 잡"
    assert a._confirm_class("cancel_job")[0] == "confirm" and a._confirm_class("heaxstep_forge_cancel_job")[0] == "confirm"
    assert a._confirm_class("list_projects") == (None, "") and a._confirm_class("heaxstep_forge_job_status") == (None, "")


def test_기본_경로는_형제_포털_리포의_정본이다(tmp_path):
    """루트 conftest 가 시험마다 '없음' 으로 돌리므로 모듈의 처음 값을 새 인터프리터로 본다(상대 경로 손잡이는 이 리포 루트가 앵커)."""
    code = "import app; print(app.CONFIRM_LISTS_PATH)"
    base = {**os.environ, "ARTIFACT_DIR": str(tmp_path / "art"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT)}
    base.pop("CONFIRM_LISTS_PATH", None)
    got = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=base, capture_output=True, text=True, timeout=120)
    assert got.stdout.strip().splitlines()[-1] == str(ROOT.parent / "HWAXPortal" / "infra" / "personas" / "he-team.json"), got.stderr[-800:]
    rel = subprocess.run([sys.executable, "-c", code], cwd="/", env={**base, "CONFIRM_LISTS_PATH": "x/he.json"},
                         capture_output=True, text=True, timeout=120)
    assert rel.stdout.strip().splitlines()[-1] == str(ROOT / "x" / "he.json"), rel.stderr[-800:]


# ── 진짜 langgraph(create_react_agent) — 막은 호출 뒤 다음 모델 호출 앞에서 끊고 흐름을 닫는다 ───────────────────────────────

def test_진짜_그래프에서도_끊기면_모델의_뒷말이_없고_카드로_끝난다(gate, monkeypatch):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.prebuilt import create_react_agent

    class _ToolModel(BaseChatModel):
        calls: int = 0

        @property
        def _llm_type(self) -> str:
            return "fake-tool-model"

        def bind_tools(self, tools, **kw):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            raise NotImplementedError

        async def _agenerate(self, messages, stop=None, run_manager=None, **kw):
            self.calls += 1
            msg = (AIMessage(content="", tool_calls=[{"name": "run_job", "args": P1_GRAPH, "id": "call_1"}]) if self.calls == 1
                   else AIMessage(content="잡을 걸었습니다 J-1"))
            return ChatResult(generations=[ChatGeneration(message=msg)])
    reached = []
    tools = {"run_job": _gw("run_job", reached)}
    agent = create_react_agent(_ToolModel(), list(tools.values()))
    evs, _ = _turn(monkeypatch, agent, tools, "이 판의 낙하에 취약한 부품을 알려줘")
    res = _result(evs)
    card = [d for e, d in evs if e == "confirm"]
    assert len(card) == 1 and card[0]["id"] in res and reached == []
    assert "잡을 걸었습니다" not in res and [e for e, _ in evs if e == "error"] == []
    assert any(s.startswith("확인 대기: run_job") for s in _steps(evs))
