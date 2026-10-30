# 챗 턴의 잡 상태 조회를 코드가 기다린다 — 상태 도구 알아보기 · 결과에서 상태 읽기 · 이 턴에 건 잡 id · 다시 조회 고리 · 안내 · 화면 프레임 · 계수
"""HWAXPortal docs/request_261030.md HA-28 · 설계 docs/request-1030/design-ha28.md · 맞물림 design-integration.md(C-3 · C-4).

DW-02 후속 — run_checklist 를 건 뒤 LLM 이 get_task 를 26번 불렀고(LLM 요청 29번 · 85초) 끝을 못 보고 저장된 옛 결과로 답했다. 상태 조회는
코드가 기다린다.

  · 상태 도구 — 원래 이름 목록(BUILTIN, 앱 접두는 tools-map 의 앱 키로 벗긴다). 게이트웨이가 routine_read(읽기로 확인된 도구)를 주면 그 안의
    도구만 열고, 그때만 박스 덧붙임(CHAT_STATUS_TOOLS)이 먹는다 — 잘못 적은 쓰기 도구를 10초마다 부르는 사고를 막는다.
  · 기다리는 때 — 챗 래퍼이고, 첫 결과가 '도는 중' 이고, **이 턴에 건 잡의 id**(born)이거나 **같은 호출의 되풀이**일 때만. 상태만 묻는
    질문('잡 42 어떻게 돼 가?')을 10분 붙잡지 않는다 — 그때는 지금 상태와 '한 번 더 부르면 코드가 기다린다' 안내를 단다.
  · born — 상태 도구가 아닌 도구의 성공한 결과가 맨 위(또는 job · task · … 칸)에 '도는 중' 잡 하나를 주면 그 사전의 id 칸 값들. 턴 상태만
    있으면 래퍼 종류와 상관없이 배운다(C-3 — 확정 종결 · 새어 나온 호출 · 승인 실행이 건 잡도).
  · 고리 — POLL_S 마다 같은 호출을 다시 하고(한 번의 한도 min(POLL_CALL_MAX_S, 남은 몫)), 끝 · 상한 · 실패 · 꼴 바뀜 · 멈춤 술어(C-4)에서
    접는다. 그동안 LLM 은 부르지 않고 화면에는 진행 줄(사건 EVENT → 챗 스트림의 status)을 흘린다. LLM 에는 마지막 결과 하나와 안내 한 줄.
  · 상태 낱말 — 받은 답(HWAXPortal docs/request-1030/outbound.md)으로 정했다. StepForge pending · running / done · failed · cancelled
    (cancel_requested 는 따로 칸 — 상태가 아니다), DocForge queued · running / done · failed · cancelled 와 **숫자 status 는 도구 오류**라
    기다리지 않는다. 표에 없는 값은 '모름' 이라 기다리지 않는다.

이 모듈은 표준 라이브러리만 읽는다 — app 을 읽지 않는다(사건 내기 · 멈춤 술어는 부르는 쪽이 넘긴다). 손잡이는 기동 때 한 번 읽는다(반영은
재기동). 시험은 모듈 속성(MAX_S · POLL_S · TELL_S …)을 바꿔 짧게 돈다.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from collections import Counter

EVENT = "hwax_status_wait"      # 사용자 사건 이름 — 챗 스트림이 status 로 바꾼다(밖으로는 나가지 않는다)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[status-wait] env {name}='{raw}' 숫자 파싱 실패 — 기본값 {default:g} 사용", flush=True)
        return default


# 손잡이 — CHAT_STATUS_WAIT_MAX_S 는 한 턴에 코드가 잡을 기다리는 시간의 **합** 상한(0 이면 끈다 — 종전 그대로), POLL_S 는 다시 조회 간격
# (2 보다 작으면 2), CHAT_STATUS_TOOLS 는 상태 도구로 더할 원래 이름(쉼표 — 게이트웨이가 routine_read 를 줄 때 그 안의 도구에만 먹는다).
MAX_S = max(0.0, _env_float("CHAT_STATUS_WAIT_MAX_S", 600.0))
POLL_S = max(2.0, _env_float("CHAT_STATUS_WAIT_POLL_S", 10.0))
EXTRA = frozenset(x.strip().lower() for x in os.environ.get("CHAT_STATUS_TOOLS", "").split(",") if x.strip())
TELL_S = 30.0               # 같은 낱말이면 진행 줄을 이 간격에 한 번(낱말이 바뀌면 곧바로) — 10분 기다림이면 활동 목록에 20줄 남짓
POLL_CALL_MAX_S = 120.0     # 다시 조회 한 번의 한도 위쪽 — 게이트웨이 600 · MCP_CALL_TIMEOUT_S 900 보다 먼저 걸려 무엇이 느린지 우리 문구로 말한다
BORN_TEXT_MAX = 20_000      # 잡 시작 결과로 읽는 글 길이 위쪽 — 긴 목록 · 보고서는 잡 하나가 아니다

BUILTIN = frozenset(("get_task", "job_status", "get_job_status", "get_job", "docforge_status", "doe_status", "study_get"))
# 상태 낱말(소문자, '-' · 빈칸은 '_'). 표에 없는 값(created · generated · paused · unknown …)은 '모름' — 기다리지 않는다.
RUNNING = frozenset((
    "queued", "pending", "running", "submitted", "started", "starting", "in_progress", "processing", "caching", "canary",
    "configuring", "completing", "cancelling", "canceling", "license_wait"))
FINISHED = frozenset((
    "done", "succeeded", "success", "completed", "complete", "finished", "ready", "failed", "failure", "error", "cancelled",
    "canceled", "timeout", "timed_out", "interrupted", "killed", "aborted", "partial", "halted", "node_fail", "preempted",
    "out_of_memory", "boot_fail", "deadline", "resubmitted", "stopped", "expired", "rejected"))
_STATE_KEYS = ("status", "state", "job_status", "job_state")
_NEST_KEYS = ("job", "task", "campaign", "study", "run", "data")
_PROGRESS_KEYS = ("progress", "progress_pct", "percent", "pct")
_DETAIL_KEYS = ("message", "stage", "step", "phase")
_ID_KEY = re.compile(r"^(id|.*_id)$")       # app._ID_ARG_RE 와 같은 꼴(시험이 둘을 묶는다)

_LOCK = threading.Lock()                    # /health 는 스레드 풀에서, 기다림은 이벤트 루프에서 돈다(app._HUB_FOLD_LOCK 과 같다)
_STAT = {"waits": 0, "ended": 0, "capped": 0, "failed": 0}
_STAT_OF = {"end": "ended", "cap": "capped", "fail": "failed", "shape": "failed"}    # stop · 취소는 어디에도 안 센다


def on() -> bool:
    return MAX_S > 0


def new_turn() -> dict:
    """턴 상태 — born(이 턴에 건 잡의 id) · seen(상태 도구의 '원래 이름|인자' 호출 수) · spent_s(이 턴에 기다린 합)."""
    return {"born": set(), "seen": Counter(), "spent_s": 0.0}


def health() -> dict:
    with _LOCK:
        stat = dict(_STAT)
    return {"on": on(), "poll_s": POLL_S, "max_s": MAX_S, **stat}


def _count(key: str) -> None:
    with _LOCK:
        _STAT[key] += 1


# ── 이름 · 결과 읽기 ───────────────────────────────────────────────────────────────────────────────────────────────

def base_name(name, app_key=None) -> str:
    """원래 이름 — 노출 이름이 앱 키 접두(<앱키에서 '-' 를 뺀 것>_)로 시작하면 벗긴다(게이트웨이는 이름이 겹칠 때만 붙인다). 소문자."""
    n = str(name or "").strip().lower()
    if app_key:
        p = str(app_key).replace("-", "").lower() + "_"
        if n.startswith(p) and len(n) > len(p):
            return n[len(p):]
    return n


def is_status_tool(name, app_key=None, routine_read=None) -> bool:
    """상태 도구인가 — 기능이 꺼져 있으면 늘 거짓. routine_read(게이트웨이가 읽기로 확인한 노출 이름)가 있으면 그 안에 있어야 하고, 그때만
    박스 덧붙임이 먹는다. 없으면(옛 게이트웨이 — '모름') 기본 목록만 연다."""
    if not on():
        return False
    base = base_name(name, app_key)
    if routine_read is None:
        return base in BUILTIN
    return base in (BUILTIN | EXTRA) and str(name or "") in routine_read


def text_of(v) -> str:
    """도구 반환에서 글만 — (내용 조각 목록, artifact) · 조각 목록 · 글. app 의 _norm 은 그림을 저장하는 부작용이 있어 중간 조회에 쓰지 않는다."""
    if isinstance(v, tuple) and len(v) == 2:
        v = v[0]
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "".join(str(i.get("text", "")) for i in v if isinstance(i, dict) and "text" in i)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, default=str)
    return ""


def _obj(text):
    s = text.strip() if isinstance(text, str) else ""
    if not s.startswith("{"):
        return None
    try:
        v = json.loads(s)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def _state(obj: dict):
    """(상태를 읽은 사전, 낱말, 'run' | 'end') — 맨 위, 그다음 job · task · … 칸의 사전에서 처음 걸린 것. 못 읽으면 None."""
    st = obj.get("status")
    if isinstance(st, (int, float)) and not isinstance(st, bool):
        return None         # 숫자 status 는 도구 오류다(DocForge {ok:false, error, status:403}) — 기다리지 않는다
    for d in (obj, *(obj.get(k) for k in _NEST_KEYS)):
        if not isinstance(d, dict):
            continue
        for k in _STATE_KEYS:
            w = d.get(k)
            if not isinstance(w, str):
                continue
            w = re.sub(r"[\s-]+", "_", w.strip().lower())
            if w in RUNNING:
                return d, w, "run"
            if w in FINISHED:
                return d, w, "end"
    return None


def _progress(d: dict):
    for k in _PROGRESS_KEYS:
        v = d.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
            continue
        if isinstance(v, float) and 0.0 <= v <= 1.0:
            v *= 100            # 0 ~ 1 실수(ODB) — 정수는 그대로(StepForge · DocForge · DynaForge). 화면에만 쓴다(LLM 에는 원문이 간다)
        return int(round(min(100.0, max(0.0, v))))
    return None


def job_state(text):
    """결과 글의 잡 상태 — {phase: 'run' | 'end', word, progress, detail}. JSON 객체가 아니거나 상태 낱말을 못 읽으면 None('모름')."""
    obj = _obj(text)
    got = _state(obj) if obj is not None else None
    if got is None:
        return None
    d, word, phase = got
    detail = next((" ".join(d[k].split())[:80] for k in _DETAIL_KEYS if isinstance(d.get(k), str) and d[k].strip()), "")
    return {"phase": phase, "word": word, "progress": _progress(d), "detail": detail}


def _ids(d) -> set:
    if not isinstance(d, dict):
        return set()
    return {str(v) for k, v in d.items()
            if isinstance(k, str) and _ID_KEY.match(k) and isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip()}


def job_record(text) -> bool:
    """결과 글이 잡 하나의 상태인가 — JSON 객체이고, 상태 낱말을 읽은 사전에 id 칸이 있고, 맨 위가 ok:false 가 아니다. 끝 낱말(failed ·
    error …)이어도 참이다 — '그 잡은 실패했다' 는 조회가 성공해 돌려준 사실이다(자기검증 원문 색인이 쓴다 — 호출 실패와 가른다)."""
    obj = _obj(text)
    if obj is None or obj.get("ok") is False:
        return False
    got = _state(obj)
    return bool(got and _ids(got[0]))


def born_ids(text) -> set:
    """잡 시작 결과에서 이 턴에 건 잡의 id — '도는 중' 상태를 읽은 그 사전의 id 칸 값들(글). 목록 속 잡 · 끝난 잡 · 긴 글은 빈 집합."""
    if not isinstance(text, str) or len(text) > BORN_TEXT_MAX:
        return set()
    obj = _obj(text)
    got = _state(obj) if obj is not None else None
    return _ids(got[0]) if got and got[2] == "run" else set()


def _call_key(base: str, args) -> str:
    try:
        s = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(args)
    return f"{base}|{s[:400]}"


# ── 글 ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def _dur(s) -> str:
    s = max(0, int(round(float(s or 0))))
    m, r = divmod(s, 60)
    return f"{m}분 {r}초" if m and r else f"{m}분" if m else f"{r}초"


def _pct(st: dict | None) -> str:
    if not st:
        return "?"
    return f"{st.get('word')} {st['progress']}%" if st.get("progress") is not None else str(st.get("word"))


def _now_txt(st: dict) -> str:
    return f"상태 {st['word']}" + (f", 진행률 {st['progress']}%" if st.get("progress") is not None else "")


def _note_snapshot(st: dict, left: float) -> str:
    return (f"[코드 기다림 — 아직 끝나지 않았다({_now_txt(st)}). 진행 상태만 물었으면 지금 상태로 답하라. 끝난 결과가 필요하면 같은 도구를 "
            f"같은 인자로 한 번 더 부르라 — 코드가 {_dur(POLL_S)}마다 다시 보며 최대 {_dur(left)} 기다린다(그 사이 LLM 을 부르지 않는다)]")


def _note_budget(st: dict) -> str:
    return (f"[코드 기다림 — 이 턴의 기다림 상한({_dur(MAX_S)})을 이미 다 써 기다리지 않았다. 아직 끝나지 않았다({_now_txt(st)}). "
            "예전 결과로 대신 답하지 말고 지금 상태를 알려라]")


def _note(outcome: str, tool: str, st: dict, elapsed: float, polls: int, why: str) -> str:
    if outcome == "end":
        return (f"[코드 기다림 — {tool} 를 {_dur(elapsed)} 기다려({_dur(POLL_S)}마다 {polls}번 다시 조회) 끝난 상태({st['word']})를 받았다. "
                "위가 끝난 뒤의 결과다 — 이 결과로 답하라]")
    if outcome == "cap":
        return (f"[코드 기다림 — {tool} 를 {_dur(elapsed)} 기다렸지만 이 턴의 상한({_dur(MAX_S)})에 닿았다. 아직 끝나지 않았다({_now_txt(st)}). "
                "위는 끝나기 전의 마지막 조회다 — 이번 잡의 결과는 아직 없다. 예전에 저장된 결과를 이번 결과처럼 답하지 말고, 지금 상태와 "
                "'끝난 뒤 다시 물어 달라' 를 알려라. 이 턴에는 더 기다리지 않는다]")
    if outcome == "fail":
        return (f"[코드 기다림 — {tool} 를 {_dur(elapsed)} 기다리다 다시 조회가 실패했다({why}). 위는 그 전 마지막 조회다({_now_txt(st)}) — "
                "끝난 결과가 아니다]")
    if outcome == "shape":
        return f"[코드 기다림 — {tool} 를 {_dur(elapsed)} 기다리는 사이 결과에서 진행 상태를 더 읽을 수 없어 멈췄다. 위가 마지막 조회 결과다]"
    return (f"[코드 기다림 — {tool} 를 {_dur(elapsed)} 기다리다 접었다(이 턴이 다른 까닭으로 먼저 멈춘다). 위는 그 전 마지막 조회다"
            f"({_now_txt(st)}) — 끝난 결과가 아니다]")


def _data(phase: str, tool: str, st: dict, elapsed: float, budget: float, polls: int, why: str = "") -> dict:
    d = {"phase": phase, "tool": tool, "state": st.get("word"), "progress": st.get("progress"), "detail": st.get("detail") or "",
         "elapsed_s": round(elapsed, 1), "max_s": round(budget, 1), "polls": polls}
    if why:
        d["why"] = why
    return d


def frame(data: dict) -> dict:
    """사건 자료 → 챗 스트림 status 의 꼴. tool 은 비운다 — 포털 중계가 서버 사본 활동에 남기지 않고, 다음 턴 근거(호출 · 결과 짝)에 섞이지
    않는다. 화면은 step 만 상태 줄로 보인다(wait 은 지금 화면이 모르고 버린다)."""
    tool, ph = data.get("tool") or "?", data.get("phase")
    st = {"word": data.get("state"), "progress": data.get("progress")}
    if ph == "start":
        step = f"코드가 잡이 끝나기를 기다립니다 — {tool} · {_pct(st)} · {_dur(POLL_S)}마다 다시 봄 · 최대 {_dur(data.get('max_s'))}"
    elif ph == "wait":
        step = (f"잡을 기다리는 중 — {tool} · {_pct(st)}" + (f" · {data['detail']}" if data.get("detail") else "")
                + f" · {_dur(data.get('elapsed_s'))} 지남")
    elif ph == "end":
        step = f"잡이 끝났습니다 — {tool} · {data.get('state')} · {_dur(data.get('elapsed_s'))} 기다림"
    elif ph == "cap":
        step = f"기다림 상한 {_dur(MAX_S)} — {tool} 는 아직 {_pct(st)} · 지금 상태로 답합니다"
    elif ph == "fail":
        step = f"잡 상태를 다시 보지 못해 기다림을 멈춥니다 — {tool} · {data.get('why') or '?'}"
    elif ph == "shape":
        step = f"잡 상태를 더 읽을 수 없어 기다림을 멈춥니다 — {tool}"
    else:
        step = f"잡 기다림을 접습니다 — {tool} · {_pct(st)} · 이 턴이 먼저 멈춥니다"
    return {"step": step, "tool": None, "wait": dict(data)}


async def _emit(emit, data: dict) -> None:
    if emit is None:
        return
    try:
        await emit(data)
    except Exception as exc:  # noqa: BLE001 — 진행 줄을 못 내도 기다림은 계속한다(사건을 낼 곳이 없는 직접 호출 등)
        print(f"[status-wait] 진행 줄을 내지 못했다: {exc!r:.160}", flush=True)


# ── 갈고리 · 고리 ──────────────────────────────────────────────────────────────────────────────────────────────────

async def handle(name, app_key, routine_read, args, out, again, turn, *, chat: bool = True, emit=None, stop=None) -> tuple:
    """도구 래퍼의 R2(성공한 호출에만) — (LLM 에 넘길 결과, 안내 글('' 이면 없음), polled).

    name · args 는 안쪽 이름 · 인자(invoke_tool 경유면 벗긴 것), again() 은 같은 호출을 다시 하는 코루틴(관문을 다시 지나지 않는다),
    turn 은 new_turn() 사전(None 이면 아무것도 하지 않는다 — 심의 · 카탈로그 · 직접 호출). chat 이 거짓이면 기다리지 않고 born 만 배운다.
    polled 는 상태 도구이고 결과에서 상태 낱말을 읽었다는 뜻이다 — 되풀이 꼬리를 달지 않는다(못 읽는 꼴은 종전 꼬리가 LLM 루프를 막는다).
    emit(data) 는 진행 줄을 내는 코루틴, stop() 은 참이면 잠 · 다시 조회 앞에서 접는 술어다(C-4 — 기본은 늘 거짓)."""
    if turn is None or not on():
        return out, "", False
    text = text_of(out)
    if not is_status_tool(name, app_key, routine_read):
        turn["born"] |= born_ids(text)
        return out, "", False
    key = _call_key(base_name(name, app_key), args)
    turn["seen"][key] += 1
    st = job_state(text)
    if st is None:
        return out, "", False
    if st["phase"] == "end" or not chat:
        return out, "", True
    left = MAX_S - turn["spent_s"]
    mine = bool(_ids(args) & turn["born"]) or turn["seen"][key] >= 2
    if left <= 0:
        return out, "\n" + _note_budget(st), True
    if not mine:
        return out, "\n" + _note_snapshot(st, left), True
    last, note = await _wait(str(name), st, out, again, turn, left, emit, stop or (lambda: False))
    return last, "\n" + note, True


async def _wait(tool: str, st: dict, out, again, turn: dict, budget: float, emit, stop) -> tuple:
    t0 = time.monotonic()
    polls, last, told, outcome, why = 0, out, t0, "cap", ""
    _count("waits")
    await _emit(emit, _data("start", tool, st, 0.0, budget, 0))
    try:
        while True:
            if stop():
                outcome = "stop"
                break
            await asyncio.sleep(min(POLL_S, max(0.0, budget - (time.monotonic() - t0))))
            if stop():
                outcome = "stop"
                break
            lim = min(POLL_CALL_MAX_S, max(POLL_S, budget - (time.monotonic() - t0)))
            try:
                nxt = await asyncio.wait_for(again(), timeout=lim)     # 한도에 걸리면 그 호출을 취소한다(읽기라 안전하다)
            except asyncio.TimeoutError:
                outcome, why = "fail", f"{_dur(lim)} 안에 답하지 않았다"
                break
            except Exception as exc:  # noqa: BLE001 — 예외 한 번에 멈춘다(LLM 이 같은 호출을 다시 하면 남은 몫 안에서 다시 기다린다)
                while getattr(exc, "exceptions", None):     # MCP 전송 예외는 anyio TaskGroup 에 싸여 온다 — 안쪽으로 말한다
                    exc = exc.exceptions[0]
                outcome, why = "fail", f"{type(exc).__name__}: {str(exc)[:200]}"
                break
            polls += 1
            nst = job_state(text_of(nxt))
            if nst is None:
                last, outcome = nxt, "shape"
                break
            last, prev, st = nxt, st, nst
            if st["phase"] == "end":
                outcome = "end"
                break
            now = time.monotonic()
            if now - t0 >= budget:
                outcome = "cap"
                break
            if st["word"] != prev["word"] or now - told >= TELL_S:
                told = now
                await _emit(emit, _data("wait", tool, st, now - t0, budget, polls))
    except asyncio.CancelledError:
        # 사용자 중지 · 탭 닫기 — 턴 태스크가 취소됐다. 잡은 앱에서 계속 돈다(기다림만 멈춘다). 계수에는 넣지 않는다.
        print(f"[status-wait] {tool}: cancel — {time.monotonic() - t0:.0f}초 · 다시 조회 {polls}번 · 상태 {st['word']}", flush=True)
        raise
    elapsed = time.monotonic() - t0
    turn["spent_s"] += elapsed
    if outcome in _STAT_OF:
        _count(_STAT_OF[outcome])
    await _emit(emit, _data(outcome, tool, st, elapsed, budget, polls, why))
    print(f"[status-wait] {tool}: {outcome} — {elapsed:.0f}초 · 다시 조회 {polls}번 · 상태 {st['word']}" + (f" · {why}" if why else ""),
          flush=True)
    return last, _note(outcome, tool, st, elapsed, polls, why)
