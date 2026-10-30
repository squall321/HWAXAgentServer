# 분석 루틴 실행기(R3) — 챗(Qwen) 길에서 루틴의 단계를 코드가 차례로 돈다(단계마다 허용 도구만 묶은 작은 ReAct · 호출은 전부 게이트웨이 routine_call · 도구 없는 봉투 턴 · 인사이트 · 보고서), 브라우저가 떠나도 도는 떼어 낸 작업
"""분석 루틴의 챗(Qwen) 길 — 정본 계약은 HWAXPortal `docs/analysis-routines/R3-design.md` §4 다.

한 턴 ReAct 에 루틴을 통째로 맡기지 않는다(AR-D2 — 8 ~ 9단계가 왕복 상한에 들지 않고, invoke_tool 로 단계가 다른 플랫폼으로 샌다). 코드가
단계를 차례로 돌고, 단계마다 그 단계의 허용 도구 {백엔드: [원래 이름]} 만 묶은 작은 ReAct 가 조회한다. 묶은 도구의 겉(이름 · 설명 · 인자
스키마)은 그 사람의 도구 그대로이고 속은 게이트웨이 `routine_call` 이다 — 울타리 · 원장이 Claude 길과 같다(R3-D3). 단계를 닫는 봉투는 도구
없는 따로 턴이 JSON 으로 쓰고(R3-D4 — 도구 왕복 뒤의 모델은 JSON 계약을 깬다, deliberation._free_gather_one 의 교훈), 포털이 원장과 맞대
상태를 고친다. 프레임에는 포털이 고친 상태를 싣는다.

루틴 도구 여섯은 일반 로더가 거른다(app._agent_denied — 일반 챗 거부, R2). 그래서 게이트웨이와 MCP 원 세션(JSON-RPC 를 httpx 로 — 포털
procedures/runner.GatewaySession 과 같은 꼴)으로 말한다(R3-D2). 어댑터를 거치면 isError 가 예외가 되고 머리 JSON 과 본문이 한 글로 붙는다.
사람 PAT 로만 붙는다 — 서비스 계정(GW_TOKEN)으로 떨어지지 않는다(게이트웨이가 루틴 도구를 거절하고, 원장 · 보고서가 그 사람 이름이어야 한다).

실행은 떼어 낸 작업이다(R3-D7 · D8). 레지스트리 _RUNS 가 작업을 쥐고, 구독자는 여럿이 붙었다 떨어질 수 있다 — 구독자가 끊겨도 작업은 돈다.
같은 프로세스에 살아 있는 실행에 '이어서' 가 오면 지금 원장 모양의 plan · 지금까지 낸 프레임을 다시 보내고 이어 붙는다(작업은 하나). 재기동
뒤에는 사람이 '이어서' 를 누른다 — 사람 PAT 는 재기동 때 사라지므로 저절로 다시 돌지 않는다.

시험 이음매(블랙박스 시험이 쓴다 — 이름을 바꾸지 않는다): GW_TRANSPORT · _gateway_url · _routine_llm · _user_tools · _tool_backends ·
run_routine · active_count · _RUNS · ROUTINE_MAX_RUNNING. 이음매 셋(_routine_llm · _user_tools · _tool_backends)은 비동기 함수이되 부르는 쪽이
돌려받은 값이 기다릴 것일 때만 기다린다 — 시험이 동기 함수로 바꿔 끼워도 된다.
"""
from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from urllib.parse import quote

import httpx

import call_trace
import external_llm
from deliberation import _env_float, _env_int, _parse_json, _sse
from evidence import unsourced_numbers

# ── 손잡이(env) — 모듈 값은 부를 때 읽는다(시험이 모듈 값을 바꾼다) ─────────────────────────────────────────────────
# 동시에 도는 실행 상한 — 넘으면 거절한다(v1 은 줄이 없다 — R3-D7). 루틴은 챗 LLM(Qwen)과 게이트웨이 · 포털 왕복을 단계마다 쓴다.
# 1 미만은 기본값으로 읽는다(0 을 넣은 박스가 모든 시작을 거절하지 않게 — delib_jobs.MAX_RUNNING 과 같은 규칙).
ROUTINE_MAX_RUNNING = _env_int("ROUTINE_MAX_RUNNING", 2)
if ROUTINE_MAX_RUNNING < 1:
    print(f"[routine] env ROUTINE_MAX_RUNNING={ROUTINE_MAX_RUNNING} 는 1 미만 — 기본값 2 로 읽는다")
    ROUTINE_MAX_RUNNING = 2
# 실행기 LLM — chat(챗 전용 LLM, 넘김 포함 — 기본) | glm(VLLM_*). 챗 전용 LLM 이 없는 박스는 둘 다 VLLM_* 다(thinking.THINK_LLM 과 같은 셈).
ROUTINE_LLM = (os.environ.get("ROUTINE_LLM") or "chat").strip().lower()
# 심의 단계의 진행 조회 간격(초) — deliberate_status 를 원 세션으로 직접 부른다(원장 · 예산을 쓰지 않는다 — R3-D5).
ROUTINE_DELIB_POLL_S = _env_float("ROUTINE_DELIB_POLL_S", 20.0)
# 도구 결과 한 건을 LLM 에 보이는 글자 상한 — 0(기본)이면 창에서 유도한다(deliberation._free_result_chars — 좌석 자유 조회와 같은 셈).
ROUTINE_RESULT_CHARS = _env_int("ROUTINE_RESULT_CHARS", 0)
# 다시 붙기용 프레임 기록(개수) — 넘치면 오래된 것부터 버린다. 다시 붙으면 지금 원장 모양의 plan 을 먼저 보내므로 단계 칩은 맞는다.
ROUTINE_HISTORY_FRAMES = _env_int("ROUTINE_HISTORY_FRAMES", 400)

# 시험 이음매 — None 이 아니면 게이트웨이로 가는 HTTP 요청이 전부 이 전송을 탄다(시험은 httpx.MockTransport 를 넣는다).
GW_TRANSPORT: httpx.AsyncBaseTransport | None = None

# 살아 있는 실행 — {실행 id(시작 전에는 임시 키): _Live}. 작업의 강한 참조이기도 하다(참조가 사라지면 asyncio 가 조용히 버린다).
_RUNS: dict = {}

_STATES = ("ok", "empty", "ambiguous", "denied", "not_connected", "unavailable", "failed", "skipped")   # 단계 봉투 상태(포털 Envelope)
_BLOCKED = ("denied", "not_connected", "unavailable")   # 실행을 시작할 때 막혀 닫힌 단계 — 보고서 절을 비워 둔다(포털이 커버리지로 말한다)
_OPEN = ("pending", "running")
_JOB_END = ("done", "error", "cancelled", "interrupted")   # 심의 잡의 끝 상태(delib_jobs)
_JOB_DEAD = ("error", "cancelled", "interrupted")          # 앞 프로세스가 세운 잡이 이렇게 끝났으면 잇지 않고 새로 세운다
# 엔진이 심의를 지금 못 세운다는 잠깐의 거절(delib_jobs.start) — 새 심의 멈춤(DelibPaused) · 동시 상한에 걸리고 줄이 찼다 · 줄을 끈 박스의 동시
# 상한. 단계 실패가 아니라 쉬었다 다시 세운다(R3-design §8). 기다림은 엔진의 retry_after_s, 없으면 _DELIB_RETRY_S 에서 거절마다 두 배씩
# _DELIB_RETRY_MAX 까지다 — 거절된 시작도 원장 한 줄 · 단계 예산 하나를 쓰므로 줄이 빠지기 전에 예산을 다 쓰지 않게.
_DELIB_BUSY = ("새 심의를 잠시 멈췄다", "대기열도 찼다", "동시 실행 상한에 걸렸")
_RETRY_AFTER = re.compile(r"retry_after_s=(\d+)")
_DELIB_RETRY_S, _DELIB_RETRY_MAX = 60.0, 600.0
_RUN_FINAL = ("done", "failed", "cancelled")    # 다시 이을 수 없는 실행 상태 — 이 실행이 세운 심의는 결정문을 읽을 데가 없다(접는다)
_REFUSED = "ROUTINE_ERROR:"
# 이 거절 뒤에는 그 단계에서 더 부르지 않는다 — 예산을 다 썼다 · 실행 상한 · 단계가 이미 닫혔다 · 실행이 도는 상태가 아니다 · 실행 · 단계가 없다.
# 글은 게이트웨이 routine_call 의 앞 검사와 포털 add_call(빠른 거절 · 원장 트랜잭션 안의 CallRefused) 둘 다다. 포털 글은 게이트웨이가 안쪽 도구를
# 부른 **뒤** 원장 쓰기에서 나와 꼬리('— 도구를 부르지 않는다')가 없다('실행이 cancelled 상태다') — 그 뒤에 더 보내면 안쪽 도구만 돌고 원장에는
# 남지 않는다. 울타리 밖 · 쓰기 도구 · 이름 겹침 거절은 그 도구만의 것이라 멈추지 않는다(다른 허용 도구는 된다).
_HALT_MARKS = ("호출 예산", "실행 상한", "도구를 부르지 않는다", "이미 닫혔다", "실행을 찾을 수 없", "이 실행에 단계")
_RUN_STATE = re.compile(r"실행이 [A-Za-z_]+ 상태다")      # 실행이 도는 상태가 아니다(사람이 멈춤 · 실패) — 게이트웨이 · 포털 같은 글
_HALT_NOTE = "더 부르지 말고 정리하라 — 이 단계에서는 더 부를 수 없다. 지금까지 받은 결과로 이 단계를 끝낸다."
# 포털 Envelope 상한(routines.Envelope) — 넘으면 포털이 422 로 통째로 거절하므로 보내기 전에 맞춘다
_SUMMARY_MAX, _REASON_MAX, _FINDINGS_MAX, _FINDING_MAX, _REFS_MAX = 6000, 500, 20, 500, 10
_KEYS_MAX, _KEY_NAME_MAX, _KEY_VAL_MAX, _CANDS_MAX, _CAND_MAX = 16, 32, 200, 20, 200
_ENV_OUT_CAP = 4096          # 봉투 턴 출력 상한(langchain-openai 1.x 는 max_completion_tokens 로 보낸다)
_FRAME_SUMMARY = 600         # step_done 프레임의 요약 글자
_SECTION_MAX = 20000         # 보고서 절 하나(포털이 이 길이에서 자른다)
_SLICE = 400                 # 보고서 token 조각 · 생각 칸 조각
_PAGE_TOOL = "routine_page"
_PLAN_HEAD = b'event: routine\ndata: {"kind": "plan"'   # 기록 속 plan 프레임의 머리(_Ctx.routine 이 kind 를 맨 앞에 싣는다)
_DELIB = "hwax-deliberation"
_STEP_SECONDS, _STEP_CALLS = 300.0, 8   # 계획에 예산이 없을 때(포털 Budget 기본값)
_NOTE_MAX = 2000             # 사용자가 덧붙인 말(포털 RunIn.note · 게이트웨이 routine_run_start · 중계 RoutineReq 와 같은 상한)
# 원장 call_id(<단계>.c<n>) — 요약 · 발견의 인용 [voc.c1] 의 숫자는 출처 있는 값이 아니다(씨앗에 넣으면 test_id=1 같은 추측이 통과한다)
_CALL_REF = re.compile(r"[a-z0-9_]+\.c\d+")


def _app():
    """app 모듈 — 부를 때 읽는다(app 이 이 모듈을 붙이므로 맨 위에서 읽으면 순환 import 다 — thinking.py 선례)."""
    import app as _a  # noqa: PLC0415
    return _a


async def _maybe(v):
    """시험 이음매의 값 — 기다릴 것이면 기다린다(동기 함수로 바꿔 끼운 시험도 그대로 돈다)."""
    return await v if inspect.isawaitable(v) else v


def _text_of(block) -> str:
    return str(block.get("text") or "") if isinstance(block, dict) and block.get("type") == "text" else ""


def _content_text(m) -> str:
    """LLM 메시지 · 조각의 글 — content 가 목록(조각들)이어도 글만 잇는다."""
    c = getattr(m, "content", m)
    if isinstance(c, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return c if isinstance(c, str) else str(c or "")


def _as_list(v) -> list:
    return v if isinstance(v, list) else []


def _last_data(text: str) -> dict:
    """SSE 응답의 마지막 `data:` 줄 JSON(게이트웨이는 event-stream 으로 답한다) — 줄이 없으면 본문 전체를 JSON 으로."""
    lines = [ln[5:].strip() for ln in (text or "").splitlines() if ln.startswith("data:")]
    return json.loads(lines[-1]) if lines else json.loads(text)


def _original(backend: str, exposed: str) -> str:
    """노출 이름 → 원래 이름 — 포털 /routines-api _catalog 와 같은 규칙(<백엔드에서 하이픈 뺀 것>_ 접두면 뗀다, 아니면 그대로)."""
    p = backend.replace("-", "") + "_"
    return exposed[len(p):] if exposed.startswith(p) else exposed


# ── 오류 ──────────────────────────────────────────────────────────────────────────────────────────────────────
class RoutineGatewayError(Exception):
    """게이트웨이 · 포털 쪽 실패 — SSE error 의 code 가 routine_gateway 다(까닭 글 그대로)."""


class _Refused(RoutineGatewayError):
    """ROUTINE_ERROR — 게이트웨이 · 포털이 까닭을 붙여 거절했다(원장에 남지 않는다). 글은 접두 뒤 그대로다."""


class _AuthRejected(RoutineGatewayError):
    """게이트웨이가 사람 PAT 를 받지 않았다(401) — 서비스 계정으로 다시 부르지 않고 실행을 멈춘다(사람이 다시 보내면 새 토큰이 실린다)."""


# ── 게이트웨이 원 세션 ─────────────────────────────────────────────────────────────────────────────────────────
class _GwSession:
    """실행 하나에 MCP 원 세션 하나 — initialize → notifications/initialized → tools/call → DELETE(포털 runner.GatewaySession 과 같은 꼴).

    머리는 Bearer 사람 PAT · X-HWAX-Groups(비면 뺀다 — 게이트웨이는 빈 머리를 권한 0 인 사람으로 읽는다) · X-HWAX-User 다. 연결 설정의
    Authorization(게이트웨이 공유 비밀)은 싣지 않는다. 세션 만료(게이트웨이 재기동 — 404) · 새 세션 거절(503 draining) · 보내기 전 끊김은 한 번
    다시 열어 다시 보낸다. 보낸 뒤 끊긴 호출은 읽기(idempotent)만 다시 보낸다 — routine_call 을 다시 보내면 원장 줄 · 예산이 두 번 든다.
    호출은 하나씩 나간다(잠금)."""

    def __init__(self, url: str, user_pat: str, groups: list, user: str):
        self.url = url
        self._pat = user_pat
        self._groups = [str(g) for g in groups or [] if str(g)]
        self._user = (user or "").strip().lower()
        self.sid: str | None = None
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()
        self._c: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._c is None:
            t = _app().MCP_CALL_TIMEOUT_S
            # 한 호출의 기한은 챗 · 심의 도구와 같은 MCP_CALL_TIMEOUT_S 다(게이트웨이 한도보다 커야 한다 — 안쪽이 먼저 걸려야 구체적인 까닭이 온다)
            self._c = httpx.AsyncClient(transport=GW_TRANSPORT, timeout=httpx.Timeout(t if t > 0 else None, connect=10.0))
        return self._c

    def _hdr(self) -> dict:
        A = _app()
        h = {"Authorization": f"Bearer {self._pat}", "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self._groups:
            h[A.GROUPS_HEADER] = quote(",".join(self._groups), safe=",")
        if self._user:
            h[A.USER_HEADER] = quote(self._user, safe="@.")
        if self.sid:
            h["mcp-session-id"] = self.sid
        return h

    async def _post(self, body: dict) -> httpx.Response:
        t = _app().MCP_CALL_TIMEOUT_S
        coro = self._client().post(self.url, headers=self._hdr(), json=body)
        # 벽시계 기한 — httpx 한도는 바이트 사이 간격이라 ping 을 싣는 게이트웨이 앞에서는 걸리지 않는다(MCP 세션의 요청 기한과 같은 뜻)
        return await (asyncio.wait_for(coro, t) if t > 0 else coro)

    async def _open(self) -> None:
        for attempt in (0, 1):
            self.sid = None
            r = await self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "initialize",
                                  "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                             "clientInfo": {"name": "hwax-agent-routine", "version": "1"}}})
            if r.status_code == 503 and attempt == 0:
                await asyncio.sleep(1.0)      # 게이트웨이 재기동 중(draining) — 새 세션만 막는다(retry-after 1)
                continue
            if r.status_code == 401:
                raise _AuthRejected("게이트웨이가 사람 토큰을 받지 않았다(401 — 만료 · 폐기). 서비스 계정으로 대신 부르지 않는다 — "
                                    "화면에서 다시 보내면 새 토큰이 실린다")
            if r.status_code != 200:
                raise RoutineGatewayError(f"게이트웨이 세션을 열지 못했다(HTTP {r.status_code})")
            self.sid = r.headers.get("mcp-session-id") or None
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            return

    async def call(self, name: str, arguments: dict, *, idempotent: bool = False) -> tuple[bool, list]:
        """(isError, content[]) 를 그대로 — 판정은 부르는 쪽이 한다. 전송 · 프로토콜 · 인증 실패는 RoutineGatewayError 다."""
        async with self._lock:
            for attempt in (0, 1):
                if self.sid is None:
                    try:
                        await self._open()
                    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                        self.sid = None        # 세션을 여는 중에 끊겼다 — 호출은 아직 안 보냈다
                        if attempt:
                            raise RoutineGatewayError(f"게이트웨이에 닿지 못했다({type(exc).__name__})") from None
                        await asyncio.sleep(1.0)
                        continue
                try:
                    r = await self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
                                          "params": {"name": name, "arguments": arguments}})
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    self.sid = None            # 보내기 전에 끊겼다 — 다시 열어 한 번 더
                    if attempt:
                        raise RoutineGatewayError(f"게이트웨이에 닿지 못했다({type(exc).__name__})") from None
                    await asyncio.sleep(1.0)
                    continue
                except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                    self.sid = None            # 보낸 뒤 끊겼다 — 읽기만 다시 보낸다(쓰기는 원장에 들었을 수 있다)
                    if attempt or not idempotent:
                        raise RoutineGatewayError(f"게이트웨이 답이 끊겼다({type(exc).__name__}) — {name}"
                                                  + ("" if idempotent else "(원장에는 남았을 수 있다)")) from None
                    continue
                if r.status_code == 404 or (r.status_code == 400 and "session" in r.text.lower()):
                    self.sid = None            # 세션 만료 — 게이트웨이가 재기동했다
                    if attempt:
                        raise RoutineGatewayError("게이트웨이 세션을 다시 열지 못했다")
                    continue
                if r.status_code == 401:
                    raise _AuthRejected("게이트웨이가 사람 토큰을 받지 않았다(401 — 만료 · 폐기). 서비스 계정으로 대신 부르지 않는다")
                if r.status_code != 200:
                    raise RoutineGatewayError(f"게이트웨이 HTTP {r.status_code} — {name}")
                try:
                    env = _last_data(r.text)
                except ValueError:
                    raise RoutineGatewayError(f"게이트웨이 답을 읽지 못했다 — {name}") from None
                if env.get("error"):
                    e = env["error"]
                    msg = e.get("message") if isinstance(e, dict) else str(e)
                    raise RoutineGatewayError(f"게이트웨이 JSON-RPC 오류 — {name}: {str(msg)[:300]}")
                res = env.get("result") or {}
                return bool(res.get("isError")), list(res.get("content") or [])
        raise RoutineGatewayError(f"게이트웨이를 부르지 못했다 — {name}")   # 닿지 않는다(위 고리가 늘 돌려주거나 올린다)

    async def tool(self, name: str, args: dict, *, idempotent: bool = False) -> dict:
        """루틴 도구(routine_get · run_start · step_done · report_submit) — 답 JSON. ROUTINE_ERROR 는 _Refused(접두 뒤 글 그대로)."""
        is_err, content = await self.call(name, args, idempotent=idempotent)
        first = _text_of(content[0]) if content else ""
        if first.startswith(_REFUSED):
            raise _Refused(first[len(_REFUSED):].strip())
        if is_err:
            raise RoutineGatewayError(f"{name} 가 오류로 답했다 — {first[:300]}")
        try:
            obj = json.loads(first)
        except ValueError:
            raise RoutineGatewayError(f"{name} 의 답을 읽지 못했다") from None
        if not isinstance(obj, dict):
            raise RoutineGatewayError(f"{name} 의 답이 객체가 아니다")
        return obj

    async def rcall(self, run_id: str, step: str, name: str, arguments: dict) -> dict:
        """routine_call — {call_id, ok, body, pages, images} 또는 거절 {refused}. 첫 블록이 ROUTINE_ERROR 면 거절이고(원장 없음), 아니면 첫
        블록이 머리 JSON 이고(raw_decode — 뒤에 글이 붙어 와도 읽는다) 나머지 블록이 본문이다. isError 는 안쪽 도구의 것이다(원장 줄은 있다)."""
        is_err, content = await self.call("routine_call", {"run_id": run_id, "step": step, "name": name, "arguments": arguments})
        first = _text_of(content[0]) if content else ""
        if first.startswith(_REFUSED):
            return {"refused": first, "ok": False}
        try:
            head, end = json.JSONDecoder().raw_decode(first)
        except ValueError:
            head, end = None, 0
        if not isinstance(head, dict) or not head.get("routine_call"):
            raise RoutineGatewayError("routine_call 의 답에 머리(call_id)가 없다 — 게이트웨이가 이 실행기와 다른 판이다")
        rest = first[end:].strip()
        texts = ([rest] if rest else []) + [_text_of(b) for b in content[1:] if _text_of(b)]
        images = sum(1 for b in content[1:] if isinstance(b, dict) and b.get("type") == "image")
        try:
            pages = max(1, int(head.get("pages") or 1))
        except (TypeError, ValueError):
            pages = 1
        return {"call_id": str(head["routine_call"]), "ok": bool(head.get("ok")) and not is_err, "body": "\n".join(texts),
                "pages": pages, "images": images}

    async def close(self) -> None:
        try:
            if self.sid:
                await asyncio.wait_for(self._client().request("DELETE", self.url, headers=self._hdr()), 5.0)
        except Exception:  # noqa: BLE001 — 닫기 실패로 실행을 실패시키지 않는다(게이트웨이 세션은 유휴 만료가 없다 — 그래서 닫는다)
            pass
        finally:
            self.sid = None
            if self._c is not None:
                try:
                    await self._c.aclose()
                except Exception:  # noqa: BLE001
                    pass
                self._c = None


# ── 시험 이음매 ───────────────────────────────────────────────────────────────────────────────────────────────
def _gateway_url(app) -> str:
    """게이트웨이 MCP 주소 — 에이전트의 MCP 연결 설정(MCP_CONFIG · MCP_SERVERS)에서. 이름이 gateway 인 것, 없으면 주소가 있는 첫 것."""
    conns = getattr(getattr(app, "state", None), "connections", None) or {}
    cfg = conns.get("gateway") if isinstance(conns.get("gateway"), dict) else None
    cfg = cfg or next((c for c in conns.values() if isinstance(c, dict) and c.get("url")), None) or {}
    url = str(cfg.get("url") or "").strip()
    if not url:
        raise RoutineGatewayError("게이트웨이 연결 설정이 없다(MCP_CONFIG 의 gateway url)")
    return url


async def _routine_llm(app):
    """(실행기 LLM, 창 덮어쓰기 토큰 또는 None) — thinking._think_llm 과 같은 셈. ROUTINE_LLM=chat 이고 챗 전용 LLM 이 켜져 있으면 그것(Qwen —
    GLM 넘김 포함)이고, 예산은 그 창으로 잡는다(두 서버 중 작은 창 − 출력 몫). 아니면 VLLM_*(GLM). 떼어 낸 작업 **안에서** 부른다 — 창 덮어쓰기는
    문맥 변수라 작업 밖에서 걸면 작업이 모른다(그러면 예산이 GLM 1M 창으로 잡혀 Qwen 이 넘치고 조용히 GLM 으로 넘어간다)."""
    if ROUTINE_LLM != "chat" or not getattr(app.state, "chat_llm_cfg", None):
        return app.state.llm, None
    A = _app()
    llm, tok = A._chat_llm_of(app), None
    cc = await asyncio.to_thread(A._chat_context_tokens)
    if cc:
        win = min(cc, await asyncio.to_thread(A._model_context_tokens))
        out = int(app.state.chat_llm_cfg.get("output_reserve") or 0)
        tok = external_llm.ctx_override.set(win - min(out, win // 2))
    return llm, tok


async def _user_tools(app, groups, user, user_pat) -> dict:
    """그 사람의 게이트웨이 도구 {노출 이름: 도구}(비동기) — 이름 · 설명 · 인자 스키마를 빌릴 뿐 이 도구를 부르지는 않는다(부르면 울타리 ·
    원장을 건너뛴다). 사람 PAT 로만 붙는다 — 401 이면 그대로 오른다(일반 로더의 '서비스 계정으로 강등' 길을 타지 않는다)."""
    A = _app()
    scoped = A._with_groups(getattr(app.state, "connections", None) or {}, list(groups or []), user or "", user_pat)
    return {t.name: t for t in await A._get_tools_retry(scoped)}


async def _tool_backends(app) -> dict:
    """노출 이름 → 게이트웨이 백엔드 키(비동기) — /tools-map(app._tools_map 의 5분 캐시, 블로킹 urllib 이라 스레드로)."""
    return dict(await asyncio.to_thread(_app()._tools_map) or {})


# ── 레지스트리 · 구독 ─────────────────────────────────────────────────────────────────────────────────────────
class _Live:
    """살아 있는 실행 하나 — 작업 · 지금까지 낸 프레임(다시 붙기) · 구독자 큐 · 마지막으로 읽은 실행 보기."""

    def __init__(self, user: str, key: str):
        self.user, self.key = user, key
        self.run_id: str | None = None
        self.task: asyncio.Task | None = None
        self.history: deque = deque(maxlen=max(1, int(ROUTINE_HISTORY_FRAMES)))
        self.subs: set = set()
        self.view: dict | None = None
        self.closed = False
        self.done_sent = False
        self.parked = False          # 쉼 — 새 심의 멈춤이 풀리길 · 줄 선 심의가 시작되길 기다린다(active_count 가 세지 않는다)

    def alive(self) -> bool:
        return self.task is not None and not self.task.done()

    def emit(self, frame: bytes, keep: bytes | None = None) -> None:
        """구독자에게는 frame, 기록(다시 붙기)에는 keep(없으면 frame) — 큰 칸을 뺀 사본을 기록에 둘 때 keep 을 준다."""
        self.history.append(frame if keep is None else keep)
        for q in list(self.subs):
            q.put_nowait(frame)

    def finish(self) -> None:
        self.closed = True
        for q in list(self.subs):
            q.put_nowait(None)


def active_count() -> int:
    """살아 있는 실행 가운데 쉬지 않는 것 — /health routine_active 이고 delib_active 에도 더한다(재기동 판정이 루틴을 기다린다 — R3-D7). 쉬는
    실행(parked — 새 심의 멈춤이 풀리길 · 줄 선 심의가 시작되길 기다린다)은 세지 않는다. update-all 은 새 심의를 멈추고 delib_active 가 0 이
    되길 기다리는데, 그 멈춤이 쉬는 실행의 심의를 붙잡고 있어 세면 기다림이 끝나지 않는다(재기동되면 그 실행은 running 으로 남고 사람이 '이어서')."""
    return sum(1 for lv in list(_RUNS.values()) if lv.alive() and not lv.parked)


def parked_count() -> int:
    """쉬는 실행 수 — /health routine_parked."""
    return sum(1 for lv in list(_RUNS.values()) if lv.alive() and lv.parked)


def _alive_count(skip: _Live | None = None) -> int:
    """살아 있는 실행 수(쉬는 것 포함) — 동시 상한이 본다. 쉬는 실행도 멈춤이 풀리면 한꺼번에 깨어나 챗 LLM · 게이트웨이를 쓴다. skip — 세지
    않을 실행(도는 실행을 잇는 요청은 이미 레지스트리에 올라 있다 — 자기는 빼고 센다)."""
    return sum(1 for lv in list(_RUNS.values()) if lv.alive() and lv is not skip)


def _forget(live: _Live) -> None:
    if _RUNS.get(live.key) is live:
        _RUNS.pop(live.key, None)


def _rekey(live: _Live, run_id: str) -> None:
    """임시 키로 올린 실행을 실행 id 로 옮긴다 — 그 id 로 '이어서' 가 오면 다시 붙는다."""
    if live.key == run_id:
        return
    other = _RUNS.get(run_id)
    if other is not None and other is not live and other.alive():
        return       # 같은 실행을 도는 작업이 이미 있다(일어나지 않는다) — 남의 자리를 덮지 않는다
    _forget(live)
    live.key = run_id
    _RUNS[run_id] = live


async def _subscribe(live: _Live, first: bytes | None = None):
    """구독 — (다시 붙기면 지금 원장 모양의 plan) · 지금까지의 프레임 · 이어지는 프레임. 조용한 동안 DELIB_HEARTBEAT_S 마다 ping 을 낸다.
    ⚠ 큐의 get 을 wait_for 로 감싸지 않는다 — 만료 때마다 get 을 취소하고, 취소와 도착이 겹친 프레임이 사라질 수 있다(app._detach_stream 과 같은
    까닭). get 을 쥔 채 asyncio.wait(timeout) 으로 기다린다. 구독이 끊기면 구독만 빠지고 작업은 그대로 돈다."""
    q: asyncio.Queue = asyncio.Queue()
    hist, closed = list(live.history), live.closed
    if not closed:
        live.subs.add(q)
    get = None
    try:
        if first is not None:
            yield first
        for fr in hist:
            if first is not None and fr.startswith(_PLAN_HEAD):
                continue        # 옛 plan(그때 모양 · resumed 거짓)은 다시 보내지 않는다 — 차례로 합치는 화면이 지금 모양을 옛것으로 덮는다
            yield fr
        if closed:
            return
        last = time.monotonic()
        while True:
            if get is None:
                get = asyncio.ensure_future(q.get())
            hb = float(getattr(_app(), "DELIB_HEARTBEAT_S", 15.0) or 0)
            if hb > 0:
                done, _pending = await asyncio.wait({get}, timeout=hb)
                if not done:
                    yield _sse("ping", {"idle_s": int(time.monotonic() - last), "ts": int(time.time() * 1000)})
                    continue
            fr, get = await get, None
            if fr is None:
                break
            last = time.monotonic()
            yield fr
    finally:
        live.subs.discard(q)
        if get is not None and not get.done():
            get.cancel()


def _bad_request(spec) -> str:
    """요청 칸 routine 의 꼴 — 틀리면 그 까닭(R3-design 3.1). 형 맞추기는 포털이 실행을 만들 때 한다."""
    if not isinstance(spec, dict):
        return "routine 은 객체다 — {id, version?, inputs, note?}(새 실행) 또는 {run_id, upload_ra?}(이어서 · RA 초안 올리기)"
    note = spec.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > _NOTE_MAX):
        return f"note(덧붙인 말)는 {_NOTE_MAX:,}자까지의 글이다"
    if spec.get("upload_ra") is not None and not isinstance(spec.get("upload_ra"), bool):
        return "upload_ra 는 참거짓이다"
    run = spec.get("run_id")
    if run not in (None, ""):
        if not isinstance(run, str) or not run.strip() or len(run) > 64:
            return "run_id 는 64자까지의 글이다"
        return ""
    rid = spec.get("id")
    if not isinstance(rid, str) or not rid.strip():
        return "id(루틴 id) 또는 run_id(이어서) 가운데 하나가 있어야 한다"
    if len(rid) > 64:
        return "id 는 64자까지다"
    v = spec.get("version")
    if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
        return "version 은 1 이상의 정수다"
    if spec.get("inputs") is not None and not isinstance(spec.get("inputs"), dict):
        return "inputs 는 객체다"
    return ""


async def _new_work_refusal(others: int) -> list:
    """새 일 앞의 문 — 새 시작(run_routine)과 도는 실행 잇기(_drive 의 이어서)가 같은 규칙 · 같은 꼴로 거절한다. 새 심의 멈춤(/drain
    scope=delib)이면 _paused_stream 그대로(심의와 같은 꼴 — 안 지키면 update-all 의 기다림이 수렴하지 않는다), 이 요청 밖의 살아 있는 실행(쉬는
    것 포함)이 상한 이상이면 routine_busy. 막으면 error · done 프레임, 아니면 빈 목록. 새 일을 시작하지 않는 요청(끝난 실행의 안내 · RA 초안
    올리기)은 이 문을 지나지 않는다 — 문은 upload_ra 깃발이 아니라 원장의 실행 상태로 정한다(도는 실행에 붙은 upload_ra 는 그대로 잇는 새 일이다)."""
    A = _app()
    paused = A._delib_pause_info()
    if paused:
        return [fr async for fr in A._paused_stream(paused)]
    if others >= ROUTINE_MAX_RUNNING:
        return [_sse("error", {"code": "routine_busy", "message": f"분석 루틴이 이미 {others}개 돈다(동시 상한 {ROUTINE_MAX_RUNNING}) — 잠시 뒤 "
                                                                  f"다시 보낸다", "retryable": True, "retry_after_s": 60}),
                _sse("done", {})]
    return []


async def run_routine(app, req):
    """app.chat() 의 루틴 가지 — SSE 바이트 비동기 생성기(R3-design §4). 검사(꼴 · 사람 PAT) → 다시 붙기(같은 프로세스에 살아 있는 같은 사람의
    실행) → 새 시작이면 심의 멈춤 · 동시 상한(_new_work_refusal) → 떼어 낸 작업을 레지스트리에 올리고 구독한다. run_id 요청의 문은
    _drive 가 실행 보기를 읽은 뒤 도는 실행을 이을 때만 본다(끝난 실행의 안내 · RA 초안 올리기는 새 일이 아니다). 모든 스트림은 done{} 으로
    끝난다."""
    spec = getattr(req, "routine", None)
    bad = _bad_request(spec)
    if bad:
        yield _sse("error", {"code": "routine_bad_request", "message": f"루틴 요청 꼴이 틀렸다 — {bad}"})
        yield _sse("done", {})
        return
    if not (getattr(req, "user_pat", "") or "").strip():
        # 서비스 계정(GW_TOKEN)으로는 돌지 않는다 — 게이트웨이가 루틴 도구를 거절하고, 원장 · 보고서가 그 사람 이름이어야 한다
        yield _sse("error", {"code": "routine_no_pat", "message": "루틴은 사람 토큰(챗 PAT)으로만 돈다 — 포털이 이번 요청에 토큰을 싣지 "
                                                                  "못했다. 잠시 뒤 다시 보낸다(계속되면 다시 로그인한다)"})
        yield _sse("done", {})
        return
    user = (getattr(req, "user_email", "") or "").strip().lower()
    run_id = str(spec.get("run_id") or "").strip()
    if run_id:
        live = _RUNS.get(run_id)
        if live is not None and live.alive():
            if live.user != user:
                # 남의 실행 id — 포털 원장도 주인만 보인다(같은 답). 남의 자리를 덮지도 않는다.
                yield _sse("error", {"code": "routine_gateway", "message": "실행을 찾을 수 없다"})
                yield _sse("done", {})
                return
            # 다시 붙기 — 새 작업 없이 지금 원장 모양의 plan · 기록 · 이어지는 프레임(두 탭이 같은 실행을 두 번 돌지 않는다 — R3-D8)
            sub = _subscribe(live, _sse("routine", _plan_frame(live.view, True)) if live.view else None)
            try:
                async for fr in sub:
                    yield fr
            finally:
                await sub.aclose()      # 브라우저가 떠났다 — 구독만 지금 빠진다(안 닫으면 GC 때까지 큐가 프레임을 쌓는다)
            return
    else:
        # 새 시작 — 새 심의 멈춤 · 동시 상한(_new_work_refusal). run_id 요청(이어서 · RA 초안 올리기)은 실행 보기를 읽어야 새 일인지
        # 안다 — _drive 가 읽고 도는 실행을 이을 때만 같은 문을 본다(끝난 실행의 안내 · RA 초안 올리기는 멈춤 · 상한 중에도 된다 — 실행기
        # 자리도 심의도 쓰지 않는다)
        refused = await _new_work_refusal(_alive_count())
        if refused:
            for fr in refused:
                yield fr
            return
    live = _Live(user, run_id or f"pending-{secrets.token_hex(8)}")
    _RUNS[live.key] = live
    live.task = asyncio.create_task(_drive_task(app, req, live), name=f"routine-{live.key[:24]}")
    live.task.add_done_callback(lambda _t, lv=live: _forget(lv))
    sub = _subscribe(live)
    try:
        async for fr in sub:
            yield fr
    finally:
        await sub.aclose()              # 브라우저가 떠나도 작업은 돈다 — 구독만 지금 빠진다


async def shutdown(grace_s: float = 2.0) -> None:
    """에이전트 서버가 내려간다 — 살아 있는 실행 작업을 멈춘다. 원장은 running 으로 남는다(사람이 '이어서' — 사람 PAT 는 재기동 때 사라진다)."""
    tasks = [lv.task for lv in list(_RUNS.values()) if lv.alive()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=grace_s)


# ── 실행 하나의 문맥 ──────────────────────────────────────────────────────────────────────────────────────────
def _plan_frame(view: dict | None, resumed: bool) -> dict:
    """routine{plan} — 실행 보기(run_view)에서. 다시 붙기는 지금 원장 모양이다."""
    view = view or {}
    run = view.get("run") or {}
    rep = view.get("report") or {}
    steps = []
    for s in view.get("steps") or []:
        bks = [str(b) for b in s.get("backends") or []]
        label = s.get("label").strip() if isinstance(s.get("label"), str) else ""
        steps.append({"id": s.get("id"), "title": s.get("title"), "kind": s.get("kind"), "state": s.get("state"), "backends": bks,
                      # 보이기용 이름 — 계획 행의 라벨(고를 때의 앱 라벨 사본, 포털 plan_steps)이 비지 않았으면 그것. 비었거나 없으면(옛 포털)
                      # 백엔드 키 그대로, 심의 단계는 '심의'
                      "label": label or ("심의" if s.get("kind") == "deliberation" else " · ".join(bks))})
    return {"kind": "plan", "run_id": run.get("id"), "routine_id": run.get("routine_id"), "version_no": run.get("version_no"),
            "title": run.get("title"), "purpose": view.get("purpose") or "", "state": run.get("state"), "resumed": bool(resumed),
            "steps": steps, "report": {"title": rep.get("title"), "sections": list(rep.get("sections") or []), "upload": rep.get("upload")}}


class _Ctx:
    """떼어 낸 작업 하나의 문맥 — 요청 · 원 세션 · LLM · 지금 실행 보기 · 이 프로세스가 본 원장 호출."""

    def __init__(self, app, req, live: _Live):
        self.app, self.req, self.live = app, req, live
        self.spec = dict(getattr(req, "routine", None) or {})
        self.user = live.user
        self.email = (getattr(req, "user_email", "") or "").strip()
        self.pat = (getattr(req, "user_pat", "") or "").strip()
        self.groups = [str(g) for g in (getattr(req, "groups", None) or []) if str(g)]
        self.gw: _GwSession | None = None
        self.llm = None
        self.run_id: str | None = None
        self.view: dict = {}
        self.tools: dict | None = None
        self.exposed_of: dict = {}       # (백엔드, 원래 이름) → 노출 이름
        self._bmap: dict | None = None
        self.lock = asyncio.Lock()       # 실행마다 하나 — 병렬 tool_calls 도 routine_call 은 하나씩(예산 검사와 원장 쓰기 사이의 경합)
        self.calls: dict = {}            # 단계 id → [{call_id, tool, exposed, args, ok, texts}] — 이 프로세스가 받은 원장 호출(texts 는 본문 · 읽은 쪽)
        self.seen_all: list = []         # 이 실행에서 받은 결과 글 전부 — 인사이트의 출처 없는 수치 대조
        self.findings: dict = {}         # 단계 id → 이 실행기가 보낸 발견(실행 보기에 findings 가 없는 옛 포털 대비)
        self.halt: dict = {}             # 단계 id → 더 부르지 않는 까닭(예산 · 상한 · 실행 상태 거절)
        self.paged: dict = {}            # 단계 id → 쪽이 여럿인 결과를 받았나(routine_page 를 묶는다)
        self.cap: dict = {}              # 단계 id → 결과 한 건을 LLM 에 보이는 글자 상한
        self.view_fails = 0              # 긴 기다림 사이 실행 보기 읽기가 잇달아 실패한 수(_view_soft) — 어디서든 한 번 읽히면 0 이다(read_view)

    # 프레임
    def emit(self, event: str, data: dict, slim: tuple = ()) -> None:
        """slim — 기록(다시 붙기)에서 뺄 큰 칸. 넓은 창의 result_full 은 한 칸이 15만 자까지라 400 프레임 기록이 실행마다 수십 MB 가 된다 —
        처음 구독(포털 중계는 브라우저가 떠나도 끝까지 읽는다)은 다 받고, 다시 붙은 구독은 지난 호출을 미리보기로만 받는다."""
        cut = {k: v for k, v in data.items() if k not in slim}
        self.live.emit(_sse(event, data), _sse(event, cut) if len(cut) < len(data) else None)

    def routine(self, kind: str, **kw) -> None:
        self.emit("routine", {"kind": kind, "run_id": self.run_id, **kw})

    def reason(self, text: str) -> None:
        if text:
            self.emit("reasoning", {"delta": text})

    def say(self, text: str) -> None:
        """보고서에 못 간 끝의 짧은 안내 — 중계는 답 글이 비면 대화에 남기지 않는다(R3-design 3.2)."""
        self.emit("token", {"delta": text})
        self.emit("result", {"type": "text", "content": text})

    def end(self, state: str, reason: str = "") -> None:
        self.routine("done", state=state, **({"reason": str(reason)[:_REASON_MAX]} if reason else {}))
        self.emit("done", {})
        self.live.done_sent = True

    @property
    def note(self) -> str:
        """사용자가 덧붙인 말 — 실행 보기의 note(시작 때 원장에 한 번 남긴 것이라 이어서 · 다시 붙기 · 고른 뒤 잇기에도 같은 말이다). 실행
        보기에 없으면(그 칸을 모르는 옛 포털 · 게이트웨이) 이 요청의 note(새 실행만 싣는다). 요청 글(message)은 쓰지 않는다 — 화면은 글이 비면
        '루틴: <제목>', 이어서는 '루틴 이어서: <제목>' 을 보내 그것이 사람의 말을 덮었다."""
        return str(self.view.get("note") or self.spec.get("note") or "").strip()

    # 실행 보기
    def adopt(self, view: dict) -> None:
        self.view = view if isinstance(view, dict) else {}
        self.live.view = self.view
        rid = str((self.view.get("run") or {}).get("id") or "")
        if rid and rid != self.run_id:
            self.run_id = self.live.run_id = rid
            _rekey(self.live, rid)
            call_trace.tag(job=rid)
        src = _app()._turn_ids.get()
        if src is not None:
            # 단계 프롬프트가 보이는 것의 정수도 '출처 있는 값' 이다(유령 ID 막이) — 입력 · 덧붙인 말 · 넘긴 키, 그리고 앞 단계의 요약 · 발견.
            # 이어서(새 작업)는 앞 작업이 도구 결과에서 배운 ID 를 모른다 — 요약 · 발견이 보이는 ID 로 부르는 호출이 막히지 않게(라이브 길과
            # 같게). 인용 [voc.c1] 의 숫자는 빼고 센다(_CALL_REF).
            A = _app()
            src |= A._int_tokens(json.dumps(self.view.get("inputs") or {}, ensure_ascii=False, default=str))
            src |= A._int_tokens(str(self.view.get("note") or ""))
            for s in self.view.get("steps") or []:
                if s.get("keys"):
                    src |= A._int_tokens(json.dumps(s["keys"], ensure_ascii=False, default=str))
                seen = [str(s.get("summary") or "")] + [str(f.get("text") or "") for f in _as_list(s.get("findings")) if isinstance(f, dict)]
                src |= A._int_tokens(_CALL_REF.sub(" ", "\n".join(seen)))

    async def read_view(self) -> dict:
        view = await self.gw.tool("routine_get", {"run_id": self.run_id}, idempotent=True)
        # 읽혔다 — 잇달은 끊김은 여기서 끝난다. 단계 사이 · 상한 닫기 · 보고서의 직접 읽기도 센다(_view_soft 만 되돌리면 앞 기다림 끝의 끊김이
        # 남아 다음 기다림의 한 번의 끊김으로 '잇달아 세 번' 이 된다)
        self.view_fails = 0
        self.adopt(view)
        return self.view

    # 도구
    async def bmap(self) -> dict:
        if self._bmap is None:
            try:
                self._bmap = dict(await _maybe(_tool_backends(self.app)) or {})
            except Exception as exc:  # noqa: BLE001 — 아래에서 '못 읽었다' 로 말한다
                print(f"[routine] /tools-map 을 읽지 못했다 — {type(exc).__name__}: {exc}"[:300])
                self._bmap = {}
        return self._bmap

    async def ensure_tools(self) -> None:
        if self.tools is not None:
            return
        try:
            got = await _maybe(_user_tools(self.app, self.groups, self.email, self.pat))
        except RoutineGatewayError:
            raise
        except Exception as exc:  # noqa: BLE001
            while getattr(exc, "exceptions", None):      # anyio TaskGroup 에 싸여 온다 — 안쪽 예외로 말한다
                exc = exc.exceptions[0]
            s = f"{type(exc).__name__}: {exc}"
            if "401" in s or "403" in s:
                raise _AuthRejected(f"내 도구 목록을 받지 못했다 — 게이트웨이가 사람 토큰을 받지 않았다({s[:160]}). 서비스 계정으로 대신 "
                                    "받지 않는다") from None
            raise RoutineGatewayError(f"내 도구 목록을 받지 못했다 — {s[:200]}") from None
        if not got:
            # 빈 목록은 '권한 없음' 이 아니라 게이트웨이의 과도 상태다(막 떠서 도구 0 — _get_tools_retry 는 마지막 시도의 빈 목록을 그대로
            # 돌려주고, 성한 게이트웨이는 안내대 도구를 늘 싣는다). 열린 단계를 다 건너뜀으로 닫으면 실행이 done 이 되어 '이어서' 를 못 한다.
            raise RoutineGatewayError("내 도구 목록이 비었다 — 게이트웨이가 막 떠서 도구를 아직 싣지 않았을 수 있다. 잠시 뒤 '이어서' 로 잇는다")
        self.tools = dict(got)
        bmap = await self.bmap()
        if not bmap:
            raise RoutineGatewayError("게이트웨이 /tools-map 을 읽지 못했다 — 도구가 어느 백엔드인지 몰라 단계 도구를 묶지 못한다")
        for name in self.tools:
            bk = bmap.get(name)
            if bk and bk != "_gateway":
                self.exposed_of.setdefault((bk, _original(bk, name)), name)

    async def exposed(self, backend: str, original: str) -> str:
        """(백엔드, 원래 이름) 의 노출 이름 — 화면의 tool 칸 · 원 세션으로 직접 부를 때. 못 찾으면 원래 이름."""
        hit = self.exposed_of.get((backend, original))
        if hit:
            return hit
        for name, b in (await self.bmap()).items():
            if b == backend and _original(backend, name) == original:
                return name
        return original


def _seed_ids(ctx: _Ctx) -> set:
    """유령 ID 막이의 씨앗 — 사용자가 덧붙인 말 · 입력의 정수(app._agent_stream_turn 의 씨앗과 같은 뜻). 넘긴 키 · 앞 단계의 요약 · 발견은 실행
    보기를 읽을 때 더한다(adopt). 요청 글(message — '루틴: <제목>' 일 수 있다)의 정수는 넣지 않는다."""
    A = _app()
    return set(A._int_tokens(ctx.note)) | A._int_tokens(json.dumps(ctx.spec.get("inputs") or {}, ensure_ascii=False, default=str))


def _fail(ctx: _Ctx, code: str, msg: str) -> None:
    """어디서든 터지면 — error · 짧은 안내 · routine{done, running}(이어서 가능) · done. 실행 id 를 아직 모르면(시작 실패) error · done 만."""
    if ctx.live.done_sent:
        return
    msg = str(msg)[:600]
    ctx.emit("error", {"code": code, "message": msg})
    if ctx.run_id:
        ctx.say(f"실행기가 멈췄다 — {msg}\n원장은 포털에 남아 있다 — '이어서' 로 잇는다.")
        ctx.end("running", msg)
    else:
        ctx.emit("done", {})
        ctx.live.done_sent = True
    print(f"[routine] 멈춤({code}) — 실행 {ctx.run_id or '(시작 전)'} · {msg[:240]}")


async def _drive_task(app, req, live: _Live) -> None:
    """떼어 낸 작업의 몸 — 문맥 변수는 **이 작업 안에서** 건다(챗 LLM 창 · 대화 머리 · 유령 ID 씨앗 · 호출 추적 자리). 작업 밖에서 걸면 작업이
    모른다. 끝나면 원 세션을 닫고 구독자에게 끝을 알린다."""
    A = _app()
    ctx = _Ctx(app, req, live)
    toks: list = []
    try:
        ctx.llm, tok = await _maybe(_routine_llm(app))
        if tok is not None:
            toks.append((external_llm.ctx_override, tok))
        toks.append((A._chat_affinity, A._chat_affinity.set(A._affinity_key(req))))   # 챗 LLM 호출의 대화 머리(W-1 — 같은 GPU 복제본)
        toks.append((A._turn_ids, A._turn_ids.set(_seed_ids(ctx))))
        call_trace.tag(kind="routine", unit=None)
        ctx.gw = _GwSession(_gateway_url(app), ctx.pat, ctx.groups, ctx.user)
        await _drive(ctx)
    except asyncio.CancelledError:
        _fail(ctx, "routine_internal", "에이전트 서버가 내려가 실행기를 멈췄다")
        raise
    except RoutineGatewayError as exc:
        _fail(ctx, "routine_gateway", str(exc))
    except Exception as exc:  # noqa: BLE001 — 작업 안 예외는 아무도 await 하지 않으면 무음이다
        logging.getLogger("agent").exception("[routine] 실행기 오류 — 실행 %s", ctx.run_id)
        _fail(ctx, "routine_internal", f"실행기 오류 — {type(exc).__name__}: {exc}")
    finally:
        if ctx.gw is not None:
            await ctx.gw.close()
        for var, tok in reversed(toks):
            try:
                var.reset(tok)
            except ValueError:      # 다른 문맥에서 닫혔다 — 그 문맥은 곧 버려진다
                pass
        if not live.done_sent:
            ctx.emit("done", {})
            live.done_sent = True
        live.finish()


# ── 실행 흐름 ─────────────────────────────────────────────────────────────────────────────────────────────────
async def _drive(ctx: _Ctx) -> None:
    """새 실행(routine_run_start — 덧붙인 말은 note 로 실행에 남긴다) 또는 이어서(routine_get → routine_run_start(run_id)) → 단계를 차례로 →
    보고서. 단계마다 실행 보기를 다시 읽는다(풀린 할 일 · 앞 단계 키 · 사람이 멈췄나 · 상한). 끝난 실행에 upload_ra 가 오면 RA 초안만 올린다
    (_upload_ra). 이어서는 실행 보기를 읽은 뒤 도는 실행을 이을 때만 새 시작과 같은 문(_new_work_refusal — 새 심의 멈춤 · 동시 상한)을 본다."""
    run_id = str(ctx.spec.get("run_id") or "").strip()
    resumed = bool(run_id)
    if run_id:
        ctx.run_id = ctx.live.run_id = run_id
        call_trace.tag(job=run_id)
        view = await ctx.read_view()
        state = (view.get("run") or {}).get("state")
        if state not in ("running", "interrupted"):
            # 새 일이 아니다 — 새 심의 멈춤 · 동시 상한(_new_work_refusal)을 보지 않는다(실행기 자리도 심의도 쓰지 않는다)
            ctx.routine("plan", **_drop_kind(_plan_frame(view, True)))
            if state == "done" and ctx.spec.get("upload_ra") is True:
                await _upload_ra(ctx)        # 끝난 실행의 RA 초안 올리기(화면의 'RA 초안 올리기' — ra.upload 'ask')
            else:
                _closed_end(ctx, view)       # upload_ra 는 끝난 실행에서만 본다 — 도는 실행은 아래에서 그대로 잇는다
            return
        # 도는 실행을 잇는다 — 새 일이라 새 시작과 같은 문(이 요청은 빼고 센다). 거절은 error · done 만이다(_fail · ctx.end 를 거치지 않는다 —
        # routine{done, running} 을 내지 않는다, 실행은 원장에 그대로 있고 다시 보내면 잇는다)
        refused = await _new_work_refusal(_alive_count(skip=ctx.live))
        if refused:
            for fr in refused:
                ctx.live.emit(fr)
            ctx.live.done_sent = True
            return
        ctx.adopt(await ctx.gw.tool("routine_run_start", {"run_id": run_id}))
    else:
        args: dict = {"routine_id": str(ctx.spec["id"]).strip(), "inputs": dict(ctx.spec.get("inputs") or {})}
        if ctx.spec.get("version"):
            args["version"] = int(ctx.spec["version"])
        cid = (getattr(ctx.req, "conversation_id", "") or "").strip()
        if cid:
            args["conversation_id"] = cid[:64]
        note = ctx.note
        if note:
            args["note"] = note              # 원장에 한 번 남긴다 — 이어서 · 고른 뒤 잇기의 프롬프트도 이 말이다(실행 보기의 note)
        ctx.adopt(await ctx.gw.tool("routine_run_start", args))
    view = ctx.view
    if not ctx.run_id:
        raise RoutineGatewayError("routine_run_start 의 답에 실행 id 가 없다")
    ctx.routine("plan", **_drop_kind(_plan_frame(view, resumed)))
    print(f"[routine] {'이어서' if resumed else '시작'} — 실행 {ctx.run_id} · {(view.get('run') or {}).get('title')} · "
          f"단계 {len(view.get('steps') or [])}개 · {ctx.user or '(신원 없음)'}")
    tried: set = set()
    while True:
        run = view.get("run") or {}
        if run.get("state") != "running":
            _closed_end(ctx, view)
            return
        opens = [s for s in view.get("steps") or [] if s.get("state") in _OPEN]
        if not opens:
            break
        if run.get("limit_reached"):
            try:
                await _close_by_limit(ctx, opens, str(run["limit_reached"]))
            except _Refused:
                view = await ctx.read_view()
                if (view.get("run") or {}).get("state") != "running":
                    continue           # 닫는 사이 실행이 멈췄다(사람이 멈춤 등) — 맨 위가 그 상태로 끝낸다('이어서' 를 말하지 않는다)
                raise
            view = await ctx.read_view()
            if any(s.get("state") in _OPEN for s in view.get("steps") or []):
                raise RoutineGatewayError("실행 상한으로 남은 단계를 닫지 못했다")
            continue
        step = opens[0]
        if step.get("id") in tried:
            raise RoutineGatewayError(f"단계 {step.get('id')} 가 닫히지 않았다 — 포털 원장을 본다")
        tried.add(step.get("id"))
        env = await _run_step(ctx, step)
        if env is not None:
            try:
                await _close_step(ctx, step, env)
            except _Refused:
                view = await ctx.read_view()
                if (view.get("run") or {}).get("state") not in ("running", "waiting"):
                    continue           # 단계 도중에 실행이 멈췄다(사람이 멈춤 등) — 맨 위가 그 상태로 끝낸다
                raise
        view = await ctx.read_view()
    await _report(ctx, view)


def _drop_kind(frame: dict) -> dict:
    return {k: v for k, v in frame.items() if k not in ("kind", "run_id")}


_END_LABEL = {"failed": "실패", "cancelled": "취소", "queued": "줄 서 있음", "interrupted": "끊김"}


def _closed_end(ctx: _Ctx, view: dict) -> None:
    """실행이 도는 상태가 아닐 때의 끝 — waiting 은 고를 것(gate), done 은 '이미 끝났다', 그 밖은 그 상태와 까닭."""
    run = view.get("run") or {}
    state, reason = str(run.get("state") or ""), str(run.get("reason") or "")
    if state == "waiting":
        st = next((s for s in view.get("steps") or [] if s.get("state") == "ambiguous"), None) or {}
        msg = (f"단계 〔{st.get('id')}〕 {st.get('title') or ''} — 고를 것이 있다. 후보 가운데 하나를 고르면 이어서 돈다."
               if st else "고를 것이 있다 — 후보를 고르면 이어서 돈다.")
        ctx.routine("gate", step=st.get("id"), candidates=list(st.get("candidates") or []), message=msg)
        ctx.say(msg)
        ctx.end("waiting", reason)
    elif state == "done":
        ctx.say("이 실행은 이미 끝났다 — 화면에서 보고서를 연다.")
        ctx.end("done")
    else:
        ctx.say(f"실행이 멈췄다({_END_LABEL.get(state, state or '모름')})" + (f" — {reason}" if reason else "") + ".")
        ctx.end(state or "failed", reason)


async def _close_by_limit(ctx: _Ctx, opens: list, limit: str) -> None:
    """실행 상한(시간 · 호출)에 닿았다 — 남은 열린 단계를 건너뜀으로 닫고 보고서로 간다(포털이 커버리지에 까닭을 적는다)."""
    reason = f"실행 상한 — {limit}"
    ctx.reason(f"\n\n({reason}: 남은 단계 {len(opens)}개를 건너뛰고 보고서로 간다)\n")
    for s in opens:
        res = await ctx.gw.tool("routine_step_done", {"run_id": ctx.run_id, "step": s.get("id"), "status": "skipped", "reason": reason,
                                                      "summary": "", "keys": {}, "findings": [], "candidates": []})
        ctx.routine("step_done", step=s.get("id"), state=res.get("state"), reason=reason, summary="", flags=list(res.get("flags") or []),
                    calls=int(s.get("calls") or 0))


async def _run_step(ctx: _Ctx, step: dict) -> dict | None:
    """단계 하나 — 봉투(또는 None: 단계 도중 실행이 도는 상태가 아니게 됐다)."""
    sid = str(step.get("id"))
    title = step.get("title") or sid
    ctx.routine("step", step=sid, title=title, state="running")
    ctx.reason(f"\n\n### 〔{sid}〕 {title}\n")
    call_trace.tag(unit=sid)
    if step.get("kind") == "deliberation":
        return await _delib_step(ctx, step)
    return await _platform_step(ctx, step)


async def _close_step(ctx: _Ctx, step: dict, env: dict) -> dict:
    """봉투를 routine_step_done 으로 — 프레임에는 **포털이 고친** 상태를 싣는다(호출 없이 ok 면 skipped 등)."""
    sid = str(step.get("id"))
    if env.get("summary"):
        ctx.reason(f"\n**단계 요약** — {env['summary']}\n")
    ctx.findings[sid] = list(env.get("findings") or [])
    res = await ctx.gw.tool("routine_step_done", {"run_id": ctx.run_id, "step": sid, **env})
    # 실행 보기의 calls(앞 프로세스 몫 포함) + 이 프로세스가 새로 부른 것 — 앞 프로세스의 호출을 실어 둔 것(_preload)은 이미 앞 수에 들었다
    calls = int(step.get("calls") or 0) + sum(1 for c in ctx.calls.get(sid) or [] if not c.get("prior"))
    ctx.routine("step_done", step=sid, state=res.get("state"), reason=env.get("reason") or "",
                summary=str(env.get("summary") or "")[:_FRAME_SUMMARY], flags=list(res.get("flags") or []), calls=calls)
    return res


# ── 단계 도구(울타리) ─────────────────────────────────────────────────────────────────────────────────────────
def _result_cap(calls_budget: int) -> int:
    """도구 결과 한 건을 LLM 에 보이는 글자 — ROUTINE_RESULT_CHARS, 0 이면 이 작업의 창에서(좌석 자유 조회와 같은 셈 — 호출 수로 나눈다)."""
    if int(ROUTINE_RESULT_CHARS or 0) > 0:
        return int(ROUTINE_RESULT_CHARS)
    try:
        from deliberation import _free_result_chars  # noqa: PLC0415 — app 을 늦게 읽는 함수다
        return int(_free_result_chars(max(1, calls_budget)))
    except Exception:  # noqa: BLE001 — 창을 못 재면 보수적인 값
        return 20000


def _proxy(ctx: _Ctx, sid: str, bk: str, orig: str, base):
    """그 사람의 노출 도구와 겉(이름 · 설명 · 인자 스키마)이 같고 속은 routine_call 인 새 도구 — 원래 도구는 부르지 않는다(부르면 울타리 ·
    원장을 건너뛴다). 챗 길의 인자 기본값(app._CHAT_ARG_DEFAULTS — plm_defect_search 의 enrich=0, 사용자 결정 10-27 D-3)도 같게 건다 —
    모델이 그 인자를 주지 않았을 때만 채우고 설명 끝에 그 글을 단다(app._cap_tool 과 같은 꼴). 루틴도 웹 챗에서 시작해 챗 LLM 으로 돈다."""
    from langchain_core.tools import StructuredTool  # noqa: PLC0415

    A = _app()
    own = A._arg_defaults_for(base.name, A._CHAT_ARG_DEFAULTS)
    desc = getattr(base, "description", "") or ""

    async def _run(**kwargs):
        for k, v in (own[0] if own else {}).items():
            kwargs.setdefault(k, v)
        return await _step_call(ctx, sid, bk, orig, base.name, kwargs)

    return StructuredTool(name=base.name, description=f"{desc} [{own[1]}]" if own else desc, coroutine=_run,
                          args_schema=getattr(base, "args_schema", None) or {"type": "object", "properties": {}})


def _bind(ctx: _Ctx, step: dict) -> tuple[list, list]:
    """(묶을 도구, 못 찾은 허용 도구) — 허용 목록 {백엔드: [원래 이름]} 의 각 도구를 그 사람의 노출 도구에서 찾아 _proxy 로. 안내대 도구
    (search_tools · invoke_tool …)는 묶지 않는다 — 허용 목록에 없다(R3-D3)."""
    sid = str(step.get("id"))
    tools, missing, seen = [], [], set()
    for bk, origs in (step.get("allowed") or {}).items():
        for orig in origs or []:
            name = ctx.exposed_of.get((bk, orig))
            base = (ctx.tools or {}).get(name) if name else None
            if base is None:
                missing.append(f"{bk}:{orig}")
            elif name not in seen:
                seen.add(name)
                tools.append(_proxy(ctx, sid, bk, orig, base))
    return tools, missing


async def _logged_call(ctx: _Ctx, sid: str, bk: str, orig: str, exposed: str, args: dict) -> dict:
    """routine_call 하나 — 시작 · 끝 status 프레임(call 로 짝, routine_step · 끝에는 routine_call)을 내고 원장 호출을 적는다(R3-design 3.2).
    tool 은 노출 이름, via 는 백엔드 키다. 인증 거절(401)은 끝 프레임을 낸 뒤 그대로 올린다(실행을 멈춘다)."""
    A = _app()
    pair = secrets.token_hex(6)
    det, full = A._tool_preview(args), A._handoff_text(args)
    ctx.emit("status", {"step": f"도구 호출: {exposed}", "tool": exposed, "via": bk, "call": pair, "routine_step": sid,
                        **({"detail": det} if det else {}), **({"detail_full": full} if len(full) > len(det) else {})},
             slim=("detail_full",))
    t0 = time.monotonic()
    rec: dict = {"ok": False, "error": "기다리기를 멈췄다(시간 예산 · 실행기 정지) — 게이트웨이에서는 끝났을 수 있다(원장을 본다)"}
    try:
        rec = await ctx.gw.rcall(ctx.run_id, sid, f"{bk}:{orig}", args)
    except _AuthRejected as exc:
        rec = {"ok": False, "error": str(exc)}
        raise
    except RoutineGatewayError as exc:
        rec = {"ok": False, "error": str(exc)}
    finally:
        out = rec.get("refused") or rec.get("error") or rec.get("body") or ""
        cid = rec.get("call_id") or ""
        prev, hand = A._tool_preview(out), A._handoff_text(out)
        ctx.emit("status", {"step": f"도구 {'완료' if cid or rec.get('refused') else '실패'}: {exposed}", "tool": exposed, "via": bk,
                            "ok": bool(rec.get("ok")), "ts": int(time.time() * 1000), "ms": round((time.monotonic() - t0) * 1000),
                            "call": pair, "routine_step": sid, **({"routine_call": cid} if cid else {}),
                            **({"result_preview": prev} if prev else {}), **({"result_full": hand} if len(hand) > len(prev) else {})},
                 slim=("result_full",))
    if rec.get("call_id"):
        ctx.calls.setdefault(sid, []).append({"call_id": rec["call_id"], "tool": f"{bk}:{orig}", "exposed": exposed, "args": args,
                                              "ok": rec["ok"], "texts": [rec["body"]]})
        ctx.seen_all.append(rec["body"])
    return rec


def _halting(text: str) -> bool:
    """이 거절 글 뒤에는 그 단계에서 더 부르지 않는다 — _HALT_MARKS 이거나 실행 상태 거절(포털 add_call 의 꼬리 없는 글 포함)."""
    return any(m in text for m in _HALT_MARKS) or bool(_RUN_STATE.search(text))


def _run_stopped(text: str) -> bool:
    """실행이 도는 상태가 아니라는 거절(사람이 멈춤 · 실패) — 게이트웨이 앞 검사 글이든 포털 add_call 글이든."""
    return "도구를 부르지 않는다" in text or bool(_RUN_STATE.search(text))


def _capped(body: str, cap: int, cid: str) -> str:
    """LLM 에 보이는 결과 한 건 — 상한에서 자르면 그렇다고 적는다(원장에는 전부 있다)."""
    return body if len(body) <= cap else (body[:cap] + f"\n…[결과 {len(body):,}자 가운데 앞 {cap:,}자만 실었다 — 원장 {cid} 에 전부 있다]")


async def _step_call(ctx: _Ctx, sid: str, bk: str, orig: str, exposed: str, args: dict) -> str:
    """단계 도구의 속 — 유령 ID 막이 → (실행마다 잠금) routine_call → LLM 에 보일 글: `[근거 <call_id>] ` + 본문(상한 · 잘리면 그렇다고) + 쪽
    안내. 거절(ROUTINE_ERROR)은 그 글 그대로 — 예산 · 상한 · 실행 상태 거절(_halting)이면 '더 부르지 말고 정리하라' 를 붙이고 그 단계에서는 더
    보내지 않는다."""
    A = _app()
    if ctx.halt.get(sid):
        return f"{ctx.halt[sid]}\n{_HALT_NOTE}"
    src = A._turn_ids.get()
    if src is not None:
        bad = A._phantom_id_arg(args, src)
        if bad:
            k, v = bad
            print(f"[gate] 루틴 {sid} · {exposed}({k}={v}) 차단 — 출처 정수 {len(src)}개")
            return (f"{k}={v} {A._PHANTOM_ID_MARK}. 추측한 ID 로 부르면 다른 대상의 진짜 데이터가 돌아와 더 위험하다. 이 단계의 목록 · 검색 "
                    "도구로 이름을 조회해 ID 를 확인한 뒤 다시 부른다(이 호출은 보내지 않았다 — 예산을 쓰지 않았다).")
    async with ctx.lock:
        if ctx.halt.get(sid):          # 잠금을 기다리는 사이 앞 호출이 예산을 다 썼다(병렬 tool_calls)
            return f"{ctx.halt[sid]}\n{_HALT_NOTE}"
        rec = await _logged_call(ctx, sid, bk, orig, exposed, args)
    if rec.get("refused"):
        t = rec["refused"]
        if _halting(t):
            ctx.halt[sid] = t
            return f"{t}\n{_HALT_NOTE}"
        return t
    if rec.get("error"):
        return (f"{A._TOOL_FAIL_MARK} 도구 {exposed} 호출 실패: {rec['error']} — 인자 문제가 아니다(게이트웨이 · 전송). 같은 호출을 "
                "되풀이하지 말고 지금까지의 결과로 정리한다")
    cid, body = rec["call_id"], rec["body"]
    A._learn_ids(body)
    cap = ctx.cap.get(sid) or _result_cap(_STEP_CALLS)
    shown = _capped(body, cap, cid)
    if rec["pages"] > 1:
        ctx.paged[sid] = True
        shown += f"\n(이 결과는 {rec['pages']}쪽 가운데 1쪽이다 — 다음 쪽은 {_PAGE_TOOL}(call_id=\"{cid}\", page=2))"
    if rec.get("images"):
        shown += f"\n(그림 {rec['images']}개는 싣지 않았다 — 루틴 원장은 글만 둔다)"
    return f"[근거 {cid}] " + ("(도구가 오류로 답했다) " if not rec["ok"] else "") + shown


def _page_tool(ctx: _Ctx, sid: str):
    """routine_page(call_id, page) — 이 단계에서 받은 큰 결과의 다음 쪽(속은 routine_get(run_id, call_id, page) — 원장 읽기라 예산을 안 쓴다).
    쪽이 여럿인 결과가 나온 단계에만 묶는다."""
    from langchain_core.tools import StructuredTool  # noqa: PLC0415

    async def _run(call_id: str = "", page: int = 2, **_kw) -> str:
        A = _app()
        cid = str(call_id or "").strip()
        mine = {c["call_id"]: c for c in ctx.calls.get(sid) or []}
        if cid not in mine:
            return f"{_REFUSED} 이 단계의 호출이 아니다 — {cid!r}(이 단계: {', '.join(mine) or '없음'})"
        try:
            p = int(page)
        except (TypeError, ValueError):
            return f"{_REFUSED} page 는 2 이상의 정수다"
        async with ctx.lock:
            try:
                got = await ctx.gw.tool("routine_get", {"run_id": ctx.run_id, "call_id": cid, "page": p}, idempotent=True)
            except _AuthRejected:
                raise
            except _Refused as exc:
                return f"{_REFUSED} {exc}"
            except RoutineGatewayError as exc:
                return f"{A._TOOL_FAIL_MARK} 쪽을 읽지 못했다: {exc}"
        text, pages = str(got.get("text") or ""), int(got.get("pages") or 1)
        mine[cid]["texts"].append(text)
        ctx.seen_all.append(text)
        A._learn_ids(text)
        cap = ctx.cap.get(sid) or _result_cap(_STEP_CALLS)
        shown = text if len(text) <= cap else text[:cap] + f"\n…[이 쪽 {len(text):,}자 가운데 앞 {cap:,}자만 실었다]"
        more = f"\n(다음 쪽은 {_PAGE_TOOL}(call_id=\"{cid}\", page={p + 1}))" if p < pages else ""
        return f"[근거 {cid} · {p}/{pages}쪽] " + shown + more

    return StructuredTool(
        name=_PAGE_TOOL, coroutine=_run,
        description="이 단계에서 받은 큰 결과의 다음 쪽을 읽는다 — 결과 끝에 '쪽' 안내가 있을 때만, 그 call_id 와 쪽 번호 그대로. 원장 읽기라 "
                    "호출 예산을 쓰지 않는다.",
        args_schema={"type": "object", "properties": {
            "call_id": {"type": "string", "description": "결과 머리 [근거 …] 의 call_id(예: voc.c2)"},
            "page": {"type": "integer", "minimum": 2, "description": "읽을 쪽 번호(2부터)"}}, "required": ["call_id", "page"]})


# ── platform · cite 단계 ──────────────────────────────────────────────────────────────────────────────────────
def _budget(step: dict) -> tuple[int, float]:
    b = step.get("budget") or {}
    try:
        calls = max(1, int(b.get("calls") or _STEP_CALLS))
    except (TypeError, ValueError):
        calls = _STEP_CALLS
    try:
        secs = float(b.get("seconds") or _STEP_SECONDS)
    except (TypeError, ValueError):
        secs = _STEP_SECONDS
    return calls, secs


async def _preload(ctx: _Ctx, step: dict) -> None:
    """앞 프로세스가 running 으로 남긴 단계 — 원장에 이미 있는 이 단계의 호출(<단계>.c1 …)을 머리 + 첫 쪽 글로 이 단계의 호출 기록에 싣는다.
    봉투 턴이 그 call_id 를 인용하고 키를 그 결과에서 찾으며, 작은 ReAct 프롬프트가 '앞 프로세스가 이미 부른 것' 으로 보인다(같은 호출을
    되풀이하지 않게). 원장 읽기라 예산을 쓰지 않는다. 남은 예산은 게이트웨이가 지킨다(넘으면 거절 → 멈춤). 원장에 없는 줄은 건너뛴다."""
    sid = str(step.get("id"))
    A = _app()
    for n in range(1, int(step.get("calls") or 0) + 1):
        try:
            got = await ctx.gw.tool("routine_get", {"run_id": ctx.run_id, "call_id": f"{sid}.c{n}"}, idempotent=True)
        except _Refused:
            continue
        text, tool = str(got.get("text") or ""), str(got.get("tool") or "")
        bk, _, orig = tool.partition(":")
        try:
            pages = max(1, int(got.get("pages") or 1))
        except (TypeError, ValueError):
            pages = 1
        ctx.calls.setdefault(sid, []).append({"call_id": str(got.get("call_id") or f"{sid}.c{n}"), "tool": tool,
                                              "exposed": ctx.exposed_of.get((bk, orig)) or orig or tool, "args": None,
                                              "ok": bool(got.get("ok")), "texts": [text], "prior": True, "pages": pages})
        ctx.seen_all.append(text)
        A._learn_ids(text)               # 앞 결과에서 본 ID 로 부르는 호출이 유령 ID 막이에 걸리지 않게(라이브 길과 같다)
        if pages > 1:
            ctx.paged[sid] = True        # 다음 쪽을 읽게 routine_page 를 처음부터 묶는다(_gather)


def _prior_calls_text(ctx: _Ctx, sid: str) -> str:
    """'앞 프로세스가 이미 부른 것' — 실어 둔 호출마다 `[근거 <call_id>] 도구 · 성공/실패` + 첫 쪽 글(라이브 길과 같은 상한 · 쪽 안내)."""
    cap = ctx.cap.get(sid) or _result_cap(_STEP_CALLS)
    out = []
    for c in ctx.calls.get(sid) or []:
        if not c.get("prior"):
            continue
        body = "\n".join(c["texts"])
        more = (f"\n(이 결과는 {c['pages']}쪽 가운데 1쪽이다 — 다음 쪽은 {_PAGE_TOOL}(call_id=\"{c['call_id']}\", page=2))"
                if c.get("pages", 1) > 1 else "")
        out.append(f"[근거 {c['call_id']}] {c['tool']} · {'성공' if c['ok'] else '실패'}\n{_capped(body, cap, c['call_id'])}{more}")
    return "\n\n".join(out)


def _prior_text(ctx: _Ctx, step: dict) -> str:
    """앞 단계(uses)의 요약 · 키 · 발견(인용 id 포함) — 실행 보기에서(옛 포털이면 이 실행기가 보낸 발견)."""
    by = {s.get("id"): s for s in ctx.view.get("steps") or []}
    out = []
    for uid in step.get("uses") or []:
        u = by.get(uid)
        if not u:
            continue
        lines = [f"〔{uid}〕 {u.get('title') or uid} — {u.get('state')}" + (f"({u.get('reason')})" if u.get("reason") else "")]
        if u.get("summary"):
            lines.append(str(u["summary"]))
        if u.get("keys"):
            lines.append("넘긴 키 — " + " · ".join(f"{k}={v}" for k, v in u["keys"].items()))
        for f in (u["findings"] if isinstance(u.get("findings"), list) else ctx.findings.get(uid) or []):
            if isinstance(f, dict) and f.get("text"):
                lines.append(f"- {f['text']}" + "".join(f" [{r}]" for r in f.get("refs") or []))
        out.append("\n".join(lines))
    return "\n\n".join(out)


def _step_prompt(ctx: _Ctx, step: dict, calls_b: int, secs: float) -> tuple[str, str]:
    sid, title = str(step.get("id")), step.get("title") or str(step.get("id"))
    keys = [str(k) for k in (step.get("expect") or {}).get("keys") or []]
    prior = _prior_text(ctx, step)
    done = _prior_calls_text(ctx, sid)
    used = int(step.get("calls") or 0)      # 앞 프로세스가 이 단계에서 이미 쓴 호출(원장) — 남은 것은 게이트웨이가 지킨다
    sysmsg = (f"너는 분석 루틴 「{(ctx.view.get('run') or {}).get('title') or ''}」의 한 단계를 맡은 조회 담당이다 — 단계 〔{sid}〕 {title}.\n"
              f"[루틴 목적] {ctx.view.get('purpose') or '(없음)'}\n"
              f"[이 단계의 할 일] {step.get('goal') or ''}\n"
              + (f"[사용자가 덧붙인 말] {ctx.note}\n" if ctx.note else "")
              + (f"[앞 단계에서 넘어온 것]\n{prior}\n" if prior else "")
              + (f"[앞 프로세스가 이미 부른 것 — 같은 호출을 되풀이하지 않는다. 근거는 그 call_id 로 단다]\n{done}\n" if done else "")
              + f"[이 단계가 다음 단계에 넘길 키] {', '.join(keys) if keys else '(없음)'}\n"
              f"[예산] 호출 {calls_b}번" + (f"(앞 프로세스가 {used}번 썼다 — 남은 것은 {max(0, calls_b - used)}번)" if used else "")
              + f" · {secs:,.0f}초\n"
              "[규칙]\n"
              "- 묶인 도구만 쓴다 — 이 단계의 울타리다. 다른 플랫폼 · 안내 도구는 없다.\n"
              "- 값(수치 · 이름 · 키)은 도구 결과에 나온 것만 쓴다. 지어내지 않고, 식별자(id)를 추측해 넣지 않는다.\n"
              "- 결과마다 [근거 <call_id>] 머리가 붙는다 — 정리할 때 근거를 [call_id] 로 단다.\n"
              "- 모르면 모른다고 한다. 결과가 할 일에 충분하면 더 부르지 않는다.\n"
              "- 끝나면 한국어로 짧게 정리한다 — 알아낸 것 · 넘길 키 후보 · 빈 곳.")
    return sysmsg, f"단계 〔{sid}〕 {title} 의 할 일을 지금 도구로 수행하라."


async def _gather(ctx: _Ctx, step: dict, tools: list, sysmsg: str, human: str, calls_b: int, secs: float) -> tuple[str, str]:
    """작은 ReAct — (AI 메모, 멈춘 까닭). create_react_agent(llm, 단계 도구) 를 astream(values) 로 돌려 도중에 터져도 받은 것은 산다
    (deliberation._free_gather_one 과 같은 까닭 — ainvoke 는 부분 결과를 통째로 잃는다). 왕복 상한은 calls×2+5. 시간 상자는 budget.seconds —
    작은 ReAct 를 작업으로 돌려 기한이 지나면 취소한다(Python 3.10 — asyncio.timeout 이 없다). 쪽이 여럿인 결과가 나오면 그 도구 마디가 끝난 뒤
    routine_page 를 더 묶어 지금까지의 메시지로 잇는다(묶음은 지을 때 정해지므로)."""
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage  # noqa: PLC0415
    from langgraph.errors import GraphRecursionError  # noqa: PLC0415
    from langgraph.prebuilt import create_react_agent  # noqa: PLC0415

    sid = str(step.get("id"))
    box: dict = {"msgs": [SystemMessage(content=sysmsg), HumanMessage(content=human)], "ai": []}

    async def run() -> None:
        # 앞 프로세스가 받은 결과가 여러 쪽이면(_preload) routine_page 를 처음부터 묶는다 — 아니면 쪽이 여럿인 결과가 나온 뒤에 묶는다
        paged_on, used, limit = bool(ctx.paged.get(sid)), 0, calls_b * 2 + 5
        bound = list(tools) + ([_page_tool(ctx, sid)] if paged_on else [])
        while True:
            agent = create_react_agent(ctx.llm, bound)
            again = False
            async for st in agent.astream({"messages": box["msgs"]}, config={"recursion_limit": max(3, limit - used)},
                                          stream_mode="values"):
                got = list((st or {}).get("messages") or [])
                if len(got) <= len(box["msgs"]):
                    continue                     # 넣은 상태 그대로(첫 값)
                used += 1
                for m in got[len(box["msgs"]):]:
                    if getattr(m, "type", "") == "ai":
                        t = _content_text(m).strip()
                        if t:
                            box["ai"].append(t)
                            ctx.reason(t + "\n")
                box["msgs"] = got
                if ctx.paged.get(sid) and not paged_on and isinstance(got[-1], ToolMessage):
                    bound, paged_on, again = bound + [_page_tool(ctx, sid)], True, True
                    break
            if not again:
                return

    task = asyncio.ensure_future(run())
    note = ""
    try:
        done, _pending = await asyncio.wait({task}, timeout=secs if secs > 0 else None)
        if not done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            note = f"시간 예산({secs:,.0f}초)에 닿아 조회를 멈췄다 — 그때까지의 호출로 닫는다"
        elif task.cancelled():
            note = "조회가 취소됐다 — 그때까지의 호출로 닫는다"
        else:
            exc = task.exception()
            if isinstance(exc, _AuthRejected):
                raise exc
            if isinstance(exc, GraphRecursionError):
                note = f"호출 왕복 상한({calls_b * 2 + 5})에 닿아 멈췄다 — 그때까지의 호출로 닫는다"
            elif exc is not None:
                note = f"조회 도중 멈췄다({type(exc).__name__}: {str(exc)[:160]}) — 그때까지의 호출로 닫는다"
    finally:
        if not task.done():
            task.cancel()                       # 바깥 작업이 취소됐다(서버 내림) — 작은 ReAct 도 멈춘다
    if note:
        ctx.reason(f"({note})\n")
    return "\n".join(box["ai"]), note


async def _platform_step(ctx: _Ctx, step: dict) -> dict:
    sid = str(step.get("id"))
    calls_b, secs = _budget(step)
    ctx.cap[sid] = _result_cap(calls_b)
    await ctx.ensure_tools()
    if int(step.get("calls") or 0) > 0:
        await _preload(ctx, step)            # 앞 프로세스가 running 으로 남긴 단계(이어서) — 이미 부른 것을 싣는다
        ctx.reason(f"(앞 프로세스가 이 단계에서 이미 부른 {len(ctx.calls.get(sid) or [])}건을 원장에서 읽어 실었다)\n")
    tools, missing = _bind(ctx, step)
    if missing:
        ctx.reason(f"(허용 도구 가운데 내 도구 목록에 없는 {len(missing)}개는 묶지 않았다: {', '.join(missing[:12])})\n")
    if not tools:
        why = ("허용 도구를 내 도구 목록에서 하나도 찾지 못해 부를 것이 없다 — " + ", ".join(missing[:8])) if missing else \
            "이 단계의 허용 도구가 비었다(계획 — 읽기 도구가 없다)"
        if ctx.calls.get(sid):
            return await _envelope(ctx, step, "", why)     # 앞 프로세스가 부른 것이 있다 — 그것으로 닫는다(버리면 원장의 근거가 봉투에서 빠진다)
        return {"status": "skipped", "reason": why[:_REASON_MAX], "summary": "", "keys": {}, "findings": [], "candidates": []}
    sysmsg, human = _step_prompt(ctx, step, calls_b, secs)
    memo, note = await _gather(ctx, step, tools, sysmsg, human, calls_b, secs)
    return await _envelope(ctx, step, memo, note)


# ── 봉투 턴 ───────────────────────────────────────────────────────────────────────────────────────────────────
_ENV_SYS = (
    "너는 분석 루틴 한 단계의 '단계 봉투' 를 쓰는 기록자다. 도구를 부르지 않는다. 아래 호출 결과만 보고 JSON 객체 하나만 출력한다.\n"
    '꼴: {"status": "...", "reason": "...", "summary": "...", "keys": {"키": "값"}, "findings": [{"text": "...", "refs": ["<call_id>"]}], '
    '"candidates": [{"label": "...", "key": "..."}]}\n'
    "- status — ok(할 일을 결과로 해냈다) · empty(조회했지만 결과가 없다) · ambiguous(기준 후보가 여럿이라 사람이 골라야 한다 — candidates "
    "필수) · failed(호출이 다 실패했거나 할 일을 못 했다) · skipped(부를 것이 없었다) · denied(권한 없음) · not_connected(토큰 없음) · "
    "unavailable(이 박스에 없음) 가운데 하나.\n"
    "- reason — ok 가 아니면 까닭(500자까지).\n"
    "- summary — 이 단계에서 알아낸 것(마크다운 6,000자까지). 수치 · 이름은 결과에 있는 그대로 쓰고 근거는 [call_id] 로 단다.\n"
    "- keys — 기대 키만, 값은 결과에 글자 그대로 있는 것만(200자까지). 결과에 없으면 넣지 않는다. 근거는 성공한 호출의 결과다 — 실패한 "
    "호출의 글이나, 그 호출에 넣은 인자를 도구가 되돌려 준 값은 근거가 아니다.\n"
    "- findings — 발견 20개까지(글 500자까지), refs 는 이 단계의 call_id 만.\n"
    "- candidates — ambiguous 일 때만, 사람이 고를 후보 {label, key} 20개까지.\n"
    "JSON 밖에는 아무것도 쓰지 않는다. 글은 한국어로 쓴다.")


def _val(v) -> str:
    """키 값 — 포털 Envelope 과 같은 맞춤(글이 아니면 str, 200자에서 자른다)."""
    return (v if isinstance(v, str) else str(v))[:_KEY_VAL_MAX]


def _sources(ctx: _Ctx, sid: str) -> list:
    """키의 근거(R3-design §8 · C5) — 이 단계의 **성공한** 호출마다 (받은 글 — 본문과 읽은 쪽, 그 호출 인자의 JSON). 실패한 호출의 글(「'SM-S999'
    를 찾을 수 없다」)과 그 호출에 넣은 인자를 도구가 되돌려 준 값({"query": "SM-S999", "items": []})은 근거가 아니다 — 포털 step_done 과 같은
    규칙이다. 앞 프로세스가 부른 호출(_preload)은 인자를 모른다(원장 읽기가 인자를 주지 않는다) — 글만 보고, 되돌려 준 값은 포털이 거른다."""
    return [("".join(c["texts"]), "" if c.get("args") is None else json.dumps(c["args"], ensure_ascii=False, default=str))
            for c in ctx.calls.get(sid) or [] if c.get("ok")]


def _sourced(v: str, sources: list) -> bool:
    return bool(v) and any(v in text and v not in args for text, args in sources)


def _cands(v) -> list:
    """사람이 고를 후보 — {label, key} 가 둘 다 비지 않은 것만(포털 Candidate 의 맞춤 — 200자에서 자르고 20개까지). 글 · 다른 칸 꼴은 버린다."""
    out = []
    for c in _as_list(v):
        if not isinstance(c, dict):
            continue
        label, key = str(c.get("label") or "").strip()[:_CAND_MAX], str(c.get("key") or "").strip()[:_CAND_MAX]
        if label and key and len(out) < _CANDS_MAX:
            out.append({"label": label, "key": key})
    return out


def _excerpt_cap(n: int) -> int:
    """봉투 턴에 싣는 호출 결과 발췌(한 건, 자) — 이 작업 창의 몫을 호출 수로 나눈다(1자 ≈ 1토큰으로 보수적으로)."""
    try:
        from deliberation import _free_ctx_tokens  # noqa: PLC0415
        total = max(8000, int(_free_ctx_tokens() * 0.8) - 8000)
    except Exception:  # noqa: BLE001
        total = 60000
    return max(1500, total // max(1, n))


def _env_human(ctx: _Ctx, step: dict, memo: str, note: str) -> str:
    A = _app()
    sid = str(step.get("id"))
    calls = ctx.calls.get(sid) or []
    keys = [str(k) for k in (step.get("expect") or {}).get("keys") or []]
    per = _excerpt_cap(len(calls))
    rows = []
    for c in calls:
        txt = "\n".join(c["texts"])
        ex = txt if len(txt) <= per else txt[:per] + f"\n…[{len(txt):,}자 가운데 앞 {per:,}자]"
        args = "(앞 프로세스가 부른 것 — 인자는 원장에 있다)" if c.get("prior") else A._tool_preview(c["args"], 400)
        rows.append(f"### {c['call_id']} · {c['tool']} · {'성공' if c['ok'] else '실패'}\n인자: {args}\n결과:\n{ex}")
    return (f"[단계] 〔{sid}〕 {step.get('title') or sid}\n[할 일] {step.get('goal') or ''}\n"
            f"[기대 키] {', '.join(keys) if keys else '(없음)'}\n"
            f"[이 단계의 call_id] {', '.join(c['call_id'] for c in calls) or '(없음 — 호출하지 않았다)'}\n"
            + (f"[멈춘 까닭] {note}\n" if note else "")
            + "\n[호출과 결과]\n" + ("\n\n".join(rows) or "(호출 없음)")
            + f"\n\n[AI 메모]\n{memo or '(없음)'}")


async def _env_turn(ctx: _Ctx, human: str):
    """봉투 턴 한 번 — 도구 없음 · 생각 끔 · 출력 상한(app._self_check_stream 과 같은 손잡이). 실패는 None(코드 봉투로 간다)."""
    try:
        r = await ctx.llm.ainvoke([("system", _ENV_SYS), ("human", human)], max_tokens=_ENV_OUT_CAP,
                                  extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    except Exception as exc:  # noqa: BLE001 — 봉투 턴 실패가 단계를 못 죽인다
        print(f"[routine] 봉투 턴 실패 — {type(exc).__name__}: {exc}"[:300])
        return None
    return _parse_json(_content_text(r))


def _env_problem(d, ids: list, sources: list) -> str:
    """봉투 검사(deliberation._persona_round 의 지적 꼴) — 다시 쓰게 할 까닭 한 줄, 문제 없으면 빈 글. 다듬은 꼴(_clean_envelope 가 보낼 것)로
    본다 — 원 꼴만 보면 글 목록 후보 · 목록 키가 검사를 지나 다듬기에서 조용히 빠졌다(고를 것이 사라진 채 다음 단계가 돌았다)."""
    if not isinstance(d, dict):
        return "직전 출력이 JSON 객체가 아니었다."
    if d.get("status") not in _STATES:
        return f"status 는 {' · '.join(_STATES)} 가운데 하나다(받은 값 {str(d.get('status'))[:40]!r})."
    known = set(ids)
    bad = sorted({str(r) for f in _as_list(d.get("findings")) if isinstance(f, dict) for r in _as_list(f.get("refs"))
                  if str(r) not in known})
    if bad:
        return f"findings 의 refs 는 이 단계의 call_id({', '.join(ids) or '없음'})만 쓴다 — 없는 것: {', '.join(bad[:8])}."
    keys = d.get("keys")
    if keys not in (None, "", [], {}) and not isinstance(keys, dict):
        return f"keys 는 객체 {{\"키\": \"값\"}} 다(받은 꼴 {type(keys).__name__})."
    if isinstance(keys, dict):
        miss = [str(k) for k, v in keys.items() if v not in (None, "") and not _sourced(_val(v), sources)]
        if miss:
            return (f"keys 의 값은 이 단계의 성공한 호출 결과에 글자 그대로 있어야 한다(실패한 호출의 글 · 그 호출에 넣은 인자를 되돌려 받은 "
                    f"값은 근거가 아니다) — 근거가 없는 것: {', '.join(miss[:8])}.")
    if d.get("status") == "ambiguous" and not _cands(d.get("candidates")):
        return ("status 가 ambiguous 면 candidates 에 사람이 고를 후보가 하나 이상 있어야 한다 — 꼴은 {\"label\": \"보이는 이름\", "
                "\"key\": \"넘길 값\"}(둘 다 비지 않은 글)이다.")
    return ""


def _clean_envelope(d: dict, ids: list, sources: list, note: str) -> dict:
    """포털에 보낼 봉투 — refs 는 이 단계 call_id 만, 키 값은 성공한 호출 결과에 글자 그대로 있는 것만(빼면 까닭에 적는다 — _sources), 포털
    상한에 맞춰 자른다. 다시 물어도 쓸 수 있는 후보가 없는 ambiguous 는 실패로 닫고 그 까닭을 적는다(포털이 '후보 없이 여럿' 으로 고치면 모델이
    후보를 낸 사실이 가려진다 — 고를 것이 조용히 사라지지 않게)."""
    status, reasons = d["status"], [str(d.get("reason") or "").strip()]
    keys, dropped = {}, []
    for k, v in (d.get("keys") or {}).items() if isinstance(d.get("keys"), dict) else []:
        if v in (None, ""):
            continue
        name, val = str(k)[:_KEY_NAME_MAX], _val(v)
        if not _sourced(val, sources):
            dropped.append(name)
        elif len(keys) < _KEYS_MAX:
            keys[name] = val
    if dropped:
        reasons.append(f"결과에 글자 그대로 없는 키는 뺐다: {', '.join(dropped[:8])}")
    if d.get("keys") not in (None, "", [], {}) and not isinstance(d.get("keys"), dict):
        reasons.append(f"keys 가 객체가 아니라({type(d['keys']).__name__}) 넘기지 않았다")
    known, findings = set(ids), []
    for f in _as_list(d.get("findings")):
        text = str(f.get("text") or "").strip()[:_FINDING_MAX] if isinstance(f, dict) else ""
        if text and len(findings) < _FINDINGS_MAX:
            findings.append({"text": text, "refs": [str(r) for r in _as_list(f.get("refs")) if str(r) in known][:_REFS_MAX]})
    cands = _cands(d.get("candidates"))
    if status == "ambiguous" and not cands:
        status = "failed"
        reasons.append("기준 후보가 여럿이라 했지만(ambiguous) 고를 후보 {label, key} 를 쓰지 못했다 — 실패로 닫는다")
    if note:
        reasons.append(note)
    return {"status": status, "reason": "; ".join(r for r in reasons if r)[:_REASON_MAX],
            "summary": str(d.get("summary") or "").strip()[:_SUMMARY_MAX], "keys": keys, "findings": findings, "candidates": cands}


def _code_envelope(calls: list, memo: str, why: str) -> dict:
    """봉투 턴이 못 쓴 때 — 성공 호출이 있으면 ok, 호출이 다 실패면 failed, 호출 0 이면 skipped, 요약은 AI 메모(포털이 원장과 맞대 다시 고친다)."""
    status = "ok" if any(c["ok"] for c in calls) else ("failed" if calls else "skipped")
    return {"status": status, "reason": why[:_REASON_MAX], "summary": (memo or "").strip()[:_SUMMARY_MAX], "keys": {}, "findings": [],
            "candidates": []}


async def _envelope(ctx: _Ctx, step: dict, memo: str, note: str) -> dict:
    """단계 봉투 — 따로 턴(JSON) → 검사 → 한 번 다시(지적을 실어) → 다듬기, 못 쓰면 코드 봉투(R3-D4)."""
    sid = str(step.get("id"))
    calls = ctx.calls.get(sid) or []
    ids, sources = [c["call_id"] for c in calls], _sources(ctx, sid)
    human = _env_human(ctx, step, memo, note)
    d = await _env_turn(ctx, human)
    prob = _env_problem(d, ids, sources)
    if prob:
        d2 = await _env_turn(ctx, human + f"\n\n(다시 — {prob} 다른 설명 없이 요구한 꼴의 JSON 객체 하나만 출력한다.)")
        if isinstance(d2, dict) or not isinstance(d, dict):
            d = d2
    if not isinstance(d, dict) or d.get("status") not in _STATES:
        why = "; ".join(x for x in (note, "봉투 턴이 쓸 수 있는 JSON 을 내지 못했다 — 상태는 코드가 호출 결과로 정했다") if x)
        return _code_envelope(calls, memo, why)
    return _clean_envelope(d, ids, sources, note)


# ── 심의 단계 ─────────────────────────────────────────────────────────────────────────────────────────────────
def _delib_detail(st: dict) -> str:
    """deliberate_status 한 줄 — '라운드 2/3 · 좌석 발언' 꼴(routine{step, detail})."""
    parts = []
    if st.get("status") == "queued":
        q = st.get("queue") or {}
        parts.append(f"줄 서 있음 — {q.get('position')}번째" if isinstance(q, dict) and q.get("position") else "줄 서 있음")
    if st.get("round"):
        parts.append(f"라운드 {st.get('round')}/{st.get('total_rounds') or '?'}")
    s = st.get("last_step") or st.get("step") or st.get("stage")
    if s:
        parts.append(str(s)[:120])
    if not parts and st.get("status"):
        parts.append(str(st["status"]))
    return " · ".join(parts)


def _parts_of(body: str) -> int:
    """심의 엔진이 결과를 쪽으로 나눴으면 그 수(mcp_pages — page.parts), 아니면 1."""
    got = _parse_json(body)
    page = got.get("page") if isinstance(got, dict) else None
    try:
        return max(1, int(page.get("parts") or 1)) if isinstance(page, dict) else 1
    except (TypeError, ValueError):
        return 1


async def _read_pages(ctx: _Ctx, sid: str, rec: dict) -> None:
    """원장 쪽(게이트웨이 60,000자 쪽)이 여럿이면 routine_get 으로 이어 붙인다 — 결정문은 끝까지 읽어야 봉투가 그 글을 본다."""
    mine = next((c for c in ctx.calls.get(sid) or [] if c["call_id"] == rec.get("call_id")), None)
    for p in range(2, int(rec.get("pages") or 1) + 1):
        got = await ctx.gw.tool("routine_get", {"run_id": ctx.run_id, "call_id": rec["call_id"], "page": p}, idempotent=True)
        text = str(got.get("text") or "")
        if mine is not None:
            mine["texts"].append(text)
        ctx.seen_all.append(text)


async def _job_status(ctx: _Ctx, job_id: str) -> dict:
    """deliberate_status 한 번 — 원 세션으로 **직접**(routine_call 이 아니다 — 원장 줄 · 예산을 쓰지 않는다, R3-D5). 실패는 RoutineGatewayError."""
    is_err, content = await ctx.gw.call(await ctx.exposed(_DELIB, "deliberate_status"), {"job_id": job_id}, idempotent=True)
    text = _text_of(content[0]) if content else ""
    if is_err:
        raise RoutineGatewayError(text[:300] or "deliberate_status 가 오류로 답했다")
    st = _parse_json(text)
    if not isinstance(st, dict):
        raise RoutineGatewayError("deliberate_status 의 답을 읽지 못했다")
    return st


async def _view_soft(ctx: _Ctx) -> dict | None:
    """긴 기다림(심의 진행 · 쉼) 사이의 실행 보기 — 한 번의 끊김(게이트웨이 재기동 · 포털 '닿지 못했다' · 5xx)은 None 이다(이번에는 사람 멈춤 ·
    상한 확인만 건너뛴다). 잇달아 세 번이면 그대로 올린다 — 실행기가 running 으로 멈추고 '이어서' 가 같은 잡을 잇는다(_prior_job). 끊김을 '모름'
    으로 바꾸지 않는다(단계가 실패로 닫혀 이어서가 그 잡을 못 잇는다). 인증 거절은 곧바로 올린다. '잇달아' 는 사이에 읽힌 적이 없다는 뜻이다 —
    어디서든 한 번 읽히면 read_view 가 수를 0 으로 되돌린다."""
    try:
        view = await ctx.read_view()
    except _AuthRejected:
        raise
    except RoutineGatewayError:
        ctx.view_fails += 1
        if ctx.view_fails >= 3:
            raise
        return None
    return view


async def _delib_wait(ctx: _Ctx, sid: str, title: str, job_id: str) -> tuple[str | None, str]:
    """(끝 상태 또는 None, 까닭) — deliberate_status 를 원 세션으로 **직접**(routine_call 이 아니다 — 원장 줄 · 예산을 쓰지 않는다) ROUTINE_DELIB_POLL_S
    마다. 사이사이 실행 보기를 읽어 실행이 도는 상태가 아니면(사람이 멈춤) None. 시간 상한에 닿아도 기다린다 — 결과 읽기는 상한 뒤에도 열린다(D5).
    잡이 줄에 서 있는 동안(queued)은 쉼(parked)이다 — 새 심의 멈춤 동안 줄은 그대로 서 있어, 세면 update-all 의 기다림이 끝나지 않는다.
    진행 조회가 잇달아 세 번 실패하면 '모름' 으로 끝낸다 — 실행 보기가 읽힌 때만 센다(게이트웨이 · 포털이 통째로 안 닿는 끊김은 심의의 실패가
    아니다). 실행 보기는 _view_soft 의 규칙이다(한 번의 끊김은 건너뛰고, 잇달아 세 번이면 실행기를 running 으로 멈춘다). 한 번의 끊김으로 몇
    시간짜리 심의를 버리지 않는다."""
    last, strikes = "", 0
    try:
        while True:
            st, err = None, None
            try:
                st = await _job_status(ctx, job_id)
            except _AuthRejected:
                raise
            except RoutineGatewayError as exc:
                err = exc
            if st:
                strikes = 0
                ctx.live.parked = st.get("status") == "queued"
                detail = _delib_detail(st)
                if detail and detail != last:
                    last = detail
                    ctx.routine("step", step=sid, title=title, state="running", detail=detail)
                if st.get("status") in _JOB_END:
                    return str(st["status"]), ""
            view = await _view_soft(ctx)
            if err is not None and view is not None:
                strikes += 1
                if strikes >= 3:
                    return "모름", f"진행 조회가 잇달아 실패했다 — {err}"[:300]
            if view is not None and (view.get("run") or {}).get("state") != "running":
                return None, ""
            await asyncio.sleep(max(0.0, float(ROUTINE_DELIB_POLL_S or 0)))
    finally:
        ctx.live.parked = False


def _delib_question(ctx: _Ctx, step: dict) -> str:
    purpose = str(ctx.view.get("purpose") or "").strip()
    q = (purpose + "\n\n" if purpose else "") + f"[이 단계의 할 일] {str(step.get('goal') or '').strip()}"
    return q + (f"\n\n[사용자가 덧붙인 말] {ctx.note}" if ctx.note else "")


def _failed_env(reason: str) -> dict:
    return {"status": "failed", "reason": str(reason)[:_REASON_MAX], "summary": "", "keys": {}, "findings": [], "candidates": []}


def _skipped_env(reason: str) -> dict:
    """실행 상한 때문에 하지 못한 단계 — _close_by_limit 과 같은 건너뜀이다."""
    return {**_failed_env(reason), "status": "skipped"}


def _calls_left(view: dict) -> int | None:
    """실행 호출 상한까지 남은 호출 — 상한을 모르면(옛 포털) None."""
    run = view.get("run") or {}
    try:
        return int((run.get("limits") or {})["calls"]) - int(run.get("calls") or 0)
    except (KeyError, TypeError, ValueError):
        return None


def _why(*parts) -> str:
    """까닭 여럿을 '; ' 로 잇는다(빈 것은 뺀다)."""
    return "; ".join(str(x) for x in parts if x)


async def _prior_job(ctx: _Ctx, sid: str, n_calls: int) -> tuple[str, str]:
    """(이을 job_id, 새로 세우는 까닭) — 앞 프로세스가 running 으로 남긴 심의 단계(원장 호출이 이미 있다). 이 단계의 원장 호출을 뒤에서부터
    (<단계>.c<n> … c1) 읽어 **마지막으로 성공한** deliberate_start 의 잡을 잇는다 — 앞 잡이 죽어 새로 세운 잡은 그 뒤에 있다(첫 호출만 보면
    도는 둘째 잡을 두고 셋째를 세운다). 더 앞의 시작은 볼 것이 없다 — 뒤의 시작은 앞 잡이 죽은 뒤에만 섰다. 다시 세우면 GLM 시간을 두 번
    쓰고 앞 잡의 결정문은 루틴에서 사라진다. 그 잡이 error · cancelled · interrupted 로 끝났으면 지금처럼 새로 세우고 그 까닭을 단계 까닭에
    적는다. 상태를 못 읽으면 잇는다(한 번의 끊김으로 도는 잡을 겹쳐 세우지 않는다 — 진행 조회가 잇달아 세 번 실패하면 _delib_wait 가 '모름'
    으로 닫는다). 성공한 시작이 원장에 없으면(거절된 시작뿐 · 다른 호출뿐) 지금처럼 세운다. 원장 읽기라 예산을 쓰지 않는다."""
    job_id = ""
    for n in range(int(n_calls), 0, -1):
        try:
            got = await ctx.gw.tool("routine_get", {"run_id": ctx.run_id, "call_id": f"{sid}.c{n}"}, idempotent=True)
        except _Refused:
            continue
        if got.get("tool") != f"{_DELIB}:deliberate_start" or not got.get("ok"):
            continue
        body = _parse_json(str(got.get("text") or ""))
        job_id = str(body.get("job_id") or "").strip() if isinstance(body, dict) else ""
        if job_id:
            break
    if not job_id:
        return "", ""
    try:
        status = str((await _job_status(ctx, job_id)).get("status") or "")
    except _AuthRejected:
        raise
    except RoutineGatewayError:
        status = ""
    if status in _JOB_DEAD:
        return "", f"앞 프로세스가 세운 심의 {job_id} 가 {status} 로 끝나 새로 세웠다"
    ctx.reason(f"앞 프로세스가 세운 심의 {job_id} 를 잇는다({status or '상태 모름'}) — 새로 세우지 않고 진행을 본다\n")
    return job_id, ""


async def _delib_park(ctx: _Ctx, sid: str, title: str, wait_s: float, cause: str, *, until_unpaused: bool) -> tuple[str, str]:
    """심의를 세우지 못한 잠깐의 거절 뒤 쉰다(parked) — ('go', '') 다시 세워 본다 · ('stop', '') 실행이 도는 상태가 아니다 · ('limit', 상한 글).
    쉬는 동안은 /health routine_active · delib_active 에 세지 않는다(active_count — update-all 의 기다림이 수렴하게). 사이사이 실행 보기를 읽어
    사람의 멈춤 · 실행 상한을 본다(_view_soft — 한 번의 끊김은 건너뛴다). until_unpaused 면(이 프로세스의 새 심의 멈춤) 기한 전이라도 멈춤이
    풀리는 대로 다시 세운다 — retry_after_s 는 멈춤이 저절로 풀리는 때라 update-all 이 먼저 내리면 그만큼 기다릴 까닭이 없다."""
    A = _app()
    n = max(1, round(wait_s))
    ctx.routine("step", step=sid, title=title, state="running", detail=f"심의가 잠시 멈춰 있다 — {n}초 뒤 다시")
    ctx.reason(f"(심의를 아직 세우지 못했다 — {cause[:240]}. {n}초 뒤 다시 세워 본다)\n")
    tick = max(0.05, float(ROUTINE_DELIB_POLL_S or 0))
    end = time.monotonic() + wait_s
    ctx.live.parked = True
    try:
        while True:
            view = await _view_soft(ctx)
            if view is not None:
                run = view.get("run") or {}
                if run.get("state") != "running":
                    return "stop", ""
                if run.get("limit_reached"):
                    return "limit", str(run["limit_reached"])
            left = end - time.monotonic()
            if left <= 0 or (until_unpaused and not A._delib_pause_info()):
                return "go", ""
            await asyncio.sleep(min(left, tick))
    finally:
        ctx.live.parked = False


async def _delib_start(ctx: _Ctx, step: dict, args: dict, renew: str) -> tuple[dict | None, dict | None]:
    """deliberate_start — (시작 기록, None) · (None, 닫을 봉투) · (None, None: 실행이 도는 상태가 아니다 — 맨 위가 그 상태로 끝낸다).
    엔진의 잠깐 거절(새 심의 멈춤 · 줄 가득 — _DELIB_BUSY)은 실패가 아니다 — 쉬었다(_delib_park) 다시 세운다, 실행 상한까지. 이 프로세스에 새 심의
    멈춤이 걸려 있으면 부르지 않고 쉰다 — 심의 엔진이 이 프로세스에 붙어 있어(심의 MCP) 부르면 거절되고 원장 한 줄 · 단계 예산 하나만 든다.
    부르기 전에 실행 호출 상한에 시작 · 결정문 읽기 두 번이 남았는지 본다 — 결정문 읽기는 단계 예산에서는 빠지지만 실행 상한은 그대로라, 하나만
    남으면 심의(분 ~ 시간의 GLM)를 세우고 결정문을 못 읽는다."""
    sid, title = str(step.get("id")), step.get("title") or str(step.get("id"))
    A = _app()
    name = await ctx.exposed(_DELIB, "deliberate_start")
    wait_s, cause, tries = 0.0, "", 0
    while True:
        info = A._delib_pause_info()
        if info:
            cause = "새 심의 멈춤(점검 중)"
            got = await _delib_park(ctx, sid, title, float(info.get("retry_after_s") or _DELIB_RETRY_S), cause, until_unpaused=True)
        elif wait_s > 0:
            got = await _delib_park(ctx, sid, title, wait_s, cause, until_unpaused=False)
        else:
            got = None
        if got is not None:
            wait_s = 0.0
            if got[0] == "stop":
                return None, None
            if got[0] == "limit":
                return None, _skipped_env(_why(renew, f"실행 상한 — {got[1]}", f"심의를 세우지 못한 채 닿았다({cause[:200]})"))
            continue                         # 멈춤을 다시 보고 세운다
        left = _calls_left(ctx.view)
        if left is not None and left < 2:
            return None, _skipped_env(_why(renew, f"실행 호출 상한에 {max(0, left)}번만 남아 심의를 세우지 않았다 — 시작과 결정문 읽기에 두 번이 든다"))
        start = await _logged_call(ctx, sid, _DELIB, "deliberate_start", name, args)
        if start.get("refused") and _run_stopped(start["refused"]):
            return None, None                # 실행이 도는 상태가 아니다(사람이 멈춤)
        if start.get("call_id") and start.get("ok"):
            return start, None
        body = str(start.get("body") or "")
        if start.get("call_id") and any(m in body for m in _DELIB_BUSY):
            tries += 1
            hit = _RETRY_AFTER.search(body)
            wait_s = float(hit.group(1)) if hit else min(_DELIB_RETRY_S * 2 ** (tries - 1), _DELIB_RETRY_MAX)
            cause = body.strip()
            continue
        why = start.get("refused") or start.get("error") or body or "까닭 모름"
        return None, _failed_env(_why(renew, f"심의를 시작하지 못했다 — {why}",
                                      f"앞서 잠깐 거절 {tries}번 — {cause[:200]}" if tries else ""))


async def _delib_fold(ctx: _Ctx, job_id: str) -> None:
    """실행이 다시 이을 수 없는 상태(사람이 멈춤 · 실패 · 끝 — _RUN_FINAL)가 됐다 — 이 실행이 세운 심의를 접는다(deliberate_cancel). 도는 심의는
    GLM 자리를 쥐고, 줄 선 심의는 나중에 시작해 몇 시간씩 도는데 그 결정문을 읽을 실행이 없다. 원 세션으로 직접 부른다(진행 조회와 같다 — 원장 ·
    예산을 쓰지 않는다 · 잡 주인은 같은 사람이다). 접기 실패는 끝을 바꾸지 않는다(생각 칸에 job_id 를 남긴다). 이어 갈 수 있는 멈춤(실행기 오류 ·
    재기동 · interrupted · waiting)에서는 접지 않는다 — '이어서' 가 같은 잡을 잇는다(_prior_job)."""
    state = str((ctx.view.get("run") or {}).get("state") or "")
    if state not in _RUN_FINAL:
        return
    try:
        is_err, content = await ctx.gw.call(await ctx.exposed(_DELIB, "deliberate_cancel"), {"job_id": job_id}, idempotent=True)
        if is_err:
            raise RoutineGatewayError((_text_of(content[0]) if content else "")[:300] or "deliberate_cancel 가 오류로 답했다")
    except RoutineGatewayError as exc:      # 인증 거절(_AuthRejected)도 — 접기 실패로 끝을 바꾸지 않는다
        ctx.reason(f"(실행이 {state} 상태라 심의 {job_id} 를 접으려 했으나 못 했다 — {str(exc)[:200]}. 서버에서 계속 돌 수 있다 — "
                   "deliberate_cancel 로 접는다)\n")
        print(f"[routine] 심의 {job_id} 를 접지 못했다 — 실행 {ctx.run_id} · {exc}"[:300])
        return
    ctx.reason(f"(실행이 {state} 상태라 이 실행이 세운 심의 {job_id} 를 접었다)\n")


async def _delib_step(ctx: _Ctx, step: dict) -> dict | None:
    """심의 단계(R3-D5) — routine_call(deliberate_start; evidence · save_report 는 넣지 않는다 — 게이트웨이가 앞 단계 원 근거 · false 로 채운다) →
    deliberate_status 를 직접 → routine_call(deliberate_result; 원장 쪽 · 엔진 쪽을 이어 읽는다) → 봉투 턴. 심의가 done 이 아니면 failed(job_id 와
    함께 — 서버에서 계속 돌 수 있다). 이어서(앞 프로세스가 이 단계에서 이미 불렀다)는 그 프로세스가 세운 심의를 잇는다(_prior_job). 엔진의 잠깐
    거절은 쉬었다 다시 세운다(_delib_start). 기다리는 사이 실행이 다시 이을 수 없는 상태가 되면(사람이 멈춤) 세운 심의를 접는다(_delib_fold).
    결정문 읽기(deliberate_result — 쪽 포함)는 단계 호출 예산에서 빠진다 — 실행 호출 상한은 그대로라 넘으면 게이트웨이가 거절하고 읽은 쪽까지로
    닫는다."""
    sid, title = str(step.get("id")), step.get("title") or str(step.get("id"))
    d = step.get("deliberation") or {}
    n = int(step.get("calls") or 0)
    job_id, renew = (await _prior_job(ctx, sid, n)) if n > 0 else ("", "")
    if not job_id:
        if renew:
            ctx.reason(f"({renew})\n")
        args: dict = {"question": _delib_question(ctx, step), "job": str(d.get("job") or "mechanism")}
        if d.get("chair_template"):
            args["advanced"] = {"chair_template": str(d["chair_template"])}
        start, env = await _delib_start(ctx, step, args, renew)
        if start is None:
            return env                       # 닫을 봉투(시작 실패 · 실행 상한) 또는 None(실행이 도는 상태가 아니다 — 맨 위가 그 상태로 끝낸다)
        got = _parse_json(start["body"])
        job_id = str(got.get("job_id") or "").strip() if isinstance(got, dict) else ""
        if not job_id:
            return _failed_env(_why(renew, "심의 시작 답에 job_id 가 없다"))
        ctx.reason(f"심의 {job_id} 를 시작했다 — 진행을 {float(ROUTINE_DELIB_POLL_S or 0):,.0f}초마다 본다\n")
    status, why = await _delib_wait(ctx, sid, title, job_id)
    if status is None:
        await _delib_fold(ctx, job_id)       # 다시 이을 수 없는 상태면 세운 심의를 접는다(이어 갈 수 있는 멈춤은 그대로 — 이어서가 잇는다)
        return None
    if status != "done":
        return _failed_env(_why(renew, f"심의가 끝나지 않았다({status}) — job_id {job_id}, 서버에서 계속 돌 수 있다" + (f" · {why}" if why else "")))
    rname = await ctx.exposed(_DELIB, "deliberate_result")
    rec = await _logged_call(ctx, sid, _DELIB, "deliberate_result", rname, {"job_id": job_id})
    if not rec.get("call_id"):
        return _failed_env(_why(renew, f"결정문을 읽지 못했다(job_id {job_id}) — {rec.get('refused') or rec.get('error') or '까닭 모름'}"))
    await _read_pages(ctx, sid, rec)
    note, part, parts = "", 1, _parts_of("\n".join(next((c["texts"] for c in ctx.calls[sid] if c["call_id"] == rec["call_id"]), [])))
    while part < parts:
        part += 1
        more = await _logged_call(ctx, sid, _DELIB, "deliberate_result", rname, {"job_id": job_id, "part": part})
        if not more.get("call_id"):
            note = f"결정문 {parts}쪽 가운데 {part - 1}쪽까지만 읽었다 — {more.get('refused') or more.get('error') or '까닭 모름'}"
            break
        await _read_pages(ctx, sid, more)
    return await _envelope(ctx, step, f"심의 {job_id} 가 끝났다(done) — 결정문은 위 결과에 있다", _why(renew, note))


# ── 인사이트 · 보고서 ─────────────────────────────────────────────────────────────────────────────────────────
_INSIGHT_SYS = (
    "너는 분석 루틴 「{title}」의 보고서에서 '인사이트' 절을 쓴다. 단계마다 조회한 결과(요약 · 발견 · 근거 call_id)를 엮어 루틴 목적에 답한다.\n"
    "[규칙]\n"
    "- 주장마다 근거를 [call_id] 로 단다(예: [ft.c2]). 근거가 없는 주장은 쓰지 않는다.\n"
    "- 수치는 아래 근거에 있는 것만 쓴다 — 계산하거나 지어내지 않는다.\n"
    "- 못 한 단계 · 빈 곳을 숨기지 않는다 — 무엇을 보지 못했는지 적는다.\n"
    "- 제목 줄 없이 마크다운 본문만, 한국어로 쓴다.")


def _section(s: dict, cached: list | None) -> str:
    """단계 하나의 글 — 요약 + '- 발견 [ref …]' 줄(인사이트 턴의 입력 · 출처 대조의 근거 글). 보고서의 단계 절은 포털이 짓는다(_report).
    발견은 실행 보기의 것(옛 포털이면 이 실행기가 보낸 것)."""
    summary = str(s.get("summary") or "").strip()
    findings = s["findings"] if isinstance(s.get("findings"), list) else (cached or [])
    lines = [f"- {f['text']}" + "".join(f" [{r}]" for r in f.get("refs") or [])
             for f in findings if isinstance(f, dict) and f.get("text")]
    return "\n\n".join(x for x in (summary, "\n".join(lines)) if x)


def _insight_human(ctx: _Ctx, view: dict) -> str:
    lines = [f"[루틴 목적] {view.get('purpose') or '(없음)'}"]
    if ctx.note:
        lines.append(f"[사용자가 덧붙인 말] {ctx.note}")
    lines.append("[단계]")
    blocked = []
    for s in view.get("steps") or []:
        sid = s.get("id")
        head = f"### 〔{sid}〕 {s.get('title') or sid} — {s.get('state')}" + (f"({s.get('reason')})" if s.get("reason") else "")
        if s.get("state") in _BLOCKED:
            blocked.append(head[4:])
        body = _section(s, ctx.findings.get(sid))
        keys = " · ".join(f"{k}={v}" for k, v in (s.get("keys") or {}).items())
        lines.append("\n".join(x for x in (head, body, f"넘긴 키 — {keys}" if keys else "") if x))
    if blocked:
        lines.append("[막힌 단계 — 이 보고서가 보지 못한 것]\n" + "\n".join(f"- {b}" for b in blocked))
    return "\n\n".join(lines)


def _evidence_text(ctx: _Ctx, view: dict) -> str:
    """출처 없는 수치 대조의 근거 글 — 이 실행에서 받은 결과 원문 · 단계 요약 · 발견 · 키 · 입력 · 사용자가 덧붙인 말."""
    parts = [ctx.note, json.dumps(view.get("inputs") or {}, ensure_ascii=False, default=str), str(view.get("purpose") or "")]
    for s in view.get("steps") or []:
        parts.append(_section(s, ctx.findings.get(s.get("id"))))
        parts.append(json.dumps(s.get("keys") or {}, ensure_ascii=False, default=str))
    return "\n".join(parts + ctx.seen_all)


async def _stream_text(ctx: _Ctx, sysmsg: str, human: str, head: str) -> str:
    """LLM 글 한 번(생각은 챗 기본) — 쓰는 동안 '생각' 칸에 조각으로 흘린다. 실패는 빈 글(부르는 쪽이 그 사실을 다룬다)."""
    ctx.reason(head)
    acc, buf = "", ""
    try:
        async for ch in ctx.llm.astream([("system", sysmsg), ("human", human)]):
            c = _content_text(ch)
            if c:
                acc, buf = acc + c, buf + c
                if len(buf) >= _SLICE:
                    ctx.reason(buf)
                    buf = ""
    except Exception as exc:  # noqa: BLE001 — 인사이트 실패가 보고서를 죽이지 않는다
        print(f"[routine] 인사이트 턴 실패 — {type(exc).__name__}: {exc}"[:300])
        ctx.reason(buf + f"\n(이 턴이 실패했다 — {type(exc).__name__})\n")
        return ""
    ctx.reason(buf + "\n")
    return acc.strip()


async def _insight(ctx: _Ctx, view: dict) -> str:
    """인사이트 절(R3-D11) — LLM 한 번, 출처 없는 수치(evidence.unsourced_numbers)가 있으면 그 목록을 주고 한 번 고쳐 쓰게 한다. 고친 글이 더
    나쁘거나(출처 없는 수치가 더 많다) 실패하면 첫 글이다 — 검사 실패가 보고서를 죽이지 않는다(자기검증의 '못 고치면 초안' 규칙)."""
    sysmsg = _INSIGHT_SYS.format(title=(view.get("run") or {}).get("title") or "")
    human = _insight_human(ctx, view)
    src = _evidence_text(ctx, view)
    ctx.emit("status", {"step": "인사이트를 쓰는 중 — 단계 요약 · 발견을 근거로"})
    first = await _stream_text(ctx, sysmsg, human, "\n\n### 〔insight〕 인사이트\n")
    if not first:
        return "_(인사이트를 쓰지 못했다 — 위 단계 절과 근거 부록을 본다)_"
    bad = unsourced_numbers(first, src, limit=0)
    if not bad:
        return first
    fix = await _stream_text(
        ctx, sysmsg,
        human + f"\n\n[고쳐 쓰기] 첫 글의 수치 가운데 근거에 없는 것 {len(bad)}개: {', '.join(bad[:20])} — 근거 [call_id] 를 달 수 있으면 달고, "
                "없으면 그 수치를 뺀다. 고친 인사이트 전문만 다시 쓴다.\n\n[첫 글]\n" + first,
        f"\n\n(인사이트에 출처 없는 수치 {len(bad)}개 — 고쳐 쓴다)\n")
    if fix and len(unsourced_numbers(fix, src, limit=0)) <= len(bad):
        return fix
    return first


async def _report(ctx: _Ctx, view: dict) -> None:
    """다 닫히면 — 인사이트(LLM) → routine_report_submit → 보고서 본문을 token 400자 조각으로 · routine{report} · result · routine{done} · done.
    보내는 절은 인사이트 하나다(루틴 보고서 절에 있을 때만). 단계 절은 보내지 않는다 — 포털이 저장한 봉투(요약 · 넘긴 키 · 발견)로 짓는다. 그래서
    두 실행자(Qwen · Claude)의 단계 절이 한 정본에서 나오고, 실행기가 지어 보내면 포털이 봉투의 키 · 발견을 또 붙여 발견이 두 번 실리던 것이
    없다. 막힌 단계는 포털이 커버리지로 말한다."""
    secs = [str(x) for x in (view.get("report") or {}).get("sections") or []]
    sections: dict = {}
    if "insight" in secs:
        sections["insight"] = (await _insight(ctx, view))[:_SECTION_MAX]
    ctx.emit("status", {"step": "보고서를 내는 중 — 포털이 커버리지 · 근거 부록을 붙여 렌더한다"})
    try:
        out = await ctx.gw.tool("routine_report_submit", {"run_id": ctx.run_id, "sections": sections})
    except _Refused:
        now = await ctx.read_view()
        if (now.get("run") or {}).get("state") != "running":
            _closed_end(ctx, now)        # 인사이트를 쓰는 사이 사람이 멈췄다 — 그 상태로 끝낸다(멈춘 실행에 '이어서' 를 말하지 않는다)
            return
        raise
    md = str(out.get("report_md") or "") or "보고서를 냈다 — 화면의 보고서에서 연다."
    for i in range(0, len(md), _SLICE):
        ctx.emit("token", {"delta": md[i:i + _SLICE]})
    _report_frame(ctx, out)
    ctx.emit("result", {"type": "text", "content": md})
    ctx.end("done")
    print(f"[routine] 끝 — 실행 {ctx.run_id} · 보고서 {len(md):,}자 · RA {out.get('ra_report_id') or out.get('ra_error') or out.get('ra_upload')}")


def _report_frame(ctx: _Ctx, out: dict) -> None:
    """routine{report} — routine_report_submit 의 답에서(R3-design 3.2). 보고서를 낸 끝과 RA 초안 올리기가 같은 꼴로 낸다."""
    ctx.routine("report", ra_upload=out.get("ra_upload"), **{k: out[k] for k in ("ra_report_id", "ra_error", "ra_next") if out.get(k)},
                coverage=out.get("coverage") or {}, warnings=list(out.get("warnings") or []))


async def _upload_ra(ctx: _Ctx) -> None:
    """끝난 실행의 Report Archive 초안 올리기(R3-design §8 — 화면의 'RA 초안 올리기', ra.upload 'ask') — routine_report_submit(upload=true).
    포털은 끝난 실행에 저장 보고서를 돌려주고(다시 렌더하지 않는다 — R3-D12), 게이트웨이는 그 사람 이름으로 올리되 이미 만든 초안(ra_report_id)이
    있으면 다시 만들지 않는다. 보고서 글은 다시 흘리지 않는다(화면이 이미 보인다) — routine{report} · 짧은 안내 · routine{done, done}. 못 올리면
    그 까닭을 안내하고 실행은 그대로 done 이다(실행기 오류의 'running · 이어서' 로 끝내지 않는다 — 끝난 실행이다)."""
    ctx.emit("status", {"step": "Report Archive 초안을 올리는 중 — 저장한 보고서로"})
    try:
        out = await ctx.gw.tool("routine_report_submit", {"run_id": ctx.run_id, "upload": True})
    except RoutineGatewayError as exc:          # 거절(저장 보고서가 없는 옛 실행 등) · 끊김 · 인증 거절 — 다시 누르면 다시 올린다
        ctx.say(f"Report Archive 초안을 올리지 못했다 — {str(exc)[:400].rstrip('.')}. 잠시 뒤 'RA 초안 올리기' 를 다시 누른다.")
        ctx.end("done", str(exc))
        print(f"[routine] RA 초안 올리기 실패 — 실행 {ctx.run_id} · {exc}"[:300])
        return
    _report_frame(ctx, out)
    rid, err = out.get("ra_report_id"), out.get("ra_error")
    if rid and out.get("ra_note"):
        text = f"이 실행의 Report Archive 초안은 이미 있다 — {rid}. 게시는 RA 에서 한다."
    elif rid:
        text = f"Report Archive 초안을 올렸다 — {rid}. 게시는 RA 에서 한다."
    elif err:
        text = f"Report Archive 초안을 올리지 못했다 — {str(err)[:400].rstrip('.')}."
    else:
        text = f"Report Archive 초안을 만들지 않았다(이 루틴의 올리기 방식 {out.get('ra_upload') or '모름'})."
    ctx.say(text)
    ctx.end("done")
    print(f"[routine] RA 초안 올리기 — 실행 {ctx.run_id} · {rid or err or out.get('ra_upload')}")
