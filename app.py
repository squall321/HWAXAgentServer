"""HWAX Agent Server — LangGraph ReAct + MCP tool fan-out.

The portal is a thin proxy; the real model call + tool use live here (계획서 §3).
Flow: ChatDock → portal /agent/chat → THIS → (vLLM for the LLM, MCP servers for tools).

A LangGraph ReAct agent runs the loop: the LLM (Qwen2.5-7B on vLLM, tool-calling
enabled) decides when to call MCP tools; tools are loaded from MCP servers via
langchain-mcp-adapters. We stream the run as the portal's §5 SSE contract:
  status (incl. tool calls) → token×N → result → done (or error).

If MCP/LLM wiring is unavailable the server still answers — it just won't have tools.

Env:
  VLLM_BASE_URL   OpenAI-compatible base (default http://127.0.0.1:8000/v1)
  VLLM_MODEL      served model name (default qwen2.5-7b-dev)
  MCP_SERVERS     comma-separated name=url pairs (default demo=http://127.0.0.1:8011/mcp)
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
MCP_SERVERS = os.environ.get("MCP_SERVERS", "demo=http://127.0.0.1:8011/mcp")
SYSTEM_PROMPT = (
    "당신은 HWAX 포털의 어시스턴트입니다. 한국어로 간결·정확하게 답하세요. "
    "계산·시간 등 도구로 처리할 수 있는 요청은 반드시 제공된 도구를 사용하세요."
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build the LLM (vLLM, OpenAI-compatible) + load MCP tools + compile the ReAct agent once.
    llm = ChatOpenAI(base_url=VLLM_BASE_URL, api_key="EMPTY", model=VLLM_MODEL, temperature=0)
    tools = []
    servers = _parse_servers(MCP_SERVERS)
    if servers:
        try:
            tools = await MultiServerMCPClient(servers).get_tools()
        except Exception as exc:  # MCP down → degrade to a no-tool agent, don't crash
            print(f"[agent] MCP tool load failed ({exc}); running without tools")
    app.state.agent = create_react_agent(llm, tools)
    app.state.tool_names = [t.name for t in tools]
    print(f"[agent] ready — model={VLLM_MODEL}, tools={app.state.tool_names}")
    yield


app = FastAPI(title="HWAX Agent Server", version="0.2.0", lifespan=lifespan)
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class ChatRequest(BaseModel):
    message: str
    system_id: str | None = None
    groups: list[str] = []


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def _agent_stream(app: FastAPI, req: ChatRequest) -> AsyncIterator[bytes]:
    agent = app.state.agent
    inputs = {"messages": [("system", SYSTEM_PROMPT), ("user", req.message)]}
    full: list[str] = []
    yield _sse("status", {"step": "분석 중", "tool": None})
    try:
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
        yield _sse("error", {"code": "agent_error", "message": str(exc)})
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
        "tools": getattr(app.state, "tool_names", []),
    }
