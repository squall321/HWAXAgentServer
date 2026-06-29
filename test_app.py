# Agent Server: 게이트웨이로 groups를 실어보내는 헤더 주입/설정 로직 단위 테스트 (네트워크 불필요).
from app import GROUPS_HEADER, _parse_servers, _with_groups


def test_with_groups_injects_comma_joined_header():
    conns = {"gateway": {"url": "http://gw/mcp", "transport": "streamable_http",
                         "headers": {"Authorization": "Bearer x"}}}
    out = _with_groups(conns, ["a", "b"])
    assert out["gateway"]["headers"][GROUPS_HEADER] == "a,b"
    assert out["gateway"]["headers"]["Authorization"] == "Bearer x"   # 기존 헤더 보존


def test_with_groups_empty_groups_empty_header():
    conns = {"gateway": {"url": "http://gw/mcp"}}
    out = _with_groups(conns, [])
    assert out["gateway"]["headers"][GROUPS_HEADER] == ""              # 빈 헤더 = 그룹 없음(게이트웨이가 공개만 노출)


def test_with_groups_adds_headers_when_absent():
    conns = {"gateway": {"url": "http://gw/mcp"}}   # headers 키 없음
    out = _with_groups(conns, ["x"])
    assert out["gateway"]["headers"] == {GROUPS_HEADER: "x"}


def test_with_groups_does_not_mutate_input():
    conns = {"gateway": {"url": "http://gw/mcp", "headers": {"Authorization": "Bearer x"}}}
    _with_groups(conns, ["a"])
    assert GROUPS_HEADER not in conns["gateway"]["headers"]            # 원본 보존
    assert conns["gateway"]["headers"] == {"Authorization": "Bearer x"}


def test_parse_servers_string_to_connections():
    out = _parse_servers("gateway=http://gw/mcp")
    assert out == {"gateway": {"url": "http://gw/mcp", "transport": "streamable_http"}}


def test_parse_servers_ignores_blank_and_malformed():
    assert _parse_servers("") == {}
    assert _parse_servers("no-equals-here") == {}
    assert _parse_servers("a=http://x, , b=http://y") == {
        "a": {"url": "http://x", "transport": "streamable_http"},
        "b": {"url": "http://y", "transport": "streamable_http"},
    }


def test_agent_for_caches_by_group_set(monkeypatch):
    # _agent_for가 그룹셋 단위로 에이전트를 캐시하는지(같은 그룹셋=재사용, 다른 셋=별도) 검증.
    # MCP 게이트웨이 호출은 스텁으로 대체 → 네트워크 불필요.
    import asyncio
    import types as pytypes

    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI

    import app as appmod

    @tool
    def t(x: str) -> str:
        """tool"""
        return x

    seen_headers = []

    class _StubClient:
        def __init__(self, connections):
            # 게이트웨이로 나갈 그룹 헤더를 기록(검증용)
            seen_headers.append(connections["gateway"]["headers"][GROUPS_HEADER])

        async def get_tools(self):
            return [t]

    monkeypatch.setattr(appmod, "MultiServerMCPClient", _StubClient)

    state = pytypes.SimpleNamespace(
        llm=ChatOpenAI(base_url="http://127.0.0.1:1/v1", api_key="EMPTY", model="x"),
        connections={"gateway": {"url": "http://gw/mcp", "headers": {"Authorization": "Bearer x"}}},
        agent_cache={},
    )
    fake = pytypes.SimpleNamespace(state=state)

    a1 = asyncio.run(appmod._agent_for(fake, ["admin"]))
    a2 = asyncio.run(appmod._agent_for(fake, ["admin"]))     # 같은 그룹셋 → 캐시 재사용
    a3 = asyncio.run(appmod._agent_for(fake, ["user"]))      # 다른 그룹셋 → 새 에이전트

    assert a1 is a2
    assert a1 is not a3
    assert set(state.agent_cache.keys()) == {frozenset({"admin"}), frozenset({"user"})}
    assert seen_headers == ["admin", "user"]                 # 게이트웨이로 그룹셋당 1회, 정렬된 헤더 전달
