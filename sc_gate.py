# 자기검증 관문 — 검토자가 낸 바꿀 곳(쌍)을 이번 턴 도구 결과 원문과 다시 대조해 넣음 · 짚음 · 버림을 정하는 순수 모듈(HWAXPortal docs/request-1030 HA-27)
"""모델이 아니라 코드가 대조한다 — 10-30 재시험에서 검토자가 맞는 초안을 틀리게 고친 넷(OT-04 34 → 13 · OT-08 다른 항목의 부서 ·
OT-09 '20' 을 '근거 없음' 으로 · DW-05 직전 기간 값을 지움)의 꼴을 막는다. 검토자는 접기 · 자르기를 지난 사본을 봤고, 바꿀 곳을 초안에
넣기 전에 근거와 다시 대조하는 걸음이 없었다.

  · 수 뽑기(num_tokens) — 답 글의 수를 열쇠 · 값 · 반올림 폭 · % · 맥락(num · date · id)으로. 주소 · 그림 주소 · 인용 표지 · 줄머리 번호 ·
    UUID · 확인 카드 번호(cf-)는 수로 보지 않는다.
  · 원문 색인(RawIndex) — 이번 턴 도구 결과 원문(접기 · 자르기 전)과 모델 사본. JSON 의 수 값 · 배열 길이는 강한 수, 글 칸 속 수(날짜 ·
    번호 조각)는 약한 수, 글 값은 원문 값 목록(다른 항목의 값으로 바꾸는 고침 — OT-08).
  · 쌍 판정(gate_patches) — 원문에 있는 수를 지우는 쌍은 버리고, 원문에도 원문 값의 합 · 차 · 비율 · 곱에도 없는 새 수를 쓰는 쌍과 원문 값을
    빼는 쌍은 짚는다.

app 을 import 하지 않는다. 근거 블록 · 심의 결정문이 함께 쓰는 evidence.py 는 건드리지 않는다(그 둘의 판정을 바꾸지 않는다). 설계는 HWAXPortal
docs/request-1030/design-ha27.md 6 · 7절, UUID · 카드 번호 가림은 design-integration.md C-13 이다.
"""
from __future__ import annotations

import json
import math
import re
from array import array
from bisect import bisect_left, bisect_right
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import NamedTuple

NUMS_MAX = 500_000      # 색인에 담는 수(강한 것 · 약한 것 합) — 넘으면 partial(meta · /health 에 남는다)
VALUES_MAX = 50_000     # 원문 값 목록
UNITS_MAX = 200_000     # 원문의 날짜 · 시각 · 숫자가 든 식별자(통째 글) — 넘으면 partial
ANCHORS_MAX = 64        # 셈의 기준 값
DERIVE_MAX = 40         # 한 번의 검토에서 셈을 해 보는 수 — 넘으면 더한 수는 '셈으로 안 나온다'(짚음 쪽), 지운 수는 '나온다'(버림 쪽)

# ── 수 뽑기 ──────────────────────────────────────────────────────────────────────────────────────────────────
# 앞이 영문 · 숫자 · 밑줄 · 점이면 수가 아니다 — 모델 코드(SM-S928B · Al6061-T6) · 버전(v1.2.3) · IP 의 꼬리가 빠진다. 앞이 한글이면 수다
# (모델34개 → 34 — evidence._NUM_TOK_RE 는 \w 뒤를 버려 이 꼴을 놓친다). 천 단위 쉼표는 셋씩일 때만, 문장 끝 점은 수에 붙지 않는다.
_NUM = re.compile(r"(?<![A-Za-z0-9_.])([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(\.[0-9]+)?(?![0-9])(?!\.[0-9])")
# 수로 보지 않는 시스템 글(C-13) — UUID 의 첫 묶음이 숫자로 시작하면 그 수가 작은 정수로 샜다. 꼴은 app 의 _UUID_RE(HA-30) · _CF_ID_RE(HA-29)와
# 같다 — tests/test_sc_gate.py 가 둘을 같은 보기로 묶는다.
UUID_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f])")
CF_ID_RE = re.compile(r"\bcf-[0-9a-f]{12}\b")
_MASK_ID = re.compile(UUID_RE.pattern + "|" + CF_ID_RE.pattern)
# 답 글에서만 가리는 것 — 주소 · 마크다운 링크와 그림 주소 · 인용 표지(조회 결과 2) · 줄머리 목록 번호. 원문 색인은 가리지 않는다(UUID · cf- 만).
# 인용 표지는 인용 꼴일 때만이다 — '결과 #N' 이거나, 뒤에 ')' · ']' · '에 따르면' · '에서' · '참고' · '참조' 가 올 때. 종전엔 뒤에 세는 말 · % 가
# 오지 않으면 다 가려 '해석 결과 312 MPa' · '측정 결과 85 dB' · '조회 결과 34가지' 의 수가 관문에서 사라졌다 — 그 수를 지우거나 바꾸는 쌍도,
# 지어낸 새 수를 더하는 쌍도 넣음이 됐다(반대 검토 2차). '조회 결과 34건' 처럼 개수인 것은 인용이 아니다(가리면 OT-04 꼴의 34 → 13 이 비켜 간다).
_MASK_TEXT = re.compile(r"https?://\S+|\]\([^)\s]*\)"
                        r"|(?:조회\s*)?결과\s*#[0-9]+(?![0-9.,])"
                        r"|(?:조회\s*)?결과\s*[0-9]+(?![0-9.,])(?=\s*(?:[)\]]|에\s*따르면|에서\b|참고|참조))"
                        r"|^[ \t]*[0-9]{1,2}[.)](?=\s)", re.M)
_DATE = re.compile(r"[0-9]{4}[-./][0-9]{1,2}[-./][0-9]{1,2}|[0-9]{4}\s*년\s*[0-9]{1,2}\s*월(?:\s*[0-9]{1,2}\s*일)?"
                   r"|[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?")
# 식별자는 낱말 머리에서만 시작한다 — 원문 색인도 이 정규식으로 훑으므로 긴 영숫자 글(해시 · 덤프)에서 자리마다 다시 시작하면 제곱 시간이 된다.
_IDENT = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+")
_PCT = ("%포인트", "%p", "%", "％", "퍼센트")
# 배수 — 수 바로 뒤의 천 · 만 · 억은 배수다. 뒤에 조사(으로 · 에 · 을 · 의 …)가 붙어도 그렇다('1.2억으로' = 1.2억). 배수가 아닌 것은 '만큼'
# (3만큼 = 3 정도) 하나다 — 종전엔 뒤가 한글이면 세는 말 · 어림 꼬리만 받아 '34만에' · '10만을' · '5천만의' 가 34 · 10 · 5 가 됐고, 원문
# 120000000 과 맞지 않아 그 수를 지우는 쌍이 넣음이 됐다(반대 검토 2차). 천만 · 백만 같은 겹낱말을 먼저 본다.
_MULTS = (("천만", 10 ** 7), ("천억", 10 ** 11), ("백만", 10 ** 6), ("백억", 10 ** 10), ("십만", 10 ** 5), ("십억", 10 ** 9),
          ("천", 10 ** 3), ("만", 10 ** 4), ("억", 10 ** 8))
_MULT_FIRST = frozenset(w[0] for w, _m in _MULTS)
_APPROX_AFTER = ("여", "가량", "정도", "남짓", "쯤")
_APPROX_BEFORE = re.compile(r"(?:(?<![가-힣])(?:약|대략|거의)|[~∼])\s?$")
_RANGE_SEP = re.compile(r"\s*[~∼\-–]\s*")
_HALF = Decimal("0.5")


class Num(NamedTuple):
    """글 속 수 하나 — text 는 쓴 그대로(배수 · % 꼬리까지), key 는 같은 수를 가르는 열쇠(배수를 곱한 십진, % 면 끝에 %), value 는 그 값
    (부호 없이), tol 은 반올림 폭, ctx 는 num · date · id."""
    text: str
    key: str
    value: float
    tol: float
    pct: bool
    ctx: str

    @property
    def small(self) -> bool:
        """작은 정수 — 0 ~ 99 는 큰 원문 어디에나 있어(날짜 · 번호 · 개수) 강한 수 · 그 줄의 셈으로만 본다(6.3)."""
        return self.ctx == "num" and not self.pct and "." not in self.text and self.value < 100


def _blank(m) -> str:
    return " " * (m.end() - m.start())


def _mult_at(s: str, i: int) -> tuple[int, int]:
    """s[i:] 가 배수 낱말(공백 하나까지 건너뛴다)이면 (배수, 낱말 뒤 자리), 아니면 (1, i). '만큼' 은 배수가 아니다."""
    j = i + 1 if i < len(s) and s[i] in " \t" else i
    if j >= len(s) or s[j] not in _MULT_FIRST:
        return 1, i
    for w, m in _MULTS:
        if s.startswith(w, j):
            if w.endswith("만") and s.startswith("큼", j + len(w)):      # 3만큼 · 3천만큼 — '만큼' 앞까지만 배수
                if len(w) == 1:
                    return 1, i
                continue
            return m, j + len(w)
    return 1, i


def _pct_at(s: str, i: int) -> tuple[bool, int]:
    j = i + 1 if i < len(s) and s[i] in " \t" else i
    for w in _PCT:
        if s.startswith(w, j):
            return True, j + len(w)
    return False, i


def _approx(s: str, start: int, end: int) -> bool:
    """어림 표지 — 앞에 약 · 대략 · 거의 · ~(범위의 물결은 아니다), 바로 뒤에 여 · 가량 · 정도 · 남짓 · 쯤."""
    m = _APPROX_BEFORE.search(s[max(0, start - 5):start])
    if m:
        if m.group(0).strip() not in ("~", "∼"):
            return True
        k = start - len(m.group(0)) - 1          # 물결 앞 글자 — 수 · % 면 범위(5~10%)다
        while k >= 0 and s[k] in " \t":
            k -= 1
        if k < 0 or not (s[k].isdigit() or s[k] in "%％"):
            return True
    j = end + 1 if end < len(s) and s[end] in " \t" else end
    return s.startswith(_APPROX_AFTER, j)


def _key(d: Decimal, pct: bool) -> str:
    t = format(d, "f")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return (t or "0") + ("%" if pct else "")


def _in(pos: int, spans: list) -> bool:
    return any(a <= pos < b for a, b in spans)


def _masked(raw: str) -> str:
    return _MASK_TEXT.sub(_blank, _MASK_ID.sub(_blank, raw))        # 같은 길이의 공백으로 — 자리를 지킨다


def _idents(s: str) -> list:
    return [m.span() for m in _IDENT.finditer(s)] if re.search(r"[A-Za-z]", s) else []


def num_tokens(text) -> list[Num]:
    """답 글의 수 — 가림(주소 · 그림 주소 · 인용 표지 · 줄머리 번호 · UUID · cf-)을 지나 쓴 차례대로. 범위 'a ~ b%' 의 a 도 % 다."""
    return _scan(text)[0]


def _scan(text) -> tuple[list[Num], list[tuple[int, int]], str]:
    """(수, 수마다 가린 글에서의 자리, 가린 글) — num_tokens 의 몸통. 쌍 판정이 단위 · 수의 경계를 볼 때 자리를 쓴다."""
    raw = str(text or "")
    if not raw:
        return [], [], ""
    s = _masked(raw)
    dates = [m.span() for m in _DATE.finditer(s)]
    idents = _idents(s)
    toks: list[Num] = []
    spans: list[tuple[int, int]] = []
    for m in _NUM.finditer(s):
        ip, fp = m.group(1), m.group(2) or ""
        try:
            d = Decimal(ip.replace(",", "") + fp)
        except InvalidOperation:
            continue
        mult, j = _mult_at(s, m.end())
        pct, k = _pct_at(s, j)
        tol = _HALF * Decimal(10) ** -(len(fp) - 1 if fp else 0) * mult
        if not fp and _approx(s, m.start(), k):
            digits = ip.replace(",", "")
            zeros = len(digits) - len(digits.rstrip("0"))
            if 0 < zeros < len(digits):
                tol = _HALF * Decimal(10) ** zeros * mult      # 약 1,800 → ±50
        dm = d * mult
        ctx = "date" if _in(m.start(), dates) else "id" if _in(m.start(), idents) else "num"
        toks.append(Num(raw[m.start():k], _key(dm, pct), float(dm), float(tol), pct, ctx))
        spans.append((m.start(), k))
    for i in range(1, len(toks)):
        if toks[i].pct and not toks[i - 1].pct and _RANGE_SEP.fullmatch(s[spans[i - 1][1]:spans[i][0]]):
            toks[i - 1] = toks[i - 1]._replace(pct=True, key=toks[i - 1].key + "%")
    return toks, spans, s


# 수 바로 뒤의 단위(공백 하나까지) — 영문 · 기호 단위만(MPa · kPa · mm · dB · ℃). 한글 세는 말(개 · 건)은 단위로 보지 않는다. 쌍 판정이 '같은 수의
# 단위만 바꾼 고침'(312.5 MPa → 312.5 kPa)을 그 수를 바꾼 것으로 센다 — 수가 그대로라 넣음이 됐다(반대 검토 2차).
_UNIT = re.compile(r"[ \t]?([A-Za-zµμΩ℃℉°][A-Za-z0-9µμΩ℃℉°/²³]*)")


def _unit_at(s: str, end: int) -> str:
    m = _UNIT.match(s, end)
    return m.group(1).lower() if m else ""


def _date_unit(t: str) -> str:
    """날짜 · 시각 글 하나를 견줄 꼴로 — '2026-10-3' · '2026.10.03' · '2026년 10월 3일' → 'd:2026-10-03', '2026년 10월' → 'd:2026-10',
    '9:05' → 't:09:05'."""
    ns = [int(x) for x in re.findall(r"[0-9]+", t)]
    if ":" in t:
        return "t:" + ":".join(f"{n:02d}" for n in ns)
    if len(ns) >= 3:
        return f"d:{ns[0]:04d}-{ns[1]:02d}-{ns[2]:02d}"
    return f"d:{ns[0]:04d}-{ns[1]:02d}" if len(ns) == 2 else ""


def unit_tokens(text) -> list[tuple[str, str]]:
    """답 글의 날짜 · 시각 · 숫자가 든 식별자 — (견줄 꼴, 쓴 그대로) 쓴 차례대로. 가림은 num_tokens 와 같다. 이 안의 수는 수가 아니라 통째 글로
    원문과 견준다(반대 검토 2차 — 날짜 조각 03 이 같은 날짜의 13 − 10 이나 다른 날짜의 07 로 '셈' 되어 정당한 날짜 고침이 늘 버려졌다)."""
    raw = str(text or "")
    if not raw:
        return []
    s = _masked(raw)
    dates = [m.span() for m in _DATE.finditer(s)]
    got = [(a, _date_unit(s[a:b]), raw[a:b]) for a, b in dates]
    got += [(a, "i:" + s[a:b].lower(), raw[a:b]) for a, b in _idents(s) if re.search(r"[0-9]", s[a:b]) and not _in(a, dates)]
    return [(u, t) for _p, u, t in sorted(got) if u]


def _raw_units(s: str):
    """원문 글의 날짜 · 시각 · 숫자가 든 식별자(견줄 꼴) — 날짜는 연월도, 초까지 쓴 시각은 시분도 함께 낸다('2026년 10월' · '09:30' 과 맞게)."""
    if "-" in s:
        s = _MASK_ID.sub(_blank, s)
    for m in _DATE.finditer(s):
        u = _date_unit(m.group(0))
        if u:
            yield u
            if u.startswith("d:") and len(u) == 12:
                yield u[:9]
            elif u.startswith("t:") and len(u) == 10:
                yield u[:7]
    for a, b in _idents(s):
        if re.search(r"[0-9]", s[a:b]):
            yield "i:" + s[a:b].lower()


def _as_num(x) -> Num | None:
    """Num · 글 · 수를 Num 하나로 — 글이면 첫 수, 수면 쓴 자릿수대로의 반올림 폭."""
    if isinstance(x, Num):
        return x
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, float):
        x = format(Decimal(repr(x)), "f")
    toks = num_tokens(str(x))
    return toks[0] if toks else None


def _raw_values(s: str):
    """원문 글 속 수의 값 — 가림은 UUID · cf- 만(주소 · 번호도 원문의 사실이다), 배수는 답 글과 같게 곱한다(1.2만 = 12000)."""
    if "-" in s:
        s = _MASK_ID.sub(_blank, s)
    for m in _NUM.finditer(s):
        try:
            d = Decimal(m.group(1).replace(",", "") + (m.group(2) or ""))
        except InvalidOperation:
            continue
        e = m.end()
        mult = _mult_at(s, e)[0] if e < len(s) and (s[e] in _MULT_FIRST or s[e] in " \t") else 1
        yield float(d * mult)


# ── 원문 색인 ─────────────────────────────────────────────────────────────────────────────────────────────────
# 앱 머리 · 접기 머리(맨 앞의 대괄호 한 줄, 두 줄까지) — 코드가 붙인 글이라 수를 근거로 세지 않고 JSON 앞에서 건너뛴다. JSON 배열([1, …])과는
# 대괄호 바로 뒤 글자로 가른다.
_HEAD_LINE = re.compile(r'\[(?![\s\[\]{}"0-9tfn-])[^\n]{0,400}\]\n')
# JSON 꼴인데 끝까지 읽지 못한 글(잘린 사본 · 상한에 걸린 원문) — 글 칸(문자열)과 맨 수를 갈라 읽는다(한 번 훑기 — 제곱 시간이 아니다).
_LOOSE = re.compile(r'"((?:[^"\\\n]|\\.)*)"(\s*:)?|(?<![A-Za-z0-9_."])(-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)')
_WS = re.compile(r"\s+")
_HAS_HANGUL = re.compile(r"[가-힣]")
_HAS_LATIN = re.compile(r"[A-Za-z]")
_FOLD_TAIL = "…[전체"          # 접은 배열의 끝 원소 — 그 배열의 길이는 개수가 아니다(앞 50개 + 표지)


def strip_heads(text: str) -> str:
    """맨 앞의 대괄호 머리 줄을 두 줄까지 뗀다(앱 머리 · 허브 접기 머리)."""
    i = 0
    for _ in range(2):
        m = _HEAD_LINE.match(text, i)
        if not m:
            break
        i = m.end()
    return text[i:] if i else text


def _norm_text(s: str) -> str:
    return _WS.sub("", s).lower()


def _value_key(s: str) -> str:
    """원문 값으로 볼 글이면 정규화한 글(공백을 걷고 영문은 소문자), 아니면 '' — 한글이 있으면 3 ~ 64자, 영문만이면 4 ~ 64자, 숫자 비중 50% 미만."""
    if len(s) > 400:
        return ""
    n = _norm_text(s)
    if not 3 <= len(n) <= 64:
        return ""
    if not _HAS_HANGUL.search(n) and (len(n) < 4 or not _HAS_LATIN.search(n)):
        return ""
    if 2 * sum(c.isdigit() for c in n) >= len(n):
        return ""
    return n


class _Builder:
    def __init__(self):
        self.strong: set = set()
        self.weak: set = set()
        self.values: dict = {}          # 정규화 → 쓴 그대로(처음 본 것)
        self.units: set = set()         # 날짜 · 시각 · 숫자가 든 식별자의 견줄 꼴(_raw_units)
        self.full = False
        self.units_full = False

    def num(self, v: float, strong: bool) -> None:
        if self.full or not math.isfinite(v):
            return
        (self.strong if strong else self.weak).add(abs(v))
        if len(self.strong) + len(self.weak) >= NUMS_MAX:
            self.full = True

    def units_from(self, s: str) -> None:
        if self.units_full:
            return
        for u in _raw_units(s):
            self.units.add(u)
            if len(self.units) >= UNITS_MAX:
                self.units_full = True
                return

    def value(self, s: str) -> None:
        if len(self.values) < VALUES_MAX:
            k = _value_key(s)
            if k and k not in self.values:
                self.values[k] = " ".join(s.split())

    def weak_from(self, strs: list) -> None:
        if strs:
            for v in _raw_values("\n".join(strs)):
                self.num(v, False)

    def walk(self, obj) -> None:
        """JSON 값 — 수 값 · 배열 길이는 강한 수, 글 값 · 열쇠 속 수는 약한 수, 글 값은 원문 값."""
        stack, strs = [obj], []
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                for k, y in x.items():
                    strs.append(str(k))
                    stack.append(y)
            elif isinstance(x, list):
                if not (x and isinstance(x[-1], str) and x[-1].startswith(_FOLD_TAIL)):
                    self.num(float(len(x)), True)
                stack.extend(x)
            elif isinstance(x, str):
                strs.append(x)
                self.value(x)
            elif isinstance(x, (int, float)) and not isinstance(x, bool):
                try:
                    self.num(float(x), True)
                except OverflowError:       # 1e308 을 넘는 정수 — 답 글의 수와 견줄 일이 없다
                    pass
        self.weak_from(strs)

    def loose(self, s: str) -> None:
        strs = []
        for m in _LOOSE.finditer(s):
            if m.group(3) is not None:
                try:
                    self.num(float(m.group(3)), True)
                except ValueError:
                    pass
                continue
            lit = m.group(1)
            try:
                lit = json.loads('"' + lit + '"')
            except ValueError:
                pass
            strs.append(lit)
            if not m.group(2):
                self.value(lit)
        self.weak_from(strs)

    def plain(self, s: str, cells: bool = True) -> None:
        for v in _raw_values(s):
            self.num(v, True)
        if cells and "|" in s:
            for line in s.splitlines():
                if "|" in line:
                    for c in line.split("|"):
                        if c.strip():
                            self.value(c.strip())

    def text(self, t: str) -> None:
        body = strip_heads(t)
        self.units_from(body)
        dec, i, n, objs = json.JSONDecoder(), 0, len(body), 0
        while True:
            while i < n and body[i] in " \t\r\n":
                i += 1
            if i >= n or body[i] not in "{[":
                break
            try:
                obj, i2 = dec.raw_decode(body, i)
            except ValueError:
                break
            self.walk(obj)
            objs += 1
            i = i2
        if objs >= 2:
            self.num(float(objs), True)      # 이어 붙은 최상위 개체 수(AIDH 목록 도구의 조각 결과)
        rest = body[i:]
        if rest.strip():
            (self.loose if rest.lstrip()[:1] in "{[" else self.plain)(rest)


def _closest(arr, lo: float, hi: float, c: float):
    i = bisect_left(arr, lo)
    if i >= len(arr) or arr[i] > hi:
        return None
    r = bisect_right(arr, hi)
    k = bisect_left(arr, c, i, r)
    cands = [arr[x] for x in (k - 1, k) if i <= x < r]
    return min(cands, key=lambda x: abs(x - c))


def _windows(n: Num):
    eps = 1e-9 * max(1.0, n.value)
    yield n.value - n.tol - eps, n.value + n.tol + eps, n.value
    if n.pct:                                              # 원문 0.125 ↔ 답 12.5%
        v, t = n.value / 100, n.tol / 100
        yield v - t - eps / 100, v + t + eps / 100, v


class RawIndex:
    """이번 턴의 원문 색인 — 수는 정렬한 array('d') 둘(강한 것 · 전부), 원문 값은 정규화한 글(OT-08), 날짜 · 시각 · 숫자가 든 식별자는 통째 글."""

    def __init__(self, strong=(), weak=(), values=None, partial=False, chars=0, units=()):
        st = set(strong)
        self.strong = array("d", sorted(st))
        self.all = array("d", sorted(st | set(weak)))
        self._vmap: dict = dict(values or {})
        self._pre: dict | None = None
        self.units = frozenset(units)
        self.partial = bool(partial)
        self.chars = int(chars)

    @classmethod
    def build(cls, texts, extra=(), *, partial=False) -> "RawIndex":
        """texts — 모델 사본 · 원문(그릇). extra — 덧붙인 근거(사람 발화 · 붙인 문서 · 이력, 수만 강하게). 수가 상한(NUMS_MAX)을 넘으면 partial.
        extra 를 먼저 넣는다 — 종전엔 앞의 큰 수치 배열이 상한을 채우면 뒤의 글(사람 발화 · 이력까지)을 통째로 건너뛰어 사람이 말한 수가 근거가
        아니게 됐다(반대 검토 2차). texts 도 부르는 쪽이 모델이 본 사본을 원문보다 앞에 둔다(_sc_index)."""
        b, chars = _Builder(), 0
        for t in extra:
            t = t if isinstance(t, str) else str(t or "")
            if t and not b.full:
                b.plain(t, cells=False)
                b.units_from(t)
        for t in texts:
            t = t if isinstance(t, str) else str(t or "")
            chars += len(t)
            if t and not b.full:
                b.text(t)
        return cls(b.strong, b.weak, b.values, partial or b.full or b.units_full, chars, b.units)

    @property
    def values(self) -> list:
        return list(self._vmap)

    def has_unit(self, u: str) -> bool:
        """원문에 이 날짜 · 시각 · 식별자(unit_tokens 의 견줄 꼴)가 통째로 있나."""
        return u in self.units

    def nearest(self, n, strong: bool = False):
        """폭 안의 원문 수 가운데 가장 가까운 것(없으면 None) — 셈의 기준 값이 된다."""
        n = _as_num(n)
        if n is None:
            return None
        arr = self.strong if strong else self.all
        best = None
        for lo, hi, c in _windows(n):
            got = _closest(arr, lo, hi, c)
            if got is not None and (best is None or abs(got - c) < abs(best[0] - best[1])):
                best = (got, c)
        return None if best is None else best[0]

    def has(self, n, strong: bool = False) -> bool:
        """원문에 이 수가 있나 — 반올림 폭 안, % 면 100 으로 나눈 값도. strong 이면 강한 수(JSON 수 값 · 배열 길이 · 글 원문)만."""
        return self.nearest(n, strong) is not None

    def _prefixes(self) -> dict:
        """원문 값의 머리 세 글자 → 값들(처음 쓸 때 한 번 짓는다)."""
        if self._pre is None:
            pre: dict = {}
            for v in self._vmap:
                pre.setdefault(v[:3], []).append(v)
            self._pre = pre
        return self._pre

    def _values_in(self, norm: str) -> list:
        if not self._vmap or len(norm) < 3:
            return []
        pre, seen = self._prefixes(), {}
        for i in range(len(norm) - 2):
            for v in pre.get(norm[i:i + 3], ()):
                if v not in seen and norm.startswith(v, i):
                    seen[v] = None
        return list(seen)

    def values_in(self, text: str) -> list:
        """글에 든 원문 값(쓴 그대로, 처음 나온 차례) — 공백을 걷고 견준다."""
        return [self._vmap[v] for v in self._values_in(_norm_text(str(text or "")))]

    def _value_seq(self, norm: str) -> list:
        """글에 든 원문 값의 차례 — 겹치지 않게, 한 자리에서는 가장 긴 값. 원문 값이 아닌 긴 낱말 속의 값(품질기술팀장님 ⊃ 품질기술팀)은
        그 값이다."""
        if not self._vmap or len(norm) < 3:
            return []
        pre, out, i, n = self._prefixes(), [], 0, len(norm)
        while i <= n - 3:
            best = max((v for v in pre.get(norm[i:i + 3], ()) if norm.startswith(v, i)), key=len, default="")
            if best:
                out.append(best)
                i += len(best)
            else:
                i += 1
        return out

    def values_delta(self, old: str, new: str) -> tuple[list, list]:
        """(옛 글보다 새 글에서 빠진 원문 값, 새로 든 원문 값) — 값이 놓인 차례를 맞대어(difflib) 센다. 개수로만 세던 때는 두 항목의 값을 서로
        바꾼 쌍(X-01 품질기술팀 · X-02 신뢰성그룹 → 서로 바꿈)이 개수가 그대로라 넣음이 됐다(OT-08 의 바꿈 꼴 — 반대 검토 2차). 띄어쓰기만 다른
        것과, 원문 값이 아닌 긴 낱말 속에 든 값은 그대로 있는 것으로 센다."""
        o = self._value_seq(_norm_text(str(old or "")))
        w = self._value_seq(_norm_text(str(new or "")))
        gone, more = _seq_diff(o, w)
        return ([self._vmap[v] for v in dict.fromkeys(gone)], [self._vmap[v] for v in dict.fromkeys(more)])

    def values_removed(self, old: str, new: str) -> list:
        return self.values_delta(old, new)[0]


# ── 출처 판정 ─────────────────────────────────────────────────────────────────────────────────────────────────
def derivable(n, anchors, budget: list | None = None, *, spent: bool = False) -> bool:
    """n 이 기준 값 a, b(같은 값 두 번도 된다)의 합 · 차 · 비(a ÷ b) · 곱으로 반올림 폭 안에 나오나 — % 면 b ÷ a · |b ÷ a − 1|(증감률)도.
    budget 은 [남은 셈 수] — 0 이면 셈하지 않고 spent 를 돌려준다. 보수적인 쪽이 판정마다 다르다 — 더한 수는 '나오지 않는다'(거짓 → 짚음),
    지운 수는 '나온다'(참 → 버림)가 보수적이다. 종전엔 늘 거짓이라 셈 상한을 다 쓴 뒤의 지움이 넣음이 됐다(반대 검토 2차)."""
    n = _as_num(n)
    if n is None or not anchors:
        return False
    if budget is not None:
        if budget[0] <= 0:
            return spent
        budget[0] -= 1
    arr = sorted({abs(float(a)) for a in anchors})
    v, t = n.value, n.tol + 1e-9 * max(1.0, n.value)

    def hit(lo: float, hi: float) -> bool:
        i = bisect_left(arr, lo)
        return i < len(arr) and arr[i] <= hi

    for a in arr:
        if hit(v - t - a, v + t - a):                                         # v ≈ a + b
            return True
        if hit(a - v - t, a - v + t) or hit(a + v - t, a + v + t):            # v ≈ |a − b|
            return True
        if a <= 0:
            if v <= t:                                                        # 0 × b · 0 ÷ b
                return True
            continue
        if hit(a / (v + t), a / (v - t) if v > t else math.inf):              # v ≈ a ÷ b
            return True
        if hit((v - t) / a, (v + t) / a):                                     # v ≈ a × b(비율의 뒤집기)
            return True
        if n.pct:
            r, rt = v / 100, t / 100
            if hit(a * (r - rt), a * (r + rt)):                               # r ≈ b ÷ a
                return True
            if hit(a * (1 + r - rt), a * (1 + r + rt)) or hit(a * (1 - r - rt), a * (1 - r + rt)):   # r ≈ |b ÷ a − 1|
                return True
    return False


def sourced(n, idx: RawIndex, anchors, strong: bool = False, budget: list | None = None) -> bool:
    """원문에 있거나(idx.has) 기준 값의 셈으로 나온다(derivable)."""
    n = _as_num(n)
    return n is not None and (idx.has(n, strong=strong) or derivable(n, anchors, budget))


def summed(n, values) -> bool:
    """n 이 값 셋 이상의 합인가 — 전부의 합이거나, 넷 이상이면 하나를 뺀 나머지의 합(줄의 합계 '403 = 120 + 135 + 148'). 셈(derivable)은
    둘씩만 보아 이 꼴의 합계를 지우는 쌍이 늘 넣음이 됐다(반대 검토 2차)."""
    n = _as_num(n)
    vs = [abs(float(v)) for v in values]
    if n is None or len(vs) < 3:
        return False
    t, tot = n.tol + 1e-9 * max(1.0, n.value), sum(vs)
    return abs(tot - n.value) <= t or (len(vs) >= 4 and any(abs(tot - v - n.value) <= t for v in vs))


def _anchors(idx: RawIndex, nums) -> list:
    """글 속 수 가운데 원문에 있는 것의 원문 값(가장 가까운 것) — 작은 정수는 강한 수일 때만, ANCHORS_MAX 개까지. 날짜 · 식별자 속 조각은 셈의
    기준이 아니다(2026 · 10 의 합 · 차로 엉뚱한 수가 '나온다')."""
    out: list = []
    for n in nums:
        if n.ctx != "num":
            continue
        a = idx.nearest(n, strong=n.small)
        if a is not None and a not in out:
            out.append(a)
            if len(out) >= ANCHORS_MAX:
                break
    return out


_LEAD = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")


def sourced_in(text: str, idx: RawIndex, *, message: str = "") -> set:
    """글 속 수 가운데 출처가 있는 것의 숫자 글자(쉼표 · 배수 · % 꼬리 뺀 — evidence.sig_numbers 의 꼴) — [코드 대조] 목록에서 뺄 것(원문에는
    있는데 사본에서 잘린 수 · 원문 값의 셈)."""
    nums = num_tokens(text)
    anchors = _anchors(idx, nums + num_tokens(message))
    budget = [DERIVE_MAX]
    out = set()
    for n in nums:
        if idx.has(n, strong=True) if n.small else (idx.has(n) or derivable(n, anchors, budget)):
            out.add(_LEAD.match(n.text).group(0).replace(",", ""))
    return out


# ── 쌍마다 결정 ───────────────────────────────────────────────────────────────────────────────────────────────
class Decision(NamedTuple):
    action: str      # keep · demote(짚음 — 넣지 않고 사람에게 보인다) · drop(버림)
    code: str
    detail: str      # 걸린 수 · 값을 쓴 그대로


KEEP = Decision("keep", "keep", "")
CODES = ("drop_sourced_number", "demote_weak_source", "drop_marks_sourced", "demote_removes_value", "demote_unsourced_number",
         "demote_coupled")
# 까닭 — 고정 글에 걸린 수 · 값만 넣는다(design-ha27.md 6.7).
WHY = {
    "drop_sourced_number": "초안의 수치 {d} 는 조회 결과 원문에 있다",
    "demote_weak_source": "초안의 {d} 가 조회 결과 글 속에만 있어 코드가 맞는지 가리지 못했다",
    "drop_marks_sourced": "'근거 없음' 을 단 수치 · 값이 조회 결과 원문에 있다({d})",
    "demote_removes_value": "조회 결과 원문의 값 '{d}' 를 빼거나 바꾼다 — 어느 항목의 값인지 코드가 확인하지 못했다",
    "demote_unsourced_number": "새 수치 {d} 는 조회 결과 원문에도, 원문 값의 합 · 차 · 비율에도 없다",
    "demote_coupled": "반영하지 않은 다른 고침과 같은 수치 · 값({d})을 쓴다",
}
# '근거 없음' 표지 — 새 글이 옛 글보다 많이 맞으면 표시를 더한 쌍이다. '(미확인)' · '(출처 불명)' · '(확인 필요)' · '(검증되지 않음)' 처럼 낱말만
# 바꾼 의심 표시도 같다 — 종전 꼴로는 원문의 수에 그 표시를 다는 쌍이 버림을 비켜 넣음이 됐다(반대 검토 2차).
_MARK_RE = re.compile(r"근거\s*(?:가\s*)?(?:없|부족|미확인|불명|불분명)|출처\s*(?:가\s*)?(?:없|불명|미상|불분명)|확인되지\s*않|미확인"
                      r"|확인\s*(?:이\s*)?필요|검증되지\s*않|검증\s*(?:이\s*)?필요|불확실|추정\s*\)")


def reason(d: Decision) -> str:
    return WHY.get(d.code, "").format(d=d.detail)


def _seq_diff(a: list, b: list) -> tuple[list, list]:
    """(a 에서 빠진 것, b 에 새로 든 것) — 놓인 차례를 맞대어(difflib) 센다. 개수로만 세면(다중집합 차이) 한 쌍 안에서 두 항목의 수 · 값을
    서로 바꾼 고침이 그대로라 넣음이 됐다(OT-08 의 바꿈 꼴 — 반대 검토 2차). 차례가 같은 덧붙임 · 지움 · 바꿈은 다중집합 차이와 같다."""
    gone, more = [], []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            gone += a[i1:i2]
        if tag in ("replace", "insert"):
            more += b[j1:j2]
    return gone, more


def _num_diff(on: list, ou: list, wn: list, wu: list) -> tuple[list, list]:
    """수의 (지운 것, 더한 것) — 열쇠의 차례로 맞대고, 같은 자리에 맞은 수라도 단위가 둘 다 있고 다르면(312.5 MPa → 312.5 kPa) 바꾼 것이다."""
    gone, more = [], []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, [n.key for n in on], [n.key for n in wn], autojunk=False).get_opcodes():
        if tag == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                if ou[i] and wu[j] and ou[i] != wu[j]:
                    gone.append(on[i])
                    more.append(wn[j])
            continue
        if tag in ("replace", "delete"):
            gone += on[i1:i2]
        if tag in ("replace", "insert"):
            more += wn[j1:j2]
    return gone, more


def _draft_span(draft: str, old: str) -> tuple[int, int] | None:
    """초안에서 옛 글의 자리 — _sc_apply 와 같은 방식(그대로, 아니면 띄어쓰기만 다르게)으로 한 곳 찾았을 때만."""
    if not draft or not old:
        return None
    hits = [m.span() for m in re.finditer(re.escape(old), draft)]
    if not hits and old.split():
        hits = [m.span() for m in re.finditer(r"\s+".join(map(re.escape, old.split())), draft)]
    return hits[0] if len(hits) == 1 else None


def _draft_lines(draft: str, old: str) -> str:
    """초안에서 옛 글이 걸친 줄 — 한 곳 찾았을 때만."""
    sp = _draft_span(draft, old)
    if sp is None:
        return ""
    s, e = sp
    b = draft.find("\n", e)
    return draft[draft.rfind("\n", 0, s) + 1:b if b >= 0 else len(draft)]


def _widen(draft: str, old: str, new: str) -> tuple[str, str]:
    """쌍의 자리가 초안의 수 · 날짜 · 식별자 한가운데에서 시작하거나 끝나면 그 경계까지 넓힌 (옛 글, 새 글). 넣기(_sc_apply)는 [원문] 을 초안의
    아무 자리에서나 찾으므로 '34개' 의 '4개' → '5개' 는 답을 '35개' 로 바꾸는데, 쌍 글만 떼어 보면 4 → 5 라 관문이 넣음으로 뒀다(반대 검토
    2차). 넓힌 글로 34 → 35 를 본다. 초안에서 한 곳을 못 찾으면(넣기도 못 한다) 그대로다."""
    sp = _draft_span(draft, old)
    if sp is None:
        return old, new
    s, e = sp
    toks, spans, ms = _scan(draft)
    units = [m.span() for m in _DATE.finditer(ms)] + _idents(ms)
    s0, e0 = s, e
    for a, b in spans + units:
        if a < s < b:
            s = a
        if a < e < b:
            e = b
    return (old, new) if (s, e) == (s0, e0) else (draft[s:e], draft[s:s0] + new + draft[e0:e])


class _Pair:
    def __init__(self, idx: RawIndex, old: str, new: str, draft: str, glob: list, budget: list):
        self.idx, self.old, self.new, self.draft, self.glob, self.budget = idx, old, new, draft, glob, budget
        xo, xw = _widen(draft, old, new)
        (on, ospan, os_), (wn, wspan, ws_) = _scan(xo), _scan(xw)
        keep_o = [i for i, n in enumerate(on) if n.ctx == "num"]           # 날짜 · 식별자 속 조각은 통째 글(units)로 본다
        keep_w = [i for i, n in enumerate(wn) if n.ctx == "num"]
        self.on, self.wn = [on[i] for i in keep_o], [wn[i] for i in keep_w]
        self.removed, self.added = _num_diff(self.on, [_unit_at(os_, ospan[i][1]) for i in keep_o],
                                             self.wn, [_unit_at(ws_, wspan[i][1]) for i in keep_w])
        self.ou, self.wu = unit_tokens(xo), unit_tokens(xw)
        gone, more = _seq_diff([u for u, _t in self.ou], [u for u, _t in self.wu])
        texts = dict(self.ou + self.wu)
        self.u_removed = [(u, texts[u]) for u in gone]
        self.u_added = [(u, texts[u]) for u in more]
        self._local: list | None = None
        self._line: list | None = None
        self._delta: tuple | None = None

    def delta(self) -> tuple:
        """(빠진 원문 값, 새로 든 원문 값) — 판정(4줄)과 묶음이 함께 쓴다."""
        if self._delta is None:
            self._delta = self.idx.values_delta(self.old, self.new)
        return self._delta

    def local(self) -> list:
        if self._local is None:
            self._local = _anchors(self.idx, self.on + self.wn + num_tokens(_draft_lines(self.draft, self.old)))
        return self._local

    def line(self) -> list:
        """초안에서 옛 글이 걸친 줄의 수 가운데 원문에 있는 것의 원문 값 — 겹친 값도 그대로(합계 셈 summed 의 재료)."""
        if self._line is None:
            got = []
            for n in num_tokens(_draft_lines(self.draft, self.old)):
                if n.ctx == "num":
                    a = self.idx.nearest(n, strong=n.small)
                    if a is not None:
                        got.append(a)
            self._line = got
        return self._line

    def src(self, n: Num, removing: bool = False) -> bool:
        """작은 정수는 강한 수이거나 그 줄의 셈, 그 밖은 원문(약한 수 포함)이거나 전역 셈. 둘 다 그 줄의 합계(summed)도 본다. removing 이면
        셈 상한을 다 쓴 뒤에도 '나온다' 로 본다(지운 수 쪽의 보수 — derivable)."""
        if n.small:
            ok = self.idx.has(n, strong=True) or derivable(n, self.local(), self.budget, spent=removing)
        else:
            ok = self.idx.has(n) or derivable(n, self.glob, self.budget, spent=removing)
        return ok or summed(n, self.line())

    def decide(self, values: bool) -> Decision:
        for n in self.removed:                                                  # ② 원문에 있는 수를 지우거나 다른 수로 바꾼다
            if self.src(n, removing=True):
                return Decision("drop", "drop_sourced_number", n.text)
        # ② 의 날짜 · 시각 — 원문에 통째로 있는 날짜를 지우거나 바꾸면 버린다. 원문의 다른 날짜로 바꾸면(어느 항목의 날짜인지 코드가 모른다) 짚는다.
        dates_in = [t for u, t in self.u_removed if u[0] in "dt" and self.idx.has_unit(u)]
        if dates_in:
            if any(u[0] in "dt" and self.idx.has_unit(u) for u, _t in self.u_added):
                return Decision("demote", "demote_removes_value", dates_in[0])
            return Decision("drop", "drop_sourced_number", dates_in[0])
        for n in self.removed:                                                  # ② 의 보완 — 글 칸 속 조각으로만 맞는 작은 정수
            if n.small and self.idx.has(n):
                return Decision("demote", "demote_weak_source", n.text)
        if len(_MARK_RE.findall(self.new)) > len(_MARK_RE.findall(self.old)):   # ② 원문의 수 · 값에 '근거 없음' 을 단다
            if self.on or self.ou:
                if all(self.src(n) for n in self.on) and all(self.idx.has_unit(u) for u, _t in self.ou):
                    return Decision("drop", "drop_marks_sourced", (self.on[0].text if self.on else self.ou[0][1]))
            elif values:
                got = self.idx.values_in(self.old)
                if got:
                    return Decision("drop", "drop_marks_sourced", got[0])
        ids_in = [t for u, t in self.u_removed if u[0] == "i" and self.idx.has_unit(u)]
        if ids_in:                                                              # 원문의 식별자(FM-0012)를 빼거나 다른 것으로 바꾼다 — 값과 같다
            return Decision("demote", "demote_removes_value", ids_in[0])
        if values:                                                              # OT-08 — 원문 값을 빼거나 다른 값으로 바꾼다
            gone = self.delta()[0]
            if gone:
                return Decision("demote", "demote_removes_value", gone[0])
        for n in self.added:                                                    # ① 새 수가 원문에도 원문 값의 셈에도 없다
            if not self.src(n):
                return Decision("demote", "demote_unsourced_number", n.text)
        for u, t in self.u_added:                                               # ① 새 날짜 · 식별자가 원문에 없다
            if not self.idx.has_unit(u):
                return Decision("demote", "demote_unsourced_number", t)
        return KEEP


def gate_patches(patches, idx: RawIndex, *, draft: str = "", message: str = "", values: bool = True) -> list[Decision]:
    """쌍 [(옛 글, 새 글)] 마다 넣음 · 짚음 · 버림 — 위에서부터 처음 맞는 줄로 정하고(design-ha27.md 6.4), 버림 · 짚음이 된 쌍이 더한 수 ·
    원문 값을 넣음 쌍이 더하면 그 쌍도 짚는다(묶음 6.5 — 답이 '34개 … 그중 13개' 로 앞뒤가 어긋나지 않게). values=False 면 원문 값 줄을 끈다
    (CHAT_SELF_CHECK_GATE=num)."""
    pairs = [(str(o or ""), str(w or "")) for o, w in patches]
    budget = [DERIVE_MAX]
    olds_news = [n for o, w in pairs for n in (*num_tokens(o), *num_tokens(w))]
    glob = _anchors(idx, olds_news + num_tokens(draft) + num_tokens(message))
    ps = [_Pair(idx, o, w, draft, glob, budget) for o, w in pairs]
    out = [p.decide(values) for p in ps]
    adds = [({**{n.key: n.text for n in p.added}, **dict(p.u_added)}, set(p.delta()[1]) if values else set()) for p in ps]
    changed = True
    while changed:
        changed = False
        keys = {k for (ks, _v), d in zip(adds, out) if d.action != "keep" for k in ks}
        vals = {v for (_k, vs), d in zip(adds, out) if d.action != "keep" for v in vs}
        for i, ((ks, vs), d) in enumerate(zip(adds, out)):
            if d.action != "keep":
                continue
            hit = next((ks[k] for k in ks if k in keys), None) or next((v for v in vs if v in vals), None)
            if hit:
                out[i] = Decision("demote", "demote_coupled", hit)
                changed = True
    return out
