# 외부 응답자(Claude Code 워크플로)를 엔진의 LLM 자리에 꽂는다 — 사람별 과제 줄 · LangChain 채팅 모델 · 기다림/답 한도(기본 꺼짐)
"""외부 응답자 — 리스크 심사를 Claude 로도 돌리는 길(HWAXPortal `docs/risk-claude-engine/` X-1).

엔진의 판단 호출(카드 대조 다섯 종류·심의 좌석/의장)이 LLM 에 묻는 자리에 `ExternalChatModel` 을 꽂으면, 그 호출은 **과제**로
줄을 서고 답을 기다린다. 잡 주인의 Claude Code 워크플로가 MCP 도구(`review_task_next` · `review_task_read` ·
`review_task_submit`)로 과제를 집어 프롬프트를 읽고 답을 낸다. 낸 글은 GLM 이 낸 글과 똑같이 엔진의 파서·검사·보충을 지난다 —
바뀌는 것은 '누가 답을 쓰나' 뿐이다(X-D3).

규칙.
1. 기본 꺼짐 — `EXTERNAL_LLM_ENABLED=1` 이 아니면 줄도 도구도 없고, `llm: "external"` 요청은 거절된다(사용자 결정 X-D1).
   켰는데 `EXTERNAL_LLM_*` 값 하나라도 못 읽거나 범위 밖이거나 시간 한도 사슬이 깨졌으면 기본값으로 메우지 않고 꺼진 채
   기동 오류 줄을 남긴다(닫힌 쪽으로 — config).
2. 주인만 — 과제는 그 호출을 낸 사람(owner)의 워크플로만 본다. 남의 과제 id 는 없는 id 와 같은 답을 받는다. 주인 이름은
   앞뒤 공백을 떼고 소문자로만 맞춘다 — 퍼센트 인코딩을 푸는 것은 게이트웨이 신원 헤더(MCP 호출자 길)뿐이다(header_owner).
3. 집기마다 임대(lease)가 붙는다 — 읽기·내기는 그 임대를 실어야 하고, 되돌려졌다 다시 집힌 과제에 묵은 임대로 낸 조각은
   버퍼에 닿지 않고 not_claimed 다(두 일꾼의 답이 한 답으로 이어 붙지 않는다).
4. 끝없이 기다리지 않는다 — 집히지 않으면 `external_worker_absent`, 집고 답이 없으면 한 번 줄로 되돌리고 그다음
   `external_answer_timeout`. 둘 다 인프라 실패라 엔진의 기존 오류 길(카드 대조 error 프레임·심의 좌석 유실/의장 실패)로 나간다.
5. 본문은 남기지 않는다 — 로그에는 과제 id·종류·크기만 적는다(프롬프트·답 글은 적지 않는다).
6. 줄은 메모리에만 있다 — 재기동하면 사라진다(도는 호출의 스트림이 끊기므로 앱이 인프라로 다시 낸다).
7. 길이·자리는 전부 유니코드 코드 포인트(파이썬 글자)로 센다 — JS 의 String.length(UTF-16 단위)와 BMP 밖 글자에서 갈린다.

시간 한도 사슬(안쪽이 먼저) — 과제 하나의 최악은 기다림 + 답 + (되돌린 뒤) 기다림 + 답 = 2×WAIT_S + 2×ANSWER_S 이고, 이것이
엔진의 LLM 논리 호출 1회 최악((1 + DELIB_LLM_MAX_RETRIES) × DELIB_TIMEOUT_S + 8 = 3,608초)보다 작아야 바깥 한도(리스크 앱의 호출
벽시계·패널 벽시계, 포털·nginx 침묵 한도)가 그대로 맞는다. 기본값 300·1,200 이면 3,000초다. 예외 하나 — 주인의 워크플로가 다른
과제를 집어 쓰는 중이면(바쁜 것이지 없는 것이 아니다) 기다리는 과제의 '집히지 않은 시간' 은 세지 않는다. 그때 한 호출의 기다림은
워크플로의 처리 속도에 달렸고 바깥 벽시계가 마지막 그물이다.
"""
from __future__ import annotations

import asyncio
import functools
import contextvars
import logging
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import call_trace


log = logging.getLogger("agent.external_llm")

# ── 손잡이 ───────────────────────────────────────────────────────────────────
ENABLED_ENV = "EXTERNAL_LLM_ENABLED"
CTX_ENV, MAX_TOKENS_ENV = "EXTERNAL_LLM_CTX_TOKENS", "EXTERNAL_LLM_MAX_TOKENS"
WAIT_ENV, ANSWER_ENV = "EXTERNAL_LLM_WAIT_S", "EXTERNAL_LLM_ANSWER_S"
MAX_INFLIGHT_ENV = "EXTERNAL_LLM_MAX_INFLIGHT"
# 외부 모델의 창·출력 상한(토큰) — 카드 대조의 예산(limits·plan)과 외부 심의의 창 유도가 이 값으로 셈한다. 실제 모델(사용자가 돌리는
# Claude)보다 크게 잡으면 그 모델이 못 받는 프롬프트가 나간다 — 작게 잡는 쪽이 안전하다.
CTX_DEFAULT = 200_000
MAX_TOKENS_DEFAULT = 32_000
# 집히지 않은 채(주인의 워크플로가 아무 과제도 집거나 내지 않는 동안) 기다리는 한도(초)와, 집은 뒤 답을 다 내는 한도(초).
WAIT_DEFAULT = 300.0
ANSWER_DEFAULT = 1200.0
# 한 사람이 동시에 낼 수 있는 외부 응답자 카드 대조 호출 수 — 외부 호출은 GLM 관문 자리를 잡지 않으므로(사람 속도의 일꾼을 기다리는
# 동안 남의 GLM 카드 대조를 막지 않게) 이것이 그 길의 상한이다. 넘으면 review_busy{reason: external_per_owner}.
MAX_INFLIGHT_DEFAULT = 8
NEXT_WAIT_MAX = 25.0              # review_task_next 의 긴 폴링 상한(초) — 게이트웨이 호출 한도(600)보다 훨씬 짧게
POLL_S = 0.5                      # 긴 폴링이 다시 집어 보는 간격(초)
READ_PAGE_MAX = 20_000            # review_task_read 응답 하나의 글자 상한(FastMCP 가 내보내는 JSON 글 그대로 잰다)
ANSWER_HARD_MAX = 2_000_000       # 나눠 받는 답의 글자 상한(메모리 보호 — max_tokens 로 자르기 전의 원문)
OWNER_IDLE_S = 86_400.0           # 과제도 없이 이만큼 조용한 주인 칸은 지운다(워크플로 쪽 호출마다 칸이 생긴다 — 쌓이지 않게)
_PRUNE_EVERY_S = 60.0             # 위 정리를 다시 보는 간격(줄의 시계로)
# 심의 LLM 논리 호출 1회의 최악을 셈하는 값 — app.py lifespan 이 심의 LLM 을 만드는 env·기본값과 같다(시험이 기동 절차로 맞댄다).
DELIB_TIMEOUT_ENV, DELIB_RETRIES_ENV = "DELIB_TIMEOUT_S", "DELIB_LLM_MAX_RETRIES"
DELIB_TIMEOUT_DEFAULT, DELIB_RETRIES_DEFAULT = 1800.0, 1

KINDS = ("sweep", "verdict", "offcard", "noinput", "summary", "deliberation")
# 워크플로가 부르는 과제 도구 — 이 서버의 챗·심의 에이전트는 이 이름을 묶지도 invoke_tool 로 부르지도 않는다(app._AGENT_DENY_TOOLS —
# 주입된 지시를 받은 GLM 턴이 사용자의 기다리는 과제를 집거나 답을 내지 못하게).
TASK_TOOLS = ("review_task_next", "review_task_read", "review_task_submit")

# 스트림 안 오류 — {code: (infra, retryable)}. 둘 다 내용 탓이 아니다(앱은 세지 않고 잡을 external_worker 로 멈춘다 — X-2).
ERRORS = {
    "external_worker_absent": (True, True),
    "external_answer_timeout": (True, True),
}
# 스트림을 열기 전의 거절 — {code: HTTP 상태}. 꺼진 서버는 409(요청이 틀린 것이 아니라 서버가 그 길을 안 연다), 반출 동의 표지
# (consent_external: true)가 없는 외부 호출은 422(과제를 만들기 전에 — 글이 밖으로 나가는 요청마다 실어야 한다), 신원 없는 외부
# 호출은 422(과제를 누구에게 줄지 모른다 — 포털 중계를 지난 호출에는 늘 있다). 이 차례로 본다.
REFUSALS = {"external_llm_disabled": 409, "external_consent_required": 422, "external_owner_missing": 422}
CONSENT_FIELD = "consent_external"

# 외부 응답자 심의의 창 — 그 심의를 도는 태스크(문맥) 안에서만 app._model_context_tokens() 가 이 값을 돌려준다. 심의의 예산(근거·
# 전사·역할 몫)은 전부 창에서 유도되는데 GLM 창(1백만 토큰)으로 짠 프롬프트는 외부 모델이 받지 못한다.
ctx_override: contextvars.ContextVar[int | None] = contextvars.ContextVar("external_llm_ctx", default=None)
# 외부 응답자 심의의 과제 주인 — 심의 입구(deliberation._external_entry)가 세운다. MCP 로 연 심의는 게이트웨이 헤더를 푼 이름
# (header_owner), 포털 /chat 심의는 원문 신원이다. 잡 기록·도구 호출 신원(user)은 종전 그대로 두고 과제의 주인만 이것으로 맞춘다.
task_owner: contextvars.ContextVar[str | None] = contextvars.ContextVar("external_llm_owner", default=None)

_ON_WORDS = ("1", "true", "yes", "on")
_OFF_WORDS = ("", "0", "false", "no", "off")
_warned: set = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(f"[external_llm] {msg}")


def enabled(env=os.environ) -> bool:
    """EXTERNAL_LLM_ENABLED — 1·true·yes·on 이면 켜짐. 비거나 0·false·no·off 면 꺼짐이고, 그 밖의 값(줄 끝 설명·오타)도 꺼짐으로
    읽되 한 번 알린다 — 반출이 걸린 스위치라 못 읽으면 닫힌 쪽으로 읽는다."""
    raw = str(env.get(ENABLED_ENV) or "").strip().lower()
    if raw in _ON_WORDS:
        return True
    if raw not in _OFF_WORDS:
        _warn_once(f"enabled:{raw}", f"env {ENABLED_ENV}='{raw[:40]}' 를 읽지 못했다 — 꺼짐으로 돈다(켜려면 1)")
    return False


class ConfigError(ValueError):
    """EXTERNAL_LLM_* 값 하나를 받을 수 없다 — knob 은 그 설정 이름이다(켜진 서버는 이것이 나면 그 길을 열지 않는다)."""

    def __init__(self, knob: str, message: str):
        super().__init__(message)
        self.knob, self.message = knob, message


def _num(env, name: str, default: float, *, floor: float, integer: bool) -> float:
    """설정 하나 — 비면 기본값. 못 읽거나(정수 자리의 소수 포함) 유한한 수가 아니거나(nan·inf) 셈할 수 없을 만큼 크거나 floor
    미만이면 ConfigError 다 — 기본값으로 메우지 않는다(틀린 값 대신 더 큰 기본 한도로 말없이 돌던 것을 닫는다)."""
    raw = str(env.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = int(raw) if integer else float(raw)
    except ValueError:
        raise ConfigError(name, f"{name}='{raw[:40]}' 를 {'정수' if integer else '수'}로 읽지 못했다") from None
    try:
        finite = math.isfinite(v)
    except ArithmeticError:     # OverflowError — 정수로는 읽혔지만 float 로 바꿀 수 없을 만큼 크다(기동이 넘어지던 자리)
        raise ConfigError(name, f"{name}={raw[:40]}…({len(raw):,}자리) 는 셈할 수 없을 만큼 크다") from None
    if not finite:
        raise ConfigError(name, f"{name}={raw[:40]} 는 유한한 수가 아니다")
    if v < floor:
        raise ConfigError(name, f"{name}={raw[:40]} 는 {floor:g} 미만이다(한도를 끄는 값은 없다 — 끄면 호출이 끝없이 기다린다)")
    return v


@dataclass(frozen=True)
class Settings:
    ctx_tokens: int = CTX_DEFAULT
    max_tokens: int = MAX_TOKENS_DEFAULT
    wait_s: float = WAIT_DEFAULT
    answer_s: float = ANSWER_DEFAULT
    max_inflight: int = MAX_INFLIGHT_DEFAULT

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        """설정을 읽는다 — 값 하나라도 받을 수 없으면 ConfigError(기본값으로 메우지 않는다)."""
        return cls(ctx_tokens=int(_num(env, CTX_ENV, CTX_DEFAULT, floor=1024, integer=True)),
                   max_tokens=int(_num(env, MAX_TOKENS_ENV, MAX_TOKENS_DEFAULT, floor=1, integer=True)),
                   wait_s=_num(env, WAIT_ENV, WAIT_DEFAULT, floor=0.001, integer=False),
                   answer_s=_num(env, ANSWER_ENV, ANSWER_DEFAULT, floor=0.001, integer=False),
                   max_inflight=int(_num(env, MAX_INFLIGHT_ENV, MAX_INFLIGHT_DEFAULT, floor=1, integer=True)))

    @property
    def call_worst_s(self) -> float:
        """과제 하나(논리 호출 1회)의 최악 — 기다림 · 답 · 되돌린 뒤 기다림 · 답. 주인의 워크플로가 바쁜 동안의 기다림은 들지 않는다."""
        return 2 * self.wait_s + 2 * self.answer_s


def delib_call_worst_s(env=os.environ) -> float | None:
    """심의 LLM 논리 호출 1회의 최악(초) — (1 + DELIB_LLM_MAX_RETRIES) × DELIB_TIMEOUT_S + 8. app.py lifespan 이 심의 LLM 을 만드는
    값과 같은 규칙으로 읽는다(못 읽으면 기본값 1800초·1회, 재시도 음수는 0). 한도가 꺼져 있으면(DELIB_TIMEOUT_S ≤ 0) None 이다 —
    그때는 견줄 바깥 한도가 없다."""
    def read(name, default, conv):
        raw = str(env.get(name) or "")
        if not raw:
            return default
        try:
            return conv(raw)
        except ValueError:
            return default

    to = read(DELIB_TIMEOUT_ENV, DELIB_TIMEOUT_DEFAULT, float)
    tries = max(0, read(DELIB_RETRIES_ENV, DELIB_RETRIES_DEFAULT, int)) + 1
    return tries * to + 8 if to > 0 else None


@dataclass(frozen=True)
class Config:
    """외부 응답자 길의 기동 판정 — settings 가 None 이면 닫혀 있다. 스위치는 켰는데 닫혔으면 error(기동 오류 줄 그대로)와 knob(고칠
    설정 이름)이 있다. 스위치가 꺼져 있으면 셋 다 비어 있다."""
    settings: Settings | None
    error: str = ""
    knob: str = ""


def config(env=os.environ) -> Config:
    """스위치·값·시간 한도 사슬을 한꺼번에 본다(닫힌 쪽으로). 켰는데 값 하나라도 받을 수 없거나, 과제 하나의 최악(2×WAIT_S +
    2×ANSWER_S)이 심의 LLM 논리 호출 1회의 최악 이상이거나 그 최악을 셈할 수 없으면(안쪽이 먼저 걸려야 사유가 먼저 온다) 길을
    열지 않고 기동 오류 줄을 한 번 남긴다 — 종전엔 틀린 값을 기본값(더 큰 한도)으로 메우거나 경고만 하고 열었다. 예외로 새지 않는다
    (import 때 도구 등록과 기동 절차가 이것을 부른다 — 새면 심의 MCP 가 통째로 꺼지거나 기동이 넘어진다)."""
    if not enabled(env):
        return Config(None)
    try:
        s = Settings.from_env(env)
    except ConfigError as exc:
        return _closed(exc.knob, exc.message)
    try:
        outer = delib_call_worst_s(env)
    except ArithmeticError:     # OverflowError — 재시도 횟수가 float 로 바꿀 수 없을 만큼 크다(기동이 넘어지던 자리)
        return _closed(f"{DELIB_RETRIES_ENV} · {DELIB_TIMEOUT_ENV}",
                       f"심의 LLM 논리 호출 1회의 최악((1 + {DELIB_RETRIES_ENV}) × {DELIB_TIMEOUT_ENV} + 8)을 셈할 수 없다 — 값이 "
                       "셈할 수 없을 만큼 크다")
    if outer is not None and s.call_worst_s >= outer:
        return _closed(f"{WAIT_ENV} · {ANSWER_ENV}",
                       f"과제 하나의 최악(2×{WAIT_ENV} + 2×{ANSWER_ENV} = {s.call_worst_s:,.0f}초)이 심의 LLM 논리 호출 1회의 "
                       f"최악((1 + {DELIB_RETRIES_ENV}) × {DELIB_TIMEOUT_ENV} + 8 = {outer:,.0f}초) 이상이다 — 바깥 한도가 그 "
                       "값에 맞춰져 있어 안쪽(외부 응답자)이 먼저 걸려야 한다")
    return Config(s)


def _closed(knob: str, why: str) -> Config:
    msg = (f"{ENABLED_ENV}=1 이지만 외부 응답자 길을 열지 않는다(설정 오류 — 닫힌 쪽으로 돈다): {why}. 고친 뒤 재기동한다")
    _warn_once(f"config:{msg}", f"⛔ {msg}")
    return Config(None, msg, knob)


def is_open(env=os.environ) -> bool:
    """외부 응답자 길이 열렸나 — 스위치가 켜졌고 설정이 맞다(config). 도구 등록·안내 글이 이것을 본다."""
    return config(env).settings is not None


class ExternalLLMError(Exception):
    """외부 응답자의 한도에 걸렸다 — code 는 ERRORS 의 이름, knob 은 그 한도를 바꾸는 설정 이름이다."""

    def __init__(self, code: str, message: str, knob: str = ""):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.knob = code, message, knob


def _absent(s: Settings, *, standing: bool) -> ExternalLLMError:
    if standing:
        msg = (f"외부 응답자가 없다 — 이 사람의 과제가 {s.wait_s:,.0f}초 동안 집히지 않아 없다고 판정한 뒤로, 워크플로가 다시 붙거나"
               f"(review_task_* 를 한 번이라도 부르거나) 그 판정에서 {s.wait_s:,.0f}초가 지날 때까지 새 과제를 줄 세우지 않고 바로 이 "
               "오류로 답한다. Claude Code 워크플로를 다시 돌린 뒤 같은 호출을 다시 낸다")
    else:
        msg = (f"외부 응답자가 이 과제를 {s.wait_s:,.0f}초 동안 집지 않았다 — 그동안 잡 주인의 워크플로가 아무 과제도 집거나 내지 "
               "않았다(돌고 있지 않거나 다른 신원으로 붙었다). Claude Code 워크플로를 잡 주인의 PAT 로 다시 돌린 뒤 같은 호출을 다시 낸다")
    return ExternalLLMError("external_worker_absent", msg, WAIT_ENV)


def _answer_timeout(s: Settings) -> ExternalLLMError:
    return ExternalLLMError(
        "external_answer_timeout",
        f"외부 응답자가 과제를 집고 {s.answer_s:,.0f}초 안에 답을 다 내지 않았다 — 한 번은 줄로 되돌려 다시 집게 했고 다시 집은 뒤에도 "
        "같았다. 답이 이 한도보다 오래 걸리는 모델이면 한도를 올린다", ANSWER_ENV)


def _norm_owner(owner) -> str:
    """주인 이름 맞추기 — 앞뒤 공백을 떼고 소문자로만. 원문 신원(카드 대조의 user_email — 포털이 검증된 주체로 덮어쓴 이메일,
    포털 /chat 심의의 호출자)은 이것만 거친다. 퍼센트 인코딩을 여기서 풀면 '%41' 이 든 원문 주소가 다른 사람('A')의 이름이 된다
    (ev%41l@ → evAl@ — 남의 워크플로가 그 과제를 집었다). 푸는 것은 header_owner 다."""
    return str(owner or "").strip().lower()


def header_owner(raw) -> str:
    """게이트웨이 신원 헤더(x-hwax-user)의 주인 이름 — 게이트웨이는 quote(user, safe="@.") 로 싣는다('+' 가 든 이메일은 %2B,
    '%' 는 %25 로 온다). 한 번 풀어 _norm_owner 와 같은 규칙으로 맞춘다. MCP 호출자 길(과제 도구·MCP 로 연 심의)만 쓴다."""
    return _norm_owner(unquote(str(raw or "")))


# ── 과제 줄 ───────────────────────────────────────────────────────────────────
class _Task:
    """과제 하나 — 기다림(waiting) 또는 집힘(claimed). 끝나면(답·실패·취소) 줄에서 빠진다. lease 는 지금 집기의 임대다(집힐 때마다
    새로, 되돌리면 지운다)."""

    __slots__ = ("id", "owner", "kind", "seq", "parts", "text", "max_tokens", "run_id", "target_key", "label",
                 "created", "created_wall", "state", "unclaimed_since", "claimed_at", "deadline", "claims", "lease", "buf",
                 "received", "fut", "loop")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class _Owner:
    """주인 한 사람의 워크플로 상태 — 마지막으로 보인 때, '없다' 판정 때(그 뒤 WAIT_S 동안만 선다), 마지막 움직임(정리 기준)."""

    __slots__ = ("alive_at", "absent_at", "last")

    def __init__(self, now: float):
        self.alive_at: float | None = None
        self.absent_at: float | None = None
        self.last = now


def _wire(obj) -> str:
    """MCP 응답이 나가는 글 그대로 — FastMCP 가 도구 결과(dict)를 내보내는 직렬화와 같다(mcp_pages.wire)."""
    import mcp_pages  # noqa: PLC0415 — 쪽 나누기 모듈과 같은 잣대를 쓴다(이 모듈을 가볍게 import 하게 늦게 가져온다)

    return mcp_pages.wire(obj)


class Board:
    """사람별 과제 줄 — 엔진 쪽(open·wait·drop)은 이벤트 루프에서, 워크플로 쪽(claim·read·submit)은 MCP 도구에서 부른다.

    상태는 잠금 하나로 지킨다(일꾼 쪽은 다른 스레드에서 불러도 된다 — 시험의 가짜 일꾼이 그렇게 돈다). 답·실패는 그 과제를 연
    이벤트 루프에 call_soon_threadsafe 로 넘긴다. 시계(clock — 단조, wall — 유닉스 초)와 id·임대는 바꿔 끼울 수 있다(시험·계약
    예제). 카드 대조 외부 호출의 사람별 동시 수(call_begin·call_end — EXTERNAL_LLM_MAX_INFLIGHT)도 여기서 센다."""

    def __init__(self, settings: Settings | None = None, *, clock=time.monotonic, wall=time.time, new_id=None,
                 new_lease=None, tick: float | None = None):
        self.settings = settings or Settings()
        self._clock, self._wall = clock, wall
        self._new_id = new_id or (lambda: f"xt-{uuid.uuid4().hex[:16]}")
        self._new_lease = new_lease or (lambda: uuid.uuid4().hex[:16])
        self._lock = threading.Lock()
        self._tasks: dict[str, _Task] = {}
        self._owners: dict[str, _Owner] = {}
        self._calls: dict[str, int] = {}
        self._seq = 0
        self._pruned_at = clock()
        s = self.settings
        # 기다리는 쪽이 한도를 다시 보는 간격(실제 초) — 한도의 1/4(바닥 0.05초, 위 1초). 시계를 바꿔 끼운 시험은 따로 준다.
        self.tick = tick if tick is not None else max(0.05, min(1.0, s.wait_s / 4, s.answer_s / 4))

    # ── 엔진 쪽 ──
    def open(self, *, owner: str, kind: str, parts: list, max_tokens: int | None, run_id: str = "",
             target_key: str = "", label: str = "") -> _Task:
        """과제를 줄 세운다(부르는 쪽은 이벤트 루프 안이다). 주인이 '없다' 로 판정된 뒤 WAIT_S 안이면 줄 세우지 않고 바로
        external_worker_absent."""
        owner = _norm_owner(owner)
        if not owner:
            raise ValueError("외부 응답자 과제에는 주인(호출자 신원)이 있어야 한다")
        loop = asyncio.get_running_loop()
        with self._lock:
            now = self._clock()
            self._sweep(now)
            st = self._owners.get(owner)
            if st is not None and self._standing(st, now):
                raise _absent(self.settings, standing=True)
            if st is None:
                st = self._owners[owner] = _Owner(now)
            st.last = now
            self._seq += 1
            text = "".join(t for _r, t in parts)
            task = _Task(id=self._new_id(), owner=owner, kind=kind, seq=self._seq, parts=list(parts), text=text,
                         max_tokens=max_tokens, run_id=run_id or "", target_key=target_key or "", label=label or "",
                         created=now, created_wall=self._wall(), state="waiting", unclaimed_since=now, claimed_at=None,
                         deadline=None, claims=0, lease=None, buf=[], received=0, fut=loop.create_future(), loop=loop)
            self._tasks[task.id] = task
        log.debug("[external] 과제 %s 줄 섬 kind=%s chars=%d max_tokens=%s", task.id, kind, len(text), max_tokens)
        return task

    async def wait(self, task: _Task) -> tuple[str, str]:
        """답(글, finish_reason)을 기다린다. 한도에 걸리면 ExternalLLMError. 취소되면 그대로 올린다(부른 쪽이 drop 한다)."""
        while True:
            done, _pending = await asyncio.wait({task.fut}, timeout=self.tick)
            if done:
                out = task.fut.result()
                if out[0] == "err":
                    raise out[1]
                return out[1], out[2]
            self.sweep()

    def drop(self, task: _Task) -> None:
        """부른 쪽이 끝났다(답을 받았거나 취소·끊김) — 아직 줄에 있으면 뺀다. 그 뒤 워크플로의 읽기·내기는 없는 과제의 답을 받는다."""
        with self._lock:
            if self._tasks.get(task.id) is task:
                del self._tasks[task.id]
                log.info("[external] 과제 %s 취소 — 부른 쪽이 떠났다(%s · 집힘 %d번)", task.id, task.state, task.claims)

    # ── 카드 대조 외부 호출의 사람별 동시 수 ──
    def call_begin(self, owner: str) -> int | None:
        """외부 응답자 카드 대조 호출 하나가 든다 — 그 주인의 도는 호출이 max_inflight 미만이면 하나 늘리고 None, 찼으면 지금 수."""
        owner = _norm_owner(owner)
        with self._lock:
            n = self._calls.get(owner, 0)
            if n >= self.settings.max_inflight:
                return n
            self._calls[owner] = n + 1
            return None

    def call_end(self, owner: str) -> None:
        owner = _norm_owner(owner)
        with self._lock:
            n = self._calls.get(owner, 0) - 1
            if n > 0:
                self._calls[owner] = n
            else:
                self._calls.pop(owner, None)

    def calls_of(self, owner: str) -> int:
        with self._lock:
            return self._calls.get(_norm_owner(owner), 0)

    def calls_active(self) -> int:
        """도는 외부 응답자 카드 대조 호출 수(모든 주인) — /health 의 review_active 에 더한다(update-all 비우기가 이것을 기다린다)."""
        with self._lock:
            return sum(self._calls.values())

    # ── 워크플로 쪽 ──
    def claim(self, owner: str) -> dict | None:
        """그 주인의 가장 오래된 기다리는 과제를 집는다 — 새 임대(lease)를 실은 머리를 돌려준다. 없으면 None."""
        owner = _norm_owner(owner)
        with self._lock:
            now = self._clock()
            self._touch(owner, now)
            self._sweep(now)
            waiting = [t for t in self._tasks.values() if t.owner == owner and t.state == "waiting"]
            if not waiting:
                return None
            t = min(waiting, key=lambda x: x.seq)
            t.state, t.claimed_at, t.deadline = "claimed", now, now + self.settings.answer_s
            t.claims += 1
            t.lease = str(self._new_lease())
            t.buf, t.received = [], 0
            head = self._head(t, now)
        log.debug("[external] 과제 %s 집힘 kind=%s chars=%d claim=%d", t.id, t.kind, len(t.text), t.claims)
        return head

    def mine_claimed(self, owner: str) -> int:
        owner = _norm_owner(owner)
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.owner == owner and t.state == "claimed")

    def _held(self, owner: str, task_id, lease, now: float) -> _Task | dict:
        """읽기·내기의 문 — 그 과제가 내 것이고 지금 이 임대로 집혀 있으면 과제, 아니면 실패 답(task_not_found · not_claimed).
        잠금을 쥔 채 부른다."""
        self._touch(owner, now)
        self._sweep(now)
        t = self._tasks.get(str(task_id or ""))
        if t is None or t.owner != owner:
            return dict(_NOT_FOUND)
        if t.state != "claimed" or not lease or str(lease) != t.lease:
            return dict(_NOT_CLAIMED)
        return t

    def read(self, owner: str, task_id: str, lease: str, offset: int, page_max: int = READ_PAGE_MAX,
             wire=_wire) -> dict:
        """집은 과제의 프롬프트 한 쪽 — {task_id, offset, next_offset, chars, text}. 응답 글(wire)이 page_max 자 안에 들게 자른다.
        자리·길이는 유니코드 코드 포인트다."""
        owner = _norm_owner(owner)
        with self._lock:
            t = self._held(owner, task_id, lease, self._clock())
            if isinstance(t, dict):
                return t
            text = t.text
        total = len(text)
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= total:
            return {"ok": False, "code": "bad_offset", "message": f"offset 은 0 이상 {total:,} 이하다", "chars": total}

        def page(n: int) -> dict:
            end = min(total, offset + n)
            return {"task_id": t.id, "offset": offset, "next_offset": end if end < total else None, "chars": total,
                    "text": text[offset:end]}

        n = max(1, page_max - 400)
        out = page(n)
        for _ in range(12):           # 줄바꿈·따옴표는 JSON 에서 두 자가 된다 — 잰 길이로 줄여 맞춘다
            size = len(wire(out))
            if size <= page_max or n <= 1:
                break
            n = max(1, int(n * page_max / size) - 64)
            out = page(n)
        return out

    def submit(self, owner: str, task_id: str, lease: str, text: str, offset: int | None, final: bool) -> dict:
        """답 조각을 받는다. offset — None 이면 끝에 붙인다, 0 이면 처음부터 다시, 받은 길이와 같으면 이어 붙인다(그 밖은
        offset_mismatch + received_chars). final 이면 답을 엔진에 넘기고 과제를 끝낸다. 임대가 지금 집기의 것이 아니면 버퍼를
        건드리지 않고 not_claimed 다(되돌려졌다 다시 집힌 과제에 묵은 집기가 낸 조각 — 지우기·붙이기·끝내기 모두)."""
        owner = _norm_owner(owner)
        text = "" if text is None else str(text)
        with self._lock:
            t = self._held(owner, task_id, lease, self._clock())
            if isinstance(t, dict):
                return t
            if offset is not None and offset not in (0, t.received):
                return {"ok": False, "code": "offset_mismatch", "received_chars": t.received,
                        "message": f"서버가 받은 답은 {t.received:,}자(유니코드 코드 포인트)다 — offset={t.received} 부터 다시 "
                                   "낸다(0 이면 처음부터)"}
            if offset == 0:
                t.buf, t.received = [], 0
            if t.received + len(text) > ANSWER_HARD_MAX:
                return {"ok": False, "code": "answer_too_large", "received_chars": t.received, "max_chars": ANSWER_HARD_MAX,
                        "message": f"답이 {ANSWER_HARD_MAX:,}자를 넘는다 — 프롬프트가 요구한 꼴과 max_tokens 안으로 다시 쓴다"}
            t.buf.append(text)
            t.received += len(text)
            if not final:
                return {"ok": True, "task_id": t.id, "done": False, "received_chars": t.received}
            answer = "".join(t.buf)
            del self._tasks[t.id]
        kept, finish = fit_answer(answer, t.max_tokens)
        _resolve(t, ("ok", kept, finish))
        log.info("[external] 과제 %s 답 받음 kind=%s chars=%d finish=%s claim=%d", t.id, t.kind, len(kept), finish,
                 t.claims)
        out = {"ok": True, "task_id": t.id, "done": True, "chars": len(kept), "finish_reason": finish}
        if len(kept) < len(answer):
            out["cut_chars"] = len(answer) - len(kept)
        return out

    # ── 상태 ──
    def sweep(self) -> None:
        with self._lock:
            self._sweep(self._clock())

    def counts(self) -> dict:
        """줄 전체의 수 — 기다림(queued)·집힘(claimed). 누구 것인지는 싣지 않는다(/health 는 인증이 없다)."""
        with self._lock:
            self._sweep(self._clock())
            claimed = sum(1 for t in self._tasks.values() if t.state == "claimed")
            return {"queued": len(self._tasks) - claimed, "claimed": claimed}

    # ── 안(잠금을 쥔 채) ──
    def _touch(self, owner: str, now: float) -> None:
        """워크플로가 불렀다 — 그 주인은 지금 있다('없다' 판정을 푼다)."""
        if owner:
            st = self._owners.get(owner)
            if st is None:
                st = self._owners[owner] = _Owner(now)
            st.alive_at, st.absent_at, st.last = now, None, now

    def _standing(self, st: _Owner, now: float) -> bool:
        """'없다' 판정이 아직 서 있나 — 판정 뒤 WAIT_S 동안만이다(그 뒤의 새 과제는 다시 WAIT_S 를 기다린다). 판정이 영영 서
        있으면 워크플로를 먼저 켜 두지 않은 사람의 새 잡이 며칠 뒤에도 줄도 서지 못하고 곧바로 실패했다."""
        return st.absent_at is not None and now - st.absent_at < self.settings.wait_s

    def _sweep(self, now: float) -> None:
        """한도를 적용한다 — ① 집힌 과제가 있는 주인은 바쁘다(있다) ② 답 한도를 넘긴 집힘은 한 번 되돌리고(임대를 지운다) 두
        번째면 실패 ③ 주인이 WAIT_S 동안 아무것도 집거나 내지 않은 채 기다린 과제는 '없다' 로 실패(그 주인의 기다리는 과제 전부)
        ④ 과제도 없이 하루 넘게 조용한 주인 칸은 지운다."""
        s = self.settings
        for t in self._tasks.values():
            if t.state == "claimed":
                st = self._owners.get(t.owner)
                if st is None:
                    st = self._owners[t.owner] = _Owner(now)
                st.alive_at = st.last = now
        for t in list(self._tasks.values()):
            if t.state == "claimed" and now >= t.deadline:
                if t.claims >= 2:
                    self._fail(t, _answer_timeout(s))
                else:
                    t.state, t.unclaimed_since, t.deadline, t.lease, t.buf, t.received = "waiting", now, None, None, [], 0
                    log.info("[external] 과제 %s 답 한도 %gs 초과 — 줄로 되돌렸다(한 번)", t.id, s.answer_s)
        for t in sorted((x for x in self._tasks.values() if x.state == "waiting"), key=lambda x: x.seq):
            st = self._owners.get(t.owner)
            if st is None:
                st = self._owners[t.owner] = _Owner(now)
            seen = st.alive_at if st.alive_at is not None else -math.inf
            standing = self._standing(st, now)
            if standing or now - max(t.unclaimed_since, seen) >= s.wait_s:
                if not standing:
                    st.absent_at = now
                st.last = now
                self._fail(t, _absent(s, standing=False))
        if now - self._pruned_at >= _PRUNE_EVERY_S:
            self._pruned_at = now
            busy = {t.owner for t in self._tasks.values()}
            for o in [o for o, st in self._owners.items() if o not in busy and now - st.last > OWNER_IDLE_S]:
                del self._owners[o]

    def _fail(self, t: _Task, err: ExternalLLMError) -> None:
        del self._tasks[t.id]
        log.info("[external] 과제 %s 실패 → %s(kind=%s · 집힘 %d번)", t.id, err.code, t.kind, t.claims)
        _resolve(t, ("err", err))

    def _head(self, t: _Task, now: float) -> dict:
        left = max(0.0, t.deadline - now)
        parts, at = [], 0
        for role, txt in t.parts:
            parts.append({"role": role, "start": at, "chars": len(txt)})
            at += len(txt)
        head = {"task_id": t.id, "lease": t.lease, "kind": t.kind, "max_tokens": t.max_tokens,
                "max_chars": max_chars(t.max_tokens), "chars": len(t.text), "parts": parts,
                "answer_deadline": int(self._wall() + left), "answer_s": int(left), "claim": t.claims,
                "created_at": int(t.created_wall)}
        for k in ("run_id", "target_key", "label"):
            if getattr(t, k):
                head[k] = getattr(t, k)
        return head


# 없는 과제 · 내 것이 아닌 과제 · 끝나거나 취소된 과제는 같은 답이다(주인만 — 남의 과제가 있는지도 알리지 않는다).
_NOT_FOUND = {"ok": False, "code": "task_not_found",
              "message": "그런 과제가 없다 — 끝났거나 취소됐거나 내 과제가 아니다. review_task_next 로 다음 과제를 집는다"}
_NOT_CLAIMED = {"ok": False, "code": "not_claimed",
                "message": "이 과제는 지금 이 임대(lease)로 집혀 있지 않다 — 답 한도가 지나 줄로 되돌아갔거나 그 뒤 다시 집혔다(새 "
                           "임대). 이 집기로 낸 조각은 버려졌다. review_task_next 로 다시 집는다"}


def max_chars(max_tokens: int | None) -> int | None:
    """max_tokens 를 엔진의 글자 어림으로 — 한국어 최악 1.05자/토큰(deliberation._EVID_KO_CPT, fit_answer 가 쓰는 토큰 어림
    app._est_tokens_all 의 한글 환산과 같은 수). 이 글자 수(유니코드 코드 포인트) 안의 답은 어떤 글자로 써도 잘리지 않는다."""
    if not max_tokens or max_tokens <= 0:
        return None
    from deliberation import _EVID_KO_CPT  # noqa: PLC0415 — deliberation 이 이 모듈을 붙이므로 부를 때 가져온다

    return int(max_tokens * _EVID_KO_CPT)


def _resolve(t: _Task, out: tuple) -> None:
    """그 과제를 연 루프에 결과를 넘긴다 — 다른 스레드에서도 된다. 이미 끝난(취소된) 기다림이나 닫힌 루프는 조용히 지난다."""
    def _set(f=t.fut):
        if not f.done():
            f.set_result(out)
    try:
        t.loop.call_soon_threadsafe(_set)
    except RuntimeError:                  # 루프가 닫혔다(재기동 중·시험이 끝났다) — 기다리는 쪽이 없다
        pass


def fit_answer(text: str, max_tokens: int | None) -> tuple[str, str]:
    """답을 출력 상한 안으로 — (글, finish_reason). 엔진의 토큰 어림(app._est_tokens_all — 한글 1.05자·그 밖 3.6자/토큰)으로
    max_tokens 를 넘으면 그 자리에서 자르고 length 다(상한에 걸린 모델의 답과 같은 모양 — 엔진의 절단 처리를 그대로 탄다)."""
    if not max_tokens or max_tokens <= 0 or not text:
        return text, "stop"
    from app import _est_tokens_all  # noqa: PLC0415 — app 이 이 모듈을 붙이므로 부를 때 가져온다

    if _est_tokens_all(text) <= max_tokens:
        return text, "stop"
    lo, hi = 0, len(text)            # 어림은 앞에서부터 늘기만 한다 — 상한 안에 드는 가장 긴 앞부분을 이분 탐색으로
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _est_tokens_all(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo], "length"


# ── 앱에 붙이기 ──────────────────────────────────────────────────────────────
def board_of(app) -> Board | None:
    """이 앱의 과제 줄 — 꺼져 있거나 설정이 틀려 닫혔으면(config) None. 처음 쓸 때(기동 때) 설정을 한 번 읽어 만든다
    (card_review.gate_of 와 같은 방식)."""
    if not enabled():
        return None
    state = getattr(app, "state", None)
    b = getattr(state, "external_board", None)
    if b is None:
        cfg = config()
        if cfg.settings is None:
            return None
        b = Board(cfg.settings)
        setattr(state, "external_board", b)
    return b


def model_for(app, *, owner: str, kind: str, run_id: str = "", target_key: str = "",
              label: str = "") -> "ExternalChatModel | None":
    """이 호출 하나의 외부 모델 — 주인·종류를 실은 채 줄에 붙는다. 꺼져 있으면 None."""
    b = board_of(app)
    if b is None:
        return None
    # 호출 추적(CALL_TRACE_URL)이 켜져 있으면 과제 하나의 기다림을 'external' 서버의 호출로 남긴다(꺼져 있으면 그대로).
    return call_trace.instrument_llm(_model_class()(board=b, max_tokens=b.settings.max_tokens, owner=_norm_owner(owner), kind=kind,
                                                    run_id=run_id, target_key=target_key, label=label), "external")


def health_fields(board: Board) -> dict:
    """/health.external_llm — 설정과 수만(과제 글·주인은 싣지 않는다). inflight 는 도는 외부 응답자 카드 대조 호출 수(모든 주인 —
    max_inflight 는 한 사람의 상한)이고 app 이 review_active 에도 더한다. evid_budget 은 app 이 외부 창으로 셈해 더한다."""
    s = board.settings
    return {"ctx_tokens": s.ctx_tokens, "max_tokens": s.max_tokens, "wait_s": s.wait_s, "answer_s": s.answer_s,
            "call_worst_s": s.call_worst_s, "max_inflight": s.max_inflight, "inflight": board.calls_active(),
            **board.counts()}


def startup_note(s: Settings, delib_read: float, delib_tries: int) -> str:
    """기동 줄 — 켜졌다는 것과 시간 한도 사슬. 사슬이 깨진 설정은 config 가 이미 닫았다 — 여기의 경고는 기동한 심의 LLM 에 실제로
    걸린 값(app 이 넘긴다)이 env 로 셈한 것과 다를 때의 그물이다."""
    line = (f"[agent] 외부 응답자(EXTERNAL_LLM_ENABLED) 켜짐 — 창 {s.ctx_tokens:,} · 출력 {s.max_tokens:,} 토큰, 기다림 "
            f"{s.wait_s:g}초 · 답 {s.answer_s:g}초(과제 하나 최악 {s.call_worst_s:,.0f}초) · 카드 대조 동시 호출 한 사람당 "
            f"{s.max_inflight}건({MAX_INFLIGHT_ENV})")
    if delib_read > 0 and delib_tries > 0:
        outer = delib_tries * delib_read + 8
        if s.call_worst_s >= outer:
            line += (f" ⚠ 심의 LLM 논리 호출 1회의 최악({outer:,.0f}초 — 바깥 한도가 이 값에 맞춰져 있다)보다 크거나 같다. "
                     f"{WAIT_ENV}·{ANSWER_ENV} 를 줄인다(안쪽이 먼저 걸려야 사유가 먼저 온다)")
    return line


# ── LangChain 채팅 모델 ──────────────────────────────────────────────────────
def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in content)
    return "" if content is None else str(content)


@functools.lru_cache(maxsize=None)
def _model_class():
    """ExternalChatModel 을 처음 쓸 때 짓는다 — langchain_core 는 그때 읽는다. 이 모듈은 deliberation 이 맨 위에서 읽으므로, 여기서
    langchain 을 읽으면 그것이 없는 가벼운 환경(포털의 계약 시험이 엔진의 _resolve_opts 를 부른다)에서 deliberation 자체를 못 읽는다."""
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from pydantic import PrivateAttr

    class ExternalChatModel(BaseChatModel):
        """엔진이 LLM 을 쓰는 자리에 그대로 들어가는 외부 응답자 — ainvoke([("system", …), ("human", …)]) 와 bind(max_tokens=…)를
        받는다. 부르면 과제를 줄 세우고 낸 글을 AIMessage(content, response_metadata.finish_reason)로 돌려준다. 토큰 사용량은 모른다
        (싣지 않는다 — 0 으로 세지 않는다). 도구를 묶을 수 없다(bind_tools 는 NotImplementedError — 이 길은 evidence_only 다)."""

        max_tokens: int | None = None
        owner: str = ""
        kind: str = "deliberation"
        run_id: str = ""
        target_key: str = ""
        label: str = ""
        _board: Any = PrivateAttr(default=None)

        def __init__(self, board: Board | None = None, **kw):
            super().__init__(**kw)
            self._board = board

        @property
        def _llm_type(self) -> str:
            return "hwax-external"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise NotImplementedError("외부 응답자는 비동기로만 부른다(ainvoke)")

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
            board = self._board
            if board is None:
                raise RuntimeError("외부 응답자 줄이 없다(EXTERNAL_LLM_ENABLED)")
            mt = kwargs.get("max_tokens")
            mt = mt if isinstance(mt, int) and not isinstance(mt, bool) and mt > 0 else self.max_tokens
            parts = [(m.type, _content_text(m.content)) for m in messages]
            task = board.open(owner=self.owner, kind=self.kind, parts=parts, max_tokens=mt, run_id=self.run_id,
                              target_key=self.target_key, label=self.label)
            try:
                text, finish = await board.wait(task)
            finally:
                board.drop(task)
            msg = AIMessage(content=text, response_metadata={"finish_reason": finish, "model_name": "external"})
            return ChatResult(generations=[ChatGeneration(message=msg, generation_info={"finish_reason": finish})])

        def bind_tools(self, tools, **kwargs):
            raise NotImplementedError("외부 응답자는 엔진의 도구를 실행할 수 없다 — 이 길은 좌석 도구 호출이 없는 evidence_only 다")

    return ExternalChatModel


def __getattr__(name: str):
    # `external_llm.ExternalChatModel` 은 그대로 쓴다(PEP 562) — 처음 닿을 때 _model_class() 가 짓는다.
    if name == "ExternalChatModel":
        return _model_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
