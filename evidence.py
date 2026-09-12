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
_NUM_TOK_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\d.])")


def sig_numbers(text: str) -> list:
    """대조할 가치가 있는 수치만. 오탐을 줄이려고 범위를 좁게 잡는다.

    작은 정수(0~99)는 개수·순번·백분율로 정상 생성되는 값이라 제외한다 — 여기까지 잡으면
    경고가 남발돼 표시 자체를 아무도 안 보게 된다. 소수점이 있거나 100 이상인 값만 본다.
    """
    out = []
    for m in _NUM_TOK_RE.finditer(text or ""):
        raw = m.group(0)
        norm = raw.replace(",", "")
        try:
            val = float(norm)
        except ValueError:
            continue
        if "." not in norm and val < 100:
            continue
        out.append((raw, norm))
    return out


def unsourced_numbers(answer: str, sources: str, limit: int = 6) -> list:
    """답변의 수치 중 도구 출력·사용자 발화 어디에도 없는 것.

    부분문자열로 대조한다 — '48039.32' 가 원문에 그대로 있으면 근거 있는 값으로 본다.
    부분 일치라 '232' 가 '1232' 에 걸려 통과하는 느슨함이 있는데, 이 방향의 오차는
    '근거 있다고 잘못 보는' 쪽이라 경고 남발보다 낫다(과다 경고는 기능을 죽인다).
    """
    if not sources:
        return []
    src_norm = sources.replace(",", "")
    seen, bad = set(), []
    for raw, norm in sig_numbers(answer):
        if norm in seen:
            continue
        seen.add(norm)
        if norm not in src_norm:
            bad.append(raw)
        if len(bad) >= limit:
            break
    return bad


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
