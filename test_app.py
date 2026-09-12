# Agent Server: 게이트웨이로 groups를 실어보내는 헤더 주입/설정 로직 단위 테스트 (네트워크 불필요).
from app import GROUPS_HEADER, _needs_final_rescue, _parse_servers, _with_groups


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
        # 실제 app.state 가 기동 시(app.py:257·261) 항상 갖는 칸 — 가짜 state 에도 그대로 둔다.
        tool_degraded={},
        tool_load_error={},
        # 게이트웨이 불통 때 직전 성공 도구로 답하는 폴백 재료(app.py 가 1491 에서 쓰고 1526 에서 읽는다).
        # 이 칸이 빠져 있어 2026-09-03 부터 이 테스트가 상시 빨간색이었고, 그동안 캐시 키 격리
        # 불변식이 검증되지 않았다 — 그 불변식은 '키에서 신원을 빼면 조용히 남의 데이터가 보인다' 다.
        tool_snapshot={},
    )
    fake = pytypes.SimpleNamespace(state=state)

    a1 = asyncio.run(appmod._agent_for(fake, ["admin"]))
    a2 = asyncio.run(appmod._agent_for(fake, ["admin"]))     # 같은 그룹셋 → 캐시 재사용
    a3 = asyncio.run(appmod._agent_for(fake, ["user"]))      # 다른 그룹셋 → 새 에이전트

    assert a1 is a2
    assert a1 is not a3
    # 캐시 키는 (groups, pins, query, sources, user, cred) 튜플이다(app.py:1325). 이 테스트가 보는
    # 것은 그룹셋 축 하나뿐이므로 첫 칸만 비교한다 — 나머지 축은 여기서 전부 기본값이다.
    assert {k[0] for k in state.agent_cache} == {frozenset({"admin"}), frozenset({"user"})}
    assert seen_headers == ["admin", "user"]                 # 게이트웨이로 그룹셋당 1회, 정렬된 헤더 전달


def test_도구_뒤_말이_없으면_예고만_남아도_구제한다():
    """'먼저 가이드와 템플릿을 확인하겠습니다' 하고 도구만 부른 뒤 끝나는 턴 — 예전에는
    text 가 비어 있지 않아 빈응답 구제를 그대로 통과했고 화면에는 그 한 줄만 남았다."""
    calls = [("get_guide", "")]
    # ① 예고만 하고 도구 결과 뒤로 한 글자도 없다 → 구제
    assert _needs_final_rescue(calls, "먼저 가이드와 템플릿을 확인하겠습니다.", 0) is True
    # ② 아무 글자도 없는 턴 → 종전대로 구제
    assert _needs_final_rescue(calls, "", 0) is True
    # ③ 도구 결과 뒤에 답을 썼다 → 구제 안 함(정상 턴)
    assert _needs_final_rescue(calls, "가이드는 3장 구성입니다…", 120) is False
    # ④ 도구를 안 쓴 턴은 이 구제의 대상이 아니다(다른 보강기가 본다)
    assert _needs_final_rescue([], "", 0) is False


def test_지정_전문가_목록_정규화():
    """여러 명 지정 — 첫 명이 주 전문가. 공백·중복·상한을 코드가 정리한다(구 계약도 같이 받는다)."""
    from app import PERSONA_MAX, _persona_keys
    assert _persona_keys(["a", "b"], None) == ["a", "b"]
    assert _persona_keys(None, "solo") == ["solo"], "한 명만 지정한 종전 계약"
    assert _persona_keys([], "solo") == ["solo"], "빈 목록이면 구 필드를 쓴다"
    assert _persona_keys([" a ", "a", "b", ""], None) == ["a", "b"], "중복·공백 제거"
    assert _persona_keys([str(i) for i in range(20)], None) == [str(i) for i in range(PERSONA_MAX)]
    assert _persona_keys(None, None) == []
    assert _persona_keys([1, None, "ok"], None) == ["ok"], "문자열 아닌 값은 버린다"


def test_예고_미호출_감지는_언어가_새도_잡는다():
    """모델이 중국어로 흘러 답해도 '부르겠다고만 하고 안 부른 턴'은 잡아야 한다 — 한국어 패턴만
    보면 그대로 통과해 예고 한 줄만 남는다(실측: dev 7B 가 중국어로 analyze_laminate 예고)."""
    from app import _announced_without_calling
    assert _announced_without_calling("我们将调用 analyze_laminate 函数来计算") is True
    assert _announced_without_calling("接下来，我们将使用 Laminate Analyzer 进行计算") is True
    assert _announced_without_calling("먼저 확인하겠습니다") is True
    assert _announced_without_calling("Let me check the guide") is True
    # 도구 이름을 설명만 하는 문장은 걸리지 않는다(오탐이 더 나쁘다)
    assert _announced_without_calling("analyze_laminate 는 적층 해석 도구입니다") is False
