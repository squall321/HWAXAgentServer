# HWAX Agent Server

The LLM/agent service behind HWAX Portal's MCP chat. The portal is a thin proxy +
auth gate (see `HWAXPortal/docs/MCP-CHAT-INTEGRATION-PLAN.md` §3); the **real model
call and tool fan-out live here**, as a separate service the portal reaches by URL
(`routes.env` / `AGENT_SERVER_URL`).

```
ChatDock (portal frontend)
  → portal /agent/chat   (auth · CSRF · concurrency cap · audit · SSE relay)
    → Agent Server /chat  (THIS) — LangGraph ReAct loop, forwards caller groups
      → vLLM /v1           (OpenAI-compatible; dev = Qwen2.5-7B on a 5070 Ti)
      → MCP Gateway /mcp    (HWAXMcpGateway; group-filters the tool set per request)
```

## API

- `POST /chat` — body `{message, system_id?, groups?}`. `groups` (the caller's JWT
  groups, handed off by the portal) are forwarded to the MCP Gateway, which returns only
  the tools those groups may use. Streams the portal's §5 SSE contract:
  `status` → `token`×N → `result` → `done` (or `error`).
- `GET /health` — `{status, model, vllm, mcp, tool_scoping}`.

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
| `MCP_CONFIG` | _(unset)_ | path to a gitignored JSON file holding the gateway entry + token (takes precedence) |
| `MCP_SERVERS` | _(empty)_ | fallback `name=url` pairs when `MCP_CONFIG` is unset (no auth headers) |

### Time limits

A deliberation with 20+ seats on a shared LLM can run for hours. Every limit on that path is an
env knob; an inner limit must stay smaller than the one that wraps it, and nothing here cuts a
whole deliberation by wall clock. Values are read once at startup (restart to apply).

| var | default | meaning |
|---|---|---|
| `LLM_CONNECT_TIMEOUT_S` | `10` | Connect timeout to the LLM endpoint, for chat and deliberation alike. Kept short on purpose: it detects a dead server, it does not measure a slow one. |
| `DELIB_TIMEOUT_S` | `1800` | Read timeout of **one attempt** of one deliberation LLM call (seat turn, free-lookup step, chair, summary). Not a limit on the whole deliberation — there is none. `0` = unlimited (logged at startup). Does not inherit `LLM_TIMEOUT_S`. A single request may ask for more with `delib_opts.timeout_s`, up to `DELIB_TIMEOUT_MAX_S`. |
| `DELIB_TIMEOUT_MAX_S` | `14400` | Largest per-call timeout a request may ask for (`delib_opts.timeout_s`, MCP `advanced.timeout_s`); the floor is 10. A value outside the range runs at the nearest end and the stream says so (card "요청 값 상한 초과", job ledger `evidence_omitted`). The **default** is the contract number: portal `DelibOpts.timeout_s` (`le`) and the frontend clamp must equal it (portal test `test_delib_timeout_cap_contract`). Values below 10 are read as the default. |
| `DELIB_LLM_MAX_RETRIES` | `1` | openai SDK retries for deliberation calls (a timeout retry restarts generation from scratch). Worst case of one logical call = (1+retries)×`DELIB_TIMEOUT_S` + backoff = 3608 s by default; outer limits (risk app panel wall clock, portal/nginx idle limits) are sized on this. |
| `MCP_CALL_TIMEOUT_S` | `900` | How long this server waits for one tool call through the gateway (seat lookups, VOC, role restore, report save). Set as the MCP session request deadline; `0` = none. Must stay **larger** than the gateway's `GATEWAY_CALL_TIMEOUT` (600, outer deadline 660) — the gateway should expire first and name the slow backend; this is the last net for a hung gateway. |
| `KNOWLEDGE_TIMEOUT_S` | `180` | One seat's knowledge-card lookup (`agent_search`) before the round starts; on expiry it asks once more in `KNOWLEDGE_FALLBACK_MODE` and the seat's status line says so. Intentionally **shorter** than the gateway limit — it is the switch to the fallback, not a wrapping layer. Must stay above AIDataHub's `DB_POOL_TIMEOUT` (60) + `AIDH_SEARCH_STATEMENT_TIMEOUT_S` (90). |
| `LLM_TIMEOUT_S` | `900` | Read timeout of one attempt of a chat / Thinking / pre-deliberation helper call. `0` = unlimited (logged at startup). Used to be unset = unlimited. |
| `LLM_MAX_RETRIES` | `2` | openai SDK retries for chat calls (the library default, now named). |
| `DELIB_HEARTBEAT_S` | `15` | While a deliberation stream has nothing to send, emit `event: ping` / `data: {"idle_s", "ts"}` at this interval so proxies and idle read timeouts see a live stream. `0` turns it off — then every outer idle limit (portal `AGENT_STREAM_IDLE_TIMEOUT_S`, nginx `NGINX_AGENT_READ_TIMEOUT`, risk app `HWAXRISK_ENGINE_READ_TIMEOUT_S`) must exceed 2×`DELIB_TIMEOUT_S`. Consumers must ignore unknown event names. |

## Status

- **Now**: LangGraph ReAct agent over vLLM, tools from the MCP Gateway. Per request the
  caller's `groups` are forwarded to the gateway via the `X-HWAX-Groups` header; the
  gateway returns only the tools those groups may use (it owns `allowed_groups` filtering —
  it knows each tool's backend, which is flattened away by the time tools reach here). The
  compiled agent is cached per group-set. MCP/gateway down → degrades to a no-tool answer.
- **Next**: `system_id`-based per-page tool scoping (portal Phase 2; accepted, not yet used).

## prod

Same code; point `VLLM_BASE_URL` at the B300 host running Qwen 72B. dev/prod differ
by that URL only.
