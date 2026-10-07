# 심의가 도는 중에 사용자 토큰이 만료돼도 조회와 보고서 저장을 잃지 않는지 — 서비스 계정으로 넘어가고 그렇다고 알린다
#
# 포털이 심의 요청에 실어 주는 사용자 토큰은 수명이 30~60분이었고, 엔진은 시작할 때 받은 것을 끝까지 썼다.
# 수 시간짜리 패널은 그 수명을 넘긴다 — 만료 뒤의 좌석 조회와 **마지막 보고서 저장**이 전부 실패했고, 사유는
# 'unhandled errors in a TaskGroup (1 sub-exception)' 한 줄이었다(진짜 원인인 401 은 그 안에 싸여 있었다).
# 시작할 때의 거절에는 서비스 계정 폴백이 있었는데 도는 중의 거절에는 없었다.
#
# **실제 MCP 서버를 띄워서** 본다 — 도는 중에 사용자 토큰만 401 로 거절하는 게이트웨이 대역이다.
#
#   실행:  .venv/bin/python -m pytest tests/test_cred_fallback.py -q
import asyncio
import json
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402
from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402

from test_delib_silent_drops import _pin_context  # noqa: E402, F401

_USER, _SVC = "Bearer user-pat", "Bearer svc-token"
_SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}]


class _Gateway:
    """게이트웨이 대역의 상태 — 무엇을 거절할지와, 도구가 불릴 때마다 어떤 자격·신원으로 왔는지."""

    def __init__(self):
        self.reject = None          # None | 401 | 403 — 사용자 토큰으로 온 요청에 줄 상태 코드
        self.expire_on = ""         # 이 도구가 불리고 나면 사용자 토큰이 만료된다
        self.calls = []             # [(도구, 자격, 신원 헤더)]
        self._now = ("", "")

    def reset(self):
        self.reject, self.expire_on, self.calls = None, "", []

    def called(self, tool):
        self.calls.append((tool, *self._now))
        if tool == self.expire_on:
            self.reject = 401


@pytest.fixture(scope="module")
def gw():
    state = _Gateway()
    mcp = FastMCP("hwax-cred-test", stateless_http=True, streamable_http_path="/")

    @mcp.tool()
    async def get_agent_session(agent_type: str = "") -> str:
        """역할 원문."""
        state.called("get_agent_session")
        return "{}"

    @mcp.tool()
    async def agent_search(agent_type: str = "", q: str = "", mode: str = "") -> str:
        """지식카드 조회."""
        state.called("agent_search")
        return json.dumps({"hits": [{"title": "카드", "snippet": "힌지 토크 실측", "record_id": "r1"}]},
                          ensure_ascii=False)

    @mcp.tool()
    async def create_report_draft(template_id: str = "", template_version: int = 1, title: str = "",
                                  blocks: dict | None = None, tags: list | None = None) -> str:
        """보고서 저장."""
        state.called("create_report_draft")
        return '{"report_id": 7}'

    inner = mcp.streamable_http_app()

    async def gate(scope, receive, send):
        if scope["type"] == "http":
            hdr = {k.decode(): v.decode() for k, v in scope["headers"]}
            state._now = (hdr.get("authorization", ""), hdr.get("x-hwax-user", ""))
            if state.reject and state._now[0] == _USER:
                body = {"error": "invalid_token" if state.reject == 401 else "forbidden"}
                await JSONResponse(body, status_code=state.reject)(scope, receive, send)
                return
        await inner(scope, receive, send)

    with socket.socket() as s:                  # 빈 포트 — 떠 있는 서비스와 겹치지 않게 커널이 고른다
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(gate, host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "시험용 MCP 서버가 뜨지 않았다"
    state.conns = {"gateway": {"url": f"http://127.0.0.1:{port}/", "transport": "streamable_http",
                               "headers": {"Authorization": _SVC}}}
    yield state
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _clean(gw, monkeypatch):
    gw.reset()
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)


def _stub(gw):
    return SimpleNamespace(state=SimpleNamespace(connections=gw.conns, llm=object(), delib_llm=None))


# ── 호출 한 건 ───────────────────────────────────────────────────────────────
def test_도는_중에_거절되면_서비스_계정으로_그_호출을_한_번_더_한다(gw):
    flag = {}

    async def go():
        d._cred_mid.set(flag)
        tools = await d._tools_by_name(_stub(gw), ["g"], user="u@x.com", user_pat="user-pat")
        first = await d._call(tools, "agent_search", {"agent_type": "mech-a", "q": "힌지"})
        gw.reject = 401                                    # 여기서 토큰이 만료된다
        second = await d._call(tools, "agent_search", {"agent_type": "mech-a", "q": "힌지"})
        third = await d._call(tools, "agent_search", {"agent_type": "rel-b", "q": "힌지"})
        return first, second, third

    first, second, third = asyncio.run(go())
    for out in (first, second, third):
        assert isinstance(out, str) and "힌지 토크 실측" in out, f"만료 뒤의 조회를 잃었다 — {out!r}"
    assert [c[1] for c in gw.calls] == [_USER, _SVC, _SVC], gw.calls
    # 서비스 계정으로 불러도 **누구의 호출인지**는 그대로 실린다 — 게이트웨이가 그것으로 범위와 사람별 자격을 정한다.
    assert all(c[2] == "u@x.com" for c in gw.calls), gw.calls
    assert "서비스 계정" in flag.get("why", ""), flag


def test_권한_없음_403_은_서비스_계정으로_넘기지_않는다(gw):
    """401 은 '누구인지 확인이 안 된다'(토큰 만료·폐기)이고 403 은 '그 사람은 안 된다' 다. 뒤엣것을 서비스 계정으로
    다시 부르면 막힌 도구가 열린다."""
    flag = {}

    async def go():
        d._cred_mid.set(flag)
        tools = await d._tools_by_name(_stub(gw), ["g"], user="u@x.com", user_pat="user-pat")
        gw.reject = 403
        return await d._call(tools, "agent_search", {"agent_type": "mech-a", "q": "힌지"})

    out = asyncio.run(go())
    assert out.startswith(a._TOOL_FAIL_MARK) and "403" in out, out
    assert gw.calls == [] and flag == {}, (gw.calls, flag)


def test_처음부터_서비스_계정이면_감싸지_않는다(gw):
    async def go():
        return await d._tools_by_name(_stub(gw), ["g"], user="u@x.com")

    tools = asyncio.run(go())
    assert all(t.coroutine.__name__ != "guarded" for t in tools.values())


def test_거절된_호출의_문구가_원인을_말한다(gw):
    """종전 문구는 'unhandled errors in a TaskGroup (1 sub-exception)' 였다 — 401 이 그 안에 싸여 있었다.
    폴백이 없는 길(챗)에서도 원인이 보여야 한다."""
    async def go():
        # 챗이 도구를 받는 길 그대로 — 연결 설정에 사용자 토큰을 얹고 챗의 래퍼만 씌운다(심의의 폴백 래퍼는 없다).
        got = await MultiServerMCPClient(a._with_groups(gw.conns, ["g"], "u@x.com", "user-pat")).get_tools()
        tools = {t.name: a._prep_tool(t) for t in got}
        gw.reject = 401
        return await d._call(tools, "agent_search", {"agent_type": "mech-a", "q": "힌지"})

    out = asyncio.run(go())
    assert out.startswith(a._TOOL_FAIL_MARK + " 도구 agent_search 호출 실패: "), out
    assert d._AUTH_REJECT_MARK in out and "만료됐거나 폐기됐다" in out, out
    assert "TaskGroup" not in out and "인자 스키마" not in out, out


def test_TaskGroup_에_싸인_예외는_안쪽_원인으로_말한다():
    """MCP 전송이 올리는 예외는 전부 이렇게 싸여 온다 — 연결 거절도 'TaskGroup 오류' 로만 보였다."""
    from exceptiongroup import ExceptionGroup

    async def _down(**_kw):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [httpx.ConnectError("All connection attempts failed")])

    tool = SimpleNamespace(name="agent_search", coroutine=_down, args_schema={}, response_format="")
    out = asyncio.run(a._cap_tool(tool).coroutine(q="힌지"))
    assert "All connection attempts failed" in out and "TaskGroup" not in out, out
    assert "인자 문제가 아니라 도구 백엔드 연결/시간초과다" in out, "연결 실패인데 그 안내가 붙지 않았다"


# ── 심의 한 판 ───────────────────────────────────────────────────────────────
def _run(gw, monkeypatch, **req):
    """실제 서버를 상대로 심의를 끝까지 돌린다(LLM 만 대역이다)."""
    seen = []

    async def _text(_obj, system, human):
        if "전문가입니다" in system:
            return json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["가", "나"],
                               "position_short": "요약", "final_position": "최종", "non_negotiable": "",
                               "vote": "진행", "stance": "동의"}, ensure_ascii=False)
        seen.append((system, human))
        return "결정문 본문" if "엔지니어링 톤" in system else "- 한 줄"

    monkeypatch.setattr(d, "_llm_text", _text)
    opts = {"personas": _SEATS, "rounds": 2, "free_tools": 0, "voc": "off", "rescreen": 0, **req}

    async def go():
        return [delib_jobs._parse_sse(c) async for c in
                d.run_deliberation(_stub(gw), "힌지 크랙 원인", ["g"], opts, "u@x.com", "user-pat")]

    events = asyncio.run(go())
    assert not [data for ev, data in events if ev == "error"], [data for ev, data in events if ev == "error"]
    return events, next((h for s_, h in seen if "엔지니어링 톤" in s_), "")


def _degraded(events):
    return [data for ev, data in events if ev == "warning" and data.get("code") == "credential_degraded"]


def test_보고서_저장_직전에_만료돼도_보고서가_저장되고_경고가_나간다(gw, monkeypatch):
    """수 시간 심의의 맨 끝이 가장 걸리기 쉽다 — 종전엔 여기서 보고서를 잃었다."""
    real_chair = d._chair_rows

    def _expire_at_chair(*args, **kw):                      # 의장 전사를 꾸리는 시점 — 라운드는 다 돌았다
        gw.reject = 401
        return real_chair(*args, **kw)

    monkeypatch.setattr(d, "_chair_rows", _expire_at_chair)
    events, _chair = _run(gw, monkeypatch, save_report=1)
    saves = [c for c in gw.calls if c[0] == "create_report_draft"]
    assert saves == [("create_report_draft", _SVC, "u@x.com")], f"보고서를 못 남겼다 — {gw.calls}"
    outcome = next(data for ev, data in events if ev == "delib" and data.get("kind") == "outcome")
    assert outcome["report_id"] == 7
    (warn,) = _degraded(events)
    assert "서비스 계정" in warn["message"] and "만료" in warn["message"], warn
    assert warn["knob"] == "포털 CHAT_PAT_TTL_S" and "CHAT_PAT_TTL_S" not in warn["message"]


def test_라운드_전에_만료되면_그_뒤_조회를_받고_결정문_머리에도_강등이_찍힌다(gw, monkeypatch):
    gw.expire_on = "get_agent_session"                      # 좌석 역할을 읽은 직후 만료 — 지식카드 조회부터 거절된다
    events, chair = _run(gw, monkeypatch, save_report=0)
    searched = [c for c in gw.calls if c[0] == "agent_search"]
    assert len(searched) == len(_SEATS) and all(c[1] == _SVC for c in searched), gw.calls
    cards = [data for ev, data in events if ev == "delib" and data.get("kind") == "evidence"
             and str(data.get("source", "")).endswith("· 지식카드")]
    assert len(cards) == len(_SEATS), "만료 뒤 좌석이 지식카드를 못 받았다"
    assert len(_degraded(events)) == 1, "경고는 한 번만 낸다"
    names = [ev if ev != "delib" else f"delib:{data.get('stage') or data.get('kind')}" for ev, data in events]
    assert names.index("warning") < names.index("delib:r1"), "라운드가 돌기 전에 알려야 한다"
    assert "자격증명 강등" in chair and "서비스 계정" in chair, "의장이 받는 근거 프로파일에 강등이 없다"


def test_만료되지_않으면_사용자_토큰으로만_돌고_경고가_없다(gw, monkeypatch):
    events, _chair = _run(gw, monkeypatch, save_report=1)
    assert gw.calls and all(c[1] == _USER for c in gw.calls), gw.calls
    assert not _degraded(events)
