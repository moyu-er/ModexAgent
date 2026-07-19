# Config UX Overhaul: Fetch Deadlock, Pool Cascade Cleanup, Rename Removal, Zero-Pool Support

Status: ready-for-agent

Related: ADR-0020 (`docs/adr/0020-pool-config-convergence-and-framework-promotion.md`
— `PoolStore` / `PoolSpec` / `PoolInstance` live in the framework); ADR-0023
(`docs/adr/0023-hybrid-persistence.md` — split store ABCs, `RecordScope`
canonical JSON drives both file path segments and DB scope columns); ADR-0018
(`docs/adr/0018-session-gc.md` — `SessionArtifactCleaner` cascade pattern);
ADR-0015 (revised — `InboxPoller` owns per-pool turn dispatch);
`CONTEXT.md` → "Pool", "Pool Instance", "Input Pipeline", "Main Agent",
"Inbox", "InboxPoller", "Agent Inbox", "RecordScope", "Session Artifact Cleaner";
`examples/bot_project/CONTEXT.md` → "Channel", "Stage", "Claim",
"Claim-and-terminate", "Session Record", "Session Artifacts", "Cascade",
"clean_session", "Deletion Backstop".

## Problem Statement

A user who opens the WebUI to configure pools and providers hits a wall at
every step of the configuration lifecycle. The configuration surface — REST
endpoints, input pipeline, file/SQL persistence — was wired point-by-point
without a coherent lifecycle story, and the gaps now compound each other into
unworkable deadlocks and silent data corruption.

**Deadlock on provider onboarding.** Adding a new provider requires fetching
its model list, but the fetch endpoint refuses to run unless the provider is
already saved to `model.yml`, and the save endpoint refuses to accept a
config without a non-empty `default_model`. A user with a brand-new
provider — no models yet — cannot save (no default model) and cannot fetch
(no saved config). The only workaround is a 7-step dance: hand-add a
placeholder model, set it as default, save, fetch, delete the placeholder,
re-set the default, save again.

**Pool deletion leaves a trail of orphaned state.** `DELETE /api/pools/{name}`
removes the `config/pools/{name}/` directory and the main-agent prompt file,
then stops. Every other artifact — transcripts, runtime state, memory,
experiences, inbox, skills, session index, pool routing entries — is left on
disk (and in the SQLite DB, when that backend is in use). The WebUI session
list still shows conversations belonging to a pool that no longer exists,
with a `pool` field pointing at a deleted name. `PoolRouter` may route new
messages to a pool that is half-gone.

**Pool and agent rename are a half-built trap.** The rename path rewrites
config files and skills directories, but leaves every other artifact —
transcripts (whose filenames embed agent names via `session_id`), runtime
state, memory, experiences, inbox, session-index records (whose `session_id`
and `agent_name` fields embed the old names), peer `peers:` lists — pointing
at the old name. Worse, on the SQLite backend, `RecordScope.canonical()` is
the source of truth for `scope_key` across every table
(`messages`/`kv`/`cursors`/`archive`/`inbox`/`session_index`), and rename
would require rewriting JSON columns and regenerating scope keys across all
of them, row by row. A user who renames a pool or agent silently loses access
to all historical sessions.

**Default pool is a hidden lock.** `DefaultPoolProtectedError` blocks
deleting or renaming the pool currently acting as `default_pool` fallback.
The user has no UI to transfer the fallback role, so once a pool becomes
default it can never be deleted or renamed through the WebUI.

**Zero-pool state is undefined behaviour.** If a user deletes every pool,
`PoolRouter.route_message` dereferences `self._default_pool` against an
empty `pools` dict and crashes. `BotService.initialize` force-creates a
pool literally named `"default"` to avoid this, which is invisible to the
user and unconfigurable through the UI.

## Solution

A coherent configuration-lifecycle story across five areas, with a strict
constraint: the fix must reduce total code, not add to it. The
half-implemented paths are deleted, not patched; the missing paths are added
once, at the right seam.

**1. Fetch accepts inline connection info.** The `POST /api/models/fetch`
endpoint accepts either `{"provider_key": "..."}` (read connection info from
the saved `model.yml`) or `{"base_url": "...", "api_key": "...",
"interface_format": "...", "models_url": "..."}` (read connection info from
the request body). Both forms converge on the same pure-function call
(`fetch_provider_models`) with the same four connection parameters; the only
difference is the source of those parameters. The api_key in the inline form
is used in-memory only — never logged, never persisted. The frontend's
save-before-fetch precondition and the `default_model` required-on-save
validation are relaxed so a provider can be saved without a default model.

**2. Pool deletion cascades across every artifact.** `DELETE /api/pools/{name}`
runs a single `_cascade_delete(name)` helper that removes, in order: config
directory, main-agent prompt, per-pool skills, pool routing entries (via a
new `PoolRoutingStore.delete_pool_routes(pool_name)` symmetric with the
existing `rename_pool`), transcripts, runtime state, memory, experiences,
inbox, and session-index records for that pool. The cascade works on both
backends: file implementations delete directories; SQLite implementations run
single-statement `DELETE WHERE pool = ?`. If any agent in the pool has an
active turn (`AgentState.WORKING`), the delete is rejected with a 409 that
lists the busy agents, so an in-flight turn is never silently truncated.

**3. Pool and agent rename are removed.** The rename paths — REST endpoints,
`PoolConfigController.rename_pool` / `_apply_pool_rename` /
`_apply_agent_renames`, `PoolStore.rename_pool`, `PoolRoutingStore.rename_pool`
(and its SQLite implementation), and the WebUI rename UI — are deleted in
full. Rename across two persistence backends, where every artifact embeds the
name in either its filesystem path or its `RecordScope.canonical()` JSON, is
a multi-table row-by-row rewrite with no test surface that can prove
correctness. Delete-and-recreate is the supported workflow: delete the old
entity (cascade-cleaned) and create a new one.

**4. The "default pool" concept is deleted; fallback is derived.** The
`_default_pool: str = "default"` hardcoded string in `BotService` is removed.
`PoolRouter.default_pool` becomes `str | None`, derived at startup as the
first pool returned by `PoolStore.list_pools()` (alphabetical). If the
fallback pool is deleted, the next call to `route_message` re-derives from
the (now shorter) `list_pools()`. `DefaultPoolProtectedError` and all its
callers are deleted; deleting a pool never fails on "is default".

**5. Zero-pool state is supported end-to-end.** `BotService.initialize`
starts normally when `config/pools/` is empty — no force-creation of a
"default" pool, no crash. New messages arriving when no pool exists are
intercepted at `ResolvePoolStage` (the input pipeline stage already
responsible for pool resolution), which returns
`Terminate(reason="no_pool_configured", response={"message": "No pool is
configured. Please create a pool in the settings first."})`. The
`response["message"]` field is the existing feedback contract — every
adapter (QQ, Telegram, WebSocket) already reads `result.response["message"]`
and forwards it to the user via `OutputAdapter.send`. No new feedback
channel is introduced. The English-only message is intentional: it is the
single canonical message, surfaced verbatim across IM and WebUI.

## User Stories

### Provider onboarding (fetch deadlock)

1. As a bot maintainer, I want to add a new provider by filling in its
   base URL, API key, interface format, and (optionally) a models URL in the
   WebUI, so that the connection info is captured before any save.
2. As a bot maintainer, I want to click "Fetch Models" on a provider that
   has not been saved yet, so that I can discover the provider's model list
   before committing it to configuration.
3. As a bot maintainer, I want the fetch request to carry my unsaved
   connection info to the backend in the request body, so that the backend
   can query the provider as if it were already saved.
4. As a bot maintainer, I want the fetched model list to appear in the
   provider's model list editor, so that I can pick a default model from
   real data rather than typing a placeholder.
5. As a bot maintainer, I want to save a provider configuration that has
   no `default_model` set, so that I can save the connection info first
   and choose a default model after fetching.
6. As a bot maintainer, I want to fetch models from an already-saved
   provider by selecting it, so that I can refresh its model list without
   re-entering connection info.
7. As a bot maintainer, I want the fetch endpoint to reject a request
   whose `provider_key` does not match any saved provider, so that a typo
   does not silently return an empty list.
8. As a bot maintainer, I want the fetch endpoint to reject an inline
   request whose `base_url` or `api_key` is empty, so that misconfiguration
   is caught early.
9. As a bot maintainer, I want the API key I send inline to a fetch
   request to never appear in server logs, so that secret material is not
   leaked through observability infrastructure.
10. As a bot maintainer, I want the API key I send inline to a fetch
    request to never be written to disk, so that a transient query does
    not mutate persisted configuration.
11. As a bot maintainer, I want the same model list returned whether I
    fetch by `provider_key` or by inline connection info (assuming the
    saved config matches the inline info), so that the two paths are
    observably the same operation.

### Pool deletion (cascade cleanup)

12. As a bot maintainer, I want to delete a pool from the WebUI and have
    every artifact owned by that pool removed, so that no orphaned state
    remains on disk or in the database.
13. As a bot maintainer, I want a deleted pool's transcripts to be
    removed, so that old conversations do not appear in the session list
    pointing at a non-existent pool.
14. As a bot maintainer, I want a deleted pool's runtime state (todos,
    turn snapshots, traces) to be removed, so that disk usage does not
    grow unbounded from abandoned pools.
15. As a bot maintainer, I want a deleted pool's memory archives to be
    removed, so that the workspace data directory reflects the actual
    configuration.
16. As a bot maintainer, I want a deleted pool's experiences to be
    removed, so that self-learned knowledge from a removed pool does not
    leak into other pools.
17. As a bot maintainer, I want a deleted pool's inbox queue to be
    removed, so that pending messages destined for a deleted pool do not
    accumulate indefinitely.
18. As a bot maintainer, I want a deleted pool's skills directory to be
    removed, so that per-agent skill assignments do not outlive their
    pool.
19. As a bot maintainer, I want a deleted pool's session-index records
    to be removed, so that the session list does not show conversations
    from a deleted pool.
20. As a bot maintainer, I want a deleted pool's routing entries in
    `PoolRoutingStore` to be removed, so that new messages for old
    sessions are not routed to a non-existent pool.
21. As a bot maintainer, I want the deletion to work identically on the
    file backend and the SQLite backend, so that I am not locked into
    one backend to get correct cleanup.
22. As a bot maintainer, I want the deletion to be rejected with a
    clear error when any agent in the pool has an active turn, so that
    an in-flight agent run is never silently killed mid-execution.
23. As a bot maintainer, I want the rejection error to list which
    agents are busy, so that I know which sessions to wait for.
24. As a bot maintainer, I want to delete a pool and immediately see
    it disappear from `GET /api/pools`, so that the UI reflects the new
    state without a restart.
25. As a bot maintainer, I want to delete a pool and have the change
    take effect on the running process after the next restart, so that
    in-memory `PoolInstance` references are cleaned up deterministically.

### Pool/agent rename removal

26. As a bot maintainer, I want the WebUI to not offer a rename action
    for pools, so that I am not tempted into a path that silently
    orphans historical sessions.
27. As a bot maintainer, I want the WebUI to not offer a rename action
    for agents, so that I am not tempted into a path that silently
    orphans transcripts and session-index records.
28. As a bot maintainer, I want the `PATCH /api/pools/{name}` rename
    endpoint to be gone, so that an outdated API client cannot trigger
    the broken rename path.
29. As a bot maintainer, I want to change a pool's identity by
    deleting it (cascade-cleaned) and creating a new one, so that the
    supported workflow has clean semantics.
30. As a bot maintainer, I want to change an agent's identity by
    deleting its pool (cascade-cleaned) and recreating it with the new
    name, so that the supported workflow has clean semantics.
31. As a framework architect, I want `PoolStore.rename_pool`,
    `PoolRoutingStore.rename_pool`, `PoolConfigController.rename_pool`,
    and `_apply_agent_renames` deleted, so that the half-built rename
    path cannot be revived by accident.

### Default pool concept removal

32. As a bot maintainer, I want to delete any pool without first
    transferring a "default" role, so that the configuration is not
    locked by an invisible concept.
33. As a bot maintainer, I want `PoolRouter` to fall back to the first
    available pool automatically, so that I do not need to designate a
    default at all.
34. As a bot maintainer, I want `DefaultPoolProtectedError` and its
    409 response gone, so that the only reason a delete fails is a
    genuine runtime constraint (active sessions).
35. As a framework architect, I want the `_default_pool: str =
    "default"` hardcoded string in `BotService` removed, so that the
    business layer does not encode a pool name that the user cannot
    see or change.

### Zero-pool support

36. As a bot maintainer, I want `BotService` to start normally when
    `config/pools/` is empty, so that I can boot a fresh install and
    create my first pool from the WebUI.
37. As a bot maintainer, I want `BotService` to not auto-create a
    pool literally named `"default"` on startup, so that the pool list
    reflects only what I have explicitly configured.
38. As a bot maintainer, I want to delete the last remaining pool,
    so that I can fully reset a workspace's pool configuration.
39. As an IM user, I want to receive a clear English message when I
    message a bot that has no pool configured, so that I understand
    the bot is not broken but is awaiting administrator setup.
40. As a WebUI user, I want to see a clear English message in the
    chat panel when I send a message to a workspace with no pool
    configured, so that I understand I need to create a pool first.
41. As a bot maintainer, I want the zero-pool feedback to be
    intercepted inside the input pipeline (not at the adapter or
    router), so that the interception lives at the single seam
    already responsible for pool resolution.
42. As a bot maintainer, I want the zero-pool feedback to use the
    existing `Terminate.response["message"]` contract, so that no new
    feedback channel is introduced across QQ, Telegram, and WebSocket
    adapters.
43. As a bot maintainer, I want the zero-pool feedback to be a single
    canonical English string, so that the message is identical across
    every channel.
44. As a bot maintainer, I want `PoolRouter` to handle a `None`
    fallback pool gracefully (log + drop), so that a race between
    deletion and message arrival does not crash the process.

### Frontend ergonomics

45. As a bot maintainer, I want the "Save & Fetch" button in the
    model editor to become "Fetch Models" unconditionally, so that I
    can fetch without the dirty-save precondition.
46. As a bot maintainer, I want the fetch modal to submit the current
    form's connection info inline when the provider is unsaved, so
    that the fetch uses the values I just typed.
47. As a bot maintainer, I want the fetch modal to submit the
    `provider_key` when the provider is already saved, so that saved
    providers reuse their stored connection info.
48. As a bot maintainer, I want the model validation to no longer
    require a `default_model` on save, so that a provider can be
    persisted before any model is chosen.
49. As a bot maintainer, I want the WebUI to not show a "cannot
    delete default pool" toast, so that the UI does not reference a
    concept that no longer exists.
50. As a bot maintainer, I want the WebUI pool list to not show a
    rename action, so that the UI matches the supported workflow
    (delete + create).

## Implementation Decisions

### Fetch endpoint convergence

The `POST /api/models/fetch` endpoint accepts a JSON body with two
mutually-exclusive shapes:

- **Form A (from saved config):** `{"provider_key": "<name>"}` — the
  handler resolves the four connection parameters (`base_url`, `api_key`,
  `interface_format`, `models_url`) from the saved `model.yml`.
- **Form B (inline):** `{"base_url": "...", "api_key": "...",
  "interface_format": "openai_compatible" | "anthropic",
  "models_url": null | "..."}` — the handler reads the four parameters
  directly from the body.

Both forms call the existing `fetch_provider_models(session, base_url,
api_key, interface_format, models_url)` pure function. Form A is a thin
adapter that resolves `provider_key` → four parameters; Form B passes them
through. The pure function is unchanged.

Validation:
- `provider_key` present → Form A; ignore any other body fields.
- `provider_key` absent → Form B; require non-empty `base_url` and
  `api_key`; `interface_format` defaults to `openai_compatible`;
  `models_url` defaults to `null`.
- Form A with an unknown `provider_key` → 404.
- Form B with empty `base_url` or `api_key` → 422.

Security:
- The handler does not log the request body. It logs `provider_key` (Form
  A) or `base_url` (Form B), never `api_key`.
- The inline `api_key` is held in a local variable for the duration of the
  `fetch_provider_models` call and discarded. No persistence path is
  touched.

### Frontend fetch flow

The `ModelEditor` "Save & Fetch" button becomes "Fetch Models"
unconditionally. The `handleFetchClick` dirty-save precondition is
deleted: clicking "Fetch Models" opens the `FetchModelsModal` directly.

The `FetchModelsModal` submit logic chooses the form based on whether the
provider being fetched is already saved:
- Saved provider (exists in `providers` list and `provider_key` is known)
  → submit Form A (`{"provider_key": "..."}`).
- Unsaved provider (draft state, no `provider_key` yet) → submit Form B
  with the four connection parameters read from the current form state.

`SettingsView.onSave` validation is relaxed: `validateModelValues` no
longer requires `default_provider` and `default_model` to be non-empty.
The validation now only checks that the `providers` list itself is
well-formed (no duplicate keys, each provider has non-empty `base_url`
and `api_key`). A config with providers but no default is saveable.

### Pool deletion cascade

`PoolConfigController.delete_pool(name)` runs, in order:

1. **Active-session check (business layer).** Look up the `PoolInstance`
   for `name` via `WorkspaceRegistry`. If found, enumerate
   `pool_instance.pool.list_agents()` and check
   `pool_instance.pool.get_status(desc.address.name)` for each. If any
   returns `AgentState.WORKING`, raise `PoolNotEmptyError` (→ HTTP 409)
   with the list of busy agent names. This is a business-layer
   computation over the framework's public `AgentRegistry` /
   `AgentState` API — no framework changes.
2. **Config cascade.** `PoolStore.delete_pool(name)` removes
   `config/pools/{name}/` (including `pool.yml`, `templates/*.yml`) and
   the main-agent prompt `agents/{main_agent_name}.md`. (Existing
   behaviour.)
3. **Skills cascade.** `SkillsStore.clear_pool_skills(name)` removes
   `skills/{name}/`. (Existing method, currently only called from
   `write_pool` for external_coding; now also called from `delete_pool`.)
4. **Routing cascade.** `PoolRoutingStore.delete_pool_routes(name)` — a
   new ABC method, symmetric to the existing `rename_pool`. File
   implementation: iterate `pool_sessions/*.json`, delete any whose
   `pool` field matches. SQLite implementation: a single
   `DELETE FROM pool_routing WHERE pool_name = ?`.
5. **Transcript cascade.** `TranscriptStore.delete_pool_transcripts(name)`
   removes all transcript files for the pool. File implementation:
   `shutil.rmtree(<ws>/.modex/sessions/{name}/)`. SQLite implementation:
   `DELETE FROM transcripts WHERE pool = ?` (or equivalent scope-filtered
   delete).
6. **Runtime state cascade.** Remove `<ws>/.modex/runtime_state/{name}/`
   (todos, turn snapshots, traces). File: `shutil.rmtree`. SQLite: delete
   from the relevant tables filtered by scope.
7. **Memory cascade.** Remove `<ws>/.modex/memory/{name}/` (session
   messages, KV, cursors, archive). File: `shutil.rmtree`. SQLite:
   `DELETE` from `memory_session_messages` / `memory_kv` /
   `memory_cursors` / `memory_archive` filtered by scope.
8. **Experiences cascade.** Remove
   `<ws>/.modex/experiences/{name}/`. File: `shutil.rmtree`. SQLite: not
   applicable (experiences are file-only).
9. **Inbox cascade.** Remove `<ws>/.modex/inbox/{name}/` (file) or
   `DELETE FROM inbox_messages WHERE pool = ?` (SQLite).
10. **Session-index cascade.** Remove
    `<ws>/.modex/session_index/{name}/` (file) or
    `DELETE FROM session_index WHERE pool = ?` (SQLite). This is the
    step that makes `GET /api/sessions` stop showing the deleted pool's
    conversations.

Steps 5–10 are packaged in a single `_cascade_delete(name)` private
helper on `PoolConfigController`, which is the single entry point. The
helper is symmetric in shape to the deleted `_apply_pool_rename` — it
owns the cross-store cascade, the stores own their own per-store
delete. The cascade is not transactional across stores (filesystem +
SQLite); a crash mid-cascade leaves partial state that the
`SessionArtifactCleaner`'s orphan sweep (ADR-0018) eventually
reconciles.

### Rename removal

Deleted from the framework:
- `PoolStore.rename_pool` (the `mv config/pools/{old} → {new}` wrapper).
- `PoolRoutingStore.rename_pool` ABC method.
- `LocalFilePoolRoutingStore.rename_pool` (the per-file scan/rewrite).
- `SqlitePoolRoutingStore.rename_pool` (the single `UPDATE`).

Deleted from the business layer:
- `PoolConfigController.rename_pool` (the HTTP-facing entry point).
- `PoolConfigController._apply_pool_rename` (the cross-store cascade
  that only handled skills + routing, leaving everything else orphaned).
- `PoolConfigController._apply_agent_renames` (the agent-level rename
  that only handled skills, leaving transcripts/memory/etc. orphaned).
- The `PATCH /api/pools/{name}` rename endpoint in `server.py`.

Deleted from the frontend:
- The rename button / inline-edit action in `PoolsView` /
- The rename-related i18n keys.

The `rename_pool` conformance tests (`tests/conformance/test_pool_routing_store_conformance.py`)
are deleted — the ABC method they test no longer exists.

### Default pool removal

- `BotService._default_pool: str = "default"` is replaced by a derived
  property `_default_pool_name -> str | None` that returns
  `list_pools()[0].name if list_pools() else None`.
- `PoolRouter.__init__` parameter `default_pool: str` becomes
  `default_pool: str | None`.
- `PoolRouter.route_message` adds a `None` / empty-pools guard: if
  `target is None or target not in self._pools`, log the error and
  return without dispatching. This is defense-in-depth — the
  `ResolvePoolStage` intercepts zero-pool messages before they reach
  the router, but the router must not crash if a race occurs.
- `DefaultPoolProtectedError` is deleted.
- `PoolConfigController.delete_pool` no longer checks `name ==
  self.default_pool`.
- `PoolStore.delete_pool` signature loses the `default_pool=` parameter.
- The 409 special-case handling in `PoolsView` is deleted.
- The `cannotDeleteDefault` i18n key is deleted.

### Zero-pool support

- `BotService.initialize` no longer force-creates a pool named
  `"default"`. If `PoolStore.list_pools()` returns empty, `BotService`
  starts with an empty `pools: dict` and logs an informational message.
- `BotInputContext` gains a new `available_pools: Callable[[],
  set[str]]` field — a callable that returns the current set of
  routable pool names (queried at call time, since the set changes as
  pools are added/deleted).
- `ResolvePoolStage.process` adds, before the existing resolution logic:
  - If `ctx.available_pools()` returns an empty set, return
    `Terminate(reason="no_pool_configured", response={"message":
    "No pool is configured. Please create a pool in the settings
    first."})`.
  - If the resolved pool name is not in `ctx.available_pools()`, return
    `Terminate(reason="pool_unavailable", response={"message":
    "Pool '<name>' is not available. It may have been removed. Please
    select a different pool."})`.
- The existing adapter feedback contract
  (`result.response["message"]` → `OutputAdapter.send` for IM adapters;
  `result.response["message"]` → `WebUIEventType.ERROR` envelope for
  WebSocket) is reused unchanged. No adapter code is modified.

### Net code impact

Approximate, excluding tests:

- **Added (~85 lines):** `_cascade_delete` helper, new
  `PoolRoutingStore.delete_pool_routes` ABC + two implementations,
  `TranscriptStore.delete_pool_transcripts`, `ResolvePoolStage`
  zero-pool / unavailable-pool guards, `BotInputContext.available_pools`
  field, `PoolRouter` `None` guard, `BotService._default_pool_name`
  derived property, fetch handler inline-form branch.
- **Deleted (~130 lines):** `DefaultPoolProtectedError` + its 409
  handling, `_default_pool` hardcoded string + 4 check sites,
  `PoolStore.rename_pool` + `default_pool=` param, all of
  `PoolConfigController.rename_pool` / `_apply_pool_rename` /
  `_apply_agent_renames`, both `PoolRoutingStore.rename_pool`
  implementations, the `PATCH /api/pools/{name}` endpoint, frontend
  `handleFetchClick` dirty-save precondition, frontend
  `modelValidation.defaultRequired`, frontend rename UI, fetch
  "save the provider first" error message, `cannotDeleteDefault` and
  rename i18n keys.
- **Net: roughly -45 lines.** The rename removal is the dominant
  reduction; the cascade and zero-pool additions are smaller because
  they reuse existing seams (`Terminate.response`, `PoolRoutingStore`
  ABC, `SessionArtifactCleaner` orphan sweep).

## Testing Decisions

### Testing philosophy

This is a lifecycle-correctness fix, not new behaviour. Tests verify
external contracts (REST responses, disk/DB state after operations,
pipeline feedback to the user), not internal mechanics. The highest
available seam is preferred for each area — if a REST-level test
passes, the internal cascade is correct. Implementation details
(which helper calls which store in which order) are not tested in
isolation.

### Test seam 1: Pool lifecycle REST API

**Existing file:** `examples/bot_project/tests/webui/test_pool_routes.py`

Extend this file to cover pool deletion cascade, rename removal, default
pool removal, and zero-pool startup. The REST API is the highest seam —
it exercises `PoolConfigController` → `PoolStore` → stores → disk/DB in
one call.

Assertions (external behaviour only):
- `DELETE /api/pools/{name}` on a pool with no active sessions returns
  200 and `{"deleted": "<name>"}`; after the call, none of these paths
  exist: `config/pools/{name}/`, `agents/{main_agent}.md`,
  `skills/{name}/`, `<ws>/.modex/sessions/{name}/`,
  `<ws>/.modex/runtime_state/{name}/`,
  `<ws>/.modex/memory/{name}/`, `<ws>/.modex/experiences/{name}/`,
  `<ws>/.modex/inbox/{name}/`, `<ws>/.modex/session_index/{name}/`;
  `PoolRoutingStore.list_prefixes()` returns no prefixes whose stored
  pool matches `{name}`; `GET /api/pools` does not include `{name}`;
  `GET /api/sessions` returns no conversations whose `pool` field is
  `{name}`.
- `DELETE /api/pools/{name}` when at least one agent in the pool has
  `AgentState.WORKING` returns 409 with a body containing
  `{"busy_agents": ["..."]}`; no artifact is removed (the cascade did
  not start).
- `DELETE /api/pools/{name}` on the last remaining pool returns 200
  (deletion succeeds; zero-pool state is valid).
- `PATCH /api/pools/{name}` (the old rename endpoint) returns 404 or
  405 (endpoint removed).
- `BotService.initialize()` with an empty `config/pools/` directory
  starts without raising; `GET /api/pools` returns `[]`; no
  `config/pools/default/` directory is created.
- After deleting the alphabetically-first pool, the next message
  routed without an explicit `pool_sessions/` entry is routed to the
  new alphabetically-first pool (fallback re-derivation).

### Test seam 2: Model fetch REST API

**Existing file:** `examples/bot_project/tests/webui/test_model_fetch.py`

Extend to cover the inline form and the convergence between the two
forms.

Assertions:
- `POST /api/models/fetch` with `{"provider_key": "openai"}` (where
  `openai` is a saved provider) returns the model list from the
  provider. (Existing behaviour, regression-protected.)
- `POST /api/models/fetch` with `{"base_url": "...", "api_key": "...",
  "interface_format": "openai_compatible"}` returns the same model
  list as Form A when the inline parameters match the saved `openai`
  provider's config.
- `POST /api/models/fetch` with `{"provider_key": "nonexistent"}`
  returns 404.
- `POST /api/models/fetch` with Form B missing `base_url` or
  `api_key` returns 422.
- After a Form B fetch, `model.yml` is unchanged (no persistence side
  effect).
- The handler's log output for a Form B request does not contain the
  `api_key` value (assert on captured log records).

### Test seam 3: Input pipeline zero-pool interception

**Existing file:**
`examples/bot_project/tests/input_pipeline/test_set_and_resolve.py`

Extend to cover the zero-pool and unavailable-pool guards in
`ResolvePoolStage`.

Assertions:
- `ResolvePoolStage.process(envelope, ctx)` where
  `ctx.available_pools()` returns `set()` returns a `Terminate` whose
  `reason == "no_pool_configured"` and whose
  `response["message"] == "No pool is configured. Please create a
  pool in the settings first."`.
- `ResolvePoolStage.process(envelope, ctx)` where the resolved pool is
  not in `ctx.available_pools()` returns a `Terminate` whose
  `reason == "pool_unavailable"` and whose `response["message"]`
  contains the unavailable pool's name.
- Both messages are in English (no localization layer; canonical
  strings).
- The pipeline stops at the `Terminate` (no downstream stage runs):
  assert that the enqueue callback (`ctx.enqueue_message`) was not
  invoked.

### What NOT to test

- Do not test the internal call order of `_cascade_delete` — it is an
  implementation detail. The REST-level assertion (all artifacts gone)
  covers correctness.
- Do not test `PoolRoutingStore.delete_pool_routes` in isolation — it
  is covered transitively by the REST-level "no prefixes match" assertion
  in seam 1. The conformance suite for `PoolRoutingStore` is updated to
  drop the `rename_pool` method (deleted) and add `delete_pool_routes`
  (the symmetric replacement); conformance tests verify the method
  contract, not the cascade.
- Do not test adapter feedback plumbing for the zero-pool case in
  isolation — the contract (`result.response["message"]` →
  `OutputAdapter.send`) is already covered by existing
  `Terminate.response` tests for other stages (pool switch, invalid
  skill). The new `Terminate` instances reuse the same contract.
- Do not test `BotService._default_pool_name` derivation in isolation
  — it is covered by the seam 1 "fallback re-derivation" assertion.

### Prior art

- `examples/bot_project/tests/webui/test_pool_routes.py` — REST-level
  pool CRUD patterns; the existing `test_delete_pool` is the direct
  extension point for cascade assertions.
- `examples/bot_project/tests/webui/test_model_fetch.py` — fetch
  endpoint test patterns; the existing `test_fetch_provider_models`
  is the direct extension point for the inline form.
- `examples/bot_project/tests/input_pipeline/test_set_and_resolve.py`
  — `ResolvePoolStage` unit test patterns; the existing
  `test_resolve_pool_*` cases are the direct extension point for the
  zero-pool guards.
- `tests/conformance/test_pool_routing_store_conformance.py` — the
  conformance suite that defines the `PoolRoutingStore` ABC contract;
  updated to drop `rename_pool` and add `delete_pool_routes`.

## Out of Scope

- **Restart protocol redesign.** All configuration changes still
  require a manual process restart to take effect on the running
  `PoolInstance` instances (the `_mark("pool")` → `restart_required`
  flow is unchanged). Per-pool hot reload is a separate architectural
  effort.
- **Skills assign/unassign semantic convergence.** Skills assignment
  remains eager (immediate disk effect) while other pool fields are
  deferred (Save button). Unifying these is a separate UX effort.
- **SecretField true-value readback.** The frontend still cannot read
  back a saved API key's true value (only its hint). Copy-to-clipboard
  of a saved secret is a separate security-scoped effort.
- **MCP server lazy cleanup.** Deleting a global MCP server still
  leaves stale references in pools until restart. Separate effort.
- **External-coding pool subagent draft preservation.** Toggling
  `external_coding` still clears subagent drafts. Separate effort.
- **Inline validation feedback.** Form errors still surface only on
  Save, not as the user types. Separate UX effort.
- **Workspace-switch dirty guard.** Editing a pool and switching
  workspaces without saving is still permitted. Separate effort.
- **Pool creation onboarding.** Pool creation still produces an empty
  pool directory; no template selector or "copy from existing" flow
  is added. Separate effort.
- **`AgentState` public API extension.** The framework's
  `AgentRegistry.list_agents` / `get_status` / `AgentState` are used
  as-is from the business layer; no framework changes.
- **Peer pool rename.** Peer relationships (`peers: [name]` lists in
  `pool.yml`) are not rewritten on any operation in scope. With rename
  removed, the only way a peer name becomes stale is if the user
  deletes a pool that another pool declares as a peer — this is left
  as a known issue (the declared peer will fail to resolve on next
  restart, with a logged warning) rather than cascading the delete
  across peer declarations. A future "peer cleanup on delete" could
  revisit this.

## Further Notes

### Implementation order (suggested)

1. **Framework: delete rename.** Remove `PoolStore.rename_pool`,
   `PoolRoutingStore.rename_pool` ABC + both implementations,
   `PoolConfigController.rename_pool` + `_apply_pool_rename` +
   `_apply_agent_renames`, the `PATCH /api/pools/{name}` endpoint, and
   the conformance tests for `rename_pool`. This is the largest
   deletion and unblocks steps 2–4 (no rename path to keep
   consistent).
2. **Framework: delete default pool.** Remove
   `DefaultPoolProtectedError`, the `default_pool=` parameter on
   `PoolStore.delete_pool`, the default-pool check in
   `PoolConfigController.delete_pool`, the 409 handling in
   `PoolsView`, and the `cannotDeleteDefault` i18n key. Replace
   `BotService._default_pool` with the derived
   `_default_pool_name` property; widen `PoolRouter.default_pool` to
   `str | None` with a `None` guard in `route_message`.
3. **Framework: add cascade delete.** Add
   `PoolRoutingStore.delete_pool_routes` ABC + file/SQLite
   implementations; add `TranscriptStore.delete_pool_transcripts`;
   add the `_cascade_delete(name)` helper on
   `PoolConfigController` and call it from `delete_pool`. Add the
   active-session check (business-layer helper over
   `AgentRegistry.list_agents` + `get_status`).
4. **Framework: add zero-pool support.** Add
   `BotInputContext.available_pools`; extend `ResolvePoolStage` with
   the two `Terminate` guards; delete the force-create-default-pool
   block in `BotService.initialize`.
5. **Framework: converge fetch endpoint.** Extend the fetch handler
   to accept Form B; relax `validateModelValues` to permit empty
   `default_model`; delete the "save the provider first" error
   message.
6. **Frontend: simplify fetch UX.** Make "Fetch Models" unconditional;
   have `FetchModelsModal` choose Form A vs Form B based on provider
   save state; delete the dirty-save precondition in
   `handleFetchClick`.
7. **Frontend: remove rename UI.** Delete the rename action from
   `PoolsView` / `PoolEditor`; delete the rename i18n keys.
8. **Run all three test seams.** Each seam must pass before the next
   step begins.

Steps 1–2 are deletions (no new behaviour to verify beyond "the old
path is gone"). Steps 3–4 are the new cascade + zero-pool paths (the
behavioural additions). Steps 5–6 are the fetch convergence. Step 7
is frontend cleanup. This ordering front-loads the deletions so the
additions land in a codebase that no longer has the half-built paths
to stay consistent with.

### ADR-0023 compatibility

The cascade delete operates on both persistence backends through the
split-store ABCs (`MessageStore` / `KVStore` / `CursorStore` /
`ArchiveStore` / `InboxMQ`) and the runtime-state ABCs. File
implementations delete directories; SQLite implementations run
`DELETE` statements filtered by the `pool` dimension extracted from
`RecordScope.canonical()`. No schema migration is required — the
`pool` generated column on every state-DB table already supports
filtered deletes. The `SessionArtifactCleaner` orphan sweep
(ADR-0018) remains the backstop for any cascade interrupted by a
crash.

### ADR-0020 compatibility

`PoolStore`, `PoolSpec`, `PoolInstance`, and `PoolRouter` are framework
types per ADR-0020. The `delete_pool_routes` ABC addition and the
`default_pool: str | None` widening are additive to the framework's
public surface; no ADR-0020 type is renamed or moved. The
business-layer `_cascade_delete` helper lives in
`PoolConfigController` (bot), not in the framework — the framework
provides the store-level primitives, the bot orchestrates the
cross-store cascade.
