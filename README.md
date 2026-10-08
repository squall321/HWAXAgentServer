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
- `GET /health` — `{status, delib_active, delib_queued, model, vllm, mcp, tool_scoping, …}`.
  `delib_active` / `delib_queued` count running and queued deliberations (web and MCP jobs);
  `start.sh` reads them before it replaces a running instance, and any unattended restart
  (the portal's update-all) should do the same.

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
whole deliberation by wall clock. Values are read once at startup (restart to apply); the
`AGENT_*` ones are read by `start.sh`.

Chain (seconds, inner < outer): LLM connect 10 < one attempt 1800 < one logical call 3608 <
chair worst case 7216. Tool calls: gateway 600 (outer 660) < `MCP_CALL_TIMEOUT_S` 900. The
knowledge lookup (180) is a fallback switch, not a layer. Limits that wrap this server — the
risk app's panel wall clock and the portal / nginx / risk-app idle limits — live in those repos
and must stay above one logical call at the request maximum (2×`DELIB_TIMEOUT_MAX_S`+8 = 28808).

The longest stretch with **no stream event** is longer than one logical call. A seat turn chains
1 + parse retries of them and emits nothing in between (`DELIB_PARSE_RETRIES`, default 1; the quote
contract `DELIB_REBUT_QUOTE`, on by default, raises it to at least 2) — 3×3608 = 10824 by default —
and a free-lookup seat chains more (its LLM steps plus tool calls). `deliberate_status` reports the
seat-turn figure as `quiet_ok_s`: inside it the job is waiting; past it the job is **not** proven
hung — with the call limits on, every inner wait is finite and a running job ends by itself.
Nothing is cut on this value. It matters to the outer idle limits only when the heartbeat is off
(`DELIB_HEARTBEAT_S` below).

| var | default | meaning |
|---|---|---|
| `LLM_CONNECT_TIMEOUT_S` | `10` | Connect timeout to the LLM endpoint, for chat and deliberation alike. Kept short on purpose: it detects a dead server, it does not measure a slow one. |
| `DELIB_TIMEOUT_S` | `1800` | Read timeout of **one attempt** of one deliberation LLM call (seat turn, free-lookup step, chair, summary). Not a limit on the whole deliberation — there is none. `0` = unlimited (logged at startup). Does not inherit `LLM_TIMEOUT_S`. A single request may ask for more with `delib_opts.timeout_s`, up to `DELIB_TIMEOUT_MAX_S`. |
| `DELIB_TIMEOUT_MAX_S` | `14400` | Largest per-call timeout a request may ask for (`delib_opts.timeout_s`, MCP `advanced.timeout_s`); the floor is 10. A value outside the range runs at the nearest end and the stream says so (card "요청 값 상한 초과", job ledger `evidence_omitted`). The **default** is the contract number: portal `DelibOpts.timeout_s` (`le`) and the frontend clamp must equal it (portal test `test_delib_timeout_cap_contract`). Values below 10 are read as the default. |
| `DELIB_LLM_MAX_RETRIES` | `1` | openai SDK retries for deliberation calls (a timeout retry restarts generation from scratch). Worst case of one logical call = (1+retries)×`DELIB_TIMEOUT_S` + backoff = 3608 s by default; outer limits (risk app panel wall clock, portal/nginx idle limits) are sized on this. |
| `DELIB_CHAIR_RETRIES` | `1` | How many more times the chair's decision synthesis is called after it fails (timeout or error); `0` = never again. No separate timeout — it uses `DELIB_TIMEOUT_S`, so the worst case is (1+this)×one logical LLM call (7216 s by default). If it still fails the rounds are kept: the seats' last positions go out in the decision slot, the report (minutes) is saved, the stream ends with `error` code `chair_failed`, and the job can be continued. |
| `LLM_TIMEOUT_S` | `900` | Read timeout of one attempt of a chat / Thinking / pre-deliberation helper call. `0` = unlimited (logged at startup). Used to be unset = unlimited. |
| `LLM_MAX_RETRIES` | `2` | openai SDK retries for chat calls (the library default, now named). |
| `LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S` | `300` | Chat **streaming** only: longest gap between chunks, including the wait for the first token. Exported by `start.sh` (the library default is 120). Deliberation calls do not stream and ignore it. |
| `THINK_SEAT_TIMEOUT_S` | `600` | One seat's answer in Thinking mode; past it only that seat drops out (`N초 초과(THINK_SEAT_TIMEOUT_S)`). Shorter than `LLM_TIMEOUT_S` on purpose — a per-seat cap. Was 180, which every seat exceeded while a large deliberation held the shared LLM. |
| `MCP_CALL_TIMEOUT_S` | `900` | How long this server waits for one tool call through the gateway (seat lookups, VOC, role restore, report save). Set as the MCP session request deadline; `0` = none. Must stay **larger** than the gateway's `GATEWAY_CALL_TIMEOUT` (600, outer deadline 660) — the gateway should expire first and name the slow backend; this is the last net for a hung gateway. |
| `KNOWLEDGE_TIMEOUT_S` | `180` | One seat's knowledge-card lookup (`agent_search`) before the round starts; on expiry it asks once more in `KNOWLEDGE_FALLBACK_MODE` and the seat's status line says so. Intentionally **shorter** than the gateway limit — it is the switch to the fallback, not a wrapping layer. Must stay above AIDataHub's `DB_POOL_TIMEOUT` (60) + `AIDH_SEARCH_STATEMENT_TIMEOUT_S` (90). |
| `DELIB_HEARTBEAT_S` | `15` | While a deliberation stream has nothing to send, emit `event: ping` / `data: {"idle_s", "ts"}` at this interval so proxies and idle read timeouts see a live stream. `0` turns it off — then every outer idle limit (portal `AGENT_STREAM_IDLE_TIMEOUT_S`, nginx `NGINX_AGENT_READ_TIMEOUT`, risk app `HWAXRISK_ENGINE_READ_TIMEOUT_S`) must exceed the silence of one seat turn, (1 + parse retries)×((1+`DELIB_LLM_MAX_RETRIES`)×`DELIB_TIMEOUT_S`+8) = 10824 s by default — not one logical call (the startup log prints the figure for the box). Consumers must ignore unknown event names. |
| `AGENT_RESTART_FORCE` | `0` | `start.sh` will not stop a running instance that reports running or queued deliberations — a restart cuts all of them. It prints the counts and exits `3` (skipped), leaving the instance up. `1` restarts anyway. If the counts cannot be read (no answer, older build) it says so and restarts. |
| `AGENT_STOP_GRACE_S` | `2` | Seconds `start.sh` waits between TERM and KILL when it replaces a running instance. Not a wait for deliberations to finish. |
| `AGENT_HEALTH_PROBE_S` | `3` | How long `start.sh` waits for `/health` when it asks the running instance for those counts. |

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
