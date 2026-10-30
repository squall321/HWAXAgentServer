# 합친 판의 맞물림 시험 I-1 ~ I-8 — 10-30 높음 넷(HA-27 자기검증 관문 · HA-28 상태 기다림 · HA-29 확인 카드 · HA-30 다른 허브 id)을 둘 이상 함께 켠 턴이 설계대로 맞물리는지 본다
"""HWAXPortal docs/request-1030/design-integration.md 11절의 I-1 ~ I-8 이다. 행마다의 시험(tests/test_confirm_gate.py · test_status_wait.py ·
test_request_1030_ha30.py · test_request_1030_ha27.py)은 그 행의 손잡이 하나만 켠다 — 여기서는 둘 이상을 함께 켜야 드러나는 자리를 본다.

  · I-1 DW-02 — 카드 턴은 잡을 보내지 않고, 승인 턴은 서버가 보낸 잡(chat=False 래퍼 — C-3 born)을 모델의 첫 get_task 부터 기다린다. 승인
        실행은 사용자 자격만 쓴다(C-11). 짝 — 손잡이를 켜지 않은 같은 턴은 종전대로다(루트 conftest 의 두 손잡이 — C-5).
  · I-2 한 단계에 막힌 잡(카드)과 늘 도는 중인 상태 조회(born)가 나란하면 기다림이 다음 조회 앞에서 접힌다(멈춤 술어 — C-4).
  · I-3 다른 허브 id 막이(G4)가 확인 카드(G5)보다 먼저다 — 막힌 호출은 카드에 들지 않고, 대상 허브 id 로 고친 같은 쓰기가 카드가 된다.
  · I-4 사본에 HA-30 거절 글(숫자로 시작하는 UUID)이 든 턴에서 UUID 조각이 수가 되지 않아 검토자의 3 → 5 가 들어간다(C-13).
  · I-5 기다림이 끝난 상태 도구 — 원문 그릇에는 마지막 결과 하나, 기다림 안내의 수는 출처 있는 수다.
  · I-6 자기검증 켬 · 카드 턴 — 검토자 0번 · skipped · 그릇에 원문과 카드 글이 함께 · 근거 블록이 카드의 작업량을 짚지 않는다(C-14).
  · I-7 띵킹 켬 · 띵킹 권한 없음 · 단추 글 — 권한 판정이 막지 않고 일반 챗이 승인을 보낸다(C-10).
  · I-8 /health 한 번에 self_check.gate · status_wait · confirm_gate · xhub_gate · hub_fold 가 함께 실린다(포털 update-all §7 이 읽는 칸).

손잡이는 시험마다 제 손으로 켠다(루트 conftest 가 끈다). 대역은 네 행 시험의 것을 그대로 쓴다 — 에이전트는 앱이 실제로 짓는 길
(test_chat_loop_fit._agent — _agent_for 가 chat=True 래퍼를 씌운다), 승인 실행 · 작업량 묻기가 받는 도구 목록은 deliberation._tools_by_name 꼴
(chat=False 래퍼)이다.

  실행:  .venv/bin/python -m pytest tests/test_request_1030_interlock.py -q
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from langchain_core.messages import ToolMessage  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402

import app as a  # noqa: E402
import sc_gate as g  # noqa: E402
import status_wait as sw  # noqa: E402
from test_chat_loop_fit import _agent  # noqa: E402 — 앱이 실제로 만드는 길(_agent_for) + 게이트웨이 스텁
from test_chat_self_check import _Reviewer, _inner, _run as _sc_run, _text  # noqa: E402
from test_confirm_gate import (CONV, EST, P1_GRAPH, USER, _GateAgent, _card_turn, _gw, _result, _sf_tools,  # noqa: E402
                               _steps, _turn)
from test_health_contract import client  # noqa: E402,F401 — /health 고정물(떠 있는 서비스에 묻지 않게 꾸민 TestClient)
from test_request_1029 import _tool_of  # noqa: E402
from test_request_1030_ha30 import (FT_GRAPH, FT_RESOLVE, LABELS, MAP, RD_RESOLVE, RESOLVE_ARGS, U_FT, U_RD,  # noqa: E402
                                    _HubLLM, _hit, _resolve)
from test_status_wait import ODB_DONE, ODB_RUN, _Script, _cache, _fake, _seq  # noqa: E402
from test_status_wait import _turn as _wait_turn  # noqa: E402

PORTAL_HE_TEAM = ROOT.parent / "HWAXPortal" / "infra" / "personas" / "he-team.json"

# DW-02(10-30 QA) — '돌려서' 는 주문 동사 꼴이 아니고(이어 주는 꼴) 잡 이름도 말에 없어 카드가 먼저 선다(design-integration 9절 9)
DW02 = "체크리스트 돌려서 결과 알려줘"
START = {"task_id": "t-77", "kind": "checklist", "status": "queued", "progress": 0.0}       # ODB run_checklist 의 시작 응답
DONE = {**ODB_DONE, "task_id": "t-77"}
# 운영자 정의 — 포털 he-team.json 의 he-data-coreontology 와 같은 꼴(정본 허브의 쓰기를 접두 별칭으로 적고 sibling 허브는 optional 이다)
HUB_TEAM = {"personas": [{
    "key": "he-data-coreontology", "apps": ["rootdesigndatahub"],
    "writes": ["rootdesigndatahub_object_create", "rootdesigndatahub_object_update"],
    "confirm": ["rootdesigndatahub_objects_delete", "rootdesigndatahub_object_merge"],
    "optional": [{"app": "failuretrace", "needs_tool": "object_resolve", "key_tools": ["failuretrace_object_resolve"]},
                 {"app": "caegroupdatahub", "needs_tool": "object_resolve"}]}]}


# ── 손잡이 — 루트 conftest 가 끈 것을 시험마다 제 손으로 켠다 ───────────────────────────────────────────────────────────────

def _gate_on(monkeypatch, tmp_path=None, lists=None) -> None:
    """확인 관문(HA-29) — 기동 때 읽는 모듈 값을 켠다. lists 를 주면 그 운영자 정의를, 안 주면 루트 conftest 의 '없음'(잡 시작 바닥 목록만)."""
    monkeypatch.setattr(a, "CHAT_CONFIRM_GATE", True)
    if lists is not None:
        p = tmp_path / "he-team.json"
        p.write_text(json.dumps(lists, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(a, "CONFIRM_LISTS_PATH", p)


def _wait_on(monkeypatch, max_s: float = 5.0, poll_s: float = 0.02) -> None:
    """상태 기다림(HA-28) — import 때 읽은 모듈 값을 켜고 짧게 돈다(tests/test_status_wait.py 의 _on 꼴)."""
    monkeypatch.setattr(sw, "MAX_S", max_s)
    monkeypatch.setattr(sw, "POLL_S", poll_s)
    monkeypatch.setattr(sw, "TELL_S", 0.0)
    monkeypatch.setattr(sw, "EXTRA", frozenset())


def _self_check_on(monkeypatch, reviewer) -> None:
    """자기검증(관문 기본 모드 1 · 원문 기본 몫) — 턴마다 읽는 환경 값이다. 검토자 대역은 챗 LLM 자리에 앉힌다."""
    monkeypatch.setenv("CHAT_SELF_CHECK", "1")
    monkeypatch.delenv("CHAT_SELF_CHECK_GATE", raising=False)
    monkeypatch.delenv("CHAT_SELF_CHECK_RAW_CHARS", raising=False)
    monkeypatch.setattr(a.app.state, "chat_llm", reviewer, raising=False)


def _xhub_on(monkeypatch) -> None:
    """다른 허브 id 막이(HA-30) — 기본이 켬이지만 셸 값에 흔들리지 않게 박는다."""
    monkeypatch.setattr(a, "_XHUB_GATE", True)


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    """떠 있는 게이트웨이 · LLM 서버에 묻지 않고(도구 지도 · 앱 캐시 · 창을 박는다) 네 행과 허브 접기의 계수를 시험마다 0 에서 센다. 손잡이는 켜지
    않는다 — 켜는 것은 시험의 몫이다."""
    _cache(monkeypatch, {})
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    for k, v in (("tool_load_error", {}), ("tool_degraded", {}), ("chat_llm", None), ("chat_llm_cfg", None), ("llm_nostream", False)):
        monkeypatch.setattr(a.app.state, k, v, raising=False)
    monkeypatch.setattr(sw, "_STAT", {"waits": 0, "ended": 0, "capped": 0, "failed": 0})
    monkeypatch.setattr(a, "_XHUB_STAT", {"blocked": 0})
    monkeypatch.setattr(a, "_CONFIRM_STAT", a.Counter())
    monkeypatch.setattr(a, "_SC_STAT", {"counts": a.Counter(), "ms": [], "last_error": None})
    monkeypatch.setattr(a, "_HUB_FOLD_STAT", {"count": 0, "chars_in": 0, "chars_out": 0})


# ── 대역 도우미 ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _pair(name, respond, props=None):
    """같은 앱 도구의 두 길 — 챗 에이전트가 묶는 것(감싸지 않은 채 넘기면 _agent_for 가 chat=True 로 감싼다)과 승인 실행 · 작업량 묻기가 받는
    것(deliberation._tools_by_name 꼴 — chat=False). 둘은 다른 도구 객체이고(감싸기는 제자리에서 바꾼다) 받은 인자는 한 기록에 쌓인다.
    → (챗 길 도구, 서버 길 도구, 받은 인자 목록)."""
    hits: list = []

    def shared(kw, _n):
        hits.append(dict(kw))
        return respond(kw, len(hits))
    chat_tool, _ = _fake(name, shared, props)
    svc_tool, _ = _fake(name, shared, props)
    return chat_tool, a._prep_tool(svc_tool), hits


def _hub_raw(name, result, log, peek=None):
    """허브 도구 대역(감싸지 않은 코루틴 도구 — 에이전트가 감싼다). 받은 인자와 peek() 값(그때의 턴 상태)을 적고 정해 둔 글을 돌려준다."""
    async def call(**kw):
        log.append((name, dict(kw), peek() if peek else None))
        return result
    return StructuredTool(name=name, description="d", coroutine=call, args_schema={"type": "object", "properties": {}})


def _spy_box(monkeypatch) -> list:
    """자기검증 감싸개가 안쪽 턴에 준 그릇을 쥔다 — 턴이 끝에 draft · calls · outputs · raw · confirm 을 싣는다(design-integration 3절)."""
    real, got = a._agent_stream_turn, []

    def spy(app_, req, box=None):
        if box is not None:
            got.append(box)
        return real(app_, req, box)
    monkeypatch.setattr(a, "_agent_stream_turn", spy)
    return got


def _cards(evs) -> list:
    return [d for e, d in evs if e == "confirm"]


def _waits(evs) -> list:
    """상태 기다림의 진행 줄(tool 없는 status · wait 칸)."""
    return [d for e, d in evs if e == "status" and "wait" in d]


def _last_tool_msg(msgs) -> str:
    return [m for m in msgs if isinstance(m, ToolMessage)][-1].content


# ── I-1 DW-02 — 카드 → 승인 실행이 건 잡을 첫 get_task 부터 기다린다(HA-28 × HA-29 · C-3 · C-11) ─────────────────────────────

def test_I1_DW02_카드_턴은_보내지_않고_승인_턴은_서버가_건_잡을_첫_get_task_부터_기다린다(monkeypatch):
    _gate_on(monkeypatch)
    _wait_on(monkeypatch)
    rc_chat, rc_svc, rc_hits = _pair("run_checklist", _seq(START), {"job_id": {"type": "string"}})
    gt_chat, gt_svc, gt_hits = _pair("get_task", _seq(ODB_RUN(0.3, "t-77"), ODB_RUN(0.6, "t-77"), DONE))
    llm = _Script(steps=[[("run_checklist", {"job_id": "j1"})], "확인 카드가 떴습니다."])
    agent = _agent(monkeypatch, llm, tools=(rc_chat, gt_chat))
    svc = {"run_checklist": rc_svc, "get_task": gt_svc}

    evs1, tbn1 = _turn(monkeypatch, agent, svc, DW02)
    cards = _cards(evs1)
    assert len(cards) == 1 and [(c["tool"], c["kind"]) for c in cards[0]["calls"]] == [("run_checklist", "job")], _steps(evs1)
    assert rc_hits == [] and gt_hits == [] and tbn1 == [], "카드 턴 — 잡을 보내지 않는다(작업량을 묻는 잡도 아니다)"
    assert len(llm.seen) == 1, "막은 단계 뒤 다음 모델 호출 앞에서 끊는다"
    assert not _waits(evs1)

    hist = [{"role": "user", "content": DW02}, {"role": "assistant", "content": _result(evs1)}]
    llm.steps, llm.seen = [[("get_task", {"task_id": "t-77"})], "체크리스트 위반은 3건입니다."], []
    evs2, tbn2 = _turn(monkeypatch, agent, svc, cards[0]["approve_text"], hist)
    assert rc_hits == [{"job_id": "j1"}], "승인 턴 — 서버가 처음 꼴 그대로 한 번 보냈다"
    assert tbn2 == [{"user": USER, "svc_fallback": False}], "승인 실행은 사용자 자격만 — 서비스 계정으로 넘기지 않는다(C-11)"
    done_rc = [d for e, d in evs2 if e == "status" and d.get("step") == "도구 완료: run_checklist"]
    assert len(done_rc) == 1 and done_rc[0]["ok"] is True and done_rc[0]["call"] == f"{cards[0]['id']}-1"
    # 모델은 get_task 를 한 번 불렀고, 그 한 번 안에서 코드가 끝까지 기다렸다 — born 이 승인 실행(chat=False 래퍼)에서 들었다(C-3)
    steps = [d for e, d in evs2 if e == "status"]
    assert sum(d.get("step") == "도구 호출: get_task" for d in steps) == 1 and len(gt_hits) == 3, (gt_hits, _steps(evs2))
    i0 = next(i for i, d in enumerate(steps) if d.get("step") == "도구 호출: get_task")
    i1 = next(i for i, d in enumerate(steps) if d.get("step") == "도구 완료: get_task")
    phases = [d["wait"]["phase"] for d in steps[i0 + 1:i1] if "wait" in d]
    assert phases and phases[0] == "start" and phases[-1] == "end", phases
    assert len(llm.seen) == 2, "기다리는 동안 모델을 부르지 않는다 — 조회 한 번 · 답 한 번"
    seen_by_model = _last_tool_msg(llm.seen[1])
    assert '"status": "done"' in seen_by_model and "끝난 상태(done)" in seen_by_model, seen_by_model
    assert "한 번 더 부르라" not in seen_by_model, "이 턴에 건 잡이다 — 지금 상태만 주고 되묻게 하지 않는다"
    assert sw.health()["waits"] == 1 and sw.health()["ended"] == 1
    assert not _cards(evs2) and _result(evs2).startswith("체크리스트 위반은 3건입니다.")


def test_I1_짝_손잡이를_켜지_않은_같은_턴은_종전대로_보내고_기다리지_않는다(monkeypatch):
    """루트 conftest 가 확인 관문(CHAT_CONFIRM_GATE=0) · 상태 기다림(CHAT_STATUS_WAIT_MAX_S=0)을 import 전에 끈다(design-integration C-5). 그 둘이
    빠지면 HA-28 의 실턴 시험 14 꼴(run_checklist → get_task)이 카드로 끝나거나 10초씩 자며 호출 수 · 글이 바뀐다 — 남의 턴 시험이 붉다."""
    rc, rc_hits = _fake("run_checklist", _seq(START), {"job_id": {"type": "string"}})
    gt, gt_hits = _fake("get_task", _seq(ODB_RUN(0.3, "t-77"), DONE))
    llm = _Script(steps=[[("run_checklist", {"job_id": "j1"})], [("get_task", {"task_id": "t-77"})], "아직 30% 진행 중입니다."])
    agent = _agent(monkeypatch, llm, tools=(rc, gt))
    evs, tbn = _turn(monkeypatch, agent, {}, DW02)
    assert not _cards(evs) and rc_hits == [{"job_id": "j1"}], "관문이 꺼져 있으면 시키지 않은 잡도 종전대로 보낸다"
    assert len(gt_hits) == 1 and not _waits(evs), "기다림이 꺼져 있으면 상태 조회는 한 번 · 진행 줄 없음"
    assert len(llm.seen) == 3 and tbn == []
    assert "[코드 기다림" not in _last_tool_msg(llm.seen[2])
    assert _result(evs).startswith("아직 30% 진행 중입니다.")


# ── I-2 카드가 설 턴의 기다림은 다음 조회 앞에서 접는다(HA-28 × HA-29 · C-4) ───────────────────────────────────────────────

def test_I2_막힌_잡과_나란한_born_상태_조회는_POLL_S_한_번_안에_접히고_카드가_선다(monkeypatch):
    _gate_on(monkeypatch)
    _wait_on(monkeypatch, max_s=3.0, poll_s=0.3)
    rc, rc_hits = _fake("run_checklist", _seq(START), {"job_id": {"type": "string"}})
    gt, gt_hits = _fake("get_task", _seq(ODB_RUN(0.4, "t-77")))                       # 늘 도는 중 — 끝나지 않는다
    rj, rj_hits = _fake("run_job", _seq({"job_id": "J-9", "status": "pending"}),
                        {"project_id": {"type": "string"}, "kind": {"type": "string"}})
    llm = _Script(steps=[[("run_checklist", {"job_id": "j1"})], [("run_job", P1_GRAPH), ("get_task", {"task_id": "t-77"})],
                         "대본 끝 — 여기까지 오면 안 된다"])
    agent = _agent(monkeypatch, llm, tools=(rc, gt, rj))
    t0 = time.monotonic()
    evs, _tbn = _turn(monkeypatch, agent, {}, "run_checklist 돌려줘")        # 이 잡은 이름으로 시켰다 — 통과해 born 이 든다
    took = time.monotonic() - t0
    assert rc_hits == [{"job_id": "j1"}] and rj_hits == [], "시킨 잡은 가고 시키지 않은 잡은 막는다"
    cards = _cards(evs)
    assert len(cards) == 1 and [c["tool"] for c in cards[0]["calls"]] == ["run_job"], _steps(evs)
    assert len(gt_hits) == 1, f"다시 조회하지 않았다 — 막은 호출이 있는 턴이다(실제 {len(gt_hits)}번)"
    waits = _waits(evs)
    assert [w["wait"]["phase"] for w in waits] == ["start", "stop"], [w["wait"] for w in waits]
    assert waits[-1]["wait"]["elapsed_s"] <= sw.POLL_S + 0.5 and took < sw.MAX_S, (waits[-1]["wait"], took)
    assert all(w["tool"] is None for w in waits)
    assert sw.health() == {"on": True, "poll_s": 0.3, "max_s": 3.0, "waits": 1, "ended": 0, "capped": 0, "failed": 0}, \
        "접은 기다림은 끝 · 상한 · 실패 어디에도 세지 않는다"
    assert len(llm.seen) == 2, "카드 턴 — 다음 모델 호출 앞에서 끊는다"


# ── I-3 다른 허브 id 막이(G4)가 확인 카드(G5)보다 먼저다(HA-29 × HA-30) ───────────────────────────────────────────────────

I3_SCRIPT = [("rootdesigndatahub_object_resolve", RESOLVE_ARGS),
             ("failuretrace_object_update", {"object_id": U_RD, "fields": {"status": "검토 중"}}),      # 정본 허브의 id — 막힌다
             ("failuretrace_object_resolve", RESOLVE_ARGS),
             ("failuretrace_object_update", {"object_id": U_FT, "fields": {"status": "검토 중"}}),      # 대상 허브 id — 카드
             "대본 끝 — 여기까지 오면 안 된다"]


def test_I3_다른_허브_id_로_막힌_쓰기는_카드에_들지_않고_대상_허브_id_로_고친_쓰기가_카드가_된다(monkeypatch, tmp_path):
    _gate_on(monkeypatch, tmp_path, HUB_TEAM)
    _xhub_on(monkeypatch)
    _cache(monkeypatch, {**MAP, "failuretrace_object_update": "failuretrace"}, labels=LABELS)
    assert a._confirm_class("failuretrace_object_update") == ("write", "object_update"), \
        "전제 — 목록의 rootdesigndatahub_object_update 가 접두를 떼어 object_update 로 맞는다(C-15 ④)"
    log: list = []

    def pending():
        ctx = a._turn_confirm.get()
        return None if ctx is None else list(ctx.pending)
    tools = (_hub_raw("rootdesigndatahub_object_resolve", RD_RESOLVE, log),
             _hub_raw("failuretrace_object_resolve", FT_RESOLVE, log, peek=pending),
             _hub_raw("failuretrace_object_update", '{"ok": true}', log))
    llm = _HubLLM(script=list(I3_SCRIPT))
    agent = _agent(monkeypatch, llm, tools=tools)
    evs, _tbn = _turn(monkeypatch, agent, {}, "이 고장모드 상태 좀 봐 줄래?")
    assert [n for n, *_ in log] == ["rootdesigndatahub_object_resolve", "failuretrace_object_resolve"], log
    assert log[1][2] == [], "다른 허브 id 로 막힌 호출은 확인 대기에 쌓이지 않는다(pending 빔)"
    blocked = _last_tool_msg(llm.seen[2])
    assert a._XHUB_ID_MARK in blocked and a._CONFIRM_MARK not in blocked, "카드가 아니라 HA-30 글 — key 로 다시 풀라는 말"
    assert 'failuretrace_object_resolve(type_slug="failure_mode", name="JNT-SLD-01")' in blocked, blocked
    done = [d for e, d in evs if e == "status" and d.get("step", "").startswith(("도구 완료: failuretrace_object_update",
                                                                                 "확인 대기: failuretrace_object_update"))]
    assert [d["step"].split(":")[0] for d in done] == ["도구 완료", "확인 대기"] and all(d["ok"] is False for d in done), done
    assert a._XHUB_ID_MARK in done[0]["result_preview"]
    cards = _cards(evs)
    assert len(cards) == 1 and len(cards[0]["calls"]) == 1, _steps(evs)
    call = cards[0]["calls"][0]
    assert (call["tool"], call["kind"], call.get("app")) == ("failuretrace_object_update", "write", "FailureTrace")
    assert f"object_id={U_FT}" in call["args_text"] and U_RD not in json.dumps(cards[0], ensure_ascii=False), \
        "사람이 승인할 대상은 대상 허브에서 다시 푼 id 다 — 틀린 id 가 카드에 오르지 않는다"
    stored = a._CONFIRM_PENDING[cards[0]["id"]]["calls"]
    assert [c["args"] for c in stored] == [{"object_id": U_FT, "fields": {"status": "검토 중"}}]
    assert a._xhub_gate_health()["blocked"] == 1 and a._confirm_health()["counts"]["held_calls"] == 1
    assert len(llm.seen) == 4, "카드 턴 — 막은 쓰기 뒤 다음 모델 호출 앞에서 끊는다"


@pytest.mark.skipif(not PORTAL_HE_TEAM.exists(), reason="형제 포털 리포가 곁에 없다")
def test_I3_형제_포털의_운영자_정의에서도_다른_허브의_같은_쓰기는_접두를_떼어_쓰기로_맞는다(monkeypatch):
    """I-3 의 전제가 정본 목록에서도 서는지 — 허브 쓰기는 정본 허브 접두(rootdesigndatahub_)로만 적혀 있다(design-integration C-15 ④)."""
    _gate_on(monkeypatch)
    monkeypatch.setattr(a, "CONFIRM_LISTS_PATH", PORTAL_HE_TEAM)
    assert a._confirm_lists()["error"] is None
    assert a._confirm_class("failuretrace_object_update") == ("write", "object_update")
    assert a._confirm_class("failuretrace_object_resolve") == (None, ""), "읽기는 목록 밖이다"


# ── I-4 HA-30 거절 글이 든 사본에서 UUID 조각은 수가 아니다(HA-27 × HA-30 · C-13) ─────────────────────────────────────────

U3 = "3abcdef0-1234-4abc-9def-0123456789ab"          # 첫 묶음이 숫자로 시작하는 UUID — 가림이 없으면 '3' 이 수로 샌다
FT_GRAPH5 = json.dumps({"focus": U_FT, "nodes": [{"id": U_FT, "key": "JNT-SLD-01", "label": "솔더 접합 불량", "type_slug": "failure_mode"}],
                        "edges": [], "defect_count": 5, "truncated": False}, ensure_ascii=False)
DRAFT4 = "FailureTrace 의 JNT-SLD-01 고장모드에 이어진 불량은 3건입니다."
REVIEW_3_TO_5 = ("[판정] 고침\n[고친 점]\n- 불량 3건 → 5건(조회 결과의 defect_count)\n[바꿀 곳]\n[원문]\n불량은 3건\n[새 글]\n불량은 5건")


def test_I4_HA30_거절_글의_UUID_조각이_강한_수가_되지_않아_검토자의_3_에서_5_가_들어간다(monkeypatch):
    _xhub_on(monkeypatch)
    rv = _Reviewer(REVIEW_3_TO_5)
    _self_check_on(monkeypatch, rv)
    _cache(monkeypatch, MAP, labels=LABELS)
    log: list = []
    tools = (_hub_raw("rootdesigndatahub_object_resolve", _resolve(_hit(U3)), log),
             _hub_raw("failuretrace_graph_neighbors", FT_GRAPH5, log),
             _hub_raw("failuretrace_object_resolve", FT_RESOLVE, log))
    llm = _HubLLM(script=[("rootdesigndatahub_object_resolve", RESOLVE_ARGS), ("failuretrace_graph_neighbors", {"object_id": U3}),
                          ("failuretrace_object_resolve", RESOLVE_ARGS), ("failuretrace_graph_neighbors", {"object_id": U_FT}), DRAFT4])
    agent = _agent(monkeypatch, llm, tools=tools)
    evs, _tbn = _turn(monkeypatch, agent, {}, "JNT-SLD-01 고장모드에 이어진 FailureTrace 불량은 몇 건이야?")
    assert [kw for n, kw, _ in log if n == "failuretrace_graph_neighbors"] == [{"object_id": U_FT}], "정본 id 로는 보내지 않았다"
    review_in = rv.calls[0][0][1].content
    assert U3[0].isdigit() and f"object_id={U3} {a._XHUB_ID_MARK}" in review_in, "전제 — 검토자 사본에 HA-30 거절 글이 들었다"
    sc = [d for e, d in evs if e == "meta"][-1]["self_check"]
    assert sc["status"] == "fixed" and sc["gate"]["applied"] == 1, sc
    assert not sc.get("flags") and not sc.get("dropped"), "UUID 조각 '3' 이 원문 수로 읽히면 이 쌍이 짚음(약한 출처) · 버림이 된다"
    assert _result(evs).startswith(DRAFT4.replace("3건", "5건")), _result(evs)[:200]


# ── I-5 기다림이 끝난 상태 도구 — 그릇에 마지막 결과 하나 · 안내의 수는 출처 있는 수(HA-27 × HA-28) ─────────────────────────────

DRAFT5 = "체크리스트 검사가 끝났습니다 — 코드가 4번 다시 조회해 끝난 상태를 받았습니다. 위반은 3건입니다."
REVIEW_DROP_POLLS = ("[판정] 고침\n[고친 점]\n- 조회 횟수는 근거가 없어 지운다\n[바꿀 곳]\n[원문]\n코드가 4번 다시 조회해\n[새 글]\n"
                     "코드가 다시 조회해")


def test_I5_기다림이_끝난_상태_도구는_그릇에_마지막_결과_하나이고_안내의_수는_출처_있는_수다(monkeypatch):
    _wait_on(monkeypatch)
    rv = _Reviewer(REVIEW_DROP_POLLS)
    _self_check_on(monkeypatch, rv)
    rc, _rc_hits = _fake("run_checklist", _seq(START), {"job_id": {"type": "string"}})
    gt, gt_hits = _fake("get_task", _seq(*(ODB_RUN(0.2 * k, "t-77") for k in range(1, 5)), DONE))
    llm = _Script(steps=[[("run_checklist", {"job_id": "j1"})], [("get_task", {"task_id": "t-77"})], DRAFT5])
    agent = _agent(monkeypatch, llm, tools=(rc, gt))
    boxes = _spy_box(monkeypatch)
    evs, _tbn = _turn(monkeypatch, agent, {}, "ODB 체크리스트를 돌려 끝나면 결과 알려줘")
    assert len(gt_hits) == 5 and len(llm.seen) == 3, "조회 한 번 안에서 코드가 네 번 다시 보고 끝을 받았다"
    box = boxes[0]
    assert box["raw"]["texts"] == [json.dumps(START, ensure_ascii=False), json.dumps(DONE, ensure_ascii=False)], \
        "원문 그릇에는 상태 도구의 마지막 결과 하나만 — 도는 중이던 결과는 붙잡지 않는다"
    note_out = next(o for o in box["outputs"] if "[코드 기다림 —" in o)
    note = note_out[note_out.index("[코드 기다림 —"):]
    assert "4번 다시 조회" in note and "끝난 상태(done)" in note, note
    idx = a._sc_index(a.ChatRequest(message="ODB 체크리스트를 돌려 끝나면 결과 알려줘", groups=[]), box["outputs"], box["raw"])
    nums = g.num_tokens(note)
    assert nums and all(idx.has(n, strong=True) for n in nums), [(n.text, idx.has(n, strong=True)) for n in nums]
    sc = [d for e, d in evs if e == "meta"][-1]["self_check"]
    assert sc["status"] == "ok" and len(sc.get("dropped") or []) == 1 and "4" in sc["dropped"][0], sc
    assert sc["gate"]["drop_sourced_number"] == 1 and sc["gate"]["applied"] == 0
    assert _result(evs).startswith(DRAFT5), "코드가 쓴 기다림 안내의 수는 근거다 — 검토자가 지우지 못한다"


# ── I-6 자기검증 켬 · 카드 턴(HA-27 × HA-29 · C-14) ──────────────────────────────────────────────────────────────────────

def test_I6_자기검증을_켜도_카드_턴은_검토하지_않고_그릇에_원문과_카드가_함께_근거_블록은_작업량을_짚지_않는다(monkeypatch):
    _gate_on(monkeypatch)
    rv = _Reviewer("[판정] 고칠 것 없음\n[고친 점]\n- 없음\n")
    _self_check_on(monkeypatch, rv)
    _cache(monkeypatch, {"run_job": "heax-step_forge", "list_projects": "heax-step_forge"}, labels={"heax-step_forge": "StepForge"})
    lp, lp_hits = _fake("list_projects", _seq("P1 판 · 파트 12개"), {})
    rj, rj_hits = _fake("run_job", _seq({"job_id": "J-1", "status": "pending"}), {"project_id": {"type": "string"}, "kind": {"type": "string"}})
    llm = _Script(steps=[[("list_projects", {}), ("run_job", P1_GRAPH)], "대본 끝 — 여기까지 오면 안 된다"])
    agent = _agent(monkeypatch, llm, tools=(lp, rj))
    reached: list = []
    boxes = _spy_box(monkeypatch)
    evs, tbn = _turn(monkeypatch, agent, _sf_tools(reached, EST), "이 판의 낙하에 취약한 부품을 알려줘")
    assert lp_hits == [{}] and rj_hits == []
    cards = _cards(evs)
    assert len(cards) == 1 and "약 1,812초" in cards[0]["calls"][0]["workload"], cards
    assert ("heaxstep_forge_estimate_cost", {"project_id": "P1", "job": "graph", "args": {}}) in reached
    assert tbn == [{"user": USER, "svc_fallback": True}], "작업량 묻기(읽기)는 기본 자격"
    assert rv.calls == [], "카드 턴은 검토자를 부르지 않는다(카드 글 · 번호를 고치지 못하게)"
    sc = [d for e, d in evs if e == "meta"][-1]["self_check"]
    assert sc["status"] == "skipped" and "확인 카드" in sc["reason"], sc
    box = boxes[0]
    # 그릇의 둘째 글은 카드가 물은 estimate_cost 답이다(서버의 작업량 묻기도 같은 래퍼를 지난다) — 카드 턴은 검토하지 않아 쓰이지 않는다
    assert box["raw"]["texts"][0] == "P1 판 · 파트 12개", "나란한 읽기의 원문을 붙잡았다(막은 잡은 부르지 않았다)"
    assert box["confirm"] and cards[0]["id"] in box["confirm"] and box["draft"].endswith(box["confirm"]), \
        "그릇에 원문과 카드 글이 함께 실린다 — 감싸개가 카드 턴을 가리고 근거 블록이 카드 글을 뗀다"
    assert cards[0]["id"] not in _text(evs, "reasoning"), "카드 글은 '생각' 칸으로 흘리지 않는다"
    res = _result(evs)
    assert res.count(cards[0]["id"]) == 1 and a._EVIDENCE_MARK in res and "`list_projects`" in res.split(a._EVIDENCE_MARK)[1]
    ev = res.split(a._EVIDENCE_MARK)[1].replace(",", "")
    assert all(v not in ev for v in ("1812", "1500", "2400")), "카드의 예상 작업량은 '출처 없는 수' 가 아니다(C-14)"


# ── I-7 띵킹 켬 · 띵킹 권한 없음 · 단추 글(HA-29 × HA-31 · C-10) ───────────────────────────────────────────────────────────

def test_I7_띵킹_권한이_없어도_카드의_단추_글은_권한_판정이_막지_않고_일반_챗이_승인_거절을_처리한다(monkeypatch):
    _gate_on(monkeypatch)
    reached: list = []
    tools = {"run_job": _gw("run_job", reached)}
    _evs, card, hist = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    thought: list = []

    async def fake_thinking(*_a, **_k):
        thought.append(1)
        yield a._sse("done", {})
    monkeypatch.setattr(a, "run_thinking", fake_thinking)
    flags = {"thinking": True, "entitlements": ["feat:chat"]}
    assert a._denied_feature(a.ChatRequest(message="다른 질문", history=hist, **flags)) == "feat:thinking", \
        "전제 — 카드의 답이 아닌 말은 띵킹 권한에 막힌다"
    assert a._denied_feature(a.ChatRequest(message=card["approve_text"], history=hist, **flags)) is None
    evs, tbn = _turn(monkeypatch, _GateAgent(tools, steps=[], say="잡 J-1 을 걸었습니다."), tools, card["approve_text"], hist,
                     via_chat=True, **flags)
    assert "권한이 없습니다" not in _text(evs, "token") and thought == [], "chat() 의 판정과 같다 — 막지도 띵킹으로 보내지도 않는다"
    assert reached == [("run_job", P1_GRAPH)] and tbn == [{"user": USER, "svc_fallback": False}]
    assert any(s.startswith(f"확인 카드 {card['id']} 승인") for s in _steps(evs))
    # 거절 단추도 같은 판정이다 — 띵킹 권한 없이도 일반 챗이 그 카드를 지운다
    _evs2, card2, hist2 = _card_turn(monkeypatch, tools, [("run_job", P1_GRAPH)])
    evs, _ = _turn(monkeypatch, _GateAgent(tools, steps=[], say="보내지 않았습니다."), tools, card2["decline_text"], hist2,
                   via_chat=True, **flags)
    assert "권한이 없습니다" not in _text(evs, "token") and thought == [] and reached == [("run_job", P1_GRAPH)]
    assert any(s.startswith(f"확인 카드 {card2['id']} 거절") for s in _steps(evs)) and card2["id"] not in a._CONFIRM_PENDING
    # 카드의 답이 아니면 종전대로 권한 판정이 막는다
    evs, _ = _turn(monkeypatch, _GateAgent(tools, steps=[], say="네."), tools, "다른 질문", hist, via_chat=True, **flags)
    assert "'Thinking' 을(를) 쓸 권한이 없습니다" in _text(evs, "token") and thought == []


# ── I-8 /health 한 번(합친 판 — 포털 update-all §7 이 읽는 칸) ────────────────────────────────────────────────────────────

def test_I8_health_한_번에_네_관문의_칸과_허브_접기가_함께_실리고_한_세션의_일이_모두_보인다(monkeypatch, tmp_path, client):
    _gate_on(monkeypatch, tmp_path, HUB_TEAM)
    _wait_on(monkeypatch)
    _xhub_on(monkeypatch)
    rv = _Reviewer("[판정] 고침\n[고친 점]\n- 321.5 → 312.5\n[바꿀 곳]\n[원문]\n최대 응력은 321.5 MPa\n[새 글]\n최대 응력은 312.5 MPa")
    _self_check_on(monkeypatch, rv)
    _cache(monkeypatch, MAP, labels=LABELS)
    # HA-34 접기 · HA-30 막이 — 큰 허브 결과를 접고(모델이 본 노드의 id 를 배운다) 그 id 를 다른 허브에 실은 호출을 막는다
    nodes = [{"id": f"{i:08x}-1111-4222-8333-{i:012x}", "key": f"N-{i}", "label": "노드 " + "x" * 200, "type_slug": "part"}
             for i in range(400)]
    big = json.dumps({"focus": U_RD, "nodes": nodes, "edges": []}, ensure_ascii=False)
    tok = a._turn_hub_ids.set({})
    try:
        seen_big = asyncio.run(_tool_of("rootdesigndatahub_graph_neighbors", big).ainvoke({"object_id": U_RD}))
        blocked = asyncio.run(_tool_of("failuretrace_graph_neighbors", FT_GRAPH).ainvoke({"object_id": nodes[3]["id"]}))
    finally:
        a._turn_hub_ids.reset(tok)
    assert "[포털 챗이 허브 응답을 접었다" in seen_big and a._XHUB_ID_MARK in blocked
    # HA-28 — 이 턴에 건 잡의 상태 조회를 끝까지 기다린다
    st_tool, _ = _fake("get_task", _seq(ODB_RUN(0.3), ODB_DONE))
    with _wait_turn(born={"t1"}):
        asyncio.run(a._prep_tool(st_tool, arg_defaults=a._CHAT_ARG_DEFAULTS, chat=True).ainvoke({"task_id": "t1"}))
    # HA-29 — 카드 턴 하나(자기검증이 켜져 있어 감싸개가 skipped 로 센다)
    _card_turn(monkeypatch, {"run_job": _gw("run_job", [])}, [("run_job", P1_GRAPH)])
    # HA-27 — 관문이 돈 검증 턴 하나(원문 사본과 대조해 넣음)
    _sc_run(monkeypatch, _inner("A 부품의 최대 응력은 321.5 MPa 이고 안전율은 1.8 입니다. B 부품은 여유가 큽니다.",
                                calls=[("query_stress", "part=A")], outputs=["max 312.5 MPa, SF 1.8"]), rv)

    h = client.get("/health").json()
    order = ["self_check", "status_wait", "confirm_gate", "xhub_gate", "core_vocab", "tool_cut", "hub_fold"]
    assert [k for k in h if k in order] == order, "design-integration 5절의 차례 — 같은 줄에 둘이 붙지 않는다"
    sc = h["self_check"]
    assert {"on", "counts", "ms", "last_error", "fixed_ratio", "gate"} <= set(sc) and sc["on"] is True
    assert sc["counts"] == {"skipped": 1, "fixed": 1} and sc["fixed_ratio"] == 1.0, sc
    assert set(sc["gate"]) == {"mode", "turns", "reviewer_fixed", "raw_partial", "counts"} and sc["gate"]["mode"] == "1"
    assert set(sc["gate"]["counts"]) == {"applied", *g.CODES, "reviewer_flags"}
    assert (sc["gate"]["turns"], sc["gate"]["reviewer_fixed"], sc["gate"]["counts"]["applied"]) == (1, 1, 1), sc["gate"]
    assert h["status_wait"] == {"on": True, "poll_s": 0.02, "max_s": 5.0, "waits": 1, "ended": 1, "capped": 0, "failed": 0}
    cg = h["confirm_gate"]
    assert set(cg) == {"on", "lists", "pending", "counts"} and cg["on"] is True and cg["pending"] == 1
    assert cg["lists"] == {"loaded": True, "error": None, "jobs": len(a._CONFIRM_JOB_TOOLS), "writes": 4, "confirm": 4}, \
        ("접두 별칭 둘씩과 그 접두를 뗀 꼴", cg["lists"])
    assert cg["counts"] == {"cards": 1, "held_calls": 1, "approved": 0, "executed": 0, "declined": 0, "unknown_reply": 0, "ordered": 0}
    assert h["xhub_gate"] == {"on": True, "blocked": 1}
    assert set(h["hub_fold"]) == {"count", "chars_in", "chars_out"} and h["hub_fold"]["count"] == 1
    assert h["hub_fold"]["chars_in"] == len(big) > h["hub_fold"]["chars_out"]
