# 심의가 게이트웨이 도구 목록을 받을 때 챗처럼 다시 묻는지 — 게이트웨이 재기동과 겹친 1~2초에 심의가 죽지 않는다
#
# 챗은 도구 로드가 일시 실패하면 0.5초·1초 쉬고 다시 물었는데(app._get_tools_retry) 심의는 한 번에 끝냈다.
# 심의는 대기열에서 수십 분 줄을 서기도 한다 — 차례가 온 그 순간이 게이트웨이 재기동과 겹치면 시작하자마자
# 죽었고, 오류는 '심의 처리 중 오류' 였다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_tool_load_retry.py -q
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402

from test_delib_silent_drops import _pin_context  # noqa: E402, F401


@tool
async def agent_search(agent_type: str = "", q: str = "", mode: str = "") -> str:
    """지식카드 조회."""
    return '{"hits": []}'


class _Gateway:
    """MultiServerMCPClient 대역 — 정해 둔 순서대로 실패하거나 도구를 준다. 무슨 자격으로 물었는지 적는다."""

    script: list = []       # 호출마다 하나씩 꺼낸다 — 예외면 올리고, 목록이면 돌려준다
    asked: list = []

    def __init__(self, connections):
        self.auth = connections["gateway"]["headers"].get("Authorization", "")

    async def get_tools(self):
        type(self).asked.append(self.auth)
        step = type(self).script.pop(0) if type(self).script else [agent_search]
        if isinstance(step, Exception):
            raise step
        return step


def _setup(monkeypatch, script):
    _Gateway.script, _Gateway.asked = list(script), []
    monkeypatch.setattr(a, "MultiServerMCPClient", _Gateway)
    slept = []

    async def _sleep(sec):          # 재시도 사이의 대기 — 실제로 자지 않고 얼마를 쉬려 했는지만 적는다
        slept.append(sec)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    conns = {"gateway": {"url": "http://gw/mcp", "transport": "streamable_http",
                         "headers": {"Authorization": "Bearer svc"}}}
    return SimpleNamespace(state=SimpleNamespace(connections=conns, llm=object(), delib_llm=None)), slept


def _down():
    return httpx.ConnectError("All connection attempts failed")


def _load(stub, **kw):
    async def go():
        return await d._tools_by_name(stub, ["g"], **kw)

    return asyncio.run(go())


def test_일시_실패는_쉬었다가_다시_물어_받는다(monkeypatch):
    stub, slept = _setup(monkeypatch, [_down(), _down()])
    tools = _load(stub)
    assert set(tools) == {"agent_search"}, "게이트웨이가 돌아왔는데 도구를 못 받았다"
    assert len(_Gateway.asked) == 3 and slept == [0.5, 1.0], (_Gateway.asked, slept)


def test_빈_목록도_다시_묻는다(monkeypatch):
    """게이트웨이가 막 뜬 직후에는 도구가 0개다 — 그것을 답으로 받으면 '도구를 불러오지 못했습니다' 로 죽는다."""
    stub, _slept = _setup(monkeypatch, [[], []])
    assert set(_load(stub)) == {"agent_search"} and len(_Gateway.asked) == 3


def test_자격_거절은_다시_묻지_않고_곧바로_서비스_계정으로_간다(monkeypatch):
    """401 은 다시 물어도 같다 — 쉬지 않고 종전의 서비스 계정 폴백으로 넘어간다."""
    req = httpx.Request("POST", "http://gw/mcp")
    denied = httpx.HTTPStatusError("Client error '401 Unauthorized'", request=req,
                                   response=httpx.Response(401, request=req))
    stub, slept = _setup(monkeypatch, [denied])
    tools = _load(stub, user="u@x.com", user_pat="user-pat")
    assert set(tools) == {"agent_search"}
    assert _Gateway.asked == ["Bearer user-pat", "Bearer svc"] and slept == [], (_Gateway.asked, slept)


def _stream(stub):
    async def go():
        return [delib_jobs._parse_sse(c) async for c in d.run_deliberation(
            stub, "힌지 크랙 원인", ["g"], {"personas": [{"key": "mech-a"}, {"key": "rel-b"}]})]

    return asyncio.run(go())


def test_끝내_못_받으면_몇_번_물었는지와_무엇이었는지_말한다(monkeypatch):
    stub, slept = _setup(monkeypatch, [_down()] * 3)
    events = _stream(stub)
    (err,) = [data for ev, data in events if ev == "error"]
    assert err["code"] == "gateway_unavailable", f"종전처럼 뭉뚱그린 오류로 끝났다 — {err}"
    assert "3회 시도" in err["message"] and "ConnectError" in err["message"], err["message"]
    assert len(_Gateway.asked) == 3 and slept == [0.5, 1.0]
    assert events[-1][0] == "done"


def test_끝까지_빈_목록이면_도구_0개라고_말한다(monkeypatch):
    stub, _slept = _setup(monkeypatch, [[], [], []])
    (err,) = [data for ev, data in _stream(stub) if ev == "error"]
    assert err["code"] == "gateway_unavailable" and "3회 시도 · 도구 0개" in err["message"], err


def test_연결_설정이_없으면_묻지도_않았다고_말한다(monkeypatch):
    stub, _slept = _setup(monkeypatch, [])
    stub.state.connections = {}
    (err,) = [data for ev, data in _stream(stub) if ev == "error"]
    assert "MCP 연결 설정이 없다" in err["message"] and "회 시도" not in err["message"], err
    assert _Gateway.asked == []
