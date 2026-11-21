# Report Archive 초안을 만들 때 쓰는 템플릿 id · 판 — 기본은 손으로 둔 'deliberation' v1, 박스 파일이 있으면 스스로 등록한 형식
"""RA 템플릿 고르기(HWAXPortal docs/leftovers-1121 L21-6 ① — 사용자 결정 2026-11-21, 기본 끔).

심의 보고서 · `/보고서` 는 RA 의 `template_id="deliberation"`(v1)에 저장한다. 그 템플릿은 RA 코드 · 이행에 없고 RA 데이터베이스에 손으로
만들어 둔 것이라, 없는 박스에서는 저장이 실패한다. RA v0.176.0 부터 시스템이 자기 형식을 스스로 등록할 수 있다
(`register_report_format` → `sys.<system>.<kind>`, 멱등). 포털의 `infra/scripts/ra-register-format.py` 가 등록하고 그 결과를 박스 파일에
적으면 여기서 그 id · 판으로 바꿔 올린다.

  박스 파일  <포털 리포>/infra/ra-formats.local.json(gitignore)   {"deliberation": {"template_id": "sys.hwax.deliberation", "template_version": 1}}

**파일이 없으면 지금과 바이트까지 같다** — 기본값이 종전에 코드에 박혀 있던 글자 그대로다. 파일이 깨졌거나 그 종류의 줄이 꼴에 안 맞으면
그 줄만 버리고 기본으로 돌며 까닭을 `/health` 의 `ra_formats.error` 에 적는다(틀릴 거면 종전 동작 쪽으로 틀린다). 바뀐 시각 · 크기가 다르면
다시 읽는다 — 재기동이 필요 없다. 게이트웨이(분석 루틴 보고서)가 같은 파일을 같은 규칙으로 읽는다(포털 시험이 두 쪽을 맞댄다).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# 종류 → (template_id, template_version). 종전에 호출부에 박혀 있던 값이다 — 바꾸면 박스 파일 없는 박스의 동작이 바뀐다.
DEFAULTS: dict[str, tuple[str, int]] = {"deliberation": ("deliberation", 1)}
FILE_NAME = "ra-formats.local.json"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")      # RA TemplateCreate 의 id 꼴(영숫자 · `.` · `_` · `-`)

_ENV = os.environ.get("RA_FORMATS_PATH", "").strip()
# 포털 · 에이전트 서버는 늘 한 박스에 형제 리포로 있다(dev ~/claude · cae00 ~/Projects) — 운영자 정의 · 코어 어휘표와 같은 선례. 상대 경로는 이 리포 루트가 앵커다.
PATH = (Path(__file__).resolve().parent / Path(_ENV).expanduser() if _ENV
        else Path(__file__).resolve().parent.parent / "HWAXPortal" / "infra" / FILE_NAME)

_STATE: dict = {"key": "아직 읽지 않음", "map": {}, "error": None}


def _load() -> dict:
    try:
        st = PATH.stat()
        key = (str(PATH), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(PATH), None, None)
    if key == _STATE["key"]:
        return _STATE
    got: dict[str, tuple[str, int]] = {}
    error = None
    if key[1] is not None:
        try:
            doc = json.loads(PATH.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("객체가 아니다")
        except (OSError, ValueError) as exc:
            error = f"못 읽음({type(exc).__name__})"
            doc = {}
        bad = []
        for kind, row in doc.items():
            if kind.startswith("_"):            # _doc 같은 메모 칸
                continue
            tid = row.get("template_id") if isinstance(row, dict) else None
            ver = row.get("template_version") if isinstance(row, dict) else None
            if (kind in DEFAULTS and isinstance(tid, str) and _ID_RE.fullmatch(tid)
                    and isinstance(ver, int) and not isinstance(ver, bool) and ver >= 1):
                got[kind] = (tid, ver)
            else:
                bad.append(str(kind)[:40])
        if bad:
            error = f"꼴이 틀리거나 모르는 종류 {', '.join(sorted(bad))} — 그 줄은 기본값으로 돈다"
        if error:
            print(f"[ra-formats] ⚠ {PATH.name} — {error}", flush=True)
    _STATE.update(key=key, map=got, error=error)
    return _STATE


def template(kind: str = "deliberation") -> tuple[str, int]:
    """그 종류의 (template_id, template_version) — 박스 파일에 맞는 줄이 있으면 그것, 아니면 기본."""
    return _load()["map"].get(kind) or DEFAULTS[kind]


def draft_args(kind: str = "deliberation") -> dict:
    """create_report_draft 인자의 템플릿 두 칸."""
    tid, ver = template(kind)
    return {"template_id": tid, "template_version": ver}


def health() -> dict:
    """/health 용 — 종류마다 지금 쓰는 id · 판과 그것이 기본인지 박스 파일인지."""
    st = _load()
    return {"file": st["key"][1] is not None, "error": st["error"],
            "kinds": {k: {"template_id": template(k)[0], "template_version": template(k)[1],
                          "source": "box" if k in st["map"] else "default"} for k in sorted(DEFAULTS)}}
