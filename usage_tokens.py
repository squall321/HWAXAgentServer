# 사람별 LLM 토큰과 '누구의 일' 자리 — 이 서버가 부른 LLM 호출의 입력 · 출력 · 캐시 읽기 토큰을 사람 × KST 날 × 종류 × 모델로 세어 두고 이용 통계 창구에 낸다(HWAXPortal docs/ops-dashboard OD-52 · OD-53)
"""사용자 결정(2026-11-22) — 에이전트 서버의 사람별 LLM 토큰을 적는다(관리자만 · 400일). 그리고 사람의 일에서 나온 서비스 자격 게이트웨이 호출에
'누구의 일' 감사 머리를 싣는다(게이트웨이는 감사 줄에만 적는다).

자리(scope)
  · 이 문맥(태스크)과 여기서 새로 만드는 태스크의 일이 누구의 무엇인지 — tag(user=…, kind=…). 챗 턴 입구 · MCP 심의 잡 · 심의 준비 · 카탈로그 창구가
    사람을, 엔진(띵킹 · 심의 · 루틴 · 카드 대조)이 종류를 단다. 호출 추적(call_trace.tag)과 같은 꼴이지만 늘 켜져 있다(그쪽은 꺼지면 아무것도 안 한다).
  · 종류는 chat · think · delib · routine · card_review · offload · other 다(offload 는 토큰 절감 2판 — 위임 · 절 초안 · 변환 · 결과에 묻기,
    HWAXPortal docs/token-saving TS-16. 그 전 기록의 그 몫은 other 로 남는다). 사람은 소문자 이메일(퍼센트로 적힌 값은 푼다) — 꼴이 아니면 빈 값이다.

세기
  · LLM 마다 콜백 하나(instrument — 앱의 LLM 공장 _mk_llm · 외부 응답자가 붙인다). 시작 콜백에서 그 자리(사람 · 종류)를 붙잡고, 끝 콜백에서 서버가 준
    사용량만 읽는다(call_trace._usage — 프롬프트 · 답 글은 열지 않는다). 사용량이 없는 호출(흘려 받는 호출 · 외부 응답자)은 '모름' 으로 센다 — 지어내지
    않는다. 실패한 호출은 세지 않는다.
  · 메모리에 더해 두고 FLUSH_S 마다(그리고 창구가 물을 때 · 내려갈 때) sqlite 에 더한다. 쓰다 실패하면 그 몫을 메모리로 되돌린다(잃지 않는다).
  · 자리 — USAGE_TOKENS_DIR, 없으면 잡 원장(DELIB_JOB_DIR)의 형제 usage/(리포 밖 — /data). 폴더 0700 · 파일 0600(사람별 줄이 든다). 400일 뒤 지운다.

이 모듈은 표준 라이브러리만 맨 위에서 읽는다 — langchain_core 는 콜백을 지을 때 읽는다(call_trace 와 같다).
"""
from __future__ import annotations

import atexit
import contextvars
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

KST = timezone(timedelta(hours=9))
KINDS = ("chat", "think", "delib", "routine", "card_review", "offload", "other")
RETAIN_DAYS = 400               # 사람별 줄의 보존(사용자 결정 — 포털 usage_actor_daily 와 같다)
FLUSH_S = 30.0                  # 메모리의 셈을 sqlite 에 더하는 간격
OPEN_MAX = 4096                 # 끝을 못 본 호출 기록의 상한 — 넘치면 가장 오래된 것을 버린다
ROOT = Path(__file__).resolve().parent
_EMAIL = re.compile(r"[^@\s,;]{1,100}@[^@\s,;]{1,100}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}")
_SCOPE: contextvars.ContextVar = contextvars.ContextVar("hwax_usage_scope", default=None)
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS tokens (day TEXT NOT NULL, user TEXT NOT NULL, kind TEXT NOT NULL, model TEXT NOT NULL, calls INTEGER NOT NULL,"
    " unknown INTEGER NOT NULL, tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL, cache_read INTEGER NOT NULL,"
    " PRIMARY KEY (day, user, kind, model)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL) WITHOUT ROWID",
)

_LOCK = threading.Lock()        # 메모리의 셈(_ACC) — 콜백(이벤트 루프)과 더하는 스레드가 함께 만진다
_DB_LOCK = threading.Lock()     # sqlite 쓰기는 한 번에 하나
_ACC: dict[tuple, list] = {}    # (날, 사람, 종류, 모델) → [호출, 모름, 입력, 출력, 캐시 읽기]
_STATE: dict = {"thread": None, "recorded": 0, "flushed": 0, "errors": 0, "last_error": None, "last_flush": None, "pruned_day": None}


# ── 자리 ───────────────────────────────────────────────────────────────────────────────────────────────────
def norm_user(v) -> str:
    """이메일 꼴로 — 퍼센트 인코딩을 풀고 소문자. 꼴이 아니면 ''."""
    try:
        s = unquote(str(v or "")).strip().lower()
    except Exception:  # noqa: BLE001
        return ""
    return s if len(s) <= 200 and _EMAIL.fullmatch(s) else ""


def tag(*, user: str | None = None, kind: str | None = None) -> None:
    """지금 문맥(태스크)과 여기서 새로 만드는 태스크의 일에 사람 · 종류를 단다. None 은 그 칸을 그대로 두고, 빈 값 · 꼴이 틀린 사람은 그 칸을 뗀다."""
    cur = dict(_SCOPE.get() or {})
    if user is not None:
        u = norm_user(user)
        if u:
            cur["user"] = u
        else:
            cur.pop("user", None)
    if kind is not None:
        cur["kind"] = kind if kind in KINDS else "other"
    _SCOPE.set(cur)


def current() -> tuple[str, str]:
    """(사람, 종류) — 자리가 없으면 ('', 'other')."""
    d = _SCOPE.get() or {}
    return d.get("user", ""), d.get("kind", "other")


def work_user() -> str:
    """이 일이 누구의 것인가 — 서비스 자격 게이트웨이 호출의 '누구의 일' 감사 머리(app._with_groups)가 읽는다."""
    return current()[0]


# ── 세기 ───────────────────────────────────────────────────────────────────────────────────────────────────
def _count(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def record(model: str | None, tokens: dict | None, *, user: str | None = None, kind: str | None = None, now: float | None = None) -> None:
    """호출 하나 — tokens 는 {prompt, completion, cache_read}(call_trace._usage 꼴). 입력 · 출력이 둘 다 없으면 '모름' 으로 센다."""
    try:
        u, k = current()
        u = norm_user(user) if user is not None else u
        k = (kind if kind in KINDS else "other") if kind is not None else k
        m = str(model or "")
        m = m if _MODEL.fullmatch(m) else ("(unknown)" if not m else "(other)")
        day = datetime.fromtimestamp(time.time() if now is None else now, KST).date().isoformat()
        t = tokens or {}
        tin, tout, tc = _count(t.get("prompt")), _count(t.get("completion")), _count(t.get("cache_read"))
        with _LOCK:
            row = _ACC.setdefault((day, u, k, m), [0, 0, 0, 0, 0])
            row[0] += 1
            if tin is None and tout is None:
                row[1] += 1
            else:
                row[2] += tin or 0
                row[3] += tout or 0
                row[4] += tc or 0
            _STATE["recorded"] += 1
        _ensure_thread()
    except Exception:  # noqa: BLE001 — 세기가 LLM 호출을 건드리지 않는다
        pass


# ── 저장 ───────────────────────────────────────────────────────────────────────────────────────────────────
def _resolve_dir() -> Path:
    """자리 — USAGE_TOKENS_DIR, 없으면 잡 원장의 형제 usage/(ARTIFACT_DIR 을 realpath 로 풀고 그 부모 — seat_box.box_dir 와 같은 셈)."""
    raw = os.environ.get("USAGE_TOKENS_DIR", "").strip()
    if raw:
        return Path(raw)
    jd = os.environ.get("DELIB_JOB_DIR", "").strip()
    if jd:
        return Path(jd).parent / "usage"
    art = os.environ.get("ARTIFACT_DIR") or str(ROOT / "artifacts")
    return Path(os.path.realpath(art).rstrip("/")).parent / "usage"


# 자리는 읽힐 때 한 번 정한다 — 더하는 스레드가 깨어날 때마다 환경을 다시 읽으면, 그때 환경이 바뀌어 있는 문맥(시험이 환경을 갈아 끼운 동안 등)에서 엉뚱한
# 자리에 쓴다. 바꾸려면 재기동한다(시험은 이 값을 갈아 끼운다).
STORE_DIR: Path = _resolve_dir()


def store_dir() -> Path:
    return STORE_DIR


def db_path() -> Path:
    return store_dir() / "llm-tokens.sqlite"


def _open() -> sqlite3.Connection:
    d = store_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    p = d / "llm-tokens.sqlite"
    if not p.exists():
        os.close(os.open(p, os.O_WRONLY | os.O_CREAT, 0o600))
    conn = sqlite3.connect(str(p), timeout=30.0, isolation_level=None)
    for ddl in _SCHEMA:
        conn.execute(ddl)
    return conn


def flush(now: float | None = None) -> int:
    """메모리의 셈을 sqlite 에 더한다 → 더한 줄 수. 실패하면 그 몫을 메모리로 되돌린다(다음에 다시 더한다)."""
    with _LOCK:
        batch = dict(_ACC)
        _ACC.clear()
    if not batch:
        return 0                # 더할 것이 없으면 파일을 열지도 않는다
    today = datetime.fromtimestamp(time.time() if now is None else now, KST).date()
    with _DB_LOCK:
        try:
            with closing(_open()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("INSERT OR IGNORE INTO meta VALUES ('started', ?)", (today.isoformat(),))
                    conn.executemany(
                        "INSERT INTO tokens VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (day, user, kind, model) DO UPDATE SET"
                        " calls = calls + excluded.calls, unknown = unknown + excluded.unknown, tokens_in = tokens_in + excluded.tokens_in,"
                        " tokens_out = tokens_out + excluded.tokens_out, cache_read = cache_read + excluded.cache_read",
                        [(*k, *v) for k, v in batch.items()])
                    if _STATE["pruned_day"] != today:
                        conn.execute("DELETE FROM tokens WHERE day < ?", ((today - timedelta(days=RETAIN_DAYS - 1)).isoformat(),))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            _STATE["pruned_day"] = today
            _STATE["flushed"] += len(batch)
            _STATE["last_flush"] = time.time()
            return len(batch)
        except Exception as exc:  # noqa: BLE001
            with _LOCK:
                for k, v in batch.items():
                    row = _ACC.setdefault(k, [0, 0, 0, 0, 0])
                    for i, n in enumerate(v):
                        row[i] += n
            _STATE["errors"] += 1
            _STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            return 0


def rows(since: str, until: str, exclude: list[str] | None = None) -> tuple[list[dict], str | None]:
    """기간(KST 날, 양끝 포함)의 줄과 적기 시작한 날 — 먼저 메모리의 셈을 더한다. 뺄 계정은 사람 칸으로 뺀다."""
    flush()
    ex = sorted({norm_user(x) for x in exclude or []} - {""})
    if not db_path().exists():
        return [], None         # 아직 한 번도 세지 않았다(적기 전) — 읽으려고 파일을 만들지 않는다
    q = f" AND user NOT IN ({','.join('?' * len(ex))})" if ex else ""
    with _DB_LOCK, closing(_open()) as conn:
        got = conn.execute("SELECT v FROM meta WHERE k = 'started'").fetchone()
        out = [{"day": d, "user": u, "kind": k, "model": m, "calls": c, "unknown": un, "tokens_in": ti, "tokens_out": to, "cache_read": cr}
               for d, u, k, m, c, un, ti, to, cr in conn.execute(
                   "SELECT day, user, kind, model, calls, unknown, tokens_in, tokens_out, cache_read FROM tokens"
                   f" WHERE day >= ? AND day <= ?{q} ORDER BY day, user, kind, model", (since, until, *ex))]
    return out, (got[0] if got else None)


def _loop() -> None:
    while True:
        time.sleep(FLUSH_S)
        try:
            flush()
        except Exception:  # noqa: BLE001 — 더하는 스레드는 죽지 않는다
            pass


def _ensure_thread() -> None:
    t = _STATE["thread"]
    if t is not None and t.is_alive():
        return
    with _LOCK:
        t = _STATE["thread"]
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=_loop, name="usage-tokens-flush", daemon=True)
        _STATE["thread"] = t
    try:
        t.start()
    except RuntimeError:        # 스레드를 못 띄운다 — 창구가 물을 때 · 내려갈 때 더한다
        _STATE["thread"] = None


@atexit.register
def _flush_at_exit() -> None:
    try:
        if _ACC:
            flush()
    except Exception:  # noqa: BLE001
        pass


def health() -> dict:
    """/health.usage_tokens — 이번 기동에 센 호출 · 더한 줄 · 실패 수와 마지막 실패(사람 · 모델 이름은 싣지 않는다)."""
    with _LOCK:
        pending = len(_ACC)
    return {"recorded": _STATE["recorded"], "flushed": _STATE["flushed"], "pending": pending, "errors": _STATE["errors"],
            "last_error": _STATE["last_error"], "last_flush": _STATE["last_flush"]}


# ── LLM 콜백 ───────────────────────────────────────────────────────────────────────────────────────────────
def _recorder_class():
    """기록기(LangChain 콜백) — 처음 쓸 때 짓는다. 스트리밍 콜백이 아니다(붙여도 비스트리밍 호출은 비스트리밍 그대로다). 프롬프트 · 답 글은 열지 않는다."""
    from langchain_core.callbacks import BaseCallbackHandler

    import call_trace  # noqa: PLC0415 — 끝난 결과에서 사용량 · 모델 이름만 꺼내는 셈을 함께 쓴다

    class UsageTokensRecorder(BaseCallbackHandler):
        run_inline = True       # 부른 태스크 안에서 곧바로 — 자리(사람 · 종류)가 그 태스크의 문맥이다
        raise_error = False

        def __init__(self, model_hint: str = ""):
            self.model_hint = model_hint
            self._open: dict = {}

        def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
            try:
                ip = kwargs.get("invocation_params")
                model = ""
                if isinstance(ip, dict):
                    model = str(ip.get("model") or ip.get("model_name") or "")
                if len(self._open) >= OPEN_MAX:
                    self._open.pop(next(iter(self._open)), None)
                self._open[run_id] = (current(), model or self.model_hint)
            except Exception:  # noqa: BLE001
                pass

        def on_llm_end(self, response, *, run_id, **kwargs):
            got = self._open.pop(run_id, None)
            if got is None:
                return
            (user, kind), model = got
            try:
                tokens, said = call_trace._usage(response)
            except Exception:  # noqa: BLE001
                tokens, said = {}, None
            record(said or model, tokens, user=user, kind=kind)

        def on_llm_error(self, error, *, run_id, **kwargs):
            self._open.pop(run_id, None)

    return UsageTokensRecorder


def instrument(llm, model_hint: str = ""):
    """그 LLM 에 기록기(콜백)를 붙인다 — 요청은 바뀌지 않는다. 붙이지 못하면 그대로 돌려준다(세지 못할 뿐이다)."""
    if llm is None:
        return llm
    try:
        cbs = getattr(llm, "callbacks", None)
        if cbs is None or isinstance(cbs, list):
            llm.callbacks = [*(cbs or []), _recorder_class()(model_hint)]
    except Exception as exc:  # noqa: BLE001
        print(f"[usage-tokens] ⚠ LLM 에 기록기를 붙이지 못했다({type(exc).__name__}) — 그 LLM 의 토큰은 세지 않는다", flush=True)
    return llm


def _reset_for_tests() -> None:
    """시험 전용 — 메모리의 셈과 상태를 비운다(떠 있는 스레드는 데몬이라 그대로 둔다)."""
    with _LOCK:
        _ACC.clear()
    _STATE.update(recorded=0, flushed=0, errors=0, last_error=None, last_flush=None, pruned_day=None)


__all__ = ["KINDS", "tag", "current", "work_user", "record", "flush", "rows", "instrument", "health", "store_dir", "db_path"]
