# 답변의 수치를 도구 출력 원문과 대조하는 공용 판정 — 챗(근거 블록)과 심의(의사결정문)가 같이 쓴다
"""모델이 아니라 **코드**가 대조한다 — 그래서 지어낼 수 없다.

챗에만 있던 판정을 심의 의사결정문에도 걸기 위해 떼어 냈다. 두 곳이 같은 기준으로
판정해야 사용자가 본 경고의 뜻이 화면마다 달라지지 않는다.
"""
from __future__ import annotations

import re

# ⚠ 뒤 경계를 `(?!\w)` 로 두면 **한글 단위가 붙은 수치를 통째로 놓친다** — 한국어 답변은
# 대부분 "408명"·"48039.32MPa" 처럼 쓴다(실측: 408명은 검사조차 안 됐고, "1250.5원" 은
# "1250" 으로 잘려 엉뚱한 값을 대조했다). 숫자 한가운데를 끊지 않게 `(?![\d.])` 로 막되
# 단위 글자는 허용한다. 가장 중요한 안전장치가 조용히 헛돌던 자리다.
# 천 단위 쉼표는 쉼표 뒤 묶음이 모두 세 자리일 때만 수 하나로 본다 — 그 꼴이 아니면 쉼표에서 끊어 하나씩 대조한다. 종전엔 쉼표로
# 잇댄 쪽 번호(p.2,3,10,11,23)를 수 하나(3101123)로 읽어 맞는 쪽 번호를 '확인되지 않은 수치' 로 띄웠다(HWAXPortal docs/siren-delib HA-38).
_NUM_TOK_RE = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\d.])")


# id 꼴은 수치가 아니다(HWAXPortal docs/request-1125 WD-12) — 잡 id(<종류>-YYYYMMDD-HHMMSS-<4~8자>) · UUID · 확인 번호(cf-) · 숫자와 a~f 가 섞인
# 7자 넘는 16진(커밋 · 해시) · 영문 머리를 붙인 번호(HA-35 · PLM-7781 · OB_89) · #번호. 종전엔 잡 id 의 날짜 조각 · 결함 번호 조각이 '확인 안 된
# 수치' 로 떠 표시가 늘었다. 답 쪽에서만 같은 길이의 빈칸으로 가린다(자리가 그대로라 % 붙음 판정이 흔들리지 않는다). 유령 ID 막이(_int_tokens)와는
# 따로다 — 그쪽은 도구 인자의 id 를 본다.
_ID_SPAN_RE = re.compile(
    r"(?<![0-9A-Za-z])[A-Za-z][A-Za-z0-9_]*-\d{8}-\d{6}-[0-9A-Za-z]{4,8}(?![0-9A-Za-z])"
    r"|(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f])"
    r"|\bcf-[0-9a-f]{12}\b"
    r"|(?<![0-9A-Za-z])(?=[0-9a-fA-F]*[0-9])(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,}(?![0-9A-Za-z])"
    r"|(?<![0-9A-Za-z])[A-Za-z]{1,12}[-_]\d+(?![\d.])"
    r"|(?<![\w#])#\d+(?![\d.])")


def _mask_ids(text: str) -> str:
    return _ID_SPAN_RE.sub(lambda m: " " * len(m.group(0)), text)


def _sig_tokens(text: str) -> list:
    """sig_numbers 의 꼴에 % 붙음을 더한 것 — [(쓴 그대로, 쉼표 뗀 글, 뒤에 % 가 붙었나)]. id 꼴은 가린 뒤에 훑는다(WD-12)."""
    text = _mask_ids(text or "")
    out = []
    for m in _NUM_TOK_RE.finditer(text):
        raw = m.group(0)
        norm = raw.replace(",", "")
        try:
            val = float(norm)
        except ValueError:
            continue
        if "." not in norm and val < 100:
            continue
        out.append((raw, norm, bool(_PCT_AFTER.match(text, m.end()))))
    return out


def sig_numbers(text: str) -> list:
    """대조할 가치가 있는 수치만. 오탐을 줄이려고 범위를 좁게 잡는다.

    작은 정수(0~99)는 개수·순번·백분율로 정상 생성되는 값이라 제외한다 — 여기까지 잡으면
    경고가 남발돼 표시 자체를 아무도 안 보게 된다. 소수점이 있거나 100 이상인 값만 본다.
    """
    return [(raw, norm) for raw, norm, _pct in _sig_tokens(text)]


def unsourced_numbers(answer: str, sources: str, limit: int = 6) -> list:
    """답변의 수치 중 도구 출력·사용자 발화 어디에도 없는 것.

    부분문자열로 대조한다 — '48039.32' 가 원문에 그대로 있으면 근거 있는 값으로 본다.
    부분 일치라 '232' 가 '1232' 에 걸려 통과하는 느슨함이 있는데, 이 방향의 오차는
    '근거 있다고 잘못 보는' 쪽이라 경고 남발보다 낫다(과다 경고는 기능을 죽인다).

    글자로 없으면 **같은 값의 다른 꼴**인지 본다(HWAXPortal docs/request-1125 OF-3 — 11-24 비교에서 어긋남 표시 21건을 하나씩 보니 모두
    오탐이었다: Pa → GPa · MPa 환산 20 · 반올림) — 천 단위 쉼표 · 숫자 묶음 사이 공백을 뗀 원문, SI 접두 ×10^(3k)(2,350,000,000 Pa ↔ 2.35 GPa),
    답의 소수 자리로 반올림(5717.116 ↔ 5,717.12), 비 ↔ %(0.125 ↔ 12.5%, % 가 한쪽에 붙었을 때만). 판정은 equivalent() 하나다 — 위임의 숫자
    대조(offload.numbers)도 같은 것을 쓴다.

    limit 은 돌려줄 건수 상한이고 **0 이하면 전부**다. 상한에서 끊으면 나머지가 있다는 것도 함께
    사라진다 — 7건이든 70건이든 6건으로 보였다. 총 건수를 알려야 하는 호출부(심의 결정문)는 전부
    받아서 보일 만큼만 보이고 건수를 적는다.
    """
    if not sources:
        return []
    src_norm = _src_flat(sources)
    pool = None
    seen, bad = set(), []
    for raw, norm, pct in _sig_tokens(answer):
        if norm in seen:
            continue
        seen.add(norm)
        if norm in src_norm:
            continue
        if pool is None:
            pool = SourcePool(sources)
        if pool.equivalent(norm, pct) is None:
            bad.append(raw)
        if 0 < limit <= len(bad):
            break
    return bad


# ── 같은 값의 다른 꼴(OF-3) ─────────────────────────────────────────────────────────────────────────────────────
# 원문 쪽만 넓힌다 — 숫자 묶음 사이 공백(2 350 000 · 얇은 공백)을 떼 '글자로 있다' 를 넓힐 뿐 답의 수를 잇지 않는다(답 쪽에서 이으면 '모델 3 100개'
# 가 3100 이 된다). 넓히는 쪽이라 어긋남을 새로 만들지 않는다.
_SRC_GROUP_SP = re.compile(r"(?<=\d)[ \u00a0\u2009\u202f](?=\d{3}(?!\d))")
_SRC_NUM_RE = re.compile(r"(?<![\w.])(?:\d{1,3}(?:[,\u00a0\u2009\u202f ]\d{3})+|\d+)(?:\.\d+)?(?![\d.])")
_PCT_AFTER = re.compile(r"\s?(?:%|％|퍼센트)")
_SI_EXPS = (3, -3, 6, -6, 9, -9, 12, -12)
SRC_VALUES_MAX = 200_000        # 원문에서 담는 수의 상한 — 넘으면 앞의 것만(넓히는 판정이라 덜 담으면 어긋남 쪽으로 남는다)


def _src_flat(sources: str) -> str:
    return _SRC_GROUP_SP.sub("", sources).replace(",", "")


def _decimals(norm: str) -> int:
    return len(norm.split(".", 1)[1]) if "." in norm else 0


class SourcePool:
    """원문의 수(값 · % 붙음)를 정렬해 둔 것 — 답의 수 하나가 같은 값의 다른 꼴로 있는지 이분 탐색으로 본다."""

    def __init__(self, sources: str):
        import bisect  # noqa: PLC0415
        self._bisect = bisect
        vals, pcts = [], []
        for m in _SRC_NUM_RE.finditer(sources or ""):
            try:
                v = float(re.sub(r"[,\u00a0\u2009\u202f ]", "", m.group(0)))
            except ValueError:
                continue
            vals.append(v)
            if _PCT_AFTER.match(sources, m.end()):
                pcts.append(v)
            if len(vals) >= SRC_VALUES_MAX:
                break
        self.values = sorted(set(vals))
        self.pcts = sorted(set(pcts))

    def _near(self, arr: list, target: float, tol: float):
        i = self._bisect.bisect_left(arr, target - tol)
        if i < len(arr) and arr[i] <= target + tol:
            return arr[i]
        return None

    def equivalent(self, norm: str, pct: bool = False) -> str | None:
        """답의 수(norm — 쉼표 뗀 글)가 원문의 어느 수와 같은 값인지 — 맞으면 그 꼴을 한 줄로('반올림 5717.116' · 'SI ×10^9 2350000000' ·
        '비 ↔ % 0.125'), 아니면 None. 반올림 폭은 답이 쓴 소수 자리의 반이다(정수면 0.5)."""
        try:
            a = float(norm)
        except ValueError:
            return None
        d = _decimals(norm)
        half = 0.5 * 10 ** (-d)
        tol = half * (1 + 1e-9) + 1e-12 * abs(a)
        b = self._near(self.values, a, tol)
        if b is not None:
            return f"반올림 {_fmt(b)}"
        for k in _SI_EXPS:
            f = 10.0 ** k
            b = self._near(self.values, a * f, tol * f)
            if b is not None:
                return f"SI 접두 10^{abs(k)} 배 {_fmt(b)}"
        if pct:
            b = self._near(self.values, a / 100.0, tol / 100.0)
            if b is not None:
                return f"비 ↔ % {_fmt(b)}"
        b = self._near(self.pcts, a * 100.0, tol * 100.0)
        if b is not None:
            return f"비 ↔ % {_fmt(b)}%"
        return None

    def suspect(self, norm: str) -> str:
        """같은 값이 아닌 수의 종류 — '환산 의심'(10 의 다른 거듭제곱 배로 맞는다) · '반올림 의심'(0.5% 안에 원문 수가 있다) · '원문에 없음'."""
        try:
            a = float(norm)
        except ValueError:
            return "원문에 없음"
        d = _decimals(norm)
        tol = 0.5 * 10 ** (-d) * (1 + 1e-9) + 1e-12 * abs(a)
        for k in (1, -1, 2, -2, 4, -4, 5, -5, 7, -7, 8, -8):
            f = 10.0 ** k
            if self._near(self.values, a * f, tol * f) is not None:
                return "환산 의심"
        if a and self._near(self.values, a, abs(a) * 0.005) is not None:
            return "반올림 의심"
        return "원문에 없음"


def _fmt(v: float) -> str:
    return f"{v:.12g}"


def derived_from(norm: str, bases: list) -> dict | None:
    """답의 수가 답에 함께 쓴 근거 수 둘로 계산한 값인지 — 합 · 차 · 비 · 증가율(%) · 곱. 맞으면 {op, from: [두 수]}, 아니면 None. 근거 수는
    원문 전체가 아니라 **답이 인용해 근거가 확인된 수**만 쓴다(원문 전체의 쌍이면 지어낸 수도 어느 쌍의 차로 맞는다)."""
    try:
        a = float(norm)
    except ValueError:
        return None
    d = _decimals(norm)
    tol = 0.5 * 10 ** (-d) * (1 + 1e-9) + 1e-12 * abs(a)
    for i, (rx, x) in enumerate(bases):
        for j, (ry, y) in enumerate(bases):
            if i == j:
                continue
            cands = (("합", x + y), ("차", x - y), ("곱", x * y))
            if y:
                cands += (("비", x / y), ("증가율 %", (x - y) / y * 100.0), ("비율 %", x / y * 100.0))
            for op, v in cands:
                if abs(v - a) <= tol:
                    return {"op": op, "from": [rx, ry]}
    return None


# ── 긴 문서를 예산에 맞추기 ──────────────────────────────────────────────────
# 추출기가 낱장마다 붙이는 표지 — '## [s.12]'(슬라이드) / '## [p.34]'(쪽).
_DOC_UNIT_RE = re.compile(r"^## \[([sp])\.(\d+)\]", re.M)


def fit_document(text: str, budget: int, head_ratio: float = 0.6) -> tuple:
    """긴 문서를 예산에 맞춘다 — **앞에서 자르지 않고 가운데를 덜어낸다.**

    발표자료·보고서는 결론이 뒤에 있다. 앞에서 budget 만큼 잘라 넣으면 배경만 읽고
    결론을 못 본 채로 답하게 되는데, 그 답은 겉보기에 멀쩡하다(실측 120슬라이드에서
    앞 60,000자만 남기면 결론 슬라이드가 통째로 사라진다). 그래서 앞뒤를 남기고
    가운데를 뺀다. 자르는 자리는 **낱장 경계**라 슬라이드가 반토막 나지 않고, 무엇이
    빠졌는지 낱장 번호로 말해 줄 수 있다 — 모델이 되물을 수 있어야 지어내지 않는다.

    챗(app._doc_block)과 심의(deliberation 근거 정규화)가 같이 쓴다. 한쪽만 고치면
    같은 문서가 화면마다 다르게 잘린다.

    반환: (실을 본문, 사람에게 보일 한 줄 설명 — 온전하면 빈 문자열)
    """
    if budget <= 0 or len(text) <= budget:
        return text, ""
    marks = list(_DOC_UNIT_RE.finditer(text))
    if len(marks) < 4:
        # 표지가 없는 평문(사람이 직접 만든 메모, 도구 결과 등)은 낱장 경계를 모른다.
        return text[:budget], f"앞 {budget:,}자만 실림(낱장 표지가 없어 글자 수로 잘랐다)"

    starts = [m.start() for m in marks] + [len(text)]
    labels = [f"{m.group(1)}.{m.group(2)}" for m in marks]

    head_cap = int(budget * head_ratio)
    hi = 0                                   # 앞에서 담을 낱장 수
    while hi < len(marks) and starts[hi + 1] <= head_cap:
        hi += 1
    lo = len(marks)                          # 뒤에서 담기 시작할 낱장 index
    tail_cap = budget - starts[hi]
    while lo > hi and len(text) - starts[lo - 1] <= tail_cap:
        lo -= 1

    if lo <= hi:                             # 낱장 하나가 예산보다 큰 경우
        return text[:budget], f"앞 {budget:,}자만 실림"

    dropped = f"{labels[hi]}~{labels[lo - 1]}" if lo - hi > 1 else labels[hi]
    note = (f"\n\n[⋯ 가운데 {lo - hi}낱장({dropped})은 길이 때문에 여기 실리지 않았다. "
            "이 구간의 내용을 묻거든 '못 봤다' 고 말하고 그 부분만 따로 붙여 달라고 하라 — "
            "추측으로 메우지 마라]\n\n")
    return text[:starts[hi]] + note + text[starts[lo]:], f"{dropped} {lo - hi}낱장이 빠짐"
