# 보고서(/보고서 · 심의 보고서)에 실을 글 속 도구 그림을 그 사람 명의로 Report Archive 에 올려 그림 칸으로 바꾸는 모듈(HWAXPortal docs/report-figures RF-9 ~ RF-12)
"""사용자 신고(2026-11-02) — StepForge · DynaForge 도구가 낸 그림이 보고서에 한 장도 들어가지 않는다.

웹 챗은 도구 그림을 ARTIFACT_DIR/<sha256 앞 20자>.<확장자> 에 두고 `/agent/artifacts/…` 로 보인다. 보고서는 글만 블록으로 만들어 그림이
포털 상대 주소를 가리키는 마크다운 글자로 RA 에 들어갔다(RA 에서는 깨진 글이다). 여기서 보고서를 만들기 **전에** 블록 글의 그림 줄을 찾아
그 사람의 PAT 로 포털 `POST /agent/figures/upload` 에 올리고(포털이 그 사람의 RA 토큰으로 RA `/api/files` 에 넣는다), 그 자리를
`[그림 N — 설명]` · 못 올렸으면 `[그림 — 올리지 못함: 까닭]` 으로 바꾼 뒤, 그림 칸(RA image 위젯)을 보고서를 만드는 같은 호출의
`extra_blocks` 로 돌려준다. 그림을 조용히 버리지 않는다 — 못 올린 것은 그 자리에 까닭이 남고 결과에 센다.

  · 찾는 것 — 마크다운 그림 `![설명](/agent/artifacts/<이름>)` 과 맨 주소 `/agent/artifacts/<이름>`(도구 결과의 '[생성된 이미지 URL …]' 줄 ·
    captured.images[].url — 에이전트가 저장한 그림 블록이 글에 남는 꼴). 처음 나온 차례, 같은 그림은 한 번(이름 · 내용 해시 둘 다로 묶는다).
  · 자격 — 사람 PAT 만. 게이트웨이 공유 비밀(GW_TOKEN)로는 올리지 않는다 — PAT 가 없는 길(MCP 로 연 심의)은 까닭을 남긴다(RF-10).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field

import httpx

# 포털 — 모든 박스에서 같은 자리(HWAXPortal infra/scripts/_common.sh PORTAL_PORT). 게이트웨이 주소 GATEWAY_HTTP_BASE 와 같은 꼴의 손잡이.
PORTAL_HTTP_BASE = os.environ.get("PORTAL_HTTP_BASE", "http://127.0.0.1:8723")
PORTAL_TRANSPORT: httpx.AsyncBaseTransport | None = None   # 시험 이음매 — None 이 아니면 포털로 가는 요청이 전부 이 전송을 탄다
FIGURES_MAX = 40          # 한 보고서의 그림 칸 — RA 한 쪽 블록 상한(200, AI_MAX_BLOCKS_PER_PAGE)에 템플릿 칸을 두고 넉넉히(RF-11)
# 한 장 — 포털이 RA 에 올리는 한도(300초)보다 길게 둔다. 안쪽이 먼저 걸려야 까닭이 온다(시간 제한은 층이다, RF-12)
UPLOAD_TIMEOUT = httpx.Timeout(330.0, connect=10.0)
_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
         "svg": "image/svg+xml"}
_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}?\.(?:png|jpe?g|gif|webp|svg)\b"
# 마크다운 그림(1: 설명 · 2: 이름) 또는 맨 주소(3: 이름). 앞에 붙은 절대 주소(https://포털/agent/artifacts/…)도 함께 바꾼다.
_REF_RE = re.compile(r"!\[([^\]\n]{0,300})\]\(\s*(?:https?://[^\s/()]+)?/agent/artifacts/(" + _NAME + r")\s*\)"
                     r"|(?:https?://[^\s/()\]]+)?/agent/artifacts/(" + _NAME + r")", re.I)
_CAPTION_MAX, _ALT_MAX = 500, 200          # RA image 위젯 files[].caption · alt 상한
NO_PAT = "그 사람의 토큰이 없는 길(MCP 로 연 심의 등)이라 올리지 않았다 — 서비스 자격으로는 그림을 올리지 않는다"
# 그림 하나만의 문제인 포털 답(꼴 · 크기 · 해시 · RA 가 그 파일만 거절) — 그 밖(자격 · 연결 · RA 오류)은 남은 그림도 같으니 더 부르지 않는다
_PER_FIGURE = frozenset({"too_large", "unsupported_media_type", "sha256_mismatch", "empty_file", "bad_meta", "ra_rejected"})


def _items(blocks: dict):
    for v in (blocks or {}).values():
        for it in (v if isinstance(v, list) else []):
            if isinstance(it, str):
                yield it


def _scan(blocks: dict) -> list[tuple[str, str]]:
    """블록 글의 그림 — [(이름, 처음 나온 설명)] 처음 나온 차례, 이름마다 하나."""
    out: dict[str, str] = {}
    for text in _items(blocks):
        for m in _REF_RE.finditer(text):
            name = m.group(2) or m.group(3)
            if not out.get(name):
                out[name] = (m.group(1) or "").strip()
    return list(out.items())


def count(blocks: dict) -> int:
    """블록 글에 그림이 몇 장인가(이름으로 셈) — 0 이면 attach 를 부르지 않아도 된다."""
    return len(_scan(blocks))


@dataclass
class Figures:
    """attach 의 결과 — 표지로 바꾼 블록 · 그림 칸 · 셈."""
    blocks: dict
    extra_blocks: list = field(default_factory=list)
    found: int = 0
    failed: list = field(default_factory=list)       # [{"ref", "reason"}]
    _ok: list = field(default_factory=list)          # [(번호, 칸 id, 주소)] — 그림 칸으로 보낸 것

    @property
    def attached(self) -> int:
        return len(self._ok)

    def saved(self, made) -> None:
        """보고서를 만든 뒤 — RA 가 우리 칸을 경고로 버렸으면(경고 글에 칸 id) 그 그림을 '못 들어감' 으로 옮긴다(RF-7 의 이쪽)."""
        warns = [str(w) for w in ((made or {}).get("warnings") or [])] if isinstance(made, dict) else []
        keep = []
        for n, bid, ref in self._ok:
            hit = next((w for w in warns if bid in w), None)
            if hit is None:
                keep.append((n, bid, ref))
            else:
                self.failed.append({"ref": ref, "reason": f"Report Archive 가 그림 {n} 의 칸을 받지 않았다({hit[:200]})"})
        self._ok = keep

    def not_saved(self) -> None:
        """보고서가 저장되지 않았다 — 올린 그림도 보고서에는 없다."""
        self.failed += [{"ref": ref, "reason": "보고서가 저장되지 않았다"} for _n, _b, ref in self._ok]
        self._ok = []

    def summary(self) -> dict:
        """결과 · outcome 사건에 싣는 셈 — 찾은 수 · 그림 칸으로 들어간 수 · 못 들어간 수와 까닭(앞 다섯)."""
        return {"found": self.found, "attached": self.attached, "failed": len(self.failed),
                "reasons": [f["reason"] for f in self.failed][:5]}

    def note(self) -> str:
        """사람에게 — 몇 장이 들어갔고 몇 장이 못 들어갔나(까닭 하나)."""
        k, f = self.attached, len(self.failed)
        why = f" — 예: {self.failed[0]['reason']}" if f else ""
        if not f:
            return f"🖼 그림 {k}장을 보고서에 그림 칸으로 실었다."
        if not k:
            return f"⚠ 그림 {self.found}장을 보고서에 싣지 못했다 — 그 자리에 까닭을 적었다{why}."
        return f"🖼 그림 {self.found}장 중 {k}장을 보고서에 그림 칸으로 실었다 — {f}장은 싣지 못해 그 자리에 까닭을 적었다{why}."


async def _upload(cli: httpx.AsyncClient, user_pat: str, name: str, raw: bytes, sha: str, alt: str) -> tuple[str | None, str, bool]:
    """포털 /agent/figures/upload — (file_id, '', False) 또는 (None, 까닭, 남은 그림도 멈출까)."""
    meta = {"caption": alt, "tool": "", "mime": _MIME[name.rsplit(".", 1)[-1].lower()], "sha256": sha, "filename": name,
            "ref": f"/agent/artifacts/{name}"}
    try:
        r = await cli.post(f"{PORTAL_HTTP_BASE.rstrip('/')}/agent/figures/upload", headers={"Authorization": f"Bearer {user_pat}"},
                           data={"meta": json.dumps(meta, ensure_ascii=False)}, files={"file": (name, raw, meta["mime"])})
    except httpx.HTTPError as exc:
        return None, f"포털에 닿지 못했다({type(exc).__name__})", True
    try:
        j = r.json()
    except ValueError:
        j = None
    if r.status_code == 200 and isinstance(j, dict) and j.get("ok") and j.get("file_id"):
        return str(j["file_id"]), "", False
    d = j.get("detail") if isinstance(j, dict) else None
    if isinstance(d, dict):                       # 포털 AuthError 의 code 꼴 — {"detail": {"code", "message"}}
        d = d.get("message")
    why = str(d or "포털이 받지 않았다").strip().rstrip(".")[:200]
    return None, f"{why}(HTTP {r.status_code})", not (isinstance(j, dict) and j.get("error") in _PER_FIGURE)


async def attach(blocks: dict, *, user_pat: str, artifact_dir: str | None = None) -> Figures:
    """블록 글의 그림을 올리고 표지로 바꾼다 — 실패해도 예외를 올리지 않는다(그림마다 까닭이 남는다). 그림이 없으면 blocks 를 그대로 돌려준다."""
    found = _scan(blocks)
    if not found:
        return Figures(blocks=blocks)
    if artifact_dir is None:
        from app import ARTIFACT_DIR as artifact_dir  # noqa: PLC0415 — app 이 이 모듈을 늦게 읽는다(순환 import 회피)
    res: dict[str, tuple] = {}      # 이름 → ("ok", 번호, 칸) | ("fail", 까닭)
    by_sha: dict[str, str] = {}     # 내용 해시 → 먼저 올린 이름(이름이 달라도 같은 그림은 칸 하나)
    out = Figures(blocks=blocks, found=len(found))
    n, stop = 0, NO_PAT if not user_pat else ""
    async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT, transport=PORTAL_TRANSPORT) as cli:
        for name, alt in found:
            try:
                if stop:                    # 사람 토큰이 없거나 앞 그림이 자격 · 연결에서 막혔다 — 남은 그림도 같은 까닭이다
                    res[name] = ("fail", stop)
                    continue
                if n >= FIGURES_MAX:
                    res[name] = ("fail", f"보고서 하나에 그림은 {FIGURES_MAX}장까지 싣는다 — 나머지는 대화에서 본다")
                    continue
                p = os.path.join(artifact_dir, name)
                if not os.path.isfile(p):
                    res[name] = ("fail", "그림 파일이 서버에 없다 — 오래된 도구 그림은 지워진다(최근 것만 남긴다)")
                    continue
                with open(p, "rb") as f:
                    raw = f.read()
                sha = hashlib.sha256(raw).hexdigest()
                if sha in by_sha:
                    res[name] = res[by_sha[sha]]
                    continue
                fid, why, fatal = await _upload(cli, user_pat, name, raw, sha, alt)
                if fid is None:
                    res[name] = ("fail", why)
                    stop = why if fatal else ""
                    continue
                n += 1
                cap = (f"그림 {n} — {alt}" if alt else f"그림 {n}")
                if len(cap) > _CAPTION_MAX:
                    cap = cap[:_CAPTION_MAX - 1] + "…"
                blk = {"id": f"fig_{sha[:16]}", "type": "image", "props": {"max_count": 1},
                       "content": {"files": [{"file_id": fid, "caption": cap, "alt": (alt or "도구 그림")[:_ALT_MAX]}]}}
                res[name] = ("ok", n, blk)
                by_sha[sha] = name
                out.extra_blocks.append(blk)
                out._ok.append((n, blk["id"], f"/agent/artifacts/{name}"))
            except Exception as exc:  # noqa: BLE001 — 그림 하나의 실패가 보고서를 막지 않는다(까닭은 그 자리에 남는다)
                res[name] = ("fail", f"올리는 중 오류({type(exc).__name__})")
    out.failed = [{"ref": f"/agent/artifacts/{name}", "reason": r[1]} for name, r in res.items() if r[0] == "fail"]

    def mark(m: re.Match) -> str:
        r = res[m.group(2) or m.group(3)]          # 같은 정규식으로 찾은 이름이라 늘 있다
        if r[0] == "fail":
            return f"[그림 — 올리지 못함: {r[1]}]"
        alt = (m.group(1) or "").strip()
        return f"[그림 {r[1]} — {alt}]" if alt else f"[그림 {r[1]}]"

    out.blocks = {k: ([_REF_RE.sub(mark, it) if isinstance(it, str) else it for it in v] if isinstance(v, list) else v)
                  for k, v in blocks.items()}
    return out
