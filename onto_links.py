# 코어 어휘표 ontology 칸을 앱 줄마다 검증하고 (게이트웨이 백엔드 키, 원래 이름)을 이 박스의 노출 이름으로 푼다 — 챗 고정 몫 · 규칙 ⑧ · 입구 표지(HWAXPortal docs/ontology-first)
"""온톨로지 먼저(HWAXPortal docs/ontology-first PLAN §4 · OF-5 · OF-14 ① · ④).

  "ontology": {
    "apps":  {"<게이트웨이 백엔드 키>": {"tools": ["<원래 이름>", …], "reserve": true|false, "entry": "<원래 이름 — tools 안>"?}},
    "facts": {…}        ← 심의 '온톨로지 먼저 보기'(포털)가 읽는다. 에이전트 서버는 읽지 않는다.
  }

  · 열쇠는 (백엔드 키, 원래 이름)이다 — 노출 이름이 아니다. 노출 이름은 박스마다 다르다(허브 가족은 늘 접두, 겹침 접두는 그 박스에 붙은 앱에 따라).
    이 박스의 노출 이름은 게이트웨이 /tools-map 의 map(노출 이름 → 백엔드)과 orig(노출 이름 → 원래 이름, 다른 것만)으로 찾는다(exposed).
  · tools 의 차례가 챗 고정 몫의 차례다(앞부분 고정 — HWAXPortal docs/request-1029 W-3). reserve 가 참인 앱의 tools 가 고정 몫이다.
  · entry 는 그 앱에서 이름 · 코드를 키로 푸는 원래 이름이다. 하나라도 있으면 챗 규칙 ⑧ 한 줄과 그 도구 설명의 '온톨로지 입구' 표지(챗 로드만)가 선다.
    입구는 판정(exact · candidates · none)이나 후보 목록을 돌려주는 도구만이다 — 규칙 ⑧ 의 '후보면 묻는다' 를 지킬 수 있어야 한다(OF-5).
    추적 기본값에는 entry 가 없다 — 기본 동작은 글자 하나 바뀌지 않는다(사용자 결정 OF-14 ④). 입구는 박스 덧씌우기(core_vocab.local.json)로 켠다.
  · 검사는 앱 줄마다다 — 틀린 줄만 버리고 까닭을 /health core_vocab.ontology.dropped 에 싣는다(페르소나 연결 E5-4 와 같은 자세). 표 전체를 버리면
    덧씌우기의 오타 하나가 코어 규칙 전체를 기본 글로 돌린다. 허브 가족(정본 · 형제)의 entry 는 버린다(줄은 남긴다) — 정본의 입구는 규칙 ① 이 말하고,
    가족 도구 설명은 게이트웨이 소개 줄 '[…]' 로 시작해 표지가 말없이 빠진다.
  · 게이트웨이(gateway._onto_entries)도 같은 규칙으로 입구 쌍을 읽는다 — 포털 시험(test_core_vocab)이 두 쪽에 같은 줄들을 넣어 견준다.
이 모듈은 예외를 내지 않는다 — 코어 어휘표 읽기(app._core_vocab)가 이것을 부르고, 거기서 예외가 나면 코어 규칙 전체가 기본 글로 돈다.
"""
from __future__ import annotations

import re

_APP_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
_TOOL_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,120}$")
_ROW_FIELDS = frozenset({"tools", "reserve", "entry", "_doc"})
MARK = "온톨로지 입구"          # 입구 도구 설명의 라벨 표지 — '[StepForge · 온톨로지 입구] …'
MARK_TAIL = f" · {MARK}"


def _row_problem(key, row) -> str:
    """앱 줄 하나가 틀린 까닭(맞으면 빈 글)."""
    if not isinstance(key, str) or not _APP_RE.fullmatch(key):
        return "앱 키는 게이트웨이 백엔드 키 꼴이다(영문 소문자 · 숫자 · _.- 80자)"
    if not isinstance(row, dict):
        return "앱 줄은 {tools, reserve?, entry?} 객체다"
    extra = sorted(str(k) for k in row if k not in _ROW_FIELDS)
    if extra:
        return f"모르는 칸 {extra[:5]} — 받는 칸은 tools · reserve · entry 다"
    tools = row.get("tools")
    if not isinstance(tools, list) or not tools or not all(isinstance(t, str) and _TOOL_RE.fullmatch(t) for t in tools):
        return "tools 는 원래 이름(빈칸 없는 글)의 비지 않은 목록이다"
    if "reserve" in row and not isinstance(row["reserve"], bool):
        return "reserve 는 참 · 거짓이다"
    entry = row.get("entry")
    if entry is not None and (not isinstance(entry, str) or entry not in tools):
        return "entry 는 tools 안의 원래 이름 하나다"
    return ""


def validate(raw, *, family=frozenset()) -> dict:
    """ontology 칸 → {present, apps{키: {tools, reserve, entry}}(어휘표 차례), entries((키, 원래 이름) …), dropped[{app, why}]}. 칸이 없으면 present 거짓.
    family — 허브 가족(정본 + 형제)의 앱 키(그 줄의 entry 는 버린다)."""
    out: dict = {"present": False, "apps": {}, "entries": (), "dropped": []}
    if raw is None:
        return out
    if not isinstance(raw, dict) or not isinstance(raw.get("apps"), dict):
        out["dropped"].append({"app": "*", "why": "ontology 는 {apps: {…}, facts?} 객체다 — 칸 전체를 읽지 않는다"})
        return out
    out["present"] = True
    entries = []
    for key, row in raw["apps"].items():
        why = _row_problem(key, row)
        if why:
            out["dropped"].append({"app": str(key)[:80], "why": why})
            continue
        entry = row.get("entry")
        if entry is not None and key in family:
            out["dropped"].append({"app": key, "why": "허브 가족(정본 · 형제)의 entry 는 받지 않는다 — 정본의 입구는 규칙 ① 이다(줄의 tools 는 남긴다)"})
            entry = None
        out["apps"][key] = {"tools": tuple(dict.fromkeys(row["tools"])), "reserve": row.get("reserve") is True, "entry": entry}
        if entry is not None:
            entries.append((key, entry))
    out["entries"] = tuple(entries)
    return out


def exposed(app: str, tool: str, *, tmap: dict, orig, family) -> tuple[str | None, bool]:
    """(백엔드, 원래 이름) → (이 박스의 노출 이름, 도구 지도에 있나). 지도가 원래 이름을 그 앱 것으로 적었으면 맨 이름, 접두 꼴을 그 앱 것으로 적었으면 접두
    꼴이다. orig 가 있으면 그 이름의 원래 이름이 맞는지까지 본다(원래 이름이 마침 그 앱의 접두 꼴인 도구 — 노출 이름으로 짐작하면 틀린다). 지도에
    없으면 이름 규칙으로 짓는다 — 허브 가족은 접두, 나머지는 맨 이름(옛 고정 몫 _ONTO_CORE 를 적은 규칙과 같다 — 기본 표에서 고정 몫이 글자 그대로다).
    그 이름이 이 박스에서 다른 앱의 것이면 None — 남의 도구를 앞세우지 않는다."""
    pre = f"{app.replace('-', '')}_{tool}"
    if tmap:
        known = isinstance(orig, dict)
        for n in (tool, pre):
            if tmap.get(n) == app and (not known or orig.get(n, n) == tool):
                return n, True
        if known:
            hit = next((n for n, o in orig.items() if o == tool and tmap.get(n) == app), None)
            if hit:
                return hit, True
    conv = pre if app in family else tool
    if tmap and tmap.get(conv) not in (None, app):
        return None, False
    return conv, False


def reserve_names(v: dict, *, tmap: dict, orig, family) -> tuple[tuple, list]:
    """고정 몫 — reserve 인 앱의 tools 를 어휘표 차례대로 노출 이름으로(겹친 이름은 한 번). 둘째 값은 이 박스 지도에 없는 '<앱>:<원래 이름>'."""
    names: list = []
    missing: list = []
    for app, row in (v.get("apps") or {}).items():
        if not row.get("reserve"):
            continue
        for t in row["tools"]:
            n, here = exposed(app, t, tmap=tmap, orig=orig, family=family)
            if n and n not in names:
                names.append(n)
            if not here:
                missing.append(f"{app}:{t}")
    return tuple(names), missing


def is_entry(name: str, app: str, entries, orig) -> bool:
    """이 노출 이름(백엔드 app)이 입구인가 — 원래 이름으로 본다. orig 를 모르는 옛 게이트웨이면 맨 이름 · 그 앱의 접두 꼴만 견준다."""
    if not app or not entries or not name:
        return False
    if isinstance(orig, dict):
        return (app, orig.get(name, name)) in entries
    if (app, name) in entries:
        return True
    pre = app.replace("-", "") + "_"
    return name.startswith(pre) and (app, name[len(pre):]) in entries


def strip_mark(desc: str) -> str:
    """설명 앞 라벨의 입구 표지를 뗀다 — 관련도(어휘 · 의미 순위)는 표지와 상관없이 지금과 같게 센다. 표지는 모델이 읽는 글일 뿐 순위의 신호가
    아니다 — '온톨로지' 를 부른 질의가 입구 도구를 끌어올리면 '온톨로지가 있는 플랫폼으로 보낸다'(버린 안 OF-1 (나))가 된다."""
    if not desc or not desc.startswith("[") or MARK_TAIL not in desc[:200]:
        return desc
    end = desc.find("]")
    if end < 0:
        return desc
    return desc[:end].replace(MARK_TAIL, "") + desc[end:]
