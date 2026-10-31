# 코어 어휘표 personas 칸을 읽고 검증해 /health 에 싣고, 켠 쪽(E5-5 · E5-7)이 골랐을 도구 · 좌석을 그림자로만 적는다 — 고르기는 바꾸지 않는다(HWAXPortal docs/request-1029 design-e5 E5-4)
"""연결 표는 도메인 ↔ 코어 타입 ↔ 도구 영역 · 앱의 얇은 줄이다(design-e5 3.1, 결정 D-E5-3 (가)).

  "personas": {
    "domains":    {"<도메인 코드>": {"core_types": [...], "areas": [...], "apps": [...]}},
    "experts":    {"<전문가 키>": {"apps": [...]}},
    "data_types": [...],
    "no_tools":   [...]
  }

검증 — 틀린 줄은 그 줄만 버리고 까닭을 /health core_vocab.personas.dropped 에 싣는다(C-11 과 같은 꼴). 도메인은 조직도 라벨 정본
(포털 frontend/src/components/chat/orgTaxonomy.json 의 domain_label)에, 타입은 어휘표 types 의 값에, 영역 · 앱은 게이트웨이 /tools-map 에 있어야
한다. 대조할 표가 없으면(형제 리포가 없다 · 도구 지도를 아직 못 받았다) 그 대조만 건너뛰고 unchecked 에 적는다. 운영자 도메인(he)은 넣지 않는다 —
운영자의 앱은 포털 infra/personas/he-team.json 이 맡는다.

그림자 — 챗 지목 전문가의 우선 도구(U1)와 심의 첫 좌석 · 띵킹 소집의 도메인 덮음(U2 · U3), 코어 데이터 질문 표지(U4 ①의 타입 쪽)를 박스 자리
(seat_box.box_dir()/shadow)에 날짜 파일로 적는다. 질문 글 · 사람은 적지 않는다(qh — 맞대기 꼴의 sha256 앞 12자만). 끄기 PERSONA_SHADOW_LOG=0.
적기는 어떤 실패도 삼킨다 — 고르기에 닿지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import seat_box

ROOT = Path(__file__).resolve().parent
ORG_TAXONOMY_PATH = ROOT.parent / "HWAXPortal" / "frontend" / "src" / "components" / "chat" / "orgTaxonomy.json"
SHADOW_DAYS = 30
_FIELDS = ("domains", "experts", "data_types", "no_tools")
_LINE_FIELDS = ("core_types", "areas", "apps")
_DOM_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,80}$")      # 앱 키 _gateway 처럼 밑줄로 시작하는 것도 있다
_SHADOW_FILE = re.compile(r"^s-(\d{4}-\d{2}-\d{2})\.jsonl$")
# 어느 좌석에나 필요한 입구 — 연결 후보 안에서 먼저 세운다(deliberation._trim_free_tools 의 entry 와 같은 낱말)
_ENTRY = ("search", "list", "find", "guide", "describe", "get_guide", "discover")
_ORG = {"mtime": None, "domains": None}
_SHADOW = {"day": None, "written": 0, "error": None}
_LOCK = threading.Lock()


def shadow_on() -> bool:
    return os.environ.get("PERSONA_SHADOW_LOG", "1").strip() != "0"


def _dom(key: str) -> str:
    k = str(key or "")
    return k.split("-", 1)[0] if "-" in k else k


def _is_op_dom(d: str) -> bool:
    from deliberation import OPERATOR_DOMAINS  # noqa: PLC0415 — 늦은 로드(이 모듈은 엔진보다 먼저 읽힐 수 있다)
    return d in OPERATOR_DOMAINS


def org_mtime():
    try:
        return ORG_TAXONOMY_PATH.stat().st_mtime_ns
    except OSError:
        return None


def org_domains() -> frozenset | None:
    """조직도 라벨 정본의 도메인 코드 — 형제 포털 리포 파일이 없거나 꼴이 틀리면 None(대조를 건너뛴다)."""
    mt = org_mtime()
    if mt is None:
        return None
    if _ORG["mtime"] != mt:
        try:
            d = json.loads(ORG_TAXONOMY_PATH.read_text(encoding="utf-8"))
            labels = d.get("domain_label") if isinstance(d, dict) else None
            _ORG.update(mtime=mt, domains=frozenset(labels) if isinstance(labels, dict) and labels else None)
        except (OSError, ValueError):
            _ORG.update(mtime=mt, domains=None)
    return _ORG["domains"]


# ── 검증 ─────────────────────────────────────────────────────────────────────
def validate(raw, *, types_map: dict, domains_known=None, areas_known=None, apps_known=None) -> dict:
    """personas 칸 → 검증한 연결. 틀린 줄은 그 줄만 버린다(목록 칸은 값 하나가 한 줄이다). raw 가 없으면 present 거짓."""
    out = {"present": False, "domains": {}, "experts": {}, "data_types": [], "no_tools": [], "dropped": [],
           "unchecked": [], "missing": [], "types_map": {str(k): str(v) for k, v in (types_map or {}).items()}}
    if raw is None:
        return out
    drop = out["dropped"]
    if not isinstance(raw, dict):
        drop.append({"at": "personas", "why": f"객체가 아니다({type(raw).__name__})"})
        return out
    out["present"] = True
    types = set(out["types_map"].values())
    for name, known in (("domains", domains_known), ("areas", areas_known), ("apps", apps_known)):
        if not known:
            out["unchecked"].append(name)
    for k in sorted(set(raw) - set(_FIELDS) - {"_doc"}):
        drop.append({"at": f"personas.{k}", "why": f"모르는 칸 — 받는 칸은 {list(_FIELDS)}"})

    def dom_why(d) -> str:
        if not (isinstance(d, str) and _DOM_RE.match(d)):
            return "도메인 코드 꼴이 아니다(소문자로 시작하는 영문 · 숫자 · 밑줄)"
        if _is_op_dom(d):
            return "운영자 도메인은 넣지 않는다 — 운영자의 앱은 he-team.json 이 맡는다"
        if domains_known and d not in domains_known:
            return "조직도 라벨(orgTaxonomy domain_label)에 없는 도메인이다"
        return ""

    def list_why(vals, what: str, known, ok_re=_NAME_RE) -> str:
        if not isinstance(vals, list):
            return f"{what} 는 목록이어야 한다"
        for x in vals:
            if not (isinstance(x, str) and ok_re.match(x)):
                return f"{what} 의 {str(x)[:40]!r} — 이름 꼴이 아니다"
            if known is not None and x not in known:
                return f"{what} 의 {x!r} — {'어휘표 types 의 값' if what == 'core_types' else '게이트웨이 도구 지도'}에 없다"
        return ""

    doms = raw.get("domains", {})
    if not isinstance(doms, dict):
        drop.append({"at": "personas.domains", "why": "객체여야 한다"})
        doms = {}
    for d, line in doms.items():
        at = f"personas.domains.{str(d)[:40]}"
        why = dom_why(d)
        if not why and not isinstance(line, dict):
            why = "줄은 {core_types, areas, apps} 객체여야 한다"
        if not why:
            bad = sorted(set(line) - set(_LINE_FIELDS))
            if bad:
                why = f"모르는 칸 {bad[:5]} — 받는 칸은 {list(_LINE_FIELDS)}"
        if not why:
            why = (list_why(line.get("core_types", []), "core_types", types, re.compile(r"^[a-z][a-z0-9_]{0,31}$"))
                   or list_why(line.get("areas", []), "areas", areas_known or None)
                   or list_why(line.get("apps", []), "apps", apps_known or None))
        if why:
            drop.append({"at": at, "why": why})
            continue
        out["domains"][d] = {f: list(dict.fromkeys(line.get(f, []))) for f in _LINE_FIELDS}

    exps = raw.get("experts", {})
    if not isinstance(exps, dict):
        drop.append({"at": "personas.experts", "why": "객체여야 한다"})
        exps = {}
    for k, line in exps.items():
        at = f"personas.experts.{str(k)[:60]}"
        why = ""
        if not (isinstance(k, str) and _KEY_RE.match(k)):
            why = "전문가 키 꼴이 아니다"
        elif _is_op_dom(_dom(k)):
            why = "운영자는 넣지 않는다 — 운영자의 앱은 he-team.json 이 맡는다"
        elif not isinstance(line, dict) or sorted(set(line) - {"apps"}):
            why = "줄은 {apps} 객체여야 한다"
        else:
            why = list_why(line.get("apps", []), "apps", apps_known or None)
        if why:
            drop.append({"at": at, "why": why})
            continue
        out["experts"][k] = {"apps": list(dict.fromkeys(line.get("apps", [])))}

    dts = raw.get("data_types", [])
    if not isinstance(dts, list):
        drop.append({"at": "personas.data_types", "why": "목록이어야 한다"})
        dts = []
    for x in dts:
        if isinstance(x, str) and x in types:
            if x not in out["data_types"]:
                out["data_types"].append(x)
        else:
            drop.append({"at": f"personas.data_types.{str(x)[:40]}", "why": "어휘표 types 의 값이 아니다"})

    nts = raw.get("no_tools", [])
    if not isinstance(nts, list):
        drop.append({"at": "personas.no_tools", "why": "목록이어야 한다"})
        nts = []
    for d in nts:
        why = dom_why(d) or ("domains 에도 줄이 있다 — 하나만 둔다" if d in out["domains"] else "")
        if why:
            drop.append({"at": f"personas.no_tools.{str(d)[:40]}", "why": why})
        elif d not in out["no_tools"]:
            out["no_tools"].append(d)

    if domains_known:
        out["missing"] = sorted(d for d in domains_known
                                if not _is_op_dom(d) and d not in out["domains"] and d not in out["no_tools"])
    return out


def health(v: dict | None) -> dict:
    """/health core_vocab.personas — 수 · 버린 줄(까닭) · 대조 못 한 표 · 줄이 없는 조직도 도메인 · 그림자 기록."""
    if not v or not v.get("present"):
        out = {"present": False}
        if v and v.get("dropped"):
            out["dropped"] = v["dropped"][:20]
        return out
    return {"present": True, "domains": len(v["domains"]), "experts": len(v["experts"]),
            "data_types": len(v["data_types"]), "no_tools": len(v["no_tools"]),
            "dropped_n": len(v["dropped"]), "dropped": v["dropped"][:20], "unchecked": list(v["unchecked"]),
            "missing_n": len(v["missing"]), "missing": v["missing"][:30],
            "shadow": {"on": shadow_on(), "written": _SHADOW["written"], "error": _SHADOW["error"]}}


# ── 켠 쪽이 고를 것 ────────────────────────────────────────────────────────────
def link_of(v: dict, key: str) -> tuple[str, list, list]:
    """(연결 갈래, 앱, 영역) — expert(전문가 덮어쓰기 ∪ 도메인 줄) · domain · no_tools · none(줄 없음 — D-E5-5 (가): 우선 도구 없음)."""
    d = _dom(key)
    if d in v["no_tools"]:
        return "no_tools", [], []
    line = v["domains"].get(d) or {}
    ex = v["experts"].get(key) or {}
    apps = list(dict.fromkeys([*ex.get("apps", []), *line.get("apps", [])]))
    areas = list(line.get("areas", []))
    if ex:
        return "expert", apps, areas
    if line:
        return "domain", apps, areas
    return "none", [], []


def link_tools(v: dict, *, key: str, names, question: str, n: int, tool_app, tool_area) -> dict:
    """U1 — 연결된 앱 · 영역의 도구를 후보로, 그 안에서 입구 도구를 먼저 · 질문 낱말이 이름에 맞는 수로 세운다(같은 점수는 이름 차례)."""
    kind, apps, areas = link_of(v, key)
    if kind in ("none", "no_tools"):
        return {"link": kind, "top": [], "n_cand": 0}
    apps_s, areas_s = set(apps), set(areas)
    cand = [x for x in names if (tool_app(x) or "") in apps_s or (tool_area(x) or "") in areas_s]
    words = {w.lower() for w in re.findall(r"[A-Za-z]{3,}|[가-힣]{2,}", question or "")}

    def score(name: str) -> tuple:
        nm = name.lower()
        s = 40 if any(nm.startswith(e) or e in nm for e in _ENTRY) else 0
        s += sum(3 for w in words if w in nm)
        return (-s, name)
    cand.sort(key=score)
    return {"link": kind, "top": cand[:n], "n_cand": len(cand)}


def detect_types(question: str, types_map: dict) -> list[str]:
    """U2 — 질문에 든 어휘표 types 낱말의 타입. 띄어쓰기는 보지 않는다('관리 기준' = '관리기준')."""
    q = re.sub(r"\s+", "", str(question or "").lower())
    got = []
    for w, slug in (types_map or {}).items():
        ww = re.sub(r"\s+", "", str(w).lower())
        if ww and ww in q and slug not in got:
            got.append(slug)
    return got


def seat_cover(v: dict, *, question: str, seats: list, want: int) -> dict:
    """U2 · U3 — 질문의 코어 타입 → 그 타입을 맡는 도메인 D(q), 앉은 좌석이 D(q) 를 덮었나(덮기 좌석을 더할 도메인), 모자란 좌석 수,
    타입이 다 data_types 인가(U4 ① 의 타입 쪽 — 분석 낱말 판정은 E5-6 몫)."""
    types = detect_types(question, v.get("types_map") or {})
    dq = sorted(d for d, line in v["domains"].items() if set(line.get("core_types") or ()) & set(types))
    seated = sorted({_dom(k) for k in seats})
    return {"types": types, "dq": dq, "seated": seated, "cover": [d for d in dq if d not in seated],
            "short": max(0, int(want or 0) - len(seats)),
            "data_only": bool(types) and set(types) <= set(v.get("data_types") or ())}


# ── 그림자 기록 ───────────────────────────────────────────────────────────────
def _write(rec: dict) -> None:
    if not shadow_on():
        return
    try:
        now = datetime.now(seat_box.KST)
        d = seat_box.box_dir() / "shadow"
        with _LOCK:
            if _SHADOW["day"] != now.date():
                _SHADOW["day"] = now.date()
                seat_box.private_dir(d)
                seat_box.drop_old(d, _SHADOW_FILE, SHADOW_DAYS, now.date())
            seat_box.append_jsonl(d / f"s-{now.date().isoformat()}.jsonl", {"t": int(time.time()), **rec})
            _SHADOW["written"] += 1
    except Exception as exc:  # noqa: BLE001 — 그림자가 고르기를 막지 않는다
        _SHADOW["error"] = f"{type(exc).__name__}: {exc}"[:200]


def shadow_tools(v: dict, *, site: str, key: str, question: str, qkey: str, names, prefer_now, tool_app, tool_area) -> dict:
    """챗 지목 전문가 — 지금 고른 우선 도구(prefer_now)와 연결이 골랐을 것을 나란히 적는다. 돌려준 값은 시험만 본다."""
    n = len(prefer_now or []) or 12
    got = link_tools(v, key=key, names=names, question=question, n=n, tool_app=tool_app, tool_area=tool_area)
    now = list(prefer_now or [])
    rec = {"site": site, "key": key, "dom": _dom(key), "link": got["link"], "now": now, "link_top": got["top"],
           "same": len(set(now) & set(got["top"])), "n_cand": got["n_cand"], "qh": seat_box.qhash(qkey)}
    _write(rec)
    return rec


def shadow_seats(v: dict, *, site: str, question: str, seats: list, want: int) -> dict:
    rec = {"site": site, **seat_cover(v, question=question, seats=seats, want=want), "n": len(seats),
           "qh": seat_box.qhash(question)}
    _write(rec)
    return rec
