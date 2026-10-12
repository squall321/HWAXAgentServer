# 카드 대조 호출의 동시 수 관문 — 줄을 세우지 않고 자리가 없으면 곧바로 '바쁨' 을 돌려주며, 호출 결과를 /health 에 셀 수를 모은다
"""공유 LLM 을 챗·심의·카드 대조가 나눠 쓴다. 카드 대조는 며칠짜리 원장의 호출이라 한꺼번에 몰리면 챗 첫 응답이
밀린다. 그래서 프로세스 전체에서 동시 수를 묶는다 — 평소 `CARD_REVIEW_CONCURRENCY`(4), 심의가 도는 동안
`CARD_REVIEW_CONCURRENCY_WITH_DELIB`(2).

**줄을 세우지 않는다**(C-24). 자리가 없으면 그 자리에서 `Busy` 를 돌려주고, 호출자(리스크 앱)는 아무것도 세지 않고
`retry_after_s` 뒤에 같은 호출을 다시 낸다. 기다리는 줄이 서버에 없으므로 끊긴 구독이 줄 자리를 쥐고 새는 일이 없다.

asyncio 기본 객체를 쓰지 않는다 — 수만 센다. 그래서 처음 쓴 이벤트 루프에 묶이지 않고(모듈 전역 asyncio 객체가 다른
루프에서 죽는 함정 — deliberation.py 의 관문 주석), 획득·반납이 await 없이 끝난다(획득과 첫 프레임 사이에 끊겨도
자리가 새지 않는다 — 부르는 쪽이 제너레이터 안에서 잡고 finally 에서 돌려준다).

M3 의 WP5a A1 이 이 파일에 밤 창·챗 세기·비우기를 더한다(관문이 두 벌이 되지 않게 이름과 API 를 지금 최소로 낸다).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

CONCURRENCY_DEFAULT = 4
WITH_DELIB_DEFAULT = 2
RETRY_AFTER_DEFAULT = 30


def _env_int(env, name: str, default: int, lo: int) -> int:
    """정수 손잡이 — 비면 기본값, 못 읽거나 lo 미만이면 경고하고 기본값(기동을 죽이지 않는다)."""
    raw = str(env.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        print(f"[review_gate] env {name}='{raw[:40]}' 정수 파싱 실패 — 기본값 {default} 사용")
        return default
    if v < lo:
        print(f"[review_gate] env {name}={v} 는 {lo} 미만이다 — 기본값 {default} 사용")
        return default
    return v


def knobs(env=os.environ) -> dict:
    """관문 손잡이 셋 — {concurrency, with_delib, retry_after_s}. 심의 중 상한이 평소보다 크면 평소 값으로 묶는다
    (심의가 돌 때 오히려 더 많이 받는 설정은 뜻이 거꾸로다)."""
    conc = _env_int(env, "CARD_REVIEW_CONCURRENCY", CONCURRENCY_DEFAULT, 1)
    busy = _env_int(env, "CARD_REVIEW_CONCURRENCY_WITH_DELIB", min(WITH_DELIB_DEFAULT, conc), 1)
    if busy > conc:
        print(f"[review_gate] CARD_REVIEW_CONCURRENCY_WITH_DELIB={busy} 가 CARD_REVIEW_CONCURRENCY={conc} 보다 크다 — "
              f"{conc} 로 묶는다")
        busy = conc
    return {"concurrency": conc, "with_delib": busy,
            "retry_after_s": _env_int(env, "CARD_REVIEW_RETRY_AFTER_S", RETRY_AFTER_DEFAULT, 0)}


@dataclass
class Ticket:
    """잡은 자리 하나 — release 에 그대로 돌려준다."""
    tag: str
    t0: float
    released: bool = False


@dataclass
class Busy:
    """자리가 없다 — 기다리지 않고 이것을 돌려준다(호출자가 retry_after_s 뒤 같은 호출을 다시 낸다)."""
    reason: str
    limit: int
    active: int
    retry_after_s: int


# /health.card_review.totals 의 칸. calls = 자리를 잡은 호출, ok·error·cancelled 는 그 끝(합이 calls 다 — 도는 것 제외),
# busy·over_budget 은 자리를 잡기 전에 돌려보낸 수다.
TOTAL_KEYS = ("calls", "ok", "error", "cancelled", "busy", "truncated", "repaired", "over_budget", "context_overflow",
              "tokens_in", "tokens_out", "tokens_unknown_calls")


class ReviewGate:
    """limit_fn() -> (상한, 사유) 를 획득 때마다 부른다 — 심의가 시작·끝나면 다음 획득부터 상한이 바뀐다.
    이미 잡은 자리는 상한이 내려가도 빼앗지 않는다(도는 호출을 끊지 않는다)."""

    def __init__(self, limit_fn, retry_after_s: int = RETRY_AFTER_DEFAULT):
        self._limit_fn = limit_fn
        self.retry_after_s = int(retry_after_s)
        self.active = 0
        self.totals = dict.fromkeys(TOTAL_KEYS, 0)
        self.last_error: str | None = None

    def limit(self) -> tuple[int, str]:
        try:
            n, why = self._limit_fn()
            return max(1, int(n)), str(why)
        except Exception as exc:  # noqa: BLE001 — 상한을 못 읽어도 관문은 닫히지 않는다(가장 좁게 연다)
            return 1, f"limit_error:{type(exc).__name__}"

    def try_acquire(self, tag: str = "") -> Ticket | Busy:
        """기다리지 않는다 — 자리가 있으면 Ticket, 없으면 Busy."""
        lim, why = self.limit()
        if self.active >= lim:
            self.totals["busy"] += 1
            return Busy(why, lim, self.active, self.retry_after_s)
        self.active += 1
        self.totals["calls"] += 1
        return Ticket(tag, time.monotonic())

    def release(self, ticket: Ticket, outcome: str, usage: dict | None = None, *, unknown_calls: int = 0,
                truncated: bool = False, repaired: bool = False) -> None:
        """자리를 돌려주고 끝을 센다. outcome 은 ok · cancelled · 오류 코드. usage 는 사용량을 아는 LLM 호출들의 합
        ({tokens_in, tokens_out}), unknown_calls 는 사용량을 모르는 LLM 호출 수다(tokens_unknown_calls 에 더한다 — 0 토큰으로
        치지 않는다). 두 번 불러도 한 번만 센다."""
        if ticket.released:
            return
        ticket.released = True
        self.active = max(0, self.active - 1)
        t = self.totals
        if outcome == "ok":
            t["ok"] += 1
        elif outcome == "cancelled":
            t["cancelled"] += 1
        else:
            t["error"] += 1
            if outcome == "context_overflow":
                t["context_overflow"] += 1
            self.last_error = f"{outcome} @ {time.strftime('%Y-%m-%dT%H:%M:%S')}"
        t["truncated"] += bool(truncated)
        t["repaired"] += bool(repaired)
        t["tokens_unknown_calls"] += max(0, int(unknown_calls))
        if usage:
            t["tokens_in"] += int(usage.get("tokens_in") or 0)
            t["tokens_out"] += int(usage.get("tokens_out") or 0)

    def note(self, outcome: str) -> None:
        """자리를 잡기 전에 돌려보낸 요청을 센다(over_budget — 스트림을 열기 전에 422 로 거절한 것)."""
        if outcome in self.totals:
            self.totals[outcome] += 1

    def snapshot(self) -> dict:
        lim, why = self.limit()
        return {"limit": lim, "limit_reason": why, "active": self.active, "retry_after_s": self.retry_after_s,
                "totals": dict(self.totals), "last_error": self.last_error}


def from_env(delib_running, env=os.environ) -> ReviewGate:
    """손잡이를 읽어 관문을 만든다. delib_running() 은 지금 도는 심의 수다(app._delib_load()[0])."""
    k = knobs(env)

    def limit_fn():
        if delib_running() > 0:
            return k["with_delib"], "delib_active"
        return k["concurrency"], "normal"

    return ReviewGate(limit_fn, k["retry_after_s"])
