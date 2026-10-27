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


# ── HA-08 — 위키가 꺼진 박스에서는 부르지 않고 한 줄만, 다단 심의는 한 번만 ─────────────────────────────────────
import deliberation as d  # noqa: E402

_KW = dict(question="q", decision="결정문", stage="default", personas=[{"key": "mech-a", "role": "기구"}], last_list=[],
           rid=None, evidence=[], pool=[])


class _WikiTool:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self.answer


def _two_stages(tools, bind=True):
    async def go():
        if bind:
            d._wiki_bind("CAEG", "job-1")
        first = await d._wiki_submit(tools, user="u1@corp.example", chair_failed=None, **_KW)
        second = await d._wiki_submit(tools, user="u1@corp.example", chair_failed=None, **{**_KW, "stage": "s2"})
        return first, second
    return asyncio.run(go())


def test_HA08_위키_도구가_없으면_부르지_않고_한_줄_다음_단은_조용하다():
    """MX 백서는 위키가 꺼지면 wiki_* 를 싣지 않는다(MW-01) — 종전엔 결정문 끝마다(다단이면 단마다) '⚠ 남기지 못했다 …' 가 붙었다."""
    first, second = _two_stages({})
    assert first == d._WIKI_OFF_NOTE and "꺼져 있거나" in first and "⚠" not in first
    assert second == "", "다단 심의의 다음 단은 같은 줄을 다시 붙이지 않는다"
    assert _two_stages({})[0] == d._WIKI_OFF_NOTE, "새 심의는 새로 본다(상태는 심의 하나의 것이다)"


def test_HA08_도구가_실려도_wiki_disabled_면_꺼짐으로_읽고_다시_부르지_않는다():
    """API 가 늦게 떠 mxwp-mcp 가 위키 도구를 실은 채 위키가 꺼진 경우 — 부른 결과가 wiki_disabled 다."""
    t = _WikiTool("(tool wiki_submit_source error: 404 wiki_disabled — MX 백서 AI 위키는 아직 열리지 않았다)")
    first, second = _two_stages({d._WIKI_TOOL: t})
    assert first == d._WIKI_OFF_NOTE and second == "" and len(t.calls) == 1


def test_HA08_켜진_위키의_다른_실패와_성공은_단마다_그대로다():
    bad = _WikiTool("(tool wiki_submit_source error: 보낸 사람이 갖지 않은 범위 토큰이다)")
    first, second = _two_stages({d._WIKI_TOOL: bad})
    assert "⚠ MX 백서 위키에 남기지 못했다" in first and "범위 토큰" in second and len(bad.calls) == 2, "꺼짐이 아닌 실패는 단마다 알린다"
    ok = _WikiTool('{"id": "4f1e", "short": "src_4f1e", "dedup": false, "ingest_state": "queued"}')
    first, second = _two_stages({d._WIKI_TOOL: ok})
    assert "src_4f1e" in first and "src_4f1e" in second and len(ok.calls) == 2, "원천은 단마다 남긴다(P8)"
