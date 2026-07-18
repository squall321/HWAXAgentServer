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

import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from deliberation import is_deliberation, run_deliberation, strip_trigger
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
    "다음 요청은 반드시 제공된 도구를 사용하세요 — 보고서/템플릿(Report Archive), "
    "VOC·고객의 소리(SignalForge), 백서·기술문서(MX White Paper), 데이터셋·데이터 허브(AI Data Hub), "
    "시뮬레이션 클러스터·Slurm 잡/노드/큐 조회(Smart Twin Cluster, slurm_* 도구). "
    "도구 결과에 근거해 답하고, 추측하지 마세요. "
    "조회 도구는 항상 좁게 호출하세요 — limit(기본 10 이하)·필터·기간을 지정하고, "
    "대량 데이터가 필요하면 요약/집계 도구를 우선 사용하세요. "
    "그래프·차트·시각화를 요청받으면 도구로 데이터를 조회한 뒤, 외부 리소스 없이 "
    "자체 완결된(self-contained, 인라인 SVG/스크립트) HTML을 ```html 코드블록으로 출력하세요 — "
    "챗이 미리보기로 렌더링합니다.\n\n"
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
    app.state.llm = ChatOpenAI(
        base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY, model=VLLM_MODEL, temperature=0
    )
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
    # 멀티턴: 이전 대화 [{"role":"user"|"assistant","content":str}, …]. 검증/절단은 _history_messages 가 담당.
    history: list[dict] = []


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


def _cap_tool(tool):
    """도구 결과를 절단해 LLM 컨텍스트를 보호한다 — 대량 조회(예: VOC 수천 건)가 그대로
    프롬프트에 들어가면 'maximum context length' 400 으로 채팅이 죽는다(실측 16385/16384)."""
    orig = tool.coroutine
    if orig is None:
        return tool

    async def capped(*a, **kw):
        out = await orig(*a, **kw)
        if isinstance(out, tuple) and len(out) == 2:  # (content, artifact) 형식 보존
            return (_cap(out[0]), out[1])
        return _cap(out)

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


def _prep_tool(tool):
    """도구 로드 직후 한 번에 적용하는 체인: 스키마 슬림 + 결과 절단."""
    return _cap_tool(_slim_tool(tool))


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


def _cap_tool_count(tools: list) -> list:
    if TOOL_MAX <= 0 or len(tools) <= TOOL_MAX:
        return tools
    rank = {n: i for i, n in enumerate(_TOOL_PRIORITY)}
    ordered = sorted(tools, key=lambda t: (rank.get(getattr(t, "name", ""), len(rank)), getattr(t, "name", "")))
    kept = ordered[:TOOL_MAX]
    print(f"[agent] TOOL_MAX={TOOL_MAX} — {len(tools)}개 중 {len(kept)}개 바인딩(소형 컨텍스트 보호)")
    return kept


async def _agent_for(app: FastAPI, groups: list[str]):
    """ReAct agent whose tools are the gateway's group-filtered set for this caller.
    Cached by group-set; the tools carry the groups header so tool *calls* are scoped too."""
    key = frozenset(groups)
    cache = app.state.agent_cache
    if key not in cache:
        tools = []
        load_failed = False
        connections = app.state.connections
        if connections:
            try:
                scoped = _with_groups(connections, sorted(groups))
                tools = _cap_tool_count([_prep_tool(t) for t in await MultiServerMCPClient(scoped).get_tools()])
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


async def _agent_stream(app: FastAPI, req: ChatRequest) -> AsyncIterator[bytes]:
    full: list[str] = []
    yield _sse("status", {"step": "분석 중", "tool": None})
    try:
        agent = await _agent_for(app, req.groups)
        messages = [("system", SYSTEM_PROMPT), *_history_messages(req.history), ("user", req.message)]
        inputs = {"messages": messages}
        async for event in agent.astream_events(inputs, version="v2"):
            kind = event["event"]
            if kind == "on_chat_model_stream":
                token = event["data"]["chunk"].content
                if token:  # empty on tool-call delta chunks — guard
                    full.append(token)
                    yield _sse("token", {"delta": token})
            elif kind == "on_tool_start":
                args = _tool_preview(event.get("data", {}).get("input"))
                yield _sse("status", {"step": f"도구 호출: {event['name']}", "tool": event["name"],
                                      **({"detail": args} if args else {})})
            elif kind == "on_tool_end":
                out = _tool_preview(event.get("data", {}).get("output"))
                yield _sse("status", {"step": f"도구 완료: {event['name']}", "tool": event["name"],
                                      **({"result_preview": out} if out else {})})
    except Exception as exc:
        # Don't leak internals (tool inputs, config, LLM details) to the browser; log server-side.
        print(f"[agent] chat error: {exc!r}")
        yield _sse("error", {"code": "agent_error", "message": "에이전트 처리 중 오류"})
        yield _sse("done", {})
        return
    yield _sse("result", {"type": "text", "content": "".join(full)})
    yield _sse("done", {})


@app.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    # 심의 모드: "/심의 <질문>" → 다중 라운드 전문가 심의 파이프라인(코드가 오케스트레이션, vLLM=GLM 이 추론).
    # 정본은 역량 있는 Claude(개인 Claude via MCP); 이건 GLM 연결 시 포털 챗으로도 되게 하는 진입점.
    if is_deliberation(req.message):
        stream = run_deliberation(app, strip_trigger(req.message), req.groups)
    else:
        stream = _agent_stream(app, req)
    return StreamingResponse(stream, media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "model": VLLM_MODEL,
        "vllm": VLLM_BASE_URL,
        "mcp": list(getattr(app.state, "connections", {})),
        "tool_scoping": "gateway (X-HWAX-Groups)",
        "tool_desc_max": TOOL_DESC_MAX,
        "hist_budget": HIST_BUDGET,
    }
