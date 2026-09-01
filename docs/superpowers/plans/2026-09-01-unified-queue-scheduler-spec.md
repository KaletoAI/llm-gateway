# Unified Queue Scheduler — Spec

User request (2026-09-01): replace priority/speed routing with ONE scheduling rule set,
for ALL requests (LLM and media, "mach es für alles generell"):

- Every request always enters a queue (backend-independent).
- Several backends free → the FASTEST free one serves the request (no more
  priority/speed switch anywhere).
- One backend free → the request runs there.
- None free → the request waits (parks), as today.
- When a backend frees, it first looks at what TYPE it just processed and takes the
  oldest queued request of the SAME type — so loaded models stay loaded.
- A new configurable fallback time bounds the wait: a request queued longer than that
  gets strict priority on the next free capable backend.
- REMOVED: the speed toggle (route_mode / route_speed) and priority as a routing input.

## Definitions

**Type key** (what a backend has "loaded"):
- media (ComfyUI) request → the generation **alias** (one alias = one workflow = one
  set of loaded models). Chain stages count separately (stage 1 = client alias,
  stage 2 = successor alias).
- LLM request → the **real model id** the request resolves to on that backend
  (llama-swap boxes avoid a model swap; for cloud APIs the key is meaningless but
  harmless).

Per backend the gateway tracks `backend_last_key[bid]` = the type key of the last
request DISPATCHED to it (set at dispatch, not completion — the model being loaded
right now is what's in VRAM next).

**Speed** (per candidate, higher is better):
- LLM: existing `backend_tps[bid]` EMA (tok/s). Unmeasured = +inf → probed first
  (existing convention).
- media: NEW `gen_speed["<alias>|<bid>"]` EMA of job duration in seconds (lower is
  faster). Unmeasured = 0.0 → probed first. Updated on every successful generation;
  best-effort seeded at boot from stats.db (`AVG(duration_ms)` per alias+backend of
  successful media calls).
- EMA alpha 0.3 (same spirit as `_TPS_ALPHA`).

**Paid tier** — cost guard: pure speed routing would send everything to cloud APIs.
New per-backend boolean `paid` (config + backends UI checkbox, default false).
Ordering is `(paid, speed)`: a paid backend is only used when NO unpaid candidate is
free — exactly today's overflow-to-cloud behaviour, minus the fine-grained priorities.
Post-deploy action (documented, not automated): set `paid` on claude, openrouter,
together in the prod config.

**Fallback time** — new config `affinity_max_wait_s`, default 120, hot-reloaded from
the `settings:` section like `async_park_timeout_s`, editable on the /ui/input page.
Semantics: the affinity reordering may delay a queued request at most this long;
after that the request is "overdue" and is served strictly oldest-first by the next
free backend that can run it.

## Dispatch rules

Fresh request (or any wake/poll of a waiting one):

1. Resolve candidates as today (health, drain, serves_path, force pin, LoRA
   eligibility, retries cap — ALL unchanged).
2. Ready (free) candidates are ordered `(paid ascending, speed descending)`.
   The shared-GPU demotion for LLMs (`_media_busy_hosts`) stays applied AFTER this
   ordering (dominant, as today). Priority no longer sorts anything.
3. If at least one ready candidate exists AND the request is the **designated taker**
   of that backend (see below), dispatch to the best ready candidate it is designated
   for. Otherwise wait (park), as today, and re-check on wake/poll.

**Designated taker** of a free backend B, decided over the waiting pool (LLM pool =
`_parked`; media pool = the new `_gen_waiting` registry; the pools are separate —
an LLM backend never serves media and vice versa):

1. Overdue entries (waited > `affinity_max_wait_s`) that B can serve → oldest first.
2. Else entries whose type key on B equals `backend_last_key[bid(B)]` → oldest first.
3. Else the oldest entry B can serve.
4. A request not in the pool yet (fresh arrival) joins the pool first — "immer in
   die Queue"; with a free designated backend it dispatches in the same call chain
   (no artificial delay).

The wake mechanism stays a broadcast (`_notify_slot_free` + the 2 s gen poll): every
waiter re-checks itself; only the designated one claims (the existing no-await
busy-check+inc guarantees a single winner; losers re-park). This deliberately keeps
today's task lifecycle, cancel and timeout plumbing intact.

Timeouts and failure semantics are UNCHANGED: `park_timeout_s` / per-alias park time
(LLM), `async_park_timeout_s` + `park_health_grace_s` (media), chain semantics from
commit 6965382 (successor park windows), self-retries, failover on connection errors.

## Removals

- `route_mode` (per-alias/model speed switch): config key, store rows/methods
  (`get_route_mode`/`set_route_mode`), `_speed_keys`, routing-page toggle chip,
  mapping-page "routing" select, chat-alias save handling.
- Per-backend `route_speed` flag: reads and UI.
- Priority as a ROUTING input: sorting in `rebuild_route_index`/`_gen_routes`,
  `_DEFAULT_PRIORITY`, per-alias priority overrides (`alias_entry` keeps parsing the
  `{model, priority}` store shape but the priority is ignored), the priority input on
  the backends page, priority in badges/labels. The `priority` key may remain in
  existing configs/stores; it is simply no longer read. `/health` drops the field.
- No other backward-compat shims (per project rule).

## Out of scope

- No change to auth, quotas, stats logging, LoRA library, chain hand-off mechanics,
  ComfyUI adapters, VRAM freeing, drain, health probing.
- No persistence of `gen_speed`/`backend_last_key` (in-memory; stats seed covers
  restarts well enough).
- Embeddings/audio/vision routes inherit the new ordering through `resolve_routes`
  automatically; they get no separate queue mechanics beyond what they have today.
