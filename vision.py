# 그림을 모델이 보게 — 서버가 그림을 받는지 판정, 아티팩트 · 첨부 그림을 data URL 조각으로, 그림을 못 받는 모델에 줄 판독문(Qwen)
"""HWAXPortal docs/chat-depth-context(T5) · docs/chat-vision(V-1~V-3).

사용자 지시(10-18) — "이전 대화나 심의에 넘기는 게 챗 이력뿐 아니라 올린 이미지나 각 디지털워커가 조회한 데이터의 이미지들도 들어가는 게
좋다. 자료를 기반으로 판단하는 게 필요하다." 결정(질문 카드) — 챗(Qwen)은 그림을 그대로 읽고, 띵킹 · 심의(GLM)는 **탐침 뒤 자동**: GLM 이
그림을 받으면 그림, 못 받으면 Qwen 이 그림마다 읽은 판독문을 근거로 싣는다.

  · 판정(supports) — 서버(주소 · 모델)마다 빨간 그림 하나로 묻는다. 200 이면 받는다 · 400(그림 거절)이면 못 받는다 · 그 밖(닿지 않음 · 5xx ·
    401)은 모른다 — 모름은 굳히지 않고 _RETRY_S 뒤 다시 묻고, 그동안은 '못 받는다' 로 다룬다(그림을 보내 실패하느니 판독문).
    설정 auto|1|0 — 1 · 0 은 묻지 않고 그 값.
  · 그림 — /agent/artifacts/<이름>(이 서버가 저장한 도구 그림 · 올린 그림, 이름이 해시라 열거 불가)과 /ai-data-hub/attachments/<경로>
    (AIDataHub 첨부 — 그쪽 로컬 원점에서 받는다). 크면(VISION_MAX_BYTES) 싣지 않고 그 사실을 남긴다(조용히 빼지 않는다).
  · 판독문(transcribe) — Qwen(그림을 읽는 챗 서버)에 생각 없이 한 번, 그림 해시로 디스크에 남긴다(같은 그림은 다시 읽지 않는다).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import struct
import time
import urllib.error
import urllib.request
import zlib

MAX_IMAGES = int(os.environ.get("VISION_MAX_IMAGES", "12"))                  # 모델 호출 하나에 싣는 그림 수 위쪽
MAX_BYTES = int(os.environ.get("VISION_MAX_BYTES", str(8 * 1024 * 1024)))   # 그림 하나 — 넘으면 싣지 않는다
HISTORY_IMAGES = int(os.environ.get("VISION_HISTORY_IMAGES", "6"))          # 앞 대화의 그림을 다시 싣는 수(최근부터)
TRANSCRIBE_MAX = int(os.environ.get("VISION_TRANSCRIBE_MAX", "8"))          # 한 번에 판독하는 그림 수(띵킹 · 심의)
_TRANSCRIBE_CONC = 4      # 판독을 한꺼번에 몇 장 — 그림 여덟 장을 줄 세우면 띵킹 · 심의의 시작이 몇 분 늦고, 챗 서버를 혼자 차지하지도 않게
_RETRY_S = 60.0
PROBE_TIMEOUT_S = float(os.environ.get("VISION_PROBE_TIMEOUT_S", "15"))   # 탐침 한 번 — 길면 그림 턴의 시작이 그만큼 늦는다
_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
IMG_URL_RE = re.compile(
    r"/agent/artifacts/[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.(?:png|jpe?g|gif|webp)\b"
    r"|/ai-data-hub/attachments/[A-Za-z0-9_][A-Za-z0-9_./-]{0,200}\.(?:png|jpe?g|gif|webp)\b", re.I)


def image_urls(*texts) -> list[str]:
    """글(들)에 나온 그림 주소 — 처음 나온 순서, 중복 없이."""
    out: list[str] = []
    for t in texts:
        for u in IMG_URL_RE.findall(str(t or "")):
            if u not in out and ".." not in u:
                out.append(u)
    return out


def history_images(history) -> list[tuple[str, str]]:
    """앞 대화의 그림 — [(주소, 출처 설명)] 시간순. 사람이 올린 그림(images) · 그 턴의 조회 근거와 답에 나온 그림 주소."""
    out: list[tuple[str, str]] = []
    seen: set = set()

    def add(u: str, why: str) -> None:
        if u not in seen:
            seen.add(u)
            out.append((u, why))
    for i, m in enumerate(history or []):
        if not isinstance(m, dict):
            continue
        if m.get("role") == "user":
            for u in (m.get("images") if isinstance(m.get("images"), list) else []):
                if isinstance(u, str) and IMG_URL_RE.fullmatch(u):
                    add(u, f"{i + 1}번째 발화에 사람이 올린 그림")
        elif m.get("role") == "assistant":
            for e in (m.get("evidence") if isinstance(m.get("evidence"), list) else []):
                if isinstance(e, dict):
                    for u in image_urls(e.get("result")):
                        add(u, f"{i + 1}번째 답 앞에 도구 {str(e.get('tool') or '')[:80]} 가 낸 그림")
            for u in image_urls(m.get("content")):
                add(u, f"{i + 1}번째 답에 실린 그림")
    return out


def load(url: str, artifact_dir: str, aidh_http: str = "") -> tuple[bytes, str] | None:
    """그림 주소 → (바이트, mime). 없거나 크거나 모르는 꼴이면 None."""
    ext = url.rsplit(".", 1)[-1].lower()
    mime = _MIME.get(ext)
    if not mime or ".." in url:
        return None
    raw = None
    if url.startswith("/agent/artifacts/"):
        p = os.path.join(artifact_dir, url.rsplit("/", 1)[-1])
        if os.path.isfile(p) and os.path.getsize(p) <= MAX_BYTES:
            with open(p, "rb") as f:
                raw = f.read()
    elif url.startswith("/ai-data-hub/attachments/") and aidh_http:
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    aidh_http.rstrip("/") + url[len("/ai-data-hub"):], timeout=10) as r:
                raw = r.read(MAX_BYTES + 1)
        except Exception:  # noqa: BLE001 — 첨부를 못 받으면 그 그림만 빠진다(호출부가 센다)
            raw = None
        if raw is not None and len(raw) > MAX_BYTES:
            raw = None
    return (raw, mime) if raw else None


def sniff(raw: bytes) -> str | None:
    """바이트의 머리로 그림 꼴을 — 이름 · 선언된 mime 이 아니라 내용으로 본다. 그림이 아닌 것을 그림 조각으로 보내면 서버가 400 으로
    그 턴을 통째로 죽인다."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def data_url(raw: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


_DU_CACHE: dict = {}       # 주소 → data URL — 루프의 모델 호출마다 같은 그림을 다시 읽고 부호화하지 않게(아티팩트는 해시 이름이라 안전하다)
_DU_CACHE_MAX = 64


def parts(urls, artifact_dir: str, aidh_http: str = "", limit: int = MAX_IMAGES) -> tuple[list[dict], int]:
    """그림 주소들 → (image_url 조각들, 못 실은 수). 최근 것이 뒤에 오는 목록이면 뒤에서부터 limit 장."""
    urls = list(urls)
    out, missing = [], 0
    for u in urls[-limit:] if limit > 0 else []:
        du = _DU_CACHE.get(u)
        if du is None:
            got = load(u, artifact_dir, aidh_http)
            if not got:
                missing += 1
                continue
            du = data_url(*got)
            if len(_DU_CACHE) >= _DU_CACHE_MAX:
                _DU_CACHE.pop(next(iter(_DU_CACHE)))
            _DU_CACHE[u] = du
        out.append({"type": "image_url", "image_url": {"url": du}})
    return out, missing + max(0, len(urls) - max(0, limit))


def red_png(n: int = 16) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * n for _ in range(n))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


_probe_cache: dict = {}


def _probe(base_url: str, api_key: str, model: str) -> bool | None:
    body = json.dumps({"model": model, "max_tokens": 1, "temperature": 0,
                       "chat_template_kwargs": {"enable_thinking": False},
                       "messages": [{"role": "user", "content": [
                           {"type": "text", "text": "색 하나만"},
                           {"type": "image_url", "image_url": {"url": data_url(red_png(), "image/png")}}]}]}).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key or 'EMPTY'}"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=PROBE_TIMEOUT_S) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return False if e.code in (400, 422) else None     # 400 · 422 — 그림을 거절했다. 401 · 5xx 는 모른다
    except Exception:  # noqa: BLE001
        return None


def supports(setting: str, base_url: str, api_key: str, model: str) -> bool:
    """그 서버가 그림을 받는가 — setting 이 1 · 0 이면 그 값, auto(기본)면 탐침(서버마다 한 번, 모름은 _RETRY_S 뒤 다시)."""
    v = (setting or "auto").strip().lower()
    if v in ("1", "true", "on"):
        return True
    if v in ("0", "false", "off"):
        return False
    key = (base_url, model)
    got = _probe_cache.get(key)
    now = time.monotonic()
    if got and (got["v"] is not None or now < got["retry_at"]):
        return bool(got["v"])
    v2 = _probe(base_url, api_key, model)
    _probe_cache[key] = {"v": v2, "retry_at": now + _RETRY_S, "at": time.time()}
    return bool(v2)


def cached(setting: str, base_url: str, model: str) -> bool | None:
    """묻지 않고 지금 아는 값 — setting 1 · 0 이면 그 값, auto 면 탐침 결과(아직 없으면 None). 이벤트 루프 안에서 막히지 않게 쓴다."""
    v = (setting or "auto").strip().lower()
    if v in ("1", "true", "on"):
        return True
    if v in ("0", "false", "off"):
        return False
    got = _probe_cache.get((base_url, model))
    return None if not got else got["v"]


def state() -> dict:
    """/health 용 — 서버(호스트:포트 · 모델)마다 판정 값(None 은 아직 모름)."""
    out = {}
    for (url, model), v in _probe_cache.items():
        host = re.sub(r"^https?://", "", url).split("/", 1)[0]
        out[f"{host} · {model}"] = v["v"]
    return out


_TRANSCRIBE_SYS = ("당신은 공학 그림 판독기입니다. 그림에 보이는 것만 글로 옮깁니다 — 지어내지 않습니다. 한국어로 씁니다(기술 용어의 영문 "
                   "원어 병기는 괜찮다).")
_TRANSCRIBE_ASK = ("이 그림을 판독하라 — ① 그림 종류(그래프 · 표 · 응력/변형 지도 · CAD 화면 · 도면 · 사진) ② 축 이름 · 단위 · 범위 ③ 범례 · "
                   "색 막대의 값 범위 ④ 읽을 수 있는 수치 전부(최댓값 · 최솟값과 그 위치, 표의 칸) ⑤ 그림 안의 글자 그대로. 읽을 수 없는 "
                   "것은 '판독 불가' 라고 적어라.")


def _tr_dir(artifact_dir: str) -> str:
    d = os.path.join(artifact_dir, ".transcripts")
    os.makedirs(d, exist_ok=True)
    return d


async def transcribe(llm, raw: bytes, mime: str, artifact_dir: str, hint: str = "") -> str:
    """그림 → 판독문. 같은 그림(해시)은 디스크에서 꺼낸다. 실패하면 빈 글(호출부가 '판독 실패' 로 센다)."""
    from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415

    sha = hashlib.sha256(raw).hexdigest()
    path = os.path.join(_tr_dir(artifact_dir), sha + ".txt")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    msgs = [SystemMessage(content=_TRANSCRIBE_SYS),
            HumanMessage(content=[{"type": "text", "text": _TRANSCRIBE_ASK + (f"\n맥락: {hint}" if hint else "")},
                                  {"type": "image_url", "image_url": {"url": data_url(raw, mime)}}])]
    try:
        # 판독에 깊은 생각은 필요 없다 — 생각을 끄고 한 번(그림 하나에 1~2분씩 걸리지 않게).
        r = await llm.ainvoke(msgs, extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    except Exception as exc:  # noqa: BLE001 — 판독 실패가 챗 · 띵킹 · 심의를 죽이지 않는다
        print(f"[vision] 판독 실패({type(exc).__name__}: {str(exc)[:120]})")
        return ""
    text = r.content if isinstance(r.content, str) else " ".join(
        p.get("text", "") for p in (r.content or []) if isinstance(p, dict))
    text = str(text or "").strip()
    if text:
        tmp = f"{path}.{os.getpid()}.tmp"       # 쓰다 만 판독문을 다른 호출이 읽지 않게 — 다 쓴 뒤 바꿔 끼운다
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    return text


async def transcribe_all(llm, pairs, artifact_dir: str, aidh_http: str = "", hint: str = "") -> list[tuple[str, str, str]]:
    """[(주소, 출처 설명)] → [(주소, 출처 설명, 판독문)] 같은 순서로. 그림을 받아 오지 못했거나 판독에 실패한 것은 판독문이 빈 글이다
    (호출부가 '판독하지 못했다' 로 센다). 챗 · 띵킹 · 심의가 같은 것을 쓴다."""
    sem = asyncio.Semaphore(_TRANSCRIBE_CONC)

    async def one(u: str, why: str) -> tuple[str, str, str]:
        async with sem:
            got = await asyncio.to_thread(load, u, artifact_dir, aidh_http)
            return u, why, (await transcribe(llm, got[0], got[1], artifact_dir, hint=hint) if got else "")
    return list(await asyncio.gather(*(one(u, w) for u, w in pairs)))


def has_images(messages) -> bool:
    """메시지들에 그림 조각(image_url)이 있나."""
    return any(isinstance(getattr(m, "content", None), list)
               and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m.content) for m in messages or [])


def tool_image_urls(messages) -> list[str]:
    """도구 결과 메시지들에 나온 그림 주소(처음 나온 순서)."""
    from langchain_core.messages import ToolMessage  # noqa: PLC0415

    texts = [m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False, default=str)
             for m in messages or [] if isinstance(m, ToolMessage)]
    return image_urls(*texts)


def strip_images(messages) -> list:
    """메시지들에서 그림 조각을 빼고 '[그림 N장 — 이 모델은 그림을 읽지 못해 뺐다]' 한 줄로 — 그림을 못 받는 모델(넘김 받는 GLM)에 보낼 때."""
    from langchain_core.messages import BaseMessage  # noqa: PLC0415

    out = []
    for m in messages:
        c = getattr(m, "content", None) if isinstance(m, BaseMessage) else None
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in c):
            n = sum(1 for p in c if isinstance(p, dict) and p.get("type") == "image_url")
            texts = [p.get("text", "") if isinstance(p, dict) else str(p) for p in c
                     if not (isinstance(p, dict) and p.get("type") == "image_url")]
            m = m.model_copy(update={"content": "\n".join(t for t in texts if t)
                                     + f"\n[그림 {n}장 — 이 모델은 그림을 읽지 못해 뺐다]"})
        out.append(m)
    return out
