# Tickets: Agent Role Descriptors + Role-Contract Provider

A vertical-slice breakdown of the feature spec at
`docs/design/agent-role-descriptors/PRD.md`. Related ADR-0026
(`docs/adr/0026-agent-role-descriptors-and-role-contract-provider.md`).

Work the **frontier**: any ticket whose blockers are all done. For this plan
that means T1 first, then T2 + T4 in parallel, then T3 last.

Dependency graph:

```
T1 (framework: AgentRole + roles field + 透传)
├── T2 (framework: AgentRoleContractProvider)
├── T3 (bot: coder→orchestrator pattern + prompt rewrites) — blocked by T1 AND T2
└── T4 (webui: Roles multi-select dropdown)
```

## T1 — Framework: `AgentRole` enum + `roles` field 透传

**What to build:** The framework gains a new `AgentRole` StrEnum (seven preset
values: `PLANNER`, `IMPLEMENTER`, `REVIEWER`, `SCOUT`, `ORACLE`,
`COORDINATOR`, `COMMUNICATOR`) and a new `roles: list[str]` field (default
`[]`) on three structures: the main-agent wire model, the subagent wire
model, and the runtime `AgentDescriptor`. The透传 chain is: wire model
(`MainAgentSpec` / `SubagentSpec`) → `AgentTemplate.materialize` /
main-agent factory → `AgentDescriptor.roles`. After this ticket a bot can
write `roles: [planner]` in `pool.yml` and the materialized descriptor
carries that value — but no runtime behavior changes yet (pure data layer).
`AgentDescriptor.__eq__` / `__hash__` (if defined) do NOT include `roles`
(roles are metadata, not identity; pool registration dedup is unaffected).

**Blocked by:** None — can start immediately.

- [x] `AgentRole` StrEnum exists in framework constants with the seven preset values; values serialize as plain strings (StrEnum behavior)
- [x] `MainAgentSpec` (or equivalent after ADR-0020 convergence) has `roles: list[str] = []` field
- [x] `SubagentSpec` (or equivalent) has `roles: list[str] = []` field
- [x] `AgentDescriptor` has `roles: list[str] = []` field
- [x] `AgentTemplate.materialize` reads `roles` from `SubagentSpec` and writes it onto the constructed `AgentDescriptor`
- [x] Main-agent factory reads `roles` from `MainAgentSpec` and writes it onto the constructed `AgentDescriptor`
- [x] `AgentDescriptor.__eq__` / `__hash__` do NOT include `roles` (verified by test: two descriptors differing only in `roles` are equal)
- [x] Existing tests still pass (no behavior change for agents without `roles` set)
- [x] New unit test: `SubagentSpec(roles=["planner"])` materializes to `AgentDescriptor(roles=["planner"])` — extends `tests/unit/multi_agent/test_template_materialize.py`
- [x] New unit test: `MainAgentSpec(roles=["coordinator"])` constructs `AgentDescriptor(roles=["coordinator"])` — extends existing main-agent factory tests
- [x] `PoolStore` round-trips `roles` through save → load unchanged (preset values stay as their string values, custom strings preserved verbatim) — extends existing pool_config round-trip tests

## T2 — Framework: `AgentRoleContractProvider`

**What to build:** A new `SystemPromptProvider` implementation that injects
role-specific runtime contracts into the system prompt based on the current
agent's `roles`. For each preset role present in `roles`, the provider
appends a short contract segment:
- `REVIEWER` → contract requiring the final reply to contain
  `<verification status="passed|failed" reason="..."/>`
- `IMPLEMENTER` → contract requiring verification (run tests / lint / build,
  or explain why impossible) after code changes
- `COORDINATOR` → contract describing reviewer's output format and the
  obligation to dispatch the implementer role on reviewer failure
- `PLANNER` / `SCOUT` / `ORACLE` / `COMMUNICATOR` → shorter contracts
  describing their core responsibility

For unrecognized role strings (e.g. `"office-expert"`), the provider
injects nothing and does not error. The provider is wired into the
`SystemPromptPipeline` constructed by `MemorySystemContextManager.load()`
(shared main + subagent system prompt assembly path), positioned after
business providers (`ExperienceProvider`, `SkillProvider`, etc.) so
contract text appears late in the system prompt. The provider's output is
byte-stable across turns for a given `roles` value (no timestamps, no
random content) to preserve prompt cache friendliness.

**Blocked by:** T1 (`AgentRole` enum and `roles` field on `AgentDescriptor`
must exist for the provider to read).

- [x] `AgentRoleContractProvider(SystemPromptProvider)` class exists in the framework's prompt pipeline module
- [x] Provider reads `roles` from the current agent's `AgentDescriptor` (passed via the pipeline construction context, same pattern as other providers)
- [x] For `roles=["reviewer"]`, injected text contains the substring `<verification status="passed|failed"`
- [x] For `roles=["implementer"]`, injected text requires verification after code changes
- [x] For `roles=["coordinator"]`, injected text describes reviewer's output format and the dispatch-on-failure obligation
- [x] For `roles=["planner"]` / `["scout"]` / `["oracle"]` / `["communicator"]`, each gets its shorter contract
- [x] For `roles=["custom_role"]`, no text is injected and no error is raised
- [x] For `roles=["reviewer", "planner"]` (multiple roles), contracts for both are injected
- [x] Provider output is byte-stable across multiple `get_or_refresh()` calls for the same `roles` value (verified by test: two calls return identical strings)
- [x] Provider is registered in `SystemPromptPipeline` after business providers — extends existing pipeline wiring (verify via existing pipeline construction test or new test)
- [x] New unit tests extend `tests/unit/memory/prompt_pipeline/test_providers.py` following the existing `ExperienceProvider` / `RuntimeProvider` / `SkillProvider` test patterns

## T3 — Bot: coder pool → orchestrator pattern + prompt rewrites

**What to build:** The reference bot's `coder` pool is restructured to the
**orchestrator pattern**. `config/pools/coder/pool.yml` changes:
`main_agent_name` from `coder` to `orchestrator`; `delegate` and
`context-builder` subagent entries removed; each remaining subagent gains a
`roles:` field (`planner`/`worker`→`implementer`/`reviewer`/`scout`/`oracle`).

`agents/coder.md` is renamed to `agents/orchestrator.md` and rewritten:
- Identity: "You are the Orchestrator, responsible for planning,
  dispatching, and integrating subagent work."
- 5-step orchestration decision tree:
  1. Does the task involve code/file modification? No → answer directly.
     Yes → step 2.
  2. Is the task well-specified? No → dispatch `planner` first, wait for
     plan. Yes → step 3.
  3. Is codebase context clear to the implementer? No → dispatch `scout`
     first to map relevant files. Yes → dispatch `worker`.
  4. After `worker` completes a code change → MUST dispatch `reviewer`.
     No exceptions.
  5. After `reviewer` returns: `status="passed"` → end turn with summary.
     `status="failed"` → dispatch `worker` again with reviewer's feedback,
     then re-dispatch `reviewer`. Max 2 review cycles, then escalate to
     user with unresolved issues.
- `oracle` usage note: dispatch for mid-task design questions, not
  implementation. Can also be dispatched before step 2 when approach is
  uncertain.
- Break-glass clause: skip a step only when the user explicitly asks.

`agents/worker.md` hardens the verification requirement from "verify when
possible" to "MUST run tests/lint/build after code changes, or explicitly
explain why verification cannot be run."

`agents/reviewer.md` is NOT changed — the `<verification status="..."/>`
format contract is injected by `AgentRoleContractProvider` (T2), not
duplicated in the `.md`.

`agents/planner.md`, `agents/scout.md`, `agents/oracle.md` receive brief
review for obvious issues; minor wording fixes only, no deep rewrite.

`agents/delegate.md` and `agents/context-builder.md` are left in place
(not deleted) but unreferenced by any `pool.yml`. A top-of-file comment
marks them as deprecated.

**Blocked by:** T1 (needs `roles` field on wire models to configure in
`pool.yml`) AND T2 (orchestrator.md's decision tree Step 5 references the
`<verification status="..."/>` format that T2's provider injects into
`reviewer`'s system prompt — without T2 the format is unguaranteed).

- [x] `config/pools/coder/pool.yml` has `main_agent_name: orchestrator`
- [x] `config/pools/coder/pool.yml` no longer references `delegate` or `context-builder` subagents
- [x] Each remaining subagent in `pool.yml` has a `roles:` field with the appropriate preset
- [x] `agents/coder.md` is renamed to `agents/orchestrator.md`
- [x] `agents/orchestrator.md` contains the 5-step orchestration decision tree with the structure specified above
- [x] `agents/orchestrator.md` contains the `oracle` usage note and the break-glass clause
- [x] `agents/worker.md` contains the hardened verification requirement ("MUST run tests/lint/build after code changes, or explicitly explain why verification cannot be run")
- [x] `agents/reviewer.md` is unchanged (no `<verification status="..."/>` format text added — that comes from T2's provider)
- [x] `agents/delegate.md` and `agents/context-builder.md` have a deprecation comment at the top
- [x] `agents/planner.md`, `agents/scout.md`, `agents/oracle.md` reviewed; minor fixes applied if obvious issues spotted
- [x] The orchestrator pool boots end-to-end: starting the bot and sending a coding task to the `coder` pool dispatches subagents per the decision tree (manual smoke test or integration test)
- [x] Existing bot tests still pass (any tests referencing `coder` as main agent name are updated to `orchestrator`)

## T4 — WebUI: PoolEditor Roles multi-select dropdown

**What to build:** The PoolEditor web UI gains a "Roles" multi-select
dropdown on both `MainAgentFields` and `SubagentCard` components. The
dropdown shows the seven preset `AgentRole` values with localized labels
(translation keys under `settings.pools.roles.*`) plus a "Custom…" entry
that reveals a free-text input for typing any string. Multiple roles can
be selected for a single agent.

TypeScript types add `roles?: string[]` to the main agent and subagent
node types. The backend save endpoint accepts `list[str]` for roles
without validating against the preset enum (custom strings are stored
as-is in `pool.yml`). The backend load endpoint round-trips preset values
as their string values (e.g. `"reviewer"`, not `"AgentRole.REVIEWER"`)
and preserves custom strings verbatim.

i18n: translation keys for the seven preset role labels are added to the
existing locale files (Chinese + English at minimum). Exact label wording
is decided at implementation time.

**Blocked by:** T1 (the backend `pool.yml` schema must support `roles`
before the UI can save/load it).

- [x] TypeScript types for main agent and subagent nodes include `roles?: string[]`
- [x] `MainAgentFields` component renders a "Roles" multi-select dropdown
- [x] `SubagentCard` component renders a "Roles" multi-select dropdown
- [x] Dropdown shows the seven preset values with localized labels
- [x] Dropdown offers a "Custom…" entry that reveals a free-text input
- [x] Multiple roles can be selected for a single agent
- [x] Saving a pool with `roles: ["reviewer", "office-expert"]` (preset + custom) round-trips through the backend unchanged on reload
- [x] Backend save endpoint accepts `list[str]` without rejecting unknown strings
- [x] i18n translation keys for the seven preset role labels are added to Chinese and English locale files
- [x] Existing PoolEditor tests still pass; new tests cover the Roles dropdown rendering, custom input, and round-trip behavior
