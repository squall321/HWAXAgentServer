# 운영 요청 모음 11-24(HWAXPortal docs/request-1125 1-3 · 2절 A-1) — 위임 · 초안의 고침 OF-1 ~ OF-5 시험(가짜는 test_offload 의 것을 쓴다)
#
#   실행:  .venv/bin/python -m pytest tests/test_offload_r1125.py -q
import asyncio
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_server as m  # noqa: E402

from test_offload import _ai, _call, _ctx, _done, _run, kit  # noqa: E402, F401


# ── OF-5 위임 상태의 기본 기다림 ────────────────────────────────────────────────────────────────────────────────
def test_OF5_위임_상태는_기본으로_기다리고_설명에_줄이_길면_직접_읽으라고_적는다():
    sig = inspect.signature(m.delegate_status)
    assert sig.parameters["wait_s"].default == m.DELEGATE_WAIT_DEFAULT_S == 240.0, "기본 0 이면 첫 호출부터 기다리지 않는다(OF-5)"
    assert m.DELEGATE_WAIT_DEFAULT_S < 600, "게이트웨이 호출 한도 600초 안쪽이어야 한다"
    for desc in (m._DG_STATUS_DESC, m._DG_START_DESC):
        assert "직접 읽는다" in desc and "delegate_cancel" in desc


def test_OF5_기본값은_서버_상한까지만_말없이_죄고_준_값은_그대로_간다(kit, monkeypatch):
    asked: list = []

    async def _sw(job_id, job, wait_s):
        asked.append(wait_s)
        return job, {"wait_s": wait_s, "waited_s": 0.0, "woke": "ended"}
    monkeypatch.setattr(m, "_status_wait", _sw)
    monkeypatch.setattr(m, "STATUS_WAIT_MAX_S", 90.0)
    kit.llm.script = [_ai("끝났다.")]

    async def scenario():
        out = await m.delegate_start(task="S26 VOC 불량 수를 알아내라", ctx=_ctx())
        await _done(out["job_id"])
        a = await m.delegate_status(out["job_id"], ctx=_ctx())
        b = await m.delegate_status(out["job_id"], wait_s=30, ctx=_ctx())
        c = await m.delegate_status(out["job_id"], wait_s=0, ctx=_ctx())
        return out, a, b, c
    out, a, b, c = _run(scenario)
    assert asked == [90.0, 30], "기본값은 상한 90 으로, 준 30 은 그대로, 0 은 기다리지 않는다"
    assert a["wait"]["wait_s"] == 90.0 and "wait" not in c
    assert "기본으로 최대 240초" in out["note"]


# ── OF-4 초안 권고는 긴 절에만 ─────────────────────────────────────────────────────────────────────────────────
def test_OF4_초안_설명은_긴_절에만_쓰라고_첫머리에_적는다():
    head = m._DR_DESC[:160]
    assert "긴 절" in head and "직접 쓴다" in head and f"{m.offload.DRAFT_TARGET_DEFAULT:,}자" in head
    assert "통째로 다시 쓰지 말고" in m._DR_DESC


# ── OF-2 멈춘 까닭 · 덜 찾음 ─────────────────────────────────────────────────────────────────────────────────────
from test_offload import MAP, RR, TOOLS, _tool  # noqa: E402

import offload  # noqa: E402


def _kit_tools(kit, extra: dict):
    tools = {**TOOLS, **extra}

    async def _pt(_app, groups, user):
        return dict(tools)

    async def _rm(_app):
        return {**MAP, **{n: "signalforge" for n in extra}}, frozenset(RR | set(extra))
    kit.mp.setattr(offload, "_person_tools", _pt)
    kit.mp.setattr(offload, "_read_map", _rm)


def test_OF2_답에_멈춘_까닭_예산_incomplete_못_채운_것이_구조로_실린다(kit):
    kit.llm.script = [_ai("", _call("voc_search", q="1")), _ai("불량 1,234건 [e1].\n[못 채운 것] 없음")]

    async def scenario():
        out = await m.delegate_start(task="S26 불량 수", ctx=_ctx())
        await _done(out["job_id"])
        return await m.delegate_result(out["job_id"], ctx=_ctx())
    res = _run(scenario)
    assert res["incomplete"] is False and "unfilled" not in res
    st = res["stop"]
    assert st["reason"] == "answered" and st["calls"] == 1 and st["steps_budget"] == offload.STEPS_DEFAULT
    assert st["minutes_budget"] == offload.MINUTES_DEFAULT and st["elapsed_s"] >= 0


def test_OF2_호출_예산으로_멈추면_incomplete_와_못_채운_항목(kit):
    kit.llm.script = [_ai("", _call("voc_search", q="1")), _ai("", _call("voc_search", q="2")),
                      _ai("불량 1,234건 [e1].\n[못 채운 것] 원인 분류; 대책 이력")]

    async def scenario():
        out = await m.delegate_start(task="부품 × 증상 표를 채워라", max_steps=1, ctx=_ctx())
        await _done(out["job_id"])
        return await m.delegate_result(out["job_id"], view="summary", ctx=_ctx())
    res = _run(scenario)
    assert res["stop"]["reason"] == "steps" and res["stop"]["calls"] == 1 and res["incomplete"] is True
    assert res["unfilled"] == ["원인 분류", "대책 이력"]


def test_OF2_시간으로_멈추고_못_채운_것을_적지_않았으면_그렇다고_적는다(kit):
    gate = asyncio.Event()

    async def _stall(_msgs):
        await gate.wait()
        return _ai("오지 않는다")
    kit.mp.setattr(offload, "_MINUTE_S", 0.3)
    kit.llm.script = [_ai("", _call("voc_search", q="S26")), _stall, _ai("정리 — 불량 1,234건 [e1].")]

    async def scenario():
        out = await m.delegate_start(task="S26 불량", max_minutes=1, ctx=_ctx())
        await _done(out["job_id"])
        return await m.delegate_result(out["job_id"], ctx=_ctx())
    res = _run(scenario)
    assert res["stop"]["reason"] == "time" and res["incomplete"] is True
    assert len(res["unfilled"]) == 1 and "적지 않았다" in res["unfilled"][0]
    wrap = kit.llm.seen[-1]
    assert "[못 채운 것]" in wrap[0].content, "정리 턴도 못 채운 것을 적게 한다"


def test_OF2_적중이_적으면_다시_찾으라는_힌트를_LLM_에만_단다(kit):
    _kit_tools(kit, {"voc_find": _tool("voc_find", '{"total": 0, "results": []}')})
    kit.llm.script = [_ai("", _call("voc_find", q="S26 화면 깨짐")), _ai("없다.\n[못 채운 것] 화면 깨짐 건수")]

    async def scenario():
        out = await m.delegate_start(task="S26 화면 깨짐 건수", ctx=_ctx())
        await _done(out["job_id"])
        return await m.delegate_result(out["job_id"], ctx=_ctx())
    res = _run(scenario)
    seen_tool = [x for x in kit.llm.seen[-1] if getattr(x, "type", "") == "tool"]
    assert seen_tool and "[실행기 힌트] 적중이 0~1건" in str(seen_tool[0].content)
    assert res["few_hits"] == 1 and res["evidence"][0]["chars"] == len('{"total": 0, "results": []}'), "근거 표의 글자 수는 힌트 앞 원문"
    assert offload._few_hits("[]") and offload._few_hits('{"count": 1}') and not offload._few_hits('{"count": 5}')
    assert not offload._few_hits("결과 없음") and not offload._few_hits('{"results": [1, 2]}')


def test_OF2_설명에_일_크기별_권장과_incomplete_를_적는다():
    for d in (m._DG_START_DESC, m._A_DG_STEPS):
        assert "20~30" in d
    assert "incomplete" in m._DG_START_DESC and "unfilled" in m._DG_RESULT_DESC and "stop{reason" in m._DG_RESULT_DESC
    assert "다시 찾는다" in offload._DELEGATE_SYS and "[못 채운 것]" in offload._DELEGATE_SYS


# ── 2절 A-1 앞선 결과 줄여 싣기 ────────────────────────────────────────────────────────────────────────────────
def test_A1_앞선_결과는_LLM_입력에서만_줄이고_방금_받은_결과는_그대로(kit):
    long_a, long_b = "가" * 4000, "나" * 4000
    _kit_tools(kit, {"rep_a": _tool("rep_a", long_a), "rep_b": _tool("rep_b", long_b)})
    kit.mp.setattr(offload, "PRIOR_RESULT_CHARS", 1000)
    kit.llm.script = [_ai("", _call("rep_a", q="1")), _ai("", _call("rep_b", q="2")), _ai("끝.\n[못 채운 것] 없음")]

    async def scenario():
        out = await m.delegate_start(task="두 보고서를 읽어라", ctx=_ctx())
        await _done(out["job_id"])
        return await m.delegate_result(out["job_id"], ctx=_ctx())
    res = _run(scenario)
    last = [x for x in kit.llm.seen[-1] if getattr(x, "type", "") == "tool"]
    assert len(last) == 2
    assert last[0].content.startswith("[근거 e1] " + "가" * 900) and long_a not in last[0].content and "앞선 결과" in last[0].content
    assert len(last[0].content) < 1200
    assert long_b in last[1].content, "방금 받은 결과는 줄이지 않는다"
    second = [x for x in kit.llm.seen[1] if getattr(x, "type", "") == "tool"]
    assert long_a in second[0].content, "그 결과를 받은 바로 다음 차례에는 전문이 보인다"
    assert [e["chars"] for e in res["evidence"]] == [4000, 4000], "근거 표 · 숫자 대조는 원문 그대로"


def test_A1_0_이면_종전처럼_줄이지_않는다(kit):
    _kit_tools(kit, {"rep_a": _tool("rep_a", "가" * 4000)})
    kit.mp.setattr(offload, "PRIOR_RESULT_CHARS", 0)
    kit.llm.script = [_ai("", _call("rep_a", q="1")), _ai("", _call("voc_search", q="2")), _ai("끝.")]

    async def scenario():
        out = await m.delegate_start(task="읽어라", ctx=_ctx())
        await _done(out["job_id"])
    _run(scenario)
    last = [x for x in kit.llm.seen[-1] if getattr(x, "type", "") == "tool"]
    assert "가" * 4000 in last[0].content
