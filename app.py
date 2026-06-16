"""HWAX Agent Server — dev minimal.

The portal is a thin proxy; the *real* LLM call lives here (계획서 §3). This dev
version relays an OpenAI-compatible vLLM stream as the portal's §5 SSE contract:
status → token×N → result → done (or error). LangGraph ReAct + MCP tool fan-out
land here later, once MCP servers exist; for now it answers straight from the model.

Env:
  VLLM_BASE_URL   OpenAI-compatible base (default http://127.0.0.1:8000/v1)
  VLLM_MODEL      served model name (default qwen2.5-7b-dev)
  AGENT_PORT      listen port (default 9000)
"""

import json
import os
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:8000/v1")
VLLM_MODEL = os.environ.get("VLLM_MODEL", "qwen2.5-7b-dev")
SYSTEM_PROMPT = (
    "당신은 HWAX 포털의 어시스턴트입니다. 한국어로 간결하고 정확하게 답하세요."
)

app = FastAPI(title="HWAX Agent Server", version="0.1.0")

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class ChatRequest(BaseModel):
    message: str
    system_id: str | None = None  # current portal sub-page → tool scope (later)
    groups: list[str] = []        # caller's groups (allowed_groups filtering, later)


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def _agent_stream(req: ChatRequest) -> AsyncIterator[bytes]:
    yield _sse("status", {"step": "모델 호출 중", "tool": None})
    payload = {
        "model": VLLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": req.message},
        ],
        "stream": True,
        "temperature": 0.4,
        "max_tokens": 1024,
    }
    full = []
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST", f"{VLLM_BASE_URL}/chat/completions", json=payload
            ) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode(errors="replace")[:200]
                    yield _sse("error", {"code": f"vllm_{r.status_code}", "message": body})
                    yield _sse("done", {})
                    return
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        obj = json.loads(chunk)
                        delta = obj["choices"][0]["delta"].get("content")
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if delta:
                        full.append(delta)
                        yield _sse("token", {"delta": delta})
    except httpx.HTTPError as exc:
        yield _sse("error", {"code": "vllm_unreachable", "message": str(exc)})
        yield _sse("done", {})
        return

    yield _sse("result", {"type": "text", "content": "".join(full)})
    yield _sse("done", {})


@app.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        _agent_stream(req), media_type="text/event-stream", headers=SSE_HEADERS
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "model": VLLM_MODEL, "vllm": VLLM_BASE_URL}
