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

