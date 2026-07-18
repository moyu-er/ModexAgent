# Tickets: Prompt Configuration Independence

A tracer-bullet breakdown of `docs/design/prompt-configuration/PRD.md` — six
vertical slices that promote agent system prompts from a side-effect of
agents to a first-class top-level configuration concept with an explicit
reference field, a dedicated Prompts tab, clean `/api/prompts` REST
endpoints, a cross-pool reference check on deletion, and a prompt-selector
linkage in the PoolEditor. Reference: PRD at
`docs/design/prompt-configuration/PRD.md`.

Work the **frontier**: any ticket whose blockers are all done. Tickets 1
and 2 can start in parallel; the rest follow the dependency graph at the
bottom of this file.

## Schema foundation: `prompt_name` field + default prompt consolidation

**What to build:** Add the `prompt_name: str | None = None` field to
`MainAgentSpec` and `SubagentSpec` so an agent can explicitly declare which
prompt it references (decoupling prompt identity from agent name). Add a
`default_prompt_seed: str` injection parameter to `PoolStore` so the
framework stops hardcoding natural-language prompt text. Delete the
framework-layer `_DEFAULT_MAIN_PROMPT` constant and have the bot-layer
wiring pass the canonical `PromptStore.DEFAULT_PROMPT_SEED` into both
`PromptStore` and `PoolStore` at construction time. This ticket delivers no
user-visible behavior change — existing configs with no `prompt_name`
field load with `None` and fall back to the agent-name convention exactly
as before. It lays the schema and injection foundation for every
subsequent ticket.

**Blocked by:** None — can start immediately.
**Status:** ✅ COMPLETED (commit `f9df3e09`)

- [x] `MainAgentSpec` gains `prompt_name: str | None = None` (frozen=True,
      extra="forbid" unchanged).
- [ ] `SubagentSpec` gains `prompt_name: str | None = None` (frozen=True,
      extra="forbid" unchanged).
- [ ] A legacy `pool.yml` with no `prompt_name` key loads with
      `prompt_name = None` for both main and subagents.
- [ ] A `PoolSpec` round-trip through `model_dump()` → `model_validate()`
      preserves `prompt_name`.
- [ ] `PoolStore.__init__` accepts `default_prompt_seed: str` (defaulting
      to empty string at the framework level so existing framework tests
      that construct `PoolStore` without the parameter still pass).
- [ ] `PoolStore._DEFAULT_MAIN_PROMPT` is deleted from the framework layer.
- [ ] `PoolStore.create_pool(name)` seeds `agents/{name}.md` with the
      injected `default_prompt_seed`, not a framework-hardcoded string.
- [ ] The bot-layer wiring (`web_ui_service.py` or equivalent) passes
      `PromptStore.DEFAULT_PROMPT_SEED` into both `PromptStore` and
      `PoolStore` at construction time.
- [ ] `PromptStore.DEFAULT_PROMPT_SEED` is the single canonical default
      prompt text (no duplicate in the framework layer).
- [ ] Existing framework unit tests for `PoolStore` pass unchanged (the
      `default_prompt_seed` parameter has a default).
- [ ] Existing `read_pool` / `write_pool` round-trip tests pass on legacy
      configs (no `prompt_name` key in YAML → field is `None`; round-trip
      does not add `prompt_name: null` to the YAML output).
- [x] `ruff check` and `mypy` pass on the touched files.

## Prompts tab: list + view (read-only)

**What to build:** Add a new "Prompts" tab in the SettingsView sidebar
(same level as Pools, MCP, Skills) where the user can see a list of all
existing prompts (every `agents/*.md` file) and click one to view its full
content in a read-only editor pane. This is the first user-visible surface
for prompts as a first-class concept. The list comes from a new
`GET /api/prompts` endpoint; the content comes from a new
`GET /api/prompts/{name}` endpoint. A new `PromptSummary` wire model
(`{name, size_bytes, mtime}`) carries the list metadata. The existing
`PromptEditor.tsx` component is reused with widened props (`{promptName}`
instead of `{pool, agent}`), though in this ticket it is rendered
read-only (the Save button and edit capability land in the next ticket).
The `settings.nav.prompts` and `settings.prompts.*` i18n keys are added.
An empty-state message renders when no prompts exist.

**Blocked by:** None — can start immediately (parallel with the schema
ticket; the list/read endpoints do not depend on the `prompt_name` field).
**Status:** ✅ COMPLETED (commit `1d0784b6`)

- [x] `GET /api/prompts` returns `200` with a list of `PromptSummary`
      objects (`{name: str, size_bytes: int, mtime: str}`), sorted
      alphabetically by name, sourced from globbing `agents/*.md`.
- [ ] `GET /api/prompts` excludes non-`.md` files (e.g. `AGENTS.md`).
- [ ] `GET /api/prompts/{name}` returns `200` with `{name: str, content:
      str}`; returns `404` if the file does not exist (does NOT seed —
      seeding is a legacy `read_or_seed_prompt` behavior scoped to the
      fallback path).
- [ ] `GET /api/prompts/{name}` validates the name against
      `^[a-z][a-z0-9_-]+$`; rejects invalid names with 400/422.
- [ ] `PromptStore.list_prompts()` is added — globs `agents/*.md`, returns
      name + size + mtime for each, sorted alphabetically.
- [ ] The `PromptSummary` wire model is added (frozen Pydantic,
      `extra="forbid"`).
- [ ] `lib/promptsApi.ts` (or equivalent) is added with `listPrompts()`
      and `getPrompt(name)` hitting the new endpoints.
- [ ] `PromptsView.tsx` is added under `components/settings/` — two-pane
      layout (list on left, editor on right), mirroring `GlobalSkillsView`
      patterns.
- [ ] `SettingsView` gains the `"prompts"` entry in `ViewKey`,
      `VALID_TABS`, `POOLS_GROUP`, and the `CATEGORY` metadata record
      (icon + catVar + i18n keys).
- [ ] The view routing branch renders `<PromptsView />` when
      `view === "prompts"`.
- [ ] `PromptEditor.tsx` props are widened from `{pool, agent}` to
      `{promptName}`; it loads via `getPrompt(promptName)`.
- [ ] In this ticket, the editor in `PromptsView` is read-only (Save
      button and edit capability land in the next ticket).
- [ ] An empty-state message renders when `GET /api/prompts` returns `[]`.
- [ ] `settings.nav.prompts` and `settings.prompts.*` i18n keys are added
      to the locale files.
- [ ] New vitest tests in `PromptsView.test.tsx` cover: list rendering,
      prompt selection loads content, empty state.
- [ ] New REST tests in `test_prompts_routes.py` (or extension of
      `test_pool_routes.py`) cover: list returns correct files, read
      returns content, 404 for missing, name validation.
- [ ] `ruff check` and `mypy` pass on the touched files.

## Edit + create prompt

**What to build:** Let the user edit a prompt's content in the Prompts tab
and save it explicitly (Save button, not auto-save), and create a new
prompt by clicking "New prompt" and providing a name. Editing a shared
prompt and saving it updates the single underlying `agents/<name>.md`
file, so all referencing agents pick up the change on next restart (the
restart-required toast is shown). Creating a new prompt validates the name
against the agent-name convention, rejects names that already exist, and
seeds the content with the default prompt. The `PUT /api/prompts/{name}`
endpoint is upsert (creates if absent) and sets the `restart_required`
dirty marker on the `prompt` class. The `POST /api/prompts` endpoint
validates the name, rejects duplicates with 409, and optionally accepts
content (defaults to the canonical default seed).

**Blocked by:** Prompts tab: list + view (read-only) (needs the list,
the read endpoint, the `PromptsView` shell, and the widened
`PromptEditor`).

- [ ] `PUT /api/prompts/{name}` with body `{content: str}` returns `200`
      with `{name, content}`; creates the file if absent (upsert);
      validates name; sets `restart_required` on the `prompt` class.
- [ ] `POST /api/prompts` with body `{name: str, content?: str}` returns
      `201` with `{name, content}`; validates name against
      `^[a-z][a-z0-9_-]+$`; rejects existing name with 409; `content`
      defaults to `PromptStore.DEFAULT_PROMPT_SEED` when omitted.
- [ ] `POST /api/prompts` with an invalid name (uppercase, starts with
      digit, contains `.` or `/`) returns 400/422.
- [ ] `PromptStore.create_prompt(name, content)` is added — validates
      name, rejects existing, atomically writes the file.
- [ ] `PromptStore.write_prompt(name, content)` is used by `PUT` (existing
      method, possibly reused or lightly adapted).
- [ ] The `PromptEditor` in `PromptsView` becomes editable: a textarea
      with a Save button; Save calls `PUT /api/prompts/{name}`.
- [ ] A "New prompt" button in `PromptsView` opens a name-entry dialog;
      submit calls `POST /api/prompts` and selects the new prompt.
- [ ] After save, a restart-required toast is shown (the running agent
      picks up the change on next process restart).
- [ ] After create, the new prompt appears immediately in the list and
      is selected.
- [ ] The editor preserves trailing newline and UTF-8 encoding.
- [ ] New vitest tests cover: Save calls `PUT`; "New prompt" flow calls
      `POST` and selects the result; 409 on duplicate name shows an error.
- [ ] New REST tests cover: `PUT` creates and updates; `POST` creates
      and rejects duplicates; name validation; `restart_required` flag
      is set after `PUT`.
- [ ] `ruff check` and `mypy` pass on the touched files.

## Delete prompt with cross-pool reference check

**What to build:** Let the user delete a prompt that no agent references,
and block deletion (with a clear dialog listing every referencing pool
and agent) when the prompt is in use. The reference check scans every
pool's `main.prompt_name`, every subagent's `prompt_name`, and — for
backward compatibility — every agent whose `prompt_name` is empty but
whose `agent_name` equals the prompt name (the fallback case). The
`DELETE /api/prompts/{name}` endpoint returns 409 with a structured
usage list (`[{pool, agent_kind, agent_name}, ...]`) when referenced, or
200 and removes the file when unreferenced. The frontend renders the
usage list in a dialog so the user understands exactly what is blocking
deletion before rewiring those references.

**Blocked by:** Schema foundation: `prompt_name` field + default prompt
consolidation (the reference check reads `prompt_name` from every pool
spec — needs the field to exist); Prompts tab: list + view (read-only)
(needs the list and the `PromptsView` shell to host the delete button).

- [ ] `PoolConfigController.find_prompt_usages(prompt_name)` is added —
      returns `list[PromptUsage]` where `PromptUsage` is
      `{pool: str, agent_kind: Literal["main", "subagent"], agent_name: str}`.
- [ ] The reference check covers: `main.prompt_name` explicit match;
      subagent `prompt_name` explicit match; fallback case where
      `prompt_name` is empty and `agent_name` equals the prompt name.
- [ ] The reference check scans ALL pools (calls `list_pools()` then
      `read_pool(name)` for each).
- [ ] `DELETE /api/prompts/{name}` returns `200` with `{deleted: str}`
      when unreferenced; removes the file.
- [ ] `DELETE /api/prompts/{name}` returns `409` with
      `{error: "in_use", usages: [...]}` when referenced; does NOT remove
      the file.
- [ ] `DELETE /api/prompts/{name}` returns `404` when the file does not
      exist.
- [ ] `PromptStore.delete_prompt(name)` is added — validates name,
      removes the file, raises `UnknownPromptError` if absent. Does NOT
      perform reference checking (that is the controller's job).
- [ ] The `PromptUsage` wire model is added (frozen Pydantic,
      `extra="forbid"`).
- [ ] A delete button is added to each prompt item in `PromptsView`; a
      confirmation dialog precedes the call.
- [ ] On 409, a dialog renders the usage list (pool name, agent kind,
      agent name for each usage).
- [ ] On successful delete, the prompt disappears from the list
      immediately.
- [ ] New vitest tests cover: delete button calls `DELETE`; 409 renders
      the usage dialog; successful delete removes the item from the list.
- [ ] New REST tests cover: unreferenced delete returns 200 and removes
      file; referenced-by-main delete returns 409 with correct usage;
      referenced-by-subagent delete returns 409; fallback reference
      (empty `prompt_name`, matching `agent_name`) returns 409; multi-pool
      reference returns 409 with all usages.
- [ ] New controller unit tests cover: `find_prompt_usages` returns
      empty / main / subagent / fallback / multi-pool cases correctly.
- [ ] `ruff check` and `mypy` pass on the touched files.

## PoolEditor: replace inline prompt editing with prompt selector

**What to build:** Remove the "Edit system prompt" button, the
`promptTarget` state, and the slide-over `<PromptEditor>` block from
`PoolEditor.tsx`. In their place, each main agent card and subagent card
gets a prompt selector dropdown populated from `GET /api/prompts`. The
selector binds to `form.main.prompt_name` /
`form.subagents[i].prompt_name`. A "none" option represents the fallback
(uses `agents/<agent_name>.md`). A "Manage prompts" link next to the
selector jumps to the Prompts tab. Selecting a prompt persists the
`prompt_name` field to the pool config on save. This ticket also removes
the frontend's calls to the old `getPrompt(pool, agent)` / `savePrompt(pool,
agent)` API client functions (they are replaced by the new
`/api/prompts`-based client from the Prompts-tab ticket), preparing the
ground for the old endpoints to be deleted in the cleanup ticket.

**Blocked by:** Schema foundation: `prompt_name` field + default prompt
consolidation (the selector binds to the `prompt_name` field, which must
exist on the spec); Prompts tab: list + view (read-only) (the selector
options come from `GET /api/prompts`, and the "Manage prompts" link
needs the Prompts tab to exist as a navigation target).

- [ ] The "Edit system prompt" button is removed from `MainAgentFields`
      in `PoolEditor.tsx`.
- [ ] The "Edit system prompt" button is removed from `SubagentCard` in
      `PoolEditor.tsx`.
- [ ] The `promptTarget` state and the slide-over `<PromptEditor>` block
      are removed from `PoolEditor.tsx`.
- [ ] The `onEditPrompt` props on `MainAgentFields` and `SubagentCard`
      are removed.
- [ ] A prompt selector dropdown is added to `MainAgentFields`, bound to
      `form.main.prompt_name`.
- [ ] A prompt selector dropdown is added to `SubagentCard`, bound to
      `form.subagents[i].prompt_name`.
- [ ] The dropdown options come from `GET /api/prompts` (via
      `listPrompts()`).
- [ ] A "none" option represents the fallback (uses `agent_name`); the
      selector shows "none" when `prompt_name` is empty.
- [ ] The selector shows the currently selected prompt name (or "none").
- [ ] Selecting a prompt updates the form state; the `prompt_name` is
      persisted to `pool.yml` on Save (via the existing `PUT /api/pools`
      path — the spec already carries the field).
- [ ] A "Manage prompts" link next to the selector calls
      `onNavigateToPrompts` (a new prop that switches `SettingsView` to
      the prompts tab).
- [ ] The frontend no longer calls `getPrompt(pool, agent)` or
      `savePrompt(pool, agent)` from `poolApi.ts` (the old pool-scoped
      client functions are unused after this ticket).
- [ ] Existing `PoolEditor` vitest tests are updated: the "Edit prompt"
      button assertions are gone; new assertions cover the selector
      dropdown, the "none" option, and the "Manage prompts" link.
- [ ] `ruff check` and `mypy` pass on the touched files.

## Cleanup: delete old prompt endpoints + remove pool-deletion md cascade

**What to build:** Delete the two old
`GET/PUT /api/pools/{pool}/agents/{agent}/prompt` REST endpoints entirely
(they return 404 after this ticket — no deprecation alias, because the
`pool` segment was never functional and the only consumer, the WebUI
frontend, was migrated in the PoolEditor-selector ticket). Remove the
`agents/{main_agent_name}.md` deletion step from `PoolStore.delete_pool`
— prompts are now independent resources that survive pool deletion; the
reference check on `DELETE /api/prompts/{name}` (from the delete-prompt
ticket) is the single source of truth for "is this prompt safe to
remove". This fixes the cross-pool shared-prompt destruction bug
inherited from the Config UX Overhaul predecessor work. Also delete the
now-unused `getPrompt` / `savePrompt` pool-scoped client functions from
`poolApi.ts`.

**Blocked by:** PoolEditor: replace inline prompt editing with prompt
selector (the frontend must no longer call the old endpoints before they
can be removed); Delete prompt with cross-pool reference check (the
reference check must be in place as the replacement safety net before
the md cascade is removed from pool deletion).

- [ ] `GET /api/pools/{pool}/agents/{agent}/prompt` is removed from
      `server.py` route registration; the handler is deleted.
- [ ] `PUT /api/pools/{pool}/agents/{agent}/prompt` is removed from
      `server.py` route registration; the handler is deleted.
- [ ] The old endpoints return 404 (route not registered).
- [ ] `PoolStore.delete_pool` no longer removes
      `agents/{main_agent_name}.md` — the md-cascade step is deleted.
- [ ] After deleting a pool, `agents/{main_agent_name}.md` still exists
      on disk (the prompt survives).
- [ ] The other cascade steps in `PoolStore.delete_pool` (skills,
      routing, transcripts, runtime state, memory, experiences, inbox,
      session index — from the Config UX Overhaul predecessor) are
      unchanged.
- [ ] `PoolConfigController.read_prompt` and `write_prompt` (the
      controller methods that delegated to the old endpoints) are
      deleted if no longer called by any handler; otherwise kept if
      they have internal callers.
- [ ] `getPrompt` and `savePrompt` (the pool-scoped versions) are
      deleted from `lib/poolApi.ts`.
- [ ] Existing REST tests for the old endpoints are deleted (the
      endpoints no longer exist).
- [ ] New REST test: `DELETE /api/pools/{name}` leaves
      `agents/{main_agent_name}.md` on disk.
- [ ] New REST test: a prompt shared between two pools (same name)
      survives deletion of one pool and remains usable by the other.
- [ ] Existing pool CRUD tests pass (no regression from the cascade
      removal).
- [ ] `ruff check` and `mypy` pass on the touched files.

---

## Dependency graph

```
Ticket 1 (schema) ──┬──→ Ticket 4 (delete) ──┐
                     └──→ Ticket 5 (selector) ─┴──→ Ticket 6 (cleanup)
Ticket 2 (list+view) ──┬──→ Ticket 3 (edit+create)
                       ├──→ Ticket 4 (delete)
                       └──→ Ticket 5 (selector)
```

- **Ticket 1** (schema) and **Ticket 2** (list+view) can start in
  parallel — neither depends on the other.
- **Ticket 3** (edit+create) depends on Ticket 2 (needs the list/view
  shell).
- **Ticket 4** (delete) depends on Ticket 1 (needs `prompt_name` for the
  reference check) and Ticket 2 (needs the `PromptsView` shell).
- **Ticket 5** (selector) depends on Ticket 1 (needs `prompt_name` on the
  spec) and Ticket 2 (needs `/api/prompts` list endpoint).
- **Ticket 6** (cleanup) depends on Ticket 5 (frontend must no longer
  call old endpoints) and Ticket 4 (reference check must be in place as
  the replacement safety net for the removed md cascade).

---

## Known pre-existing issue (NOT introduced by this feature)

### ADR-0006 violation: core runtime-imports memory / runtime / workspace

`tests/architecture/test_dependency_tree.py::test_core_no_unexpected_runtime_upward_imports`
fails on the `develop_gyt` branch. This failure **predates the
prompt-configuration feature** — verified by `git stash` on the
prompt-configuration working tree: the test fails identically with all
prompt-configuration changes reverted. The base commit `2b4bd098a` (the
parent of Ticket 1) already fails.

**Root cause:** introduced by the SQLite persistence refactor (commit
`5ef3ee7a`). Two files under `src/modex_agent/core/` runtime-import
tier-2+ modules, violating ADR-0006's dependency tiering:

- `src/modex_agent/core/cleanup.py` imports:
  - `modex_agent.memory.stores.utils.sanitize_scope_key`
  - `modex_agent.runtime.store.JsonFileTodoStore` / `JsonFileTurnStateStore`
  - `modex_agent.workspace.paths.WorkspacePaths` / `safe_segment`
- `src/modex_agent/core/session_scope_discovery.py` imports:
  - `modex_agent.workspace.paths.SUBDIR_MEMORY` / `WorkspacePaths`

Both files carry a `TODO(adr-0006)` comment block documenting the
violation, the failing test, the root-cause commit, and the fix
direction.

**Fix direction (separate ticket, NOT in this feature's scope):**
dependency inversion — move the consumed surfaces
(`sanitize_scope_key`, `JsonFileTodoStore` / `JsonFileTurnStateStore`,
`WorkspacePaths` / `safe_segment` / `SUBDIR_MEMORY`) down into `core`,
or relocate `cleanup.py` and `session_scope_discovery.py` out of
`core` (they are `SessionArtifactCleaner` implementation details, not
core ABCs). Open a new design doc under `docs/design/` (e.g.
`core-upward-imports-cleanup/`) and run `/to-spec` → `/to-tickets` →
implement on a dedicated branch.

**Do NOT silence** by adding these three modules to `EXPECTED_OFFENDERS`
in `test_dependency_tree.py` without a follow-up ticket — the
`EXPECTED_OFFENDERS` set is intentionally empty by design (the comment
says "This set shrinks to empty as fixes land; the assertion stays
strict"), and adding entries without a plan would bury the debt.
