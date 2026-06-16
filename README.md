# HWAX Agent Server

The LLM/agent service behind HWAX Portal's MCP chat. The portal is a thin proxy +
auth gate (see `HWAXPortal/docs/MCP-CHAT-INTEGRATION-PLAN.md` §3); the **real model
call and tool fan-out live here**, as a separate service the portal reaches by URL
(`routes.env` / `AGENT_SERVER_URL`).

```
ChatDock (portal frontend)
  → portal /agent/chat   (auth · CSRF · concurrency cap · audit · SSE relay)
    → Agent Server /chat  (THIS) — LLM call, later LangGraph ReAct + MCP tools
      → vLLM /v1           (OpenAI-compatible; dev = Qwen2.5-7B on a 5070 Ti)
```

## API

- `POST /chat` — body `{message, system_id?, groups?}`. Streams the portal's §5 SSE
  contract: `status` → `token`×N → `result` → `done` (or `error`).
- `GET /health` — `{status, model, vllm}`.

## Run (dev)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
VLLM_BASE_URL=http://127.0.0.1:8000/v1 VLLM_MODEL=qwen2.5-7b-dev \
  .venv/bin/uvicorn app:app --port 9009
```

vLLM itself: see `HWAXPortal/docs/dev-vllm-setup.md` (apptainer `:latest` +
`PYTHONNOUSERSITE=1`, Qwen2.5-7B-AWQ).

## Env

| var | default | meaning |
|---|---|---|
| `VLLM_BASE_URL` | `http://127.0.0.1:8000/v1` | OpenAI-compatible inference base |
| `VLLM_MODEL` | `qwen2.5-7b-dev` | served model name |

## Status

- **Now**: relays a vLLM completion as SSE (straight model answer).
- **Next**: LangGraph ReAct loop + MCP tool fan-out (the portal already filters by
  `allowed_groups`; this server runs the tools the user is allowed to use).

## prod

Same code; point `VLLM_BASE_URL` at the B300 host running Qwen 72B. dev/prod differ
by that URL only.
