# MCP 심의 잡 원장을 KST 날마다 세어 내는 이용 통계 창구의 셈(HWAXPortal docs/ops-dashboard M2 · OD-12)
"""포털 관리자 '이용 통계' 가 날마다 접어 가는 원천이다 — 포털 컨테이너는 잡 원장(DELIB_JOB_DIR)을 열 수 없어 공유 비밀 창구(/usage-rollup)로 묻는다.

  · 세는 것은 잡 원장에 남는 심의(MCP 로 맡긴 잡 · 심의 묶음이 띄운 잡)뿐이다. 웹 챗에서 연 심의는 원장에 남지 않는다(메모리에만) — 포털의 챗 감사가
    그 턴을 센다. 루틴 · 카드 대조도 이 서버에 오래 남는 원장이 없다(포털의 루틴 실행 원장 · 카드 대조 감사가 센다). 답의 note 가 그렇게 말한다.
  · 잡은 줄을 서거나 돌기 시작한 KST 날에 든다. 종류 · 상태 · 판단 LLM(glm · external)마다 수 · 보고서가 붙은 수 · 좌석 · 라운드 · 걸린 시간의 합.
  · 원장은 목록 색인(delib_jobs.usage_heads)으로 읽는다 — 원장 전부(발언 · 결정문)를 열지 않고, 창 앞에서 멈춘 원장은 색인도 열지 않는다.
  · 사람은 소문자 메일(퍼센트로 적힌 주인은 푼다)이고 그때의 소속(잡을 연 때 게이트웨이가 실은 값)을 함께 낸다. 주인이 없는 잡은 수만 센다.
  · 뺄 계정(exclude)은 잡 수 · 사람 줄 모두에서 뺀다. 화두 · 결정문 · 라벨 · 좌석 이름은 싣지 않는다.
  · 사람별 LLM 토큰(usage_tokens — 사용자 결정 2026-11-22 · OD-53)도 함께 낸다 — 날 × 사람 × 종류 × 모델마다 호출 · 모름(사용량이 없던 호출) · 입력 ·
    출력 · 캐시 읽기와 적기 시작한 날(llm_since — 그 전 날은 기록 전이다). 뺄 계정은 사람 칸으로 뺀다.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote

CONTRACT = 1
KST = timezone(timedelta(hours=9))
MAX_RANGE_DAYS = 400            # 포털 이용 통계의 가장 긴 기간(HWAXPortal backend/app/usage_stats.py MAX_DAYS)
DEFAULT_RANGE_DAYS = 3          # 비우면 오늘까지 사흘 — 포털이 날마다 다시 접는 창
EXCLUDE_MAX = 200
FIELDS = ("since", "until", "exclude")
NOTE = ("잡 원장에 남는 심의(MCP 로 맡긴 잡 · 심의 묶음)만 센다 — 웹 챗에서 연 심의는 원장에 남지 않아(메모리에만) 빠지고, 루틴 · 카드 대조도 이 서버에 "
        "오래 남는 원장이 없다(포털의 챗 감사 · 루틴 실행 원장 · 카드 대조 감사가 센다). 잡은 줄을 서거나 돌기 시작한 날(KST)에 든다.")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_KIND_OK = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_STATUS_OK = re.compile(r"[a-z_]{1,24}")


def _day_of(v, what: str) -> date:
    if not isinstance(v, str) or not _DAY.fullmatch(v):
        raise ValueError(f"{what} 는 YYYY-MM-DD(KST 날)다")
    try:
        return date.fromisoformat(v)
    except ValueError as exc:
        raise ValueError(f"{what} 는 YYYY-MM-DD(KST 날)다") from exc


def params(raw: dict, today: str | None = None) -> dict:
    """물음의 칸을 읽는다 — 틀린 꼴은 ValueError(창구가 422 로 답한다). 기간은 조용히 줄이지 않는다."""
    end = _day_of(raw["until"], "until") if raw.get("until") is not None else (
        date.fromisoformat(today) if today else datetime.now(KST).date())
    start = _day_of(raw["since"], "since") if raw.get("since") is not None else end - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if start > end:
        raise ValueError("since 가 until 보다 뒤다")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"기간은 {MAX_RANGE_DAYS}일까지다")
    ex = raw.get("exclude")
    if ex is not None and (not isinstance(ex, list) or len(ex) > EXCLUDE_MAX or not all(isinstance(x, str) for x in ex)):
        raise ValueError(f"exclude 는 글자 목록이다({EXCLUDE_MAX}개까지)")
    return {"since": start.isoformat(), "until": end.isoformat(), "exclude": sorted({x.strip().lower() for x in ex or [] if x.strip()})}


def since_ts(p: dict) -> float:
    """기간 첫날의 KST 자정(epoch 초) — 원장 파일을 mtime 으로 먼저 거르는 문턱."""
    d = date.fromisoformat(p["since"])
    return datetime(d.year, d.month, d.day, tzinfo=KST).timestamp()


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def _born(job: dict) -> float | None:
    """잡이 줄을 서거나 돌기 시작한 때 — 둘 다 있으면 이른 쪽. 둘 다 없으면(옛 기록) 마지막으로 고친 때."""
    seen = [t for t in (_num(job.get("queued_at")), _num(job.get("started_at"))) if t is not None]
    return min(seen) if seen else _num(job.get("updated_at")) or _num(job.get("finished_at"))


def fold(heads, p: dict) -> dict:
    """잡의 머리(목록 색인 · 메모리의 잡)들 → 기간의 답. 읽는 칸은 색인에 있는 것뿐이다."""
    ex = set(p["exclude"])
    jobs: dict[tuple, dict] = {}
    actors: dict[tuple, dict] = {}
    anon: dict[str, int] = {}
    for j in heads:
        born = _born(j)
        if born is None:
            continue
        day = datetime.fromtimestamp(born, KST).date().isoformat()
        if not p["since"] <= day <= p["until"]:
            continue
        user = unquote(str(j.get("user") or "")).strip().lower()
        if user in ex:
            continue
        kind = str(j.get("job") or j.get("kind") or "")
        kind = kind if _KIND_OK.fullmatch(kind) else "(other)"
        status = str(j.get("status") or "")
        status = status if _STATUS_OK.fullmatch(status) else "(other)"
        llm = "external" if (j.get("opts") or {}).get("llm") == "external" else "glm"
        r = jobs.setdefault((day, kind, status, llm), {"day": day, "kind": kind, "status": status, "llm": llm, "n": 0, "with_report": 0,
                                                       "seats": 0, "seated": 0, "rounds": 0, "dur_s": 0.0, "dur_n": 0})
        seats = len(j.get("seats") or [])
        r["n"] += 1
        r["with_report"] += int(j.get("report_id") not in (None, "", 0))
        r["seats"] += seats
        r["seated"] += int(seats > 0)
        rnd = j.get("round")
        r["rounds"] += rnd if isinstance(rnd, int) and not isinstance(rnd, bool) and rnd > 0 else 0
        started, finished = _num(j.get("started_at")), _num(j.get("finished_at"))
        if started is not None and finished is not None and finished >= started:
            r["dur_s"] = round(r["dur_s"] + (finished - started), 1)
            r["dur_n"] += 1
        if "@" in user:
            a = actors.setdefault((day, user), {"day": day, "user": user, "aff": "", "n": 0})
            a["n"] += 1
            a["aff"] = str(j.get("owner_aff") or "").strip()[:80] or a["aff"]
        else:
            anon[day] = anon.get(day, 0) + 1
    return {"contract": CONTRACT, "tz": "Asia/Seoul", "since": p["since"], "until": p["until"], "excluded": len(ex),
            "jobs": [jobs[k] for k in sorted(jobs)], "actors": [actors[k] for k in sorted(actors)],
            "anon": [{"day": d, "n": n} for d, n in sorted(anon.items())], "note": NOTE}


def view(raw: dict, today: str | None = None) -> dict:
    """창구 한 번 — 물음을 읽고(틀리면 ValueError) 잡 원장의 머리를 세어 낸다. 이벤트 루프 밖에서 부른다(파일을 읽는다)."""
    import delib_jobs  # noqa: PLC0415 — /health · /insights/cards 와 같다(심의 MCP 가 비활성이어도 답한다)
    import usage_tokens  # noqa: PLC0415
    p = params(raw, today)
    heads = delib_jobs.usage_heads(since_ts(p))
    llm, llm_since = usage_tokens.rows(p["since"], p["until"], p["exclude"])
    return {**fold(heads, p), "ledger": {"first_day": delib_jobs.usage_first_day(), "scanned": len(heads)},
            "llm": llm, "llm_since": llm_since}
