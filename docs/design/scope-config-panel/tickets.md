# Tickets: Scope Config Panel

A tracer-bullet breakdown of `docs/design/scope-config-panel/PRD.md` —
backend cleanup first (each step independently verifiable against compiled
assembly goldens), then the structured API, then the frontend panel.
Reference: PRD at `docs/design/scope-config-panel/PRD.md`.

Work the **frontier**: T1 → T2 → T3 → (T4, T5) → T6. T4 and T5 both build
on T3 and can be worked in either order; T5's form actions reuse T4's
components.

## T1 — Remove workspace-level MCP

**What to build:** Delete `WorkspaceSpec.mcp` (spec.py),
`validate_workspace_mcp_set` and `workspace_mcp_prewarm_names`
(declaration.py), and their `core.py` call sites. Boot pre-warm becomes the
full registry name list (the existing undeclared-workspace fallback,
inlined). Add `validate_agent_mcp_sets(scope_spec, registry_names)` in
declaration.py — loud `UnknownMcpServer` on any agent-level `mcp:` name
absent from the registry, warn-and-skip on an empty registry — and wire it
where the workspace check used to run. Delete the workspace `mcp:` block
from `config/scopes/bot.yml`. Grep-sweep for remaining references (tests,
docs, comments).

**Verify:** suite green; new test — agent `mcp: [typo]` aborts boot with
`UnknownMcpServer`; new test — pre-warm covers the full registry when
servers are declared only at agent level; boot smoke with the shipped
config.

## T2 — Strip default-valued declarations and dead config

**What to build:** `config/scopes/bot.yml`: remove workspace
`persistence`/`paths`, all five `context_mode: fresh`, `office-expert`
`max_steps: 100`, the three bare `tools:` keys, and the repeated ticket
comments. `config/bot_config.yml`: remove the `multi_agent.session_retention`
block (values identical to `SessionRetentionConfig` defaults), the
commented `eval:` block, the dormant observability comment block, and the
dead-`workspace.enabled` comment block. Delete
`tests/integration/fixtures/pools/review/pool.yml` and fix the stale
pool.yml-era comments (`peer_resolution.py:132`, `spec.py:106`).

**Verify:** compiled assembly is byte-identical before/after (run the
split-brain/bill goldens; if none pin the full assembly, add one golden
first, then strip) — this is the proof the strip is behavior-neutral;
suite green; boot smoke.

## T3 — Canonical serializer + structured model endpoints

**What to build:** `bot/service/scope_serialize.py` — spec model →
canonical YAML (fixed field order, strip-on-default including
position-derived defaults, `use_terminal`/`terminal_visibility` always
emitted on NATIVE pool roots — external agents carry no terminal face —
`+`/`-` hook prefixes and capability vetoes preserved). Routes in
`bot/webui/routes/scope_routes.py`:
`GET /api/scope/model` (yaml.safe_load of the declaration → JSON tree,
same workspace resolution as the sibling routes), `PUT /api/scope/model`
(JSON tree → canonical YAML → stage → **reuse the existing PUT gate
chain** load → validate → compile → validate-effective → atomic replace;
400 with `issues[]` on failure), `POST /api/scope/preview` (same gate
chain minus the commit → bill-shaped effective view of the DRAFT; powers
the panel's live effective-state display), `GET /api/scope/options`
(toolsets, context modes, execution strategies, provider kinds,
capabilities + **capability_bundles** (carried tools/hooks per capability,
`contribute()` probed across both tree positions), hooks +
position-default hooks, interceptors, commands, MCP registry names, both
position-default rows — all enumerated from the same `ComponentRegistry`
the compile gate uses).

**Verify:** round-trip golden — serialize(shipped declaration) is stable
and idempotent (YAML→model→YAML ≡ YAML); strip goldens (`context_mode:
fresh`/`max_steps: 100` absent, native-root terminal flags present,
external root without them); PUT with an invalid model (unknown
capability, second root, non-root approval) → 400 with rule/node/message;
preview of a draft with a capability toggled shows the bundle-carried
hooks in the returned bill and writes nothing to disk; options endpoint
lists bot-owned components (`model_choice_bind`) alongside framework ones
and carries the todo/tracing/subagents bundles; suite green.

## T4 — Pools config panel v2: tree + effective-state agent form

**What to build:** New `pools` tab in `SettingsView.tsx` (Pools & Agents
group; `scope` tab untouched). Under
`webui/src/components/settings/pools/`: declaration tree (workspace →
pools → agents, selection-driven) + the v2 agent form per PRD Part C:
**C0** every effective section reads the bill (`GET /api/scope/bill` when
clean, debounced `POST /api/scope/preview` when dirty); **C1** capability
bundle rows with tri-state (auto-with-reason / declared / vetoed) +
carried tools/hooks chips; **C2** hooks as an effective roster with origin
badges (默认 / 能力: X / 声明), veto by unchecking defaults AND by vetoing
bundle-carried hooks (vetoed bundle hooks stay visible struck-through with
a restore action; dangling vetoes surface for cleanup), add via
backend-fed combobox (roster − bundle-carried − effective); **C3**
strategy-first runtime block — external agents get the provider panel
(brand icon + provider_kind + "provider owns tools/approval/MCP/prompt"
explanation, native sections replaced, not just hidden); permissions
section (root-only, interceptors same effective-roster treatment,
sandbox_guard backend/write_surface dropdowns, approval switch +
allowed_paths, apply-to-other-pools). Whole-document dirty tracking, Save
→ `PUT /api/scope/model`, issues mapped back to tree nodes with
focus-first-invalid, restart toast. i18n keys (en catalog — the repo ships
no zh catalog; keep keys structured for a future zh). Reuse `ui/`
primitives; visible labels, helper text stating the effective default per
enumeration, focus rings, no emoji icons.

**Verify:** webui build clean; unit tests over the draft→declaration
mapping (veto semantics — default-hook veto AND bundle-hook veto both
write `-name`, restore removes it; tri-state capability writes;
bundle-carried hooks never emitted as `+name`); GUI test
(web-gui-tester) — the subagent of a
pool shows `subagent_auto_send` as effective with a `能力: subagents`
badge WITHOUT any declaration; vetoing a bundle-carried hook writes the
`-name` entry and the preview shows it struck-through; toggling the todo
capability off removes its three hooks from the effective roster in
preview; switching a pool root to external replaces native sections with
the provider panel; save → YAML shows exactly the deviation; tab deep-link
`?tab=pools` works.

## T5 — Structure operations: add/delete, peers sync, apply-to-pools

**What to build:** Tree affordances — add pool, add subagent, delete
node/pool with confirmation dialog. Pool form: peers checkbox group
syncing both sides of the bidirectional edge in the model. Permissions
section: "应用到其他 pool" action copying
`interceptors`/`interceptor_configs`/`approval` onto sibling pool roots.
All client-side model edits saving through the same PUT.

**Verify:** GUI test — add pool with root agent → declaration validates
and saves; delete pool → gone after save; peers toggle produces a V5-clean
declaration (never a one-sided edge); apply-to-pools leaves the three
roots' permission blocks identical.

## T6 — Docs and ADR amendment

**What to build:** Amend ADR-0042 in place (living document): the
declaration gains a structured write face (`/api/scope/model`), canonical
serialization with strip-on-default, workspace-level `mcp` removed with
rationale (validation sank to agent selections; pre-warm derives from the
registry). Update `examples/bot_project/AGENTS.md` (config files table,
settings tabs) and any scope/SPEC cross-references to the deleted
workspace mcp row.

**Verify:** docs index (`docs/AGENTS.md`) links resolve; no dangling
references to `workspace.mcp` / `validate_workspace_mcp_set` anywhere in
`src/`, `examples/`, `docs/`.
