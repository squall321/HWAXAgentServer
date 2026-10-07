# 시험 설계(test-plan)가 호출자가 넣은 사람 의견과 지정 좌석을 버리지 않는지 — 좌석이 받은 프롬프트로 본다
#
# run_test_plan 은 사람 의견 칸을 엔진 지시문 + 물성 현황으로 **덮어쓰고** 좌석을 고정 다섯으로 갈아 끼웠다.
# 호출자의 의견은 어느 좌석 프롬프트에도 없었고 지정한 좌석은 앉지 않았는데, 카드도 경고도 없었고 잡 기록은
# 넘긴 의견과 좌석 수를 '걸린 값' 으로 적었다. 이어하기에서는 그 의견이 부르는 까닭 전부다.
# 정본 파이프라인(HWAXPortal infra/pipeline/hwax-test-plan.js)은 둘 다 싣는다 — 고정 좌석 뒤에 지정 좌석을,
# 엔진 지시문 뒤에 사람 의견을. 파이썬 엔진만 어긋나 있었다.
#
# **끝까지 돌려서 좌석 프롬프트를 센다.** 옵션에 값이 들어 있는지만 보면 '값은 있는데 프롬프트에 안 간다' 를 못 잡는다.
#
#   실행:  .venv/bin/python -m pytest tests/test_test_plan_caller_inputs.py -q
import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_silent_drops import _cards, _pin_context, _Tool  # noqa: E402, F401

_MARK = "QZX-CALLER-NOTE-7731"          # 호출자 의견에만 있는 표식
_PICKED = "mech-caller-picked"          # 호출자가 지정한 좌석 키
_SEAT_SYS = re.compile(r"당신은 '([^']+)' 전문가")


def _drive(monkeypatch, entry, req_opts):
    """진입 함수를 done 까지 실제로 돌린다. 반환 (이벤트, 좌석 프롬프트 [(좌석 키, 본문)])."""
    seat_prompts = []

    async def _llm(_obj, system, human):
        hit = _SEAT_SYS.search(system)
        if not hit:
            return "결정문 본문"
        seat_prompts.append((hit.group(1), human))
        return json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["A", "B"],
                           "position_short": "요약", "concede": [], "rebut": ["반박"], "deepen": "심화",
                           "final_position": "최종", "non_negotiable": "", "vote": "진행",
                           "stance": "동의"}, ensure_ascii=False)

    async def _tools(*_a, **_k):
        return {"agent_search": _Tool("agent_search")}

    async def _snapshot(*_a, **_k):
        return "[물성 근거 현황] 조회 결과 본문"

    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "_tools_by_name", _tools)
    monkeypatch.setattr(d, "_material_evidence_snapshot", _snapshot)
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None))
    opts = {"free_tools": 0, "voc": "off", "rescreen": 0, "save_report": 0, "persona_knowledge": 0,
            "rebut_quote": 0, "rounds": 2, **req_opts}

    async def go():
        return [delib_jobs._parse_sse(c) async for c in entry(stub, "접착층 박리 강도", [], opts)]

    events = asyncio.run(go())
    assert not [data for ev, data in events if ev == "error"], [data for ev, data in events if ev == "error"]
    assert seat_prompts, "좌석이 한 번도 발언하지 않았다 — 하네스가 낡았다"
    return events, seat_prompts


def _seated(events):
    return [p["key"] for p in next(data["personas"] for ev, data in events
                                   if ev == "delib" and data.get("kind") == "personas")]


_REQ = {"human_note": f"현장 관측 {_MARK}: 박리는 모서리에서 시작한다",
        "personas": [{"key": "rel-test-measurement", "role": ""}, {"key": _PICKED, "role": "호출자 지정"}]}


def test_자유_심의는_의견이_좌석마다_실린다(monkeypatch):
    """대조군 — 이게 안 서면 아래 시험이 하네스 탓으로 통과·실패한다."""
    _events, prompts = _drive(monkeypatch, d.run_deliberation, dict(_REQ))
    assert all(_MARK in h for _k, h in prompts)


def test_시험_설계도_호출자_의견이_좌석마다_실린다(monkeypatch):
    events, prompts = _drive(monkeypatch, d.run_test_plan, dict(_REQ))
    missing = sorted({k for k, h in prompts if _MARK not in h})
    assert not missing, f"호출자 의견을 못 받은 좌석 — {missing} ({len(prompts)}개 프롬프트)"
    assert all("이미 실측이 있는 항목" in h and "[물성 근거 현황]" in h for _k, h in prompts), (
        "엔진 지시문·물성 현황이 빠졌다 — 호출자 의견이 그것을 밀어냈다")
    shown = next(c for c in _cards(events, included=True) if c["source"] == "인간 검토자 의견")
    assert _MARK in shown["text"], "화면의 사람 의견 카드에 호출자 의견이 없다"


def test_시험_설계는_고정_좌석_뒤에_지정_좌석을_앉힌다(monkeypatch):
    events, prompts = _drive(monkeypatch, d.run_test_plan, dict(_REQ))
    assert _seated(events) == list(d._TEST_FIXED) + [_PICKED], _seated(events)
    assert _PICKED in {k for k, _h in prompts}, "지정한 좌석이 발언하지 않았다"


def test_이어하기가_만드는_옵션_모양으로도_실린다(monkeypatch):
    """deliberate_continue 는 사람 의견이 부르는 까닭 전부다 — 이전 좌석도 그대로 앉히겠다고 한다."""
    o = m._build_opts(human_note=f"이어하기 관측 {_MARK}: 2차 시험에서 온도 의존성이 보였다",
                      continue_summary="이전 결정문 요약", rounds_so_far=3,
                      personas=[{"key": k, "role": ""} for k in d._TEST_FIXED]
                               + [{"key": _PICKED, "role": "이전 회차 좌석"}])
    events, prompts = _drive(monkeypatch, d.run_test_plan, o)
    assert all(_MARK in h for _k, h in prompts), sorted({k for k, h in prompts if _MARK not in h})
    assert _seated(events) == list(d._TEST_FIXED) + [_PICKED]


def test_의견을_안_넣으면_종전_그대로다(monkeypatch):
    events, prompts = _drive(monkeypatch, d.run_test_plan, {})
    assert _seated(events) == list(d._TEST_FIXED)
    assert not any("호출자가 넣은 사람 의견" in h for _k, h in prompts), "없는 의견의 머리말을 실었다"
