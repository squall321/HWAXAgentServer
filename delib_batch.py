# 심의 묶음 실행 — 항목을 동시 한도 안에서 하나씩 띄우고, 같은 key 를 두 번 · 끝난 key 를 다시 돌리지 않으며, 멈춤 · 재시도 · 좌석 유실 표지를 단다(DL-02)
"""HWAXPortal docs/request-1117 (DL-02) — deliberate_batch_start · deliberate_batch_status · deliberate_batch_stop.

**규칙(굳은 것).**
  · 같은 key 는 두 번 돌지 않는다 — 묶음 안 key 는 유일하고, 항목은 상태 기계(pending → starting → running → 끝)를 한 동기 구간에서만 넘어가며,
    '띄우는 중(starting)' 은 띄우기 전에 원장에 쓰고 잡에 표지(batch.token)를 단다 — 재기동이 그 사이에 끼면 표지로 잡을 찾아 잇는다(새로 띄우지 않는다).
    같은 사람의 같은 이름 묶음이 돌고 있으면 새 묶음을 받지 않는다.
  · 끝난 key 는 다시 돌지 않는다 — done 은 끝이다. 같은 사람이 같은 이름으로 다시 시작하면 그 이름의 앞 묶음 **전부**(보존 기한 안)에서 끝난 key 는
    건너뛴다(skipped · done_in) — 멈춘 묶음을 같은 이름으로 다시 시작하면 남은 것만 돈다.
  · 멈춘 뒤에는 새로 띄우지 않는다 — 멈춤은 줄 선(pending) 항목과 엔진 줄에 서 있기만 한(아직 돌지 않은) 잡을 stopped 로 닫는다. 이미 도는 잡은 끝까지
    지켜본다(접으려면 그 job_id 로 deliberate_cancel — 도는 잡을 접을지는 사용자 결정으로 남아 있다).
  · 실패한 항목은 retries 번까지만 새 잡으로 다시 띄운다(한 항목의 시도는 1 + retries 번) — 다시 띄우기는 물러섰다가(retry_at — RETRY_BACKOFF_S 에서
    두 배씩, 위 RETRY_BACKOFF_MAX_S) 다음 틱부터다. 한 틱에서 같은 항목의 시도를 둘 쓰지 않는다(2차 검토 1 — 종전엔 게이트웨이가 잠깐 끊긴 6ms 에 시도가
    다 탔다). 근거 참조가 불통(시간 초과 · 게이트웨이를 못 부름)이면 시도로 세지 않고 물러섰다 다시 본다(UNREACHABLE_WAITS_MAX 번까지 — 그 뒤는 실패한
    시도). 사람이 접은 잡은 다시 띄우지 않는다(cancelled). 항목의
    인자는 묶음을 받을 때 엔진의 검사(delib_jobs.start(dry_run))로 먼저 거른다 — 틀리면 묶음을 만들지 않는다. 띄울 때 생긴 뜻밖의 오류도 실패한 시도다.
  · 근거 참조는 다 풀려야 띄운다 — 묶음 항목의 참조가 하나라도 풀리지 않으면 그 시도는 실패다(근거 없이 돌아 '믿을 만한' 결정으로 끝나지 않게).
    deliberate_start 하나는 종전대로 빼고 evidence_omitted 로 알린다.
  · 동시 한도 안에서 하나씩 — 이 묶음의 도는 · 줄 선 잡은 concurrency 건까지(기본 1 — 아래 CONCURRENCY_DEFAULT). 엔진의 사람별 · 전역 · 좌석 합 한도와 줄
    (delib_jobs)은 그대로 걸린다 — 엔진이 지금 이 사람의 잡을 받지 않으면(delib_jobs.accepts — start 와 같은 판정: 멈춤이거나, 이 사람이 상한에 걸렸고 줄도
    찼다) 그 항목은 pending 으로 남고 다음 틱에 다시 본다(시도로 세지 않는다 · 참조도 풀지 않는다). 남이 줄을 채워도 상한에 안 걸린 사람의 묶음은 간다.
  · 좌석 유실 — 끝난 잡의 좌석 가운데 어느 라운드든 발언하지 못한 좌석(엔진의 '좌석 유실' 알림 · 라운드별 전사 · 마지막 표결의 응답 못 함)이 하나라도
    있으면 그 항목의 결정은 '믿을 수 없음(not_trustworthy)' 이다. 비율로 가르지 않는다 — 하나라도 빠지면 늘 그렇다.
  · 멈춤 감지(stall_s — 기본 0 · 끔) — 켜면 도는 잡이 마지막 사건 뒤 max(stall_s, 그 잡의 quiet_ok_s) 초 넘게 조용할 때 접고 실패한 시도로 센다.
    엔진은 시간으로 잡을 죽이지 않는다(조용한 것이 멈춘 것이 아니다 — delib_jobs.summary 의 ⚠) — 그래서 끄는 것이 기본이고 하한(STALL_FLOOR_S)을 둔다.
  · 재기동 — 묶음 원장(<잡 원장>/batches/<id>.json, 0600)으로 넘긴다. 줄 선 잡은 엔진이 다시 세우고, 끊긴 잡(interrupted)은 제자리 이어하기
    (delib_jobs.resume_load — deliberate_continue 의 같은 잡 길)로 잇는다. 못 이으면 실패한 시도다.
  · 그룹 — 묶음의 잡은 묶음에 적힌 그룹으로 돈다(엔진 줄이 '줄에 설 때의 그룹' 으로 도는 것과 같다 — delib_jobs DO-D2 ③). 관리자 표지(portal-admin —
    게이트웨이가 '지금' 관리자일 때만 싣는다)는 적지 않고, 주인이 진행 조회 · 멈춤을 부를 때마다 그때의 그룹으로 고쳐 적는다. 끊긴 잡의 제자리
    이어하기는 그 그룹으로 resume_load 의 '권한이 줄었다' 검사를 받는다.
  · 원장 — 묶음 id 꼴(BATCH_ID_RE)이 아니면 파일을 보지 않는다. 끝난 묶음은 KEEP_S(30일) 뒤 지운다(그 뒤로는 그 묶음의 끝난 key 를 모른다).
    줄 선 항목의 근거 · 손잡이는 원장에 든다 — 항목마다 evidence · advanced 를 JSON 으로 적은 길이의 합이 INLINE_MAX 를 넘으면 받지 않는다(큰 근거는
    참조). 보존 정리는 기동 때와 하루마다(PRUNE_EVERY_S) 돈다.
  · 첫 먹이기는 요청 밖에서 돈다(kick — 근거 참조 풀이가 길어도 게이트웨이의 호출 한도 600초에 걸리지 않게). 시작 응답에는 아직 엔진에 든 잡이 없을 수 있다.

**끝남 알림.** 바깥 주소로 보내지 않는다(웹훅 없음 — 사용자 결정 R-5 ③). 끝나면 묶음 원장에 finished_at · notice 를 쓰고 진행 조회
(deliberate_batch_status)가 finished · notice 로 보인다. 포털은 끝난 묶음 창구(finished_since — app POST /deliberate/batch/finished, 공유 비밀만 ·
HWAXPortal docs/request-1117 contracts §7)를 주기로 읽어 주인에게 알림을 남긴다(엔진이 밀지 않는다 — 사용자 결정 R-12 ②). 끝난 묶음의 끝난 시각 ·
수 · 알림 글은 원장과 그 요약 색인에 남아 재기동을 넘기고, 보존 기한(KEEP_S)이 지나 지워지면 창구에서도 사라진다.

**권한.** 시작은 신원(사람)이 있는 호출만 — 묶음의 잡은 그 사람 이름으로 돈다(그 사람의 사람별 한도 안). 조회는 잡과 같은 규칙(주인 · 같은 소속 ·
관리자 · 신원 없는 운영), 멈춤은 주인만(deliberate_cancel 과 같다).
신원은 게이트웨이가 사람의 PAT 를 검증한 뒤 머리(x-hwax-user · x-hwax-groups)에 박아 공유 비밀로 부를 때의 그것이다 — HTTP 창구(app
/deliberate/batch/*)도 같은 신뢰로 연다(2차 검토 8 — 사용자가 열어 둔 설계 판단). 묶음은 수 시간 ~ 며칠 돌고 토큰을 쥐지 않으므로(원장 · 시작 인자 파일에
토큰을 남기지 않는다) 사람 PAT 를 받아도 시작 때 한 번 보는 것이 전부다. 그 비밀(게이트웨이 연결의 Authorization — seat_box.secret)을 쥔 쪽은 게이트웨이
(GW_TOKEN)와 포털(GATEWAY_SHARED_TOKEN)뿐이다 — 그 둘만이 신원을 말할 수 있다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

import delib_jobs
import delib_refs

log = logging.getLogger("agent.batch")

BATCH_DIR = "batches"
ITEMS_MAX = 200
NAME_MAX = 120
KEY_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
ITEM_FIELDS = ("key", "question", "job", "evidence", "advanced")
# 기본 동시 수 1 — 10~11석 심의 둘을 함께 돌리면 공유 LLM 에 줄을 서 거의 모든 좌석이 시간 초과로 빠졌다(11-17 요청 — 4시간 손실). 올릴 때는 좌석 수와
# LLM 서버를 보고 올린다. 위는 엔진의 사람별 한도와 CONCURRENCY_MAX 가운데 작은 쪽이다.
CONCURRENCY_DEFAULT = 1
CONCURRENCY_MAX = 6
RETRIES_DEFAULT = 1
RETRIES_MAX = 5
STALL_FLOOR_S = 600.0
TICK_S = 10.0
_LIVE = ("running", "stopping")             # 묶음이 아직 할 일이 있는 상태
_FINISHED = ("done", "stopped", "failed")   # 끝난 묶음 — failed 는 멈추지 않고 끝났는데 끝난(done · skipped) 항목이 없고 실패한 항목이 있다
_FLIGHT = ("starting", "running")           # 항목이 엔진에 있거나 들어가는 중
_TERMINAL = ("done", "failed", "cancelled", "stopped", "skipped")
BATCH_ID_RE = re.compile(r"batch-\d{8}-\d{6}-[0-9a-f]{6}")
KEEP_S = 30 * 86400                         # 끝난 묶음 원장의 보존(초) — 지나면 bind · 색인을 읽을 때 지운다
INLINE_MAX = 2_000_000                      # 한 묶음의 항목 근거 · 손잡이(JSON 길이) 합 상한(자) — 원장이 줄 선 항목의 그것을 들고 있다
RETRY_BACKOFF_S, RETRY_BACKOFF_MAX_S = 60.0, 1800.0     # 다시 띄우기 · 불통 기다림의 물러서기(초) — n 번째에 60 · 120 · 240 … · 위 30분
UNREACHABLE_WAITS_MAX = 12                  # 근거 참조 불통으로 물러서는 횟수의 위 — 넘으면 실패한 시도로 센다(합 약 5시간)
PRUNE_EVERY_S = 86400.0                     # 보존 정리 간격(초) — 기동 때 한 번, 그 뒤로 하루마다
ADMIN_GROUP = "portal-admin"                # 게이트웨이가 '지금' 관리자일 때만 싣는 표지(mcp_server._ADMIN_GROUP 와 같다) — 묶음에 적지 않는다
TRUST_OK, TRUST_BAD, TRUST_UNKNOWN = "trustworthy", "not_trustworthy", "unknown"

_BATCHES: dict[str, dict] = {}
_INDEX: dict | None = None                   # 묶음 id → 요약(_summary) — 처음 쓸 때 원장 폴더를 한 번 읽고, 쓸 때마다 고친다
_FEEDING: set = set()
_KICKS: set = set()                          # 요청 밖에서 도는 첫 먹이기 태스크(kick) — 끝날 때까지 붙잡아 둔다
_LAST_PRUNE = 0.0
_APP = None
_TICKER = None


# ── 원장 ──────────────────────────────────────────────────────────────────────────────────────────────────
def _dir() -> Path:
    return delib_jobs.JOB_DIR / BATCH_DIR


def _path(bid: str) -> Path:
    return _dir() / f"{bid}.json"


def _persist(b: dict) -> None:
    """묶음 원장을 원자적으로 쓴다(폴더 0700 · 파일 0600 — 화두 · 근거가 든다). 못 쓰면 경고만 — 묶음은 메모리에서 계속 돈다."""
    b["updated_at"] = delib_jobs._now()
    d = _dir()
    tmp = d / f".{b['id']}.{os.getpid()}.tmp"
    try:
        delib_jobs._private_dir(d)
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(b, ensure_ascii=False))
        os.replace(tmp, _path(b["id"]))
        _index()[b["id"]] = _summary(b)
    except (OSError, TypeError, ValueError) as exc:
        log.warning("[batch %s] 원장을 못 썼다: %r", b.get("id"), exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _load(bid: str) -> dict | None:
    if not isinstance(bid, str) or not BATCH_ID_RE.fullmatch(bid):
        return None                         # 꼴이 아니면 파일을 보지 않는다(경로 조각이 섞이지 않게)
    try:
        b = json.loads(_path(bid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return b if isinstance(b, dict) and b.get("v") == 1 and isinstance(b.get("items"), list) else None


def get(bid: str) -> dict | None:
    if not isinstance(bid, str) or not BATCH_ID_RE.fullmatch(bid):
        return None
    return _BATCHES.get(bid) or _load(bid)


def _summary(b: dict) -> dict:
    """색인 한 줄 — 같은 이름 거절 · 끝난 key 건너뛰기 · 보존이 쓰는 칸만."""
    return {"id": b["id"], "owner": delib_jobs._norm(b.get("owner")), "name": b.get("name"), "status": b.get("status"),
            "created_at": b.get("created_at"), "finished_at": b.get("finished_at"),
            "done": {it["key"]: (it.get("done_in") or {"batch_id": b["id"], "job_id": it.get("job_id")})
                     for it in b.get("items") or [] if it.get("state") in ("done", "skipped")},
            "trust": {it["key"]: {"trust": it.get("trust"), "seat_loss": it.get("seat_loss")}
                      for it in b.get("items") or [] if it.get("state") in ("done", "skipped")},
            # 끝난 묶음 창구(finished_since)가 원장을 다시 읽지 않고 내는 칸
            **({"counts": _counts(b), "notice": b.get("notice")} if b.get("status") in _FINISHED else {})}


def _expired(sm: dict) -> bool:
    return sm.get("status") in _FINISHED and delib_jobs._now() - float(sm.get("finished_at") or 0) > KEEP_S


def _index() -> dict:
    """묶음 색인 — 처음 한 번 원장 폴더를 읽고(보존 기한이 지난 끝난 묶음은 지운다) 그 뒤로는 메모리에서 고친다(만들기마다 파일을 다 읽지 않게)."""
    global _INDEX  # noqa: PLW0603
    if _INDEX is None:
        idx = {}
        try:
            files = list(_dir().glob("batch-*.json"))
        except OSError:
            files = []
        for f in files:
            b = _load(f.stem)
            if b is None:
                continue
            sm = _summary(b)
            if _expired(sm):
                try:
                    f.unlink()
                except OSError:
                    pass
                continue
            idx[b["id"]] = sm
        _INDEX = idx
    return _INDEX


# ── 만들기 ────────────────────────────────────────────────────────────────────────────────────────────────
def _int(name: str, v, lo: int, hi: int, default: int) -> int:
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
        raise ValueError(f"{name} 는 정수다 — 받은 값 {str(v)[:40]}")
    v = int(v)
    if not lo <= v <= hi:
        raise ValueError(f"{name} 는 {lo}~{hi} 다 — 받은 값 {v}")
    return v


def _items(raw, *, app, user: str) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("items 는 비지 않은 목록이다 — [{key, question, job, evidence, advanced}]")
    if len(raw) > ITEMS_MAX:
        raise ValueError(f"items 가 {len(raw)}건이다 — 한 묶음은 {ITEMS_MAX}건까지 받는다")
    seen, out = set(), []
    for i, it in enumerate(raw):
        if not isinstance(it, dict):
            raise ValueError(f"items[{i}] 는 객체다 — 받은 꼴: {type(it).__name__}")
        extra = sorted(set(it) - set(ITEM_FIELDS))
        if extra:
            raise ValueError(f"items[{i}] 에 모르는 칸 {extra[:5]} — 받는 칸은 {' · '.join(ITEM_FIELDS)} 다")
        key = it.get("key")
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise ValueError(f"items[{i}].key 는 영문 · 숫자 · _.:- 64자 안의 글이다 — 받은 값: {str(key)[:40]!r}")
        if key in seen:
            raise ValueError(f"items 에 key {key!r} 가 둘이다 — 같은 key 는 한 묶음에 하나다(두 번 돌지 않는다)")
        seen.add(key)
        q = it.get("question")
        if not isinstance(q, str) or not q.strip():
            raise ValueError(f"items[{i}]({key}).question 이 비었다 — 심의할 화두가 필요하다")
        job = delib_jobs.resolve_job(str(it.get("job") or "default"))
        ev = it.get("evidence")
        if ev is not None and not isinstance(ev, list):
            raise ValueError(f"items[{i}]({key}).evidence 는 목록이다 — deliberate_start 의 evidence 와 같은 꼴")
        try:
            delib_refs.parse(ev)
        except delib_refs.RefError as exc:
            raise ValueError(f"items[{i}]({key}) — {exc}") from None
        adv = it.get("advanced")
        if adv is not None and not isinstance(adv, dict):
            raise ValueError(f"items[{i}]({key}).advanced 는 객체다 — deliberate_start 의 advanced 와 같다")
        _preflight(app, i, key, job, q, adv, user)
        out.append({"key": key, "question": q.strip(), "job": job, "evidence": list(ev) if ev else None,
                    "advanced": dict(adv) if adv else None, "state": "pending", "job_id": None, "tries": 0, "attempts": []})
    return out


def _preflight(app, i: int, key: str, job: str, question: str, adv, user: str) -> None:
    """항목 하나를 엔진이 받는지 띄우기 전에 본다 — 띄울 때와 같은 손잡이 짓기(mcp_server._build_opts)와 엔진의 검사(delib_jobs.start(dry_run))."""
    import external_llm  # noqa: PLC0415
    import mcp_server  # noqa: PLC0415 — 도구 인자 → 엔진 손잡이를 같은 함수로
    try:
        delib_jobs.start(app, job, question, delib_opts=mcp_server._build_opts(advanced=adv) or None, user_email=user,
                         owner=external_llm.header_owner(user), dry_run=True)
    except Exception as exc:  # noqa: BLE001 — 무엇이든 받지 못하는 인자다(TypeError 따위도 — 띄울 때 묶음을 막지 않게, 반대 검토 M1)
        raise ValueError(f"items[{i}]({key}) — 엔진이 이 항목을 받지 못한다({type(exc).__name__}: {str(exc)[:200]})") from None


def _keep_groups(groups) -> list:
    """묶음에 적을 그룹 — 관리자 표지는 뺀다(그 표지는 '지금' 관리자인 호출에만 실린다 — 묶음의 잡이 며칠 뒤에도 관리자로 돌지 않게)."""
    return [str(g) for g in (groups or []) if str(g) != ADMIN_GROUP]


def _same_name(owner: str, name: str) -> list[dict]:
    """같은 사람의 같은 이름 묶음 요약 전부(보존 기한 안) — 오래된 차례."""
    me = delib_jobs._norm(owner)
    return sorted((sm for sm in _index().values() if sm["owner"] == me and sm["name"] == name),
                  key=lambda sm: float(sm.get("created_at") or 0))


def create(app, *, name, items, concurrency=None, retries=None, stall_s=None, user: str, groups: list, owner_aff: str = "") -> dict:
    """묶음을 만들고 곧바로 한 번 먹인다(띄울 수 있는 만큼). 틀리면 ValueError — 아무것도 만들지 않는다."""
    if not str(user or "").strip():
        raise ValueError("신원 없는 호출은 묶음을 시작하지 못한다 — 묶음의 잡은 그 사람 이름으로 돈다(사람 PAT 로 게이트웨이를 거쳐 부른다)")
    name = str(name or "").strip()
    if not name or len(name) > NAME_MAX:
        raise ValueError(f"name 은 1~{NAME_MAX}자 글이다")
    rows = _items(items, app=app, user=str(user).strip())
    # 원장에 드는 것은 본문 칸 하나가 아니라 항목 전부다 — 다른 칸(text · args …)에 실은 큰 값도 센다(2차 검토 6)
    inline = sum(len(json.dumps({"evidence": it.get("evidence"), "advanced": it.get("advanced")}, ensure_ascii=False, default=str))
                 for it in rows)
    if inline > INLINE_MAX:
        raise ValueError(f"항목들의 근거 · 손잡이가 JSON 으로 합 {inline:,}자다 — 묶음 원장이 줄 선 항목의 그것을 들고 있어 {INLINE_MAX:,}자까지 받는다. "
                         "큰 근거는 참조({ref: {kind, id}})로 준다")
    cap = max(1, min(CONCURRENCY_MAX, int(delib_jobs.limits()["max_running_per_user"])))
    conc = _int("concurrency", concurrency, 1, CONCURRENCY_MAX, CONCURRENCY_DEFAULT)
    notes = []
    if conc > cap:
        notes.append(f"concurrency {conc} → {cap}(엔진의 사람별 한도 안)")
        conc = cap
    tries = _int("retries", retries, 0, RETRIES_MAX, RETRIES_DEFAULT)
    if stall_s is None or stall_s == 0:
        stall = 0.0
    else:
        if isinstance(stall_s, bool) or not isinstance(stall_s, (int, float)) or stall_s < 0:
            raise ValueError(f"stall_s 는 0(끔) 이상의 초다 — 받은 값 {str(stall_s)[:40]}")
        stall = float(stall_s)
        if stall < STALL_FLOOR_S:
            notes.append(f"stall_s {stall:g} → {STALL_FLOOR_S:g}초(하한 — 조용한 좌석 발언 하나를 멈춘 것으로 읽지 않게)")
            stall = STALL_FLOOR_S
    same = _same_name(user, name)
    live = [sm for sm in same if sm.get("status") in _LIVE]
    if live:
        raise ValueError(f"같은 이름의 묶음 {live[-1]['id']} 가 아직 돈다 — 같은 key 를 두 번 돌리지 않는다. deliberate_batch_status 로 보거나 "
                         "deliberate_batch_stop 으로 멈춘 뒤(끝난 뒤) 같은 이름으로 다시 시작하면 남은 key 만 돈다")
    prev = same[-1] if same else None
    done, trust = {}, {}
    for sm in same:                         # 앞 묶음 전부의 끝난 key(반대 검토 L3) — 나중 묶음의 것이 이긴다
        done.update(sm.get("done") or {})
        trust.update(sm.get("trust") or {})
    for it in rows:
        src = done.get(it["key"])
        if src is not None:
            it.update(state="skipped", job_id=src.get("job_id"), done_in=src, evidence=None,
                      trust=(trust.get(it["key"]) or {}).get("trust"), seat_loss=(trust.get(it["key"]) or {}).get("seat_loss"))
    now = delib_jobs._now()
    b = {"v": 1, "id": f"batch-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}", "name": name, "owner": str(user).strip(),
         "groups": _keep_groups(groups), "groups_at": now, "owner_aff": str(owner_aff or "").strip(), "status": "running", "created_at": now,
         "finished_at": None, "concurrency": conc, "retries": tries, "stall_s": stall, "items": rows, "notes": notes,
         **({"previous": prev["id"]} if prev is not None else {})}
    _finish_if_done(b)                      # 다 건너뛴 묶음은 곧바로 닫는다 — 닫은 뒤에 쓴다(쓴 뒤에 닫으면 원장에 running 이 남아 이름을 잠갔다 — M2)
    if b["status"] in _LIVE:
        _BATCHES[b["id"]] = b
    _persist(b)
    global _APP  # noqa: PLW0603 — 먹이기가 쓰는 앱(바인드 전 시험에서도 돈다)
    _APP = app if app is not None else _APP
    log.info("[batch %s] 시작 %s — 항목 %d · 동시 %d · 재시도 %d · 멈춤 감지 %s", b["id"], name, len(rows), conc, tries, stall or "끔")
    return b


# ── 먹이기 ────────────────────────────────────────────────────────────────────────────────────────────────
def _attempt(it: dict, job_id, status: str, why: str = "") -> None:
    it["attempts"] = [*(it.get("attempts") or []), {"job_id": job_id, "status": status, "at": round(delib_jobs._now(), 3),
                                                     **({"why": str(why)[:300]} if why else {})}]


def _terminal(it: dict, state: str, why: str = "") -> None:
    it["state"] = state
    if why:
        it["error"] = str(why)[:400]
    ev = it.get("evidence")
    if ev:
        it["evidence_n"] = len(ev)
    it["evidence"] = None           # 끝난 항목의 근거(참조 또는 본문)는 원장에 남기지 않는다 — 잡의 시작 인자 파일과 같은 규칙
    it.pop("token", None)
    it.pop("retry_at", None)


def _backoff(n: int) -> float:
    return min(RETRY_BACKOFF_S * (2 ** max(0, int(n) - 1)), RETRY_BACKOFF_MAX_S)


def _failed_try(b: dict, it: dict, why: str) -> None:
    """실패한 시도 — 시도가 1 + retries 번 안이고 묶음이 멈추지 않았으면 물러섰다(retry_at) 다시 띄우도록 pending, 아니면 failed."""
    it.pop("stalled", None)
    if b.get("status") == "running" and int(it.get("tries") or 0) <= int(b.get("retries") or 0):
        it.update(state="pending", job_id=None, retry_at=round(delib_jobs._now() + _backoff(int(it.get("tries") or 1)), 3))
        it["last_error"] = str(why)[:400]
    else:
        _terminal(it, "failed", why)


def seat_loss(job: dict) -> dict:
    """끝난 잡의 좌석 유실 — {lost[좌석], by_round{라운드: [좌석]}, notices(엔진의 '좌석 유실' 알림 수), unanswered(마지막 표결에 응답 못 한 좌석),
    transcript_capped, known}. 셋 가운데 하나라도 있으면 유실이다. known 이 거짓이면(좌석 · 표결 · 전사가 없다) 가를 수 없다."""
    seats = [s.get("key") for s in (job.get("seats") or []) if isinstance(s, dict) and s.get("key")]
    t = job.get("turns") or []
    tf = job.get("turns_from")
    tf = tf if isinstance(tf, int) and not isinstance(tf, bool) and 0 <= tf <= len(t) else 0
    rows = t[tf:]
    capped = any(delib_jobs._turn_cap_row(x) for x in t)
    total = job.get("total_rounds")
    by_round: dict = {}
    if seats and isinstance(total, int) and total > 0 and not capped:
        for r in range(1, total + 1):
            spoke = {x.get("persona") for x in rows if isinstance(x, dict) and x.get("round") == r}
            miss = [s for s in seats if s not in spoke]
            if miss:
                by_round[str(r)] = miss
    unanswered = [v.get("seat") for v in (job.get("votes") or []) if isinstance(v, dict) and v.get("responded") is False]
    notices = sum(1 for x in (job.get("evidence_omitted") or []) if isinstance(x, dict) and x.get("source") == "좌석 유실")
    tally = job.get("tally") if isinstance(job.get("tally"), dict) else {}
    short = int(tally.get("total") or 0) - int(tally.get("responded") or 0) if tally else 0
    lost = list(dict.fromkeys([s for ks in by_round.values() for s in ks] + unanswered))
    known = bool(seats or job.get("votes") or rows)
    return {"lost": lost, "by_round": by_round, "notices": notices, "unanswered": unanswered,
            "transcript_capped": capped, "known": known, "lost_any": bool(lost or notices or short > 0)}


def _trust(sl: dict) -> str:
    if sl["lost_any"]:
        return TRUST_BAD
    return TRUST_OK if sl["known"] else TRUST_UNKNOWN


def _finished_job(b: dict, it: dict, job: dict) -> None:
    """엔진이 끝낸 잡 → 항목. done 이고 결정문이 있으면 끝(좌석 유실을 본다), 그 밖은 실패한 시도 · 사람의 접기."""
    st = job.get("status")
    if st == "done" and (job.get("decision") or job.get("result_text")):
        sl = seat_loss(job)
        it["seat_loss"], it["trust"] = sl, _trust(sl)
        _attempt(it, job["id"], "done")
        _terminal(it, "done")
        return
    if st == "cancelled" and not it.get("stalled"):
        _attempt(it, job["id"], "cancelled", job.get("error") or "")
        _terminal(it, "cancelled", "사람이 접었다 — 다시 띄우지 않는다")
        return
    why = ("멈춤 감지(stall_s)로 접었다" if st == "cancelled" else
           "결정문 없이 끝났다" if st == "done" else str(job.get("error") or st))
    _attempt(it, job["id"], st, why)
    _failed_try(b, it, why)


def _resume_interrupted(b: dict, it: dict, job: dict) -> str:
    """끊긴 잡을 제자리로 잇는다(mcp_server._resume_in_place 와 같은 길). 이었으면 '', 다음 틱에 다시 볼 일이면 'later', 못 이으면 까닭."""
    if b.get("status") != "running":
        return "묶음을 멈췄다 — 끊긴 잡을 다시 띄우지 않는다"
    res, why = delib_jobs.resume_load(job, groups=list(b.get("groups") or []))
    if res is None:
        return f"끊겼고 제자리로 잇지 못했다({why})"
    args = delib_jobs._read_args(job["id"]) or {}
    res.by = b["owner"]
    try:
        delib_jobs.start(_APP, job.get("job") or "default", job["question"], groups=list(args.get("groups") or []),
                         delib_opts=dict(args.get("opts") or {}), user_email=str(args.get("user_email") or ""),
                         owner=str(args.get("owner") or ""), owner_aff=str(args.get("owner_aff") or ""), resume=res)
    except (RuntimeError, delib_jobs.DelibPaused):
        return "later"
    except (ValueError, OSError) as exc:
        return f"끊겼고 제자리로 잇지 못했다({type(exc).__name__}: {str(exc)[:160]})"
    _attempt(it, job["id"], "resumed")
    return ""


def _stall(b: dict, it: dict, job: dict) -> None:
    """멈춤 감지 — 켠 묶음에서 도는 잡이 너무 오래 조용하면 접는다(다음 틱에 cancelled 로 보고 실패한 시도로 센다)."""
    if not b.get("stall_s") or job.get("status") != "running" or job.get("finishing"):
        return
    idle = delib_jobs._now() - float(job.get("updated_at") or delib_jobs._now())
    try:
        import mcp_server  # noqa: PLC0415 — 진행 조회와 같은 셈(quiet_ok_s)을 쓴다
        quiet = mcp_server._quiet_ok_s(job) or 0.0
    except Exception:  # noqa: BLE001 — 셈하지 못하면 stall_s 만 본다
        quiet = 0.0
    limit = max(float(b["stall_s"]), float(quiet))
    if idle <= limit:
        return
    try:
        delib_jobs.cancel(job["id"], by=b["owner"])
    except delib_jobs.DelibFinishing:
        return
    except (ValueError, PermissionError) as exc:
        log.warning("[batch %s] 멈춘 잡 %s 를 접지 못했다: %r", b["id"], job["id"], exc)
        return
    it["stalled"] = True
    log.warning("[batch %s] %s 의 잡 %s 가 %.0f초 조용해(한도 %.0f초) 접었다 — 실패한 시도로 센다", b["id"], it["key"], job["id"], idle, limit)


def _reconcile(b: dict) -> None:
    """엔진에 있는 항목을 원장과 맞춘다(동기 — await 없음)."""
    for it in b["items"]:
        if it.get("state") == "starting" and it.get("token") and not it.get("job_id"):
            _adopt(b, it)
        if it.get("state") != "running":
            continue
        job = delib_jobs.get(it.get("job_id") or "")
        if job is None:
            _attempt(it, it.get("job_id"), "missing", "잡 원장이 없다")
            _failed_try(b, it, "잡 원장이 없다")
            continue
        st = job.get("status")
        if st in ("queued", "running"):
            _stall(b, it, job)
            continue
        if st == "interrupted":
            why = _resume_interrupted(b, it, job)
            if why == "later" or not why:
                continue
            _attempt(it, job["id"], "interrupted", why)
            if b.get("status") != "running":
                _terminal(it, "stopped", why + f" — deliberate_continue({job['id']}) 로 이어 볼 수 있다")
            else:
                _failed_try(b, it, why)
            continue
        _finished_job(b, it, job)


def _adopt(b: dict, it: dict) -> None:
    """'띄우는 중' 에 끊긴 항목 — 그 표지(token)를 단 잡이 원장에 있으면 그 잡을 잇고, 없으면 pending 으로 되돌린다(띄우지 않았다)."""
    tok = it["token"]
    try:
        files = list(delib_jobs.JOB_DIR.glob("*.json"))
    except OSError:
        files = []
    for f in files:
        rec = delib_jobs._list_load(f)
        if rec and (rec.get("batch") or {}).get("token") == tok:
            it.update(state="running", job_id=rec["id"])
            it["tries"] = int(it.get("tries") or 0) + 1
            _attempt(it, rec["id"], "started")
            return
    it.pop("token", None)
    if b.get("status") == "running":
        it.update(state="pending")
    else:                                   # 멈춘 묶음 — 띄우지 않은 항목은 멈춤으로 닫는다(반대 검토 L1 — stopping 에 갇히지 않게)
        _terminal(it, "stopped", "묶음을 멈췄다 — 띄우지 않았다")


def _engine_full(b: dict, it: dict) -> bool:
    """엔진이 지금 이 사람의 잡을 받지 않을 때 — start 와 같은 판정(delib_jobs.accepts — 멈춤이거나, 이 사람이 상한에 걸렸고 줄도 찼다). 참조를 풀기 전에
    본다(반대 검토 L2). 줄 길이만 보면 남이 줄을 채운 동안 상한에 안 걸린 이 사람의 묶음도 굶었다(2차 검토 5)."""
    import external_llm  # noqa: PLC0415
    import mcp_server  # noqa: PLC0415
    return not delib_jobs.accepts(b["owner"], owner=external_llm.header_owner(b["owner"]),
                                  delib_opts=mcp_server._build_opts(advanced=it.get("advanced")) or None)


async def _launch(b: dict, it: dict) -> tuple[str, str]:
    """항목 하나를 띄운다 → (결과, 까닭). 결과 — started · later(멈춤 · 줄 참 — 시도로 세지 않는다) · stopped(푸는 사이 멈췄다) · refused(다시 해도
    같다 — 끝) · failed_try(실패한 시도 — retries 안이면 다시)."""
    if _engine_full(b, it):
        return "later", ""
    it["state"], it["token"] = "starting", uuid.uuid4().hex
    _persist(b)                                     # 띄우기 전에 쓴다 — 재기동이 끼면 표지로 잡을 찾는다(_adopt)
    ev, refs = it.get("evidence"), None
    try:
        slots = delib_refs.parse(ev)
        if slots is not None:
            res = await delib_refs.resolve(_APP, ev, groups=list(b.get("groups") or []), user=b["owner"], slots=slots)
            ev, refs = res["evidence"], res
    except delib_refs.RefError as exc:
        return "refused", str(exc)
    except Exception as exc:  # noqa: BLE001 — 뜻밖의 오류는 실패한 시도다(묶음을 막지 않는다 — 반대 검토 M1)
        log.exception("[batch %s] %s 의 근거 참조를 풀다 오류", b["id"], it["key"])
        return "failed_try", f"근거 참조를 풀다 오류가 났다({type(exc).__name__}: {str(exc)[:160]})"
    if b.get("status") != "running":                # 근거를 푸는 사이에 멈췄다 — 띄우지 않는다
        return "stopped", ""
    if refs is not None and refs["omitted"]:
        # 묶음 항목은 근거가 다 있어야 돈다(반대 검토 M7) — 빠진 채 돌면 근거 없는 결정이 '믿을 만한' 끝으로 남는다. 빠진 것이 모두 불통(시간 초과 ·
        # 게이트웨이를 못 부름)이면 시도가 아니라 물러섰다 다시 본다(2차 검토 1).
        why = "근거 참조를 다 풀지 못했다 — " + " / ".join(x["text"] for x in refs["omitted"][:5])
        bad = [r for r in refs["refs"] if not r.get("ok")]
        return ("wait" if bad and all(r.get("unreachable") for r in bad) else "failed_try"), why
    try:
        import external_llm  # noqa: PLC0415
        import mcp_server  # noqa: PLC0415 — 도구 인자 → 엔진 손잡이를 같은 함수로(_build_opts)
        opts = mcp_server._build_opts(evidence=ev, advanced=it.get("advanced"))
        job = delib_jobs.start(_APP, it["job"], it["question"], delib_opts=opts or None, groups=list(b.get("groups") or []),
                               user_email=b["owner"], owner=external_llm.header_owner(b["owner"]), owner_aff=b.get("owner_aff") or "",
                               refs=refs, batch={"id": b["id"], "key": it["key"], "token": it["token"]})
    except (RuntimeError, delib_jobs.DelibPaused):
        return "later", ""
    except ValueError as exc:
        return "refused", str(exc)
    except Exception as exc:  # noqa: BLE001 — 뜻밖의 오류는 실패한 시도다(반대 검토 M1 — 종전엔 묶음이 그 항목에서 영영 멈췄다)
        log.exception("[batch %s] %s 를 띄우다 오류", b["id"], it["key"])
        return "failed_try", f"띄우다 오류가 났다({type(exc).__name__}: {str(exc)[:160]})"
    it.update(state="running", job_id=job["id"])
    it["tries"] = int(it.get("tries") or 0) + 1
    _attempt(it, job["id"], "started")
    return "started", ""


def _sig(b: dict) -> tuple:
    """원장을 다시 쓸지 가르는 서명 — 묶음 상태와 항목마다 (상태 · 잡 · 시도 · 기록 수 · 믿음 · 멈춤 표지)."""
    return (b.get("status"), *((it.get("state"), it.get("job_id"), it.get("tries"), len(it.get("attempts") or []), it.get("trust"),
                               bool(it.get("stalled")), it.get("retry_at"), it.get("waits")) for it in b["items"]))


async def feed(b: dict) -> None:
    """묶음 하나를 먹인다 — 엔진과 맞추고(_reconcile) 빈 자리만큼 차례대로 띄운다(_launch). 같은 묶음을 겹쳐 먹이지 않는다. 원장은 바뀐 것이 있을
    때만 다시 쓴다(줄 선 항목의 근거 본문이 들어 있어 틱마다 쓰면 무겁다)."""
    if b["id"] in _FEEDING or b.get("status") not in _LIVE:
        return
    _FEEDING.add(b["id"])
    before = _sig(b)
    tried: set = set()
    try:
        _reconcile(b)
        while b.get("status") == "running" and not delib_jobs._CLOSING and _APP is not None:
            if sum(1 for x in b["items"] if x.get("state") in _FLIGHT) >= int(b.get("concurrency") or 1):
                break
            now = delib_jobs._now()
            it = next((x for x in b["items"] if x.get("state") == "pending" and float(x.get("retry_at") or 0) <= now
                       and x["key"] not in tried), None)
            if it is None:
                break
            tried.add(it["key"])            # 한 틱에서 같은 항목을 두 번 띄우지 않는다(2차 검토 1)
            outcome, why = await _launch(b, it)
            if outcome == "later":                  # 줄이 찼거나 새 심의 멈춤 — 띄우지 않았다(시도로 세지 않는다). 다음 틱에 다시 본다
                if it.get("state") == "starting":
                    it["state"] = "pending"
                    it.pop("token", None)
                    _persist(b)                     # '띄우는 중' 을 쓴 원장을 곧바로 고쳐 쓴다
                before = _sig(b)
                break
            if outcome == "stopped":
                _terminal(it, "stopped", "묶음을 멈췄다 — 띄우지 않았다")
            elif outcome == "refused":
                _attempt(it, None, "refused", why)
                _terminal(it, "failed", why)
            elif outcome == "wait" and int(it.get("waits") or 0) < UNREACHABLE_WAITS_MAX:
                it["waits"] = int(it.get("waits") or 0) + 1
                it.pop("token", None)
                it.update(state="pending", last_error=why[:400], retry_at=round(delib_jobs._now() + _backoff(it["waits"]), 3))
            elif outcome in ("failed_try", "wait"):
                it["tries"] = int(it.get("tries") or 0) + 1
                it.pop("token", None)
                _attempt(it, None, "failed", why)
                _failed_try(b, it, why)
            _persist(b)
            before = _sig(b)
        _finish_if_done(b)
        if _sig(b) != before:
            _persist(b)
    finally:
        _FEEDING.discard(b["id"])


def _counts(b: dict) -> dict:
    c = {k: 0 for k in ("pending", "starting", "running", "done", "failed", "cancelled", "stopped", "skipped")}
    for it in b["items"]:
        c[it.get("state")] = c.get(it.get("state"), 0) + 1
    c["not_trustworthy"] = sum(1 for it in b["items"] if it.get("trust") == TRUST_BAD)
    return c


def _finish_if_done(b: dict) -> None:
    """모든 항목이 끝났으면 묶음을 닫는다 — done(멈춘 묶음은 stopped) · finished_at · notice. 바깥으로 알리지 않는다(진행 조회가 보인다)."""
    if b.get("status") not in _LIVE or any(it.get("state") not in _TERMINAL for it in b["items"]):
        return
    c = _counts(b)
    b["status"] = ("stopped" if b.get("status") == "stopping" else
                   "failed" if c["failed"] and not (c["done"] or c["skipped"]) else "done")
    b["finished_at"] = delib_jobs._now()           # 끝난 시각 — 부른 쪽이 곧바로 원장에 쓴다(feed · create · stop) · 색인 요약에도 실린다
    b["notice"] = (f"묶음 「{b['name']}」 이 끝났다({ {'stopped': '멈춤', 'failed': '모두 실패'}.get(b['status'], '모두 끝남') }) — 끝남 {c['done']} · "
                   f"건너뜀 {c['skipped']} · "
                   f"실패 {c['failed']} · 접힘 {c['cancelled']} · 멈춤 {c['stopped']}"
                   + (f" · 좌석 유실로 믿을 수 없는 결정 {c['not_trustworthy']}건" if c["not_trustworthy"] else "") + ".")
    for it in b["items"]:
        it["evidence"] = None
    log.info("[batch %s] %s", b["id"], b["notice"])


# ── 멈춤 · 조회 ───────────────────────────────────────────────────────────────────────────────────────────
def touch(b: dict, *, user: str, groups) -> None:
    """주인이 부르면 그때의 그룹으로 고쳐 적는다(관리자 표지는 빼고) — 이 뒤로 띄우는 잡 · 제자리 이어하기의 권한 검사가 지금 그룹을 본다(반대 검토 L4).
    주인이 아니거나 신원이 없으면 그대로다."""
    me = delib_jobs._norm(user)
    if not me or me != delib_jobs._norm(b.get("owner")) or b.get("status") not in _LIVE:
        return
    g = _keep_groups(groups)
    b["groups_at"] = delib_jobs._now()
    if g != list(b.get("groups") or []):
        b["groups"] = g
        _persist(b)


def stop(bid: str, *, by: str) -> dict:
    b = get(bid)
    if b is None:
        raise ValueError(f"그런 묶음이 없다: {str(bid)[:80]}")
    if delib_jobs._foreign({"user": b.get("owner")}, by):
        raise PermissionError(f"내가 시작한 묶음이 아니다: {bid} — 멈출 수 있는 것은 제 것뿐이다")
    if b.get("status") in _LIVE:
        _BATCHES[bid] = b
        b["status"] = "stopping"
        b["stopped_by"], b["stopped_at"] = str(by or ""), delib_jobs._now()
        for it in b["items"]:
            if it.get("state") == "pending":
                _terminal(it, "stopped", "묶음을 멈췄다 — 띄우지 않았다")
            elif it.get("state") == "running" and it.get("job_id") in delib_jobs._PENDING:
                # 엔진 줄에 서 있기만 한 잡 — 멈춘 뒤에 돌기 시작하지 않게 줄에서 뺀다(반대 검토 M6). 이미 도는 잡은 그대로 지켜본다.
                try:
                    delib_jobs.cancel(it["job_id"], by=b["owner"])
                except (delib_jobs.DelibFinishing, ValueError, PermissionError) as exc:
                    log.warning("[batch %s] 줄 선 잡 %s 를 빼지 못했다: %r", b["id"], it["job_id"], exc)
                    continue
                _attempt(it, it["job_id"], "stopped", "묶음을 멈췄다 — 엔진 줄에서 뺐다(시작하지 않았다)")
                _terminal(it, "stopped", "묶음을 멈췄다 — 엔진 줄에서 뺐다(시작하지 않았다)")
        _finish_if_done(b)
        _persist(b)
    return b


def can_read(b: dict, viewer: str, aff: str = "", admin: bool = False) -> bool:
    """잡과 같은 규칙(delib_jobs.can_read — 주인 · 같은 소속 · 관리자 · 신원 없는 운영)."""
    return delib_jobs.can_read({"user": b.get("owner"), "owner_aff": b.get("owner_aff")}, viewer, aff, admin)


def view(b: dict) -> dict:
    """진행 조회의 답 — 묶음 칸 · 항목마다 상태 · 잡 · 시도 · 믿음 · 좌석 유실. 도는 항목은 잡의 상태 · 단계 · 라운드 · 조용한 시간을 덧붙인다."""
    items = []
    for it in b["items"]:
        row = {k: it.get(k) for k in ("key", "state", "job", "job_id", "tries", "attempts", "trust", "seat_loss", "error", "last_error",
                                      "done_in") if it.get(k) not in (None, [], {})}
        row.setdefault("tries", int(it.get("tries") or 0))
        if it.get("state") == "running" and it.get("job_id"):
            j = delib_jobs.get(it["job_id"])
            if j is not None:
                s = delib_jobs.summary(j)
                row["live"] = {k: s.get(k) for k in ("status", "stage", "round", "total_rounds", "idle_s", "queue") if s.get(k) is not None}
        items.append(row)
    c = _counts(b)
    finished = b.get("status") in _FINISHED
    out = {"batch_id": b["id"], "name": b["name"], "status": b["status"], "finished": finished,
           "owner": delib_jobs._norm(b.get("owner")), "created_at": b.get("created_at"), "finished_at": b.get("finished_at"),
           "concurrency": b.get("concurrency"), "retries": b.get("retries"), "stall_s": b.get("stall_s"),
           "counts": c, "items": items}
    for k in ("previous", "notes", "stopped_by", "notice"):
        if b.get(k):
            out[k] = b[k]
    out["note"] = (b.get("notice") if finished else
                   f"도는 중 — 동시 {b.get('concurrency')}건씩 차례로 띄운다(남은 {c['pending']} · 도는 {c['running'] + c['starting']}). "
                   "끝나면 status 가 done(멈추면 stopped · 끝난 항목 없이 실패만 있으면 failed) · finished 가 참 · notice 가 붙는다 — 바깥으로 알리지 "
                   "않는다(포털은 끝난 묶음 창구를 읽어 알린다). 몇 분 간격으로 다시 본다.")
    return out


# ── 끝난 묶음 창구(HWAXPortal docs/request-1117 contracts §7) ────────────────────────────────────────────────────
FINISHED_LIMIT_DEFAULT, FINISHED_LIMIT_MAX = 100, 500


def finished_since(since: float, limit: int = FINISHED_LIMIT_DEFAULT) -> dict:
    """끝난 묶음 — finished_at 이 since 이상인 것을 (finished_at, batch_id) 차례로 limit 개까지. 같은 since 로 다시 물으면 같은 답이다(그 사이 새로 끝난
    묶음은 뒤에 붙는다). next_since 는 실은 마지막 묶음의 finished_at(없으면 since) — 경계의 묶음은 다음 부름에 다시 나온다(포털이 batch_id 로 한 번만
    알린다 — /insights/cards 와 같은 규칙). 보존 기한이 지나 지운 묶음은 없다. counts 의 stopped 에는 사람이 접은 잡(cancelled)도 센다.
    답 {batches: [{batch_id, owner, name, status, finished_at, counts{done, failed, stopped, skipped, not_trustworthy}, notice}], next_since}."""
    lim = max(1, min(int(limit), FINISHED_LIMIT_MAX))
    rows = sorted((sm for sm in _index().values()
                   if sm.get("status") in _FINISHED and isinstance(sm.get("finished_at"), (int, float)) and float(sm["finished_at"]) >= since
                   and not _expired(sm)),
                  key=lambda sm: (float(sm["finished_at"]), sm["id"]))[:lim]
    out = []
    for sm in rows:
        c = sm.get("counts") or {}
        out.append({"batch_id": sm["id"], "owner": sm["owner"], "name": sm["name"], "status": sm["status"],
                    "finished_at": float(sm["finished_at"]),
                    "counts": {"done": int(c.get("done") or 0), "failed": int(c.get("failed") or 0),
                               "stopped": int(c.get("stopped") or 0) + int(c.get("cancelled") or 0), "skipped": int(c.get("skipped") or 0),
                               "not_trustworthy": int(c.get("not_trustworthy") or 0)},
                    "notice": str(sm.get("notice") or "")})
    return {"batches": out, "next_since": out[-1]["finished_at"] if out else float(since)}


# ── 기동 · 틱 ────────────────────────────────────────────────────────────────────────────────────────────
def bind(app) -> int:
    """기동 — 원장의 할 일이 남은 묶음을 메모리에 올린다(재기동 정리 reap_orphans 뒤 — 끊긴 잡이 이미 interrupted 다). 올린 수."""
    global _APP  # noqa: PLW0603
    _APP = app
    global _INDEX, _LAST_PRUNE  # noqa: PLW0603
    _INDEX = None                           # 원장 폴더를 다시 읽는다(보존 기한이 지난 끝난 묶음은 여기서 지운다)
    _LAST_PRUNE = delib_jobs._now()
    n = 0
    for sm in list(_index().values()):
        if sm.get("status") in _LIVE and sm["id"] not in _BATCHES:
            b = _load(sm["id"])
            if b is not None:
                _BATCHES[b["id"]] = b
                n += 1
    if n:
        log.info("재기동 — 할 일이 남은 심의 묶음 %d개를 다시 먹인다", n)
    return n


def prune() -> int:
    """보존 기한(KEEP_S)이 지난 끝난 묶음 원장을 지운다 — 지운 수. 기동 때(색인을 다시 읽으며)와 하루마다(tick) 돈다."""
    global _LAST_PRUNE  # noqa: PLW0603
    _LAST_PRUNE = delib_jobs._now()
    n = 0
    idx = _index()
    for bid, sm in list(idx.items()):
        if _expired(sm):
            try:
                _path(bid).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning("[batch %s] 보존이 지난 원장을 못 지웠다: %r", bid, exc)
                continue
            idx.pop(bid, None)
            n += 1
    return n


def kick(b: dict):
    """첫 먹이기를 요청 밖(이 이벤트 루프의 따로 된 태스크)에서 돌린다 — 근거 참조 풀이가 길어도 부른 요청이 기다리지 않는다. 태스크를 돌려준다."""
    t = asyncio.ensure_future(feed(b))
    _KICKS.add(t)

    def done(task):
        _KICKS.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("[batch %s] 첫 먹이기 실패 — 주기 먹이기가 다시 본다: %r", b.get("id"), task.exception())
    t.add_done_callback(done)
    return t


async def tick() -> None:
    if delib_jobs._now() - _LAST_PRUNE >= PRUNE_EVERY_S:
        try:
            prune()
        except Exception:  # noqa: BLE001
            log.exception("묶음 보존 정리 실패 — 다음 틱에 다시 본다")
    for b in list(_BATCHES.values()):
        try:
            await feed(b)
        except Exception:  # noqa: BLE001 — 한 묶음의 실패가 다른 묶음을 멈추지 않는다
            log.exception("[batch %s] 먹이기 실패 — 다음 틱에 다시 본다", b.get("id"))
        if b.get("status") not in _LIVE:
            _BATCHES.pop(b["id"], None)


async def _loop() -> None:
    while True:
        await asyncio.sleep(TICK_S)
        await tick()


def start_ticker() -> None:
    """묶음을 TICK_S 마다 먹인다 — 기동 때(mcp_server.bind, 도는 이벤트 루프 안). 루프 밖에서 부르면 아무것도 안 한다(시험)."""
    global _TICKER  # noqa: PLW0603
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _TICKER is None or _TICKER.done():
        _TICKER = loop.create_task(_loop(), name="delib-batch-tick")


def stop_ticker() -> None:
    global _TICKER  # noqa: PLW0603
    if _TICKER is not None:
        _TICKER.cancel()
        _TICKER = None
