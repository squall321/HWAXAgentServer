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
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

NUMS_MAX = 500_000      # 색인에 담는 수(강한 것 · 약한 것 합) — 넘으면 partial(meta · /health 에 남는다)
VALUES_MAX = 50_000     # 원문 값 목록
ANCHORS_MAX = 64        # 셈의 기준 값
DERIVE_MAX = 40         # 한 번의 검토에서 셈을 해 보는 수 — 넘으면 '셈으로 안 나온다'(짚음 쪽 — 보수적)

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
# 인용 표지는 뒤에 세는 말 · % · 수가 이어지지 않을 때만이다 — '조회 결과 34건' 은 인용이 아니라 개수다(가리면 OT-04 꼴의 34 → 13 이 관문을 비켜 간다).
_MASK_TEXT = re.compile(r"https?://\S+|\]\([^)\s]*\)"
                        r"|(?:조회\s*)?결과\s*#?[0-9]+(?![0-9.,%％]|\s*(?:[개건명원회초분대장곳종번배톤]|퍼센트|%|％))"
                        r"|^[ \t]*[0-9]{1,2}[.)](?=\s)", re.M)
_DATE = re.compile(r"[0-9]{4}[-./][0-9]{1,2}[-./][0-9]{1,2}|[0-9]{4}\s*년\s*[0-9]{1,2}\s*월(?:\s*[0-9]{1,2}\s*일)?"
                   r"|[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?")
_IDENT = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+")
_PCT = ("%포인트", "%p", "%", "％", "퍼센트")
# 배수 — 뒤가 한글이 아니거나 세는 말 · 어림 꼬리일 때만(만큼 · 만에서 는 배수가 아니다). 천만 · 백만 같은 겹낱말을 먼저 본다.
_MULTS = (("천만", 10 ** 7), ("천억", 10 ** 11), ("백만", 10 ** 6), ("백억", 10 ** 10), ("십만", 10 ** 5), ("십억", 10 ** 9),
          ("천", 10 ** 3), ("만", 10 ** 4), ("억", 10 ** 8))
_MULT_FIRST = frozenset(w[0] for w, _m in _MULTS)
_COUNTERS = frozenset("개건명원회초분대장곳종번배톤")
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


def _is_hangul(c: str) -> bool:
    return "가" <= c <= "힣"


def _mult_at(s: str, i: int) -> tuple[int, int]:
    """s[i:] 가 배수 낱말(공백 하나까지 건너뛴다)이면 (배수, 낱말 뒤 자리), 아니면 (1, i)."""
    j = i + 1 if i < len(s) and s[i] in " \t" else i
    if j >= len(s) or s[j] not in _MULT_FIRST:
        return 1, i
    for w, m in _MULTS:
        if s.startswith(w, j):
            k = j + len(w)
            if k >= len(s) or not _is_hangul(s[k]) or s[k] in _COUNTERS or s.startswith(_APPROX_AFTER, k):
                return m, k
            return 1, i
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


def num_tokens(text) -> list[Num]:
    """답 글의 수 — 가림(주소 · 그림 주소 · 인용 표지 · 줄머리 번호 · UUID · cf-)을 지나 쓴 차례대로. 범위 'a ~ b%' 의 a 도 % 다."""
    raw = str(text or "")
    if not raw:
        return []
    s = _MASK_TEXT.sub(_blank, _MASK_ID.sub(_blank, raw))        # 같은 길이의 공백으로 — 자리를 지킨다
    dates = [m.span() for m in _DATE.finditer(s)]
    idents = [m.span() for m in _IDENT.finditer(s)] if re.search(r"[A-Za-z]", s) else []
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
    return toks


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
        self.full = False

    def num(self, v: float, strong: bool) -> None:
        if self.full or not math.isfinite(v):
            return
        (self.strong if strong else self.weak).add(abs(v))
        if len(self.strong) + len(self.weak) >= NUMS_MAX:
            self.full = True

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
    """이번 턴의 원문 색인 — 수는 정렬한 array('d') 둘(강한 것 · 전부), 원문 값은 정규화한 글(OT-08)."""

    def __init__(self, strong=(), weak=(), values=None, partial=False, chars=0):
        st = set(strong)
        self.strong = array("d", sorted(st))
        self.all = array("d", sorted(st | set(weak)))
        self._vmap: dict = dict(values or {})
        self._pre: dict | None = None
        self.partial = bool(partial)
        self.chars = int(chars)

    @classmethod
    def build(cls, texts, extra=(), *, partial=False) -> "RawIndex":
        """texts — 원문(그릇) · 모델 사본. extra — 덧붙인 근거(사람 발화 · 붙인 문서 · 이력, 수만 강하게). 수가 상한(NUMS_MAX)을 넘으면 partial."""
        b, chars = _Builder(), 0
        for t in texts:
            t = t if isinstance(t, str) else str(t or "")
            chars += len(t)
            if t and not b.full:
                b.text(t)
        for t in extra:
            t = t if isinstance(t, str) else str(t or "")
            if t and not b.full:
                b.plain(t, cells=False)
        return cls(b.strong, b.weak, b.values, partial or b.full, chars)

    @property
    def values(self) -> list:
        return list(self._vmap)

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

    def _values_in(self, norm: str) -> list:
        if not self._vmap or len(norm) < 3:
            return []
        if self._pre is None:
            pre: dict = {}
            for v in self._vmap:
                pre.setdefault(v[:3], []).append(v)
            self._pre = pre
        seen: dict = {}
        for i in range(len(norm) - 2):
            for v in self._pre.get(norm[i:i + 3], ()):
                if v not in seen and norm.startswith(v, i):
                    seen[v] = None
        return list(seen)

    def values_in(self, text: str) -> list:
        """글에 든 원문 값(쓴 그대로, 처음 나온 차례) — 공백을 걷고 견준다."""
        return [self._vmap[v] for v in self._values_in(_norm_text(str(text or "")))]

    def values_delta(self, old: str, new: str) -> tuple[list, list]:
        """(옛 글보다 새 글에서 줄어든 원문 값, 늘어난 원문 값) — 다른 낱말 안에 든 값(품질기술팀 ⊂ 품질기술팀장)은 줄지 않은 것으로 센다."""
        o, w = _norm_text(str(old or "")), _norm_text(str(new or ""))
        found = list(dict.fromkeys(self._values_in(o) + self._values_in(w)))
        gone = [self._vmap[v] for v in found if w.count(v) < o.count(v)]
        more = [self._vmap[v] for v in found if w.count(v) > o.count(v)]
        return gone, more

    def values_removed(self, old: str, new: str) -> list:
        return self.values_delta(old, new)[0]


# ── 출처 판정 ─────────────────────────────────────────────────────────────────────────────────────────────────
def derivable(n, anchors, budget: list | None = None) -> bool:
    """n 이 기준 값 a, b(같은 값 두 번도 된다)의 합 · 차 · 비(a ÷ b) · 곱으로 반올림 폭 안에 나오나 — % 면 b ÷ a · |b ÷ a − 1|(증감률)도.
    budget 은 [남은 셈 수] — 0 이면 셈하지 않고 거짓(짚음 쪽)이다."""
    n = _as_num(n)
    if n is None or not anchors:
        return False
    if budget is not None:
        if budget[0] <= 0:
            return False
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


def _anchors(idx: RawIndex, nums) -> list:
    """글 속 수 가운데 원문에 있는 것의 원문 값(가장 가까운 것) — 작은 정수는 강한 수일 때만, ANCHORS_MAX 개까지."""
    out: list = []
    for n in nums:
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
# '근거 없음' 표지 — 새 글이 옛 글보다 많이 맞으면 표시를 더한 쌍이다.
_MARK_RE = re.compile(r"근거\s*(?:가\s*)?(?:없|부족|미확인)|출처\s*(?:가\s*)?없|확인되지\s*않|추정\s*\)")


def reason(d: Decision) -> str:
    return WHY.get(d.code, "").format(d=d.detail)


def _minus(a: list, b: list) -> list:
    """다중집합 차이(열쇠로) — a 에서 b 에 짝이 있는 것을 하나씩 뺀다(쓴 차례 그대로)."""
    left = Counter(n.key for n in b)
    out = []
    for n in a:
        if left[n.key] > 0:
            left[n.key] -= 1
        else:
            out.append(n)
    return out


def _draft_lines(draft: str, old: str) -> str:
    """초안에서 옛 글이 걸친 줄 — _sc_apply 와 같은 방식(그대로, 아니면 띄어쓰기만 다르게)으로 한 곳 찾았을 때만."""
    if not draft or not old:
        return ""
    hits = [m.span() for m in re.finditer(re.escape(old), draft)]
    if not hits and old.split():
        hits = [m.span() for m in re.finditer(r"\s+".join(map(re.escape, old.split())), draft)]
    if len(hits) != 1:
        return ""
    s, e = hits[0]
    b = draft.find("\n", e)
    return draft[draft.rfind("\n", 0, s) + 1:b if b >= 0 else len(draft)]


class _Pair:
    def __init__(self, idx: RawIndex, old: str, new: str, draft: str, glob: list, budget: list):
        self.idx, self.old, self.new, self.draft, self.glob, self.budget = idx, old, new, draft, glob, budget
        self.on, self.wn = num_tokens(old), num_tokens(new)
        self.removed, self.added = _minus(self.on, self.wn), _minus(self.wn, self.on)
        self._local: list | None = None
        self._delta: tuple | None = None

    def delta(self) -> tuple:
        """(줄어든 원문 값, 늘어난 원문 값) — 판정(4줄)과 묶음이 함께 쓴다."""
        if self._delta is None:
            self._delta = self.idx.values_delta(self.old, self.new)
        return self._delta

    def local(self) -> list:
        if self._local is None:
            self._local = _anchors(self.idx, self.on + self.wn + num_tokens(_draft_lines(self.draft, self.old)))
        return self._local

    def src(self, n: Num) -> bool:
        """작은 정수는 강한 수이거나 그 줄의 셈, 그 밖은 원문(약한 수 포함)이거나 전역 셈."""
        if n.small:
            return self.idx.has(n, strong=True) or derivable(n, self.local(), self.budget)
        return self.idx.has(n) or derivable(n, self.glob, self.budget)

    def decide(self, values: bool) -> Decision:
        for n in self.removed:                                                  # ② 원문에 있는 수를 지우거나 다른 수로 바꾼다
            if self.src(n):
                return Decision("drop", "drop_sourced_number", n.text)
        for n in self.removed:                                                  # ② 의 보완 — 글 칸 속 조각으로만 맞는 작은 정수
            if n.small and self.idx.has(n):
                return Decision("demote", "demote_weak_source", n.text)
        if len(_MARK_RE.findall(self.new)) > len(_MARK_RE.findall(self.old)):   # ② 원문의 수 · 값에 '근거 없음' 을 단다
            if self.on:
                if all(self.src(n) for n in self.on):
                    return Decision("drop", "drop_marks_sourced", self.on[0].text)
            elif values:
                got = self.idx.values_in(self.old)
                if got:
                    return Decision("drop", "drop_marks_sourced", got[0])
        if values:                                                              # OT-08 — 원문 값을 빼거나 다른 값으로 바꾼다
            gone = self.delta()[0]
            if gone:
                return Decision("demote", "demote_removes_value", gone[0])
        for n in self.added:                                                    # ① 새 수가 원문에도 원문 값의 셈에도 없다
            if not self.src(n):
                return Decision("demote", "demote_unsourced_number", n.text)
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
    adds = [({n.key: n.text for n in p.added}, set(p.delta()[1]) if values else set()) for p in ps]
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
