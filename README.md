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
  (the portal's update-all) should do the same. `delib_as_of: true` says this build takes the deliberation reference
  date (`delib_opts.as_of`, portal `docs/request-1027` HA-11); the portal refuses a dated web deliberation when the
  running instance does not say so (an older build drops the key silently and filters nothing).
- `GET /limits` · `POST /limits` — the deliberation job limits without a restart (portal `docs/delib-ops` R4).
  A box file `<DELIB_JOB_DIR>/state/limits.json` overlays `DELIB_JOB_MAX_RUNNING` · `DELIB_JOB_MAX_RUNNING_PER_USER` ·
  `DELIB_JOB_QUEUE_MAX` and is read at every start and queue decision (a person may edit it by hand too).
  `POST {max_running?, max_running_per_user?, queue_max?, max_seats_running?, by, reason?, reset?}` — integers (1–1000 ·
  0–1000 · 0–1000 · 0–1000, per-user `0` = follow the global, seats `0` = off); `null` removes that key's overlay, `reset: true` deletes the file; an unknown field
  is 422 `unknown_fields`, a file that cannot be written 503 `limits_unavailable`. Raising starts queued jobs at once;
  lowering never stops a running job — new starts queue. A per-user limit that env left following the global follows the
  overlaid global. A broken file is ignored (env values apply) and the answer's `error` says why. Loopback only, like
  `/drain`; the portal does not relay it. `/health.limits` carries the same values with `source` per key (`file` · `env`).
  A fourth key, `max_seats_running` (`DELIB_JOB_MAX_SEATS_RUNNING`, default `0` = off), caps the **seats** of running GLM
  jobs: the three above count jobs, so a 3-seat and a 20-seat deliberation weigh the same, while one deliberation sends
  as many LLM calls at a time as it has seats. A job holds its seated count (`personas` event — the current stage of a
  multi-stage job) or, before seating, an estimate (requested seats + 1, or `DELIB_PERSONAS` + `DELIB_COUNTER_SEATS` + 1).
  A job that would pass the cap queues; one alone runs even if larger; the first seat-blocked job holds the line so a
  large deliberation is not starved by small ones behind it; seating re-checks the queue at once. External-responder jobs
  and web deliberations (portal screen · risk panels, bounded by the risk app's `HWAXRISK_CONCURRENCY`) are not in the
  sum. `/health.delib_seats_running` and `deliberate_jobs` (`running_seats` · `running_seats_max`) show it.
- `POST /drain {scope: "delib", on, ttl_s | hold, by, reason?, force?}` — **pause new deliberations** (portal
  `docs/delib-ops` R1). Same rules as the card-review drain flag (6 h TTL cap, owner `by` — 409 `drain_owned` /
  `drain_held`, `hold` lives until a person lifts it, atomic writes, 503 `drain_unavailable`), but its own file
  `<DELIB_JOB_DIR>/state/delib-pause.json`, so the two never touch each other; `until_idle` is refused (422 — a pause is
  for holding the queue across a restart, not "until idle"). Turning it off needs `scope: "delib"` too (an off request
  without a scope is the card-review flag, as before) and starts held queued jobs at once. While on: MCP
  `deliberate_start` · `deliberate_continue` fail with "점검 중 — 약 N분 뒤 다시 (retry_after_s=…)" (`retry_after_s` = time
  left, or 600 for a `hold`); the web triggers `/심의` · `/시뮬심의` · `/시험계획` answer an SSE `error`
  `{code: "delib_paused", message, retry_after_s, infra, retryable}` then `done` — except panels the risk app starts
  (`delib_opts.started_by = "app"`: the app holds its own panels on the card-review flag, and a refused panel would close
  with an error); queued jobs are not started; running jobs run to the end. Works without the card-review module.
  `GET /drain` is unchanged (the card-review flag only — a contract shared with the risk app and the portal);
  `/health.delib_paused` · `delib_pause` show the pause. A pause that ends by its TTL is noticed by the queue tick
  (every 15 s).
- **The queue survives a restart** (R2). Every MCP job writes all its start arguments — question, evidence bodies,
  seats, human note, knobs, identity (email, groups as received, affiliation), **never a token** — to
  `<DELIB_JOB_DIR>/args/<id>.json` (0600). At startup queued jobs are put back in their original order (`queued_at`) and
  started by the queue tick (held while paused); a job without that file (older build, write failure, a caller token)
  is closed as before ("시작하지 못했다"). The file is removed when the job ends; an interrupted job keeps it for 7 days
  (or until it is resumed). `/health.delib_queue_durable` is true when every queued job has its file (so a restart loses
  none — `start.sh` and the portal's `lib/delib-busy.sh` then stop counting queued jobs as blocking), and
  `/health.delib_requeued` counts the jobs put back at this startup.
- **Resume an interrupted job** (R3) — `deliberate_continue(previous_job_id=<interrupted job>)`, no human note needed.
  The status carries `resume{rounds_done, total_rounds, rounds_left, args, resumed_by}`: rounds done are counted from
  the stage (entering round N means N−1 are done; `decide` · `explain` · `report` mean all). The new job carries each
  seat's last position of the finished rounds as its summary ("결정문은 아직 없다") — fitted **per seat** inside the length the
  engine takes for a summary (8,000 characters up to a 300K window, 29,077 on 1M), so a 20-seat panel loses no seat (long
  positions are cut to an equal share with their full length marked; a chained earlier decision summary gets at most a
  third) — runs only the remaining rounds (at
  least 2 — the engine's floor), keeps the seats and the round numbering, and reuses the original arguments from the
  args file (a given human note is appended to the original one). Same question and kind only; a job resumes once
  (`resumed_by` / `resumed_from`); multi-stage jobs and jobs cut in round 1 start over with the same arguments. A
  graceful shutdown now leaves running jobs `interrupted` (not `cancelled`), so they can be resumed.

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
| `LLM_CONTEXT_TOKENS` | _(unset — 128000)_ | The window, normally read from the server's `/v1/models` (`max_model_len`). Set by a person, it is also a **cap** on the server's answer (`min(server, this)`, `source` `env` — portal `docs/delib-ops` R5): a pool of servers with different windows sets the smallest one here. Unset, it is only the value used while the server cannot be read |
| `LLM_CONTEXT_TTL_S` | `600` | Re-ask the window this often, in the background (callers get the current value and never wait). A re-asked answer is taken **only when smaller** — widening needs a restart: a pool's busy small server can be missed by `least_conn`, and a window that moves changes the card-review `limits_rev`, which holds the risk app's runs. A larger answer shows in `/health.context.wider`. A failed re-ask keeps the value and tries again after 60 s. `0` = keep the first answer for the process's life (the old behavior) |
| `LLM_CONTEXT_SAMPLES` | `3` | `/v1/models` asks per lookup; the smallest answer wins (a pool answers from a different server each time). `/health.context` shows `tokens`, `source`, the last `server` answer and its `samples`, `env_cap`, `wider`, `reask_s` and `asked_s_ago` |
| `MCP_CONFIG` | _(unset)_ | path to a gitignored JSON file holding the gateway entry + token (takes precedence) |
| `MCP_SERVERS` | _(empty)_ | fallback `name=url` pairs when `MCP_CONFIG` is unset (no auth headers) |

### Time limits

A deliberation with 20+ seats on a shared LLM can run for hours. Every limit on that path is an
env knob; an inner limit must stay smaller than the one that wraps it, and nothing here cuts a
whole deliberation by wall clock. Values are read once at startup (restart to apply); the
`AGENT_*` ones are read by `start.sh`.

Chain (seconds, inner < outer): LLM connect 10 < one attempt 1800 < one logical call 3608 <
chair worst case 7216. On a box with the external responder on (`EXTERNAL_LLM_ENABLED=1`), one
external call is bounded by 2×`EXTERNAL_LLM_WAIT_S` + 2×`EXTERNAL_LLM_ANSWER_S` = 3000 and must stay
below one logical call (otherwise the path stays closed). Tool calls: gateway 600 (outer 660) <
`MCP_CALL_TIMEOUT_S` 900. The
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
| `LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S` | `300` | Chat **streaming** only: longest gap between chunks, including the wait for the first token. Exported by `start.sh` (the library default is 120). Deliberation calls do not stream and ignore it. On a streaming box it fires before `LLM_TIMEOUT_S`; the chat then says `LLM 이 N초 동안 토큰을 보내지 않았습니다(LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S)` — at the end of a partial answer, on the auto-retry status line, or as the final error when `CHAT_AUTO_RETRY=0`. |
| `THINK_SEAT_TIMEOUT_S` | `600` | One seat's answer in Thinking mode; past it only that seat drops out (`N초 초과(THINK_SEAT_TIMEOUT_S)`). Shorter than `LLM_TIMEOUT_S` on purpose — a per-seat cap. Was 180, which every seat exceeded while a large deliberation held the shared LLM. |
| `MCP_CALL_TIMEOUT_S` | `900` | How long this server waits for one tool call through the gateway (seat lookups, VOC, role restore, report save). Set as the MCP session request deadline; `0` = none. Must stay **larger** than the gateway's `GATEWAY_CALL_TIMEOUT` (600, outer deadline 660) — the gateway should expire first and name the slow backend; this is the last net for a hung gateway. |
| `KNOWLEDGE_TIMEOUT_S` | `180` | One seat's knowledge-card lookup (`agent_search`) before the round starts; on expiry it asks once more in `KNOWLEDGE_FALLBACK_MODE` and the seat's status line says so. Intentionally **shorter** than the gateway limit — it is the switch to the fallback, not a wrapping layer. Must stay above AIDataHub's `DB_POOL_TIMEOUT` (60) + `AIDH_SEARCH_STATEMENT_TIMEOUT_S` (90). |
| `DELIB_HEARTBEAT_S` | `15` | While a deliberation stream has nothing to send, emit `event: ping` / `data: {"idle_s", "ts"}` at this interval so proxies and idle read timeouts see a live stream. `0` turns it off — then every outer idle limit (portal `AGENT_STREAM_IDLE_TIMEOUT_S`, nginx `NGINX_AGENT_READ_TIMEOUT`, risk app `HWAXRISK_ENGINE_READ_TIMEOUT_S`) must exceed the silence of one seat turn, (1 + parse retries)×((1+`DELIB_LLM_MAX_RETRIES`)×`DELIB_TIMEOUT_S`+8) = 10824 s by default — not one logical call (the startup log prints the figure for the box). Consumers must ignore unknown event names. |
| `AGENT_RESTART_FORCE` | `0` | `start.sh` will not stop a running instance that reports running or queued deliberations — a restart cuts all of them. It prints the counts and exits `3` (skipped), leaving the instance up. When the instance reports `delib_queue_durable: true` (the queue survives a restart), queued jobs no longer block — only running ones do. `1` restarts anyway. If the counts cannot be read (no answer, older build) it says so and restarts. |
| `AGENT_STOP_GRACE_S` | `2` | Seconds `start.sh` waits between TERM and KILL when it replaces a running instance. Not a wait for deliberations to finish. A value that is not a plain number (trailing comment or space, a unit) is read as the default, with a warning. |
| `AGENT_HEALTH_PROBE_S` | `3` | How long `start.sh` waits for `/health` when it asks the running instance for those counts. A value that is not a plain number is read as the default, with a warning — it no longer turns the guard off. |
| `EXTERNAL_LLM_WAIT_S` | `300` | External responder only (`EXTERNAL_LLM_ENABLED=1`): how long a task waits while the owner's workflow shows no sign of life, then `external_worker_absent`. Full meaning under External responder. |
| `EXTERNAL_LLM_ANSWER_S` | `1200` | External responder only: how long a claimed task may take to its final submit; requeued once, the second miss is `external_answer_timeout`. 2×this + 2×`EXTERNAL_LLM_WAIT_S` must stay below one logical deliberation call (3608 by default) or the path stays closed. |

### MCP result pages

Claude Code takes an MCP tool result only up to `MAX_MCP_OUTPUT_TOKENS` (default 25,000 tokens); past it
the client saves the result to a file instead of showing it. A wide-window transcript batch, decision or
job list is far larger, so `deliberate_transcript`, `deliberate_result`, `deliberate_status` and
`deliberate_list` split a large response into pages (`page.part` / `page.next` / `page.pieces`,
`mcp_pages.py`) and never cut it. `deliberate_start` / `deliberate_continue` echo only the head of a long
question (`question_chars`, full text via `deliberate_status`). Pages are re-cut on every call, so a page
that does not join the previous one (`from` off) means the job changed in between — fetch again from part 1.

| var | default | meaning |
|---|---|---|
| `DELIB_MCP_PAGE_CHARS` | `25000` | Page size in characters (1 char ≈ 1 token against the client limit). Lower it together with a lower `MAX_MCP_OUTPUT_TOKENS` on the client. Floor 4000. |

`deliberate_list` and the restart sweep read a small per-job list index (`<DELIB_JOB_DIR>/list/<id>.json`,
written next to every ledger write) instead of the full ledger, which grows to MBs in a wide window. The
index carries the ledger's mtime and size; a stale or missing index is rebuilt from the ledger on the next
read, so the `list/` folder is derived data and may be deleted.

### Card review

`card_review.py` — one expert checks their own knowledge cards against one review unit (risk-review
redesign M2, `HWAXPortal/docs/risk-review-redesign/M2.md` §4 is the contract; M3 adds two kinds, the long-document
search, `mech` and drain — `M3.md` §4.3–4.7). The risk app calls it through
the portal relay `/agent/card-review/*`, which overwrites the four identity fields (`groups`, `user_email`,
`user_pat`, `entitlements`). Not a meeting: no tools, no seats, one LLM call per request (plus repairs).

| route | what |
|---|---|
| `GET /card-review/limits` | Budgets derived from the context window (`ctx_source` = `server` · `env` · `fallback`, `ctx_retrying`), `kinds` (five) and `cell_kinds` (four — `ctx_too_small` looks at these only; a box that cannot run `summary` says `runnable.summary=false` and keeps doing cell work), per kind budgets (`noinput.items_max`, `summary.{line_max, narrative_max, cover_max, cover_hard_max}`, `verdict.mech_max`, `offcard.longdoc_max`), `llm_max_tokens` + `max_tokens_source` (`card_review` · `delib` · `none`), `search.timeout_s`, versions (`prompt_rev` per kind, `norm_rev`, `limits_rev_by_kind{sweep, verdict, offcard, noinput, summary}` — each kind's rev hashes only the inputs that move that kind's budget; verdict·offcard·noinput share the unit cap so they share those inputs; `summary` also hashes `CARD_REVIEW_PARSE_RETRIES` (it sets `cover_hard_max`) — and `limits_rev`, the global rev an M2 app compares, now a hash of the four cell kinds' revs so summary-only knobs do not move it) and `gate{limit, limit_reason, active}`. A cell kind that cannot emit one row with the output cap (`k_out` 0, no sweep/offcard/noinput row) is `runnable=false` and `ctx_too_small{reason: output_cap, need_max_tokens}`; a window that is too small is `reason: window` with `need_ctx_tokens`. `ctx_too_small.reason_by_kind{<cell kind>: output_cap · window · output_too_large}` gives each kind its own reason (a `run` 422 carries its kind's), and `need_ctx_tokens` is measured at the raised output cap, so a box short on both gets both numbers in one answer. |
| `POST /card-review/pack` | `{agent_key}` → all of the expert's cards, normalised (`pack_hash`). User PAT only — a rejected PAT stops with `credential_*` (the remaining card fetches are cancelled at once), it never falls back to the service account. At most 2 packs are built at a time; a request that cannot get a slot within 30 s gets 429 `review_busy{retry_after_s, limit}` instead of queueing past the portal's unary timeout, and a build whose caller has gone away is cancelled. |
| `POST /card-review/plan` | `{cards[{alias, record_id, checkable, chars}], units[{unit_id, manifest_chars, body_chars}]}` → verdict decks, sweep decks, manifest pages, `oversize`, `soft_over`, `pair_over[{deck_id, unit_id, need, have}]`, planned against the current `limits_rev` (the answer also carries `limits_rev_by_kind`, read only — `expect` still takes `limits_rev` only). Every (verdict deck, unit) and (sweep deck, manifest page) pair it hands out fits the window; a pair that only fits card by card is listed in `pair_over` (run those cards for that unit as `solo`), and a unit that does not fit even with its largest card is in `oversize.units`. |
| `POST /card-review/run` | One call (`kind` = `sweep` · `verdict` (`fwd`·`rev`·`solo`) · `offcard` · `noinput` · `summary`). SSE: `status{step:"search"}` (offcard with `extra.search` only) · `status{step:"attempt", n}` · `ping{idle_s, ts}` · `result` · `error{code, message, infra, retryable, knob?}` · `done`. No `hello` frame. |
| `POST /drain` · `GET /drain` | The update-all drain flag (not relayed by the portal, loopback only): `{on, scope: all\|panels, ttl_s \| until_idle{grace_s, max_s}, hold?, by, reason, force?}`. Life only grows; a `hold` flag is changed only by a `hold` request (a `hold` with no `ttl_s`/`until_idle` lives until a human turns it off — it does not inherit an earlier TTL); turning off needs the same `by` (or `force`) — otherwise 409 `drain_held`. A non-`hold` request from a `by` that is not the live flag's owner is refused with 409 `drain_owned` (the body's `drain` names the owner, `scope` and `until`; nothing is merged — update-all reads any 409 as "someone else owns it, don't touch"); a `hold` request is accepted from anyone and takes ownership. TTL flags are capped at 6 h: one request's `ttl_s` ≤ 21,600 (above is 422 — the portal clamps), a renewal by the owner while the TTL is alive extends the life and restarts the 6 h window (a renewer is not a forgotten flag; one that stops renewing dies at its last renewal + `ttl_s`), a TTL request after the TTL part died (e.g. the flag lived on through `until_idle`) starts a fresh TTL, and a remaining life longer than the cap — a clock that went back — is not trusted; `hold` flags are exempt. `ttl_s`/`max_s` must be finite; `by` is cut at 120 characters and `reason` at 300. A flag file with any bad field (or one that fails to evaluate) reads as off and is deleted; an unreadable file reads as off and is logged once per 5 min; failing to record `until_idle.idle_since` on a read keeps the flag alive (logged once per 5 min) and the grace clock is
kept in memory, so the flag still ends after `grace_s`; a released flag whose file cannot be removed logs its release once per
5 min. A flag that cannot be written — or, on `on:false`, removed — is 503 `drain_unavailable`. `scope=all` makes the gate 0 (`review_busy{reason:"draining", retry_after_s: 60}` for run and pack), `panels` only tells the risk app not to pick new panels. The flag is a file (`DRAIN_STATE_PATH`, default `<ARTIFACT_DIR>/review-drain.json`) and survives a restart. |

Request models are `extra="forbid"` at every level, nested items included: an undeclared field is a 422
`unknown_fields` naming its path (`extra.known.0.reason`), not silently dropped (rejections echo caller values cut at 80 characters each — `fields` lists at most 20 with `fields_n` the full count). The `run` body (`?` = optional,
text fields accept `null` as empty):

| field | shape |
|---|---|
| `kind` · `variant` · `target_key` | `sweep` · `verdict` (`fwd`·`rev`·`solo`) · `offcard` · `noinput` · `summary`; `variant` is `null` except for verdict |
| `expert` | `{key, name?, domain?, role?}` — `key` is 1–120 chars without whitespace, `«»`, braces or brackets. `null` for (and only for) `summary` |
| `summary` | `{scope{kind: domain\|target, key, label?}, rows[{ref, text}], must_cover[ref], blocks[{name, title?, text?}]}` — `summary` only; rows get `R001…` aliases, `must_cover` above `cover_hard_max` is 422 `cover_too_large`, an assembled prompt above `p_hard` is 422 `over_budget` (never re-split) |
| `cards[]` | pack cards exactly as `/pack` returned them (`alias`, `sections[{sid, title, text}]`, …), sent back unchanged |
| `digest[]` | `{alias, type?, title?, record_id?, card_id?}` — background cards shown by title only (verdict · offcard) |
| `unit` | `{unit_id, kind?, title?, summary?, notes?, lines[], context[], evidence[], missing[], unit_hash?, summary_sha?, evidence_sha?, evidence_status?, input_gaps?}` for verdict · offcard, `null` for sweep |
| `unit.lines[]` · `context[]` · `evidence[]` | `{ref, text, owner_unit?}` |
| `unit.missing[]` | `{kind, note?}` (e.g. `{"kind": "ecad", "note": "회로 변경 정보가 없다"}`) |
| `manifest[]` | `{unit_id, kind?, title?, text?}` (sweep only) |
| `extra` | `{domains[{key, label?}], mechanisms[{code, label?}], known[{alias, judgement?, why?}], missing[{kind, note?}], search{queries[1..3]}}` — the risk taxonomy's `domain.code` goes in `key`; `missing` (expert-level gaps) is for `sweep` only, `search` (long-document excerpts `D01…`) for `offcard` only |
| `expect` | `{prompt_rev?, limits_rev?, limits_rev_kind?}` — only the given ones are compared: `limits_rev_kind` (M3 app) against `limits_rev_by_kind[kind]` of the requested kind, `limits_rev` (M2 app) against the global rev. `plan` takes `{prompt_rev?, limits_rev?}` only. |
| identity | `groups` · `user_email` · `user_pat` · `entitlements` (overwritten by the portal relay) |

Everything that comes from cards or the risk app is rendered either inside the prompt's `«…»` data frame
(titles, section titles and bodies, review status, standard refs, unit text, notes, missing notes, known whys) or,
for short labels that stand outside it (card type/confidence/causal status, section ids, line refs, missing kinds,
manifest kinds, known alias/judgement, mechanism and domain codes and labels), flattened to one line with `«»`,
`[]` and `{}` removed — a planted `[지시]` block can never stand outside the frame.

Rows per kind (JSON Lines): verdict `v` (+ `mech`/`mech_free` — required on WARNING·FAIL rows when a mechanism
list was sent, `checks.mech` = `ok`·`free`·`missing`·`no_list` — a flag only: it is never a repair reason and never
decides which of two rows for the same card survives), `noinput` `q1{impact, title, mech, mech_free, path, l,
k, cq, sev, why}` · `q2{item, needs, k, l, why}` · `more` (`quality.maybe_more` when `q1` reaches `items_max` or
`more=yes`; a `q1` with `impact=no` or a `q2` with an empty `item` needs a reason — `checks.why="short"` is re-asked;
`quality.ok`/`flagged` count `q1` and `q2` rows; `q1` rows fold only when identical; a repair replaces an earlier `q1`
with the same title and card only when that row had something to fix — a wrong quote, an `impact` the engine coerced,
or a short reason (same impact) — preferring the same place (`path` · `l`), then the same impact; and a repaired `q2` replaces the earlier empty-item `q2` whose reason was
short; above `items_max` the hits the model wrote (`yes` · `unknown`) are kept first, then engine-coerced ones, and `quality.dropped_over_cap` counts each dropped
row once), `summary` `head` · `risk` · `conflict` ·
`gain` · `watch` · `ask` · `para` with `r` (row aliases) and `refs` (the row refs) — `head`/`para`/`risk`/`conflict`
need text (`checks.text="empty"` counts as a missing line), a `risk` cites at most 3 rows (`checks.refs="too_many"`),
and a `must_cover` row counts as covered only by a `risk`/`conflict` with text and `checks.refs` `ok` or `unknown_alias`
(the valid aliases left count), one line covering at most 3 `must_cover` rows (the prompt and the repair hint say
so — `cr-summary-2`; a `risk` line over 3 covers none, and the hint says that); what is not
covered is re-asked, what is still missing after the repairs is in `result.missing` (`head`/`para` and R aliases —
resolve them with `meta.aliases.rows`). Several `head` lines keep the first with text (`checks.extra_heads`). When two
rows for the same card/unit/impact disagree, a hit (WARNING·FAIL, `rel` yes·maybe, `impact` yes·unknown) is never
displaced by a non-hit — unless the engine coerced that verdict from an out-of-vocabulary value (`rel` → `maybe`,
`impact` → `unknown`, `judgement` → `undetermined`), which an explicit answer replaces. Before coercing, a value is
matched case- and space-insensitively and through the alias table below (an aliased word is the model's explicit meaning,
not a coercion; the raw word stays in `checks.alias` as `"<field>:<word>"`). A coerced row (a `v` row whose `judgement` or
`applies` was coerced, a `u` row's `rel`, a `q1` row's `impact`) never disappears unflagged: replaced by an explicit
answer it leaves a `flipped_from`, and when it clashes with a row of another verdict it is a conflict like any other
(`checks.dup_conflict`). The word the model wrote is kept as `raw` in `flipped_from` or `contested_by` — each is a single
object and the first `raw` is never overwritten, so a later word is not always kept (the guarantee is the flag, not
every word). A repair row that ties an earlier row hands its own `dup_conflict`/`contested_by` to the row that stays.
The survivor carries
`checks.dup_conflict=true` and the losing row as `contested_by{verdict, cq, quote, why, attempt}`; once a contesting answer's quote is verified, the survivor's
own failed quote is not re-asked (its `checks.quote` stays) — for `q1` only when the contesting row is in the same place
(`contested_by` then also carries its `path` and `l`). Between rows of the same standing, repair reasons
(out-of-vocabulary values, failed quotes, short reasons, empty text) weigh before flag-only checks (unsourced numbers,
refs). A repair that replaces a row with a different verdict carries `flipped_from{verdict, attempt}` (plus `raw`, the
value the model wrote, when the replaced verdict was coerced; a repair that re-sends an existing explicit `q1`
replaces a coerced `q1` only at the same place — card, `path`, `l` — and never when either row already has
`flipped_from`; known limit: two coerced `q1` rows at the same place answered by one explicit line leave the second one
coerced — it stays `unknown`, a hit, until the retries run out), and a replacement keeps the `dup_conflict`, `contested_by` and `flipped_from` of the row it replaces. `quality.dup_conflict` and `quality.flipped` count those rows. `meta.basis` is `cards` · `background` · `role_only` · `search_only` ·
`noinput` · `rows`; offcard with search carries `meta.search{state: ok·empty_scope·no_hits·failed, queries, hits_n,
scope_n, used[{alias, record_id, section_id, title, chars, orig_chars, clipped}], note, req_cut}` (`failed` = could
not ask — the only one the app re-issues; an error envelope — no `hits` list, `ok:false`, `status:"error"`, `error` or
`errors` — is `failed`, while `refused:true` is not an error). Search runs with the user's PAT only. A clipped excerpt
carries its length mark on the line after the closing `»`, outside the quoted text, so a quote cannot borrow the mark. Every
result carries `meta.clipped{<input>: {orig_chars, kept_chars, …}}` for the inputs cut to their share (`role`,
`digest`, `missing`, `known` with `orig_items`/`kept_items`, `mechanisms`/`domains` with `labels_dropped`, summary
`blocks`; `{}` when nothing was cut). Summary number checks use the text the model actually saw (not the clip mark, nor a number the clip cut in half).

Enum aliases (`card_review.ENUM_ALIASES`, matched after removing case and all spaces; anything else is coerced):

| field | means | words |
|---|---|---|
| `rel` (sweep `u`) | `no` | none · irrelevant · not relevant · n/a · 없음 · 무관 · 해당 없음 |
| `rel` | `yes` | relevant · likely · 해당 |
| `impact` (noinput `q1`) | `no` | none · irrelevant · not relevant · n/a · 없음 · 무관 · 해당 없음 |
| `impact` | `yes` | likely · relevant · 해당 · 있음 |
| `judgement` (verdict `v`) | `OK` | pass · passed · 통과 · 문제없음 |
| `judgement` | `WARNING` | warn · 경고 |
| `judgement` | `FAIL` | failed · 실패 |
| `judgement` | `na` | n/a · 해당 없음 |

Other refusals before the stream opens: 422 `bad_request` · `unsupported_kind` ·
`unsupported_unit_kind` · `empty_unit` · `deck_too_large` · `over_budget{need, have, part}` · `ctx_too_small`,
409 `rev_mismatch{prompt_rev, limits_rev, limits_rev_kind, expect}` (the request's `expect` differs), 403 `denied` (no
`feat:deliberation`), and 503 `ctx_unknown{retry_after_s, infra: true, retryable: true}` from `run` and `plan`
while the window lookup is failing (`ctx_retrying`): budgets on an assumed window are neither computed nor compared
(that used to surface as a contract-class 409), so the caller retries after `retry_after_s` without counting
anything. A settled `fallback` (the server answered without a window, `ctx_retrying` false) is budgeted normally.
Engine error codes in the stream: `review_busy`, `llm_unreachable`, `llm_timeout`,
`llm_error`, `llm_misconfigured`, `llm_rejected`, `context_overflow`, `truncated_empty`, `no_rows` — and
`internal_error` for a defect in this module (outside the contract table, so the risk app counts it against the
cell instead of retrying forever).

There is **no queue**: when the gate is full the stream answers `error{review_busy, retry_after_s}` → `done`
at once and the caller retries later without counting it. Card-review calls are **not** counted in
`delib_active` / `delib_queued` and do not hold a restart back (a cut call is re-issued by the risk app);
`/health` reports them separately as `review_active`, `draining`, `drain{on, scope, until, until_idle, hold, by,
reason, set_at}`, `capabilities: ["card_review", "drain"]`,
`card_review{kinds, limit, limit_reason, delib_human, active, timeout_s, tries, attempts_max, search_timeout_s,
request_worst_s, prompt_rev, totals, last_error}` and `load.chat{active, window, ttft_ms{idle, review}, ratio_p50, slow,
slow_since}` (first chunk latency of chat turns while card review is idle vs running; `slow` when both rings have
10 turns and review p50 > 2 × idle p50 — logged once, never acted on: lower `CARD_REVIEW_CONCURRENCY`). The gate
halves only for deliberations a person started (`delib_opts.started_by != "app"`); the risk app's panels count in
`delib_active` (restart hold) but not in `delib_human`. Closing the stream cancels the running LLM call (and a
running search) and frees the slot. `scripts/llm_ttft_probe.py` samples the LLM's first-chunk latency every
minute with the same `.env` (`--dry-run` touches nothing).

It uses the deliberation LLM (`DELIB_*`) as is (with `CARD_REVIEW_MAX_TOKENS` bound per call when set). Worst case
of one request = (1 + `CARD_REVIEW_PARSE_RETRIES`) × ((1 + `DELIB_LLM_MAX_RETRIES`) × `DELIB_TIMEOUT_S` + 8) +
`CARD_REVIEW_SEARCH_TIMEOUT_S` = 10884 s by default (`request_worst_s`); the risk app's per-call wall clock must stay
above it. These knobs are deliberately not in the Time limits table yet (the portal's env kit test reads that table;
merging the chains is M3 `WP5a P3`).

| var | default | meaning |
|---|---|---|
| `CARD_REVIEW_K` | `8` | Most cards in one verdict call (also capped by the output budget, `verdict.k_out`). |
| `CARD_REVIEW_SHARE_VERDICT` · `_SWEEP` · `_OFFCARD` · `_NOINPUT` · `_SUMMARY` | `0.25` · `0.50` · `0.30` · `0.30` · `0.50` | Share of the window each kind aims for. Going over is flagged (`soft_over`), not refused; the hard refusal is the window itself (`p_hard`). |
| `CARD_REVIEW_MAX_TOKENS` | `0` | Output cap for card-review calls only (`llm.bind(max_tokens)`); `0` follows `DELIB_MAX_TOKENS`. Opens `k_out`, `cover_max`, `items_max`; part of `limits_rev`. |
| `CARD_REVIEW_CONCURRENCY` | `8` | Card-review calls running at once in this process. |
| `CARD_REVIEW_CONCURRENCY_WITH_DELIB` | half of the above (`4`) | The same while a deliberation a person started is running (clamped to the value above). |
| `CARD_REVIEW_SEARCH_TIMEOUT_S` | `60` | Deadline of one long-document search query (the up to three queries run at once). |
| `DRAIN_STATE_PATH` | `<ARTIFACT_DIR>/review-drain.json` | The drain flag file. |
| `CARD_REVIEW_RETRY_AFTER_S` | `30` | `retry_after_s` in a `review_busy` answer. |
| `CARD_REVIEW_PARSE_RETRIES` | `2` | Repair calls for missing rows, unreadable lines, out-of-vocabulary values and failed quotes. A failure during a repair ends with `result` and `quality.repair_error` (rows already received are kept). |

Contract examples the risk app and the portal test against live in `tests/fixtures/card_review/contract/`
(`index.json` maps each scene to its request, script, window, frames and final frame). They are produced by
the real routes with the scripted fake LLM (`tests/fakes/llm.py`), times masked, and rewritten only with
`CARD_REVIEW_CONTRACT_UPDATE=1 .venv/bin/python -m pytest tests/test_card_review_contract.py -q`.

For a wiring rehearsal with a 1,048,576-token window (the dev LLM has 16K), `tests/fakes/llm_server.py` is an
OpenAI-compatible fake (`/v1/models` reports `--ctx`, `/v1/chat/completions` replays the same scripts). Start it
and a throwaway agent server on free ports — never 9009, and never with the real `DELIB_JOB_DIR` (startup marks
that ledger's running jobs as interrupted). `tests/test_card_review_e2e.py` does exactly this and its header has
the command lines. It is test-only: no production knob points at it.

### External responder (Claude path — off by default)

`external_llm.py` — the risk review's judgement calls can be answered by an **external responder** instead of this
server's LLM: the job owner's Claude Code workflow (`HWAXPortal/docs/risk-claude-engine/`, decisions X-D1..X-D3). The
engine's prompts, parsers, repairs and contracts stay as they are — only *who writes the answer* changes. A call that
selects it is queued as a **task**; the owner's workflow claims it through three MCP tools, reads the prompt in pages,
and submits the answer text, which the engine then parses, checks and repairs exactly like a GLM answer. Off by
default; it is never exercised with a real Claude here (tests drive a fake worker through the real MCP tool functions —
`tests/fakes/worker.py`). **Consent flag:** every request that would send content to the responder must carry
`consent_external: true` — card review `run` with `llm: "external"`, deliberation `delib_opts` (MCP `advanced`) with
`llm: "external"`; otherwise it is refused before any task exists (`external_consent_required`). `limits` and `plan` send
no content and need no flag. The flag is all the engine checks — asking a person for consent is the caller's job (the
risk app, X-2).

| var | default | meaning |
|---|---|---|
| `EXTERNAL_LLM_ENABLED` | `0` | `1` (also `true`·`yes`·`on`) opens the path: the task queue, the three MCP tools and `llm: "external"` on card review and deliberation. Anything else — including an unreadable value, warned once — keeps it closed. |
| `EXTERNAL_LLM_CTX_TOKENS` | `200000` | The external model's window (integer ≥ 1024). Card review `limits`/`plan` budget with it, and an external deliberation derives every window-based budget from it (evidence, transcript, role and summary shares). Set it to the smallest window the owners' Claude may have. |
| `EXTERNAL_LLM_MAX_TOKENS` | `32000` | The external model's output cap (integer ≥ 1) — the `max_tokens` of every external call (card review binds it per call; `CARD_REVIEW_MAX_TOKENS` is a GLM-box knob and does not apply). An answer longer than the call's `max_tokens` (by the engine's own token estimate — `_est_tokens_all`) is cut there and comes back with `finish_reason=length`, as a capped model's would; truncation errors and warnings on this path (`truncated_empty`, `chair_output_cut`) name this knob. |
| `EXTERNAL_LLM_WAIT_S` | `300` | How long a task may wait **while the owner's workflow shows no sign of life** (no claim, read or submit, and no task of theirs claimed). Past it the call fails with `external_worker_absent`, and for the next `EXTERNAL_LLM_WAIT_S` every waiting or new task of that owner fails the same way at once — until the workflow calls any of the three tools again or that time has passed (then a new task queues and waits again). While the owner has a task claimed, waiting tasks do not age — a busy worker is not an absent one. |
| `EXTERNAL_LLM_ANSWER_S` | `1200` | How long a claimed task may take to its final submit. Past it the task goes back to the queue **once** (it is claimed again as `claim: 2` with a new `lease`; reads and submits under the first claim's lease get `not_claimed` and never touch the new claim's answer), and the second miss fails the call with `external_answer_timeout`. |
| `EXTERNAL_LLM_MAX_INFLIGHT` | `8` | External card-review calls one owner may have in flight (integer ≥ 1). External calls take no slot of the GLM review gate; this is their limit instead (see Card review). |

Values are read once at startup and the path **fails closed**: with the switch on, a value that cannot be read (an
integer knob given a fraction or a word), is not finite (`nan`, `inf`), is too large to compute with (an integer far
past the float range) or is below its floor, or a broken time chain (below — including one that cannot be computed, e.g.
such a `DELIB_LLM_MAX_RETRIES`), keeps the path closed — nothing falls back to a default and nothing raises (startup and
the deliberation MCP tools come up as on a disabled box). Startup prints one `⛔` line naming the setting; the
box then answers as a disabled one (no capability, no MCP tools, 409 `external_llm_disabled` whose message carries that
line and whose `knob` names the setting). The two time knobs are also rows of the Time limits table, and the portal's
env kit (`infra/env-kits/agent-server.env`) lists all six.

Time limits (inner first): the worst bounded case of one external call is wait + answer + (requeued) wait + answer
= 2 × `EXTERNAL_LLM_WAIT_S` + 2 × `EXTERNAL_LLM_ANSWER_S` = 3000 s by default (`/health.external_llm.call_worst_s`),
below one logical deliberation LLM call ((1 + `DELIB_LLM_MAX_RETRIES`) × `DELIB_TIMEOUT_S` + 8 = 3608 s) that the outer
limits are sized on — so card review's `request_worst_s` and the risk app's wall clocks stay valid. If the external worst
case is not below it the path stays closed (the deliberation limits are read the way startup builds the deliberation
LLM; `DELIB_TIMEOUT_S` ≤ 0 has no outer limit to compare). The startup line prints the chain. The exception is a busy
worker: while the owner's workflow has a task claimed, a waiting task's wait is bounded only by the workflow's
throughput (the queue is FIFO per owner, oldest first), and the outer wall clocks are the last net — size the
workflow's concurrency to the engine's (`EXTERNAL_LLM_MAX_INFLIGHT`, seats).

Rules: tasks are **owner-only** — the owner is the call's identity and the workflow's identity is the gateway's
`x-hwax-user` header (its PAT), so the workflow must run with the job owner's PAT. Raw identities — card review
`user_email` (the portal relay overwrites it with the verified principal) and the portal `/chat` deliberation's
caller — are compared trimmed and lower-cased only. The gateway header identity (the three task tools, and the owner of a
deliberation started through MCP `deliberate_start`/`deliberate_continue`) is percent-decoded once first, because the
gateway sends it as `quote(user, safe="@.")`; nothing else is decoded (`ev%41l@…` stays `ev%41l@…`, not `evAl@…`).
An MCP job's record keeps the raw header as its `user`, as before. Another owner's task id gets exactly the answer of a
missing one. Every claim carries a **lease**: `review_task_read`/`review_task_submit` must send the lease of the
claim they belong to. A caller that goes away (card review stream closed, deliberation job cancelled) removes its task;
the workflow then gets `task_not_found`. The queue lives in memory — a restart loses it (the running streams break too,
so the app re-issues as infra). Owner entries with no task that have been idle for a day are dropped. Logs carry task
ids, kinds and sizes only — never prompt or answer text. All lengths and positions (`chars`, `start`, `offset`,
`next_offset`, `received_chars`, `cut_chars`, `max_chars`) count **Unicode code points** — JavaScript's `String.length`
counts UTF-16 units and differs on characters outside the BMP (𝜎, 𝜀, some CJK, emoji).

The agent server's own chat and deliberation agents never bind the three task tools: they are dropped where gateway
tools are loaded (`app._get_tools_retry` — chat, deliberation and catalogue paths all pass there), also under the
gateway's `<app>_<tool>` alias, and a call through the always-bound `invoke_tool` is refused before it reaches the
gateway — so a prompt-injected GLM turn cannot claim or answer the user's pending task. Nor can they create external
work: a `deliberate_start`/`deliberate_continue` call of theirs (bound directly or named through `invoke_tool`, aliases
included) whose `advanced.llm` reads as `external` (the engine's own reading) or that carries any `consent_external` key
(top level or in `advanced`, whatever its value; JSON-string arguments are read the way FastMCP pre-parses them) is
refused with a tool error and never sent — the gateway relays such a call under the user's identity, so a forged flag
would start an external deliberation and record a consent nobody gave. The same refusal covers the risk app's job start
(X-D7 ④): a `risk_start_job` call of theirs (bound directly or through `invoke_tool`, aliases included) whose `llm`
reads as `claude` or `external` (trimmed, case-insensitive) or that carries any `consent_external` key, and the gateway
REST bridge: a `rest_call` whose `body` carries any `consent_external` key (any site or path), or whose `path` is one of
the risk app's job routes (`…/targets/<key>/jobs`, `…/jobs/<id>/<action>`) with a `body.llm` of `claude`/`external`.
Consent comes only from the person's own client. Calls without those fields are unchanged.

**Card review.** `GET /card-review/limits?llm=external` and `llm: "external"` in the `plan`/`run` bodies select it
(absent or `glm` = this server's LLM, unchanged; any other value is 422 `bad_request`). The external limits are
computed from `EXTERNAL_LLM_CTX_TOKENS`/`EXTERNAL_LLM_MAX_TOKENS` with `ctx_source`, `max_tokens_source` and `model`
= `external` and an `llm: "external"` field, and every `limits_rev`/`limits_rev_by_kind` hashes the path name too, so it
differs from GLM's even at equal numbers — switching a job's engine reruns its cells. `plan` answers carry
`llm: "external"`, `run` results `meta.llm: "external"` (`meta.model` `external`, `meta.usage` null — unknown, not 0).
External calls take **no slot of the GLM review gate** (a human-paced worker must not starve other users' GLM card
reviews); instead an owner may have `EXTERNAL_LLM_MAX_INFLIGHT` external calls in flight and the next gets
`review_busy{reason: "external_per_owner", limit, active, retry_after_s, knob: "EXTERNAL_LLM_MAX_INFLIGHT"}` before a
task exists. They are still refused while draining (scope `all` → `review_busy{reason: "draining"}`, as GLM calls), and
in-flight external calls are counted in `/health.review_active` (and `/drain`'s), which update-all's drain waits on —
a restart loses the in-memory queue. `/health.card_review` (`active`, `totals`) is the GLM gate only. For
`llm=external`, `limits.gate` describes this lane: `{limit: EXTERNAL_LLM_MAX_INFLIGHT (0 while draining), limit_reason:
"external_per_owner" | "draining", active: external card-review calls in flight, all owners}`. Refusals before the
stream, in this order after 403 `denied` and the shape checks: 409 `external_llm_disabled{knob}` (the server did not open
the path — the app holds the job, nothing is counted), then for `run` 422 `external_consent_required` (no
`consent_external: true` — a string or `1` is not `true`) and 422 `external_owner_missing` (no `user_email` — nobody's
workflow could claim the task). In the stream, a failed call is `error{code: external_worker_absent |
external_answer_timeout, infra: true, retryable: true, knob, attempt}`; during a repair it ends with `result` and
`quality.repair_error` like any other failure (`card_review.EXTERNAL_ERRORS` — separate from `ERRORS`, whose examples
the risk app copies byte for byte).

**Deliberation.** `delib_opts.llm: "external"` with `consent_external: true` (portal `/chat` deliberation, `/시뮬심의`,
`/시험계획`, and MCP `deliberate_start`'s `advanced`) runs every judgement call of that deliberation — seats, chair,
summaries, tool-argument planning — as tasks of the caller. **Seat free lookups run as a text round trip**
(`_TextReactAgent`, HWAXPortal `docs/claude-delib-parity`): tools cannot be bound to the responder
(`ExternalChatModel.bind_tools` raises), so the seat's lookup task carries the seat's own tool list (name, description, args
JSON schema — the same per-seat pick a GLM seat is bound with) and a rule block; the responder writes one
`CALL {"tool": "<name>", "args": {…}}` per line (a line wrapped in single backticks is accepted), **the engine** runs those
tools and sends their results back in the next task. At most `tool_budget` calls in total and `tool_budget + 1` tasks per
seat and round; a tool not on the list, a line that is not a JSON object, non-object `args` or a line past the budget is not
run and the next task says why; `CALL` lines in the last task are not run. The tools are the same objects a GLM seat gets
(read-only allow list, per-deliberation cache, result cap, phantom-id gate), and the calls flow into the same places —
status lines with the `call` pairing key, evidence cards, the shared lookup pool, the number-source corpus, the risk app's
`used_tool`. `free_tools=0` (or no lookup tool available — `warning{code: free_tools_unavailable}`) is **evidence_only**:
seats speak from `[근거]` and knowledge cards only. A notice card names the path (`delib` evidence `source: "외부 응답자 실행 —
좌석 도구 글 왕복"` or `"외부 응답자 실행 — evidence_only"`, `notice: true`). Engine-run steps (pinned tools, knowledge
cards, VOC recall, evidence prepass) are unchanged; their LLM parts go to the responder as text tasks. The whole deliberation budgets with `EXTERNAL_LLM_CTX_TOKENS` (a context
variable set at its entry — other deliberations, card review and chat keep this server's window);
`/health.external_llm.evid_budget` is the evidence share under that window, for the risk app's brief. Marks: the
decision text starts with `■ 외부 응답자 실행 — …` (stamped by code, like the sealed mark — it names the lookup path), the
`result` frame gains `llm: "external", tool_mode: "tools" | "evidence_only"` (the path that actually ran: `tools` when the
seats had a lookup agent — the risk ledger's word), and the MCP job's summary and transcript carry the same two fields (the
job records the requested path at start and takes the engine's on `result`; a job record without the field predates the
text round trip and reads `evidence_only`). The job's `applied_opts` show `llm`, `consent_external` and the caller's own
`free_tools` if sent (read as on the GLM path — nothing is forced); a continuation runs on this server's LLM unless
`advanced.llm` is sent again (with the flag). Limit failures go through
the existing paths with `cause`: `warning{code: seat_lost, cause}`, skip warnings (`summary_skipped`, …) and
`error{code: chair_failed, cause}`; a cut chair decision's `warning{code: chair_output_cut}` names
`EXTERNAL_LLM_MAX_TOKENS`; with an absent worker the first round waits once and the rest fail at once. Refusals:
`error{code: bad_request | external_llm_disabled | external_consent_required | external_owner_missing}` → `done` before
anything runs (in that order; MCP: `ValueError` `"<code>: …"`, no job is created). `bad_request` is an `llm` value other
than absent, `glm` or `external` (trimmed, case-insensitive) — refused whether the switch is on or off, as card review
does; it never runs on this server's LLM instead. `delib_opts.timeout_s` does not apply to the external responder.
**Target key** (X-D8 ⑦): an external deliberation may carry `delib_opts.target_key` (MCP `advanced.target_key`) — the risk
app's target key, `diff:<id>` or `snap:<id>` (`<id>` letters, digits, `_`, `-`; at most 64). It is put on every task of
that deliberation (`review_task_next`'s `target_key` and the task record) so the workflow can tell which job a task
belongs to; the risk app's issue-panel questions start with `[쟁점 …]`, so the question head
(`[리스크심사 <code> <target_key>]`) cannot supply it. When absent or empty the head is read as before; when both are
present the field wins. Another shape is refused before anything runs with `bad_request` (switch on or off). It is used
for nothing else (not the prior-report filter), and a GLM deliberation does not read it at all — sent or not, valid or
not, the GLM path is unchanged.
**Job id**: every task of an external MCP job (`deliberate_start`/`deliberate_continue`) also carries `job_id` — that job's
id (`review_task_next`'s `job_id` and the task record) — so a general deliberation, which has no risk target key, can be
answered by a workflow told to take that job (HWAXPortal `hwax-risk-claude.js` `jobId`). A portal `/chat` deliberation
(the risk app's panels) has none.
External MCP jobs (`deliberate_start`/`deliberate_continue`) take **no slot of `DELIB_JOB_MAX_RUNNING`** (nor of
`DELIB_JOB_MAX_RUNNING_PER_USER` — both count GLM jobs only): a human-paced worker must not hold the GLM deliberation
slots for hours. Instead one external deliberation runs per owner (the task owner); the owner's next ones wait in the
same queue (`DELIB_JOB_QUEUE_MAX`, `queue.why` says so) and start when it ends, even while the GLM slots are full. They
still count in `/health.delib_active` (a restart cuts them).

**MCP tools** (registered only when the path is open; a call without the gateway identity header is a tool error):

| tool | args | returns |
|---|---|---|
| `review_task_next` | `wait_s` (s, long-poll, capped at 25) | the caller's oldest waiting task, now claimed: `{task_id, lease, kind (sweep·verdict·offcard·noinput·summary·deliberation), max_tokens, max_chars, chars, parts[{role, start, chars}], answer_deadline (unix s), answer_s, claim (1·2), created_at, run_id?, target_key?, label?}` — or `{empty: true, claimed, waited_s}`. `max_chars` is `max_tokens` in the engine's own character estimate (1.05 per token, the Korean worst case): an answer of at most that many code points is never cut. |
| `review_task_read` | `task_id`, `lease`, `offset` | `{task_id, offset, next_offset, chars, text}` — one page; each result is ≤ 20,000 characters as FastMCP sends it (or `DELIB_MCP_PAGE_CHARS` if lower). Concatenate pages until `next_offset` is null and split by `parts`. |
| `review_task_submit` | `task_id`, `lease`, `text`, `offset?`, `final` | chunked: no `offset` appends, `0` restarts, otherwise it must equal the received length → `{ok: true, done: false, received_chars}`; `final: true` hands the answer to the engine → `{ok: true, done: true, chars, finish_reason (stop·length), cut_chars?}` |

Expected failures are results, not tool errors: `{ok: false, code}` with `task_not_found` (missing, finished,
cancelled or another owner's), `not_claimed` (not claimed under this `lease` — requeued after `EXTERNAL_LLM_ANSWER_S`,
claimed again, or a missing/wrong lease; the buffer is not touched), `offset_mismatch{received_chars}`, `bad_offset`,
`answer_too_large` (over 2,000,000 characters). The tool descriptions tell the workflow what the answer must look like
(exactly the format the prompt asks for — JSON Lines with no prose or fences for the five card-review kinds; for
deliberation the prompt's own JSON or prose-then-JSON, and the chair's Markdown decision with its fenced ```` ```json ````
`risk_spec` block for a risk review), the `max_tokens`/`max_chars` cap, the code-point units, and that only requests
carrying `consent_external: true` become tasks (the flag is all the server checks).

**Workflow (X-3).** Answer only tasks whose `run_id`/`target_key` belong to the job the workflow is serving — not every
task `review_task_next` hands out. The queue is FIFO per owner, so any other external call under the owner's identity
(another run of theirs, or a request that carried the flag without them) lands in the same line; this is defence in
depth on top of the consent flag and the agent refusal above. A claimed task that is never answered goes back to the
queue after `EXTERNAL_LLM_ANSWER_S` once and then fails its call with `external_answer_timeout`.

**/health.** Enabled boxes add `"external_llm"` to `capabilities` and a block `external_llm{ctx_tokens, max_tokens,
wait_s, answer_s, call_worst_s, max_inflight, inflight, queued, claimed, evid_budget}` — counts and settings only
(`inflight` is external card-review calls in flight, also added to `review_active`). Disabled boxes — including ones
closed by a configuration error — answer exactly as before.

Contract examples for this path live in `tests/fixtures/external_llm/contract/` (`index.json` maps the run scenes; the
other files are the limits, plan, refusals, `/health`, the MCP tool list, every MCP tool result shape — including a
stale-lease submit — and the deliberation frames). They are produced by the real routes and tool functions with the fake
worker, ids, leases and clocks fixed, and rewritten only with
`EXTERNAL_LLM_CONTRACT_UPDATE=1 .venv/bin/python -m pytest tests/test_external_llm_contract.py -q`. The regression tests
for the first adversarial review (stale lease, owner decoding, gate, consent, tool deny list, evidence-budget cache,
absent lapse, units and `max_chars`, cut knobs, continuation `free_tools`, fail-closed settings, owner pruning, and
GLM byte-identity with the switch on/off and `llm: "glm"`) are `tests/test_external_llm_review_fixes.py`; those for the
second (the agent's forged consent, values too large to compute with, the shell's exported switch — the suite's
`conftest.py` drops every `EXTERNAL_LLM_*` but `EXTERNAL_LLM_CONTRACT_UPDATE` — unknown `llm` values, and the external
job slots) are `tests/test_external_llm_review2_fixes.py`.

### Call trace (Phoenix — off by default)

`call_trace.py` — one span per LLM call and per MCP tool call, sent to a Phoenix collector (portal
`docs/improvement-1015/call-trace.md`, decision IM-D6 ③). **No text:** prompts, answers, stream chunks, tool arguments,
tool results and exception messages are never read. The recorder takes timings, the token usage the server reports,
the model name, the HTTP status, the LLM router's `x-hwax-llm-upstream` response header, and the ids the engine
attaches with `call_trace.tag` (only short ASCII tokens without spaces or `@` are accepted — a rejected value drops that
id rather than keeping the previous one). No setting turns text on.

| var | default | meaning |
|---|---|---|
| `CALL_TRACE_URL` | _(unset)_ | Phoenix base URL, e.g. `http://127.0.0.1:8750` (`/v1/traces` is appended). Unset or empty = off: no callback on any LLM, tools unwrapped, no thread, no network. A value that is not an `http(s)://host:port` URL keeps it off with one `[call-trace] ⚠` line. Read once at startup (restart to apply). |

A span carries — LLM: `hwax.llm.server` (host:port of `VLLM_BASE_URL` — or of `CHAT_LLM_BASE_URL` for the chat LLM — or `external` for the external responder),
`hwax.llm.upstream` (the server the LLM router actually used), `llm.model_name`, `llm.token_count.*` (when the server
reports usage — streaming chat calls usually do not, and the request is not changed to ask for it),
`hwax.llm.ttft_ms` / `hwax.llm.gen_ms` (streaming calls only: wait for the first chunk — queue plus prefill — and the
rest), `hwax.llm.http_status`, `hwax.llm.attempts` (SDK retries show as more than 1), `error.type`. Tool: `tool.name`,
start/end, `error.type` and `hwax.http_status` on failure (measured below `_cap_tool`, so gate refusals that never reach
the gateway are not spans). Both: `hwax.kind` (`chat`, `delib`, `sim-delib`, `test-plan`, `thinking`,
`card-review:<kind>`), `hwax.turn_id` or `hwax.job_id` (also `session.id`, so Phoenix groups them), `hwax.round`,
`hwax.seat`, `hwax.unit`, `hwax.target`. Calls after the last round (chair, summary) keep that round number and carry no
seat. A call that is cancelled before it ends (thinking seat timeout, job cancel, card-review stream closed, chat
disconnect — LangChain then calls neither end nor error callback) is closed as `error.type=CancelledError` when its task
ends or the next call starts in the same task; its end time is that moment.

Export is OTLP/HTTP protobuf — Phoenix 20.19's `/v1/traces` accepts only `application/x-protobuf`, so the few message
types needed are encoded by hand (no new runtime dependency). One daemon thread drains a 2,000-span queue with a 2 s
send timeout: a dead or slow collector drops spans and never makes a call wait. It logs one `⚠` line when sending
fails and one `✓` line when sending resumes; a full queue logs one `⚠` line and stays quiet until the queue drains
below half or sending resumes. The exporter is tested against a fixed golden byte string as well. `tests/test_call_trace.py` checks that the off state
attaches nothing, that the LLM request bytes are the same with the recorder on, that a canary planted in every text
position never reaches the exported bytes, and — when `requirements-dev.txt` is installed — decodes the bytes with the
official `opentelemetry-proto`.

### Chat LLM (separate server — off by default)

Chat can use its own OpenAI-compatible server while thinking, deliberation prep (expert-axis pick, VOC preview,
clarify, topic derivation), deliberation and card review stay on `VLLM_*` (portal `docs/chat-llm-stcx` — on cae00 the
stcx GPU LLM `127.0.0.1:18700/v1`, Qwen3.8-Flash-Next). Changing `VLLM_BASE_URL` instead would move deliberation too —
both clients read it. Only the chat ReAct agent and the chat-turn tool planners use the chat LLM.

| var | default | meaning |
|---|---|---|
| `CHAT_LLM_BASE_URL` · `CHAT_LLM_MODEL` | _(unset)_ | both set = on. Unset = chat uses the `VLLM_*` client (nothing changes). A URL without a model stays off and says so in `/health` |
| `CHAT_LLM_API_KEY` | `EMPTY` | bearer for the chat server |
| `CHAT_LLM_ENABLE_THINKING` | `1` | thinking on (user decision 2026-10-18 — it was `0` on 10-17). `0` sends `chat_template_kwargs.enable_thinking=false` and turns the effort and output reserve off too |
| `CHAT_LLM_REASONING_EFFORT` | `xhigh` | sent as `reasoning_effort` while thinking is on; empty = not sent. `LLM_REASONING_EFFORT` is a `VLLM_*` setting and is not sent to the chat server |
| `CHAT_LLM_MAX_TOKENS` | `0` while thinking (not sent) | the stcx server caps output with `--override-generation-config` (131,072). Thinking off → `LLM_MAX_TOKENS` |
| `CHAT_LLM_OUTPUT_RESERVE` | `65536` while thinking | tokens kept free for the answer: the chat turn budgets with min(chat window, GLM window) minus this (at most half the window) — a prompt that fills the window leaves no room to think |
| `CHAT_LLM_VISION` | `auto` | does the chat server take images — `auto` probes once (a red PNG, `VISION_PROBE_TIMEOUT_S` 120 — a freshly started or busy vLLM took longer than 15 s) when an image is about to be sent; `1` / `0` skip the probe. `LLM_VISION` is the same for `VLLM_*`. Only a 400/422 that talks about images or multimodal input counts as "no"; anything else (timeout, other 400, 5xx) is "unknown" and is asked again 60 s later |
| `CHAT_LLM_DISABLE_STREAMING` | _(follows `LLM_DISABLE_STREAMING`)_ | a box that turned GLM streaming off (tool_calls lost) starts the chat server non-streaming too; set `0` once the chat server's streaming tool calls are verified |
| `CHAT_LLM_MAX_RETRIES` | `1` | SDK retries on the chat server before falling back |
| `CHAT_LLM_FALLBACK` | `1` | `0` = no fallback (the failure shows in chat) |
| `CHAT_LLM_CONTEXT_TOKENS` | `LLM_CONTEXT_TOKENS` or 128000 | window to assume while the chat server's `/v1/models` cannot be read |

**Fallback is per LLM call** (`with_fallbacks`): a connection error, timeout, 5xx, 401/403, a stream silent before its
first chunk, or a 400 that says the prompt does not fit the chat server's window (`ChatCtxOverflow`) sends that call to a
dedicated `VLLM_*` client — never the whole turn, which would run tools a second time. Other 400s are not retried
elsewhere (the same request fails there too) and 404 (wrong model name) is a configuration error that should show. A
stream that already sent tokens cannot move. If the call carries images and `VLLM_*` does not take images, the images
are replaced by one line saying how many were dropped (`_VisionGuardLLM`). The fallback client keeps its own streaming setting; the chat turn emits text per model call
(a call that ended without stream chunks has its text taken from the end event), so a streaming chat server with a
non-streaming GLM fallback loses nothing. **Context:** a chat turn budgets with the smaller of the two windows
(`_external_llm.ctx_override` for that task, reset when the turn ends), so a 262K chat server never receives a request
sized for GLM's 1M — and the fallback still fits. **`/health` `chat_llm`:** server (host:port), model, thinking,
effort, max_tokens, output_reserve, streaming, fallback `{enabled, to, count, last_at}`, the chat server's last error,
`ctx_tokens` / `ctx_source` / `model_served` once read (no key). Top level: `tool_cut` (the last chat turn's schema cut —
count and apps; null = nothing cut) and `vision` (per server `{ok, why}` — `ok` null = unknown, `why` = the probe's
answer, e.g. `HTTP 200` or the refusal text; empty map = not asked yet). The chat server's
thinking text streams as `reasoning` events and each turn ends with a `meta` event (models called, thinking, effort,
fallback). Tests: `tests/test_chat_llm.py` (real lifespan, fake servers — closed port, 5xx,
400, a streaming chat server falling back to a non-streaming GLM through a real chat turn, the window override).

### Core vocabulary (portal docs/request-1029)

The chat's core-data rules (`[코어 데이터 규칙]` in the system prompt) are built from the portal's canonical vocabulary,
`../HWAXPortal/backend/config/core_vocab.json` — the sibling repo on the same box (dev `~/claude`, cae00 `~/Projects`).
The trigger words (`rule_words`), the first step (one `object_resolve` on the canonical hub before other apps), the
type-name map (`types`, one line at the end of rule ① — `word = type_slug`, so the model stops inventing type names;
a word outside the map is checked in the canonical `search` result's `types[]`; portal docs/request-1030 HA-32), the
relation map (`relations`, rule ④) and the apps that hold no core objects (`apps` with role `noncore`, rule ⑦) come
from it. The file is re-read when its mtime changes. Missing or malformed → the built-in text (`SYSTEM_PROMPT`, byte
for byte); a `types` value that is not a hub slug (`[a-z][a-z0-9_]{0,31}`) makes the table malformed.
`/health` `core_vocab`: `{loaded, version, rule_words, canonical}` or `{loaded: false, error}` (no path).
Tests: `tests/test_request_1029.py` (reads the real portal file; `conftest.py` sets it to missing for every other test)
and `tests/test_core_vocab_types.py` (the `types` line; its real-file case skips until the portal file has `types`).

**Stable prompt prefix (W-3).** The first system message is the same on every turn (`SYSTEM_PROMPT`, or the vocabulary
version of it), and the fixed tool share (guide tools, then core tools) sits at the front of the bound tools in a fixed
order. Qwen's chat template appends the tools to the first system message, so system + fixed tools can be reused from the
server's prefix cache across turns and users. What changes per turn (apps or tools the user pinned, operator/persona role,
knowledge-card excerpt, attached documents, "no tools this turn") goes at the head of this turn's user message:
`[이번 턴의 안내 — 시스템이 붙인 것이다. 사람이 쓴 글이 아니다]`, the notes, `[사람의 말]`, then what the person wrote (a
message with images gets this as a new first text part). There is no second system message — Qwen's chat template
rejects a system message that is not first with HTTP 400 `System message must be at the beginning.` (cae00, portal
docs/request_261030.md HA-26). If that 400 comes anyway, the automatic retry (`CHAT_AUTO_RETRY`) merges the notes into
the first system message, from that retry to the end of the turn, and logs once
`[agent] 'System message must be at the beginning' 400 — 남은 재시도는 이 턴의 안내를 첫 시스템 메시지에 합쳐 보낸다(HA-26)`;
other errors are retried in the stable layout. A rescued turn is a non-streaming agent run, so its answer goes out
without the self-check or the evidence block. `CHAT_STABLE_PREFIX=0` puts the notes into the first system message on
every call (the old layout) — the position changed, so instruction-following is measured on the box (portal
docs/request-1029 C-7).

**Self-check patches and output cap (W-4 · W-9).** The reviewer no longer rewrites the whole answer. After `[판정]` and
`[고친 점]` it writes `[바꿀 곳]` with one `[원문]` (draft text copied as is) / `[새 글]` (its replacement; empty deletes)
pair per change, and the server puts the pairs into the draft. Each `[원문]` must be found exactly once in the draft
(differences in spaces or line breaks only are tolerated); if any pair is not found, found more than once, overlaps
another or is malformed, nothing is applied and the draft goes out (`self_check.status` `suspect` with the reason). The
reviewer call sends `max_tokens` 4,096 (langchain-openai sends it as `max_completion_tokens`; with
`CHAT_SELF_CHECK_THINK=1` the thinking counts against it too); the input budget reserves the same amount. A cut reply
keeps the draft. The main chat answer has no cap (portal decision D-6).

**Tool description budget (W-2).** With no tool cap (`TOOL_MAX=0`, cae00), the bound tool schemas are limited to 45% of
the chat window (about 88,000 tokens on a 262,144 window). `CHAT_TOOL_SCHEMA_BUDGET` (estimated tokens) sets it to a fixed
value instead; empty or 0 keeps the 45%. The default is unchanged on purpose (portal decision D-3) — compare 30,000 and
40,000 on the box with the same 16 questions first. A smaller bundle means more `search_tools` → `invoke_tool` round trips.

### Images (portal docs/chat-depth-context T5)

Images reach the models as `image_url` parts (`vision.py`). **Chat:** images uploaded with a message
(`ChatRequest.images` — `[{name, mime, data}]`, checked by content, at most `VISION_MAX_IMAGES` 12 of `VISION_MAX_BYTES`
8 MiB) are stored as artifacts and returned in a `user_images` event; the portal puts those URLs on the history item
(`images`). The last `VISION_HISTORY_IMAGES` (6) images of the conversation — uploads, tool images named in a turn's
evidence or answer — are attached again, and a tool's image is attached right after that tool call's results. A chat
model that does not take images gets transcripts from a model that does (or a line saying the images could not be
read). **Thinking:** seats get the images if the seat model takes them, otherwise transcripts as `[g:N]` in the session
context. **Deliberation** (`/심의`, `/시뮬심의`, `/시험계획`): transcripts are always added to `delib_opts.evidence`
(citable as `[e:N]`); if `VLLM_*` takes images they are also attached to every seat and chair call (`DELIB_IMAGES`).
Transcripts are cached by image hash in `ARTIFACT_DIR/.transcripts`, four at a time, at most `VISION_TRANSCRIBE_MAX` (8).
Nothing is probed at startup. Tests: `tests/test_chat_vision.py`, `tests/test_vision_think_delib.py`; checked once against a real
vision model on vLLM 0.23 (Qwen2.5-VL-3B — upload, an earlier turn's image, a tool's image, deliberation transcripts; portal
`docs/chat-depth-context` CD-D13).

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
