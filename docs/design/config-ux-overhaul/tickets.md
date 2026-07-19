# Tickets: Config UX Overhaul

A tracer-bullet breakdown of `docs/design/config-ux-overhaul/PRD.md` — four
vertical slices that fix the fetch deadlock, the pool deletion cascade, the
rename trap, and the zero-pool state. Reference: PRD at
`docs/design/config-ux-overhaul/PRD.md`.

Work the **frontier**: any ticket whose blockers are all done. For a purely
linear chain that means top to bottom.

## Remove pool and agent rename (full stack)

**What to build:** Delete the entire rename path — framework ABC method,
business-layer controller helpers, REST endpoint, frontend UI, i18n keys, and
unit/conformance tests — so no user (WebUI or API client) can trigger the
half-built rename that silently orphans transcripts, memory, experiences,
inbox, and session-index records. After this ticket, the only supported
identity-change workflow is delete (cascade-cleaned) + create new.

**Blocked by:** None — can start immediately.
**Status:** ✅ COMPLETED

- [x] `PATCH /api/pools/{name}` rename endpoint in `bot/webui/server.py`
      returns 404 or 405 (endpoint removed).
- [x] `PoolStore.rename_pool` is deleted from
      `src/modex_agent/multi_agent/pool_config/store.py`.
- [x] `PoolRoutingStore.rename_pool` ABC method is deleted from
      `src/modex_agent/multi_agent/pool_router.py`.
- [x] `LocalFilePoolRoutingStore.rename_pool` is deleted from the file
      adapter.
- [x] `SqlitePoolRoutingStore.rename_pool` is deleted from
      `src/modex_agent/persistence/adapters/pool_routing_store.py`.
- [x] `PoolConfigController.rename_pool`,
      `PoolConfigController._apply_pool_rename`, and
      `PoolConfigController._apply_agent_renames` are deleted from
      `examples/bot_project/bot/service/pool_config_controller.py`.
- [x] Frontend `PoolsView` / `PoolEditor` no longer offer a rename action
      (button, inline edit, or otherwise).
- [x] Rename-related i18n keys are deleted from the locale files.
- [x] `tests/conformance/test_pool_routing_store_conformance.py` no
      longer asserts the `rename_pool` method (the ABC method is gone);
      the conformance suite for `PoolRoutingStore` is updated to the new
      ABC shape.
- [x] Existing pool CRUD tests pass — no rename regression, no broken
      import paths.
- [x] `ruff check` and `mypy` pass on the touched files.

## Remove default pool concept and add zero-pool support

**What to build:** Delete the `_default_pool: str = "default"` hardcoded
string, the `DefaultPoolProtectedError` class, and every 409 special-case
that blocked deleting "the default pool". Replace the fallback with a derived
property — `list_pools()[0]` if pools exist, `None` otherwise — and widen
`PoolRouter.default_pool` to `str | None` with a non-crashing guard. Support
a true zero-pool state end-to-end: `BotService.initialize` starts normally
with an empty `config/pools/` (no force-created "default" pool), and any
message arriving when no pool exists is intercepted at `ResolvePoolStage`
with a clear English `Terminate` feedback that rides the existing
`result.response["message"]` adapter contract (no new feedback channel).

**Blocked by:** None — can start immediately.
**Status:** ✅ COMPLETED

- [x] `BotService._default_pool: str = "default"` is replaced by a derived
      `_default_pool_name -> str | None` property returning
      `list_pools()[0].name if list_pools() else None`.
- [x] `BotService.initialize` no longer force-creates a pool named
      `"default"`; an empty `config/pools/` starts normally with an
      informational log.
- [x] `PoolRouter.__init__` parameter `default_pool: str` widens to
      `default_pool: str | None`.
- [x] `PoolRouter.route_message` adds a `None`/empty-pools guard: if
      `target is None or target not in self._pools`, log the error and
      return without dispatching (defense-in-depth — the
      `ResolvePoolStage` intercepts earlier).
- [x] `DefaultPoolProtectedError` class and all its raise/catch sites are
      deleted.
- [x] `PoolConfigController.delete_pool` no longer checks
      `name == self.default_pool`.
- [x] `PoolStore.delete_pool` signature loses the `default_pool=` parameter.
- [x] Frontend `PoolsView` 409 "cannot delete default" special-case handling
      is deleted.
- [x] `cannotDeleteDefault` i18n key is deleted.
- [x] `GET /api/pools` returns `[]` when `config/pools/` is empty.
- [x] `BotInputContext` gains `available_pools: Callable[[], set[str]]` —
      a callable returning the current routable pool names (queried at
      call time).
- [x] `ResolvePoolStage.process` returns
      `Terminate(reason="no_pool_configured", response={"message":
      "No pool is configured. Please create a pool in the settings
      first."})` when `ctx.available_pools()` returns an empty set.
- [x] `ResolvePoolStage.process` returns
      `Terminate(reason="pool_unavailable", response={"message":
      "Pool '<name>' is not available. It may have been removed. Please
      select a different pool."})` when the resolved pool is not in
      `ctx.available_pools()`.
- [x] Both `Terminate.response["message"]` strings are English and
      canonical (single source, no localization layer).
- [x] The pipeline stops at the `Terminate` — no downstream stage runs
      (the enqueue callback is not invoked).
- [x] Deleting any pool — including the first (alphabetical) and the last
      — does not return a 409 from the default-pool path. (A 409 from the
      active-session check in the cascade ticket is still expected when
      applicable.)
- [x] `ruff check` and `mypy` pass on the touched files.

## Pool deletion cascade cleanup

**What to build:** Make `DELETE /api/pools/{name}` remove every artifact
owned by that pool — not just `config/pools/{name}/` and the main-agent
prompt, but also skills, routing entries, transcripts, runtime state,
memory, experiences, inbox, and session-index records — across both the
file and SQLite backends. Reject the delete with a clear 409 listing busy
agents when any agent in the pool has an active turn (`AgentState.WORKING`),
so an in-flight run is never silently truncated. The cascade is packaged in
a single `_cascade_delete(name)` helper on `PoolConfigController`; each
store owns its own per-store delete through a new
`PoolRoutingStore.delete_pool_routes` ABC method (symmetric to the deleted
`rename_pool`).

**Blocked by:** Remove default pool concept and add zero-pool support
(the cascade must not crash `PoolRouter` when the last pool is deleted,
which requires the zero-pool `None` guard landed first).
**Status:** ✅ COMPLETED

- [x] `DELETE /api/pools/{name}` on a pool with no active sessions returns
      200 and `{"deleted": "<name>"}`.
- [x] After a successful delete, none of these paths/rows exist:
      `config/pools/{name}/`, `agents/{main_agent}.md`,
      `skills/{name}/`, `<ws>/.modex/sessions/{name}/`,
      `<ws>/.modex/runtime_state/{name}/`,
      `<ws>/.modex/memory/{name}/`,
      `<ws>/.modex/experiences/{name}/`,
      `<ws>/.modex/inbox/{name}/`,
      `<ws>/.modex/session_index/{name}/`.
- [x] `PoolRoutingStore.list_prefixes()` returns no prefixes whose stored
      pool matches `{name}` after the delete.
- [x] `GET /api/pools` does not include `{name}` after the delete.
- [x] `GET /api/sessions` returns no conversations whose `pool` field is
      `{name}` after the delete.
- [x] `DELETE /api/pools/{name}` when at least one agent in the pool has
      `AgentState.WORKING` returns 409 with a body containing
      `{"busy_agents": ["..."]}`.
- [x] On the 409 rejection, no artifact is removed — the cascade did not
      start.
- [x] The active-session check is implemented in the business layer using
      the framework's public `AgentRegistry.list_agents` +
      `get_status` + `AgentState.WORKING`; no framework changes.
- [x] `PoolRoutingStore.delete_pool_routes(pool_name)` is added to the
      ABC, symmetric in shape to the deleted `rename_pool`.
- [x] `LocalFilePoolRoutingStore.delete_pool_routes` iterates
      `pool_sessions/*.json` and deletes any whose `pool` field matches.
- [x] `SqlitePoolRoutingStore.delete_pool_routes` runs a single
      `DELETE FROM pool_routing WHERE pool_name = ?`.
- [x] `TranscriptStore.delete_pool_transcripts(pool_name)` removes all
      transcript files for the pool (file: `shutil.rmtree`; SQLite:
      scope-filtered `DELETE`).
- [x] The cross-store cascade is packaged in a single
      `PoolConfigController._cascade_delete(name)` private helper, called
      from `delete_pool` after the active-session check passes.
- [x] The cascade works identically on the file backend and the SQLite
      backend.
- [x] The conformance suite for `PoolRoutingStore` covers
      `delete_pool_routes`.
- [x] `ruff check` and `mypy` pass on the touched files.

## Fetch model list without prior save (backend + frontend)

**What to build:** Let a user fetch a provider's model list before saving
the provider, by accepting the connection info inline in the fetch request
body — converging with the existing "read from saved config" form on the
same pure-function call. On the frontend, the "Save & Fetch" button becomes
an unconditional "Fetch Models"; the fetch modal chooses the request shape
based on whether the provider is already saved. Relax the save validation
so a provider can be persisted without a `default_model`, breaking the
deadlock where a new provider cannot be saved (no default) and cannot be
fetched (not saved). The inline `api_key` is used in-memory only — never
logged, never persisted.

**Blocked by:** None — can start immediately.
**Status:** ✅ COMPLETED

- [x] `POST /api/models/fetch` with body `{"provider_key": "<name>"}`
      returns the model list read from the saved `model.yml` (existing
      behaviour, regression-protected).
- [x] `POST /api/models/fetch` with body `{"base_url": "...",
      "api_key": "...", "interface_format": "openai_compatible",
      "models_url": null}` returns the model list read from the request
      body (new inline form).
- [x] Both forms return the same model list when the inline parameters
      match the saved provider's config.
- [x] Both forms converge on the existing `fetch_provider_models` pure
      function — no behavioural fork between the two paths.
- [x] `provider_key` present → Form A; the handler resolves the four
      connection parameters (`base_url`, `api_key`, `interface_format`,
      `models_url`) from the saved config and ignores other body fields.
- [x] `provider_key` absent → Form B; the handler reads the four
      parameters from the body. `interface_format` defaults to
      `openai_compatible`; `models_url` defaults to `null`.
- [x] Form A with an unknown `provider_key` returns 404.
- [x] Form B with empty `base_url` or `api_key` returns 422.
- [x] The handler does not log the request body. It logs `provider_key`
      (Form A) or `base_url` (Form B), never `api_key`.
- [x] A Form B fetch leaves `model.yml` unchanged — no persistence side
      effect.
- [x] Frontend "Save & Fetch" button becomes "Fetch Models"
      unconditionally; the `handleFetchClick` dirty-save precondition is
      deleted.
- [x] `FetchModelsModal` submits Form A (`{"provider_key": "..."}`) when
      the provider is already saved.
- [x] `FetchModelsModal` submits Form B (inline four parameters) when the
      provider is unsaved (draft state, no `provider_key`).
- [x] `validateModelValues` no longer requires `default_provider` and
      `default_model` to be non-empty; a provider list with no default
      is saveable as long as each provider is well-formed (non-empty
      `base_url` and `api_key`).
- [x] The "save the provider first" backend error message and its
      frontend counterpart are deleted.
- [x] `ruff check` and `mypy` pass on the touched files.
