# 지식카드 조회 헬퍼 단위 테스트 — 느린 검색·삼켜진 오류·폴백을 상정한 동작(네트워크 불필요)
import asyncio

import deliberation as d


class _Tool:
    """_call 이 기대하는 최소 모양. 모드별로 다른 동작을 심는다."""

    def __init__(self, behave):
        self._behave = behave
        self.calls: list[str] = []

    async def ainvoke(self, args):
        mode = args.get("mode")
        self.calls.append(mode)
        return await self._behave(mode)


def _run(behave, **kw):
    t = _Tool(behave)
    hits, note = asyncio.run(d._agent_search_hits({"agent_search": t}, "dom-x", "질의", **kw))
    return hits, note, t.calls


async def _ok(_mode):
    return '{"hits": [{"title": "t", "snippet": "s"}], "refused": false}'


def test_normal_call_returns_hits_without_note():
    hits, note, calls = _run(_ok)
    assert len(hits) == 1 and note == "" and calls == ["hybrid"]


def test_refusal_is_a_verdict_not_a_degradation():
    async def refused(_mode):
        return '{"hits": [], "refused": true}'

    hits, note, calls = _run(refused)
    # 거절은 그 좌석의 정상 판정이다 — 폴백을 돌리지도, 강등으로 표시하지도 않는다.
    assert hits == [] and note == "" and calls == ["hybrid"]


def test_timeout_falls_back_to_semantic_and_says_so():
    async def slow_then_fast(mode):
        if mode == "hybrid":
            await asyncio.sleep(5)
            return '{"hits": []}'
        return '{"hits": [{"title": "t", "snippet": "s"}]}'

    hits, note, calls = _run(slow_then_fast, timeout_s=0.05)
    assert len(hits) == 1
    assert calls == ["hybrid", "semantic"]
    assert "초과" in note and "semantic" in note      # 강등 사실이 문자열로 드러난다


def test_both_modes_timing_out_returns_empty_with_reason():
    async def always_slow(_mode):
        await asyncio.sleep(5)
        return '{"hits": []}'

    hits, note, calls = _run(always_slow, timeout_s=0.05)
    assert hits == [] and calls == ["hybrid", "semantic"]
    assert "폴백도 실패" in note                      # 조용한 0건이 아니다


def test_swallowed_tool_error_string_is_detected():
    # _call 은 예외를 "(tool X error: …)" 문자열로 삼킨다 — 정상 응답과 타입이 같다.
    async def swallowed(mode):
        if mode == "hybrid":
            return "(tool agent_search error: Timed out while waiting for response)"
        return '{"hits": [{"title": "t", "snippet": "s"}]}'

    hits, note, calls = _run(swallowed)
    assert len(hits) == 1 and calls == ["hybrid", "semantic"]
    assert "오류" in note


def test_no_fallback_when_mode_already_is_the_fallback():
    async def slow(_mode):
        await asyncio.sleep(5)
        return '{"hits": []}'

    hits, note, calls = _run(slow, mode="semantic", timeout_s=0.05)
    assert hits == [] and calls == ["semantic"]       # 같은 모드로 두 번 묻지 않는다
    assert "초과" in note


def test_missing_tool_is_reported_not_silently_empty():
    hits, note = asyncio.run(d._agent_search_hits({}, "dom-x", "질의", timeout_s=1))
    # 도구가 없으면 _call 이 None 을 준다 — 그것을 '지식 0건' 으로 조용히 접지 않는다.
    assert hits == [] and note
