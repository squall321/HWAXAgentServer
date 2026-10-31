# 전문가 고르기 기준선의 박스 자리 · 공유 비밀 — 평가 창구 · 실사용 질문 · 그림자 기록 · 평가셋이 함께 쓰는 자리와 파일 쓰기(HWAXPortal docs/request-1029 design-e5)
"""자리 — SEAT_EVAL_DIR, 없으면 잡 원장(DELIB_JOB_DIR)의 형제 seat-eval/(리포 밖 — /data). 아래 sets/ · runs/ · questions/ · shadow/ 를 둔다.
폴더는 0700, 파일은 0600 이다(질문 원문 · 사람 표시가 든다 — 잡 원장과 같은 권한).

공유 비밀 — 게이트웨이 연결의 Authorization 값(Bearer <GW_TOKEN>) 그대로다. 포털 GATEWAY_SHARED_TOKEN 과 같은 값이라 박스에 새로 채울 것이 없고,
게이트웨이 /search-eval 과 같은 꼴이다. /drain · /limits 처럼 루프백만 믿지 않는 까닭 — 이 창구들은 질문 원문을 내거나 AIDataHub 를 수백 번 부른다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))


def box_dir() -> Path:
    """기준선 박스 자리 — 원장 자리 셈은 delib_jobs.JOB_DIR 과 같다(ARTIFACT_DIR 을 realpath 로 풀고 그 부모 — 리포의 artifacts 는 /data 로 가는
    심링크다). 이관하지 않은 박스에서는 리포 안 seat-eval/ 이 되고 .gitignore 가 막는다."""
    raw = os.environ.get("SEAT_EVAL_DIR", "").strip()
    if raw:
        return Path(raw)
    jd = os.environ.get("DELIB_JOB_DIR", "").strip()
    if jd:
        return Path(jd).parent / "seat-eval"
    art = os.environ.get("ARTIFACT_DIR") or str(ROOT / "artifacts")
    return Path(os.path.realpath(art).rstrip("/")).parent / "seat-eval"


def private_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def append_jsonl(path: Path, rec: dict) -> None:
    """한 줄 덧붙인다 — 파일은 0600 으로 만든다."""
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def drop_old(d: Path, pat: re.Pattern, days: int, today=None) -> int:
    """날짜 파일(pat 의 첫 무리가 YYYY-MM-DD) 가운데 days 일보다 오랜 것을 지운다 — 굴러가는 창(게이트웨이 질의 기록과 같은 꼴)."""
    today = today or datetime.now(KST).date()
    n = 0
    try:
        files = list(d.iterdir())
    except OSError:
        return 0
    for f in files:
        m = pat.match(f.name)
        if not m:
            continue
        try:
            if (today - datetime.strptime(m.group(1), "%Y-%m-%d").date()).days >= days:
                f.unlink(missing_ok=True)
                n += 1
        except (ValueError, OSError):
            continue
    return n


def norm(text: str) -> str:
    """질문 글의 맞대기 꼴 — 소문자 · 공백 하나. 질문 id(H-…) · 그림자 기록의 qh 가 이 꼴의 sha256 이다."""
    return " ".join(str(text or "").lower().split())


def qhash(text: str) -> str:
    return hashlib.sha256(norm(text).encode("utf-8")).hexdigest()[:12]


def secret(app) -> str:
    """창구의 공유 비밀 — 게이트웨이 연결의 Authorization 값 그대로. 연결이 여럿이면 이름 gateway 를 먼저 본다."""
    conns = getattr(getattr(app, "state", None), "connections", None) or {}
    for n in sorted(conns, key=lambda n: (n != "gateway", n)):
        h = (conns.get(n) or {}).get("headers") or {}
        v = str(h.get("Authorization") or h.get("authorization") or "")
        if v.startswith("Bearer ") and len(v) > len("Bearer "):
            return v
    return ""


def authorized(app, header: str) -> tuple[int, str]:
    """(상태, 까닭) — 0 이면 통과. 비밀이 없는 박스는 503(무엇을 채울지 말한다), 다르면 401."""
    want = secret(app)
    if not want:
        return 503, "공유 비밀이 없다 — MCP_CONFIG 의 게이트웨이 연결에 Authorization(Bearer <GW_TOKEN>)이 없다"
    if not hmac.compare_digest(str(header or "").encode("latin-1", "replace"), want.encode("latin-1", "replace")):
        return 401, "unauthorized"
    return 0, ""
