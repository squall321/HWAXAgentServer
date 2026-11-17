# 결정문의 수 · ID · 부품 번호 · 넷 이름을 낱말로 뽑아 자리와 함께 근거 항목에 맞대고, 쪽 나눈 결정문이 빈틈 없이 이어지는지 본다(DL-04)
"""HWAXPortal docs/request-1117 (DL-04) — deliberate_verify(job_id, scope, forbidden?).

**판정하지 않는다 — 드러낸다.** 낱말(token)마다 결정문의 자리(pos — 글자 번호)와 그 값이 든 근거 항목 key(없으면 'no source')를 준다. 같은 수가 다른
판 쌍의 비교값일 수 있다('53 → 53') — 그 뜻을 가리는 것은 사람이다. 이 도구는 근거에 그 낱말이 있는지만 말한다.

**낱말 종류(KINDS — 정규식은 PATTERNS 한 곳).** 겹치는 자리는 앞 종류가 이긴다(net → id → part → num). 경계는 ASCII 영숫자 · 밑줄이다 — 한글 조사가
붙어도('R12의') 낱말이다.
  · net  — 넷 이름: 대문자 · 숫자를 밑줄로 이은 것(VDD_1V8 꼴) · 전원 · 접지(GND · VCC · VDD · VSS · VBAT · VBUS · VSYS 로 시작)
  · id   — ID 꼴 코드: 글자로 시작해 - 또는 . 로 이은 영숫자 마디에 숫자가 든 것(ABC-12-003 꼴) · 대문자 넷 이상 뒤에 숫자(ABCD2 꼴)
  · part — 부품 번호: 참조 지정자(대문자 1~3 + 숫자 1~4 + 글자 하나까지 — R12 · U301 꼴) · 제조사 품번(대문자 · 숫자 8자 이상, 글자 둘 · 숫자 셋 이상)
  · num  — 수: 작은 정수까지 전부(천 단위 쉼표 · 소수). 단위 글자가 붙어도 수다('3.5mm').
뺀 것 — 근거 표지([e:N|KEY]) · 줄 머리의 순번(1. · 2) — 값이 아니다) · 엔진이 결정문 끝에 붙이는 안내(수치 대조 경고 · 보고서 · 위키 안내 —
tail_from 부터)는 보지 않는다.

**근거.** 잡을 열 때 근거 항목마다 낱말 색인을 원장(evidence_index)에 남긴다(index — 본문도 낱말 글자도 남기지 않고 낱말 지문(token_hash)만 ·
엔진이 고르는 본문과 번호 [e:N] 그대로 · 항목마다 **엔진이 좌석에 싣는 글**(_fit_ev — 넘치면 낱장 표지 경계에서 가운데를 덜어 앞뒤를 남긴다, 항목 천장
_EVID_ITEM_MAX 안)). 근거가 크면(INDEX_SYNC_CHARS 넘음) 잡 태스크가 스레드에서 짓는다.
화두(question)와 사람 의견(human_note)도 출처로 센다. 좌석 자유 조회 · 지식카드 · 사전 조회 결과는 색인에 없다 — 그 수는 결정문 수치 대조(numcheck,
그대로 둔다)가 본다. 색인 이전에 돈 잡은 출처를 모른다고 말한다.

**쪽 이음.** deliberate_result 의 full · summary 를 쪽(mcp_pages.PAGE_CHARS)마다 받아 결정문 조각을 이으면 원문과 글자까지 같은지 — 빈틈(gap) ·
겹침(overlap)을 자리로 준다.
"""
from __future__ import annotations

import hashlib
import re

import deliberation as _engine   # 근거 본문 고르기(_ev_bodies) · 건수 상한 — 엔진이 [e:N] 을 매기는 대로 센다

KINDS = ("num", "id", "part", "net")
NO_SOURCE = "no source"
_A = r"(?<![A-Za-z0-9_])"
_Z = r"(?![A-Za-z0-9_])"
PATTERNS = (
    ("net", re.compile(_A + r"(?:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|(?:[ADP]?GND|V(?:CC|DD|SS|BAT|BUS|SYS))[A-Z0-9]*)" + _Z)),
    # 숫자가 든 것만 id 다 — 그 조건은 맞은 뒤에 본다(_DIGIT). 앞보기로 적으면(?=[A-Za-z0-9.-]*\d) 자리마다 남은 줄기를 다시 훑어 '-' · '.' 이
    # 긴 글에서 제곱으로 느려졌다(4만 자 4.4초 — 반대 검토 H1). 이 꼴은 줄기 하나를 한 번만 지난다.
    ("id", re.compile(_A + r"(?:[A-Za-z][A-Za-z0-9]*(?:[-.][A-Za-z0-9]+)+|[A-Z]{4,}\d+[A-Z0-9]*)" + _Z)),
    ("part", re.compile(_A + r"(?:[A-Z]{1,3}\d{1,4}[A-Z]?|(?=(?:[A-Z0-9]*[A-Z]){2})(?=(?:[A-Z0-9]*\d){3})[A-Z0-9]{8,})" + _Z)),
    ("num", re.compile(r"(?<![A-Za-z0-9_.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?!\d|\.\d)")),
)
_DIGIT = re.compile(r"\d")
_MASK = re.compile(r"\[e:\d+(?:\|[A-Za-z0-9_.-]*)?\]|^[ \t]*(?:#+[ \t]*)?\d{1,3}[.)](?=[ \t])", re.M)
# 엔진이 결정문 끝에 붙이는 안내의 머리 — 처음 나온 자리부터는 의장이 쓴 글이 아니다(deliberation.py 의 수치 대조 경고 · 보고서 · 위키 안내).
TAIL_MARKS = ("\n\n> ⚠ 다음 수치는 심의에 제시된 근거에서 확인되지 않았습니다", "\n\n📄 Report Archive", "\n\n⚠ Report Archive",
              "\n\n⚠ 의장이 결정문을 내지 못해 MX 백서", "\n\n⚠ 신원 없는 호출이라 MX 백서")
INDEX_ITEM_MAX = 5000            # 항목 하나의 낱말 수 상한(종류 합) — 넘으면 cut(그 항목의 'no source' 는 틀릴 수 있다)
INDEX_TOTAL_MAX = 50000          # 잡 하나의 낱말 수 상한
# 항목 하나에서 훑는 글자 수의 위 — 엔진이 항목 하나에 싣는 천장(_EVID_ITEM_MAX). 훑는 글은 엔진이 싣는 그 글이다(_fit_ev — 앞만 자르지 않는다)
SCAN_ITEM_MAX = _engine._EVID_ITEM_MAX
# 이 글자 수(근거 전체에서 훑을 몫의 합) 안이면 잡을 열 때 그 자리에서 색인을 짓는다. 넘으면 잡 태스크가 이벤트 루프 밖(스레드)에서 짓는다
# (delib_jobs — 큰 근거 하나가 서버 전체를 멈추지 않게, 반대 검토 H1).
INDEX_SYNC_CHARS = 200_000
HASH_HEX = 16                    # 색인에 남기는 낱말 지문의 길이(sha256 앞 16자) — 낱말 글자 그대로는 원장에 남기지 않는다(반대 검토 L7)
FORBIDDEN_MAX, FORBIDDEN_LEN = 200, 200


def _norm(kind: str, raw: str) -> str:
    return raw.replace(",", "") if kind == "num" else raw


def tokens(text: str, kinds=KINDS) -> list[tuple[str, str, int]]:
    """글 → [(종류, 값, 자리)] 를 자리 차례로. 겹치면 앞 종류가 이긴다(PATTERNS 차례). kinds 밖의 종류도 자리는 차지한다(다른 종류로 잘못 읽지 않게)."""
    text = text or ""
    taken = bytearray(len(text))           # 차지한 글자 — 겹침 판정을 낱말 길이만큼만 본다
    for m in _MASK.finditer(text):
        taken[m.start():m.end()] = b"\x01" * (m.end() - m.start())
    out = []
    for kind, pat in PATTERNS:
        for m in pat.finditer(text):
            a, b = m.start(), m.end()
            if kind == "id" and not _DIGIT.search(m.group(0)):
                continue                   # 숫자 없는 점 · 하이픈 이음(e.g · risk-review)은 id 가 아니다 — 자리를 차지하지 않는다
            if not any(taken[a:b]):
                taken[a:b] = b"\x01" * (b - a)
                if kind in kinds:
                    out.append((kind, _norm(kind, m.group(0)), a))
    return sorted(out, key=lambda t: t[2])


def token_hash(kind: str, value: str) -> str:
    """낱말 지문 — sha256('종류\\0값') 앞 HASH_HEX 자. 색인은 이것만 남기고(낱말 글자는 남기지 않는다), 대조할 때 결정문의 낱말도 같은 식으로 잰다."""
    return hashlib.sha256(f"{kind}\0{value}".encode("utf-8")).hexdigest()[:HASH_HEX]


def _bodies(evidence) -> list:
    """엔진처럼 본문 있는 항목만 [e:N] 차례로(건수 상한까지) — [(항목, 본문)]."""
    out = []
    for it in evidence or []:
        b = _engine._ev_bodies(it) if isinstance(it, dict) else []
        if not b:
            continue
        if len(out) >= _engine._EVID_ITEMS:
            break
        out.append((it, b[0][1]))
    return out


def scan_chars(evidence) -> int:
    """색인이 훑을 글자 수의 합(항목마다 SCAN_ITEM_MAX 까지) — INDEX_SYNC_CHARS 와 견줘 그 자리에서 지을지 가른다."""
    return sum(min(len(body), SCAN_ITEM_MAX) for _it, body in _bodies(evidence))


def index(evidence) -> list[dict]:
    """근거 목록 → 항목마다 낱말 색인 [{n, key, source, chars, t{종류: [낱말 지문]}, cut?}] — 엔진처럼 본문 있는 항목만 [e:N] 차례로. 낱말 글자는
    남기지 않는다(token_hash). 항목 하나는 엔진이 좌석에 싣는 글을 훑는다(_fit_ev — 그 글이 줄었으면 engine_cut · 2차 검토 2: 앞 SCAN_ITEM_MAX 자만
    훑던 때는 낱장 문서의 결론(뒤쪽 — 엔진은 실었다)이 'no source' 였다). cut 은 낱말 수 상한에 걸려 색인이 덜 담겼다는 표지다."""
    out, total = [], 0
    for it, body in _bodies(evidence):
        seen: dict[str, dict] = {k: {} for k in KINDS}
        sent, cut_info = _engine._fit_ev(body)
        for kind, val, _pos in tokens(sent[:SCAN_ITEM_MAX + 400]):     # 줄였다는 꼬리 표식 몫만큼 — 그래도 위가 있다(선형)
            seen[kind][token_hash(kind, val)] = None
        t = {k: list(v) for k, v in seen.items() if v}
        n = sum(len(v) for v in t.values())
        room = max(0, min(INDEX_ITEM_MAX, INDEX_TOTAL_MAX - total))
        cut = n > room
        if n > room:
            left, kept = room, {}
            for k, v in t.items():
                kept[k], left = v[:left], max(0, left - len(v))
            t = {k: v for k, v in kept.items() if v}
        total += min(n, room)
        key = it.get("key") if isinstance(it.get("key"), str) and _engine._EVID_KEY_RE.fullmatch(it.get("key") or "") else ""
        out.append({"n": len(out) + 1, "key": key, "source": str(it.get("source") or "")[:120], "chars": len(body), "t": t,
                    **({"cut": True} if cut else {}), **({"engine_cut": True} if cut_info else {})})
    return out


def _label(row: dict) -> str:
    return row.get("key") or f"e:{row['n']}"


def tail_from(text: str) -> int | None:
    hits = [i for i in (text.find(m) for m in TAIL_MARKS) if i >= 0]
    return min(hits) if hits else None


def _forbidden(text: str, values) -> list[dict]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > FORBIDDEN_MAX:
        raise ValueError(f"forbidden 은 글 목록이다({FORBIDDEN_MAX}개까지)")
    out = []
    for v in values:
        if not isinstance(v, str) or not v.strip() or len(v) > FORBIDDEN_LEN:
            raise ValueError(f"forbidden 의 값은 1~{FORBIDDEN_LEN}자 글이다 — 받은 값: {str(v)[:40]!r}")
        v = v.strip()
        # 낱말 경계로 찾는다 — '53' 이 '1532' 에 걸리지 않게(ASCII 영숫자 · 밑줄 · 점이 앞뒤에 붙으면 다른 낱말이다)
        pat = re.compile(r"(?<![A-Za-z0-9_.])" + re.escape(v) + r"(?![A-Za-z0-9_]|\.\d)")
        pos = [m.start() for m in pat.finditer(text)]
        out.append({"value": v, "count": len(pos), "pos": pos})
    return out


def parse_scope(scope) -> list[str]:
    kinds = [s.strip() for s in str(scope or "").split(",") if s.strip()]
    bad = [k for k in kinds if k not in KINDS]
    if bad or not kinds:
        raise ValueError(f"scope 는 {','.join(KINDS)} 가운데 쉼표로 고른 것이다 — 받은 값: {str(scope)[:60]!r}")
    return list(dict.fromkeys(kinds))


def verify(job: dict, *, scope="num,id,part,net", forbidden=None, pages: dict | None = None) -> dict:
    """deliberate_verify 의 답. pages 는 쪽 이음 점검의 결과(page_join — mcp_server 가 같은 쪽 나누기로 잰다)."""
    import delib_decision  # noqa: PLC0415 — 결정문 글을 같은 함수로 읽는다
    kinds = parse_scope(scope)
    text = delib_decision.decision_text(job)
    out = {"job_id": job.get("id"), "status": job.get("status"), "scope": kinds}
    if not text:
        st = job.get("status")
        out["note"] = (f"아직 끝나지 않았다(status {st}) — 결정문이 없다" if st in ("queued", "running") else
                       f"원장에 결정문이 없다(status {st})")
        out["tokens"], out["totals"] = [], {}
        return out
    tf = tail_from(text)
    body = text if tf is None else text[:tf]
    idx = job.get("evidence_index")
    pseudo = [("question", str(job.get("question") or ""))]
    hn = (job.get("opts") or {}).get("human_note")
    if isinstance(hn, str) and hn.strip():
        pseudo.append(("human_note", hn))
    pseudo_sets = [(name, {token_hash(k, v) for k, v, _p in tokens(src)}) for name, src in pseudo]
    idx_sets = [(_label(e), {h for vs in (e.get("t") or {}).values() for h in vs}) for e in (idx or []) if isinstance(e, dict)]
    rows: dict[tuple[str, str], dict] = {}
    for kind, val, pos in tokens(body, kinds):
        r = rows.get((kind, val))
        if r is None:
            h = token_hash(kind, val)
            src = [name for name, s in idx_sets + pseudo_sets if h in s]
            r = rows[(kind, val)] = {"kind": kind, "value": val, "pos": [], "src": src or NO_SOURCE}
        r["pos"].append(pos)
    toks = sorted(rows.values(), key=lambda r: r["pos"][0])
    totals = {}
    for k in kinds:
        mine = [r for r in toks if r["kind"] == k]
        bad = [r for r in mine if r["src"] == NO_SOURCE]
        totals[k] = {"tokens": sum(len(r["pos"]) for r in mine), "unique": len(mine), "no_source": len(bad),
                     "no_source_hits": sum(len(r["pos"]) for r in bad)}
    out.update(text_chars=len(text), checked_chars=len(body), tail_from=tf, totals=totals,
               index={"present": idx is not None, "items": len(idx or []), "cut": any(e.get("cut") for e in (idx or [])),
                      "pseudo_sources": [name for name, _s in pseudo]})
    if forbidden is not None:
        out["forbidden"] = _forbidden(body, forbidden)
    if pages is not None:
        out["pages"] = pages
    out["note"] = ("판정하지 않는다 — 낱말마다 결정문의 자리(pos)와 그 값이 든 근거 항목 key(없으면 'no source')를 준다. 'no source' 는 근거 목록 · "
                   "화두 · 사람 의견에 그 낱말이 없다는 뜻이다(좌석 자유 조회 · 지식카드의 수는 결정문 수치 대조 numcheck 가 본다). 같은 수가 다른 판 "
                   "쌍의 비교값일 수 있다 — 자리를 열어 사람이 읽는다."
                   + ("" if idx is not None else " 이 잡은 근거 색인 이전에 돌았거나 근거가 없다 — 근거 항목 출처는 모른다(화두 · 사람 의견만 맞댔다).")
                   + (" 색인이 상한에서 잘린 항목이 있다(index.cut) — 그 항목의 'no source' 는 틀릴 수 있다." if out["index"]["cut"] else ""))
    out["tokens"] = toks
    return out


def page_join(out: dict, paged) -> dict:
    """쪽 이음 점검 — paged(part) 가 돌려주는 쪽마다 decision 조각을 모아 잇는다. {parts, ok, text_chars, joined_chars, gaps, overlaps}."""
    text = out.get("decision") or ""
    first = paged(1)
    parts = int((first.get("page") or {}).get("parts") or 1)
    joined, at, gaps, overlaps = [], 0, [], []
    for p in range(1, parts + 1):
        page = first if p == 1 else paged(p)
        if "decision" not in page:
            continue
        piece = next((x for x in (page.get("page") or {}).get("pieces") or [] if x.get("path") == "decision"), None)
        frm, to = (piece["from"], piece["to"]) if piece else (0, len(page["decision"]))
        if frm > at:
            gaps.append({"part": p, "from": at, "to": frm})
        elif frm < at:
            overlaps.append({"part": p, "from": frm, "to": min(at, to)})
        joined.append(page["decision"])
        at = max(at, to)
    j = "".join(joined)
    return {"parts": parts, "ok": j == text and not gaps and not overlaps and at == len(text), "text_chars": len(text),
            "joined_chars": len(j), "gaps": gaps, "overlaps": overlaps}
