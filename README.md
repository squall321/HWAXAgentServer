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
| `LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S` | `300` | Chat **streaming** only: longest gap between chunks, including the wait for the first token. Exported by `start.sh` (the library default is 120). Deliberation calls do not stream and ignore it. On a streaming box it fires before `LLM_TIMEOUT_S`; the chat then says `LLM 이 N초 동안 토큰을 보내지 않았습니다(LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S)` — at the end of a partial answer, on the auto-retry status line, or as the final error when `CHAT_AUTO_RETRY=0`. |
| `THINK_SEAT_TIMEOUT_S` | `600` | One seat's answer in Thinking mode; past it only that seat drops out (`N초 초과(THINK_SEAT_TIMEOUT_S)`). Shorter than `LLM_TIMEOUT_S` on purpose — a per-seat cap. Was 180, which every seat exceeded while a large deliberation held the shared LLM. |
| `MCP_CALL_TIMEOUT_S` | `900` | How long this server waits for one tool call through the gateway (seat lookups, VOC, role restore, report save). Set as the MCP session request deadline; `0` = none. Must stay **larger** than the gateway's `GATEWAY_CALL_TIMEOUT` (600, outer deadline 660) — the gateway should expire first and name the slow backend; this is the last net for a hung gateway. |
| `KNOWLEDGE_TIMEOUT_S` | `180` | One seat's knowledge-card lookup (`agent_search`) before the round starts; on expiry it asks once more in `KNOWLEDGE_FALLBACK_MODE` and the seat's status line says so. Intentionally **shorter** than the gateway limit — it is the switch to the fallback, not a wrapping layer. Must stay above AIDataHub's `DB_POOL_TIMEOUT` (60) + `AIDH_SEARCH_STATEMENT_TIMEOUT_S` (90). |
| `DELIB_HEARTBEAT_S` | `15` | While a deliberation stream has nothing to send, emit `event: ping` / `data: {"idle_s", "ts"}` at this interval so proxies and idle read timeouts see a live stream. `0` turns it off — then every outer idle limit (portal `AGENT_STREAM_IDLE_TIMEOUT_S`, nginx `NGINX_AGENT_READ_TIMEOUT`, risk app `HWAXRISK_ENGINE_READ_TIMEOUT_S`) must exceed the silence of one seat turn, (1 + parse retries)×((1+`DELIB_LLM_MAX_RETRIES`)×`DELIB_TIMEOUT_S`+8) = 10824 s by default — not one logical call (the startup log prints the figure for the box). Consumers must ignore unknown event names. |
| `AGENT_RESTART_FORCE` | `0` | `start.sh` will not stop a running instance that reports running or queued deliberations — a restart cuts all of them. It prints the counts and exits `3` (skipped), leaving the instance up. `1` restarts anyway. If the counts cannot be read (no answer, older build) it says so and restarts. |
| `AGENT_STOP_GRACE_S` | `2` | Seconds `start.sh` waits between TERM and KILL when it replaces a running instance. Not a wait for deliberations to finish. A value that is not a plain number (trailing comment or space, a unit) is read as the default, with a warning. |
| `AGENT_HEALTH_PROBE_S` | `3` | How long `start.sh` waits for `/health` when it asks the running instance for those counts. A value that is not a plain number is read as the default, with a warning — it no longer turns the guard off. |

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
| `POST /drain` · `GET /drain` | The update-all drain flag (not relayed by the portal, loopback only): `{on, scope: all\|panels, ttl_s \| until_idle{grace_s, max_s}, hold?, by, reason, force?}`. Life only grows; a `hold` flag is changed only by a `hold` request (a `hold` with no `ttl_s`/`until_idle` lives until a human turns it off — it does not inherit an earlier TTL); turning off needs the same `by` (or `force`) — otherwise 409 `drain_held`. A non-`hold` request from a `by` that is not the live flag's owner is refused with 409 `drain_owned` (the body's `drain` names the owner, `scope` and `until`; nothing is merged — update-all reads any 409 as "someone else owns it, don't touch"); a `hold` request is accepted from anyone and takes ownership. TTL flags are capped at 6 h: one request's `ttl_s` ≤ 21,600 (above is 422 — the portal clamps), a renewal by the owner while the TTL is alive extends the life and restarts the 6 h window (a renewer is not a forgotten flag; one that stops renewing dies at its last renewal + `ttl_s`), a TTL request after the TTL part died (e.g. the flag lived on through `until_idle`) starts a fresh TTL, and a remaining life longer than the cap — a clock that went back — is not trusted; `hold` flags are exempt. `ttl_s`/`max_s` must be finite; `by` is cut at 120 characters and `reason` at 300. A flag file with any bad field (or one that fails to evaluate) reads as off and is deleted; an unreadable file reads as off and is logged once per 5 min; failing to record `until_idle.idle_since` on a read keeps the flag alive (logged once per 5 min). A flag that cannot be written — or, on `on:false`, removed — is 503 `drain_unavailable`. `scope=all` makes the gate 0 (`review_busy{reason:"draining", retry_after_s: 60}` for run and pack), `panels` only tells the risk app not to pick new panels. The flag is a file (`DRAIN_STATE_PATH`, default `<ARTIFACT_DIR>/review-drain.json`) and survives a restart. |

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
or a short reason (same impact) — preferring the same impact, then the same place (`path` · `l`); and a repaired `q2` replaces the earlier empty-item `q2` whose reason was
short; above `items_max` the hits the model wrote (`yes` · `unknown`) are kept first, then engine-coerced ones, and `quality.dropped_over_cap` counts each dropped
row once), `summary` `head` · `risk` · `conflict` ·
`gain` · `watch` · `ask` · `para` with `r` (row aliases) and `refs` (the row refs) — `head`/`para`/`risk`/`conflict`
need text (`checks.text="empty"` counts as a missing line), a `risk` cites at most 3 rows (`checks.refs="too_many"`),
and a `must_cover` row counts as covered only by a `risk`/`conflict` with text and `checks.refs` `ok` or `unknown_alias`
(the valid aliases left count), one line covering at most 3 `must_cover` rows (the prompt and the repair hint say
so — `cr-summary-2`); what is not
covered is re-asked, what is still missing after the repairs is in `result.missing` (`head`/`para` and R aliases —
resolve them with `meta.aliases.rows`). Several `head` lines keep the first with text (`checks.extra_heads`). When two
rows for the same card/unit/impact disagree, a hit (WARNING·FAIL, `rel` yes·maybe, `impact` yes·unknown) is never
displaced by a non-hit — unless the engine coerced that verdict from an out-of-vocabulary value (`rel` → `maybe`,
`impact` → `unknown`), which an explicit answer replaces (a value differing only in case or surrounding spaces —
`Yes`, `Fail` — is taken as written, not coerced; a coerced verdict never makes a conflict). The survivor carries `checks.dup_conflict=true` and the losing
row as `contested_by{verdict, cq, quote, why, attempt}`; once a contesting answer's quote is verified, the survivor's
own failed quote is not re-asked (its `checks.quote` stays) — for `q1` only when the contesting row is in the same place
(`contested_by` then also carries its `path` and `l`). Between rows of the same standing, repair reasons
(out-of-vocabulary values, failed quotes, short reasons, empty text) weigh before flag-only checks (unsourced numbers,
refs). A repair that replaces a row with a different verdict carries `flipped_from{verdict, attempt}` (plus `raw`, the
value the model wrote, when the replaced verdict was coerced), and a
replacement keeps the `dup_conflict`, `contested_by` and `flipped_from` of the row it replaces. `quality.dup_conflict` and `quality.flipped` count those rows. `meta.basis` is `cards` · `background` · `role_only` · `search_only` ·
`noinput` · `rows`; offcard with search carries `meta.search{state: ok·empty_scope·no_hits·failed, queries, hits_n,
scope_n, used[{alias, record_id, section_id, title, chars, orig_chars, clipped}], note, req_cut}` (`failed` = could
not ask — the only one the app re-issues; an error envelope — no `hits` list, `ok:false`, `status:"error"`, `error` or
`errors` — is `failed`, while `refused:true` is not an error). Search runs with the user's PAT only. A clipped excerpt
carries its length mark on the line after the closing `»`, outside the quoted text, so a quote cannot borrow the mark. Every
result carries `meta.clipped{<input>: {orig_chars, kept_chars, …}}` for the inputs cut to their share (`role`,
`digest`, `missing`, `known` with `orig_items`/`kept_items`, `mechanisms`/`domains` with `labels_dropped`, summary
`blocks`; `{}` when nothing was cut). Summary number checks use the text the model actually saw (not the clip mark, nor a number the clip cut in half).

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
