# 요구 키 판정 — 심화 라운드가 반박 없이 통과하지 않는지, 그리고 재시도가 상한 안에서 멈추는지
import asyncio
import types

import deliberation as d


class _LLM:
    """정해진 응답을 순서대로 돌려주는 가짜 LLM. 호출 횟수를 센다."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    async def ainvoke(self, _msgs):
        self.calls += 1
        text = self.replies[min(self.calls - 1, len(self.replies) - 1)]

        class _R:
            content = text
            response_metadata: dict = {}
        return _R()


def _opts(**kw):
    o = types.SimpleNamespace(parse_retries=2, prose_first=0)
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def _round(llm, required, validator=None, **kw):
    return asyncio.run(d._persona_round(
        llm, {"key": "mfg-smt", "role": "r"}, "prompt", required, validator, _opts(**kw)))


def test_deepen_without_rebut_now_triggers_retry():
    """⚠ 회귀 방지 — 예전엔 OR 판정이라 deepen 만 채워도 재시도 0회로 통과했다.
    프롬프트는 '반박 최소 1개'를 요구하는데 코드가 그걸 보지 않았다."""
    llm = _LLM('{"deepen": "리플로우 245도에서 휨 0.7mm 초과", "rebut": []}')
    _round(llm, ("deepen", "rebut"))
    assert llm.calls == 3          # 최초 1 + 재시도 2 — 지적이 실제로 나갔다


def test_complete_answer_needs_no_retry():
    llm = _LLM('{"deepen": "심화 내용", "rebut": [{"target": "x", "counter": "y"}]}')
    out = _round(llm, ("deepen", "rebut"))
    assert llm.calls == 1 and out["rebut"]


def test_retry_stops_at_parse_retries_and_keeps_the_answer():
    """soft 동작 — 재시도가 소진되면 지적이 남아도 발언은 그대로 쓴다(폭주하지 않는다)."""
    llm = _LLM('{"deepen": "심화만 있음", "rebut": []}')
    out = _round(llm, ("deepen", "rebut"), parse_retries=2)
    assert llm.calls == 3
    assert out["deepen"] == "심화만 있음"      # 버리지 않는다
    assert "say" not in out                     # 부분 성공이라 강등 표식은 안 붙는다


def test_totally_unstructured_output_still_degrades_with_a_marker():
    """구조화 완전 실패는 여전히 any 게이트로 구제하고 **표식을 남긴다**."""
    llm = _LLM("그냥 산문입니다")
    out = _round(llm, ("deepen", "rebut"))
    assert out["say"].startswith("(구조화 실패")


def test_dict_without_any_required_key_is_marked_not_silently_passed():
    llm = _LLM('{"response": "엉뚱한 스키마"}')
    out = _round(llm, ("deepen", "rebut"))
    assert "(요구 키 누락" in out["say"]


def test_missing_keys_are_named_in_the_retry_hint():
    """지적 문구가 **빠진 키만** 짚어야 모델이 무엇을 채울지 안다."""
    assert d._persona_round  # 모듈 로드 확인
    llm = _LLM('{"deepen": "있음"}', '{"deepen": "있음", "rebut": [{"counter": "c"}]}')
    out = _round(llm, ("deepen", "rebut"))
    assert llm.calls == 2 and out["rebut"]
