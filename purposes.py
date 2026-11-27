# 게이트웨이 목적 묶음 표(/tools-map purposes)를 읽어 질의 · 의장 틀로 목적을 고르고 목적 예산 묶음을 셈하는 순수 함수(HWAXPortal docs/mcp-modernize M3)
"""목적 → 묶음 → 기능 MCP — 쓰는 쪽(웹 챗 · 심의 좌석 · 위임 · 루틴)이 게이트웨이 정본 표 하나를 같은 규칙으로 읽는다.

정본은 게이트웨이 `tool_purposes.json`(+ 박스 덧씌우기)이고, 게이트웨이가 `/tools-map` 의 `purposes` 로 싣는다 — 목적마다 key · title · summary · kind ·
apps · entry{backend, tool, name} · live · steps[{backend, tool, name, why, confirm, write?}] · triggers · samples, 그리고 app_entries. name 은 이 박스의
노출 이름이고 이 박스에 없는 단계는 null 이다. 이 모듈은 망을 두드리지 않는다 — 부르는 쪽이 받아 둔 지도에서 준다.

고르는 규칙은 게이트웨이 search_tools 의 목적 층(gateway.py `_search_tools` — switches.purpose_boost)과 같다. 트리거 하나의 띄어 쓴 조각이 모두 빈칸 뺀
질의(소문자)에 들면 그 목적이 맞는다. 점수는 맞은 트리거 가운데 조각 글자 수 합이 가장 큰 값(MATCH_MIN 이상), 덮음은 맞은 조각(합집합)의 글자 수 /
빈칸 뺀 질의 글자 수다. 점수 내림차순(같으면 표의 차례)으로 세우고 덮음이 COVER_MIN 미만이면 버린다 — 도구 하나를 겨눈 긴 문장('SUS304 물성을 표로
정리하고 …')에서는 목적이 순위를 건드리지 않게(게이트웨이 weights.purpose_cover 와 같은 까닭).
"""
from __future__ import annotations

import re

MATCH_MIN = 2              # 게이트웨이 PURPOSE_MATCH_MIN — 맞았다고 보는 트리거 조각 글자 수 합의 바닥('단면' · '좌굴' 같은 두 글자 낱말까지)
COVER_MIN = 0.2            # 게이트웨이 search_tuning.json weights.purpose_cover 기본값
MAX_PURPOSES = 3           # 한 턴에 고르는 목적 수 상한(게이트웨이는 1위 + 더 볼 곳 셋)
_KEY_RE = re.compile(r"[a-z][a-z0-9-]{1,31}")
_WS = re.compile(r"\s+")


def mode(raw, allowed: tuple, default: str) -> str:
    """박스 손잡이 값 — 허용 낱말이면 그것, 비었거나 모르는 값이면 기본값. '0' · 'false' 는 off 로 읽는다(다른 손잡이와 같은 꼴)."""
    v = str(raw or "").strip().lower()
    if v in ("0", "false", "no"):
        v = "off"
    return v if v in allowed else default


def parse(raw) -> dict | None:
    """/tools-map 의 purposes → {fingerprint, items, apps}. 칸이 없거나 꼴이 아니면 None(옛 게이트웨이 · 목적을 끈 박스 — GATEWAY_PURPOSES=0).
    목적 하나의 꼴이 틀리면 그 목적만 버린다(게이트웨이가 이미 표 전체를 검증했다 — 여기서 표를 통째로 버리면 덧씌우기 한 줄이 쓰는 쪽을 다 끈다)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        return None
    items = []
    for it in raw["items"]:
        if not isinstance(it, dict) or not isinstance(it.get("key"), str) or not _KEY_RE.fullmatch(it["key"]):
            continue
        steps = []
        for st in it.get("steps") or ():
            if not isinstance(st, dict):
                continue
            nm = st.get("name")
            steps.append({"backend": str(st.get("backend") or ""), "tool": str(st.get("tool") or ""),
                          "name": nm if isinstance(nm, str) and nm else None,
                          "confirm": st.get("confirm") is True,
                          "write": st.get("write") if isinstance(st.get("write"), bool) else None})
        ent = it.get("entry") if isinstance(it.get("entry"), dict) else {}
        en = ent.get("name")
        trig = tuple(t for t in (tuple(w for w in tr.lower().split() if w)
                                 for tr in (it.get("triggers") or ()) if isinstance(tr, str)) if t)
        items.append({"key": it["key"], "title": str(it.get("title") or it["key"]), "kind": str(it.get("kind") or ""),
                      "trig": trig, "steps": steps, "entry": en if isinstance(en, str) and en else None})
    apps = {}
    for bk, a in (raw.get("app_entries") or {}).items() if isinstance(raw.get("app_entries"), dict) else ():
        if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"]:
            apps[str(bk)] = a["name"]
    return {"fingerprint": raw.get("fingerprint") if isinstance(raw.get("fingerprint"), str) else None,
            "items": items, "apps": apps}


def table_health(table: dict | None) -> dict:
    """/health 의 표 칸 — 받았나 · 목적 수 · 이 박스에서 단계가 하나라도 있는 목적 수 · 지문. 이름 · 질의는 싣지 않는다."""
    if not table:
        return {"present": False}
    return {"present": True, "count": len(table["items"]),
            "live": sum(1 for p in table["items"] if any(s["name"] for s in p["steps"])),
            "fingerprint": table.get("fingerprint")}


def _flat(text: str) -> str:
    return _WS.sub("", (text or "").strip().lower())


def _purpose_view(p: dict, avail) -> tuple[list, str | None]:
    """(이 호출자에게 보이는 단계의 노출 이름 — 표의 차례, 입구). 입구가 없거나 안 보이면 보이는 첫 단계가 입구다(게이트웨이 _purpose_live 와 같다)."""
    names = [s["name"] for s in p["steps"] if s["name"] and (avail is None or s["name"] in avail)]
    entry = p["entry"] if p["entry"] in names else (names[0] if names else None)
    return names, entry


def match(text: str, table: dict | None, avail=None, *, cover_min: float = COVER_MIN, limit: int = MAX_PURPOSES) -> list[dict]:
    """질의 하나에 맞는 목적 — [{key, title, score, cover, names, entry}] (점수 내림차순, 같으면 표의 차례). avail(노출 이름 셋)을 주면 단계가 하나라도
    그 안에 있는 목적만 본다(게이트웨이 _purposes_for 의 시야). None 이면 이 박스에 있는 단계(name 이 있는 것)로 본다."""
    q_flat = _flat(text)
    if not table or not q_flat:
        return []
    hits = []
    for i, p in enumerate(table["items"]):
        names, entry = _purpose_view(p, avail)
        if not names:
            continue
        stems, best = set(), 0
        for parts in p["trig"]:
            if all(w in q_flat for w in parts):
                stems.update(parts)
                best = max(best, sum(len(w) for w in parts))
        if best >= MATCH_MIN:
            hits.append((-best, i, p, names, entry, sum(len(w) for w in stems) / max(1, len(q_flat))))
    hits.sort(key=lambda h: (h[0], h[1]))
    return [{"key": p["key"], "title": p["title"], "score": -nb, "cover": round(cov, 3), "names": names, "entry": entry}
            for nb, _i, p, names, entry, cov in hits if cov >= cover_min][:max(0, limit)]


def match_first(texts, table: dict | None, avail=None, *, limit: int = MAX_PURPOSES) -> tuple[list[dict], int]:
    """(맞은 목적, 몇 번째 글에서 맞았나 — 없으면 -1). 글을 차례로 보고 처음 맞은 글의 목적을 쓴다 — 챗은 이번 발화를 먼저, 못 맞히면 앞 사람 발화를 본다
    ('그럼 다음은?' 같은 뒤이은 발화는 트리거가 없다). 글을 이어 붙여 한 번에 보지 않는다 — 덮음(맞은 조각 / 질의 길이)이 앞 대화 길이만큼 묽어진다."""
    for i, t in enumerate(texts or ()):
        got = match(t, table, avail, limit=limit)
        if got:
            return got, i
    return [], -1


def by_keys(keys, table: dict | None, avail=None) -> list[dict]:
    """키로 고른 목적(의장 틀의 기본 목적) — match 와 같은 꼴(점수 0 · 덮음 0). 표에 없거나 보이는 단계가 없는 키는 건너뛴다."""
    if not table:
        return []
    want = list(dict.fromkeys(keys or ()))
    by = {p["key"]: p for p in table["items"]}
    out = []
    for k in want:
        p = by.get(k)
        if p is None:
            continue
        names, entry = _purpose_view(p, avail)
        if names:
            out.append({"key": k, "title": p["title"], "score": 0, "cover": 0.0, "names": names, "entry": entry})
    return out


def entries(hits) -> list[str]:
    """고른 목적의 입구 — 목적 차례, 겹침 없이."""
    return list(dict.fromkeys(h["entry"] for h in hits or () if h.get("entry")))


def lead(hits) -> list[str]:
    """먼저 묶을 이름 — 고른 목적들의 입구를 먼저(예산이 빠듯해도 입구가 남게), 이어 목적 차례대로 그 단계들. 겹침 없이."""
    out = entries(hits)
    for h in hits or ():
        out += h["names"]
    return list(dict.fromkeys(out))


def fit(tools, budget: int, cost, keep=()) -> tuple[list, int]:
    """(남긴 도구, 추정 토큰 합) — 앞에서부터 담고 넘치는 것은 건너뛴다(keep 의 이름은 예산과 상관없이 남긴다). app._fit_schema 와 같은 셈이고 로그 ·
    /health 절단 기록(_TOOL_CUT)을 건드리지 않는다 — 그림자 셈이 쓴다."""
    keep = set(keep or ())
    total, kept = 0, []
    for t in tools:
        c = cost(t)
        if kept and total + c > budget and getattr(t, "name", "") not in keep:
            continue
        kept.append(t)
        total += c
    return kept, total


def compare(now: list, new: list, hits) -> dict:
    """지금 묶음과 목적 묶음 — 수 · 겹침 · 더해진 수 · 빠진 수 · 고른 목적의 입구와 단계가 각 묶음에 몇 개 들었나(이름 목록은 싣지 않는다 — 수만)."""
    a, b = set(now), set(new)
    ents = entries(hits)
    steps = list(dict.fromkeys(n for h in hits or () for n in h["names"]))
    return {"now": len(now), "purpose": len(new), "overlap": len(a & b), "added": len(b - a), "removed": len(a - b),
            "entries": len(ents), "entries_now": sum(1 for n in ents if n in a), "entries_purpose": sum(1 for n in ents if n in b),
            "steps": len(steps), "steps_now": sum(1 for n in steps if n in a), "steps_purpose": sum(1 for n in steps if n in b)}


def brief(hits) -> list[dict]:
    """기록용 목적 칸 — 키 · 덮음 · 점수(이름 목록은 싣지 않는다)."""
    return [{"key": h["key"], "cover": h["cover"], "score": h["score"], **({"src": h["src"]} if h.get("src") else {})} for h in hits or ()]


class Tally:
    """그림자 셈의 합 — 기동 뒤 수 · 목적이 맞은 수 · 토큰 합 · 실패 · 마지막 한 건(위임 · 루틴이 같이 쓴다). 목적 키와 수만 쥔다."""

    def __init__(self) -> None:
        self.runs = self.matched = self.tokens_now = self.tokens_purpose = self.errors = 0
        self.error: str | None = None
        self.last: dict | None = None

    def add(self, rec: dict) -> None:
        self.runs += 1
        self.matched += int(bool(rec.get("purposes")))
        self.tokens_now += int(rec.get("tokens_now") or 0)
        self.tokens_purpose += int(rec.get("tokens_purpose") or 0)
        self.last = rec

    def fail(self, exc: BaseException) -> None:
        self.errors += 1
        self.error = f"{type(exc).__name__}: {exc}"[:200]

    def health(self, mode: str) -> dict:
        n = self.runs
        return {"mode": mode, "runs": n, "matched": self.matched,
                "avg_tokens_now": round(self.tokens_now / n) if n else None,
                "avg_tokens_purpose": round(self.tokens_purpose / n) if n else None,
                "errors": self.errors, "error": self.error, "last": self.last}


def line(rec: dict) -> str:
    """위임 · 루틴 그림자 로그 한 줄의 몸 — 목적 · 지금 수 · 토큰 → 목적 예산 수 · 토큰 · 입구."""
    ptxt = " · ".join(f"{p['key']}({p['cover']:.2f})" for p in rec.get("purposes") or ()) or "없음"
    return (f"목적 {ptxt} — 지금 {rec['now']}개 {rec['tokens_now']:,}토큰 → 목적 예산 {rec['purpose']}개 {rec['tokens_purpose']:,}토큰"
            f"(예산 {rec['budget_purpose']:,} · 입구 {rec['entries_purpose']}/{rec['entries']} · 단계 {rec['steps_purpose']}/{rec['steps']})")
