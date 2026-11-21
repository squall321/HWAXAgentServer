# 게이트웨이 절차 도구(procedure_*)의 관문 — 웹 챗에서는 실행 시작이 확인 카드로 멈추고, 챗 턴 밖(심의 · 띵킹)에서는 시작하지 않는다
#
# 게이트웨이가 포털 절차를 범용 도구 넷으로 낸다(HWAXPortal docs/procedures W-116 · 사용자 결정 11-22 — procedure_list · procedure_get ·
# procedure_run_start · procedure_run_status). 실행 시작은 그 사람 이름으로 도구 연쇄를 돌린다.
#   A. 웹 챗 — 실행 시작은 잡 시작 바닥 목록(_CONFIRM_JOB_TOOLS)이다. 사람이 이번 말로 시키지 않았으면 보내지 않고 확인 카드다(직접 · 별칭 ·
#      invoke_tool 의 안쪽 모두). 읽기 셋(목록 · 계약 · 상태)은 카드 없이 간다.
#   B. 챗 턴 밖 — 심의의 지정 도구 · 좌석 자유 조회 · 외부 응답자의 CALL 줄 · 띵킹에는 사람의 확인 카드가 서지 않는다. 래퍼(G2c)가 실행 시작을
#      보내지 않는다(REST 다리 쓰기의 G2b 와 같은 자리). 읽기 셋은 그대로 간다.
#   C. 좌석 — 이름이 엔진의 자유 조회 접두(_FREE_ALLOW)에 걸리지 않아 심의 · 리스크 심사 좌석의 후보에 들지 않는다.
#
#   실행:  .venv/bin/python -m pytest tests/test_procedure_tools_gate.py -q
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import app as a  # noqa: E402
import deliberation as d  # noqa: E402
from test_confirm_gate import _gw, _held, _run  # noqa: E402
from test_confirm_gate import gate, maps, turn_ctx  # noqa: E402, F401 — 픽스처

START = {"procedure_id": "a" * 32, "inputs": {"odb_job": "J1"}, "mode": "live"}
READS = (("procedure_list", {}), ("procedure_get", {"procedure_id": "a" * 32}), ("procedure_run_status", {"run_id": "r" * 32}))


# ── A. 웹 챗 ─────────────────────────────────────────────────────────────────────────────────────────────

def test_실행_시작은_잡_시작_바닥_목록이고_읽기_셋은_목록_밖이다(gate):
    assert a._confirm_class("procedure_run_start", START) == ("job", "procedure_run_start")
    assert a._confirm_class("gateway_procedure_run_start") == ("job", "procedure_run_start"), "별칭 · 충돌 접두 꼴도 같다"
    for name, args in READS:
        assert a._confirm_class(name, args) == (None, ""), name


@pytest.mark.parametrize("form", ["direct", "invoke_dict", "invoke_json"])
@pytest.mark.parametrize("mode", ["live", "plan", None])
def test_사람이_이번_말로_시키지_않은_실행_시작은_보내지_않고_확인_카드다(turn_ctx, form, mode):
    """계획 모드도 같다 — 관문은 이름으로 서고(인자로 가르면 모드를 못 읽은 호출이 샌다), 계획 실행도 그 사람의 실행 원장에 쓴다."""
    ctx = turn_ctx("열충격 절차가 뭐가 있는지 알려줘")
    args = {k: v for k, v in START.items() if k != "mode"} if mode is None else {**START, "mode": mode}
    reached = []
    if form == "direct":
        out = _run(_gw("procedure_run_start", reached), args)
    else:
        out = _run(_gw("invoke_tool", reached), {"name": "procedure_run_start", "arguments": json.dumps(args) if form == "invoke_json" else args})
    assert _held(out) and reached == [], (form, mode, out)
    assert [c["kind"] for c in ctx.pending] == ["job"] and ctx.pending[0]["base"] == "procedure_run_start"


def test_도구를_콕_집어_돌리라고_한_말은_한_번_보낸다(turn_ctx):
    turn_ctx("procedure_run_start 돌려줘")
    reached = []
    assert _run(_gw("procedure_run_start", reached), START) == "gateway called procedure_run_start"
    assert _held(_run(_gw("procedure_run_start", reached), START)), "같은 호출이 이 턴에 다시 오면 카드다"
    assert reached == [("procedure_run_start", START)]


def test_읽기_셋은_챗에서_카드_없이_간다(turn_ctx):
    turn_ctx("내 절차 목록 보여줘")
    reached = []
    for name, args in READS:
        assert _run(_gw(name, reached), args) == f"gateway called {name}"
    assert [n for n, _ in reached] == [n for n, _ in READS]


# ── B. 챗 턴 밖 ──────────────────────────────────────────────────────────────────────────────────────────

def _g2c(out) -> bool:
    return isinstance(out, str) and out.startswith(a._TOOL_FAIL_MARK) and "절차 실행 시작(procedure_run_start)" in out


@pytest.mark.parametrize("knob", [False, True])
def test_G2c_챗_턴_밖에서는_절차_실행을_시작하지_않는다(monkeypatch, knob):
    """심의 지정 도구 · 좌석 자유 조회 · 외부 응답자의 CALL 줄 · 띵킹 · 카드 대조는 챗 턴 맥락이 없어 확인 카드(G5)가 서지 않는다 — 챗 관문
    손잡이와 상관없이 막는다(닫힌 쪽). 별칭 · invoke_tool 의 안쪽(dict · JSON 글)도, 계획 모드도 같다. 읽기 셋은 그대로 간다."""
    monkeypatch.setattr(a, "CHAT_CONFIRM_GATE", knob)
    assert a._turn_confirm.get() is None
    reached = []
    plan = {"procedure_id": "a" * 32}
    for out in (_run(_gw("procedure_run_start", reached), START), _run(_gw("procedure_run_start", reached), plan),
                _run(_gw("gateway_procedure_run_start", reached), START),
                _run(_gw("invoke_tool", reached), {"name": "procedure_run_start", "arguments": START}),
                _run(_gw("invoke_tool", reached), {"name": "procedure_run_start", "arguments": json.dumps(START)})):
        assert _g2c(out) and a._result_ok(out) is False, out
    assert reached == []
    for name, args in READS:
        assert _run(_gw(name, reached), args) == f"gateway called {name}"
    assert _run(_gw("invoke_tool", reached), {"name": "procedure_run_status", "arguments": {"run_id": "r" * 32}}) == \
        "gateway called procedure_run_status"
    assert len(reached) == 4


def test_G2c_외부_응답자의_CALL_줄도_같은_래퍼에서_막힌다():
    reached = []
    ag = d._TextReactAgent(None, [_gw("procedure_run_start", reached)], 2)
    assert _g2c(asyncio.run(ag._run("procedure_run_start", START))) and reached == []


# ── C. 좌석 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_절차_도구는_좌석_자유_조회_후보에_들지_않는다():
    for name in ("procedure_list", "procedure_get", "procedure_run_start", "procedure_run_status"):
        assert d._free_tool_ok(name) is False, f"{name} — 심의 · 리스크 심사 좌석의 자유 조회에 저절로 열린다"
        assert not name.startswith(d._FREE_ALLOW)
    assert d._risk_designated_refusal("procedure_run_start") == "", "리스크 심사의 지정 도구 거절 갈래가 아니다 — 챗 턴 밖 관문(G2c)이 막는다"
