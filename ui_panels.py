# 포털 챗의 MCP Apps 패널 — 패널 도구 알아보기 · 원 결과 붙잡기(인터셉터) · ui_resource 사건 꼴과 상한(HWAXPortal docs/request-1027 HA-20)
"""HWAXPortal docs/request-1027/design-ha09-ha20.md B5.2 · 사용자 결정 10-31(B-1 ~ B-7 전부 (가)).

  · 켜는 표지 — 포털이 이번 턴 요청에 `ui_host: "mcp-apps/1"` 을 실었을 때만(화면에 호스트 부품이 있고 · 박스 손잡이가 켜졌고 · feat:panels 가
    있을 때 포털이 싣는다). 손잡이 CHAT_UI_HOST=0 이면 표지를 무시한다(HA-05 그대로 — 박스가 급히 끄는 길).
  · 패널 도구 — 도구 목록의 `_meta.ui.resourceUri`(옛 키 "ui/resourceUri")가 ui:// 인 도구와 HA-05 이름 집합(app._CHAT_PANEL_TOOLS). 표지가 있는
    턴은 주소가 있는 것만 묶고 invoke_tool 로 부르면 막는다(직접 부르라고 한다 — 사건이 도구 목록의 `_meta` 를 봐야 해서다, 게이트웨이 지침
    1번과 같다). 그 밖(옛 화면 · 켜지 않은 박스 · 손잡이 끔 · 심의 · 루틴 · 카탈로그)은 HA-20 전 그대로다 — HA-05 이름 집합만 묶지도 부르지도
    않고, 집합 밖의 주소 도구는 종전처럼 묶이고 invoke_tool 로도 간다(반대 검토 10-31 — 그 도구를 어떻게 다룰지는 Q-A1 답 뒤 사람 결정).
    `_meta.ui.visibility` 에 "model" 이 없는 도구(패널 전용)는 표지 턴에 모델에 묶지 않는다.
  · 붙잡기 — 어댑터(langchain-mcp-adapters 0.3)는 호출 결과의 `_meta` 를 버린다. 인터셉터가 원 CallToolResult 의 글 블록 · structuredContent ·
    `_meta` 를 문맥 변수 그릇에 적고 결과는 손대지 않고 넘긴다 — 모델에 가는 결과는 지금과 같다(B-6 (가) — `_meta` 는 패널에만).
  · 사건 — 턴 그릇(Turn)에 쌓고 챗 스트림이 그 도구의 on_tool_end 에서 SSE ui_resource 로 낸다. 사용자 사건(adispatch_custom_event)은
    LangGraph 의 Send 노드 실행으로 매겨져 상태줄의 call(도구 실행 id)과 이어지지 않았다(10-31 실측) — 그래서 끝 사건에서 짝짓는다. 끝 사건이
    없는 길(재시도의 ainvoke)은 턴 끝에서 call 없이 낸다.
  · 상한 — 사건 한 건의 JSON 이 UI_RESOURCE_MAX_BYTES(기본 256 KiB)를 넘으면 result 를 빼고 cut(원래 바이트)을 싣는다(패널이 도구를 다시
    불러 받는다). 그래도 넘으면 input 도 뺀다.

이 모듈은 앱(app.py)을 읽지 않는다. 문맥 변수는 여기 둔다 — 시험이 app 을 다시 읽어도(reload) 같은 변수다.
"""
from __future__ import annotations

import contextvars
import json
import os

HOST_TOKEN = "mcp-apps/1"          # 포털 화면의 호스트 부품 판 — 포털 ChatRequest.ui_host 와 같은 글자(두 리포 시험이 본다)
EVENT = "ui_resource"              # 챗 SSE 사건 이름 — 포털 화면 chat.api.ts dispatch 가 받는다


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        print(f"[ui-host] env {name}='{raw}' 숫자 파싱 실패 — 기본값 {default} 사용", flush=True)
        return default


# 손잡이 — 기동 때 한 번 읽는다(반영은 재기동). 시험은 모듈 속성을 바꾼다.
ON = os.environ.get("CHAT_UI_HOST", "1").strip() != "0"
MAX_BYTES = max(1024, _env_int("UI_RESOURCE_MAX_BYTES", 256 * 1024))

# 이번 턴이 패널을 그리는 화면의 턴인가 — 턴 그릇(Turn) 또는 None. 챗 턴(app._agent_stream_turn)이 들머리에서 건다.
TURN: contextvars.ContextVar["Turn | None"] = contextvars.ContextVar("hwax_ui_turn", default=None)
# 도구 호출 하나의 원 결과 그릇 — capped 가 원 호출 앞에 걸고 인터셉터가 채운다(같은 도구 태스크 안이라 닿는다).
_SLOT: contextvars.ContextVar["dict | None"] = contextvars.ContextVar("hwax_ui_slot", default=None)
# 게이트웨이 도구 목록에서 본 패널 도구 이름(resourceUri 를 단 것) — invoke_tool 안쪽 이름 막이가 읽는다. 목록을 받을 때마다 더한다.
_LEARNED: set[str] = set()


def _ui_meta(tool) -> dict:
    """도구 목록의 `_meta` — 어댑터가 metadata["_meta"] 에 둔다(langchain_mcp_adapters/tools.py)."""
    md = getattr(tool, "metadata", None) or {}
    m = md.get("_meta") if isinstance(md, dict) else None
    return m if isinstance(m, dict) else {}


def resource_uri(tool) -> str | None:
    """패널 주소 — `_meta.ui.resourceUri`(새 키) 또는 "ui/resourceUri"(옛 키)가 ui:// 일 때만."""
    m = _ui_meta(tool)
    ui = m.get("ui") if isinstance(m.get("ui"), dict) else {}
    u = ui.get("resourceUri") or m.get("ui/resourceUri")
    return u if isinstance(u, str) and u.startswith("ui://") and len(u) <= 2048 else None


def model_hidden(tool) -> bool:
    """`_meta.ui.visibility` 가 목록이고 "model" 이 없다 — 패널만 부르는 도구다(MCP Apps). 칸이 없으면 둘 다(모델 · 패널)다."""
    ui = _ui_meta(tool).get("ui")
    vis = ui.get("visibility") if isinstance(ui, dict) else None
    return isinstance(vis, list) and "model" not in vis


def learn(tools) -> None:
    """도구 목록에서 패널 도구 이름을 배운다 — 표지가 없는 길(심의 · 카탈로그 · 루틴)의 목록도 배운다. 읽는 쪽(invoke_tool 막이)은 표지 턴뿐이다."""
    for t in tools or ():
        if resource_uri(t):
            n = str(getattr(t, "name", "") or "").strip().lower()
            if n:
                _LEARNED.add(n)


def known(name) -> bool:
    """배운 패널 도구인가 — 이름 그대로이거나 게이트웨이 별칭(<앱키>_<도구>)을 단 꼴(app._chat_panel_tool 과 같은 규칙)."""
    n = str(name or "").strip().lower()
    return bool(n) and any(n == t or n.endswith("_" + t) for t in _LEARNED)


async def capture(request, handler):
    """도구 호출 인터셉터(MultiServerMCPClient tool_interceptors) — 그릇이 걸려 있으면 원 CallToolResult 의 글 블록 · structuredContent ·
    `_meta` 를 적는다. 결과는 그대로 돌려준다(모델에 가는 결과를 바꾸지 않는다). 그릇이 없으면(패널 도구가 아니거나 표지 없는 턴) 아무것도 안 한다."""
    result = await handler(request)
    slot = _SLOT.get()
    if slot is not None and hasattr(result, "content") and not getattr(result, "isError", False):
        slot["content"] = [{"type": "text", "text": c.text} for c in (getattr(result, "content", None) or [])
                           if getattr(c, "type", "") == "text" and isinstance(getattr(c, "text", None), str)]
        slot["structuredContent"] = getattr(result, "structuredContent", None)
        slot["_meta"] = getattr(result, "meta", None)
        slot["seen"] = True
    return result


def begin() -> tuple:
    """원 호출 앞 — 빈 그릇을 건다. (토큰, 그릇) 을 돌려준다."""
    slot: dict = {}
    return _SLOT.set(slot), slot


def end(tok) -> None:
    try:
        _SLOT.reset(tok)
    except ValueError:          # 다른 문맥에서 닫혔다 — 그 문맥은 곧 버려진다
        pass


def _jsonable(v):
    """사건에 실을 값 — JSON 으로 못 쓰는 것(도구 실행 문맥 같은 객체)은 뺀다."""
    try:
        return json.loads(json.dumps(v, ensure_ascii=False, default=lambda _o: None))
    except (TypeError, ValueError):
        return None


def event(*, tool: str, app: str, uri: str, args: dict, slot: dict) -> dict:
    """ui_resource 사건 꼴(design B5.2) — {tool, app, uri, input, result: {content(글 블록만), structuredContent, _meta}, cut?}. call 은 낼 때
    단다(Turn.drain). 합이 MAX_BYTES 를 넘으면 result 를 빼고 cut(원래 바이트)을 싣는다 · 그래도 넘으면 input 도 뺀다."""
    ev = {"tool": tool, "app": app or "", "uri": uri,
          "input": _jsonable({k: v for k, v in (args or {}).items() if k != "runtime"}) or {},
          "result": {"content": slot.get("content") or [],
                     "structuredContent": _jsonable(slot.get("structuredContent")),
                     "_meta": _jsonable(slot.get("_meta"))}}
    n = len(json.dumps(ev, ensure_ascii=False).encode("utf-8"))
    if n > MAX_BYTES:
        ev.pop("result")
        ev["cut"] = n
        if len(json.dumps(ev, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
            ev["input"] = {}
            ev["input_cut"] = True
        print(f"[ui-host] {tool} ui_resource 가 {n:,}바이트라 상한 {MAX_BYTES:,}(UI_RESOURCE_MAX_BYTES)을 넘어 결과를 뺐다 — 패널이 다시 부른다",
              flush=True)
    return ev


class Turn:
    """한 챗 턴의 패널 사건 그릇 — capped 가 더하고 스트림이 낸다. call 은 그 도구의 끝 사건(on_tool_end)에서 단다(먼저 온 것부터)."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.sent = 0

    def add(self, ev: dict) -> None:
        self.items.append(ev)

    def take(self, tool: str, call: str) -> list[dict]:
        """그 도구의 끝 사건 — 아직 안 낸 같은 이름의 첫 항목에 call 을 달아 낸다(없으면 빈 목록)."""
        for i, ev in enumerate(self.items):
            if ev.get("tool") == tool:
                self.items.pop(i)
                self.sent += 1
                return [{"call": call, **ev}]
        return []

    def rest(self) -> list[dict]:
        """턴 끝 — 끝 사건 없이 남은 것(재시도 길의 ainvoke)을 call 없이 낸다."""
        out = [{"call": "", **ev} for ev in self.items]
        self.sent += len(out)
        self.items.clear()
        return out
