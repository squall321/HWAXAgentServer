# 실사용 첫 발화 모으기 · 사람 표시 — 전문가 고르기 기준선 H 의 재료를 이름 · 메일 · 대화 id 없이 박스 파일에(기본 꺼짐 — HWAXPortal docs/request-1029 design-e5 E5-2 · 결정 D-E5-2)
"""모으는 것 — 넷이다(design-e5 2.2 H).
  persona     전문가를 지목한 대화의 첫 발화(그 대화에 앞선 사람 발화가 없다)
  thinking    띵킹 질문
  delib       사람이 좌석을 고른 심의의 화두(웹 /심의 · /시뮬심의 · /시험계획 의 delib_opts.personas, MCP deliberate_start 의 personas)
  expert_cmd  '/전문가' 질의(빗금 트리거 넷 — /전문가 · /에이전트 · /agents · /agent)

모으기는 박스마다 켠다 — SEAT_QUESTIONS_COLLECT=1(기본 꺼짐 · 사용자 허락이 전제다 — 도구 검색 질의 모으기와 같은 꼴, docs/followups-1017 결정 21).
꺼져 있으면 아무것도 적지 않고, 포털이 지난 60일을 밀어 넣는 창구(ingest)도 거절한다. 적는 것은 날짜(KST — 시각은 적지 않는다) · 출처 · 질문 글(메일 · 대화 id 꼴 ·
전화 · 그 사람 메일의 이름 조각을 지운 것) · 사람이 고른 전문가(지목 · 지정 좌석 — 화면에는 내지 않는다, 표시 뒤 맞대기에만)다. 누가 · 어느 대화인지는
적지 않는다. 날짜 파일은 SEAT_QUESTIONS_DAYS(기본 60)일 뒤 지워진다. 사람 표시(labels.jsonl)는 지우지 않는다 — 기준선 H 그 자체라 표시한 질문 글을
함께 둔다.

화면 쪽(view) — 순위 · 점수 · 지금 좌석 · 사람이 고른 전문가를 싣지 않는다(누설 규칙 L6). 8자 아래 · 홀로 서지 않는 발화('그럼 그건?') · 3글자 조각
자카드 0.8 이상의 겹침은 뺀 수만 알린다. 출처를 번갈아 늘어놓는다(출처마다 몫을 고르게 — 2.2). 다는 사람은 사람이 정한다 —
SEAT_QUESTIONS_LABELERS(쉼표, 비면 누구나)가 받을 표시자 이름이다(한 사람 190건 · 다른 사람 30건 겹쳐 — design-e5 2.4).
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from datetime import datetime

import seat_box

SOURCES = ("persona", "thinking", "delib", "expert_cmd")
NONE_KINDS = ("core_data", "out_of_pool")
VIA = ("tree", "search")
MIN_CHARS = 8
MAX_CHARS = 2000
_DAY_FILE = re.compile(r"^q-(\d{4}-\d{2}-\d{2})\.jsonl$")
_QID_RE = re.compile(r"^H-[0-9a-f]{12}$")
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}$")
_BY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,39}$")
_LABEL_FIELDS = ("qid", "by", "primary", "also", "none", "unsure", "via", "memo", "remove")
_ITEM_FIELDS = ("src", "text", "ts", "picked")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEXID = re.compile(r"\b[0-9a-fA-F]{16,}\b")          # 대화 id(uuid4 hex 32자) · 잡 id 꼴
_PHONE = re.compile(r"(?<!\d)(?:\+?82[- ]?)?0\d{1,2}[- ]?\d{3,4}[- ]?\d{4}(?!\d)")
# 홀로 서지 않는 발화의 머리 — 짧을 때만 뺀다('다시 말해 PCB 휨 …' 처럼 긴 글은 남는다)
_OPENERS = ("그럼", "그건", "그거", "그것", "그게", "이건", "이거", "이게", "저건", "저거", "아까", "위에", "위의", "방금", "그래서",
            "그러면", "그리고", "또 ", "다시", "이어서", "계속", "네 ", "응 ", "ㅇㅇ", "ok", "okay")
_LOCK = threading.Lock()
_STATE = {"day": None, "written": 0, "error": None}
_VIEW_CACHE: dict = {"key": None, "rows": None, "filtered": None}


def enabled() -> bool:
    return os.environ.get("SEAT_QUESTIONS_COLLECT", "0").strip() == "1"


def days() -> int:
    try:
        return max(1, int(os.environ.get("SEAT_QUESTIONS_DAYS", "60") or 60))
    except ValueError:
        return 60


def labelers() -> list[str]:
    return [x.strip() for x in os.environ.get("SEAT_QUESTIONS_LABELERS", "").split(",") if x.strip()]


def qdir():
    return seat_box.box_dir() / "questions"


def health() -> dict:
    return {"collect": enabled(), "days": days(), "written": _STATE["written"], "error": _STATE["error"],
            "labelers": len(labelers())}


# ── 지우기 · 모으기 ────────────────────────────────────────────────────────────
def strip(text: str, user: str = "") -> str:
    """질문 글에서 메일 · uuid · 긴 16진 id(대화 id 꼴) · 전화 · 그 사람 메일의 이름 조각을 지운다. 공백은 하나로, 앞 MAX_CHARS 자."""
    s = str(text or "")
    s = _EMAIL.sub("[메일]", s)
    s = _UUID.sub("[id]", s)
    s = _HEXID.sub("[id]", s)
    s = _PHONE.sub("[번호]", s)
    local = str(user or "").split("@", 1)[0].strip()
    for part in sorted({local, *re.split(r"[._+-]+", local)}, key=len, reverse=True):
        if len(part) >= 3:
            s = re.sub(rf"(?i)(?<![A-Za-z0-9]){re.escape(part)}(?![A-Za-z0-9])", "[이름]", s)
    return " ".join(s.split())[:MAX_CHARS]


def qid_of(text: str) -> str:
    return "H-" + seat_box.qhash(text)


def _day_of(ts: float):
    return datetime.fromtimestamp(ts, seat_box.KST).date()


def _write(rec: dict, day) -> None:
    d = qdir()
    with _LOCK:
        today = datetime.now(seat_box.KST).date()
        if _STATE["day"] != today:
            _STATE["day"] = today
            seat_box.private_dir(d)
            seat_box.drop_old(d, _DAY_FILE, days(), today)
        seat_box.append_jsonl(d / f"q-{day.isoformat()}.jsonl", rec)
        _STATE["written"] += 1


def note(src: str, text: str, *, user: str = "", picked=None, ts: float | None = None) -> bool:
    """발화 하나를 남긴다 — 꺼져 있거나 · 출처가 틀렸거나 · 8자 아래면 남기지 않는다. 실패는 삼킨다(챗을 막지 않는다)."""
    if not enabled() or src not in SOURCES:
        return False
    try:
        t = strip(text, user)
        if len(t) < MIN_CHARS:
            return False
        ts = float(ts) if ts else time.time()
        # 날짜만 둔다 — 시각은 포털 대화 저장소와 맞대면 그 대화 · 사람을 가리킨다
        rec = {"qid": qid_of(t), "d": _day_of(ts).isoformat(), "src": src, "text": t}
        keys = [k for k in (str(x or "").strip() for x in (picked or [])) if _KEY_RE.match(k)][:20]
        if keys:
            rec["picked"] = keys
        _write(rec, _day_of(ts))
        return True
    except Exception as exc:  # noqa: BLE001 — 모으기가 챗 · 심의를 막지 않는다
        _STATE["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return False


def ingest(items) -> tuple[int, dict]:
    """POST /seat-questions/ingest — 포털이 대화 저장소의 지난 날들을 밀어 넣는다. (상태, 본문). 꺼져 있으면 409.
    항목은 {src, text, ts?, picked?} 만 받는다 — 사람 · 대화를 가리키는 칸은 거절한다(조용히 버리지 않는다)."""
    if not enabled():
        return 409, {"error": "모으기가 꺼져 있다 — 박스의 SEAT_QUESTIONS_COLLECT=1 뒤 재기동(사용자 허락이 전제다)"}
    if not isinstance(items, list) or not 1 <= len(items) <= 1000:
        return 422, {"error": "items 는 1 ~ 1000 건의 목록이다"}
    have = {r.get("qid") for r in _rows_raw()}
    cut = datetime.now(seat_box.KST).date().toordinal() - days()
    added = dup = old = short = 0
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            return 422, {"error": f"items[{i}] 는 객체여야 한다"}
        bad = sorted(set(it) - set(_ITEM_FIELDS))
        if bad:
            return 422, {"error": f"items[{i}] 에 받지 않는 칸 {[str(x)[:40] for x in bad[:10]]} — 받는 칸은 {list(_ITEM_FIELDS)}"
                                  "(사람 · 대화를 가리키는 칸은 싣지 않는다)"}
        if it.get("src") not in SOURCES or not isinstance(it.get("text"), str):
            return 422, {"error": f"items[{i}] — src 는 {list(SOURCES)}, text 는 글이다"}
        ts = it.get("ts")
        if ts is not None and (not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts <= 0):
            return 422, {"error": f"items[{i}].ts 는 에포크 초다"}
        pk = it.get("picked")
        if pk is not None and not (isinstance(pk, list) and all(isinstance(x, str) for x in pk)):
            return 422, {"error": f"items[{i}].picked 는 전문가 키 목록이다"}
    for it in items:
        t = strip(it["text"])
        if len(t) < MIN_CHARS:
            short += 1
            continue
        if qid_of(t) in have:
            dup += 1
            continue
        ts = float(it.get("ts") or time.time())
        if _day_of(ts).toordinal() <= cut:
            old += 1
            continue
        if note(it["src"], t, picked=it.get("picked"), ts=ts):
            have.add(qid_of(t))
            added += 1
    return 200, {"added": added, "dup": dup, "old": old, "short": short}


# ── 읽기 ─────────────────────────────────────────────────────────────────────
def _files():
    d = qdir()
    try:
        return sorted(f for f in d.iterdir() if _DAY_FILE.match(f.name))
    except OSError:
        return []


def _rows_raw() -> list[dict]:
    cut = (datetime.now(seat_box.KST).date().toordinal() - days())
    out = []
    for f in _files():
        m = _DAY_FILE.match(f.name)
        try:
            if datetime.strptime(m.group(1), "%Y-%m-%d").date().toordinal() <= cut:
                continue
        except ValueError:
            continue
        out += seat_box.read_jsonl(f)
    return out


def _grams(s: str) -> set:
    t = re.sub(r"\s+", "", str(s or "").lower())
    return {t[i:i + 3] for i in range(max(0, len(t) - 2))} or ({t} if t else set())


def standalone(text: str) -> bool:
    s = str(text or "").strip()
    return len(s) >= MIN_CHARS and not (len(s) < 30 and s.lower().startswith(_OPENERS))


def _filtered() -> tuple[list[dict], dict]:
    """(후보, 뺀 수) — qid 겹침 · 8자 아래 · 홀로 서지 않음 · 3글자 조각 자카드 0.8 이상을 뺀다(먼저 온 것을 남긴다). 파일이 바뀔 때만 다시 센다."""
    files = _files()
    key = tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in files if f.exists())
    lab = qdir() / "labels.jsonl"
    key += ((lab.stat().st_mtime_ns if lab.exists() else None), days())
    if _VIEW_CACHE["key"] == key:
        return _VIEW_CACHE["rows"], _VIEW_CACHE["filtered"]
    rows = sorted(_rows_raw(), key=lambda r: (str(r.get("d") or ""), r.get("qid") or ""))
    # 표시된 질문은 날짜 파일이 지워진 뒤에도 후보로 남는다(표시를 다시 볼 수 있게) — 글은 표시 기록에 있다
    for lb in labels_all():
        if lb.get("text"):
            rows.append({"qid": lb["qid"], "d": lb.get("d"), "src": lb.get("src"), "text": lb["text"]})
    seen, kept, index = set(), [], {}
    filt = {"short": 0, "not_standalone": 0, "near_dup": 0}
    for r in rows:
        q = r.get("qid")
        if not q or q in seen or r.get("src") not in SOURCES:
            continue
        seen.add(q)
        t = str(r.get("text") or "")
        if len(t.strip()) < MIN_CHARS:
            filt["short"] += 1
            continue
        if not standalone(t):
            filt["not_standalone"] += 1
            continue
        g = _grams(t)
        shared: dict = {}
        for x in g:
            for j in index.get(x, ()):
                shared[j] = shared.get(j, 0) + 1
        if any(c / (len(g) + len(kept[j][1]) - c) >= 0.8 for j, c in shared.items()):
            filt["near_dup"] += 1
            continue
        for x in g:
            index.setdefault(x, []).append(len(kept))
        kept.append((r, g))
    out = [r for r, _g in kept]
    _VIEW_CACHE.update(key=key, rows=out, filtered=filt)
    return out, filt


def _balanced(rows: list[dict]) -> list[dict]:
    """출처를 번갈아 — 출처 안의 차례는 qid 의 해시(사람이 매기는 차례 · 시스템 점수와 무관하다)."""
    by = {s: sorted((r for r in rows if r["src"] == s), key=lambda r: hashlib.sha256(("e5:" + r["qid"]).encode()).hexdigest())
          for s in SOURCES}
    out, i = [], 0
    while any(i < len(v) for v in by.values()):
        for s in SOURCES:
            if i < len(by[s]):
                out.append(by[s][i])
        i += 1
    return out


def view(src: str | None = None, by: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    """GET /seat-questions — 표시 화면에 줄 후보. 순위 · 점수 · 지금 좌석 · 사람이 고른 전문가는 싣지 않는다(L6). by 를 주면 그 사람의 표시를
    mine 에, 남의 표시는 수(others)로만 — 겹쳐 다는 둘째 사람이 첫째의 답을 보지 않는다."""
    rows, filt = _filtered()
    eff = labels_effective()
    counts = {s: sum(1 for r in rows if r["src"] == s) for s in SOURCES}
    pool = _balanced([r for r in rows if not src or r["src"] == src])
    page = pool[max(0, offset):max(0, offset) + max(1, min(500, limit))]
    items = []
    for r in page:
        mine = eff.get((r["qid"], by)) if by else None
        others = sum(1 for (q, b) in eff if q == r["qid"] and b != by)
        items.append({"qid": r["qid"], "d": r.get("d"), "src": r["src"], "text": r["text"],
                      "mine": _label_public(mine) if mine else None, "others": others})
    return {"collect": enabled(), "days": days(), "total": len(pool), "counts": counts, "filtered": filt,
            "labeled": {"mine": sum(1 for (q, b) in eff if b == by) if by else None,
                        "any": len({q for (q, _b) in eff})},
            "labelers": labelers(), "items": items}


# ── 사람 표시 ─────────────────────────────────────────────────────────────────
def labels_all() -> list[dict]:
    return seat_box.read_jsonl(qdir() / "labels.jsonl")


def labels_effective() -> dict:
    """(qid, by) → 마지막 표시. 지움(remove)은 그 짝을 뺀다."""
    out: dict = {}
    for lb in labels_all():
        k = (lb.get("qid"), lb.get("by"))
        if lb.get("remove"):
            out.pop(k, None)
        else:
            out[k] = lb
    return out


def _label_public(lb: dict) -> dict:
    return {k: lb.get(k) for k in ("primary", "also", "none", "unsure", "via", "memo", "t")}


def check_label(body) -> tuple[dict | None, str]:
    if not isinstance(body, dict):
        return None, "본문은 JSON 객체여야 한다"
    bad = sorted(set(body) - set(_LABEL_FIELDS))
    if bad:
        return None, f"선언하지 않은 칸 {[str(x)[:40] for x in bad[:10]]} — 받는 칸은 {list(_LABEL_FIELDS)}"
    qid, by = body.get("qid"), body.get("by")
    if not (isinstance(qid, str) and _QID_RE.match(qid)):
        return None, "qid 는 H-<12자> 다"
    if not (isinstance(by, str) and _BY_RE.match(by)):
        return None, "by 는 표시자 이름(영문 · 숫자 · '._@-' 40자 이내)이다"
    if labelers() and by not in labelers():
        return None, f"표시자 {by!r} 는 이 박스의 SEAT_QUESTIONS_LABELERS 에 없다"
    if body.get("remove") is not None and not isinstance(body.get("remove"), bool):
        return None, "remove 는 true|false 다"
    if body.get("remove"):
        return {"qid": qid, "by": by, "remove": True}, ""
    prim, also = body.get("primary", []), body.get("also", [])
    none, unsure = body.get("none"), body.get("unsure", False)
    for name, v, lo, hi in (("primary", prim, 0, 3), ("also", also, 0, 5)):
        if not (isinstance(v, list) and lo <= len(v) <= hi and all(isinstance(x, str) and _KEY_RE.match(x) for x in v)):
            return None, f"{name} 는 전문가 키 {lo} ~ {hi}개다"
    if set(prim) & set(also) or len(set(prim)) != len(prim) or len(set(also)) != len(also):
        return None, "primary · also 는 겹치지 않는 키다"
    if none is not None and none not in NONE_KINDS:
        return None, f"none 은 {list(NONE_KINDS)} 가운데 하나거나 비운다"
    if not isinstance(unsure, bool):
        return None, "unsure 는 true|false 다"
    kinds = sum([bool(prim), none is not None, unsure])
    if kinds != 1:
        return None, "주 전문가(1 ~ 3) · 없음(core_data · out_of_pool) · 모르겠음 가운데 하나만 고른다"
    if also and not prim:
        return None, "같이 맡을 전문가는 주 전문가가 있을 때만 단다"
    via = body.get("via") or {}
    if not isinstance(via, dict) or any(k not in set(prim) | set(also) or v not in VIA for k, v in via.items()):
        return None, f"via 는 고른 키 → {list(VIA)} 다(나무에서 골랐나 · 찾아서 골랐나)"
    memo = body.get("memo", "")
    if not isinstance(memo, str) or len(memo) > 300:
        return None, "memo 는 300자 이내의 글이다"
    return {"qid": qid, "by": by, "primary": prim, "also": also, "none": none, "unsure": unsure, "via": via, "memo": memo}, ""


def label(body) -> tuple[int, dict]:
    """POST /seat-questions/label — 사람 표시 하나(같은 사람 · 같은 질문은 바꿔 쓴다, remove 는 지운다). 질문 글 · 출처 · 고른 전문가를 함께 둔다 —
    날짜 파일이 지워져도 기준선 H 가 남는다."""
    rec, why = check_label(body)
    if rec is None:
        return 422, {"error": why}
    src = None
    for r in _rows_raw():
        if r.get("qid") == rec["qid"]:
            src = r
    if src is None:
        for lb in labels_all():
            if lb.get("qid") == rec["qid"] and lb.get("text"):
                src = {"text": lb["text"], "src": lb.get("src"), "d": lb.get("d"), "picked": lb.get("picked")}
    if src is None:
        return 404, {"error": "그 질문이 박스에 없다(지난 날짜 파일이 지워졌거나 qid 가 틀렸다)"}
    full = {"t": int(time.time()), **rec}
    if not rec.get("remove"):
        full.update(text=src.get("text"), src=src.get("src"), d=src.get("d"))
        if src.get("picked"):
            full["picked"] = src["picked"]
    with _LOCK:
        seat_box.append_jsonl(qdir() / "labels.jsonl", full)
    return 200, {"ok": True, "qid": rec["qid"], "by": rec["by"], "removed": bool(rec.get("remove"))}


def export(by: str | None = None) -> list[dict]:
    """기준선 H — 표시된 질문마다 {qid, q, src, primary, also, none, picked, by, others}. by 를 주면 그 사람의 표시를, 아니면 먼저 단 사람의 것을
    머리로 쓴다(모르겠음은 뺀다). others 는 다른 표시자의 표시(일치도 κ 셈)다. 러너(tools/seat_eval.py)가 박스에서 읽는다."""
    eff = labels_effective()
    by_q: dict = {}
    for (q, b), lb in sorted(eff.items(), key=lambda kv: kv[1].get("t") or 0):
        by_q.setdefault(q, []).append(lb)
    out = []
    for q, lbs in by_q.items():
        head = next((x for x in lbs if x.get("by") == by), None) if by else lbs[0]
        if head is None or head.get("unsure"):
            continue
        out.append({"qid": q, "q": head.get("text") or "", "src": head.get("src"), "primary": head.get("primary") or [],
                    "also": head.get("also") or [], "none": head.get("none"), "picked": head.get("picked") or [],
                    "by": head.get("by"),
                    "others": [_label_public(x) | {"by": x.get("by")} for x in lbs if x is not head]})
    return out
