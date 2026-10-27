# 10-27 수정 요청(HWAXPortal docs/request-1027 03-HWAXAgentServer.md)의 에이전트 서버 항목 — 항목마다 고친 자리를 실제로 돌려 본다
"""항목 번호(HA-xx)는 요청서의 것이다. 판단의 까닭은 HWAXPortal docs/request-1027/context-notes.md 에 있다."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from langchain_core.tools import StructuredTool, tool  # noqa: E402

import app as a  # noqa: E402
import external_llm as x  # noqa: E402


# ── HA-05 — 패널을 그리지 못하는 챗 · 심의는 패널 전용 도구를 묶지도 부르지도 않는다 ─────────────────────────
def test_HA05_패널_전용_도구는_정확한_이름과_별칭만_막는다():
    for n in ("open_resin_panel", "simflow_open_resin_panel", "OPEN_WORKFLOW_PANEL", " panel_upload_commit ",
              "open_camfpcb_panel", "open_frc_panel", "open_gi_panel", "open_magnet_panel", "panel_upload_begin", "panel_upload_append"):
        assert a._agent_denied(n), n
    for n in ("risk_list_panels", "list_panels", "open_resin_panel_help", "prepare_run", "arp_upload_open", "panel"):
        assert not a._agent_denied(n), f"{n} 는 막을 이름이 아니다 — `*panel*` 무늬로 넓히면 조회 도구까지 걸린다"
    assert a._AGENT_DENY_TOOLS == frozenset(x.TASK_TOOLS), "외부 응답자 과제 도구 집합은 그대로다(따로 둔다)"


def test_HA05_invoke_tool_로도_패널_도구를_부르지_못하고_까닭을_말한다():
    calls = []

    @tool("invoke_tool")
    async def invoke(name: str, arguments: dict | None = None) -> str:
        """게이트웨이 invoke_tool 대역."""
        calls.append(name)
        return f"called {name}"

    t = a._prep_tool(invoke)

    async def go():
        return [await t.ainvoke({"name": n, "arguments": {}}) for n in ("open_resin_panel", "simflow_panel_upload_begin", "prepare_run")]

    out = asyncio.run(go())
    assert calls == ["prepare_run"], "막아야 할 이름이 게이트웨이까지 갔다"
    for o in out[:2]:
        assert o.startswith(a._TOOL_FAIL_MARK) and "패널" in o and "prepare_run" in o and "Claude Code" not in o, o
    assert out[2] == "called prepare_run"
