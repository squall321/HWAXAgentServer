# 챗 뷰어 카드 — 도구 결과에서 같은 출처 /apps/ 뷰어 주소를 모아 SSE embeds 로 낸다(포털 docs/chat-embeds)
"""포털 docs/chat-embeds PLAN 1절 · CE-2(사용자 결정 11-05).

  · 무엇을 — 도구 결과 JSON 의 `embed_url`(플랫폼의 명시 표지 — 늘 후보)과 알려진 혼자 도는 뷰어의 `public_url` · `viewer_url`(그 경로가
    CHAT_EMBED_PATHS 의 정규식 하나와 통째로 맞을 때만 — 기본은 StepForge 단면 지도 `/apps/step_forge/view/<과제>/atlas`). StepForge 3D 뷰어
    (`/view/<과제>`)는 로그인 쿠키로 API 를 불러 격리 창(sandbox="allow-scripts" — 불투명 출처)에서 깨지므로 기본에 넣지 않는다(링크 그대로).
  · 주소 — 같은 출처 `/apps/<앱>/…` 경로만. 공개 주소(`http(s)://…/apps/…`)는 경로(+ 질의)만 쓴다 — 포털 정문이 같은 `/apps/` 를 내주므로
    오리진 · 포트가 달라도(dev `STEPFORGE_PUBLIC_BASE`) 같은 출처가 된다. `#조각` 은 뗀다. 포털 중계(backend/app/agent/embeds.py safe_path)와
    화면이 같은 규칙으로 다시 거른다 — 여기는 그 규칙에 `..` · `//` 를 질의까지 버리는 것을 더해 조금 좁다(내는 쪽이 좁으면 어긋나도 카드가
    빠질 뿐이고, 넓으면 화면이 버린 카드를 서버 사본만 쥔다).
  · 모델이 쓴 글은 읽지 않는다 — 도구 결과에서만 집는다(지어낸 주소를 띄우지 않게).
  · 턴 그릇 — 챗 턴(app._agent_stream_turn)만 TURN 에 목록을 건다. 도구 래퍼(app._cap_tool.capped)가 접기 · 자르기 전 원문에서 더하고, 스트림이
    result 앞에 한 번 낸다(rest). 그릇이 없으면(심의 · 띵킹 · 루틴 · 카탈로그) 아무것도 안 한다 — 글을 읽지도 않는다.

이 모듈은 앱(app.py)을 읽지 않는다. 문맥 변수는 여기 둔다 — 시험이 app 을 다시 읽어도(reload) 같은 변수다(ui_panels 와 같은 까닭).
"""
from __future__ import annotations

import contextvars
import json
import os
import re

EVENT = "embeds"                    # 챗 SSE 사건 이름 — 포털 중계(routes.py)와 화면이 받는다
MAX_ITEMS = 4                       # 턴마다 — 옛 대화를 열 때처럼 창 여럿이 한꺼번에 뜨지 않게
MAX_URL = 2048
_MAX_TITLE = 120
# 결과 JSON 을 걷는 상한 — 깊이 · 본 값 수. 큰 조회 결과(수천 행)에 턴 시간을 쓰지 않는다. 단면 지도는 [ {…, viewer: {public_url}} ] 로 깊이 2 다.
_MAX_DEPTH = 8
_MAX_NODES = 5000
_MARK_KEY = "embed_url"
_ALLOW_KEYS = ("public_url", "viewer_url")
# 기본 허용 목록 — StepForge 단면 지도 뷰어(외부 라이브러리 없는 HTML 한 장 · D-665). 쉼표로 가르므로 쉼표가 든 정규식({1,3} 등)은 쓰지 못한다.
DEFAULT_PATHS = r"^/apps/step_forge/view/[^/?#]+/atlas$"

# 포털 safe_path 와 같은 꼴 — 따옴표 · 꺾쇠 · 공백 · 역슬래시 · # 은 글자 집합에 없다. 질의는 하나.
_SAFE_PATH = re.compile(r"/apps/[a-z0-9][a-z0-9_-]{0,63}/[A-Za-z0-9._~!$&()*+,;=:@/%-]*(?:\?[A-Za-z0-9._~!$&()*+,;=:@/?%-]*)?")
# 경로를 벗어나는 꼴(포털 _ESCAPE 그대로 — 경로 몫에만 건다) — 점 마디 · 겹 빗금 · 인코딩한 점 · 빗금 · 역슬래시(브라우저는 %2e 를 점 마디로 푼다)
_ESCAPE = re.compile(r"(?:^|/)\.\.?(?:/|$|\?)|//|%2e|%2f|%5c", re.I)
_APP = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")      # 포털 pointer_of 의 앱 이름 꼴 — 아니면 경로의 앱 마디
_ORIGIN = re.compile(r"https?://[^/?#]+", re.I)       # 공개 주소의 오리진 — 떼고 경로만 쓴다
_DEC = json.JSONDecoder()

# 이번 챗 턴의 카드 그릇(목록) 또는 None — 챗 턴(app._agent_stream_turn)이 들머리에서 건다. 도구 코루틴은 복사된 문맥에서 같은 목록을 고친다.
TURN: contextvars.ContextVar["list | None"] = contextvars.ContextVar("hwax_chat_embeds", default=None)
_PATS: dict = {"raw": None, "pats": ()}


def to_path(value) -> str | None:
    """카드로 띄울 같은 출처 경로 — `/apps/…` 경로는 그대로, 공개 주소(http(s)://오리진/apps/…)는 경로(+ 질의)만. `#조각` 은 뗀다. 규칙 밖이면 None."""
    if not isinstance(value, str):
        return None
    v = value.split("#", 1)[0]
    if not v.startswith("/apps/"):
        m = _ORIGIN.match(v)
        if m is None:
            return None          # javascript: · //다른곳 · 앱 안 경로(/view/…) — 후보가 아니다
        v = v[m.end():]
    if (len(v) > MAX_URL or not _SAFE_PATH.fullmatch(v) or ".." in v or "//" in v
            or _ESCAPE.search(v.split("?", 1)[0][1:])):
        return None
    return v


def allowed_paths() -> tuple:
    """CHAT_EMBED_PATHS(쉼표로 가른 정규식 — 없으면 DEFAULT_PATHS) — 부를 때마다 읽고 값이 바뀌었을 때만 다시 짓는다. 비우면 허용 목록이 없다
    (명시 표지 embed_url 만). 깨진 식은 버리고 한 줄 남긴다(나머지 식은 그대로 쓴다)."""
    raw = os.environ.get("CHAT_EMBED_PATHS", DEFAULT_PATHS)
    if raw != _PATS["raw"]:
        pats = []
        for s in (x.strip() for x in raw.split(",")):
            if not s:
                continue
            try:
                pats.append(re.compile(s))
            except re.error as exc:
                print(f"[embeds] CHAT_EMBED_PATHS 의 '{s[:80]}' 를 버림 — 정규식 오류: {exc}", flush=True)
        _PATS.update(raw=raw, pats=tuple(pats))
    return _PATS["pats"]


def _json(text: str):
    """결과 글 → JSON 값. 앞의 값 하나만 읽는다 — 래퍼가 글 뒤에 붙인 안내(그림 주소 등 — app._cap_tool._norm)는 버린다. JSON 이 아니면 None."""
    s = text.lstrip()
    if not s.startswith(("{", "[")):
        return None
    try:
        return _DEC.raw_decode(s)[0]
    except (ValueError, RecursionError):
        return None


def _item(url: str, owner: dict, tool: str, app: str) -> dict:
    """카드 한 장 {app, tool, url, title} — 제목은 같은 객체의 title(없으면 name), 없으면 '앱 · 도구'(포털 pointer_of 와 같은 꼴)."""
    a = app if _APP.fullmatch(app) else url.split("/", 3)[2]
    title = next((x.strip()[:_MAX_TITLE] for x in (owner.get("title"), owner.get("name")) if isinstance(x, str) and x.strip()), "")
    return {"app": a, "tool": tool, "url": url, "title": title or f"{a} · {tool}".strip(" ·")}


def _walk(node, depth: int, seen: list, out: list, tool: str, app: str, pats: tuple) -> None:
    """결과 안 차례(먼저 온 것이 이긴다)로 걷는다 — 딕셔너리의 표지 · 허용 키를 보고 안쪽 딕셔너리 · 목록으로 내려간다(깊이 · 값 수 상한)."""
    pairs = node.items() if isinstance(node, dict) else ((None, v) for v in node)
    for k, v in pairs:
        seen[0] += 1
        if seen[0] > _MAX_NODES or len(out) >= MAX_ITEMS:
            return
        if isinstance(v, str):
            if k == _MARK_KEY or (pats and k in _ALLOW_KEYS):
                p = to_path(v)
                if p and (k == _MARK_KEY or any(r.fullmatch(p) for r in pats)) and all(x["url"] != p for x in out):
                    out.append(_item(p, node, tool, app))
        elif isinstance(v, (dict, list)) and depth < _MAX_DEPTH:
            _walk(v, depth + 1, seen, out, tool, app, pats)


def collect(text, tool: str = "", app: str = "") -> list[dict]:
    """도구 결과 하나 → 카드 후보 [{app, tool, url, title}](결과 안 차례 · 같은 주소는 하나 · MAX_ITEMS 까지). JSON 이 아니면 빈 목록.
    app 은 게이트웨이 도구 지도의 백엔드 이름(모르면 경로의 앱 마디), tool 은 실제로 부른 도구(invoke_tool 경유면 안쪽 이름)."""
    pats = allowed_paths()
    if isinstance(text, str):
        # 싸게 먼저 거른다 — 키 이름이 글에 없으면 JSON 을 풀지 않는다(챗 턴의 조회 결과는 수십만 자도 된다)
        if f'"{_MARK_KEY}"' not in text and not (pats and any(f'"{k}"' in text for k in _ALLOW_KEYS)):
            return []
        obj = _json(text)
    else:
        obj = text
    if not isinstance(obj, (dict, list)):
        return []
    out: list[dict] = []
    _walk(obj, 0, [0], out, str(tool or "")[:_MAX_TITLE], str(app or ""), pats)
    return out


def add(text, tool: str = "", app: str = "") -> None:
    """도구 결과 하나를 이번 챗 턴 그릇에 — 그릇이 없으면(챗 턴 밖) 글을 읽지도 않는다. 같은 주소는 먼저 온 것 하나, 턴에 MAX_ITEMS 까지.
    나란한 도구 코루틴은 같은 이벤트 루프에서 await 없이 더하므로 자물쇠가 필요 없다(app._sc_keep_raw 와 같은 까닭). 도구 래퍼 안에서 불리므로
    무엇이 터져도 삼키고 한 줄 남긴다 — 카드를 못 모은 것이 도구 결과를 죽이지 않는다."""
    bag = TURN.get()
    if bag is None or len(bag) >= MAX_ITEMS:
        return
    try:
        got = collect(text, tool, app)
    except Exception as exc:  # noqa: BLE001
        print(f"[embeds] {str(tool)[:80]} 결과에서 뷰어 주소를 못 모음 — {type(exc).__name__}: {exc}"[:300], flush=True)
        return
    for it in got:
        if len(bag) >= MAX_ITEMS:
            break
        if all(x["url"] != it["url"] for x in bag):
            bag.append(it)


def rest(bag) -> list[dict]:
    """턴 끝(result 앞) — 모은 것이 있으면 사건 자료 하나 [{"items": […]}] 를 내고 그릇을 비운다(같은 턴에 두 번 내지 않는다). 없으면 빈 목록."""
    if not bag:
        return []
    items = list(bag)
    bag.clear()
    return [{"items": items}]
