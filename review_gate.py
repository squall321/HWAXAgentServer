# 카드 대조 호출의 동시 수 관문 — 줄을 세우지 않고 자리가 없으면 곧바로 '바쁨' 을 돌려주며, 호출 결과를 /health 에 셀 수를 모은다
"""공유 LLM 을 챗·심의·카드 대조가 나눠 쓴다. 카드 대조는 며칠짜리 원장의 호출이라 한꺼번에 몰리면 챗 첫 응답이
밀린다. 그래서 프로세스 전체에서 동시 수를 묶는다 — 평소 `CARD_REVIEW_CONCURRENCY`(8), **사람이 띄운** 심의가 도는
동안 `CARD_REVIEW_CONCURRENCY_WITH_DELIB`(기본 평소의 절반 — 4). 리스크 앱 러너가 띄운 패널(`delib_opts.started_by:"app"`)은
절반 판정에 세지 않는다(M3.md #26 — 쟁점 패널이 도는 며칠 동안 카드 대조가 반으로 묶이지 않게). update-all 동안의
비우기(scope `all`)면 상한이 0 이다(새 호출을 받지 않는다).

**줄을 세우지 않는다**(C-24). 자리가 없으면 그 자리에서 `Busy` 를 돌려주고, 호출자(리스크 앱)는 아무것도 세지 않고
`retry_after_s` 뒤에 같은 호출을 다시 낸다. 기다리는 줄이 서버에 없으므로 끊긴 구독이 줄 자리를 쥐고 새는 일이 없다.

asyncio 기본 객체를 쓰지 않는다 — 수만 센다. 그래서 처음 쓴 이벤트 루프에 묶이지 않고(모듈 전역 asyncio 객체가 다른
루프에서 죽는 함정 — deliberation.py 의 관문 주석), 획득·반납이 await 없이 끝난다(획득과 첫 프레임 사이에 끊겨도
자리가 새지 않는다 — 부르는 쪽이 제너레이터 안에서 잡고 finally 에서 돌려준다).

비우기 깃발(DrainState)도 이 파일에 둔다 — 관문이 두 벌이 되지 않게(M3 WP5a A1). 깃발은 파일 하나(재기동을 넘긴다)이고
판정은 읽을 때 한다(따로 도는 시계가 없다 — 앱이 15초마다 /health 를 읽는다). 계약은 docs/risk-review-redesign/M3.md 4.7.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

CONCURRENCY_DEFAULT = 8           # D-34 — 낮 시간 동시 수 8(4 에서)
RETRY_AFTER_DEFAULT = 30
DRAIN_RETRY_AFTER_S = 60          # 비우기 중의 review_busy{reason:"draining"} 대기(M3.md 4.5)
DRAIN_SCOPES = ("all", "panels")  # all = 카드 대조 새 호출도 받지 않는다 · panels = 앱이 새 패널만 집지 않는다
DRAIN_FILE = "review-drain.json"


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
    """관문 손잡이 셋 — {concurrency, with_delib, retry_after_s}. 심의 중 상한의 기본은 평소의 절반(바닥 1)이고, 적으면 그
    값이다. 심의 중 상한이 평소보다 크면 평소 값으로 묶는다(심의가 돌 때 오히려 더 많이 받는 설정은 뜻이 거꾸로다).
    손잡이 하나(CARD_REVIEW_CONCURRENCY)를 낮추면 심의 중 값도 따라 내려간다."""
    conc = _env_int(env, "CARD_REVIEW_CONCURRENCY", CONCURRENCY_DEFAULT, 1)
    busy = _env_int(env, "CARD_REVIEW_CONCURRENCY_WITH_DELIB", max(1, conc // 2), 1)
    if busy > conc:
        print(f"[review_gate] CARD_REVIEW_CONCURRENCY_WITH_DELIB={busy} 가 CARD_REVIEW_CONCURRENCY={conc} 보다 크다 — "
              f"{conc} 로 묶는다")
        busy = conc
    return {"concurrency": conc, "with_delib": busy,
            "retry_after_s": _env_int(env, "CARD_REVIEW_RETRY_AFTER_S", RETRY_AFTER_DEFAULT, 0)}


def limit_for(concurrency: int, with_delib: int, delib_human: int, drain_scope: str | None) -> tuple[int, str]:
    """관문 상한 표(순수) — (상한, 사유). 비우기 scope all 이면 (0, draining), 사람이 띄운 심의가 하나라도 돌면
    (with_delib, delib_human), 아니면 (concurrency, normal). 비우기 scope panels 는 카드 대조를 막지 않는다(앱이 새 패널만
    집지 않는다). 리스크 앱 러너의 패널(started_by=app)은 delib_human 에 들지 않는다 — 부르는 쪽이 센다."""
    if drain_scope == "all":
        return 0, "draining"
    if int(delib_human or 0) > 0:
        return with_delib, "delib_human"
    return concurrency, "normal"


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
# busy·over_budget 은 자리를 잡기 전에 돌려보낸 수다(busy 는 비우기로 돌려보낸 것도 센다).
TOTAL_KEYS = ("calls", "ok", "error", "cancelled", "busy", "truncated", "repaired", "over_budget", "context_overflow",
              "tokens_in", "tokens_out", "tokens_unknown_calls")


class ReviewGate:
    """limit_fn() -> (상한, 사유) 를 획득 때마다 부른다 — 심의가 시작·끝나거나 비우기가 서면 다음 획득부터 상한이 바뀐다.
    이미 잡은 자리는 상한이 내려가도 빼앗지 않는다(도는 호출을 끊지 않는다). delib_human 은 /health 에 싣는 수(사람이 띄운
    도는 심의)를 주는 함수다 — 없으면 0."""

    def __init__(self, limit_fn, retry_after_s: int = RETRY_AFTER_DEFAULT, *, delib_human=None, drain=None):
        self._limit_fn = limit_fn
        self.retry_after_s = int(retry_after_s)
        self.delib_human = delib_human
        self.drain = drain
        self.active = 0
        self.totals = dict.fromkeys(TOTAL_KEYS, 0)
        self.last_error: str | None = None

    def limit(self) -> tuple[int, str]:
        """(상한, 사유). 0 은 비우기(draining)일 때만 낸다 — 그 밖의 0·음수는 1 로 연다(관문이 손잡이 실수로 닫히지 않는다)."""
        try:
            n, why = self._limit_fn()
            why = str(why)
            if why == "draining":
                return 0, why
            return max(1, int(n)), why
        except Exception as exc:  # noqa: BLE001 — 상한을 못 읽어도 관문은 닫히지 않는다(가장 좁게 연다)
            return 1, f"limit_error:{type(exc).__name__}"

    def try_acquire(self, tag: str = "") -> Ticket | Busy:
        """기다리지 않는다 — 자리가 있으면 Ticket, 없으면 Busy. 비우기면 retry_after_s 가 DRAIN_RETRY_AFTER_S 다."""
        lim, why = self.limit()
        if self.active >= lim:
            self.totals["busy"] += 1
            return Busy(why, lim, self.active, DRAIN_RETRY_AFTER_S if why == "draining" else self.retry_after_s)
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

    def human_delibs(self) -> int:
        """사람이 띄운 도는 심의 수(/health.card_review.delib_human). 못 세면 0 이 아니라 -1 이다(모름)."""
        if self.delib_human is None:
            return 0
        try:
            return int(self.delib_human())
        except Exception:  # noqa: BLE001
            return -1

    def snapshot(self) -> dict:
        lim, why = self.limit()
        return {"limit": lim, "limit_reason": why, "active": self.active, "retry_after_s": self.retry_after_s,
                "delib_human": self.human_delibs(), "totals": dict(self.totals), "last_error": self.last_error}


# ── 비우기 깃발(update-all 동안) ──────────────────────────────────────────────
def drain_state_path(env=os.environ) -> str:
    """깃발 파일 — DRAIN_STATE_PATH, 없으면 <ARTIFACT_DIR>/review-drain.json(ARTIFACT_DIR 가 없으면 이 리포의 artifacts/ —
    app.py 의 기본값과 같다). delib_jobs 를 모듈 머리에서 가져오지 않는다(/health 가 감싼 실패가 기동 실패로 바뀐다 — P-17)."""
    own = str(env.get("DRAIN_STATE_PATH") or "").strip()
    if own:
        return own
    base = str(env.get("ARTIFACT_DIR") or "").strip() or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                      "artifacts")
    return os.path.join(base, DRAIN_FILE)


class DrainError(ValueError):
    """비우기 요청이 계약에 안 맞거나(422 bad_request) 사람의 고정·다른 주체의 깃발에 막혔다(409 drain_held)."""

    def __init__(self, code: str, message: str, status: int, snapshot: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.snapshot = code, message, status, snapshot

    def body(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.snapshot is not None:
            out["drain"] = self.snapshot
        return out


OFF = {"on": False, "scope": None, "until": None, "until_idle": None, "hold": False, "by": None, "reason": None,
       "set_at": None}


class DrainState:
    """비우기 깃발 하나(파일). 규칙(M3.md 4.7 · engine-ops 계약 9).

    - 수명은 늘리기만 한다 — ttl 은 until = max(지금 until, now + ttl). until_idle 은 심의 합(delib_load)이 처음 0 이 된 때
      (idle_since)부터 grace_s 뒤에 풀리고, 그 전에 심의가 다시 서면 idle_since 를 지운다. until_idle 은 어떤 경우에도 세운 때
      + max_s 를 넘기지 않는다. 둘이 다 있으면 둘 다 끝나야 풀린다(나중 요청이 앞 수명을 줄이지 않는다).
    - scope 는 마지막으로 세운 요청의 것이다(update-all 끝맺음이 all → panels 로 바꾼다).
    - hold 깃발은 hold 아닌 요청으로 바뀌지 않는다(409 drain_held). 끄기는 세운 주체(by)와 같아야 하고, 다르거나 hold 면
      force 가 있어야 한다. 수명 없는 깃발은 hold 만 받는다(사람이 풀 때까지).
    - 판정은 읽을 때 한다. 깨진 파일은 '꺼짐' 으로 읽고 지우며 한 줄 남긴다. 쓰기는 원자적이다(임시 파일 → os.replace).
    clock 은 epoch 초(time.time), delib_load() 는 (도는, 줄 선) 심의 수다(시험이 둘 다 주입한다)."""

    def __init__(self, path: str, *, clock=time.time, delib_load=None):
        self.path = path
        self.clock = clock
        self.delib_load = delib_load

    # 파일 ───────────────────────────────────────────────────────────────────
    def _read(self) -> dict | None:
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            print(f"[drain] 깃발 파일을 읽지 못했다({type(exc).__name__}) — 꺼짐으로 읽는다: {self.path}")
            return None
        try:
            st = json.loads(raw)
            if not isinstance(st, dict) or st.get("scope") not in DRAIN_SCOPES:
                raise ValueError("모양이 다르다")
            for k in ("until", "set_at"):
                if st.get(k) is not None and not isinstance(st[k], (int, float)):
                    raise ValueError(f"{k} 가 수가 아니다")
            ui = st.get("until_idle")
            if ui is not None and not (isinstance(ui, dict) and isinstance(ui.get("grace_s"), (int, float))
                                       and isinstance(ui.get("max_s"), (int, float))):
                raise ValueError("until_idle 이 {grace_s, max_s} 가 아니다")
            return st
        except ValueError as exc:
            print(f"[drain] 깃발 파일이 깨졌다({exc}) — 꺼짐으로 읽고 지운다: {self.path}")
            self._remove()
            return None

    def _write(self, st: dict) -> None:
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        tmp = f"{self.path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _remove(self) -> None:
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[drain] 깃발 파일을 지우지 못했다({type(exc).__name__}): {self.path}")

    def _delib_sum(self) -> int | None:
        if self.delib_load is None:
            return 0
        try:
            a, q = self.delib_load()
            return int(a) + int(q)
        except Exception:  # noqa: BLE001 — 모르면 풀지 않는다(심의가 도는 쪽으로 읽는다)
            return None

    # 판정 ───────────────────────────────────────────────────────────────────
    def _live(self, st: dict, now: float) -> bool:
        """깃발이 아직 살아 있나 — until_idle 의 idle_since 는 여기서 고친다(바뀌면 파일에 쓴다)."""
        alive, any_life = False, False
        if st.get("until") is not None:
            any_life = True
            alive = alive or now < float(st["until"])
        ui = st.get("until_idle")
        if ui is not None:
            any_life = True
            cap = float(st.get("set_idle_at") or st.get("set_at") or now) + float(ui["max_s"])
            if now < cap:
                n = self._delib_sum()
                if n is None or n > 0:
                    if ui.get("idle_since") is not None:
                        ui["idle_since"] = None
                        self._write(st)
                    alive = True
                else:
                    if ui.get("idle_since") is None:
                        ui["idle_since"] = now
                        self._write(st)
                    if now < float(ui["idle_since"]) + float(ui["grace_s"]):
                        alive = True
        if not any_life:
            return bool(st.get("hold"))           # 수명 없는 깃발은 사람의 고정뿐이다 — 풀 때까지 산다
        return alive

    def _current(self) -> dict | None:
        st = self._read()
        if st is None:
            return None
        if not self._live(st, self.clock()):
            self._remove()
            print(f"[drain] 깃발 수명이 끝나 풀었다(by {st.get('by')}, scope {st.get('scope')})")
            return None
        return st

    @staticmethod
    def _view(st: dict | None) -> dict:
        if st is None:
            return dict(OFF)
        ui = st.get("until_idle")
        return {"on": True, "scope": st["scope"], "until": st.get("until"),
                "until_idle": None if ui is None else {
                    "grace_s": ui["grace_s"], "max_s": ui["max_s"], "idle_since": ui.get("idle_since"),
                    "max_until": float(st.get("set_idle_at") or st.get("set_at") or 0) + float(ui["max_s"])},
                "hold": bool(st.get("hold")), "by": st.get("by"), "reason": st.get("reason"), "set_at": st.get("set_at")}

    def snapshot(self) -> dict:
        """{on, scope, until, until_idle, hold, by, reason, set_at} — 꺼져 있으면 on=false 와 빈 칸."""
        return self._view(self._current())

    def active_scope(self) -> str | None:
        st = self._current()
        return None if st is None else st["scope"]

    # 쓰기 ───────────────────────────────────────────────────────────────────
    def set_on(self, *, scope: str, by: str, reason: str = "", ttl_s: float | None = None,
               until_idle: dict | None = None, hold: bool = False) -> dict:
        """깃발을 세우거나 늘린다 — 지금 깃발(snapshot 꼴)을 돌려준다. 계약에 안 맞으면 DrainError(422), 사람이 고정한 깃발에
        hold 아닌 요청이면 DrainError(409 drain_held)."""
        if scope not in DRAIN_SCOPES:
            raise DrainError("bad_request", f"scope 는 {'·'.join(DRAIN_SCOPES)} 가운데 하나다", 422)
        by = str(by or "").strip()
        if not by:
            raise DrainError("bad_request", "by(세운 주체)가 비었다 — 끄는 쪽이 같은 이름으로 끈다", 422)
        if ttl_s is not None and until_idle is not None:
            raise DrainError("bad_request", "ttl_s 와 until_idle 은 함께 주지 않는다", 422)
        if ttl_s is not None and not (isinstance(ttl_s, (int, float)) and not isinstance(ttl_s, bool) and ttl_s > 0):
            raise DrainError("bad_request", "ttl_s 는 0 보다 큰 초다", 422)
        if until_idle is not None:
            g, m = (until_idle or {}).get("grace_s"), (until_idle or {}).get("max_s")
            if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 for x in (g, m)) or g > m:
                raise DrainError("bad_request", "until_idle 은 {grace_s, max_s}(0 보다 큰 초, grace_s ≤ max_s) 다", 422)
        if ttl_s is None and until_idle is None and not hold:
            raise DrainError("bad_request", "수명(ttl_s 또는 until_idle)이 없다 — 수명 없는 깃발은 사람의 고정(hold)뿐이다",
                             422)
        now = self.clock()
        cur = self._current()
        if cur is not None and cur.get("hold") and not hold:
            raise DrainError("drain_held", f"사람이 고정한 비우기 깃발이다(by {cur.get('by')}) — hold 아닌 요청으로 바꾸지 "
                             "않는다", 409, self._view(cur))
        st = dict(cur or {})
        st.update(scope=scope, by=by, reason=str(reason or "")[:300], hold=bool(hold or (cur or {}).get("hold")))
        if cur is None:
            st["set_at"] = now
            st["until"], st["until_idle"] = None, None
        if ttl_s is not None:
            st["until"] = max(float(st.get("until") or 0), now + float(ttl_s))
        if until_idle is not None:
            old = st.get("until_idle")
            if old is None:
                st["until_idle"] = {"grace_s": float(until_idle["grace_s"]), "max_s": float(until_idle["max_s"]),
                                    "idle_since": None}
                st["set_idle_at"] = now
            else:                                  # 늘리기만 — 창은 큰 쪽, 상한 시각도 늦은 쪽
                old_cap = float(st.get("set_idle_at") or st.get("set_at") or now) + float(old["max_s"])
                new_cap = now + float(until_idle["max_s"])
                old["grace_s"] = max(float(old["grace_s"]), float(until_idle["grace_s"]))
                if new_cap > old_cap:
                    st["set_idle_at"], old["max_s"] = now, float(until_idle["max_s"])
        self._write(st)
        return self._view(st)

    def set_off(self, *, by: str, force: bool = False) -> dict:
        """깃발을 내린다 — 내린 뒤의 snapshot(꺼짐)을 돌려준다. 이미 꺼져 있으면 그대로 꺼짐이다(멱등). 사람이 고정했거나 다른
        주체가 세운 깃발은 force 가 있어야 내린다(DrainError 409 drain_held)."""
        cur = self._current()
        if cur is None:
            return dict(OFF)
        by = str(by or "").strip()
        if not force and (cur.get("hold") or cur.get("by") != by):
            why = "사람이 고정한 깃발이다" if cur.get("hold") else f"다른 주체(by {cur.get('by')})가 세운 깃발이다"
            raise DrainError("drain_held", f"{why} — 내리려면 force 를 준다", 409, self._view(cur))
        self._remove()
        print(f"[drain] 깃발을 내렸다(by {by}{', force' if force else ''})")
        return dict(OFF)


def from_env(delib_human, env=os.environ, *, drain: DrainState | None = None) -> ReviewGate:
    """손잡이를 읽어 관문을 만든다. delib_human() 은 지금 도는 **사람이 띄운** 심의 수다(리스크 앱 러너의 패널은 빼고 —
    #26). drain 을 주면 그 깃발의 scope 를 획득 때마다 읽는다(all 이면 상한 0)."""
    k = knobs(env)

    def limit_fn():
        scope = drain.active_scope() if drain is not None else None
        return limit_for(k["concurrency"], k["with_delib"], delib_human(), scope)

    return ReviewGate(limit_fn, k["retry_after_s"], delib_human=delib_human, drain=drain)
