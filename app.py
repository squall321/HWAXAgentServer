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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
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
    "보고서 템플릿·작성, VOC(고객의 소리) 데이터 조회·분석 등은 반드시 제공된 도구를 사용하세요. "
    "도구 결과에 근거해 답하고, 추측하지 마세요. "
    "조회 도구는 항상 좁게 호출하세요 — limit(기본 10 이하)·필터·기간을 지정하고, "
    "대량 데이터가 필요하면 요약/집계 도구를 우선 사용하세요.\n\n"
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


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


TOOL_RESULT_MAX = int(os.environ.get("TOOL_RESULT_MAX", "6000"))  # 도구 결과 절단(문자) — 컨텍스트 보호


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


async def _agent_for(app: FastAPI, groups: list[str]):
    """ReAct agent whose tools are the gateway's group-filtered set for this caller.
    Cached by group-set; the tools carry the groups header so tool *calls* are scoped too."""
    key = frozenset(groups)
    cache = app.state.agent_cache
    if key not in cache:
        tools = []
        connections = app.state.connections
        if connections:
            try:
                scoped = _with_groups(connections, sorted(groups))
                tools = [_cap_tool(t) for t in await MultiServerMCPClient(scoped).get_tools()]
            except Exception as exc:  # gateway down → degrade to a no-tool agent, don't crash
                print(f"[agent] tool load failed for groups={sorted(groups)} ({exc}); no tools")
        cache[key] = create_react_agent(app.state.llm, tools)
    return cache[key]


async def _agent_stream(app: FastAPI, req: ChatRequest) -> AsyncIterator[bytes]:
    full: list[str] = []
    yield _sse("status", {"step": "분석 중", "tool": None})
    try:
        agent = await _agent_for(app, req.groups)
        inputs = {"messages": [("system", SYSTEM_PROMPT), ("user", req.message)]}
        async for event in agent.astream_events(inputs, version="v2"):
            kind = event["event"]
            if kind == "on_chat_model_stream":
                token = event["data"]["chunk"].content
                if token:  # empty on tool-call delta chunks — guard
                    full.append(token)
                    yield _sse("token", {"delta": token})
            elif kind == "on_tool_start":
                yield _sse("status", {"step": f"도구 호출: {event['name']}", "tool": event["name"]})
            elif kind == "on_tool_end":
                yield _sse("status", {"step": f"도구 완료: {event['name']}", "tool": event["name"]})
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
    return StreamingResponse(
        _agent_stream(app, req), media_type="text/event-stream", headers=SSE_HEADERS
    )


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "model": VLLM_MODEL,
        "vllm": VLLM_BASE_URL,
        "mcp": list(getattr(app.state, "connections", {})),
        "tool_scoping": "gateway (X-HWAX-Groups)",
    }
