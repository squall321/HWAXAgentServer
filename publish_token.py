# Report Archive 게시 확인 토큰(confirm_token)을 턴 밖으로 나가는 글(심의 근거 · /보고서 · 확인 카드)에서 가리는 도우미
"""RA 의 게시 미리보기(preview_publish)는 600초 동안 그 보고서를 그 게시판에 올릴 수 있는 확인 토큰(`<만료초>.<서명 43자>`, 54자 —
ReportArchive mounts/confirm.py)을 준다. 챗의 다음 턴 게시(publish_report)는 그 값을 **저장된 대화 이력**에서 읽으므로 대화 저장소 · 이력 근거는
가리지 않는다(HWAXPortal 절차 W-106, 사용자 결정 10-31 '내보내는 곳만 가린다'). 그 값이 턴보다 오래 남고 진짜 값이 필요 없는 곳으로 흘러갈 때만
가린다 — 심의가 받는 챗 근거 · 대화 맥락(좌석 · 결정문 · 보고서 · 위키가 본다), '/보고서' 가 Report Archive 에 남기는 대화, 답 본문에 찍히는 확인
카드 글.

규칙은 포털 절차 실행기와 같다(HWAXPortal backend/app/procedures/runner.py `_mask_publish_token` · `_redact`, from_chat.py `TOKEN_ECHO_MIN`) —
JSON 칸 `"confirm_token": "<값>"`(JSON 글 안에 한 겹 더 싸여 따옴표 앞에 역빗금이 붙어도)의 값을 `[가림:confirm_token]` 으로 바꾸고, 그렇게 찾은
값이 20자 이상이면 같은 글의 다른 자리(검증 오류가 받은 인자를 파이썬 꼴 `'confirm_token': '…'` 로 되비친 곳 따위)도 글자 그대로 가린다. 칸 이름으로
가린다 — 노출 이름에는 앱 접두가 붙을 수 있고(`reportarchive_preview_publish`) 이 칸은 RA 게시 확인뿐이다. 짧은 값(모델이 지어낸 '1' · 'yes')은
RA 가 거절해 살아 있지 않고, 글자 그대로 가리면 같은 글자가 든 숫자 · 낱말까지 지워져 칸만 가린다."""
from __future__ import annotations

import re

FIELD = "confirm_token"
MASK = f"[가림:{FIELD}]"
TOKEN_ECHO_MIN = 20
_FIELD_RE = re.compile(r'(\\*"' + FIELD + r'\\*"\s*:\s*\\*")([^"\\]+)')


def found(*texts) -> set:
    """글들에서 확인 토큰 칸의 값 — 이미 가린 값은 뺀다."""
    return {m.group(2) for t in texts if isinstance(t, str) and t for m in _FIELD_RE.finditer(t)} - {MASK}


def mask(text, tokens=()):
    """글 하나를 가린다 — 칸의 값과, 그 글 · tokens(같은 묶음의 다른 글에서 찾은 값)의 20자 이상 값이 되비친 자리. 글이 아니면 그대로 돌려준다."""
    if not isinstance(text, str) or not text:
        return text
    echo = {x for x in (found(text) | set(tokens or ())) if len(x) >= TOKEN_ECHO_MIN}
    out = _FIELD_RE.sub(lambda m: m.group(1) + MASK, text)
    for tok in sorted(echo, key=len, reverse=True):
        out = out.replace(tok, MASK)
    return out
