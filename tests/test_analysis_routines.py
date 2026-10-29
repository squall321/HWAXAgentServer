# 분석 루틴 — 에이전트 서버 몫(HWAXPortal docs/analysis-routines). R2: 일반 챗 ReAct 는 게이트웨이의 루틴 도구 여섯을 묶지도 부르지도 않는다
import app as a


def test_일반_챗은_루틴_도구를_묶지도_invoke_tool_로_부르지도_않는다():
    """루틴은 사람이 골라 켜는 것이다 — 자유 턴의 모델이 실행을 시작하거나 원장에 쓰면 사람이 고르지 않은 연쇄가 돈다."""
    for n in ("routine_list", "routine_get", "routine_run_start", "routine_call", "routine_step_done", "routine_report_submit"):
        assert a._agent_denied(n) and a._agent_denied(n.upper()), n
    assert not a._agent_denied("routine_summary") and not a._agent_denied("search_tools"), "정확한 이름만"
    assert a._AGENT_DENY_TOOLS == frozenset(a._external_llm.TASK_TOOLS), "외부 응답자 과제 도구 집합은 그대로다(따로 둔다)"
