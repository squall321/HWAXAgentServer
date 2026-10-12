# 카드 대조 관문(review_gate)이 줄을 세우지 않고 상한에서 곧바로 '바쁨' 을 돌려주며, 심의가 돌면 상한을 낮추고, 끝을 빠짐없이 세는지 본다
#
# 관문은 수만 센다(asyncio 객체가 없다) — 처음 쓴 이벤트 루프에 묶이지 않고, 획득·반납이 await 없이 끝난다. 자리를 두 번
# 돌려줘도 한 번만 세야 한다(제너레이터의 finally 가 두 길로 올 수 있다). 사용량을 모르는 호출은 0 토큰이 아니라 '모름'
# 으로 센다.
#
#   실행:  .venv/bin/python -m pytest tests/test_review_gate.py -q
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import review_gate as rg  # noqa: E402


def test_손잡이_기본값과_잘못된_값():
    assert rg.knobs({}) == {"concurrency": 4, "with_delib": 2, "retry_after_s": 30}
    bad = {"CARD_REVIEW_CONCURRENCY": "넷", "CARD_REVIEW_CONCURRENCY_WITH_DELIB": "0", "CARD_REVIEW_RETRY_AFTER_S": "-1"}
    assert rg.knobs(bad) == {"concurrency": 4, "with_delib": 2, "retry_after_s": 30}
    # 심의 중 상한이 평소보다 크면 평소 값으로 묶는다(심의가 돌 때 더 받는 설정은 뜻이 거꾸로다).
    assert rg.knobs({"CARD_REVIEW_CONCURRENCY": "3", "CARD_REVIEW_CONCURRENCY_WITH_DELIB": "5"})["with_delib"] == 3
    # 평소 1 이면 심의 중 기본값도 1 이다.
    assert rg.knobs({"CARD_REVIEW_CONCURRENCY": "1"})["with_delib"] == 1


def test_상한에서_기다리지_않고_바쁨():
    g = rg.ReviewGate(lambda: (2, "normal"), retry_after_s=30)
    t1, t2 = g.try_acquire("a"), g.try_acquire("b")
    busy = g.try_acquire("c")
    assert isinstance(t1, rg.Ticket) and isinstance(t2, rg.Ticket)
    assert isinstance(busy, rg.Busy) and (busy.limit, busy.active, busy.retry_after_s, busy.reason) == (2, 2, 30, "normal")
    g.release(t1, "ok")
    assert isinstance(g.try_acquire("d"), rg.Ticket), "자리가 났는데 다시 잡지 못했다"
    assert g.totals["busy"] == 1 and g.totals["calls"] == 3


def test_심의가_돌면_상한이_내려가고_도는_호출은_빼앗지_않는다():
    running = {"n": 0}
    g = rg.from_env(lambda: running["n"], env={})
    held = [g.try_acquire(str(i)) for i in range(4)]
    assert all(isinstance(t, rg.Ticket) for t in held)
    assert g.snapshot()["limit"] == 4 and g.snapshot()["limit_reason"] == "normal"
    running["n"] = 1
    snap = g.snapshot()
    assert (snap["limit"], snap["limit_reason"], snap["active"]) == (2, "delib_active", 4)
    for t in held[:2]:
        g.release(t, "ok")
    assert isinstance(g.try_acquire("x"), rg.Busy), "심의 중 상한 2 인데 셋째 자리를 줬다"
    g.release(held[2], "ok")
    assert isinstance(g.try_acquire("y"), rg.Ticket)


def test_두_번_돌려줘도_한_번만_센다():
    g = rg.ReviewGate(lambda: (4, "normal"))
    t = g.try_acquire("a")
    g.release(t, "ok")
    g.release(t, "cancelled")
    assert g.active == 0 and g.totals["ok"] == 1 and g.totals["cancelled"] == 0


def test_끝을_센다_오류_사용량_모름():
    g = rg.ReviewGate(lambda: (4, "normal"))
    g.release(g.try_acquire("a"), "ok", {"tokens_in": 100, "tokens_out": 20}, repaired=True)
    g.release(g.try_acquire("b"), "context_overflow", None, unknown_calls=1)
    g.release(g.try_acquire("c"), "ok", {"tokens_in": 5, "tokens_out": 1}, unknown_calls=1, truncated=True)
    g.release(g.try_acquire("d"), "cancelled")
    g.note("over_budget")
    t = g.totals
    assert (t["calls"], t["ok"], t["error"], t["cancelled"]) == (4, 2, 1, 1)
    assert (t["context_overflow"], t["over_budget"], t["truncated"], t["repaired"]) == (1, 1, 1, 1)
    assert (t["tokens_in"], t["tokens_out"], t["tokens_unknown_calls"]) == (105, 21, 2)
    assert g.last_error.startswith("context_overflow @ ")
    assert set(g.snapshot()["totals"]) == set(rg.TOTAL_KEYS)


def test_상한을_못_읽으면_가장_좁게_연다():
    def boom():
        raise RuntimeError("x")

    g = rg.ReviewGate(boom)
    assert isinstance(g.try_acquire("a"), rg.Ticket)
    busy = g.try_acquire("b")
    assert isinstance(busy, rg.Busy) and busy.limit == 1 and busy.reason == "limit_error:RuntimeError"
