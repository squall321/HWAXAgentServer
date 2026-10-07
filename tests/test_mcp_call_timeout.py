# 도구 호출 1건의 엔진측 기한(MCP_CALL_TIMEOUT_S) — 답하지 않는 게이트웨이를 끝없이 기다리지 않는지
#
# 엔진이 게이트웨이에 건 한도는 전송 한도(30초/300초)뿐이었고 그것은 바이트 사이 **침묵**만 잰다. 게이트웨이는
# 호출이 도는 동안 15초마다 ping 을 보내므로, 게이트웨이가 ping 만 보내며 멈추면 엔진은 끝없이 기다렸다 —
# 좌석 자유 조회·RA 저장·VOC·역할 복원이 전부 그 대기 위에 있고, 그 대기가 심의 잡 자리를 붙든다.
#
# **실제 MCP 서버를 띄워서** 본다 — 이 박스의 빈 포트에 답이 늦은 도구 하나를 가진 서버를 올리고, 엔진이 쓰는
# 그 경로(_with_groups → MultiServerMCPClient → _prep_tool)로 부른다.
#
#   실행:  .venv/bin/python -m pytest tests/test_mcp_call_timeout.py -q
import asyncio
import socket
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
import uvicorn  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

import app as a  # noqa: E402
import deliberation as d  # noqa: E402

_SLOW_S = 2.0          # 느린 도구가 답하기까지 — 시험이 거는 기한(0.4초)의 5배


@pytest.fixture(scope="module")
def gateway():
    """답이 늦은 도구(slow_lookup)와 바로 답하는 도구(quick_lookup)를 가진 MCP 서버 — 게이트웨이 대역이다."""
    mcp = FastMCP("hwax-timeout-test", stateless_http=True, streamable_http_path="/")

    @mcp.tool()
    async def slow_lookup(q: str = "") -> str:
        """답이 늦은 조회."""
        await asyncio.sleep(_SLOW_S)
        return "늦은 답"

    @mcp.tool()
    async def quick_lookup(q: str = "") -> str:
        """바로 답하는 조회."""
        return f"빠른 답 {q}"

    with socket.socket() as s:                  # 빈 포트 — 떠 있는 서비스(9009·9110 등)와 겹치지 않게 커널이 고른다
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), host="127.0.0.1", port=port,
                                           log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "시험용 MCP 서버가 뜨지 않았다"
    yield {"gateway": {"url": f"http://127.0.0.1:{port}/", "transport": "streamable_http"}}
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    """사내 프록시가 걸린 박스에서도 루프백은 곧장 간다."""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)


def _call(conns, name, args=None):
    """엔진이 도구를 부르는 그 길 — 연결 설정에 신원·기한을 얹고, 받은 도구를 챗과 같은 래퍼에 통과시켜 부른다."""
    stub = SimpleNamespace(state=SimpleNamespace(connections=conns))

    async def go():
        tools = await d._tools_by_name(stub, ["g"])
        t0 = time.monotonic()
        out = await d._call(tools, name, args or {})
        return out, time.monotonic() - t0

    return asyncio.run(go())


def test_답하지_않는_도구를_기한에서_끊고_기한과_손잡이를_말한다(gateway, monkeypatch):
    monkeypatch.setattr(a, "MCP_CALL_TIMEOUT_S", 0.4)
    out, took = _call(gateway, "slow_lookup")
    assert took < _SLOW_S - 0.5, f"{took:.1f}초를 기다렸다 — 기한(0.4초)이 걸리지 않았다"
    assert isinstance(out, str) and out.startswith(a._TOOL_FAIL_MARK + " 도구 slow_lookup 호출 실패: "), out
    assert "0초 안에 답하지 않았다(MCP_CALL_TIMEOUT_S)" in out, out       # 0.4초를 정수로 적은 것이다
    assert "GATEWAY_CALL_TIMEOUT" in out and "게이트웨이 무응답" in out, out
    # 인자를 고쳐 다시 부르라고 하지 않는다 — 인자 문제가 아니다.
    assert "인자 문제가 아니라" in out and "인자 스키마" not in out, out
    # 심의가 실패로 읽는다(성공한 조회처럼 근거에 실리지 않는다).
    assert not d._delib_tool_result_ok(out)


def test_기한_안에_답하는_도구는_그대로_돈다(gateway, monkeypatch):
    monkeypatch.setattr(a, "MCP_CALL_TIMEOUT_S", 0.4)
    out, _took = _call(gateway, "quick_lookup", {"q": "힌지"})
    assert out == "빠른 답 힌지", out


def test_기한을_0_으로_끄면_종전처럼_끝까지_기다린다(gateway, monkeypatch):
    """끄는 길을 남긴다 — 그때는 연결 설정에 세션 기한을 싣지 않는다(종전 동작 그대로)."""
    monkeypatch.setattr(a, "MCP_CALL_TIMEOUT_S", 0.0)
    assert "session_kwargs" not in a._with_groups(gateway, ["g"])["gateway"]
    out, took = _call(gateway, "slow_lookup")
    assert out == "늦은 답" and took >= _SLOW_S - 0.5, (out, took)


def test_기본값은_900초이고_게이트웨이_한도보다_크다():
    """게이트웨이는 호출 600초·전송 read 와 단발 세션 바깥 기한 660초다. 안쪽이 먼저 걸려야 어느 백엔드가
    느린지 문구가 나온다. 환경값이 아니라 **소스의 기본값**을 본다."""
    import re

    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    m = re.search(r'^MCP_CALL_TIMEOUT_S = _env_float\("MCP_CALL_TIMEOUT_S", (\d+)\.0\)$', src, re.M)
    assert m and int(m.group(1)) == 900 and int(m.group(1)) > 660, m and m.group(0)


def test_연결_설정에_세션_기한을_얹되_있던_설정은_지킨다(monkeypatch):
    monkeypatch.setattr(a, "MCP_CALL_TIMEOUT_S", 900.0)
    conns = {"gateway": {"url": "http://gw/mcp", "transport": "streamable_http",
                         "session_kwargs": {"client_info": "박스 설정"}}}
    out = a._with_groups(conns, ["g"], "u@x.com", "pat")
    assert out["gateway"]["session_kwargs"] == {"client_info": "박스 설정",
                                                "read_timeout_seconds": timedelta(seconds=900)}
    assert conns["gateway"]["session_kwargs"] == {"client_info": "박스 설정"}, "입력을 고쳤다"
