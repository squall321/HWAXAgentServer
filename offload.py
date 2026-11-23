# 토큰 절감 2판 — 개인 Claude 의 일을 서버 Qwen · GLM 에 맡긴다: 위임 잡 · 절 초안 · 단순 변환 · 결과에 묻기(스위치 넷, 모두 기본 꺼짐)
"""정본 계약은 HWAXPortal `docs/token-saving/offload-v2.md` 다(3-1 위임 · 3-3 작성 · 변환 · 3-2 의 묻기 창구). 결정은 같은 폴더 context-notes TS-6.

**스위치.** DELEGATE_ENABLED · DRAFT_ENABLED · TRANSFORM_ENABLED · RESULT_ASK_ENABLED — 값이 '1' 일 때만 켜진다. import 때 한 번 읽는다(ON) —
도구 등록(mcp_server._register_offload) · 창구 등록(app) · /health 의 offload 가 같은 값을 본다. 꺼지면 도구 · 창구가 아예 없다(목록 · 응답이
스위치 전과 바이트까지 같다).

**신원 · 시야.** 위임 · 작성의 도구 호출은 MCP 로 연 심의 잡과 같은 길이다 — 게이트웨이 공유 비밀 + 그 호출을 한 사람의 신원 머리
(deliberation._tools_by_name — x-hwax-user · x-hwax-groups). 새 신원 길을 만들지 않는다. 신원 없는 호출은 받지 않는다(서비스 계정 시야로 돌면
'그 사람의 시야' 가 아니다).

**울타리.** 루틴과 같다 — 게이트웨이가 읽기로 **확인한** 도구만(/tools-map 의 routine_read — 표지 · 읽기 목록 · 박스 덧씌우기, 모르면 막는다).
그 위에 이 서버의 도구(hwax-deliberation — 심의 · 위임 자신)와 게이트웨이 로컬 도구(invoke_tool 등)를 뺀다. routine_read 를 모르는 옛
게이트웨이 · 못 읽은 때는 빈 울타리다(닫힌 쪽). 쓰기 · 잡 제출 · 지우기는 묶이지 않는다 — 위임은 조사만 한다.

**숫자.** 답의 수를 근거 원문과 코드로 대조한다(evidence.unsourced_numbers — 챗 · 결정문과 같은 판정). 어긋나면 표시만 하고 고쳐 쓰지 않는다.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from urllib.parse import quote, unquote

import httpx

import external_llm
from evidence import sig_numbers, unsourced_numbers

log = logging.getLogger("agent.offload")

# ── 스위치 ────────────────────────────────────────────────────────────────────────────────────────────────────
SWITCH_ENV = {"delegate": "DELEGATE_ENABLED", "draft": "DRAFT_ENABLED", "transform": "TRANSFORM_ENABLED",
              "result_ask": "RESULT_ASK_ENABLED"}


def switches(env=os.environ) -> dict:
    """{delegate, draft, transform, result_ask} — 값이 '1' 인 것만 참."""
    return {k: str(env.get(v) or "").strip() == "1" for k, v in SWITCH_ENV.items()}


ON = switches()


def health() -> dict:
    """/health 의 offload — 네 스위치의 켜짐(포털 E 의 설정 배치가 '이 박스에서 꺼져 있음' 을 가른다)."""
    return dict(ON)


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        log.warning("env %s=%r 정수 파싱 실패 — 기본값 %d", name, raw[:40], default)
        return default


# ── 상한 ──────────────────────────────────────────────────────────────────────────────────────────────────────
# 위임(3-1) — 호출 수 · 분은 계약의 기본 · 상한. 동시는 전체 DELEGATE_MAX_RUNNING(기본 2) · 사람마다 1 — 심의 · 띵킹과 LLM 을 같이 쓴다(넘치면 줄).
STEPS_DEFAULT, STEPS_MAX = 12, 30
MINUTES_DEFAULT, MINUTES_MAX = 10, 30
_MINUTE_S = 60.0           # 분 → 초(시험이 시간 상자를 짧게 바꾼다)
DELEGATE_MAX_RUNNING = _env_int("DELEGATE_MAX_RUNNING", 2)
if DELEGATE_MAX_RUNNING < 1:
    log.warning("env DELEGATE_MAX_RUNNING=%d 는 1 미만 — 기본값 2 로 읽는다", DELEGATE_MAX_RUNNING)
    DELEGATE_MAX_RUNNING = 2
DELEGATE_PER_USER = 1
TASK_MAX = 8000             # 맡길 일 한 줄의 글자 상한 — 넘으면 거절한다(근거 본문은 일에 싣지 말고 도구로 읽게 한다)
APPS_MAX = 10
ANSWER_MAX = 10_000         # 위임 답 · 결과 회수가 MCP 결과 상한(mcp_pages.PAGE_CHARS 25,000자) 안에 들게
ARGS_SHOW = 300             # 근거 표에 싣는 인자 JSON 의 글자
NUMS_SHOW = 30              # 어긋난 수 목록의 상한(넘으면 mismatched_total 로 센다)
OWN_BACKEND = "hwax-deliberation"       # 이 서버(심의 · 위임)의 게이트웨이 백엔드 키 — 위임이 저를 다시 부르지 않게
GATEWAY_LOCAL = "_gateway"
# 동기 호출(작성 · 변환 · 묻기)의 벽시계 — 게이트웨이 호출 한도(GATEWAY_CALL_TIMEOUT 600초)보다 넉넉히 아래(안쪽이 먼저 걸려야 까닭이 간다)
SYNC_LIMIT_S = 540.0
# 작성(3-3 C)
DRAFT_REFS_MAX = 10
DRAFT_TARGET_DEFAULT, DRAFT_TARGET_MAX = 3000, 20_000
DRAFT_OUT_MAX = 20_000
OUTLINE_MAX = 8000
STYLES = {"report": "보고서 절(사실 · 근거 · 판단을 문단으로)", "memo": "메모(짧은 문단 · 글머리표)",
          "minutes": "회의록(결정 · 할 일 · 논점을 항목으로)", "plain": "꾸밈 없는 평문"}
# 변환(3-3 D)
INSTRUCTION_MAX = 4000
TRANSFORM_OUT_MAX = 20_000
# 묻기(3-2)
QUESTION_MAX = 2000
ASK_ANSWER_DEFAULT, ASK_ANSWER_MIN, ASK_ANSWER_MAX = 4000, 200, 20_000
ASK_PAGES_MAX = 6           # 창이 모자라 쪽마다 읽을 때 쪽 수 상한 — 넘으면 413(동기 호출 벽시계 안에 끝나게)
ASK_CONCURRENCY = 2
ASK_PAGE_CHARS = 0          # LLM 한 번에 실을 쪽 글자 — 0 이면 창에서 유도한다(시험이 작게 바꾼다)
QUOTES_MAX = 8
QUOTE_MIN = 8               # 이보다 짧은 인용은 원문 어디에나 있어 근거가 못 된다
RESULT_PAGE_CHARS = 20_000  # quotes 의 page — 게이트웨이 result_get 의 기본 쪽 크기(page_chars=20000)로 센 쪽 번호
_RESERVE_TOKENS = 6000      # 창에서 프롬프트 머리 · 출력 몫으로 떼는 토큰(쪽 글자 셈)
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HALT = "✖"


class OffloadError(ValueError):
    """부르는 쪽이 고칠 수 있는 거절 · 실패 — 글 그대로 돌려준다(MCP 도구 오류 · HTTP 422)."""


class TooLarge(OffloadError):
    """창 · 쪽 상한을 넘었다 — 줄이라고 알린다(HTTP 413)."""


# ── 시험 이음매(블랙박스 시험이 바꿔 낀다 — 이름을 바꾸지 않는다) ────────────────────────────────────────────────
# GW_TRANSPORT — None 이 아니면 게이트웨이 내부 ref 창구로 가는 HTTP 가 이 전송을 탄다. _person_tools · _read_map · _chat_llm · _glm_llm 은
# 비동기여도 동기여도 된다(부르는 쪽이 기다릴 것만 기다린다).
GW_TRANSPORT: httpx.AsyncBaseTransport | None = None


def _app():
    import app as _a  # noqa: PLC0415 — app 이 이 모듈을 붙인다(순환 import 회피 — routine._app 과 같다)
    return _a


async def _maybe(v):
    return await v if inspect.isawaitable(v) else v


async def _person_tools(app, groups: list, user: str) -> dict:
    """그 사람의 게이트웨이 도구 {노출 이름: 도구} — MCP 로 연 심의 잡과 같은 길(deliberation._tools_by_name: 공유 비밀 + 신원 머리, 사람 PAT
    없음 · 챗과 같은 결과 래퍼 _prep_tool). 결과 상한은 기본(TOOL_RESULT_MAX) — 숫자 대조가 원문을 본다(LLM 에는 따로 줄여 보인다)."""
    import deliberation  # noqa: PLC0415
    return await deliberation._tools_by_name(app, list(groups or []), user=user)


async def _read_map(app) -> tuple[dict, frozenset | None]:
    """(노출 이름 → 백엔드 키, 게이트웨이가 읽기로 확인한 노출 이름 또는 None) — /tools-map(app._tools_map 5분 캐시, 블로킹이라 스레드로)."""
    A = _app()
    m = dict(await asyncio.to_thread(A._tools_map) or {})
    rr = A._TOOLS_MAP_CACHE.get("routine_read")
    return m, (rr if isinstance(rr, frozenset) else None)


async def _chat_llm(app):
    """(챗 LLM — 넘김 GLM, 창 덮어쓰기 토큰 또는 None) — 루틴 실행기와 같은 셈(routine._routine_llm 을 늘 chat 으로)."""
    import routine  # noqa: PLC0415
    return await routine._routine_llm(app, "chat")


def _glm_llm(app):
    """GLM — 긴 글 · 결정문과 같은 자리(심의 LLM)."""
    return getattr(app.state, "delib_llm", None) or app.state.llm


# ── 도우미 ────────────────────────────────────────────────────────────────────────────────────────────────────
def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _text(v) -> str:
    """도구 결과 · 메시지 본문을 글로(deliberation._as_text 와 같은 셈 — 그 모듈을 늦게 읽지 않으려 여기 둔다)."""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in v)
    c = getattr(v, "content", None)
    if c is not None:
        return _text(c)
    try:
        return json.dumps(v, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(v)


def _clamp(v, default: int, lo: int, hi: int) -> tuple[int, str]:
    """(값, 죈 사실) — 비면 기본값. 범위 밖이면 그 끝으로 죄고 그렇다고 적는다."""
    if v is None or v == "":
        return default, ""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default, f"{v!r} 는 정수가 아니다 — 기본값 {default}"
    use = min(max(n, lo), hi)
    return use, ("" if use == n else f"{n} → {use}(받는 범위 {lo}~{hi})")


def _cut(text: str, cap: int) -> tuple[str, str]:
    """(실을 글, 자른 사실) — 넘으면 앞 cap 자와 표지."""
    if len(text) <= cap:
        return text, ""
    return text[:cap] + f"\n…[{len(text):,}자 가운데 앞 {cap:,}자에서 잘랐다]", f"{len(text):,}자 → 앞 {cap:,}자"


def numbers(answer: str, sources: str) -> dict:
    """답의 수를 근거 원문과 대조한다 — {checked: 대조한 수의 개수, mismatched: [원문에 없는 수]}(+ 넘치면 mismatched_total). 판정은 챗 · 결정문과
    같다(evidence — 소수점이 있거나 100 이상인 수만, 부분 문자열). 어긋나도 고쳐 쓰지 않는다."""
    checked = len({norm for _raw, norm in sig_numbers(answer)})
    bad = unsourced_numbers(answer, sources, limit=0) if sources else [raw for raw, _n in sig_numbers(answer)]
    bad = list(dict.fromkeys(bad))
    out = {"checked": checked, "mismatched": bad[:NUMS_SHOW]}
    if len(bad) > NUMS_SHOW:
        out["mismatched_total"] = len(bad)
    return out


def _model_of(msg, llm) -> str:
    """답한 모델 — 응답 메타의 이름(넘김이면 넘겨받은 쪽), 없으면 묶은 LLM 의 이름."""
    md = getattr(msg, "response_metadata", None) or {}
    name = md.get("model_name") or md.get("model")
    if isinstance(name, str) and name:
        return name
    for o in (llm, getattr(llm, "runnable", None), getattr(llm, "bound", None)):
        n = getattr(o, "model_name", None) or getattr(o, "model", None)
        if isinstance(n, str) and n:
            return n
    return ""


def _usage(msgs) -> dict | None:
    """AI 메시지의 사용량 합 {input, output} — 하나도 없으면 None(모름)."""
    inp = out = 0
    seen = False
    for m in msgs:
        u = getattr(m, "usage_metadata", None)
        if isinstance(u, dict) and ("input_tokens" in u or "output_tokens" in u):
            seen = True
            inp += int(u.get("input_tokens") or 0)
            out += int(u.get("output_tokens") or 0)
    return {"input": inp, "output": out} if seen else None


def _args_show(args) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        s = str(args)
    return s if len(s) <= ARGS_SHOW else s[:ARGS_SHOW] + "…"


def header_user(hdr) -> str:
    """HTTP 창구의 사람 — 게이트웨이가 싣는 신원 머리(x-hwax-user, 퍼센트 인코딩)를 MCP 길과 같은 셈으로 푼다(mcp_server._identity)."""
    return unquote(hdr.get("x-hwax-user") or "").strip()


# ── 울타리 ────────────────────────────────────────────────────────────────────────────────────────────────────
async def read_fence(app, groups: list, user: str, apps=None) -> tuple[dict, str]:
    """(묶을 도구 {노출 이름: 도구}, 비었을 때의 까닭) — 그 사람의 도구 ∩ 게이트웨이가 읽기로 확인한 것(routine_read) − 이 서버의 도구 − 게이트웨이
    로컬 도구, apps 를 주면 그 앱(백엔드 키)의 것만. routine_read 를 모르면 빈 울타리다(모르면 막는다 — 루틴과 같다)."""
    tools = await _maybe(_person_tools(app, groups, user))
    m, rr = await _maybe(_read_map(app))
    if rr is None:
        return {}, "게이트웨이가 읽기 판정(/tools-map 의 routine_read)을 주지 않았다 — 읽기로 확인된 도구가 없어 아무것도 묶지 않는다(옛 게이트웨이 · 불통)"
    want = {str(a).strip() for a in (apps or []) if str(a).strip()}
    allowed = {n: t for n, t in (tools or {}).items()
               if n in rr and m.get(n) and m[n] not in (OWN_BACKEND, GATEWAY_LOCAL) and (not want or m[n] in want)}
    if allowed:
        return allowed, ""
    return {}, ("고른 앱(" + ", ".join(sorted(want)) + ")에" if want else "이 사람의 도구에") + " 읽기로 확인된 도구가 하나도 없다"


# ── 게이트웨이 내부 ref 창구(B 의 손잡이 — 게이트웨이 작업자가 짓는다) ──────────────────────────────────────────────────
def _gw_base(app) -> str:
    """게이트웨이 HTTP 뿌리 — 이 서버의 게이트웨이 연결 주소(MCP_CONFIG 의 gateway url)에서 끝의 /mcp 를 뗀 자리(게이트웨이 계약 — 내부 창구는
    X-Forwarded-* 가 실린 바깥 길을 받지 않는다). 연결 설정이 없으면 GATEWAY_HTTP_BASE."""
    import routine  # noqa: PLC0415
    try:
        url = routine._gateway_url(app).rstrip("/")
    except Exception:  # noqa: BLE001 — 연결 설정이 없다
        return _app()._GW_HTTP.rstrip("/")
    return url[:-len("/mcp")] if url.endswith("/mcp") else url


async def read_ref(app, ref: str, user: str) -> str:
    """B 결과 손잡이의 원문 — GET {게이트웨이 뿌리}/internal/result/{ref}(게이트웨이 계약 — 답 {ref, tool, chars, sha256, pages, created, expires,
    text, structured} 의 text 가 원문 전부다 · pages 는 result_get 의 쪽 수라 여기서 쪽을 받지 않는다). 자격은 게이트웨이 연결의 Authorization
    (Bearer <GW_TOKEN> — 에이전트 서버 창구들이 받는 공유 비밀과 같은 값, seat_box.secret) 한 번 · 그 사람 이메일 머리 X-HWAX-User(%-인코딩)다.
    404 는 없음 · 기한 지남 · 남의 것(같은 404), 400 은 사람 머리 없음, 401 은 비밀 어긋남."""
    import seat_box  # noqa: PLC0415
    ref = str(ref or "").strip()
    if not _REF_RE.match(ref):
        raise OffloadError(f"ref 꼴이 아니다({ref[:40]!r}) — 결과 머리의 ref 를 그대로 준다")
    if not (user or "").strip():
        raise OffloadError("신원 없는 호출은 손잡이를 읽지 않는다(주인 대조가 신원 머리로 된다)")
    secret = seat_box.secret(app)
    if not secret:
        raise OffloadError("게이트웨이 공유 비밀이 없다 — MCP_CONFIG 의 게이트웨이 연결에 Authorization(Bearer <GW_TOKEN>)이 없어 ref 를 읽지 못한다")
    A = _app()
    url = f"{_gw_base(app)}/internal/result/{quote(ref, safe='')}"
    hdr = {"Authorization": secret, A.USER_HEADER: quote(user.strip().lower(), safe="@.")}
    async with httpx.AsyncClient(transport=GW_TRANSPORT, timeout=httpx.Timeout(60.0, connect=10.0)) as c:
        try:
            r = await c.get(url, headers=hdr)
        except httpx.HTTPError as exc:
            raise OffloadError(f"게이트웨이 내부 창구에 닿지 못했다({type(exc).__name__}) — ref {ref}") from None
    if r.status_code == 404:
        raise OffloadError(f"손잡이 {ref} 가 없다 — 기한이 지났거나 틀렸거나 내 결과의 ref 가 아니다(result_get 으로 확인)")
    if r.status_code in (401, 403):
        raise OffloadError(f"게이트웨이가 이 서버의 자격을 받지 않았다(HTTP {r.status_code} — 공유 비밀 어긋남) — ref {ref}")
    if r.status_code != 200:
        raise OffloadError(f"게이트웨이가 손잡이를 주지 않았다(HTTP {r.status_code}) — ref {ref}")
    try:
        d = r.json()
    except ValueError:
        raise OffloadError("게이트웨이 내부 창구의 답이 JSON 이 아니다") from None
    t = d.get("text") if isinstance(d, dict) else None
    if not isinstance(t, str):
        raise OffloadError("게이트웨이 내부 창구의 답에 text 가 없다 — 창구 꼴이 이 판과 다르다")
    return t


# ── A 위임 ─────────────────────────────────────────────────────────────────────────────────────────────────────
class _Run:
    """위임 하나의 기록 — 호출 수 · 근거 표 · 근거 원문(숫자 대조용) · AI 메시지 · 진행 줄."""

    def __init__(self, max_steps: int, emit):
        self.max_steps, self.emit = max_steps, emit
        self.calls = 0
        self.evidence: list = []
        self.texts: list = []
        self.shown: list = []       # (근거 id, 도구, LLM 에 보인 글) — 시간 · 예산으로 멈췄을 때 정리 턴이 본다
        self.msgs: list = []
        self.final = ""
        self.final_msg = None


def _proxy(base, run: _Run, cap: int):
    """그 사람의 노출 도구와 겉(이름 · 설명 · 인자 스키마)이 같고 속은 그 도구를 부르며 근거를 적는 새 도구(routine._proxy 와 같은 꼴). 호출 예산을
    다 쓰면 부르지 않고 '정리하라' 를 돌려준다. LLM 에는 cap 자까지 보이고, 숫자 대조에는 받은 글 전부를 쓴다."""
    from langchain_core.tools import StructuredTool  # noqa: PLC0415
    A = _app()

    async def _run(**kwargs):
        if run.calls >= run.max_steps:
            return f"{_HALT} 호출 예산 {run.max_steps}번을 다 썼다 — 더 부르지 말고 지금까지 받은 결과로 답을 정리하라."
        run.calls += 1
        eid = f"e{run.calls}"
        run.emit({"step": f"도구 호출 {run.calls}/{run.max_steps}: {base.name}", "tool": base.name})
        ok = True
        try:
            text = _text(await base.ainvoke(kwargs))
        except Exception as exc:  # noqa: BLE001 — 실패도 근거 표에 남기고 LLM 에 까닭을 준다
            ok, text = False, f"{_HALT} 도구 {base.name} 호출 실패: {type(exc).__name__}: {str(exc)[:300]}"
        # 관문(유령 ID · 쓰기 막이 등 — app._cap_tool)이 보내지 않은 호출 · 자격 거절은 근거가 아니다
        if text.startswith(A._TOOL_FAIL_MARK) or "는 이번 대화 어디에도 없는 값이다" in text:
            ok = False
        run.evidence.append({"id": eid, "tool": base.name, "args": _args_show(kwargs), "chars": len(text), "ok": ok})
        if ok:
            run.texts.append(text)
        shown, _c = _cut(text, cap)
        run.shown.append((eid, base.name, shown))
        return f"[근거 {eid}] " + ("(도구가 실패했다) " if not ok else "") + shown

    return StructuredTool(name=base.name, description=getattr(base, "description", "") or "", coroutine=_run,
                          args_schema=getattr(base, "args_schema", None) or {"type": "object", "properties": {}})


_DELEGATE_SYS = (
    "너는 HWAX 서버의 조사 위임 실행기다 — 사용자의 Claude 가 맡긴 일 하나를 묶인 읽기 도구로 조사해 답한다.\n"
    "[규칙]\n"
    "- 묶인 도구만 쓴다 — 읽기 도구뿐이다. 쓰기 · 잡 제출 · 지우기는 할 수 없고, 하겠다고 말하지도 않는다.\n"
    "- 값(수치 · 이름 · 키)은 도구 결과에 나온 것만 쓴다. 지어내지 않고 식별자(id)를 추측해 넣지 않는다 — 목록 · 검색 도구로 먼저 확인한다.\n"
    "- 결과마다 [근거 eN] 머리가 붙는다 — 답의 사실마다 근거를 [eN] 으로 단다.\n"
    "- 모르면 모른다고 한다. 일에 충분하면 더 부르지 않는다.\n"
    "- 예산 — 도구 호출 {steps}번 · {minutes}분.\n"
    "- 답은 한국어로 짧고 정확하게 — 알아낸 것 · 근거 · 빈 곳(확인하지 못한 것)."
)
_WRAP_SYS = (
    "너는 HWAX 서버의 조사 위임 실행기다. 도구를 더 부르지 않는다. 아래 [받은 결과] 만으로 [맡은 일] 에 답한다.\n"
    "- 값은 결과에 있는 그대로 쓰고 사실마다 근거를 [eN] 으로 단다. 결과에 없는 것은 '확인하지 못했다' 로 적는다.\n"
    "- 답은 한국어로 짧고 정확하게."
)


async def _react(llm, tools: list, sysmsg: str, human: str, run: _Run) -> None:
    """작은 ReAct — create_react_agent 를 astream(values) 로 돌려 도중에 끊겨도 받은 메시지는 run 에 남는다(routine._gather 와 같은 까닭).
    도구 호출이 없는 마지막 AI 글이 답이다. 왕복 상한은 호출 수 × 2 + 5."""
    from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415
    from langgraph.prebuilt import create_react_agent  # noqa: PLC0415
    run.msgs = [SystemMessage(content=sysmsg), HumanMessage(content=human)]
    agent = create_react_agent(llm, tools)
    async for st in agent.astream({"messages": run.msgs}, config={"recursion_limit": run.max_steps * 2 + 5}, stream_mode="values"):
        got = list((st or {}).get("messages") or [])
        if len(got) <= len(run.msgs):
            continue
        for m in got[len(run.msgs):]:
            if getattr(m, "type", "") == "ai":
                t = _text(getattr(m, "content", "")).strip()
                if t and not getattr(m, "tool_calls", None):
                    run.final, run.final_msg = t, m
        run.msgs = got


async def run_delegate(app, task: str, groups: list, opts: dict, user_email: str):
    """위임 잡의 몸 — delib_jobs 의 잡 원장이 소비하는 SSE 조각(status · delib stage · warning · error · result)을 낸다(delib_jobs.start_task 가
    잡 태스크 안에서 부른다 — 창 덮어쓰기 · 유령 ID 씨앗은 이 태스크의 문맥에만 걸린다). 결과 프레임의 task 칸이 원장의 task_result 가 된다."""
    import routine  # noqa: PLC0415 — 결과 상한 셈을 같이 쓴다
    A = _app()
    steps, _c1 = _clamp(opts.get("max_steps"), STEPS_DEFAULT, 1, STEPS_MAX)
    minutes, _c2 = _clamp(opts.get("max_minutes"), MINUTES_DEFAULT, 1, MINUTES_MAX)
    apps = [str(a) for a in (opts.get("apps") or []) if str(a).strip()][:APPS_MAX]
    toks: list = []
    react = getter = None
    try:
        yield _sse("delib", {"kind": "stage", "stage": "tools"})
        try:
            allowed, why = await read_fence(app, groups, user_email, apps)
        except Exception as exc:  # noqa: BLE001 — 게이트웨이 불통 · 자격 거절은 까닭을 붙여 끝낸다
            allowed, why = {}, f"게이트웨이 도구를 불러오지 못했다({type(exc).__name__}: {str(exc)[:200]})"
        if not allowed:
            yield _sse("error", {"message": f"위임을 시작하지 못했다 — {why}"})
            return
        yield _sse("status", {"step": f"읽기 도구 {len(allowed)}개를 묶었다(그 사람 시야 ∩ 게이트웨이가 읽기로 확인한 것)"})
        llm, tok = await _maybe(_chat_llm(app))
        if tok is not None:
            toks.append((external_llm.ctx_override, tok))
        toks.append((A._turn_ids, A._turn_ids.set(set(A._int_tokens(task)))))   # 유령 ID 막이의 씨앗 — 맡긴 일에 든 정수(루틴의 씨앗과 같은 뜻)
        queue: asyncio.Queue = asyncio.Queue()
        run = _Run(steps, queue.put_nowait)
        cap = routine._result_cap(steps)
        tools = [_proxy(t, run, cap) for t in allowed.values()]
        yield _sse("delib", {"kind": "stage", "stage": "gather"})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + minutes * _MINUTE_S
        react = asyncio.ensure_future(_react(llm, tools, _DELEGATE_SYS.format(steps=steps, minutes=minutes), task, run))
        note = ""
        while True:
            getter = asyncio.ensure_future(queue.get())
            done, _p = await asyncio.wait({react, getter}, timeout=max(0.0, deadline - loop.time()),
                                          return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield _sse("status", getter.result())
                getter = None
                continue
            getter.cancel()
            getter = None
            if react in done:
                exc = react.exception()
                if exc is not None:
                    from langgraph.errors import GraphRecursionError  # noqa: PLC0415
                    note = (f"호출 왕복 상한({steps * 2 + 5})에 닿아 멈췄다" if isinstance(exc, GraphRecursionError)
                            else f"조회 도중 멈췄다({type(exc).__name__}: {str(exc)[:160]})")
                break
            react.cancel()
            await asyncio.gather(react, return_exceptions=True)
            note = f"시간 예산({minutes}분)에 닿아 조회를 멈췄다"
            break
        while not queue.empty():
            yield _sse("status", queue.get_nowait())
        if run.calls >= steps and not run.final:
            note = note or f"호출 예산({steps}번)을 다 썼다"
        if note:
            yield _sse("warning", {"message": note + " — 그때까지 받은 결과로 답을 정리한다"})
        yield _sse("delib", {"kind": "stage", "stage": "answer"})
        answer, msg = run.final, run.final_msg
        if not answer:
            from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415
            got = "\n\n".join(f"[근거 {eid}] {name}\n{shown}" for eid, name, shown in run.shown) or "(받은 결과가 없다)"
            msg = await llm.ainvoke([SystemMessage(content=_WRAP_SYS),
                                     HumanMessage(content=f"[맡은 일]\n{task}\n\n[받은 결과]\n{got}\n\n[멈춘 까닭] {note or '답을 내지 않았다'}")])
            answer = _text(getattr(msg, "content", "")).strip()
            run.msgs.append(msg)
        if not answer:
            yield _sse("error", {"message": "LLM 이 빈 답을 냈다" + (f" — {note}" if note else "")})
            return
        shown, cut = _cut(answer, ANSWER_MAX)
        result = {"answer": shown, "evidence": run.evidence,
                  "numbers": numbers(answer, "\n".join(run.texts) + "\n" + task),
                  "model": _model_of(msg, llm), "steps": run.calls, "tokens": _usage(run.msgs),
                  "notes": [x for x in (note, f"답을 {cut}에서 잘랐다" if cut else "", _c1 and f"max_steps {_c1}",
                                        _c2 and f"max_minutes {_c2}") if x]}
        yield _sse("result", {"content": shown, "task": result})
    finally:
        for f in (getter, react):
            if f is not None and not f.done():
                f.cancel()
        for var, tok in reversed(toks):
            try:
                var.reset(tok)
            except ValueError:      # 다른 문맥에서 닫혔다
                pass


def delegate_caps() -> tuple[int, int]:
    """위임 자리 — (전체 DELEGATE_MAX_RUNNING, 사람마다 1). 심의 자리(GLM)와 따로 센다."""
    return DELEGATE_MAX_RUNNING, DELEGATE_PER_USER


def arm() -> None:
    """위임 잡 종류를 원장에 올린다(delib_jobs.register_kind) — 스위치가 켜진 서버만(mcp_server 가 import 때 부른다)."""
    import delib_jobs  # noqa: PLC0415
    delib_jobs.register_kind("delegate", label="위임", entry=run_delegate, caps=delegate_caps,
                             interrupted="서버 재기동으로 중단됨 — 이어 돌리지 않는다. 읽기만 하는 일이라 같은 일을 delegate_start 로 다시 맡기면 된다")


def status_view(job: dict) -> dict:
    """delegate_status 의 답 — 상태 · 단계 · 마지막 일 · 호출 진행 · 줄 · 오류. 맡긴 일은 머리 80자만."""
    import delib_jobs  # noqa: PLC0415
    s = delib_jobs.summary(job)
    q = str(job.get("question") or "")
    out = {"job_id": s["job_id"], "status": s["status"], "stage": s.get("stage"), "step": s.get("step"),
           "task_head": q if len(q) <= 80 else q[:80] + "…", "elapsed_s": s.get("elapsed_s"),
           "steps_recent": list(job.get("steps") or [])[-5:], "steps_total": int(job.get("steps_total") or 0)}
    for k in ("idle_s", "last_step", "queue", "waited_s"):
        if k in s:
            out[k] = s[k]
    if job.get("warnings"):
        out["warnings"] = list(job["warnings"])[-5:]
    if s.get("error"):
        out["error"] = s["error"]
    return out


def result_view(job: dict, view: str) -> dict:
    """delegate_result 의 답 — 끝났으면 {answer, evidence(full) · evidence_n(summary), numbers, model, steps, tokens}, 아니면 상태와 안내."""
    st = job.get("status")
    r = job.get("task_result")
    if st != "done" or not isinstance(r, dict):
        out = {"job_id": job["id"], "status": st}
        if job.get("error"):
            out["error"] = job["error"]
        out["note"] = ("아직 끝나지 않았다 — delegate_status(job_id, wait_s) 로 한 번 기다린 뒤 다시 받는다" if st in ("queued", "running")
                       else "답 없이 끝났다 — 까닭은 error. 다시 맡기려면 delegate_start")
        return out
    out = {"job_id": job["id"], "status": st, "view": view, "answer": r.get("answer"), "numbers": r.get("numbers"),
           "model": r.get("model"), "steps": r.get("steps"), "tokens": r.get("tokens")}
    ev = r.get("evidence") or []
    if view == "full":
        out["evidence"] = ev
    else:
        out["evidence_n"] = len(ev)
        out["evidence_tools"] = sorted({e.get("tool") for e in ev if isinstance(e, dict) and e.get("tool")})
    if r.get("notes"):
        out["notes"] = r["notes"]
    return out


# ── C 절 초안 ──────────────────────────────────────────────────────────────────────────────────────────────────
async def _gather_sources(app, refs: list, user: str, groups: list) -> list[dict]:
    """근거마다 {n, kind(ref · tool), ref | tool · args, chars, ok, error?, text} — ref 는 게이트웨이 내부 창구로, {tool, args} 는 위임과 같은 읽기
    울타리 안의 도구로 읽는다(울타리 밖이면 부르지 않고 그렇다고 적는다)."""
    out, fence = [], None
    for i, r in enumerate(refs, 1):
        row: dict = {"n": i}
        try:
            if isinstance(r, str):
                row.update(kind="ref", ref=r.strip())
                text = await read_ref(app, r, user)
            elif isinstance(r, dict) and isinstance(r.get("tool"), str) and r["tool"].strip():
                name, args = r["tool"].strip(), r.get("args") or {}
                row.update(kind="tool", tool=name, args=_args_show(args))
                if not isinstance(args, dict):
                    raise OffloadError("args 는 객체다")
                if fence is None:
                    fence, _why = await read_fence(app, groups, user)
                t = fence.get(name)
                if t is None:
                    raise OffloadError(f"{name} 는 읽기 울타리 밖이다(게이트웨이가 읽기로 확인한 내 도구만 부른다) — 부르지 않았다")
                text = _text(await t.ainvoke(args))
                if text.startswith(_app()._TOOL_FAIL_MARK):
                    raise OffloadError(text[:300])
            else:
                raise OffloadError("refs 의 항목은 ref 글이거나 {tool, args} 다")
            row.update(chars=len(text), ok=True, text=text)
        except OffloadError as exc:
            row.update(chars=0, ok=False, error=str(exc)[:300], text="")
        except Exception as exc:  # noqa: BLE001 — 근거 하나의 실패가 다른 근거를 막지 않는다
            row.update(chars=0, ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}", text="")
        out.append(row)
    return out


def _window_chars(reserve_tokens: int) -> int:
    """지금 문맥의 LLM 창(토큰)에서 reserve 를 뗀 글자 — 1자 ≈ 1토큰(한국어에 보수적)."""
    return max(4000, int(_app()._model_context_tokens()) - int(reserve_tokens))


async def draft_section(app, *, outline: str, refs, style: str, target_chars, user: str, groups: list) -> dict:
    """C — 개요 + 근거로 GLM 이 절 초안을 쓴다 → {draft, numbers, sources, model}. 근거가 창을 넘으면 거절한다(줄이라 알린다 — 잡으로 돌리지 않는다)."""
    outline = str(outline or "").strip()
    if not outline:
        raise OffloadError("outline 이 비었다")
    if len(outline) > OUTLINE_MAX:
        raise TooLarge(f"outline 이 {len(outline):,}자다 — {OUTLINE_MAX:,}자 안으로 줄인다(근거 본문은 refs 로 준다)")
    if style not in STYLES:
        raise OffloadError(f"style 은 {' · '.join(STYLES)} 가운데 하나다")
    target, _c = _clamp(target_chars, DRAFT_TARGET_DEFAULT, 200, DRAFT_TARGET_MAX)
    refs = list(refs or [])
    if len(refs) > DRAFT_REFS_MAX:
        raise TooLarge(f"refs 가 {len(refs)}건이다 — {DRAFT_REFS_MAX}건까지(나눠 부른다)")
    src = await _gather_sources(app, refs, user, groups)
    room = _window_chars(_RESERVE_TOKENS + target * 2)
    have = sum(s["chars"] for s in src) + len(outline)
    if have > room:
        raise TooLarge(f"근거 · 개요가 {have:,}자로 GLM 창에 실을 수 있는 {room:,}자를 넘는다 — refs 를 줄이거나 절을 나눠 부른다")
    from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415
    sysmsg = (f"너는 보고서 절 초안 작성자다. [개요] 와 [근거] 만으로 {STYLES[style]} 꼴의 한국어 절을 마크다운으로 쓴다 — 약 {target:,}자.\n"
              "- 수치 · 이름 · 키는 근거에 있는 그대로 쓰고, 사실마다 근거를 [s번호] 로 단다.\n"
              "- 근거에 없는 값은 지어내지 않는다 — 없으면 '근거 없음' 으로 둔다.\n"
              "- 머리말 · 맺음말 · 설명 없이 절 본문만 낸다.")
    body = "\n\n".join(f"[s{s['n']}] " + (f"ref {s.get('ref')}" if s["kind"] == "ref" else f"{s.get('tool')} {s.get('args')}")
                       + ("\n" + s["text"] if s["ok"] else f"\n(읽지 못했다 — {s.get('error')})") for s in src)
    llm = await _maybe(_glm_llm(app))
    msg = await asyncio.wait_for(llm.ainvoke([SystemMessage(content=sysmsg),
                                              HumanMessage(content=f"[개요]\n{outline}\n\n[근거]\n{body or '(근거 없음)'}")]),
                                 SYNC_LIMIT_S)
    draft = _text(getattr(msg, "content", "")).strip()
    if not draft:
        raise OffloadError("LLM 이 빈 초안을 냈다")
    shown, cut = _cut(draft, DRAFT_OUT_MAX)
    out = {"draft": shown, "numbers": numbers(draft, "\n".join(s["text"] for s in src) + "\n" + outline),
           "sources": [{k: v for k, v in s.items() if k != "text"} for s in src], "model": _model_of(msg, llm)}
    notes = [x for x in (cut and f"초안을 {cut}에서 잘랐다", _c and f"target_chars {_c}") if x]
    if notes:
        out["notes"] = notes
    return out


# ── D 변환 ─────────────────────────────────────────────────────────────────────────────────────────────────────
async def transform(app, *, instruction: str, text: str, ref: str, user: str) -> dict:
    """D — 번역 · 요약 · 표 바꾸기 · 용어 통일을 챗 LLM 으로 → {text, numbers, model}. 결과가 길면 TRANSFORM_OUT_MAX 에서 자르고 그렇다고 적는다
    (긴 결과를 B 저장소에 두는 일은 게이트웨이 몫이다). 원문이 창을 넘으면 거절한다."""
    instruction = str(instruction or "").strip()
    if not instruction:
        raise OffloadError("instruction 이 비었다")
    if len(instruction) > INSTRUCTION_MAX:
        raise TooLarge(f"instruction 이 {len(instruction):,}자다 — {INSTRUCTION_MAX:,}자 안으로")
    has_text, has_ref = bool(str(text or "")), bool(str(ref or "").strip())
    if has_text == has_ref:
        raise OffloadError("text 와 ref 가운데 하나만 준다")
    src = str(text) if has_text else await read_ref(app, ref, user)
    from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415
    llm, tok = await _maybe(_chat_llm(app))
    try:
        room = _window_chars(_RESERVE_TOKENS + TRANSFORM_OUT_MAX)
        if len(src) + len(instruction) > room:
            raise TooLarge(f"원문이 {len(src):,}자로 챗 LLM 창에 실을 수 있는 {room:,}자를 넘는다 — 나눠 부르거나 result_ask 로 묻는다")
        msg = await asyncio.wait_for(llm.ainvoke([
            SystemMessage(content="너는 글 변환기다. [지시] 대로만 바꾼다 — 번역 · 요약 · 표 바꾸기 · 용어 통일. 원문에 없는 사실 · 수치를 더하지 "
                                  "않는다. 바꾼 글만 낸다(설명 · 머리말 없이)."),
            HumanMessage(content=f"[지시]\n{instruction}\n\n[원문]\n{src}")]), SYNC_LIMIT_S)
    finally:
        if tok is not None:
            try:
                external_llm.ctx_override.reset(tok)
            except ValueError:
                pass
    out_text = _text(getattr(msg, "content", "")).strip()
    if not out_text:
        raise OffloadError("LLM 이 빈 글을 냈다")
    shown, cut = _cut(out_text, TRANSFORM_OUT_MAX)
    out = {"text": shown, "numbers": numbers(out_text, src + "\n" + instruction), "model": _model_of(msg, llm)}
    if cut:
        out["cut"] = f"결과를 {cut}에서 잘랐다 — 원문을 나눠 다시 부른다"
    return out


# ── 결과에 묻기 창구(3-2 — 게이트웨이 result_ask 가 부른다) ─────────────────────────────────────────────────────────
_ASK_ONE = (
    "너는 긴 도구 결과를 대신 읽는 독자다. [원문] 만 보고 [질문] 에 답한다. 출력은 JSON 객체 하나뿐이다:\n"
    '{"answer": "답(한국어, {cap}자 안)", "quotes": [{"text": "답의 근거가 된 원문 구절 — 원문 글자 그대로 복사"}]}\n'
    "- quotes 는 원문에서 글자 그대로 옮긴다(고치지 않는다 · 8자 이상 · {qmax}개까지). 원문에 없는 글은 코드가 버린다.\n"
    "- 원문에 답이 없으면 answer 에 '원문에 없다' 로 쓰고 quotes 는 비운다. 지어내지 않는다."
)
_ASK_PAGE = (
    "너는 긴 도구 결과의 한 쪽을 읽는 독자다(전체 {pages}쪽 가운데 {page}쪽). [이 쪽] 만 보고 [질문] 에 쓸 것을 모은다. 출력은 JSON 객체 하나뿐이다:\n"
    '{"relevant": true 또는 false, "notes": "이 쪽에서 질문에 쓸 사실(한국어, 짧게)", "quotes": [{"text": "근거 구절 — 이 쪽 글자 그대로 복사"}]}\n'
    "- quotes 는 이 쪽에서 글자 그대로 옮긴다(8자 이상 · {qmax}개까지). 질문과 무관한 쪽이면 relevant false · 빈 notes · 빈 quotes."
)
_ASK_MERGE = (
    "너는 긴 도구 결과를 쪽마다 읽은 메모를 모아 [질문] 에 답한다. 출력은 JSON 객체 하나뿐이다:\n"
    '{"answer": "답(한국어, {cap}자 안)", "quotes": [{"text": "[쪽 메모] 의 인용 가운데 답의 근거를 글자 그대로"}]}\n'
    "- 메모에 없는 것은 지어내지 않는다. 답이 없으면 '원문에 없다'."
)


def _pages(text: str, size: int) -> list[tuple[int, str]]:
    """(시작 자리, 쪽 글) — size 자 안에서 줄 끝을 찾아 자른다(없으면 글자로)."""
    out, i, n = [], 0, len(text)
    while i < n:
        j = min(n, i + size)
        if j < n:
            k = text.rfind("\n", i + size // 2, j)
            if k > i:
                j = k + 1
        out.append((i, text[i:j]))
        i = j
    return out or [(0, "")]


def _clean_quote(q) -> str:
    s = str((q.get("text") if isinstance(q, dict) else q) or "").strip()
    for a, b in (("«", "»"), ("“", "”"), ('"', '"'), ("'", "'"), ("「", "」")):
        if len(s) >= 2 and s.startswith(a) and s.endswith(b):
            s = s[1:-1].strip()
    return s


def _place(quotes, text: str, base: int, full: str) -> tuple[list, int]:
    """인용을 원문 자리로 — ([{page, start, end, text}], 버린 수). text 에서 먼저 찾고 없으면 full 전체에서. 원문에 글자 그대로 없거나 너무 짧으면 버린다."""
    out, dropped, seen = [], 0, set()
    for q in quotes if isinstance(quotes, list) else []:
        s = _clean_quote(q)
        if len(s) < QUOTE_MIN:
            dropped += 1
            continue
        at = text.find(s)
        start = base + at if at >= 0 else full.find(s)
        if start < 0:
            dropped += 1
            continue
        if start in seen:
            continue
        seen.add(start)
        out.append({"page": start // RESULT_PAGE_CHARS + 1, "start": start, "end": start + len(s), "text": s})
    return out[:QUOTES_MAX], dropped


async def ask_over_text(app, *, text: str, question: str, max_answer_chars=None) -> dict:
    """원문에 묻는다 → {answer, quotes: [{page, start, end, text}], numbers, model}. 창 안이면 한 번, 넘으면 쪽마다 읽고 모은다. 인용은 코드가 원문과
    대조해 자리를 단다(원문에 없는 것은 버린다). page 는 RESULT_PAGE_CHARS(게이트웨이 result_get 기본 20,000자) 쪽 번호, start · end 는 원문
    전체의 글자 자리(코드 포인트 · end 미포함)."""
    from deliberation import _parse_json  # noqa: PLC0415 — LLM 출력의 JSON 을 관대하게 읽는다(산문 앞섬 · 펜스)
    from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415
    text, question = str(text or ""), str(question or "").strip()
    if not text.strip():
        raise OffloadError("text 가 비었다")
    if not question:
        raise OffloadError("question 이 비었다")
    if len(question) > QUESTION_MAX:
        raise TooLarge(f"question 이 {len(question):,}자다 — {QUESTION_MAX:,}자 안으로")
    cap, _c = _clamp(max_answer_chars, ASK_ANSWER_DEFAULT, ASK_ANSWER_MIN, ASK_ANSWER_MAX)
    llm, tok = await _maybe(_chat_llm(app))
    try:
        size = ASK_PAGE_CHARS or _window_chars(_RESERVE_TOKENS + cap)
        pages = _pages(text, size)
        if len(pages) > ASK_PAGES_MAX:
            raise TooLarge(f"원문 {len(text):,}자가 한 번에 {size:,}자씩 {len(pages)}쪽이라 {ASK_PAGES_MAX}쪽 상한을 넘는다 — 질문을 좁히거나 "
                           "result_get 으로 쪽을 받아 그 쪽에 묻는다")

        async def call(sysmsg: str, human: str):
            msg = await llm.ainvoke([SystemMessage(content=sysmsg), HumanMessage(content=human)])
            raw = _text(getattr(msg, "content", "")).strip()
            try:
                d = _parse_json(raw)
            except Exception:  # noqa: BLE001
                d = None
            return msg, (d if isinstance(d, dict) else None), raw

        async def whole():
            dropped = 0
            if len(pages) == 1:
                msg, d, raw = await call(_ASK_ONE.replace("{cap}", f"{cap:,}").replace("{qmax}", str(QUOTES_MAX)),
                                         f"[질문]\n{question}\n\n[원문]\n{text}")
                answer = str((d or {}).get("answer") or ("" if d else raw)).strip()
                quotes, dropped = _place((d or {}).get("quotes"), text, 0, text)
                return msg, answer, quotes, dropped
            sem = asyncio.Semaphore(ASK_CONCURRENCY)

            async def one(i: int, base: int, part: str):
                async with sem:
                    sysmsg = (_ASK_PAGE.replace("{pages}", str(len(pages))).replace("{page}", str(i + 1))
                              .replace("{qmax}", str(QUOTES_MAX)))
                    return await call(sysmsg, f"[질문]\n{question}\n\n[이 쪽]\n{part}")
            got = await asyncio.gather(*(one(i, b, p) for i, (b, p) in enumerate(pages)))
            memos, pool = [], []
            for (base, part), (_m, d, raw) in zip(pages, got):
                if not d:
                    continue
                qs, dr = _place(d.get("quotes"), part, base, text)
                dropped += dr
                if d.get("relevant") is False and not qs:
                    continue
                pool += qs
                memos.append(f"[쪽 메모 — 원문 {base:,}자부터]\n{str(d.get('notes') or '').strip()}"
                             + "".join(f"\n- 인용: {q['text']}" for q in qs))
            msg, d, raw = await call(_ASK_MERGE.replace("{cap}", f"{cap:,}"),
                                     f"[질문]\n{question}\n\n" + ("\n\n".join(memos) if memos else "(질문과 관계된 쪽이 없었다)"))
            answer = str((d or {}).get("answer") or ("" if d else raw)).strip()
            quotes, dr = _place((d or {}).get("quotes"), text, 0, text)
            dropped += dr
            if not quotes:
                quotes = pool[:QUOTES_MAX]
            return msg, answer, quotes, dropped

        msg, answer, quotes, dropped = await asyncio.wait_for(whole(), SYNC_LIMIT_S)
    finally:
        if tok is not None:
            try:
                external_llm.ctx_override.reset(tok)
            except ValueError:
                pass
    if not answer:
        raise OffloadError("LLM 이 답을 내지 않았다")
    shown, _cut_note = _cut(answer, cap)
    out = {"answer": shown, "quotes": quotes, "numbers": numbers(answer, text + "\n" + question), "model": _model_of(msg, llm)}
    if dropped:
        log.info("[offload] 묻기 — 원문에 없는 인용 %d개를 버렸다", dropped)
    return out
