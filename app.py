"""HWAX Agent Server — LangGraph ReAct + group-scoped MCP tool fan-out.

The portal is a thin proxy; the real model call + tool use live here (계획서 §3).
Flow: ChatDock → portal /agent/chat → THIS → (vLLM for the LLM, MCP gateway for tools).

A LangGraph ReAct agent runs the loop: the LLM (Qwen2.5-7B on vLLM, tool-calling
enabled) decides when to call MCP tools; tools come from the HWAX MCP Gateway via
langchain-mcp-adapters. We stream the run as the portal's §5 SSE contract:
  status (incl. tool calls) → token×N → result → done (or error).

Authorization (계획서 §4): the portal hands off the caller's `groups` (JWT claim, from
SAML memberOf). The *gateway* owns group-based tool filtering (it knows each tool's
backend); this server simply forwards the caller's groups to the gateway on every request
via the `X-HWAX-Groups` header, then builds the ReAct agent from whatever tools the
gateway returns for those groups. The tool set is therefore per-caller, so we load tools
per request and cache the compiled agent by the caller's group-set.

If MCP/LLM wiring is unavailable the server still answers — it just won't have tools.

Env:
  VLLM_BASE_URL   OpenAI-compatible base (default http://127.0.0.1:8000/v1)
  VLLM_MODEL      served model name (default qwen2.5-7b-dev)
  MCP_CONFIG      path to a JSON file (gitignored — holds the gateway token) of per-server
                  config, e.g. {"gateway":{"url":"http://127.0.0.1:9110/mcp",
                  "transport":"streamable_http","headers":{"Authorization":"Bearer hwaxgw_…"}}}.
                  Takes precedence over MCP_SERVERS.
  MCP_SERVERS     fallback: comma-separated name=url pairs (no per-server auth headers).
"""

import contextvars
import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from deliberation import (
    N_PERSONAS,
    _call,
    _env_float,
    _env_int,
    _first_dict,
    _parse_json,
    _tools_by_name,
    is_deliberation,
    is_report_save,
    run_deliberation,
    run_report_save,
    strip_report_trigger,
    strip_trigger,
)
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:8000/v1")
VLLM_MODEL = os.environ.get("VLLM_MODEL", "qwen2.5-7b-dev")
# 인증 있는 OpenAI 호환 서버(상암 B300 등)용 — 미설정이면 "EMPTY"(로컬 vLLM 무인증과 동일).
VLLM_API_KEY = os.environ.get("VLLM_API_KEY") or "EMPTY"
MCP_SERVERS = os.environ.get("MCP_SERVERS", "")
MCP_CONFIG = os.environ.get("MCP_CONFIG", "")
GROUPS_HEADER = "X-HWAX-Groups"  # gateway reads this to filter tools by the caller's groups
SYSTEM_PROMPT = (
    "당신은 HWAX 포털의 어시스턴트입니다. 반드시 한국어로만 답하세요 — "
    "중국어·영어 등 다른 언어로 절대 전환하지 마세요. 간결·정확하게. "
    "조회·분석 요청은 반드시 제공된 도구를 사용하세요(예: 보고서/템플릿, VOC·고객의 소리, 백서·기술문서, "
    "데이터셋·데이터 허브, 시뮬레이션 클러스터·Slurm, 재료·복합재·열충격 등 공학해석 앱). "
    "당신의 능력은 '지금 당신에게 주어진 도구 목록(각 도구의 이름·설명)'이 전부이며, 이 목록은 HEAX Hub 등에서 "
    "도구가 계속 추가되어 늘어납니다 — 그러니 특정 기능의 유무를 물으면(예: '열충격/재료/warpage 같은 게 있냐'), "
    "고정된 지식이나 아래 예시 목록이 아니라 실제 당신 도구의 이름·설명을 근거로 판단하세요. 맞는 도구가 있으면 "
    "그 도구로 답하고, 없으면 솔직히 없다고 하세요(도구에 없는 기능을 지어내지 마세요). "
    "도구 결과에 근거해 답하고, 추측하지 마세요. "
    "조회 도구는 항상 좁게 호출하세요 — limit(기본 10 이하)·필터·기간을 지정하고, "
    "대량 데이터가 필요하면 요약/집계 도구를 우선 사용하세요. "
    "그래프·차트·시각화를 요청받으면 도구로 데이터를 조회한 뒤, 외부 리소스 없이 "
    "자체 완결된(self-contained, 인라인 SVG/스크립트) HTML을 ```html 코드블록으로 출력하세요 — "
    "챗이 미리보기로 렌더링합니다. "
    "도구 결과에 이미지 URL(captured.images[].url 의 /agent/artifacts/… 또는 attachment 의 /ai-data-hub/attachments/… 형태)이 있으면 그 그래프를 "
    "반드시 마크다운 이미지 문법 ![설명](url) 로 본문에 포함하세요 — 챗이 이미지로 렌더링합니다. "
    "이미지 URL 이 있으면 HTML 차트를 새로 만들지 말고 그 이미지 포함을 우선하세요.\n\n"
    "포털 사용법·시작 방법을 물으면 다음을 안내하세요(도구 호출 불필요). "
    "권장 사용법은 이 웹 챗이 아니라 개인 Claude(Desktop/Claude Code)에 이 포털을 MCP로 연결해 쓰는 것입니다 — "
    "웹 챗은 가벼운 확인·데모용이며 본격 업무 사용은 권장되지 않습니다. 연결 방법: "
    "① 포털 상단 'API 토큰' 메뉴(/tokens)에서 토큰을 발급합니다(한 번만 표시되니 즉시 복사). "
    "② 같은 화면에 나오는 등록 명령을 실행합니다 — Claude Code는 `claude mcp add --transport http hwax "
    "<포털주소>/mcp-gw/mcp --header \"Authorization: Bearer <토큰>\"`, Claude Desktop은 표시된 JSON 설정을 붙여넣기. "
    "③ 이후 자신의 Claude에서 이 포털에 연결된 모든 서비스 도구(보고서·VOC·백서 등)를 바로 쓸 수 있습니다. "
    "토큰은 /tokens 화면에서 언제든 폐기할 수 있습니다."
)


def _parse_servers(spec: str) -> dict:
    out = {}
    for part in spec.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, url = part.split("=", 1)
        out[name.strip()] = {"url": url.strip(), "transport": "streamable_http"}
    return out


def _load_mcp_config() -> dict:
    if MCP_CONFIG and os.path.exists(MCP_CONFIG):
        with open(MCP_CONFIG) as f:
            return json.load(f)
    return _parse_servers(MCP_SERVERS)


def _with_groups(connections: dict, groups: list[str]) -> dict:
    """Clone the MCP connection config, injecting the caller's groups header so the gateway
    can filter the tool list (and guard tool calls). Does not mutate the input."""
    hdr = ",".join(groups)
    out = {}
    for name, cfg in connections.items():
        cfg = dict(cfg)
        cfg["headers"] = {**cfg.get("headers", {}), GROUPS_HEADER: hdr}
        out[name] = cfg
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build the LLM once. Tools are loaded per request (they depend on the caller's groups),
    # so we keep the raw connection config and compile/cache a ReAct agent per group-set.
    # LLM_DISABLE_STREAMING=1: 토큰 스트리밍 대신 완결 응답(invoke) — vLLM 0.23.0의 GLM-5.2
    # 스트리밍 경로가 tool_calls 를 유실(빈 배열 + finish_reason:stop)하는 서빙 결함 우회.
    # 텍스트가 모델 턴 단위로 도착하는 대신 도구 호출이 정상 동작한다. dev(qwen)는 기본 0(스트리밍).
    disable_stream = os.environ.get("LLM_DISABLE_STREAMING", "0") == "1"

    # LLM 튜닝은 전부 env — 환경(dev qwen 16K vs 상암 GLM)마다 다른 값을 배포 없이 적용한다.
    # 미설정 시 기존 동작과 동일(temperature=0, max_tokens 미전송=무상한, effort·timeout 미전달).
    # 파싱은 안전 파서(_env_*) — 오타 값이 lifespan 에서 서버 기동을 죽이지 않고 경고 후 기본값.
    #   LLM_TEMPERATURE      : 기본 0 (ReAct 도구호출 결정성)
    #   LLM_MAX_TOKENS       : 0/미설정=미전송. 설정 시 8192급 여유값 권장 — 2048~4096은
    #                          thinking 토큰과 겹치면 미완 JSON 절단을 유발하므로 금지.
    #   LLM_REASONING_EFFORT : 빈 값=미전달(RA 규약). GLM 계열은 반드시 extra_body 경유
    #                          chat_template_kwargs 로 전달 — 톱레벨 reasoning_effort 필드는
    #                          OpenAI 표준 파라미터로 나가므로 쓰지 않는다.
    #   LLM_TIMEOUT_S        : 챗(ReAct) 경로 포함 전역 타임아웃. 0/미설정=라이브러리 기본(600s).
    #   DELIB_*              : 심의 라운드 전용 오버라이드(temperature/max_tokens/effort/timeout).
    #                          챗 경로는 위 LLM_* 만 따르므로 심의 튜닝이 챗에 새지 않는다.
    def _mk_llm(temperature: float, max_tokens: int, effort: str, timeout_s: float = 0.0) -> ChatOpenAI:
        kw: dict = dict(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY, model=VLLM_MODEL,
                        temperature=temperature, disable_streaming=disable_stream)
        if max_tokens > 0:
            kw["max_tokens"] = max_tokens
        if effort:
            kw["extra_body"] = {"chat_template_kwargs": {"reasoning_effort": effort}}
        if timeout_s > 0:
            kw["timeout"] = timeout_s
        return ChatOpenAI(**kw)

    base_t = _env_float("LLM_TEMPERATURE", 0.0)
    base_mt = _env_int("LLM_MAX_TOKENS", 0)
    base_eff = os.environ.get("LLM_REASONING_EFFORT", "")
    base_to = _env_float("LLM_TIMEOUT_S", 0.0)
    app.state.llm = _mk_llm(base_t, base_mt, base_eff, base_to)

    # 심의(라운드 토론) 전용 오버라이드 — DELIB_* 가 하나라도 설정되면 별도 인스턴스.
    if any(os.environ.get(k, "") for k in
           ("DELIB_TEMPERATURE", "DELIB_MAX_TOKENS", "DELIB_REASONING_EFFORT", "DELIB_TIMEOUT_S")):
        d_t = _env_float("DELIB_TEMPERATURE", base_t)
        d_mt = _env_int("DELIB_MAX_TOKENS", base_mt)
        d_eff = os.environ.get("DELIB_REASONING_EFFORT", "") or base_eff
        d_to = _env_float("DELIB_TIMEOUT_S", base_to)
        app.state.delib_llm = _mk_llm(d_t, d_mt, d_eff, d_to)
        print(f"[agent] 심의 전용 LLM 오버라이드 — temperature={d_t}, "
              f"max_tokens={d_mt or '미전송'}, effort={d_eff or '미전달'}, "
              f"timeout={d_to or '기본'}")
    else:
        d_t, d_mt, d_eff, d_to = base_t, base_mt, base_eff, base_to
        app.state.delib_llm = app.state.llm
    # 요청 단위 타임아웃 오버라이드(웹 토글)용 — 같은 temperature/max_tokens/effort 로 timeout 만
    # 바꿔 재구성하는 팩토리(구성만·연결 없음). 기본 타임아웃도 노출해 오버라이드 필요 판정에 쓴다.
    app.state.delib_timeout_s = d_to
    app.state.mk_delib_llm = (lambda ts, _t=d_t, _m=d_mt, _e=d_eff: _mk_llm(_t, _m, _e, ts))
    app.state.llm_nostream = disable_stream
    if disable_stream:
        print("[agent] LLM_DISABLE_STREAMING=1 — 토큰 스트리밍 비활성(도구호출 우선 모드)")
    app.state.connections = _load_mcp_config()
    app.state.agent_cache = {}  # frozenset(groups) -> compiled ReAct agent
    print(f"[agent] ready — model={VLLM_MODEL}, mcp={list(app.state.connections)}")
    yield


app = FastAPI(title="HWAX Agent Server", version="0.3.0", lifespan=lifespan)
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class ChatRequest(BaseModel):
    message: str
    system_id: str | None = None  # sub-page context → tool scope (portal Phase 2; accepted, not yet used)
    groups: list[str] = []
    # 사용자 지정 우선 도구 — AI 의 도구 선택을 100% 신뢰할 수 없어, 사용자가 도구 카탈로그에서
    # 직접 고른 도구를 우선 사용하게 강제한다(바인딩 보장 + 시스템 프롬프트 지시).
    pinned_tools: list[str] = []
    # 사용자 지정 전문가(agent_type) — 이 전문가의 역할/시스템프롬프트를 페르소나로 주입해
    # '전문가와 대화' 모드가 된다(챗 시작 전 선택 패널에서 지정).
    pinned_agent: str | None = None
    # 멀티턴: 이전 대화 [{"role":"user"|"assistant","content":str}, …]. 검증/절단은 _history_messages 가 담당.
    history: list[dict] = []
    # 심의 손잡이 요청 오버라이드(웹 토글) — deliberation._resolve_opts 가 화이트리스트 키만 읽고
    # 클램프하므로 raw dict 로 받아도 안전. 미지정 키는 env 기본값. 심의(/심의) 경로에서만 쓰인다.
    delib_opts: dict | None = None


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


TOOL_RESULT_MAX = int(os.environ.get("TOOL_RESULT_MAX", "6000"))  # 도구 결과 절단(문자) — 컨텍스트 보호
TOOL_DESC_MAX = int(os.environ.get("TOOL_DESC_MAX", "240"))  # 도구 description 절단(문자) — 스키마 슬림
HIST_ITEM_MAX = int(os.environ.get("HIST_ITEM_MAX", "4000"))  # history 항목별 절단(문자)
HIST_BUDGET = int(os.environ.get("HIST_BUDGET", "16000"))  # history 전체 예산(문자) — 최신 우선
HIST_MAX_ITEMS = 40  # history 최대 항목 수


def _cap(text, limit=None):
    limit = limit or TOOL_RESULT_MAX
    s = text if isinstance(text, str) else str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n…[도구 출력 {len(s)}자 → {limit}자로 절단. 필요하면 limit/필터로 좁혀 다시 조회하세요]"


# ── 도구 산출 이미지 아티팩트 — base64 캡처를 파일로 저장하고 URL 로 치환 ─────────────
# AIDH 러너의 captured.images[].data(base64)가 그대로 LLM 에 가면 절단 캡에 깨지고 컨텍스트를
# 태운다. 파일로 강등해 /artifacts 로 서빙하고, 결과에는 짧은 url 만 남긴다(챗 이미지 렌더 근원).
ARTIFACT_DIR = os.environ.get(
    "ARTIFACT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts"))
os.makedirs(ARTIFACT_DIR, exist_ok=True)
_ARTIFACT_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
                 "image/webp": "webp", "image/svg+xml": "svg"}


def _stash_artifacts(obj, path_map: list | None = None) -> bool:
    import base64
    import hashlib
    changed = False

    def walk(v):
        nonlocal changed
        if isinstance(v, dict):
            imgs = v.get("images")
            if isinstance(imgs, list):
                for im in imgs:
                    if isinstance(im, dict) and isinstance(im.get("data"), str) and len(im["data"]) > 256:
                        try:
                            raw = base64.b64decode(im["data"])
                        except Exception:  # noqa: BLE001 — 손상 base64 는 그대로 둔다
                            continue
                        ext = _ARTIFACT_EXT.get(str(im.get("mime")), "png")
                        name = hashlib.sha256(raw).hexdigest()[:20] + "." + ext
                        with open(os.path.join(ARTIFACT_DIR, name), "wb") as f:
                            f.write(raw)
                        im.pop("data", None)
                        im["url"] = f"/agent/artifacts/{name}"
                        if path_map is not None and im.get("path"):
                            path_map.append((f"/work/{im['path']}", im["url"]))
                        changed = True
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(obj)
    return changed


# AIDH 가 결과에 싣는 자기 로컬 원점(첨부 URL 등) → 포털 공개 경로 재작성.
# 예: http://127.0.0.1:8001/attachments/… → /ai-data-hub/attachments/… (nginx 라우트 경유,
# 브라우저에서 로드 가능). env 로 원점/공개경로 오버라이드 가능.
_AIDH_LOCAL_ORIGINS = tuple(o.strip() for o in os.environ.get(
    "AIDH_LOCAL_ORIGINS", "http://127.0.0.1:8001,http://localhost:8001").split(",") if o.strip())
_AIDH_PUBLIC_BASE = os.environ.get("AIDH_PUBLIC_BASE", "/ai-data-hub")


def _rewrite_local_urls(s: str) -> str:
    for o in _AIDH_LOCAL_ORIGINS:
        if o in s:
            s = s.replace(o, _AIDH_PUBLIC_BASE)
    return s


# 이번 턴에 생성된 이미지 URL — 약한 모델이 ![](url) 인용을 빠뜨려도 결정적으로 첨부한다.
_turn_images: contextvars.ContextVar = contextvars.ContextVar("turn_images", default=None)


def _stash_image_items(items) -> list:
    """MCP content 의 이미지 블록(base64)을 파일로 저장하고 서빙 URL 목록을 돌려준다."""
    import base64
    import hashlib
    urls = []
    for it in items:
        if not isinstance(it, dict):
            continue
        # 키 이름은 계층마다 다르다 — MCP SDK(data/mimeType) vs 게이트웨이 정규화(base64/mime_type).
        data = it.get("data") or it.get("base64")
        mime = it.get("mimeType") or it.get("mime_type") or it.get("mime") or ""
        if not (isinstance(data, str) and len(data) > 256 and str(mime).startswith("image/")):
            continue
        try:
            raw = base64.b64decode(data)
        except Exception:  # noqa: BLE001 — 손상 base64 는 건너뜀
            continue
        name = hashlib.sha256(raw).hexdigest()[:20] + "." + _ARTIFACT_EXT.get(mime, "png")
        with open(os.path.join(ARTIFACT_DIR, name), "wb") as f:
            f.write(raw)
        urls.append(f"/agent/artifacts/{name}")
    bag = _turn_images.get()
    if bag is not None:
        for u in urls:
            if u not in bag:
                bag.append(u)
    return urls


def _extract_artifacts_text(s):
    """도구 결과 문자열에서 captured.images base64 → 파일+url 치환 + 로컬 URL 공개경로 재작성."""
    if isinstance(s, str):
        s = _rewrite_local_urls(s)
    if not isinstance(s, str) or '"images"' not in s or '"data"' not in s:
        return s
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return s
    pm: list = []
    if _stash_artifacts(obj, pm):
        out = json.dumps(obj, ensure_ascii=False)
        # 결과 텍스트(stdout 의 out_path 등)에 남은 샌드박스 경로를 서빙 URL 로 치환 —
        # LLM 이 /work/out.png 를 그대로 인용해 깨진 이미지가 되는 것을 막는다(실측).
        for old_p, url in pm:
            out = out.replace(old_p, url).replace(old_p.replace("/", "\\/"), url)
        return out
    return s


def _cap_tool(tool):
    """도구 결과를 절단해 LLM 컨텍스트를 보호한다 — 대량 조회(예: VOC 수천 건)가 그대로
    프롬프트에 들어가면 'maximum context length' 400 으로 채팅이 죽는다(실측 16385/16384)."""
    orig = tool.coroutine
    if orig is None:
        return tool

    def _norm(v):
        # MCP content-item 리스트 처리. str(list) 파이썬 repr 로 문자열화되면 URL 재작성·
        # 아티팩트 추출이 전부 우회된다(실측: 튜플 content 도 리스트).
        # 이미지 블록(type=image, base64 data)은 AIDH 가 JSON 밖 ImageContent 로 보내므로
        # 여기서 파일로 저장하고 URL 을 텍스트에 덧붙여야 LLM 이 ![](url) 로 인용할 수 있다.
        if isinstance(v, list) and v and all(isinstance(i, dict) for i in v):
            texts = [str(i.get("text", "")) for i in v if "text" in i]
            urls = _stash_image_items(v)
            v = "".join(texts)
            if urls:
                v += "\n[생성된 이미지 URL — 답변에 ![설명](url) 로 포함하세요]\n" + "\n".join(urls)
        return _extract_artifacts_text(v)

    def _arg_hint() -> str:
        """도구 인자 스키마 요약 — 실패 시 되돌려 LLM 이 인자명·타입·단위를 교정하게 한다."""
        sc = getattr(tool, "args_schema", None)
        if not isinstance(sc, dict):
            return ""
        req = set(sc.get("required") or [])
        rows = []
        for k, v in list((sc.get("properties") or {}).items())[:14]:
            if isinstance(v, dict):
                rows.append(f"{k}{'*' if k in req else ''}:{v.get('type','any')}"
                            + (f" — {str(v.get('description',''))[:60]}" if v.get("description") else ""))
        return " | ".join(rows)

    def _maybe_repair_hint(s):
        # 인자 검증 실패(누락·타입·단위)는 스키마만 다시 보여주면 다음 시도에서 대개 교정된다.
        if not isinstance(s, str):
            return s
        head = s[:600].lower()
        # 실측 문구: "3 validation errors for …Arguments / Field required" (pydantic),
        # "missing required arg" (매니페스트 검증), "입력 스키마 위반" (heax 앱 규약).
        if any(k in head for k in ("missing required arg", "입력 스키마 위반", "validation error",
                                   "field required", "unexpected keyword", "invalid arguments",
                                   "error executing tool")):
            hint = _arg_hint()
            if hint:
                return s + f"\n[인자 스키마 — 이 형식으로 교정해 다시 호출하세요]\n{hint}"
        return s

    async def capped(*a, **kw):
        try:
            out = await orig(*a, **kw)
        except Exception as exc:  # noqa: BLE001 — 인자 검증 실패는 코루틴 안에서 raise 된다(실측).
            # 예외를 그대로 올리면 LangChain 이 원문만 보여줘 LLM 이 같은 실수를 반복한다.
            # 스키마를 실어 돌려주면 다음 시도에서 인자명·타입·단위가 교정된다.
            hint = _arg_hint()
            msg = f"도구 {getattr(tool, 'name', '?')} 호출 실패: {str(exc)[:500]}"
            msg += f"\n[인자 스키마 — 이 형식으로 교정해 다시 호출]\n{hint}" if hint else ""
            # response_format 계약 준수 — content_and_artifact 도구는 2-튜플을 요구한다(실측).
            return (msg, None) if getattr(tool, "response_format", "") == "content_and_artifact" else msg
        if isinstance(out, tuple) and len(out) == 2:  # (content, artifact) 형식 보존
            return (_maybe_repair_hint(_cap(_norm(out[0]))), out[1])
        return _maybe_repair_hint(_cap(_norm(out)))

    tool.coroutine = capped
    return tool


def _cap_desc(s):
    if isinstance(s, str) and len(s) > TOOL_DESC_MAX:
        return s[:TOOL_DESC_MAX] + "…"
    return s


def _slim_tool(tool):
    """도구 스키마를 슬림하게 — 99개 도구의 긴 description 이 통째로 프롬프트에 들어가면
    16K 모델에서 첫 호출부터 'maximum context length' 400 이 난다(실측 16385/16384).
    tool.description 을 TOOL_DESC_MAX 로 절단하고, args_schema 가 JSON 스키마 dict 면
    각 필드 description 도 같은 캡을 적용한다(pydantic 모델이면 건드리지 않음)."""
    try:
        tool.description = _cap_desc(tool.description)
        schema = getattr(tool, "args_schema", None)
        if isinstance(schema, dict):  # MCP 어댑터 도구는 JSON 스키마 dict — 필드 description 도 절단
            for prop in schema.get("properties", {}).values():
                if isinstance(prop, dict) and isinstance(prop.get("description"), str):
                    prop["description"] = _cap_desc(prop["description"])
    except Exception as exc:  # 슬림 실패해도 도구 자체는 살린다
        print(f"[agent] tool slim skipped for {getattr(tool, 'name', '?')}: {exc!r}")
    return tool


def _tag_tool_app(tool):
    """도구 설명 앞에 소유 앱 라벨을 붙인다 — get_*/list_* 처럼 이름이 겹치는 도구가 여러 앱에
    걸쳐 있어(실측: get_* 7개 앱) LLM 이 도메인을 혼동한다. 이름을 안 바꾸므로 호출 계약은 불변."""
    try:
        _, label = _group_of(getattr(tool, "name", ""))
        if label and label != "기타" and not str(tool.description or "").startswith("["):
            tool.description = f"[{label}] {tool.description or ''}"
    except Exception:  # noqa: BLE001 — 태깅 실패해도 도구는 살린다
        pass
    return tool


def _attach_validation_hint(tool):
    """인자 검증 실패는 코루틴 진입 전(StructuredTool.ainvoke 의 pydantic 검증)에 발생해
    결과 래퍼로는 못 잡는다(실측). LangChain 의 handle_validation_error 훅에 스키마 힌트를
    실어, 인자명·타입·단위를 틀려도 다음 시도에서 교정되게 한다."""
    try:
        sc = getattr(tool, "args_schema", None)
        hint = ""
        if isinstance(sc, dict):
            req = set(sc.get("required") or [])
            rows = []
            for k, v in list((sc.get("properties") or {}).items())[:14]:
                if isinstance(v, dict):
                    rows.append(f"{k}{'*' if k in req else ''}:{v.get('type','any')}"
                                + (f" — {str(v.get('description',''))[:60]}" if v.get("description") else ""))
            hint = " | ".join(rows)
        if hint:
            tool.handle_validation_error = (
                lambda e, _h=hint: f"인자 오류: {e}\n[인자 스키마 — 이 형식으로 교정해 다시 호출]\n{_h}")
    except Exception:  # noqa: BLE001 — 훅 부착 실패해도 도구는 살린다
        pass
    return tool


def _prep_tool(tool):
    """도구 로드 직후 한 번에 적용하는 체인: 앱 태깅 + 검증힌트 + 스키마 슬림 + 결과 절단."""
    return _cap_tool(_slim_tool(_attach_validation_hint(_tag_tool_app(tool))))


# 소형 컨텍스트(dev 16K) 보호 — 도구 스키마 총량이 프롬프트를 넘치면 LLM 400으로 챗 전체가 죽는다.
# TOOL_MAX(0=무제한, prod 기본)로 바인딩 개수를 캡하고, 자주 쓰는 핵심 도구를 우선 남긴다.
TOOL_MAX = int(os.environ.get("TOOL_MAX", "0"))
_TOOL_PRIORITY = (
    "recommend_agents", "get_agent_session", "agent_search", "semantic_search", "list_records",
    "data_aggregate", "alert_check", "daily_briefing", "query_voc", "search_voc", "get_top_issues",
    "create_report_draft", "update_report_draft", "search_reports", "list_templates",
    "analyze_laminate", "evaluate_laminate", "solve_load_response", "list_materials", "plot_ashby",
    "search_documents", "search_knowledge", "get_material", "compare_products",
)


def _select_tools(tools: list, query: str = "", pinned: list[str] | None = None) -> list:
    """질의 관련도 기반 도구 선택 — 도구가 수백 개로 늘어도 '알파벳 순 절단'이 아니라
    '이 질문에 필요한 것부터' 남긴다. 우선순위: ① 사용자 지정(핀) ② 질의 어휘 관련도
    ③ 상시 핵심(라우팅·검색) ④ 나머지. 캡 미설정(0)이면 전부 바인딩(회귀 0)."""
    if TOOL_MAX <= 0 or len(tools) <= TOOL_MAX:
        return tools
    pin = set(pinned or [])
    core = {n: i for i, n in enumerate(_TOOL_PRIORITY)}
    # 관련도 — 이름+설명 어휘 겹침(_rank_tools 와 동일 원리, 여기선 전 도구 대상 점수만).
    qtok = _tok_query(query)
    def rel(t) -> float:
        if not qtok:
            return 0.0
        prof = f"{getattr(t,'name','')} {getattr(t,'description','') or ''}".lower().replace("_", " ")
        hit = sum(1 for k in qtok if k in prof)
        return hit / len(qtok)
    scored = [(t, rel(t)) for t in tools]
    ordered = sorted(scored, key=lambda x: (
        0 if getattr(x[0], "name", "") in pin else 1,   # 핀 최우선
        -round(x[1], 3),                                 # 질의 관련도 높은 순
        core.get(getattr(x[0], "name", ""), len(core)),  # 상시 핵심
        getattr(x[0], "name", ""),
    ))
    kept = [t for t, _ in ordered[:TOOL_MAX]]
    n_rel = sum(1 for t, sc in ordered[:TOOL_MAX] if sc > 0)
    print(f"[agent] tool select: {len(tools)}개 → {len(kept)}개 (질의관련 {n_rel}, 핀 {len(pin)})")
    return kept


# ── 도구 검색/카탈로그 — 사용자가 직접 도구를 확인·선택하게 하는 진입점 ─────────────
# 질의 토큰(조사·불용어 제거)과 도구 name+description 의 어휘 겹침으로 관련도 랭킹.
# recommend_svc 의 어휘 매칭과 같은 원리 — CJK 단일자(휨 등) 유지, 범용어 제거.
_TOOL_STOP = {
    "도구", "툴", "tool", "tools", "검색", "추천", "찾아", "찾아줘", "알려", "알려줘", "목록",
    "리스트", "보여", "보여줘", "가능", "사용", "쓸", "뭐", "뭐가", "무엇", "무슨", "어떤",
    "있어", "있냐", "있나", "관련", "대해", "대한", "해줘", "좀", "the", "a", "for", "what",
    "which", "list", "show", "available", "use",
}
_JOSA_T = ("으로", "에서", "에게", "까지", "부터", "이나", "을", "를", "이", "가", "은", "는",
           "의", "에", "과", "와", "도", "로", "만")


def _tok_query(text: str) -> set[str]:
    out = set()
    for t in re.split(r"[\s,./·|()\[\]{}:;\"'?!]+", (text or "").lower()):
        if not t:
            continue
        for j in sorted(_JOSA_T, key=len, reverse=True):   # 조사 제거(어간 2자+ 남을 때만)
            if t.endswith(j) and len(t) - len(j) >= 2:
                t = t[: -len(j)]
                break
        if t in _TOOL_STOP:
            continue
        if len(t) >= 2 or any("가" <= ch <= "힣" for ch in t):
            out.add(t)
    return out


# 도구 → 소유 MCP 앱(백엔드) 매핑 — 166개가 평평하게 쏟아지면 고르기 어려워, 게이트웨이의
# /tools-map 으로 '어느 앱의 기능인지'를 붙여 계층 선택이 되게 한다(짧은 TTL 캐시).
_GW_HTTP = os.environ.get("GATEWAY_HTTP_BASE", "http://127.0.0.1:9110")
_TOOLS_MAP_CACHE: dict = {"at": 0.0, "map": {}}
_GROUP_LABEL = {
    "ai-data-hub": "AI 데이터 허브", "mx-white-paper": "MX 백서", "reportarchive": "리포트 아카이브",
    "signalforge": "SignalForge VOC", "smart-twin-cluster": "시뮬레이션 클러스터",
    "heax-thermal_shock_mcp": "열충격 해석", "heax-materialtwin_web": "재료 물성(MaterialTwin)",
    "heax-laminate_analyzer_mcp": "적층 복합재 해석", "heax-web_design_agents": "웹 디자인 에이전트",
    "_gateway": "게이트웨이 공통",
}


def _tools_map() -> dict:
    import time
    import urllib.request
    now = time.time()
    if _TOOLS_MAP_CACHE["map"] and now - _TOOLS_MAP_CACHE["at"] < 300:
        return _TOOLS_MAP_CACHE["map"]
    try:
        with urllib.request.urlopen(f"{_GW_HTTP}/tools-map", timeout=5) as r:
            m = (json.loads(r.read()) or {}).get("map") or {}
        if m:
            _TOOLS_MAP_CACHE.update({"at": now, "map": m})
    except Exception as exc:  # noqa: BLE001 — 매핑 실패 시 그룹 없이 동작(회귀 0)
        print(f"[tools] map fetch failed: {exc!r}")
    return _TOOLS_MAP_CACHE["map"]


def _pretty_group(key: str) -> str:
    """미등록 앱의 표시 이름 자동 생성 — 새 MCP 앱이 붙어도 코드 수정 없이 읽을 만하게 뜬다.
    예: heax-voice_recorder → Voice Recorder, my-new-app → My New App."""
    if not key:
        return "기타"
    k = key[5:] if key.startswith("heax-") else key
    k = k.removesuffix("_mcp").removesuffix("-mcp")
    words = [w for w in re.split(r"[-_\s]+", k) if w]
    return " ".join(w if w.isupper() else w.capitalize() for w in words) or key


def _group_of(name: str) -> tuple:
    key = _tools_map().get(name, "")
    return key, _GROUP_LABEL.get(key) or _pretty_group(key)


def _rank_tools(tools: dict, query: str, top_k: int = 12) -> list[dict]:
    """게이트웨이 도구를 질의 관련도순으로 — [{name, desc, score}] (score>0 만)."""
    qtok = _tok_query(query)
    if not qtok:
        return []
    scored = []
    for name, t in tools.items():
        profile = f"{name} {getattr(t, 'description', '') or ''}".lower().replace("_", " ")
        hits = sum(1 for tk in qtok if tk in profile)
        if hits:
            scored.append((hits / len(qtok), name, (getattr(t, "description", "") or "")[:160]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out = []
    for sc, n, d in scored[:top_k]:
        gk, gl = _group_of(n)
        out.append({"name": n, "desc": d, "score": round(sc, 3), "group": gk, "group_label": gl})
    return out


def _tool_catalog(tools: dict) -> list[dict]:
    out = []
    for n, t in sorted(tools.items()):
        gk, gl = _group_of(n)
        out.append({"name": n, "desc": (getattr(t, "description", "") or "")[:160],
                    "group": gk, "group_label": gl})
    return out


_TOOL_SEARCH_TRIGGERS = ("/도구", "/tools", "/tool")
_TOOL_SEARCH_RE = re.compile(r"(도구|툴|tool)\w*\s*(검색|추천|찾|알려|목록|리스트|보여|뭐|무엇|어떤|있)", re.IGNORECASE)


def is_tool_search(message: str) -> bool:
    m = (message or "").strip()
    if any(m.startswith(t) for t in _TOOL_SEARCH_TRIGGERS):
        return True
    # 짧은 발화의 '도구 뭐 있어/추천해줘' 류만 — 긴 작업 지시문('~도구를 사용해 분석하라')은 오탐 방지
    return len(m) <= 80 and bool(_TOOL_SEARCH_RE.search(m))


def strip_tool_trigger(message: str) -> str:
    m = (message or "").strip()
    for t in _TOOL_SEARCH_TRIGGERS:
        if m.startswith(t):
            return m[len(t):].strip()
    return m


async def run_tool_search(app: FastAPI, query: str, groups: list[str]):
    """도구 카탈로그 SSE — 코드가 결정적으로 처리(LLM 미경유). 프론트가 tools 이벤트를 받아
    선택 UI 를 그리고, 사용자가 고른 도구는 다음 발화부터 pinned_tools 로 실린다."""
    yield _sse("status", {"step": "사용 가능 도구 검색 중", "tool": None})
    try:
        tools = await _tools_by_name(app, groups)
    except Exception as exc:  # noqa: BLE001
        print(f"[tools] load failed: {exc!r}")
        tools = {}
    if not tools:
        yield _sse("result", {"type": "text", "content": "게이트웨이 도구를 불러오지 못했습니다 — 게이트웨이 상태를 확인하세요."})
        yield _sse("done", {})
        return
    recommended = _rank_tools(tools, query)
    catalog = _tool_catalog(tools)
    yield _sse("tools", {"query": query, "recommended": recommended, "all": catalog})
    if recommended:
        head = ", ".join(r["name"] for r in recommended[:5])
        text = (f"질의와 관련된 도구 {len(recommended)}개를 추천합니다(상위: {head}). "
                f"전체 {len(catalog)}개 중 아래 카드에서 직접 선택·추가하면 이후 대화에서 그 도구를 우선 사용합니다.")
    else:
        text = (f"사용 가능한 도구 {len(catalog)}개입니다. 아래 카드에서 검색해 직접 선택하면 "
                f"이후 대화에서 그 도구를 우선 사용합니다.")
    yield _sse("token", {"delta": text})
    yield _sse("result", {"type": "text", "content": text})
    yield _sse("done", {})



async def _agent_for(app: FastAPI, groups: list[str], pinned: list[str] | None = None,
                     query: str = ""):
    """ReAct agent whose tools are the gateway's group-filtered set for this caller.
    Cached by group-set; the tools carry the groups header so tool *calls* are scoped too.
    pinned 은 TOOL_MAX 캡 환경에서만 바인딩 구성을 바꾸므로 그때만 캐시 키에 포함한다
    (무제한 환경은 바인딩 동일 → 키 분화 없이 시스템 프롬프트 지시로만 우선순위 반영)."""
    pin_key = tuple(sorted(pinned)) if (pinned and TOOL_MAX > 0) else ()
    # 캡이 걸린 환경에서는 바인딩 도구가 질의에 따라 달라진다 — 질의 토큰을 캐시 키에 넣어
    # 같은 주제는 재사용하고 다른 주제는 새로 구성한다(무제한 환경은 종전대로 그룹 단위 캐시).
    q_key = tuple(sorted(_tok_query(query))[:8]) if TOOL_MAX > 0 else ()
    key = (frozenset(groups), pin_key, q_key)
    cache = app.state.agent_cache
    if key not in cache:
        tools = []
        load_failed = False
        connections = app.state.connections
        if connections:
            try:
                scoped = _with_groups(connections, sorted(groups))
                tools = _select_tools([_prep_tool(t) for t in await MultiServerMCPClient(scoped).get_tools()],
                                      query, pinned)
            except Exception as exc:  # gateway down → degrade to a no-tool agent, don't crash
                load_failed = True
                print(f"[agent] tool load failed for groups={sorted(groups)} ({exc}); no tools")
        agent = create_react_agent(app.state.llm, tools)
        if load_failed:
            # 실패 결과는 캐시하지 않는다 — 캐시하면 게이트웨이가 복구돼도 이 그룹은
            # 재시작 전까지 영구 no-tool 이 된다(조용한 최악의 실패 모드). 이번 요청만
            # 도구 없이 응답하고, 다음 요청에서 재시도한다.
            return agent
        cache[key] = agent
    return cache[key]


def _history_messages(history: list[dict]) -> list[tuple[str, str]]:
    """멀티턴 history 를 검증·절단해 LangChain 메시지 tuple 로 만든다.
    방어: role 이 user/assistant 외면 무시, 항목별 HIST_ITEM_MAX 절단, 항목 수 최대
    HIST_MAX_ITEMS, 전체는 최신 것 우선으로 HIST_BUDGET 안에서 오래된 것부터 버림."""
    items: list[tuple[str, str]] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        role, content = entry.get("role"), entry.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content:
            continue
        if len(content) > HIST_ITEM_MAX:
            content = content[:HIST_ITEM_MAX] + "…"
        items.append((role, content))
    items = items[-HIST_MAX_ITEMS:]
    kept: list[tuple[str, str]] = []
    used = 0
    for role, content in reversed(items):  # 최신부터 예산을 채우고, 넘치는 오래된 것은 버림
        if used + len(content) > HIST_BUDGET:
            break
        kept.append((role, content))
        used += len(content)
    kept.reverse()
    return kept


def _tool_preview(v, n: int = 220) -> str:
    """활동 패널 드릴다운용 도구 입출력 요약 — 안전 문자열화 + 공백 압축 + 절단."""
    try:
        if v is None:
            return ""
        content = getattr(v, "content", None)   # ToolMessage 등 랩퍼 언랩
        if content is not None:
            v = content
        s = json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else str(v)
        s = re.sub(r"\s+", " ", s).strip()
        return s[:n]
    except Exception:  # noqa: BLE001 — 미리보기 실패가 스트림을 죽이면 안 됨
        return ""


async def _persona_role(app: FastAPI, groups: list[str], agent_type: str) -> str:
    """선택 전문가의 역할 원문 로드(get_agent_session) — 프로세스 캐시(불변 가정, 재시작 시 갱신)."""
    cache = getattr(app.state, "persona_cache", None)
    if cache is None:
        cache = app.state.persona_cache = {}
    if agent_type in cache:
        return cache[agent_type]
    role = ""
    try:
        tools = await _tools_by_name(app, groups)
        sess = _first_dict(_parse_json(await _call(tools, "get_agent_session", {"agent_type": agent_type})))
        sd = _first_dict(sess.get("data", sess))
        role = str(sd.get("system_prompt") or sd.get("description") or "")[:4000]
    except Exception as exc:  # noqa: BLE001 — 실패 시 페르소나 없이 일반 챗
        print(f"[agent] persona load failed for {agent_type}: {exc!r}")
    if role:
        cache[agent_type] = role
    return role


async def _agent_stream(app: FastAPI, req: ChatRequest) -> AsyncIterator[bytes]:
    full: list[str] = []
    turn_imgs: list = []
    _turn_images.set(turn_imgs)
    yield _sse("status", {"step": "분석 중", "tool": None})
    try:
        # 사용자 지정 우선 도구 — 도구 카탈로그에서 직접 고른 것. 바인딩 보장(+캡 환경 우선순위)
        # 과 시스템 프롬프트 지시 둘 다로 강제한다(모델의 자율 선택은 유지 — 금지가 아니라 우선).
        pinned = [str(n)[:80] for n in (req.pinned_tools or []) if isinstance(n, str) and n.strip()][:12]
        agent = await _agent_for(app, req.groups, pinned, req.message)
        sys_prompt = SYSTEM_PROMPT
        if pinned:
            sys_prompt += ("\n\n[사용자 지정 우선 도구]\n" + ", ".join(pinned)
                           + "\n사용자가 직접 선택한 도구다. 이 질문 처리에 적합하면 반드시 이 도구들을 "
                             "우선 호출하고, 결과를 답변에 인용하라(다른 도구 사용 금지는 아님).")
        # 지정 전문가 페르소나 — 선택 패널에서 고른 전문가의 역할로 답하는 '전문가와 대화' 모드.
        agent_key = (req.pinned_agent or "").strip()[:120]
        if agent_key:
            role = await _persona_role(app, req.groups, agent_key)
            if role:
                sys_prompt += (f"\n\n[전문가 페르소나 — 사용자가 선택]\n너는 '{agent_key}' 전문가다. "
                               f"아래 역할과 범위를 지켜 그 전문가로서 답하라.\n{role}\n"
                               f"이 전문가 도메인의 데이터 조회는 agent_search(\"{agent_key}\", 질문) 를 우선 사용하라.")
        messages = [("system", sys_prompt), *_history_messages(req.history), ("user", req.message)]
        inputs = {"messages": messages}
        async for event in agent.astream_events(inputs, version="v2"):
            kind = event["event"]
            if kind == "on_chat_model_stream":
                token = event["data"]["chunk"].content
                if token:  # empty on tool-call delta chunks — guard
                    full.append(token)
                    yield _sse("token", {"delta": token})
            elif kind == "on_chat_model_end" and getattr(app.state, "llm_nostream", False):
                # 비스트리밍 모드 — stream 이벤트가 없으므로 완결 응답에서 텍스트를 회수해
                # 모델 턴 단위로 방출한다(도구 호출만 한 턴은 content 가 비어 자연히 skip).
                out_msg = event.get("data", {}).get("output")
                content = getattr(out_msg, "content", "") or ""
                if isinstance(content, list):  # 멀티파트 content 방어
                    content = "".join(
                        p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
                if content:
                    full.append(content)
                    yield _sse("token", {"delta": content})
            elif kind == "on_tool_start":
                args = _tool_preview(event.get("data", {}).get("input"))
                yield _sse("status", {"step": f"도구 호출: {event['name']}", "tool": event["name"],
                                      **({"detail": args} if args else {})})
            elif kind == "on_tool_end":
                # 도구 출력에 실린 아티팩트 URL 수집 — contextvar 는 LangGraph 실행 컨텍스트를
                # 넘지 못해(실측 빈 값) 스트림 쪽에서 직접 회수한다.
                _raw = event.get("data", {}).get("output")
                _txt = getattr(_raw, "content", _raw)
                if isinstance(_txt, str):
                    for _u in re.findall(r"/agent/artifacts/[A-Za-z0-9][A-Za-z0-9_.-]*", _txt):
                        if _u not in turn_imgs:
                            turn_imgs.append(_u)
                out = _tool_preview(_raw)
                yield _sse("status", {"step": f"도구 완료: {event['name']}", "tool": event["name"],
                                      **({"result_preview": out} if out else {})})
    except Exception as exc:
        # 상세는 서버 로그에만(내부 유출 방지). 단 AGENT_DEBUG_ERRORS=1 이면 예외 타입·메시지를
        # 브라우저 응답에도 실어 운영자가 바로 원인을 본다(기본 꺼짐 — 켜면 재시작 필요).
        print(f"[agent] chat error: {exc!r}")
        import traceback as _tb
        _tb.print_exc()
        detail = ""
        if os.environ.get("AGENT_DEBUG_ERRORS") == "1":
            detail = f" — {type(exc).__name__}: {str(exc)[:400]}"
        yield _sse("error", {"code": "agent_error", "message": f"에이전트 처리 중 오류{detail}"})
        yield _sse("done", {})
        return
    text = "".join(full)
    # 도구가 만든 그래프를 모델이 인용하지 않았으면 코드가 붙인다 — 소형 모델의 지시 누락으로
    # 생성된 이미지가 화면에서 사라지는 것을 막는 결정적 보강(중복 첨부는 방지).
    # 마크다운 이미지 형태(](url))가 없으면 첨부 — 모델이 URL 을 본문에 평문으로만 적은 경우도 보강.
    missing = [u for u in turn_imgs if f"]({u})" not in text]
    if missing:
        add = "\n\n" + "\n".join(f"![생성된 그래프]({u})" for u in missing)
        text += add
        yield _sse("token", {"delta": add})
    yield _sse("result", {"type": "text", "content": text})
    yield _sse("done", {})


@app.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    # 심의 모드: "/심의 <질문>" → 다중 라운드 전문가 심의 파이프라인(코드가 오케스트레이션, vLLM=GLM 이 추론).
    # 정본은 역량 있는 Claude(개인 Claude via MCP); 이건 GLM 연결 시 포털 챗으로도 되게 하는 진입점.
    if is_deliberation(req.message):
        stream = run_deliberation(app, strip_trigger(req.message), req.groups, req.delib_opts)
    elif is_report_save(req.message):
        # "/보고서 <선택: 결론>" → 대화 이력을 코드가 blocks 로 만들어 RA 저장(결정적 — LLM 미경유).
        stream = run_report_save(app, strip_report_trigger(req.message), req.history, req.groups)
    elif is_tool_search(req.message):
        # "/도구 <질의>" 또는 '도구 뭐 있어' 류 → 도구 카탈로그+추천을 SSE tools 이벤트로(결정적).
        # 프론트가 선택 UI 를 그리고, 고른 도구는 다음 발화부터 pinned_tools 로 우선 사용된다.
        stream = run_tool_search(app, strip_tool_trigger(req.message), req.groups)
    else:
        stream = _agent_stream(app, req)
    return StreamingResponse(stream, media_type="text/event-stream", headers=SSE_HEADERS)


class ExpertsRequest(BaseModel):
    message: str
    groups: list[str] = []


def _parse_json_multi(text) -> list:
    """AIDH list 반환 툴은 원소별 content 로 직렬화돼 _call 이 이어붙인다 — 연결된 JSON
    객체들을 모두 추출해 리스트로. (단일 배열/객체도 지원.)"""
    if isinstance(text, list):
        return text
    s = str(text or "").strip()
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else [v]
    except Exception:
        pass
    dec = json.JSONDecoder()
    out: list = []
    i = 0
    while i < len(s):
        while i < len(s) and s[i] not in "{[":
            i += 1
        if i >= len(s):
            break
        try:
            o, end = dec.raw_decode(s, i)
            out.append(o)
            i = end
        except Exception:
            i += 1
    return out


@app.post("/deliberate/experts")
async def deliberate_experts(req: ExpertsRequest) -> dict:
    """심의 전 전문가 선정 미리보기 — 자동 추천(recommend_agents) + 전체 풀(list_agents compact).
    프론트가 추천을 미리 보여주고, 사용자가 확인·수동추가한 personas 로 심의를 실행한다."""
    tools = await _tools_by_name(app, req.groups)
    if not tools:
        return {"recommended": [], "pool": [], "error": "gateway_unavailable"}

    def _norm(d: dict) -> dict:
        d = _first_dict(d)
        key = d.get("agent_type") or d.get("id") or ""
        return {"key": key, "name": d.get("name") or key,
                "role": (d.get("description") or "")[:280],
                "tags": list(d.get("common_tags") or [])}

    # 질문 연관 순위 — recommend_agents 를 넉넉히(top_k=CAND) 받아 관련도순 후보로 쓴다.
    # recommended = 상위 N(기본 선택), candidates = 관련 전문가 목록(수동 추가 기본 노출·검색 우선).
    cand_k = int(os.environ.get("EXPERT_CANDIDATE_TOP_K", "40"))
    candidates: list[dict] = []
    try:
        recd = _parse_json(await _call(tools, "recommend_agents", {"q": req.message, "top_k": cand_k}))
        items = recd if isinstance(recd, list) else (
            (recd or {}).get("recommendations") or (recd or {}).get("agents") or (recd or {}).get("data") or [])
        for it in (items or [])[:cand_k]:
            it = _first_dict(it)
            n = _norm(it)
            if not n["key"]:
                continue
            n["score"] = it.get("score")
            n["why"] = it.get("why") or ""
            candidates.append(n)
    except Exception as exc:  # noqa: BLE001 — 추천 실패해도 풀로 수동 선택 가능
        print(f"[experts] recommend failed: {exc!r}")
    recommended = candidates[:N_PERSONAS]

    # 전체 풀(compact — 관련도 밖 전문가까지 키워드로 찾을 때). 647+ 규모라 role 은 싣지 않는다.
    pool: list[dict] = []
    try:
        for a in _parse_json_multi(await _call(tools, "list_agents", {"compact": True})):
            n = _norm(a)
            if n["key"]:
                pool.append({"key": n["key"], "name": n["name"], "tags": n["tags"]})
    except Exception as exc:  # noqa: BLE001
        print(f"[experts] list_agents failed: {exc!r}")

    # 도구 — 파이프라인이 자동 쓰는 도구(정보 표시) + 주제 관련 추천 + 전체 카탈로그(검색 추가용).
    # 사용자가 고른 도구(delib_opts.tools)는 심의에서 실제 호출돼 정량 근거로 주입된다.
    _PIPELINE_TOOLS = ("recommend_agents", "get_agent_session", "alert_check", "daily_briefing",
                       "query_voc", "hybrid_search", "search_knowledge", "search_reports",
                       "create_report_draft")
    tools_info = {
        "recommended": _rank_tools(tools, req.message),
        "pipeline": [n for n in _PIPELINE_TOOLS if n in tools],
        "all": _tool_catalog(tools),
    }
    return {"recommended": recommended, "candidates": candidates, "pool": pool, "tools": tools_info}


class AgentDetailRequest(BaseModel):
    key: str
    groups: list[str] = []


@app.post("/catalog/agent")
async def catalog_agent(req: AgentDetailRequest) -> dict:
    """전문가 상세 + 보유 지식(레코드 목록) — 브라우즈 UI 용(결정적·LLM 미경유).
    LLM 텍스트 나열은 결과 절단 캡에 걸려 잘리므로, 탐색은 이 데이터로 UI 가 그린다."""
    tools = await _tools_by_name(app, req.groups)
    if not tools:
        return {"error": "gateway_unavailable"}
    key = req.key.strip()[:120]
    out: dict = {"key": key, "name": key, "role": "", "tags": [], "samples": [], "records": []}
    try:
        sess = _first_dict(_parse_json(await _call(tools, "get_agent_session", {"agent_type": key})))
        sd = _first_dict(sess.get("data", sess))
        out["name"] = sd.get("name") or key
        out["role"] = str(sd.get("description") or "")[:2000]
        out["tags"] = list(sd.get("common_tags") or [])[:30]
        out["samples"] = [str(s)[:200] for s in (sd.get("sample_queries") or [])][:5]
    except Exception as exc:  # noqa: BLE001 — 상세 실패해도 지식 목록은 시도
        print(f"[catalog] agent session failed: {exc!r}")
    try:
        raw = _parse_json(await _call(tools, "list_records", {"agents": [key], "limit": 20}))
        recs = raw if isinstance(raw, list) else (
            (raw or {}).get("records") or (raw or {}).get("items") or (raw or {}).get("data") or [])
        for r in recs[:20]:
            r = _first_dict(r)
            rid = r.get("id") or r.get("record_id") or ""
            title = r.get("title") or ""
            if rid or title:
                out["records"].append({"id": str(rid)[:80], "title": str(title)[:160],
                                       "data_type": str(r.get("data_type") or "")[:20]})
    except Exception as exc:  # noqa: BLE001
        print(f"[catalog] records failed: {exc!r}")
    return out


@app.get("/artifacts/{name}")
def get_artifact(name: str) -> FileResponse:
    """도구 산출 이미지 서빙 — _stash_artifacts 가 저장한 파일만(이름은 해시라 열거 불가)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name) or ".." in name:
        raise HTTPException(status_code=404)
    p = os.path.join(ARTIFACT_DIR, name)
    if not os.path.isfile(p):
        raise HTTPException(status_code=404)
    return FileResponse(p)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "model": VLLM_MODEL,
        "vllm": VLLM_BASE_URL,
        "mcp": list(getattr(app.state, "connections", {})),
        "tool_scoping": "gateway (X-HWAX-Groups)",
        "tool_desc_max": TOOL_DESC_MAX,
        "tool_max": TOOL_MAX,   # 0=무제한. >0 이고 게이트웨이 도구수보다 작으면 챗에 일부 도구 미바인딩
        "hist_budget": HIST_BUDGET,
    }
