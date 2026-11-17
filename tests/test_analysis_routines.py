# 분석 루틴 — 에이전트 서버 몫(HWAXPortal docs/analysis-routines). R2: 일반 챗 ReAct 는 게이트웨이의 루틴 도구 여섯을 묶지도 부르지도 않는다
import app as a


def test_일반_챗은_루틴_도구를_묶지도_invoke_tool_로_부르지도_않는다():
    """루틴은 사람이 골라 켜는 것이다 — 자유 턴의 모델이 실행을 시작하거나 원장에 쓰면 사람이 고르지 않은 연쇄가 돈다."""
    for n in ("routine_list", "routine_get", "routine_run_start", "routine_call", "routine_step_done", "routine_report_submit"):
        assert a._agent_denied(n) and a._agent_denied(n.upper()), n
    assert not a._agent_denied("routine_summary") and not a._agent_denied("search_tools"), "정확한 이름만"
    assert a._AGENT_DENY_TOOLS == frozenset(a._external_llm.TASK_TOOLS), "외부 응답자 과제 도구 집합은 그대로다(따로 둔다)"


def test_게이트웨이_온톨로지_조회는_묶지_않지만_부르기는_막지_않는다():
    """HWAXPortal docs/ontology-first PLAN §12 · OF-41 — 개인 Claude 의 입구라 웹 챗 기본(묶인 도구)은 그대로다. 표지 턴(패널)도 같다."""
    from types import SimpleNamespace
    t = SimpleNamespace(name="ontology_lookup", annotations=None, meta=None)
    assert not a._bindable(t) and not a._bindable(t, allow_panels=True)
    assert not a._agent_denied("ontology_lookup"), "invoke_tool 로는 닿는다(검색 손잡이를 켠 박스에서 챗의 search_tools 답에 선다)"
    assert a._bindable(SimpleNamespace(name="search_tools", annotations=None, meta=None))


def test_게이트웨이_관리자_보기도_챗에_묶지_않는다():
    """HWAXPortal docs/request-1117 R-10 ③ — 관리자의 tools/list 에만 서는 읽기 전용 허용 목록 보기가 관리자의 웹 챗 묶음을 바꾸지 않는다."""
    from types import SimpleNamespace
    assert not a._bindable(SimpleNamespace(name="gateway_readonly_allowlist", annotations=None, meta=None))
