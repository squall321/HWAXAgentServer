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
  · AIDataHub 첨부 그림(`/ai-data-hub/attachments/<경로>` — 지식카드에 붙은 그림)도 같은 길로 싣는다(사용자 결정 2026-11-22 — RF-13 의 미룸을
    뒤집음, HWAXPortal docs/leftovers-1121 L21-10 ④). 바이트는 챗의 그림 읽기(vision.load)와 같은 자리에서 받는다(AIDataHub 의 첨부 길 · 8 MB 까지).
    래스터만이다. 끄려면 REPORT_FIG_ATTACHMENTS=0 — 그때 첨부 주소는 종전처럼 글에 그대로 남는다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass, field

import httpx

import vision

# 포털 — 모든 박스에서 같은 자리(HWAXPortal infra/scripts/_common.sh PORTAL_PORT). 게이트웨이 주소 GATEWAY_HTTP_BASE 와 같은 꼴의 손잡이.
PORTAL_HTTP_BASE = os.environ.get("PORTAL_HTTP_BASE", "http://127.0.0.1:8723")
PORTAL_TRANSPORT: httpx.AsyncBaseTransport | None = None   # 시험 이음매 — None 이 아니면 포털로 가는 요청이 전부 이 전송을 탄다
FIGURES_MAX = 40          # 한 보고서의 그림 칸 — RA 한 쪽 블록 상한(200, AI_MAX_BLOCKS_PER_PAGE)에 템플릿 칸을 두고 넉넉히(RF-11)
# 한 장 — 포털이 RA 에 올리는 한도(300초)보다 길게 둔다. 안쪽이 먼저 걸려야 까닭이 온다(시간 제한은 층이다, RF-12)
UPLOAD_TIMEOUT = httpx.Timeout(330.0, connect=10.0)
_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
         "svg": "image/svg+xml"}
# 이름 끝은 영숫자 · _ 가 아니면 된다 — \b 는 한글을 낱말 글자로 봐서 '…png에' 를 놓쳤다(반대 검토 F1).
_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}?\.(?:png|jpe?g|gif|webp|svg)(?![A-Za-z0-9_])"
# 그림의 자리 둘 — 에이전트가 저장한 도구 그림 · AIDataHub 첨부(래스터만 — RA 는 SVG 를 그림으로 받지 않는다).
_ART_HEAD, _ATT_HEAD = "/agent/artifacts/", "/ai-data-hub/attachments/"
_PATH = (r"(" + re.escape(_ART_HEAD) + _NAME + r"|" + re.escape(_ATT_HEAD)
         + r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,200}?\.(?:png|jpe?g|gif|webp)(?![A-Za-z0-9_]))")
# 마크다운 그림(1: 설명 · 2: 주소 — 꺾쇠 <주소> · 제목 "…" 도) 또는 맨 주소(3: 주소). 앞에 붙은 절대 주소(https://포털/agent/artifacts/…)도 함께 바꾼다.
_REF_RE = re.compile(r"!\[([^\]\n]{0,300})\]\(\s*<?\s*(?:https?://[^\s/()<>]+)?" + _PATH + r"\s*>?"
                     r"(?:\s+(?:\"[^\"\n]{0,300}\"|'[^'\n]{0,300}'))?\s*\)"
                     r"|(?:https?://[^\s/()\]<>]+)?" + _PATH, re.I)
ATTACHMENTS = os.environ.get("REPORT_FIG_ATTACHMENTS", "1").strip() != "0"     # AIDataHub 첨부 그림도 싣는다(기본 켬 — 사용자 결정 11-22)


def _is_art(ref: str) -> bool:
    return ref.lower().startswith(_ART_HEAD)


def _wanted(ref: str) -> bool:
    """이 주소를 그림 칸으로 바꿀 것인가 — 도구 그림은 늘, 첨부는 켜져 있고 경로가 위로 벗어나지 않을 때만."""
    return _is_art(ref) or (ATTACHMENTS and ".." not in ref)
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
    """블록 글의 그림 — [(주소, 처음 나온 설명)] 처음 나온 차례, 주소마다 하나. 주소는 포털 상대 경로다(/agent/artifacts/… · /ai-data-hub/attachments/…)."""
    out: dict[str, str] = {}
    for text in _items(blocks):
        for m in _REF_RE.finditer(text):
            ref = m.group(2) or m.group(3)
            if _wanted(ref) and not out.get(ref):
                out[ref] = (m.group(1) or "").strip()
    return list(out.items())


def count(blocks: dict) -> int:
    """블록 글에 그림이 몇 장인가(이름으로 셈) — 0 이면 attach 를 부르지 않아도 된다."""
    return len(_scan(blocks))


def uncited(blocks: dict, urls, label=None, *, upload: bool = True) -> tuple[str, list[str]]:
    """에이전트가 저장한 그림(urls — /agent/artifacts/<이름>, 처음 나온 차례) 중 블록 글에 주소가 없는 것 → (블록에 더할 한 항목, 그 주소들).
    없으면 ('', []). label(주소 → 설명)이 주는 설명을 그림 줄에 단다(그 그림을 받은 좌석 · 도구). 심의는 좌석 조회가 받은 그림을 발언에 주소로 적지
    않는다 — 이 항목이 없으면 그 그림들은 보고서에 길이 없다(RF-16). upload 가 거짓이면(사람 토큰이 없다) 그림 줄 대신 장수와 까닭 한 줄이다 —
    같은 까닭의 '올리지 못함' 줄을 그림마다 늘어놓지 않는다(반대 검토 F2). 그때 주소들은 부른 쪽이 셈(add_failed)에 넣는다."""
    have = {n for n, _a in _scan(blocks)}
    refs, lines = [], []
    for u in dict.fromkeys(urls or []):
        m = _REF_RE.fullmatch(u) if isinstance(u, str) else None
        if m is None or m.group(3) is None or not _is_art(m.group(3)) or m.group(3) in have:
            continue
        alt = re.sub(r"[\]\[\n]+", " ", str((label(u) if label else "") or "심의 중 도구 그림")).strip()[:120]
        refs.append(u)
        lines.append(f"![{alt}]({u})")
    if not refs:
        return "", []
    if not upload:
        return f"심의 중 도구가 낸 그림 {len(refs)}장(본문에 주소가 없던 것) — [그림 — 올리지 못함: {NO_PAT}]", refs
    return "심의 중 도구가 낸 그림(본문에 주소가 없던 것):\n" + "\n".join(lines), refs


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

    def add_failed(self, refs, reason: str) -> None:
        """글에 줄로 싣지 않고 한 줄로 센 그림(uncited 의 upload=False) — 찾은 수 · 못 들어간 수에 넣는다."""
        self.found += len(refs)
        self.failed += [{"ref": r, "reason": reason} for r in refs]

    def summary(self) -> dict:
        """결과 · outcome 사건에 싣는 셈 — 찾은 수 · 그림 칸으로 들어간 수 · 못 들어간 수와 까닭(다른 것만 앞 다섯) · 못 들어간 그림의 주소(앞 스물 —
        어느 그림인지 가리킨다, 반대 검토 F2)."""
        return {"found": self.found, "attached": self.attached, "failed": len(self.failed),
                "reasons": list(dict.fromkeys(f["reason"] for f in self.failed))[:5],
                "failed_refs": [f["ref"] for f in self.failed][:20]}

    def note(self) -> str:
        """사람에게 — 몇 장이 들어갔고 몇 장이 못 들어갔나(까닭 하나)."""
        k, f = self.attached, len(self.failed)
        why = f" — 예: {self.failed[0]['reason']}" if f else ""
        if not f:
            return f"🖼 그림 {k}장을 보고서에 그림 칸으로 실었다."
        if not k:
            return f"⚠ 그림 {self.found}장을 보고서에 싣지 못했다 — 그 자리에 까닭을 적었다{why}."
        return f"🖼 그림 {self.found}장 중 {k}장을 보고서에 그림 칸으로 실었다 — {f}장은 싣지 못해 그 자리에 까닭을 적었다{why}."


async def _upload(cli: httpx.AsyncClient, user_pat: str, name: str, raw: bytes, sha: str, alt: str,
                  ref: str | None = None) -> tuple[str | None, str, bool]:
    """포털 /agent/figures/upload — (file_id, '', False) 또는 (None, 까닭, 남은 그림도 멈출까). ref 는 그 그림의 원래 주소(감사 기록용 —
    없으면 도구 그림 주소로 적는다)."""
    meta = {"caption": alt, "tool": "", "mime": _MIME[name.rsplit(".", 1)[-1].lower()], "sha256": sha, "filename": name,
            "ref": ref or f"/agent/artifacts/{name}"}
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


async def attach(blocks: dict, *, user_pat: str, artifact_dir: str | None = None, aidh_http: str | None = None) -> Figures:
    """블록 글의 그림을 올리고 표지로 바꾼다 — 실패해도 예외를 올리지 않는다(그림마다 까닭이 남는다). 그림이 없으면 blocks 를 그대로 돌려준다."""
    found = _scan(blocks)
    if not found:
        return Figures(blocks=blocks)
    if artifact_dir is None:
        from app import ARTIFACT_DIR as artifact_dir  # noqa: PLC0415 — app 이 이 모듈을 늦게 읽는다(순환 import 회피)
    if aidh_http is None and any(not _is_art(r) for r, _a in found):
        from app import AIDH_HTTP as aidh_http  # noqa: PLC0415 — 첨부 그림이 있을 때만(같은 까닭)
    res: dict[str, tuple] = {}      # 주소 → ("ok", 번호, 칸, 설명) | ("fail", 까닭, 설명)
    by_sha: dict[str, str] = {}     # 내용 해시 → 먼저 올린 주소(이름이 달라도 같은 그림은 칸 하나 · 한 장으로 센다)
    out = Figures(blocks=blocks, found=len(found))
    n, stop = 0, NO_PAT if not user_pat else ""
    async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT, transport=PORTAL_TRANSPORT) as cli:
        for ref, alt in found:
            name = ref.rsplit("/", 1)[-1]
            try:
                if stop:                    # 사람 토큰이 없거나 앞 그림이 자격 · 연결에서 막혔다 — 남은 그림도 같은 까닭이다
                    res[ref] = ("fail", stop, alt)
                    continue
                if n >= FIGURES_MAX:
                    res[ref] = ("fail", f"보고서 하나에 그림은 {FIGURES_MAX}장까지 싣는다 — 나머지는 대화에서 본다", alt)
                    continue
                if _is_art(ref):
                    p, up_name, up_ref = os.path.join(artifact_dir, name), name, None
                    if name.lower().endswith(".svg") and os.path.isfile(os.path.join(artifact_dir, name[:-4] + ".png")):
                        # 단면 짝(HWAXPortal docs/report-figures RF-40) — 챗은 같은 그림을 PNG(판독) · SVG(화면 호버) 짝으로 같은 줄기에 둔다. 보고서는 래스터다.
                        up_name = name[:-4] + ".png"
                        p = os.path.join(artifact_dir, up_name)
                    if not os.path.isfile(p):
                        res[ref] = ("fail", "그림 파일이 서버에 없다 — 오래된 도구 그림은 지워진다(최근 것만 남긴다)", alt)
                        continue
                    with open(p, "rb") as f:
                        raw = f.read()
                else:
                    # AIDataHub 첨부 — 챗의 그림 읽기와 같은 자리에서 받는다(vision.load — 8 MB 를 넘거나 없으면 None). 막는 I/O 라 스레드로 뺀다.
                    got = await asyncio.to_thread(vision.load, ref, artifact_dir, aidh_http or "")
                    if got is None:
                        res[ref] = ("fail", "첨부 그림을 받지 못했다 — AIDataHub 에 없거나 "
                                            f"{vision.MAX_BYTES // (1024 * 1024)} MB 를 넘는다", alt)
                        continue
                    raw, up_name, up_ref = got[0], name, ref
                sha = hashlib.sha256(raw).hexdigest()
                if sha in by_sha:
                    res[ref] = res[by_sha[sha]]
                    out.found -= 1
                    continue
                fid, why, fatal = await _upload(cli, user_pat, up_name, raw, sha, alt, up_ref)
                if fid is None:
                    res[ref] = ("fail", why, alt)
                    stop = why if fatal else ""
                    continue
                n += 1
                cap = (f"그림 {n} — {alt}" if alt else f"그림 {n}")
                if len(cap) > _CAPTION_MAX:
                    cap = cap[:_CAPTION_MAX - 1] + "…"
                blk = {"id": f"fig_{sha[:16]}", "type": "image", "props": {"max_count": 1},
                       "content": {"files": [{"file_id": fid, "caption": cap, "alt": (alt or "도구 그림")[:_ALT_MAX]}]}}
                res[ref] = ("ok", n, blk, alt)
                by_sha[sha] = ref
                out.extra_blocks.append(blk)
                out._ok.append((n, blk["id"], ref))
            except Exception as exc:  # noqa: BLE001 — 그림 하나의 실패가 보고서를 막지 않는다(까닭은 그 자리에 남는다)
                res[ref] = ("fail", f"올리는 중 오류({type(exc).__name__})", alt)
    out.failed = [{"ref": ref, "reason": r[1]} for ref, r in res.items() if r[0] == "fail"]

    def mark(m: re.Match) -> str:
        # 같은 정규식으로 찾은 이름이라 늘 있다. 설명은 그 그림의 것 하나(처음 나온 설명 — 캡션과 같다)라 같은 그림은 어디서나 같은 표지다.
        # 못 올린 그림도 설명을 남긴다 — 어느 그림이었는지 읽는 사람이 안다(반대 검토 F2 · F5).
        r = res.get(m.group(2) or m.group(3))
        if r is None:                       # 바꾸지 않기로 한 주소(첨부 그림을 끈 박스 · 위로 벗어나는 경로) — 글자 그대로 둔다
            return m.group(0)
        if r[0] == "fail":
            return f"[그림 — 올리지 못함: {r[1]} · {r[2]}]" if r[2] else f"[그림 — 올리지 못함: {r[1]}]"
        return f"[그림 {r[1]} — {r[3]}]" if r[3] else f"[그림 {r[1]}]"

    out.blocks = {k: ([_REF_RE.sub(mark, it) if isinstance(it, str) else it for it in v] if isinstance(v, list) else v)
                  for k, v in blocks.items()}
    return out
