# Prompt Configuration Independence: Top-Level Prompt Library + Pool Selection Linkage

Status: ready-for-agent

Related: Predecessor PRD `docs/design/config-ux-overhaul/PRD.md` (Config UX
Overhaul — pool deletion cascade, rename removal, zero-pool support; the
prompt-md cascade-delete bug identified there is solved here); ADR-0020
(`docs/adr/0020-pool-config-convergence-and-framework-promotion.md` —
`PoolStore` / `PoolSpec` / `MainAgentSpec` / `SubagentSpec` live in the
framework; the new `prompt_name` field is an additive change to these
framework types); `CONTEXT.md` → "Pool", "Main Agent", "Subagent",
"Pool Instance"; `examples/bot_project/CONTEXT.md` → "Session Record",
"Session Artifacts", "Cascade".

## Problem Statement

A bot maintainer who wants to manage agent system prompts — view them,
edit them, create new ones, delete unused ones, share one prompt across
multiple agents — has no first-class surface to do any of this. Prompts
are a side-effect of agents, accessed through the Pools editor, and the
prompt's identity is implicitly coupled to the agent's name. Every
configuration lifecycle operation on prompts is either missing, broken,
or reached through a misleading path.

**Prompts are buried inside the Pools editor.** To edit a prompt, the
maintainer must enter the Pools tab, select a pool, find the agent card,
and click "Edit system prompt" — which opens a slide-over editor scoped
to that specific pool+agent pair. There is no way to see all prompts at
a glance, no way to create a prompt without first creating an agent that
references it, and no way to delete a prompt that is no longer used. The
prompt has no independent existence in the UI.

**The prompt's identity is the agent's name, with no explicit reference
field.** `MainAgentSpec` and `SubagentSpec` carry no `prompt_name` /
`prompt_id` / `prompt_ref` field. The runtime resolves the prompt file
purely by convention: `agent_name = "coder"` → read `agents/coder.md`.
This means renaming an agent renames its prompt file (because the
`PoolStore` cascages md renames as a side-effect of agent rename), and
two agents with the same name in different pools silently share one
prompt file with no visible coupling. The maintainer cannot point an
agent at a differently-named prompt, cannot share one prompt across two
differently-named agents, and cannot see which prompts are referenced
where.

**The REST API path lies about its semantics.** The prompt endpoints are
`GET/PUT /api/pools/{pool}/agents/{agent}/prompt`, but the backend
handler extracts only `agent` from the URL and never passes `pool` to
the prompt store. The `pool` segment is vestigial — prompts are already
a flat global namespace keyed by agent name. The URL suggests
pool-scoping that does not exist, which is misleading to any API
consumer and blocks a clean "list all prompts" endpoint.

**Deleting a pool can destroy a prompt that another pool still uses.**
The predecessor work (Config UX Overhaul) added a cascade-delete to
`PoolStore.delete_pool` that removes `agents/{main_agent_name}.md` as
part of pool deletion. But because prompts are keyed by agent name
(implicitly shared across pools when names collide), deleting one pool
can delete a prompt file that another pool's agent — with the same name
— is still actively using. The maintainer has no warning, no reference
check, and no recovery path. This is a known bug inherited from the
predecessor work, called out there as "a potential bug... new design
should solve."

**There is no list / create / delete API for prompts.** `PromptStore`
exposes only `read_prompt` / `write_prompt` / `prompt_exists` /
`read_or_seed_prompt`. There is no `list_prompts`, no `create_prompt`,
no `delete_prompt`. The only way to discover the set of prompts is to
glob the `agents/` directory; the only way to create one is to
implicitly seed it via `read_or_seed_prompt` on first read; the only
way to delete one is to delete the pool whose agent happens to share
the name (with the cascade bug above).

## Solution

Promote prompts from a side-effect of agents to a first-class top-level
configuration concept — same level as Pools, MCP, and Skills — with an
explicit reference field that decouples prompt identity from agent
identity. The fix is strictly additive on the data model (one optional
field) and strictly subtractive on the REST surface (delete the
misleading pool-scoped endpoints, add clean prompt-scoped ones). No
data migration is required.

**1. A new `prompt_name` field on agent specs, with a fallback chain.**
`MainAgentSpec` and `SubagentSpec` each gain `prompt_name: str | None =
None`. The runtime resolves the system prompt by a three-step priority:
(a) if `prompt_name` is non-empty, read `agents/<prompt_name>.md`; if
that file is absent, use the hardcoded default; (b) if `prompt_name` is
empty, fall back to `agents/<agent_name>.md` (backward compatibility —
existing configs with no `prompt_name` behave identically); (c) if that
file is also absent, use the hardcoded default. This preserves the
existing agent-name convention as a fallback while allowing explicit
decoupling.

**2. A top-level Prompts tab in SettingsView.** A new `"prompts"` view
key joins the existing `"pools" | "mcp" | "skills"` group. The new
`PromptsView` component renders a two-pane layout (prompt list on the
left, editor on the right), mirroring the existing `GlobalSkillsView`
pattern — the closest UI analog (a global library with per-agent
assignment). The existing `PromptEditor` component is reused, with its
props widened from `{pool, agent}` to `{promptName}`.

**3. A clean `/api/prompts` REST series; the old pool-scoped prompt
endpoints are deleted.** Five new endpoints — `GET /api/prompts` (list),
`POST /api/prompts` (create), `GET /api/prompts/{name}` (read),
`PUT /api/prompts/{name}` (write), `DELETE /api/prompts/{name}` (delete
with reference check) — replace the two old
`GET/PUT /api/pools/{pool}/agents/{agent}/prompt` endpoints. The old
endpoints are removed entirely (no deprecation alias) because their
`pool` segment was never functional and the only consumer is the WebUI
frontend, which is updated in the same change.

**4. Prompt deletion is guarded by a cross-pool reference check.**
`DELETE /api/prompts/{name}` calls a new
`PoolConfigController.find_prompt_usages(name)` that scans every pool's
`main.prompt_name`, every subagent's `prompt_name`, and — for backward
compatibility — every agent whose `prompt_name` is empty but whose
`agent_name` equals `{name}` (the fallback case). If any reference is
found, the endpoint returns 409 with a structured usage list
(`[{pool, agent_kind, agent_name}, ...]`). The frontend renders this
list in a dialog that explains which pools depend on the prompt. If no
references are found, the prompt md file is deleted.

**5. Pool deletion no longer cascades to prompt md files.** The
`PoolStore.delete_pool` cascade that removes `agents/{main_agent}.md`
is deleted. Prompts are independent resources; a pool deletion removes
only the pool's config directory (`config/pools/{name}/`). If a prompt
becomes orphaned (no pool references it), the maintainer deletes it
manually from the Prompts tab — with the reference check ensuring it
is genuinely unreferenced. This fixes the cross-pool shared-prompt
destruction bug inherited from the predecessor work.

**6. The PoolEditor replaces inline prompt editing with a prompt
selector.** The "Edit system prompt" button in `MainAgentFields` and
`SubagentCard` is removed, along with the `promptTarget` state and the
slide-over block. In its place, each agent card gets a prompt selector
dropdown populated from `GET /api/prompts`. The selector binds to
`form.main.prompt_name` / `form.subagents[i].prompt_name`. A "none"
option represents the fallback (uses `agent_name`). A "Manage prompts"
link jumps to the Prompts tab. Editing a prompt's content happens
exclusively in the Prompts tab — the Pools editor only selects which
prompt an agent references.

**7. The default prompt text is consolidated and injected.** The
predecessor work left two near-identical hardcoded default prompt
strings — one in the framework (`PoolStore._DEFAULT_MAIN_PROMPT`) and
one in the bot layer (`PromptStore.DEFAULT_PROMPT_SEED`). These are
consolidated into a single source owned by the bot layer
(`PromptStore`), and the framework's `PoolStore` receives it as an
injection parameter (`default_prompt_seed: str`) rather than hardcoding
natural-language prompt text. This respects the framework/bot layer
split: the framework owns the schema and resolution contract; the bot
layer owns the business decision of what the default prompt says.

**8. The `system_prompt_mode` field (REPLACE/APPEND) is unchanged and
orthogonal.** `SubagentSpec.system_prompt_mode` controls how the
subagent's resolved prompt combines with the parent's assembled prompt
at runtime — it is not a prompt reference. It continues to work
exactly as before, applying to whatever prompt the `prompt_name` (or
fallback) resolves to.

## User Stories

### Prompt listing & viewing

1. As a bot maintainer, I want a dedicated "Prompts" tab in the
   SettingsView sidebar (same level as Pools, MCP, Skills), so that I
   can manage prompts as a first-class configuration concept.
2. As a bot maintainer, I want to open the Prompts tab and see a list
   of all existing prompts (every `agents/*.md` file), so that I can
   survey the prompt library at a glance.
3. As a bot maintainer, I want to click a prompt in the list and see
   its full content in an editor pane on the right, so that I can read
   the prompt without leaving the tab.
4. As a bot maintainer, I want the prompt list to show each prompt's
   name, so that I can identify prompts by their human-readable name.
5. As a bot maintainer, I want to see an empty-state message when no
   prompts exist, so that I understand the library is empty rather
   than broken.
6. As a bot maintainer, I want the prompt list to be ordered
   consistently (alphabetical by name), so that I can find prompts
   predictably.

### Prompt creation

7. As a bot maintainer, I want a "New prompt" button in the Prompts
   tab, so that I can create a new prompt independent of any pool or
   agent.
8. As a bot maintainer, I want to provide a name for the new prompt,
   so that it has a human-readable identity I can reference from
   agents.
9. As a bot maintainer, I want the name to be validated against the
   agent-name convention (lowercase letter first, then lowercase
   alnum / underscore / dash), so that the name is filesystem-safe and
   consistent with existing agent naming.
10. As a bot maintainer, I want the create endpoint to reject a name
    that already exists, so that I do not accidentally overwrite an
    existing prompt.
11. As a bot maintainer, I want a newly created prompt to start with
    the default prompt content, so that I have a sensible starting
    point to edit.
12. As a bot maintainer, I want the new prompt to appear immediately
    in the list after creation, so that I can confirm it was saved.

### Prompt editing

13. As a bot maintainer, I want to edit a prompt's content in a
    textarea editor, so that I can refine the system prompt text.
14. As a bot maintainer, I want to save changes explicitly via a Save
    button (not per-keystroke auto-save), so that I control when the
    change is committed.
15. As a bot maintainer, I want a restart-required toast after saving
    a prompt, so that I understand the running agent will pick up the
    change on next process restart.
16. As a bot maintainer, I want to edit a prompt that is shared by
    multiple agents and have all referencing agents pick up the change
    after restart, so that I can update a shared prompt once rather
    than per-agent.
17. As a bot maintainer, I want the editor to preserve the prompt's
    trailing newline and UTF-8 encoding, so that formatting is not
    silently altered.

### Prompt deletion (with reference check)

18. As a bot maintainer, I want to delete a prompt that no agent
    references, so that I can clean up unused prompts from the
    library.
19. As a bot maintainer, I want deletion to require a confirmation
    dialog, so that I do not accidentally delete a prompt.
20. As a bot maintainer, I want the delete endpoint to check all pools
    for references before deleting, so that a referenced prompt is
    never removed.
21. As a bot maintainer, I want the delete endpoint to check both
    `main.prompt_name` and every subagent's `prompt_name`, so that
    subagent references are also caught.
22. As a bot maintainer, I want the delete endpoint to also catch
    fallback references — where an agent's `prompt_name` is empty but
    its `agent_name` equals the prompt name — so that the backward-
    compatibility fallback does not create a deletion hole.
23. As a bot maintainer, I want a referenced-prompt deletion to return
    a 409 with a structured usage list (pool name, agent kind
    "main"/"subagent", agent name), so that the frontend can render a
    helpful dialog.
24. As a bot maintainer, I want the frontend to show a dialog listing
    every pool and agent that references the prompt, so that I
    understand exactly what is blocking deletion before I go rewire
    those references.
25. As a bot maintainer, I want a successfully deleted prompt to
    disappear from the list immediately, so that the UI reflects the
    new state.

### Pool ↔ prompt linkage (selection in PoolEditor)

26. As a bot maintainer, I want the main agent card in the PoolEditor
    to show a prompt selector dropdown instead of an "Edit system
    prompt" button, so that I select a prompt by reference rather
    than editing it inline.
27. As a bot maintainer, I want each subagent card in the PoolEditor
    to show the same prompt selector dropdown, so that subagent
    prompts are also managed by reference.
28. As a bot maintainer, I want the selector dropdown to list all
    prompts from `GET /api/prompts`, so that I can pick from the full
    library.
29. As a bot maintainer, I want the selector to offer a "none"
    option, so that I can fall back to the agent-name convention
    (use `agents/<agent_name>.md`).
30. As a bot maintainer, I want selecting a prompt to save the
    `prompt_name` field to the pool config, so that the reference is
    persisted.
31. As a bot maintainer, I want the selector to show the currently
    selected prompt name (or "none" / fallback indicator), so that I
    can see the current linkage at a glance.
32. As a bot maintainer, I want a "Manage prompts" link next to the
    selector that jumps to the Prompts tab, so that I can edit or
    create a prompt without leaving the flow awkwardly.
33. As a bot maintainer, I want an agent's `agent_name` to no longer
    need to match its prompt's name, so that I can name an agent
    "primary-coder" while pointing it at a prompt named "coder-base".

### Backward compatibility & migration

34. As a bot maintainer with existing pool configs, I want my
    existing `pool.yml` files (which have no `prompt_name` field) to
    continue working without modification, so that I do not need to
    migrate.
35. As a bot maintainer, I want my existing `agents/*.md` files to
    become the initial prompt library automatically, so that no data
    migration is needed.
36. As a bot maintainer, I want pools that previously shared an agent
    name (and thus shared a prompt) to continue sharing that prompt
    after the change, so that the implicit sharing is preserved.
37. As a bot maintainer, I want the `system_prompt_mode` (REPLACE /
    APPEND) field on subagents to continue working exactly as before,
    so that the runtime prompt-combination behavior is unchanged.

### REST API migration

38. As an API consumer, I want `GET /api/prompts` to return the full
    list of prompts with their names, so that I can discover the
    library programmatically.
39. As an API consumer, I want `POST /api/prompts` with a `{name,
    content}` body to create a new prompt, so that I can create
    prompts programmatically.
40. As an API consumer, I want `GET /api/prompts/{name}` to return
    the prompt's content, so that I can read it.
41. As an API consumer, I want `PUT /api/prompts/{name}` with a
    `{content}` body to write the prompt's content, so that I can
    update it.
42. As an API consumer, I want `DELETE /api/prompts/{name}` to delete
    the prompt or return 409 with a usage list if it is referenced,
    so that I can safely remove unused prompts.
43. As an API consumer, I want the old
    `GET/PUT /api/pools/{pool}/agents/{agent}/prompt` endpoints to be
    gone (return 404), so that I am not misled by the vestigial
    pool-scoped path.
44. As an API consumer, I want `POST /api/prompts` and
    `PUT /api/prompts/{name}` to validate the name against the
    agent-name convention, so that malformed names are rejected with
    a 400/422.

### Bug fix: pool deletion no longer destroys shared prompts

45. As a bot maintainer, I want deleting a pool to leave
    `agents/*.md` files untouched, so that a prompt shared with
    another pool survives the deletion.
46. As a bot maintainer, I want orphaned prompts (no pool references
    them) to remain in the library until I explicitly delete them
    from the Prompts tab, so that I can re-reference them later or
    clean them up deliberately.
47. As a bot maintainer, I want the reference check on prompt
    deletion to be the single source of truth for "is this prompt
    safe to delete", so that the pool-deletion cascade and the
    prompt-deletion check do not disagree.

### Default fallback

48. As a bot maintainer, I want a prompt whose `prompt_name` points
    to a non-existent `.md` file to fall back to the hardcoded
    default prompt, so that a missing file does not crash the agent.
49. As a bot maintainer, I want the hardcoded default prompt text to
    come from a single canonical source (not duplicated across
    framework and bot layer), so that there is one place to update
    the default.
50. As a framework architect, I want `PoolStore` to receive the
    default prompt seed as an injection parameter rather than
    hardcoding natural-language text in the framework, so that the
    framework stays free of business-layer content decisions.

## Implementation Decisions

### Schema change: `prompt_name` field

`MainAgentSpec` and `SubagentSpec` each gain a new field:

```
prompt_name: str | None = None
```

The field is optional with a `None` default, so existing `pool.yml`
files that omit the field continue to validate. The field is
serialized to YAML as `prompt_name: <value>` when non-empty; when
`None`, it is omitted from the YAML output (so a round-trip through
`write_pool` → `read_pool` on a legacy config does not add a
`prompt_name: null` line).

The `frozen=True, extra="forbid"` model config is unchanged. The
field is a plain `str | None` — no new enum, no nested model.

### Prompt resolution priority

The runtime resolves an agent's base system prompt by this algorithm
(implemented in the existing `resolve_system_prompt` function, widened
to accept `prompt_name`):

1. If `prompt_name` is non-empty:
   a. Read `agents/<prompt_name>.md`. If it exists, use its content.
   b. If it does not exist, use the hardcoded default prompt seed.
2. If `prompt_name` is empty (None):
   a. Read `agents/<agent_name>.md`. If it exists, use its content.
      (This is the backward-compatibility fallback.)
   b. If it does not exist, use the hardcoded default prompt seed.

The `read_or_seed_prompt` behavior is unchanged for the fallback path
— if `agents/<agent_name>.md` is missing and the controller is reading
via the legacy path, it seeds the file with the default. For the
explicit `prompt_name` path, a missing file does NOT seed (the prompt
was explicitly referenced; silently creating it would be surprising) —
it falls through to the default in-memory.

### REST API contract: `/api/prompts` series

Five new endpoints, all under `/api/prompts`:

- **`GET /api/prompts`** → `200` with `[{name: str, size_bytes: int,
  mtime: str}, ...]` (alphabetical by name). Returns the set of
  `agents/*.md` files.
- **`POST /api/prompts`** with body `{name: str, content: str}` →
  `201` with `{name: str, content: str}`. Validates `name` against
  `^[a-z][a-z0-9_-]+$`; rejects if a file with that name already
  exists (409). `content` is optional — if omitted, the default
  prompt seed is used.
- **`GET /api/prompts/{name}`** → `200` with `{name: str, content:
  str}`. If the file does not exist, returns 404 (does NOT seed —
  seeding is a legacy `read_or_seed_prompt` behavior scoped to the
  fallback path).
- **`PUT /api/prompts/{name}`** with body `{content: str}` → `200`
  with `{name: str, content: str}`. Creates the file if it does not
  exist (so `PUT` is upsert). Validates `name`. Sets the
  `restart_required` dirty marker on the `prompt` class.
- **`DELETE /api/prompts/{name}`** → `200` with `{deleted: str}` on
  success; `409` with `{error: "in_use", usages: [{pool: str,
  agent_kind: "main" | "subagent", agent_name: str}, ...]}` if
  referenced. If the file does not exist, returns 404.

The `PromptContent` wire model (`{name: str, content: str}`) is
unchanged. A new `PromptSummary` wire model (`{name: str, size_bytes:
int, mtime: str}`) is added for the list endpoint.

### Deleted endpoints

The two old endpoints are removed entirely (no deprecation alias):

- `GET /api/pools/{pool}/agents/{agent}/prompt`
- `PUT /api/pools/{pool}/agents/{agent}/prompt`

The `pool` path segment in these endpoints was never functional — the
handler extracted only `agent` and never passed `pool` to the store.
The only consumer is the WebUI frontend, which is updated in the same
change to call the new `/api/prompts` endpoints.

### PromptStore extensions

`PromptStore` gains three new methods:

- **`list_prompts() -> list[PromptSummary]`** — globs `agents/*.md`,
  returns name + size + mtime for each, sorted alphabetically.
- **`create_prompt(name, content) -> PromptContent`** — validates
  name, rejects if exists, atomically writes the file. `content`
  defaults to the canonical default seed if not provided.
- **`delete_prompt(name) -> None`** — validates name, removes the
  file. Raises `UnknownPromptError` if absent. Does NOT perform
  reference checking — that is the controller's responsibility.

The existing `read_prompt` / `write_prompt` / `prompt_exists` /
`read_or_seed_prompt` methods are unchanged. `read_or_seed_prompt`
remains scoped to the fallback path (controller calls it when
resolving via `agent_name`, not when resolving via `prompt_name`).

### Reference check: `find_prompt_usages`

`PoolConfigController` gains a new method:

```
find_prompt_usages(prompt_name: str) -> list[PromptUsage]
```

where `PromptUsage` is `{pool: str, agent_kind: Literal["main",
"subagent"], agent_name: str}`.

The algorithm:
1. Call `list_pools()` to get all pool names.
2. For each pool, call `read_pool(name)` to get the `PoolSpec`.
3. Check `pool.main.prompt_name`:
   - If non-empty and equals `prompt_name` → record
     `{pool: name, agent_kind: "main", agent_name: pool.main.agent_name}`.
   - If empty and `pool.main.agent_name` equals `prompt_name` →
     record (fallback reference).
4. For each subagent in `pool.subagents`, apply the same two checks
   with `agent_kind: "subagent"`.
5. Return the full list.

The `DELETE /api/prompts/{name}` handler calls this method; if the
list is non-empty, returns 409 with the structured list.

### Default prompt consolidation

The two existing hardcoded default prompt strings are consolidated:

- **Deleted:** `PoolStore._DEFAULT_MAIN_PROMPT` (framework layer).
- **Canonical source:** `PromptStore.DEFAULT_PROMPT_SEED` (bot layer).
- **Injection:** `PoolStore.__init__` gains a
  `default_prompt_seed: str` parameter. The bot layer
  (`web_ui_service.py` wiring) passes `PromptStore.DEFAULT_PROMPT_SEED`
  into both `PromptStore` and `PoolStore` at construction time.

This respects the framework/bot layer split: the framework owns the
schema and resolution contract; the bot layer owns the business
decision of what the default prompt says. The framework no longer
hardcodes natural-language prompt text.

### PoolStore.delete_pool behavior change

The cascade step that removes `agents/{main_agent_name}.md` is
deleted from `PoolStore.delete_pool`. Pool deletion now removes only
`config/pools/{name}/` (the pool config directory, including
`pool.yml` and `templates/*.yml`). The prompt md files are
independent resources and survive pool deletion.

This is a behavior change from the predecessor work (Config UX
Overhaul), which added the md cascade. That cascade was identified as
a bug when prompts are shared across pools — the cascade is replaced
by the explicit reference check on `DELETE /api/prompts/{name}`.

The other cascade steps from the predecessor work (skills, routing,
transcripts, runtime state, memory, experiences, inbox, session
index) are unchanged — they clean up pool-scoped artifacts, not
prompt files.

### Frontend: PromptsView component

A new `PromptsView.tsx` component is added under
`components/settings/`. It mirrors `GlobalSkillsView.tsx` (the
closest UI analog — a global library with a list + detail layout):

- Left pane: prompt list from `GET /api/prompts`, with a "New
  prompt" button and per-item delete button.
- Right pane: the `PromptEditor` component (reused, props widened)
  showing the selected prompt's content with a Save button.
- New-prompt flow: a dialog prompts for a name; on submit, calls
  `POST /api/prompts` and selects the new prompt.
- Delete flow: calls `DELETE /api/prompts/{name}`; on 409, renders a
  dialog listing the usages from the response body.

### Frontend: SettingsView tab addition

The `ViewKey` union gains `"prompts"`. The `VALID_TABS` set, the
`POOLS_GROUP` nav entry list, and the `CATEGORY` metadata record each
gain a `"prompts"` entry with an icon, a `catVar`, and i18n keys
(`settings.nav.prompts`, `settings.prompts.*`). The view routing
branch gains `view === "prompts" ? <PromptsView /> : ...`.

### Frontend: PoolEditor selector replacement

In `PoolEditor.tsx`:
- **Removed:** the `promptTarget` state, the `onEditPrompt` props on
  `MainAgentFields` and `SubagentCard`, the "Edit system prompt"
  buttons, the slide-over block rendering `<PromptEditor>`.
- **Added:** a prompt selector dropdown in `MainAgentFields` (bound
  to `form.main.prompt_name`) and in `SubagentCard` (bound to
  `form.subagents[i].prompt_name`). The dropdown options come from
  `GET /api/prompts`. A "none" option represents the fallback. A
  "Manage prompts" link calls `onNavigateToPrompts` (a new prop that
  switches the SettingsView to the prompts tab).

The `PromptEditor.tsx` component is reused in `PromptsView` with
widened props: `{promptName: string}` instead of `{pool: string,
agent: string}`. The API calls change from `getPrompt(pool, agent)`
to `getPrompt(promptName)` (new `/api/prompts/{name}` endpoint).

### Frontend: API client changes

`lib/poolApi.ts` loses `getPrompt` and `savePrompt` (the pool-scoped
versions). A new `lib/promptsApi.ts` (or extension of an existing
module) adds `listPrompts()`, `createPrompt(name, content)`,
`getPrompt(name)`, `savePrompt(name, content)`,
`deletePrompt(name)` — all hitting the new `/api/prompts` endpoints.

### `system_prompt_mode` orthogonality

`SubagentSpec.system_prompt_mode` (REPLACE / APPEND) is unchanged. It
controls how the subagent's resolved prompt combines with the
parent's assembled prompt at runtime — it applies to whatever prompt
the `prompt_name` (or fallback) resolves to. No interaction with the
`prompt_name` field.

### Net code impact (approximate, excluding tests)

- **Added (~120 lines):** `prompt_name` field on two specs,
  `PromptSummary` / `PromptUsage` wire models, three `PromptStore`
  methods, `find_prompt_usages` on the controller, five REST
  handlers, `default_prompt_seed` injection parameter on `PoolStore`,
  `PromptsView.tsx`, prompt selector in `PoolEditor`, new API client
  module, i18n keys.
- **Deleted (~80 lines):** two old REST handlers, `promptTarget`
  state + slide-over block + "Edit prompt" buttons in `PoolEditor`,
  `_DEFAULT_MAIN_PROMPT` constant in framework `PoolStore`, the
  md-cascade step in `PoolStore.delete_pool`, old `getPrompt` /
  `savePrompt` in `poolApi.ts`.
- **Net: roughly +40 lines.** The additions (new view, new endpoints,
  selector) outweigh the deletions because the feature adds a
  first-class UI surface that did not exist.

## Testing Decisions

### Testing philosophy

This is a configuration-management feature — CRUD over prompt
resources plus a reference-linkage change. Tests verify external
contracts (REST responses, disk state after operations, pool-config
round-trips), not internal mechanics. The highest available seam is
preferred for each area. Implementation details (which store method
calls which) are not tested in isolation unless the logic is
non-trivial (the `find_prompt_usages` reference check is non-trivial
and gets its own unit seam).

### Test seam 1: Prompt REST API (highest seam)

**Existing file:** `examples/bot_project/tests/webui/test_pool_routes.py`
(extended), plus a new `test_prompts_routes.py` for the prompt-specific
endpoints.

The REST API is the highest seam — it exercises `PoolConfigController`
→ `PromptStore` → disk in one call, and for delete-with-reference-
check it also exercises `PoolStore.read_pool` across all pools.

Assertions (external behaviour only):
- `GET /api/prompts` returns the list of `agents/*.md` files with
  name + size + mtime, sorted alphabetically.
- `POST /api/prompts` with a valid name and content creates the file
  and returns 201 with `{name, content}`.
- `POST /api/prompts` with a name that already exists returns 409.
- `POST /api/prompts` with an invalid name (uppercase, starts with
  digit, contains `.` or `/`) returns 400/422.
- `GET /api/prompts/{name}` returns the content; 404 if absent.
- `PUT /api/prompts/{name}` creates or updates the file; sets
  `restart_required` on the `prompt` class.
- `DELETE /api/prompts/{name}` on an unreferenced prompt returns 200
  and removes the file.
- `DELETE /api/prompts/{name}` on a prompt referenced by a main agent
  (explicit `prompt_name` match) returns 409 with the usage list
  containing `{pool, agent_kind: "main", agent_name}`.
- `DELETE /api/prompts/{name}` on a prompt referenced by a subagent
  returns 409 with `agent_kind: "subagent"`.
- `DELETE /api/prompts/{name}` on a prompt referenced only via
  fallback (`prompt_name` empty, `agent_name` matches) returns 409.
- `DELETE /api/prompts/{name}` referenced by multiple pools returns
  409 with all usages in the list.
- The old `GET/PUT /api/pools/{pool}/agents/{agent}/prompt` endpoints
  return 404 (removed).

### Test seam 2: Framework specs (schema)

**Existing file:** `tests/unit/multi_agent/pool_config/` (extended).

Assertions:
- `MainAgentSpec` and `SubagentSpec` accept `prompt_name: str | None`
  with default `None`.
- `extra="forbid"` still rejects unknown fields.
- `frozen=True` still prevents mutation.
- A `PoolSpec` round-trip through `model_dump()` → `model_validate()`
  preserves `prompt_name`.
- A legacy `pool.yml` (no `prompt_name` key) loads with `prompt_name
  = None`.

### Test seam 3: PoolStore behavior change

**Existing file:** `tests/unit/multi_agent/pool_config/test_store.py`
(extended).

Assertions:
- `PoolStore.delete_pool(name)` removes `config/pools/{name}/` but
  does NOT remove `agents/{main_agent_name}.md`.
- `PoolStore.__init__` accepts `default_prompt_seed: str` and uses it
  when seeding a new pool's main-agent prompt (via `create_pool`).
- `PoolStore.create_pool(name)` seeds `agents/{name}.md` with the
  injected `default_prompt_seed`, not a framework-hardcoded string.

### Test seam 4: PromptStore extensions

**Existing file:** `examples/bot_project/tests/bot/config/` (new
`test_prompt_store.py` or extended existing).

Assertions:
- `list_prompts()` returns all `agents/*.md` with correct name, size,
  mtime; sorted alphabetically; excludes non-`.md` files (e.g.
  `AGENTS.md`).
- `create_prompt(name, content)` creates the file atomically; rejects
  existing name; validates name format.
- `delete_prompt(name)` removes the file; raises `UnknownPromptError`
  if absent.
- `DEFAULT_PROMPT_SEED` is the single canonical default (no duplicate
  in the framework layer).

### Test seam 5: PoolConfigController reference check

**Existing file:**
`examples/bot_project/tests/bot/service/test_pool_config_controller.py`
(extended or new).

Assertions:
- `find_prompt_usages(name)` returns an empty list when no pool
  references the name (neither via `prompt_name` nor via fallback
  `agent_name`).
- Returns main-agent usage when `pool.main.prompt_name == name`.
- Returns subagent usage when `subagent.prompt_name == name`.
- Returns fallback usage when `agent.prompt_name` is empty and
  `agent.agent_name == name`.
- Returns usages from multiple pools when more than one pool
  references the name.
- The returned `PromptUsage` records have the correct `pool`,
  `agent_kind`, and `agent_name` fields.

### Test seam 6: Frontend components (vitest)

**New file:** `webui/src/components/settings/PromptsView.test.tsx`

Assertions:
- Renders the prompt list from `GET /api/prompts`.
- "New prompt" button opens a name-entry dialog; submit calls
  `POST /api/prompts` and selects the new prompt.
- Delete button calls `DELETE /api/prompts/{name}`; on 409, renders a
  dialog listing the usages.
- Selecting a prompt loads its content into the editor.
- Save button calls `PUT /api/prompts/{name}`.

**Existing file:** `webui/src/components/settings/PoolEditor.test.tsx`
(extended).

Assertions:
- The "Edit system prompt" button is gone (no slide-over).
- A prompt selector dropdown is rendered for main agent and each
  subagent.
- The dropdown options come from `GET /api/prompts`.
- Selecting a prompt updates `form.main.prompt_name` /
  `form.subagents[i].prompt_name`.
- "Manage prompts" link calls `onNavigateToPrompts`.

**Existing file:**
`webui/src/components/settings/PromptEditor.test.tsx` (adapted).

Assertions:
- Props are `{promptName}` instead of `{pool, agent}`.
- Loads via `getPrompt(promptName)`; saves via
  `savePrompt(promptName, content)`.

### What NOT to test

- Do not test the internal call order of `find_prompt_usages` — it is
  covered by the REST-level 409 assertions in seam 1.
- Do not test `resolve_system_prompt` in isolation beyond what seam 2
  (schema) and seam 1 (REST round-trip) already cover — the
  resolution priority is a pure function whose inputs are the spec
  fields and the disk state; the REST and schema seams cover both.
- Do not test the `PoolStore.delete_pool` md-cascade removal in
  isolation beyond seam 3 — the REST-level "prompt survives pool
  deletion" assertion in seam 1 covers it transitively.
- Do not test adapter or runtime prompt-assembly behavior — the
  `SystemPromptPipeline` and `SystemPromptProvider` chain are
  unchanged; only the base-section source resolution changes, and
  that is covered by the schema + REST seams.

### Prior art

- `examples/bot_project/tests/webui/test_pool_routes.py` — REST-level
  pool CRUD patterns; the existing prompt read/write tests are the
  direct extension point for the new `/api/prompts` endpoints.
- `examples/bot_project/tests/webui/test_skills_routes.py` (or
  equivalent) — the Skills REST API is the closest analog for a
  global-library CRUD pattern with per-agent assignment.
- `tests/unit/multi_agent/pool_config/test_store.py` — `PoolStore`
  unit test patterns; the existing `test_delete_pool` is the direct
  extension point for the md-cascade-removal assertion.
- `webui/src/components/settings/GlobalSkillsView.test.tsx` (if it
  exists) — the closest UI analog for a global-library + list/detail
  layout.

## Out of Scope

- **Prompt versioning / history.** Atomic writes protect against
  corruption but do not keep history. Undo, diff, or version
  snapshots are a separate effort.
- **Prompt metadata (description, tags, creation time).** The
  `PromptSummary` wire model carries only name + size + mtime. Rich
  metadata is a separate effort.
- **Hot-reload of prompt changes.** Writing a prompt still sets
  `restart_required`; the running agent picks up the change on next
  process restart. Live prompt hot-reload is a separate architectural
  effort (would require wiring a cache-invalidation signal from
  `PoolConfigController` to `BotService._system_prompt_cache`).
- **Prompt import / export.** Bulk import from a file, export to a
  zip, or sharing prompts across workspaces is out of scope.
- **Prompt templates with variables.** The existing
  `PromptRegistry` in `src/modex_agent/memory/prompts/` supports
  `{variable}` substitution for internal framework prompts. Extending
  that to agent system prompts is a separate effort.
- **Per-pool prompt scoping.** Prompts remain a flat global namespace
  (`agents/<name>.md`). Pool-scoped prompt directories are out of
  scope.
- **Migration of existing `pool.yml` to add `prompt_name`.** The
  field is optional with a `None` default; legacy configs work
  unchanged. There is no migration script and none is needed.
- **External consumers of the old prompt endpoints.** The old
  `GET/PUT /api/pools/{pool}/agents/{agent}/prompt` endpoints are
  removed with no deprecation alias. If an external consumer (outside
  the WebUI) depends on them, that consumer must migrate to
  `/api/prompts`. This is acceptable because the old endpoints were
  misleading (the `pool` segment was never functional).
- **Prompt content validation.** The feature does not validate
  prompt text content (no linting, no schema for prompt structure).
  Any content is accepted as long as it is a string.
- **Concurrent prompt editing.** Two browsers editing the same prompt
  simultaneously is last-write-wins (atomic write via `.tmp` +
  `os.replace`). Optimistic locking is out of scope.

## Further Notes

### Implementation order (suggested)

1. **Framework: schema + injection.** Add `prompt_name: str | None =
   None` to `MainAgentSpec` and `SubagentSpec`. Add
   `default_prompt_seed: str` injection parameter to `PoolStore`.
   Delete `PoolStore._DEFAULT_MAIN_PROMPT`. Update the bot-layer
   wiring to pass `PromptStore.DEFAULT_PROMPT_SEED` into both stores.
   This is purely additive — existing configs and tests pass
   unchanged.
2. **Bot layer: PromptStore extensions.** Add `list_prompts`,
   `create_prompt`, `delete_prompt` to `PromptStore`. Consolidate
   `DEFAULT_PROMPT_SEED` as the single canonical default.
3. **Bot layer: reference check.** Add
   `PoolConfigController.find_prompt_usages`. Add the `PromptUsage`
   wire model.
4. **Bot layer: REST endpoints.** Add the five `/api/prompts`
   handlers. Delete the two old
   `/api/pools/{pool}/agents/{agent}/prompt` handlers.
5. **Framework: delete md cascade.** Remove the
   `agents/{main_agent_name}.md` deletion step from
   `PoolStore.delete_pool`.
6. **Frontend: API client.** Add `lib/promptsApi.ts`; remove the old
   `getPrompt` / `savePrompt` from `poolApi.ts`.
7. **Frontend: PromptsView.** Add the new component; add the
   `"prompts"` tab to `SettingsView` (ViewKey, VALID_TABS,
   POOLS_GROUP, CATEGORY).
8. **Frontend: PoolEditor selector.** Remove the inline prompt
   editing (buttons, state, slide-over); add the prompt selector
   dropdowns. Widen `PromptEditor` props.
9. **Frontend: i18n.** Add the `settings.nav.prompts` and
   `settings.prompts.*` keys.
10. **Run all test seams.** Each seam must pass before the next step
    begins.

Steps 1–3 are backend additive (schema, store, controller). Step 4
is the REST surface swap. Step 5 is the framework behavior change
(md cascade removal). Steps 6–9 are frontend. This ordering
front-loads the backend so the frontend always has a stable API to
call.

### ADR-0020 compatibility

`PoolSpec`, `MainAgentSpec`, `SubagentSpec`, and `PoolStore` are
framework types per ADR-0020. The `prompt_name` field addition is
additive to the framework's public surface (optional field, default
`None`, no existing call site breaks). The `default_prompt_seed`
injection parameter on `PoolStore` is also additive (the parameter
has a default at the framework level — the empty string — so
existing framework tests that construct `PoolStore` without the
parameter continue to pass; the bot layer overrides with the real
default). No ADR-0020 type is renamed or moved.

### Relationship to Config UX Overhaul (predecessor)

This PRD directly addresses the prompt-md cascade-delete bug
identified in the predecessor PRD (`docs/design/config-ux-overhaul/
PRD.md`, Problem Statement → "Pool deletion leaves a trail of
orphaned state" and Further Notes). The predecessor added the
md-cascade as part of pool-deletion cleanup; this PRD removes it
because prompts are now independent resources with their own
reference-check guard. The other cascade steps from the predecessor
(skills, routing, transcripts, runtime state, memory, experiences,
inbox, session index) are unchanged — they clean up pool-scoped
artifacts, not prompt files.

### Framework / bot layer split

The default prompt text consolidation respects the framework/bot
layer split documented in the root `AGENTS.md`:

- **Framework owns:** the `prompt_name` field schema, the resolution
  priority contract, the `default_prompt_seed` injection parameter
  (received, not hardcoded).
- **Bot layer owns:** the `PromptStore` (read/write/list/create/
  delete), the canonical `DEFAULT_PROMPT_SEED` text, the
  `PoolConfigController.find_prompt_usages` reference check, the REST
  endpoints, the WebUI.

The framework stays free of natural-language prompt content; the bot
layer makes all business decisions about what the default prompt
says and how prompts are managed.
