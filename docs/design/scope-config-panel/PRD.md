# Scope Config Panel: Structured Pool/Agent Configuration + Declaration Cleanup

Status: ready-for-agent

Related: ADR-0042 (`docs/adr/0042-scope-declaration-tree.md` — scope
declaration is the sole structural primitive; WebUI writes back via the scope
declaration editor; restart-effective by decision); ADR-0047 (capability
bundles — `capabilities:` override map); ADR-0019 (bidirectional peer
topology); ADR-0043 (topology canvas, 同形不合并);
`docs/design/scope-assembly/` (SPEC §3 — declaration/validation/compile);
`docs/design/config-ux-overhaul/` (pre-scope pool.yml era, orthogonal
concerns — do not merge).

## Problem Statement

Since the scope cutover (2026-08-25, commits `298bbb90` / `d6b88f71`), the
only way to configure pools and agents in the WebUI is a single raw YAML
editor over the whole declaration (`ScopeView.tsx` → `PUT
/api/scope/declaration`). The structured per-pool forms (~4000 lines:
`PoolEditor.tsx`, `PoolsView.tsx`, `AgentMcpSelector.tsx`,
`ExternalMainAgentFields.tsx`) were deleted in the same commit. Every change
— one `max_steps` bump, one capability toggle — requires hand-editing YAML
keys the user must know by name, with no enumeration, no defaults
visibility, and no field-level error mapping. The read-only topology canvas
and provenance bill show *what* the declaration compiles to but offer no
affordance to change it.

The declaration file itself has drifted into noise: values that restate
framework defaults (`context_mode: fresh` ×5, `max_steps: 100`, bare
`tools:` keys, workspace `persistence`/`paths` matching service defaults), a
workspace-level `mcp:` list that attaches no tools, and copy-pasted
sandbox/approval blocks across three pool roots. `bot_config.yml` carries a
`session_retention` block byte-identical to the framework defaults plus
commented-out dead sections.

## Settled Decisions

1. **Workspace-level `mcp` is deleted, not fixed.** It only scopes shared
   connection pre-warm and adds a boot-time name check — both derivable.
   Removal: pre-warm falls back to the full registry (already the
   documented undeclared-workspace behavior; servers outside the set
   connect lazily regardless), and loud name validation moves to the
   per-agent `mcp:` selections, which are the selections that actually
   attach tools. The `WorkspaceSpec.mcp` field is removed outright —
   `extra="forbid"` then rejects the key, surfacing stale declarations at
   load time.

2. **The declaration carries deviations only.** Anything equal to the
   spec/position-derived default is not written. One deliberate exception:
   `use_terminal` / `terminal_visibility` stay explicitly declared on pool
   roots (owner's call — the permission-relevant terminal face must be
   visible in the file, not implicit). Sandbox/approval blocks stay
   per-pool-root declarations (no new workspace-level mechanism); the
   frontend removes the copy-paste burden with an "apply to other pools"
   action instead.

3. **The YAML editor stays; a structured panel is added beside it.** The
   existing `scope` tab (topology canvas + provenance bill + YAML editor)
   is untouched. A new `pools` settings tab provides the user-friendly
   face: dropdowns, checkboxes, and number inputs only — no free-form
   key/value entry. Both write paths converge on the same backend gate
   chain (load → validate → compile → validate-effective → atomic write).

4. **Canonical serialization on structured saves.** A structured save
   regenerates the YAML from the spec model in canonical field order with
   defaults stripped. Hand-written comments do not survive a structured
   save (last writer wins; the YAML tab remains for those who want them).
   This makes "deviations only" a mechanism, not a discipline.

## Design

### Part A — Declaration cleanup

**A1. Remove workspace-level MCP.**

- `src/modex_agent/scope/spec.py`: delete `WorkspaceSpec.mcp`.
- `examples/bot_project/bot/service/pool/declaration.py`: delete
  `validate_workspace_mcp_set` and `workspace_mcp_prewarm_names`; add
  `validate_agent_mcp_sets(scope_spec, registry_names)` — collects every
  agent's `mcp:` selection across all pools, raises `UnknownMcpServer` on
  names absent from the registry, warns-and-skips on an empty registry
  (same degenerate-deployment semantics as the deleted workspace check).
- `examples/bot_project/bot/service/core.py` (boot, ~lines 344-371):
  pre-warm set becomes the full registry name list (the existing
  `workspace_mcp_prewarm_names` fallback branch, inlined); the validation
  call switches to `validate_agent_mcp_sets`.
- `config/scopes/bot.yml`: delete the workspace `mcp:` block.

**A2. Strip default-valued declarations.**

`config/scopes/bot.yml` removals (behavior unchanged — every removed line
restates the value the compiler would derive anyway):

- workspace `persistence: {backend: sqlite}` and `paths: {data_dir_name:
  .modex}` (the file header itself documents these as equal to the shipped
  service defaults);
- `context_mode: fresh` on all five subagents (`AgentSpec` default is
  `FRESH`);
- `max_steps: 100` on `office-expert` (equals the field default);
- bare `tools:` keys on the three pool roots (YAML null ≡ field absent ≡
  defer to position profile);
- historical ticket comments repeated verbatim under each pool (keep one
  short pointer where it earns its place).

`config/bot_config.yml` removals:

- `multi_agent.session_retention` (10/200/86400/1800 — byte-identical to
  `SessionRetentionConfig` defaults);
- the commented-out `eval:` block and dormant observability field comments
  (`checkpoint_per_iteration`, `cassette_*`, `training_*`) — the schema
  fields stay, env overrides keep working;
- the dead-`workspace.enabled` comment block (the history lives in
  ADR-0042 and this PRD).

Repository hygiene: delete the orphaned legacy fixture
`examples/bot_project/tests/integration/fixtures/pools/review/pool.yml`
(zero readers) and refresh the two stale pool.yml-era comments
(`src/modex_agent/multi_agent/communication/peer_resolution.py:132`,
`src/modex_agent/scope/spec.py:106`).

### Part B — Structured scope API

Three endpoints, registered beside the existing scope routes
(`bot/webui/routes/scope_routes.py`). The YAML declaration file remains the
single source of truth; these are read/write faces over it.

**`GET /api/scope/model`** → the declaration as a JSON tree.
Implementation: read `config/scopes/bot.yml`, `yaml.safe_load`, return the
mapping. The nested YAML form *is* the UI's tree model (workspace → pools
→ agents; root-ness derived from nesting depth), so no flatten/un-flatten
translation layer is introduced. Honors the same workspace resolution
(`X-Workspace-Id` / `ws`) as the existing scope routes.

**`PUT /api/scope/model`** → `{saved, restart_required}` or `400 {error,
issues[]}`.
Pipeline: JSON tree → canonical YAML text (serializer below) → stage to
`.tmp` sibling → **the exact gate chain of the existing YAML PUT**
(`load_scope_declaration` → `validate_declaration` → `compile_scope` →
`validate_effective_configs`) → atomic replace. The JSON path adds no
second parse/validate road; it converges onto the existing one by
construction.

**`POST /api/scope/preview`** → the bill-shaped effective view of a DRAFT
model (or `400 {issues[]}`). Runs the same gate chain as the PUT minus the
commit — the panel's effective-state sections track the dirty draft live
(debounced), and validation errors surface before save.

**`GET /api/scope/options`** → the enumeration source for every form
control, so the frontend hardcodes nothing:

| Field | Source |
|---|---|
| `toolsets` | `ToolPreset` members (with per-position default marker) |
| `context_modes` | `ContextMode` members |
| `execution_strategies` | `ComponentRegistry.names(ComponentSlot.EXECUTION_STRATEGY)` |
| `provider_kinds` | `ProviderKind` members |
| `capabilities` | `ComponentRegistry.names(ComponentSlot.CAPABILITY)` |
| `capability_bundles` | per-capability carried tools/hooks — `contribute()` probed across both tree positions at default config (pure per SPEC P1) |
| `hooks` | `ComponentRegistry.names(ComponentSlot.HOOK)` + `POSITION_DEFAULT_HOOKS` |
| `interceptors` | `ComponentRegistry.names(ComponentSlot.INTERCEPTOR)` |
| `commands` | `ComponentRegistry.names(ComponentSlot.COMMAND_HANDLER)` |
| `mcp_servers` | `read_registry()` name list (`config/mcp/registry.json`) |
| `position_defaults` | `defaults_for_position(is_root=…)`, both rows |

**Canonical serializer** (bot-side, e.g.
`bot/service/scope_serialize.py`): spec model → YAML text.

- Fixed field order matching the shipped file's reading order
  (`description, max_steps, use_terminal, terminal_visibility, toolset,
  tools, capabilities, hooks, hook_configs, interceptors,
  interceptor_configs, approval, mcp, execution_strategy, provider_kind,
  context_mode, …, agents`).
- Strip-on-default: a field is emitted only when it differs from its
  effective default — spec field defaults (`max_steps: 100`,
  `context_mode: fresh`) and position-derived defaults (`toolset` None,
  `eager` None) alike.
- Exception (Decision 2): `use_terminal` / `terminal_visibility` are always
  emitted on pool roots; on subagents only when non-default.
- `hooks` keep their `+`/`-` prefixes verbatim; `capabilities: {name:
  False}` vetoes are preserved.
- Output is golden-tested: serializing the shipped declaration yields a
  stable, minimal file; a YAML→model→YAML round trip is idempotent.

### Part C — Pools config panel (frontend, v2 effective-state-driven)

**v1 of this section (flat checkbox grids over declared fields) was reviewed
and rejected** — it presented the *declaration* as if it were the *effective
configuration*. Two observed lies: `subagent_auto_send` rendered unchecked
on subagents while the `subagents` capability auto-applies and carries it
(capability contributions are position-derived, not declaration-derived);
and bundle-carried hooks (`todo_*`, `trace_*`, `experience_review`)
appeared as free-standing toggles although they are not independently
meaningful — enabling the `todo` capability is what brings
`todo_write`/`todo_read` + its three hooks as one bundle (ADR-0047). The
v2 design below replaces it.

New settings tab `pools` in the "Pools & Agents" group
(`SettingsView.tsx`), label from i18n. The `scope` tab is unchanged. New
components under `webui/src/components/settings/pools/`, built from the
existing `ui/` primitives and Tailwind — visual consistency with the
shipped settings tabs beats any imported style. UX bar (per ui-ux-pro-max):
visible labels on every control, helper text under enumerations stating
the effective default, inline error placement near the offending field,
dirty-state guard before switching nodes, save button disabled while
clean/in-flight, restart toast after save (same `restartToast` pattern as
the scope tab), 150–300ms transitions, focus rings, no emoji icons.

**C0 — Show effective state, edit declared deviations.** The panel renders
what WILL take effect (with provenance), never a flat mirror of declared
fields. Three read faces feed every agent form:

- `GET /api/scope/model` — the declaration draft (the edit target;
  deviations only);
- `GET /api/scope/options` — the enumeration source (registries, defaults,
  per-capability bundles);
- the **bill** — per-agent effective tools/hooks/capabilities with origins
  (`framework | profile | local`, `auto | declared | vetoed`), already
  exposed by `GET /api/scope/bill`.

While the draft is dirty the on-disk bill is stale w.r.t. the form, so a
fourth face closes the loop: **`POST /api/scope/preview`** accepts the
draft model, runs the same load → validate → compile chain WITHOUT
writing, and returns the bill-shaped effective view of the draft (or the
validation issues, letting the panel surface errors BEFORE save). Compile
is pure and fast; the preview reuses the PUT gate chain minus the commit.

**C1 — Capabilities are the bundle unit.** The capabilities section is a
list of rows, one per registry capability. Each row shows:

- **State** (from the bill, three-state): `auto` (auto-applied — show WHY,
  e.g. "非根 agent 自动启用"), `declared` (explicitly on), `vetoed`
  (explicitly off), or absent;
- **Bundle contents** as read-only chips: the tools and hooks the
  capability carries (from `options.capability_bundles`, computed
  backend-side from `contribute()` probed across tree positions);
- **Control**: a tri-state — follow auto (absent) / force on (`{}`) /
  force off (`false`). Where a capability never auto-applies, the
  tri-state degrades to a plain on/off checkbox.

**C2 — The hooks section is an effective roster with provenance, not a
toggle grid.** It lists the agent's EFFECTIVE hooks (preview bill), each
with an origin badge:

| Origin | Badge | Affordance |
|---|---|---|
| position default | `默认` | uncheck → writes `-name` veto |
| capability-carried | `能力: todo` | **veto → writes `-name`** (compiler merge-base veto applies to capability contributions exactly as to position defaults); a vetoed bundle hook stays visible struck-through while its capability is enabled (bundle contents minus effective roster, computed from `options.capability_bundles` + bill), with a restore action that removes the veto |
| declared | `声明` | remove → drops the `+name` entry |

A dangling veto (capability off but `-name` still declared) renders as an
inactive entry with a remove affordance — the serializer preserves it, the
panel surfaces it for cleanup.

Adding a hook uses a **dropdown combobox** fed by the backend roster
(`options.hooks`) minus bundle-carried names minus already-effective
names — never a hardcoded list, never a free-text key. The same
effective-roster treatment applies to the (root-only) interceptors face.

**C3 — Strategy-first dual form (native vs external).**
`execution_strategy` is the agent form's TOP control, rendered as a
runtime-first block. `external` shows the provider panel restored from the
deleted `ExternalMainAgentFields.tsx` pattern (`git show d6b88f71^:…`):
provider brand icon + label (`PROVIDER_BRAND_ICONS` presentation assets),
`provider_kind` dropdown, and the explicit principle in helper text —
**the provider CLI owns max steps, terminal, tools, approval, MCP, skills
and the system prompt; the framework declaration carries only identity
(description) and topology (peers/children)**. Every native section
(Tools/Hooks/Permissions/Advanced) is REPLACED by that explanation for
external agents, not merely hidden. `react` shows the native sections.

**C4 — Frontend hardcodes nothing.** Every enumeration (toolsets, context
modes, strategies, provider kinds, capabilities + bundles, hooks,
interceptors, MCP servers, position defaults) comes from
`/api/scope/options`; every effective value from the bill/preview. The
frontend owns only presentation: labels (i18n), icons, layout. The one
deliberate exception is provider brand icons — presentation assets, not
config keys.

**Layout** — two columns inside the settings modal:

- Left: declaration tree (workspace → pools → agents), selection drives
  the form. Node-type affordances: add pool, add subagent, delete
  (confirmation dialog; deleting a pool root deletes the pool).
- Right: the selected node's form.

**Workspace form**: name (read-only after creation); `persistence` /
`paths` as optional overrides with "继承服务配置 （默认）" as the leading
dropdown option — selecting it omits the block.

**Pool form**: peers as a checkbox group over the other pool names;
toggling syncs both sides of the bidirectional edge in the model (V5
topology rule enforced by construction, not by error message).

**Agent form sections** (native agents):

| Section | Content |
|---|---|
| 运行时 | Strategy-first block (C3) — native/external choice + provider panel |
| 基本 | `description` textarea; `max_steps` number input; root only: `use_terminal`, `terminal_visibility` checkboxes (always visible — Decision 2) |
| 能力 | Capability bundle rows (C1) — the PRIMARY composition surface |
| 工具 | `toolset` dropdown ("按位置默认" + presets with the resolved default in helper text); effective tool roster chips from the preview bill (origin-badged, read-only — replacements like `edit ← aci_edit` shown); `mcp` checkbox group over registry servers |
| Hooks | Effective roster with provenance + veto/add combobox (C2) |
| 权限 (pool root only) | `interceptors` effective roster (same treatment as hooks); `sandbox_guard` config expanded when on: `backend` dropdown, `write_surface` dropdown; `approval` enable switch + per-tool `allowed_paths` (workspace-only / custom paths); **"应用到其他 pool"** action copying the whole permissions block onto sibling pool roots (Decision 2) |
| 高级 | `context_mode` dropdown (`fork` reveals `fork_max_messages`); `eager` tri-state (默认/eager/lazy); root only: `memory` archive/core toggles; `prompt_name` dropdown fed by the existing prompts API |

**Save flow**: the whole declaration model is one dirty-tracked document;
every edit re-previews (debounced `POST /api/scope/preview`) so the
effective sections track the draft live; Save → `PUT /api/scope/model` →
on 400, issues map back to tree nodes (`node` field) and scroll/focus the
first invalid field; on success, restart toast. Free-text input exists
only where the value is genuinely unbounded (`description`, custom
approval paths) — everything else is enumeration-driven.

## Non-goals

- Hot reload. Restart-effective stays the contract (ADR-0042 v1 decision);
  the `seam.py` N2 seam stays dormant.
- Editing hook/interceptor/capability *implementations* or arbitrary
  `tool_configs` payloads — open-extension payloads remain YAML-only.
- Restoring era-3 per-agent memory/governance tuning knobs
  (short_term/long_term/dream_engine) — that config surface was removed at
  the framework level; re-adding it is a separate design.
- Touching the `scope` tab, the MCP registry tab, skills, prompts, model,
  or IM tabs.

## Risks / Watch-items

- **Dual write paths.** YAML editor and panel both write the same file;
  canonical serialization means a panel save rewrites hand formatting.
  Accepted (Decision 4); the scope tab's save already warns
  restart-required, and both paths pass identical gates.
- **Registry-dependent options.** `GET /api/scope/options` needs the fully
  populated `ComponentRegistry`; it must resolve through the same registry
  instance the compile gate uses, not a fresh minimal one (the
  `model_choice_bind` lesson — bot-owned components must be present).
- **Golden drift.** A2 changes the shipped declaration; any test pinning
  the compiled assembly (split-brain goldens, bill snapshots) must be
  verified byte-identical after the strip, which is also the proof the
  strip is behavior-neutral.
