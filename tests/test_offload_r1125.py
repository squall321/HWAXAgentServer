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
