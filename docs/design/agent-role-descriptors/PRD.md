# Agent Role Descriptors + Role-Contract System Prompt Provider

Status: ready-for-agent

Related: ADR-0026 (`docs/adr/0026-agent-role-descriptors-and-role-contract-provider.md`);
ADR-0015 (`docs/adr/0015-unified-inbox-driven-agent-messaging.md` — `AgentTemplate.materialize`
is the subagent construction path that this feature extends with `roles`透传);
ADR-0020 (`docs/adr/0020-pool-config-convergence-and-framework-promotion.md` —
`PoolSpec`/`MainAgentSpec`/`SubagentSpec` are the wire models this feature extends);
ADR-0022 (`docs/adr/0022-external-coding-agent-integration.md` — D1 deferred
capability extends external_coding to subagent backend);
`CONTEXT.md` → "PoolSpec", "MainAgentSpec", "SubagentSpec", "AgentTemplate",
"AgentDescriptor", "SystemPromptPipeline", "SystemPromptProvider";
`docs/design/external-coding-agent-integration/deferred.md` (D1 record).

## Problem Statement

A bot developer running ModexAgent's multi-agent `coder` pool observes that the
main agent (currently named "coder") makes poor orchestration decisions: it
dispatches `worker` on complex tasks without first consulting `planner`; it
forgets to dispatch `reviewer` after code changes; it ignores `reviewer`'s
failure reports and ends the turn without dispatching `worker` to fix the
issues. From the user's perspective this looks like "the agent claims the task
is done but it isn't verified, or the agent drops the ball mid-task."

The framework today has no concept of "what role does this agent play in the
team" — every agent is just a name with a system prompt. There is no
framework-level way to:

- Distinguish a verification-role subagent (`reviewer`) from an
  implementation-role subagent (`worker`) at the descriptor level.
- Inject role-specific runtime contracts (e.g. "your final reply MUST contain
  `<verification status="passed|failed"/>`") consistently across all agents
  with a given role, regardless of which bot pool they live in.
- Let a bot UI expose role assignment as a first-class configuration concept
  for both main agents and subagents.

Without `roles`, every bot must hardcode role semantics into per-agent
`.md` prompt files, leading to drift between reviewer's output format and
orchestrator's parsing expectations, and no shared vocabulary for future
framework features (verifiers, dispatch policies, observability) to build on.

## Solution

Introduce **`AgentRole`** as a framework-level concept: an open string-typed
role tag carried on every agent's runtime descriptor. The framework ships a
small preset enum (`PLANNER` / `IMPLEMENTER` / `REVIEWER` / `SCOUT` / `ORACLE`
/ `COORDINATOR` / `COMMUNICATOR`) but accepts any custom string a bot wants
to use (e.g. `"office-expert"`).

On top of `roles`, introduce a single new `SystemPromptProvider` —
**`AgentRoleContractProvider`** — that injects role-specific runtime contracts
into the system prompt. Contracts are short, behavior-shaping directives
("you are a verification role; your final reply MUST contain
`<verification status="..."/>`"), owned by the framework so they are
consistent across all bots. The provider is the single source of truth for
these contracts; per-agent `.md` files continue to carry static identity and
capability descriptions but do NOT duplicate contract text.

Both main agents and subagents carry `roles`. The PoolEditor web UI exposes
role assignment as a multi-select dropdown (preset values + custom input),
writing the choice back to `pool.yml`.

The reference `coder` pool is renamed to the **orchestrator pattern**:
`coder` main agent becomes `orchestrator` (role `coordinator`), `worker`
stays named `worker` but carries role `implementer`, `reviewer` carries role
`reviewer`, etc. Two underused subagents (`delegate`, `context-builder`) are
removed from the pool config. The orchestrator's `.md` system prompt is
rewritten with an explicit 5-step orchestration decision tree (does the
task involve code → is it well-specified → is context clear → after code
changes MUST dispatch reviewer → reviewer failed MUST dispatch worker, max
2 cycles).

A deferred capability **D1** (external coding agent as a subagent backend)
is recorded but not implemented in this feature. D1 lets an `AgentTemplate`
declare `execution_strategy: external_coding` and materialize as an
`ExternalCodingAgent` instead of a `ReActAgent`, giving the orchestrator a
subagent-shaped interface to OpenCode/Pi. It is deferred because it touches
subagent materialize, lifecycle, and stop-event translation — higher risk
than this feature. When D1 lands, the related "auxiliary model routing"
capability drops in priority because the bulk of LLM calls shift to the
external subagent.

## User Stories

### Framework developers (consumers of the multi-agent capability)

1. As a framework developer, I want to import an `AgentRole` enum of preset
   role constants from the framework, so that I can reference common roles
   (`planner`, `implementer`, `reviewer`, `scout`, `oracle`, `coordinator`,
   `communicator`) without redefining them per bot.

2. As a framework developer, I want to assign custom role strings (e.g.
   `"office-expert"`, `"translator"`) to agents that the framework does
   not preset, so that I can express business-specific roles without
   forking the framework.

3. As a framework developer, I want every agent — main or subagent — to
   carry a `roles: list[str]` field on its runtime descriptor, so that
   downstream consumers (providers, hooks, observability) have a uniform
   place to read role information.

4. As a framework developer, I want `roles` to default to an empty list,
   so that existing agents without explicit role assignment keep working
   unchanged (zero behavior change for opt-out users).

5. As a framework developer, I want `roles` to NOT participate in
   `AgentDescriptor` equality or hash semantics, so that pool registration
   dedup is unaffected by role changes.

6. As a framework developer, I want a framework-provided
   `AgentRoleContractProvider` that injects role-specific runtime contracts
   into the system prompt, so that I do not have to hand-write contract
   text in every bot's `.md` files.

7. As a framework developer, I want `AgentRoleContractProvider` to be a
   `SystemPromptProvider` (not a hook), so that contract injection follows
   the same caching / compression / truncation path as every other system
   prompt segment.

8. As a framework developer, I want `AgentRoleContractProvider` to be
   byte-stable across turns for a given agent instance (because `roles`
   do not change mid-instance), so that it does not break prompt caching.

9. As a framework developer, I want `AgentRoleContractProvider` to ignore
   custom role strings it does not recognize, so that bot-specific roles
   do not cause errors and the framework stays generic.

10. As a framework developer, I want the `AgentRoleContractProvider` to
    inject distinct contracts for `REVIEWER`, `IMPLEMENTER`, and
    `COORDINATOR` roles (and shorter contracts for the other presets),
    so that the three core orchestration roles get meaningful runtime
    guidance while lesser-used presets still get a baseline.

### Bot maintainers (configuring the reference bot_project)

11. As a bot maintainer, I want to assign roles in `pool.yml` for both the
    main agent and each subagent, so that the PoolEditor UI and the
    framework runtime both see the role assignment.

12. As a bot maintainer, I want the `coder` pool's main agent renamed
    from `coder` to `orchestrator`, so that the agent's name reflects its
    actual responsibility (planning, dispatching, integrating) rather
    than implying it writes code itself.

13. As a bot maintainer, I want `delegate` and `context-builder`
    subagents removed from the `coder` pool config, so that the pool
    stops referencing dead/underused roles whose work is covered by
    `orchestrator` (delegation) and `scout` (context gathering).

14. As a bot maintainer, I want the orchestrator's system prompt to
    contain an explicit 5-step orchestration decision tree, so that the
    LLM has clear, ordered rules for when to dispatch `planner`,
    `worker`, `reviewer`, `scout`, and `oracle`.

15. As a bot maintainer, I want the orchestrator's decision tree to
    include a "MUST dispatch reviewer after code changes — no exceptions"
    rule, so that code changes are always reviewed before the turn ends.

16. As a bot maintainer, I want the orchestrator's decision tree to
    include a "reviewer status=failed → dispatch worker with feedback,
    max 2 review cycles, then escalate to user" rule, so that the
    orchestrator cannot silently swallow reviewer failures.

17. As a bot maintainer, I want the orchestrator's decision tree to
    include a "break-glass: skip a step only when the user explicitly
    asks" escape hatch, so that user intent always overrides the
    default orchestration rules.

18. As a bot maintainer, I want the `worker` system prompt to
    harden the "after code changes, MUST verify (run tests / lint /
    build, or explain why you cannot)" requirement, so that
    implementation subagents do not declare done without validation.

19. As a bot maintainer, I want `agents/delegate.md` and
    `agents/context-builder.md` to be considered deprecated (removed
    from pool.yml; files left in place but unreferenced), so that
    future cleanup is straightforward without breaking anything
    immediately.

### End users (via the PoolEditor web UI)

20. As an end user configuring a pool in the web UI, I want a "Roles"
    multi-select dropdown on every main agent and subagent card, so
    that I can assign roles without editing YAML by hand.

21. As an end user, I want the Roles dropdown to show preset values
    (`planner`, `implementer`, `reviewer`, `scout`, `oracle`,
    `coordinator`, `communicator`) with localized labels, so that I
    can pick from a sensible default list.

22. As an end user, I want the Roles dropdown to offer a "Custom…"
    input that lets me type any string, so that I can assign
    business-specific roles the framework does not preset.

23. As an end user, I want the Roles dropdown to allow selecting
    multiple roles for a single agent, so that I can express
    "this agent is both `planner` and `oracle`" when appropriate.

24. As an end user, I want the saved pool.yml to round-trip my
    role selection exactly (preset values stay as their string
    values, custom strings stay as typed), so that reloading the
    pool config in the UI shows the same selection I saved.

25. As an end user, I want the save endpoint to accept any
    `list[str]` for roles without rejecting unknown strings,
    so that custom roles do not cause validation errors.

### Future-facing (deferred D1 — recorded but not implemented)

26. As a future framework developer, I want the D1 capability
    (external coding agent as a subagent backend via
    `execution_strategy: external_coding` on `AgentTemplate`)
    to be documented as a deferred follow-up, so that the path
    from "orchestrator dispatches coding work to OpenCode via
    peer communication" to "orchestrator dispatches coding work
    to OpenCode as a subagent with `SubagentAutoSendHook`
    auto-notification" is visibly planned and not forgotten.

27. As a future framework developer, I want the documentation
    to note that "auxiliary model routing" (using cheaper models
    for curator / review / embedding) drops in priority after D1
    lands, so that future planning does not over-invest in
    auxiliary routing when the bulk of LLM cost has shifted to
    the external subagent.

## Implementation Decisions

### `AgentRole` preset enum

- New `AgentRole(StrEnum)` in the framework constants module with seven
  preset values: `PLANNER`, `IMPLEMENTER`, `REVIEWER`, `SCOUT`, `ORACLE`,
  `COORDINATOR`, `COMMUNICATOR`.
- `IMPLEMENTER` is the framework-layer abstract name for the
  implementation role; bots may keep their concrete agent named `worker`
  but tag it with role `implementer`. The framework never references
  business-specific names like `worker`.
- `REVIEWER` is the verification role. `COORDINATOR` is the main-agent
  orchestration role. Other presets carry their natural meaning.
- The enum is `StrEnum` so values serialize as plain strings in YAML
  and JSON without `.value` access.

### `roles` field on agent descriptors

- The `roles` field is `list[str]` (NOT `list[AgentRole]`) everywhere
  it appears. Rationale: bots must be free to use custom role strings
  the framework does not preset, and Pydantic `list[AgentRole]` would
  reject them.
- Three structures gain the field:
  - The framework's main-agent wire model (`MainAgentSpec` /
    `AgentConfig` depending on the current naming after ADR-0020
    convergence).
  - The framework's subagent wire model (`SubagentSpec` /
    `AgentTemplateSpec`).
  - The runtime `AgentDescriptor` (main + subagent shared descriptor).
- Default value is `[]` (empty list). Existing agents without explicit
  role assignment behave identically to today.
- The透传 chain is: wire model (`MainAgentSpec` / `SubagentSpec`) →
  `AgentTemplate.materialize` / main-agent factory → `AgentDescriptor.roles`.
  Both main-agent factory and `AgentTemplate.materialize` read `roles`
  from their input wire model and write it onto the descriptor they
  construct.
- `AgentDescriptor.__eq__` and `__hash__` (if defined) do NOT include
  `roles`. Roles are metadata, not identity. Pool registration dedup
  is unaffected.

### `AgentRoleContractProvider` (new SystemPromptProvider)

- New class implementing the framework's `SystemPromptProvider` ABC.
- Wired into the `SystemPromptPipeline` constructed by
  `MemorySystemContextManager.load()` (the shared main + subagent
  system prompt assembly path). Position in the provider chain is
  after business providers (`ExperienceProvider`, `SkillProvider`,
  etc.) so contract text appears late in the system prompt
  (high-priority position).
- The provider reads `roles` from the agent's `AgentDescriptor`
  (passed via the pipeline construction context, the same way other
  providers receive agent identity).
- For each preset role present in `roles`, the provider appends a
  short contract segment to the system prompt:
  - `REVIEWER` → contract requiring the final reply to contain
    `<verification status="passed|failed" reason="..."/>`.
  - `IMPLEMENTER` → contract requiring verification (run tests /
    lint / build, or explain why impossible) after code changes.
  - `COORDINATOR` → contract describing reviewer's output format
    and the obligation to dispatch the implementer role on
    reviewer failure.
  - `PLANNER` / `SCOUT` / `ORACLE` / `COMMUNICATOR` → shorter
    contracts describing their core responsibility.
- For unrecognized role strings, the provider injects nothing.
  No warning, no error — custom roles are legitimate.
- The provider's output is byte-stable for a given `roles` value
  across turns (no timestamps, no random content). This preserves
  prompt cache friendliness.
- Exact contract text wording is decided at implementation time
  (brief design during implementation, not in this spec). The
  contracts must be short (one short paragraph each), behavior-
  shaping, and unambiguous about the required output format.

### `pool.yml` and `.md` changes in `examples/bot_project/`

- `config/pools/coder/pool.yml`:
  - `main_agent_name`: `coder` → `orchestrator`.
  - Each subagent gains a `roles:` field with the appropriate preset.
  - `delegate` and `context-builder` subagent entries removed.
- `agents/coder.md` → renamed to `agents/orchestrator.md`, content
  rewritten:
  - Identity: "You are the Orchestrator, responsible for planning,
    dispatching, and integrating subagent work."
  - Tool usage: unchanged (orchestrator retains its tool access).
  - **5-step orchestration decision tree** (the core change):
    1. Does the task involve code/file modification? No → answer
       directly. Yes → step 2.
    2. Is the task well-specified? No → dispatch `planner` first,
       wait for plan. Yes → step 3.
    3. Is codebase context clear to the implementer? No → dispatch
       `scout` first to map relevant files. Yes → dispatch `worker`.
    4. After `worker` completes a code change → MUST dispatch
       `reviewer`. No exceptions.
    5. After `reviewer` returns: `status="passed"` → end turn with
       summary. `status="failed"` → dispatch `worker` again with
       reviewer's feedback, then re-dispatch `reviewer`. Max 2
       review cycles, then escalate to user with unresolved issues.
  - `oracle` usage note: dispatch for mid-task design questions,
    not implementation. Can also be dispatched before step 2 when
    approach is uncertain.
  - Break-glass clause: skip a step only when the user explicitly
    asks.
- `agents/worker.md`: harden the verification requirement from
  "verify when possible" to "MUST run tests/lint/build after code
  changes, or explicitly explain why verification cannot be run."
- `agents/reviewer.md`: NO change to the `.md` file itself — the
  `<verification status="..."/>` format contract is injected by
  `AgentRoleContractProvider`, not duplicated in the `.md`. The
  `.md` continues to describe reviewer's identity, review types,
  and working rules.
- `agents/planner.md`, `agents/scout.md`, `agents/oracle.md`:
  brief review for obvious issues; not deeply rewritten. Any
  changes are minor wording fixes.
- `agents/delegate.md`, `agents/context-builder.md`: left in place
  (not deleted) but unreferenced by any pool.yml. Marked as
  deprecated via a top-of-file comment.

### Web UI changes (`examples/bot_project/webui/`)

- TypeScript types: add `roles?: string[]` to the main agent and
  subagent node types in the PoolEditor's type definitions.
- `MainAgentFields` and `SubagentCard` components: add a "Roles"
  multi-select dropdown. The dropdown shows the seven preset
  `AgentRole` values with i18n labels (translation keys under
  `settings.pools.roles.*`) plus a "Custom…" entry that reveals
  a free-text input.
- Backend save endpoint: accept `list[str]` for roles without
  validating against the preset enum. Custom strings are stored
  as-is in `pool.yml`.
- Backend load endpoint: round-trip preset values as their string
  values (e.g. `"reviewer"`, not `"AgentRole.REVIEWER"`) and
  preserve custom strings verbatim.
- i18n: translation keys for the seven preset role labels are
  added to the existing locale files (Chinese + English at
  minimum). Exact label wording is decided at implementation
  time.

### D1 deferred capability (recorded, not implemented)

- Documented in `docs/design/external-coding-agent-integration/deferred.md`
  (already updated) and ADR-0026.
- The capability extends `AgentTemplate` to declare
  `execution_strategy: external_coding` + `provider_kind: opencode`,
  materializing as `ExternalCodingAgent` instead of `ReActAgent`.
- Prerequisites for restarting D1: the orchestrator pattern
  (this feature) must be stable in production use; the next
  bottleneck must be "coding delegation quality / cost to
  OpenCode."
- D1 touches three non-trivial areas:
  `SubagentDispatchStrategy` / `AgentTemplate.materialize` (new
  strategy branch); subagent lifecycle (external subagents own
  workdir / CLI process / provider session, eviction must reap
  these, session resume reuses provider session); stop-event
  translation (external backend stop events → ModexAgent
  `StopReason`).
- During D1's deferral, the orchestrator dispatches coding work
  to the `opencode` pool via cross-pool peer communication
  (ADR-0019). This lacks `SubagentAutoSendHook` auto-notification
  but is acceptable as a transitional state.
- When D1 lands, "auxiliary model routing" (using cheaper models
  for curator / review / embedding) drops in priority because
  LLM call volume shifts to the external subagent.

### Out-of-scope items (decided during design grill)

- **`BeforeEndHook` + `StopVerifier` + injection of
  `system_reminder` messages**: CUT. The事后拦截 (post-hoc
  interception) path was designed to prevent "agent ends without
  dispatching reviewer" but analysis showed: (a) for the
  "reviewer failed but orchestrator still ends" case,
  `SubagentAutoSendHook` already fold-in'd the reviewer's report,
  so injecting another reminder is duplicate information; (b) for
  the "never dispatched reviewer" case, the prompt decision tree
  + provider contract already cover it, and if the LLM still does
  not comply, post-hoc interception cannot rescue it either. The
  cost (new HookPoint + ABC + verdict type + custom message role
  + XML tag) exceeds the value.
- **`system_reminder` custom message role**: CUT (follows from
  the above).
- **Auxiliary model routing** (cheaper models for curator /
  review / embedding): DEFERRED, and explicitly de-prioritized
  after D1.
- **Prompt caching investigation / `cache_control` breakpoints**:
  OUT OF SCOPE. The system prompt is rebuilt every turn under
  a version-controlled scheme; introducing Anthropic
  `cache_control` breakpoints is not pursued.
- **Transient-failure framework-level retry for external coding
  agents**: OUT OF SCOPE.
- **First-exchange protection / Microcompact optional wiring in
  governance**: OUT OF SCOPE.
- **`experience` → `skill` promotion** (curator pattern):
  OUT OF SCOPE. The `experience` system is the bot's chosen
  mechanism for this functionality and is treated as equivalent
  to skills; the two systems remain parallel and non-interacting.
- **Coder pool → external OpenCode subagent migration**: DEFERRED
  via D1. During this feature, the `coder` pool retains its own
  `worker` subagent for in-process implementation; OpenCode
  remains a separate pool reachable via peer communication.

## Testing Decisions

### What makes a good test here

Tests should verify **external behavior**, not implementation
details. For this feature, external behavior means:

- Given a `SubagentSpec` with `roles=["reviewer"]`, the
  materialized `AgentDescriptor` carries `roles=["reviewer"]`.
- Given an `AgentDescriptor` with `roles=["reviewer"]`, the
  `AgentRoleContractProvider` injects text containing
  `<verification status="passed|failed"` into the system prompt.
- Given an `AgentDescriptor` with `roles=["custom_role"]`, the
  provider injects nothing (and does not error).
- Given a `pool.yml` with `roles: [planner]` on a subagent, the
  PoolStore round-trips the value back unchanged on load.
- Given a PoolEditor save with `roles: ["reviewer", "custom"]`,
  the backend persists both values to `pool.yml` without
  validation errors.

Tests should NOT verify:

- The exact wording of contract text (this is intentionally
  left to implementation-time brief design and may evolve).
- The internal data layout of `AgentDescriptor` beyond the
  `roles` field's presence and value.
- The order of providers in the `SystemPromptPipeline` (this
  is a wiring detail).

### Modules to be tested

1. **`AgentRole` enum** — value stability, StrEnum serialization,
   preset values cover the seven documented roles. New test file
   under the existing `tests/unit/core/` pattern (alongside
   existing `test_constants.py` if present, or a new file in the
   same directory).

2. **`AgentTemplate.materialize` roles透传** — extend the existing
   `tests/unit/multi_agent/test_template_materialize.py` with
   cases verifying that `SubagentSpec.roles` appears on the
   materialized `AgentDescriptor.roles`. Prior art: the existing
   tests in this file already assert descriptor fields after
   materialize (e.g. `test_materialize_subagent_inherits_reasoning_effort`).

3. **Main-agent factory roles透传** — extend existing main-agent
   factory tests under `tests/unit/ioc/` or
   `tests/unit/multi_agent/` with cases verifying that
   `MainAgentSpec.roles` appears on the constructed
   `AgentDescriptor.roles`. Prior art: existing factory tests
   that assert descriptor fields.

4. **`AgentRoleContractProvider`** — extend the existing
   `tests/unit/memory/prompt_pipeline/test_providers.py` with
   cases verifying injection for each preset role, no-op for
   unknown roles, byte-stability across calls, and combined
   injection when multiple roles are present. Prior art: the
   existing tests for `ExperienceProvider`, `RuntimeProvider`,
   `SkillProvider` in the same file follow exactly this pattern.

5. **`PoolStore` round-trip** — extend existing pool config
   tests under `tests/unit/multi_agent/pool_config/` (or
   equivalent location after ADR-0020 convergence) verifying
   that `roles` survives a save → load cycle. Prior art:
   existing round-trip tests for other `SubagentSpec` /
   `MainAgentSpec` fields.

6. **Web UI** — extend existing PoolEditor tests (under
   `examples/bot_project/webui/`) verifying the Roles dropdown
   renders preset options, accepts custom input, and round-trips
   through the save endpoint. Prior art: existing PoolEditor
   component tests.

### Test seams (highest-seam principle)

Two existing seams cover the framework-level behavior; no new
seam is introduced:

- **`tests/unit/multi_agent/test_template_materialize.py`** —
  the single materialize seam covers `roles`透传 for subagents.
  Main-agent factory透传 is verified through a parallel existing
  factory test (no new seam).
- **`tests/unit/memory/prompt_pipeline/test_providers.py`** —
  the single provider seam covers `AgentRoleContractProvider`
  injection behavior.

These two seams cannot be merged (materialize is a
construction-time concern, provider injection is a
prompt-assembly-time concern), so two seams is the minimum.

## Out of Scope

- The `BeforeEndHook` / `StopVerifier` / `system_reminder`
  post-hoc interception path (designed and cut during grill;
  see ADR-0026 Considered Options).
- Prompt caching / `cache_control` breakpoints.
- Auxiliary model routing (deferred, de-prioritized after D1).
- Transient-failure framework-level retry for external coding
  agents.
- First-exchange protection / Microcompact governance wiring.
- `experience` → `skill` promotion.
- D1 itself (external coding agent as subagent backend) —
  documented as deferred, not implemented.
- Deep rewrite of `planner.md` / `scout.md` / `oracle.md` —
  only minor wording fixes if obvious issues are spotted.
- The exact wording of role contract text and i18n labels —
  left to implementation-time brief design.

## Further Notes

### Items intentionally left to implementation-time brief design

The following details were discussed during the design grill
but deliberately not finalized in this spec. The implementer
should make a brief design decision (a few sentences in the
PR or commit message) at implementation time:

- **Exact wording of `AgentRoleContractProvider` contract text
  for each preset role.** Constraints: short (one paragraph
  each), behavior-shaping, unambiguous about required output
  format (especially for `REVIEWER`'s
  `<verification status="passed|failed" reason="..."/>`).
- **Exact i18n label wording for the seven preset roles in the
  PoolEditor dropdown** (Chinese + English at minimum).
- **Exact wording of the 5-step orchestration decision tree in
  `agents/orchestrator.md`.** The spec fixes the structure
  (5 steps, max 2 review cycles, break-glass clause) but not
  the prose.
- **Priority value / position of `AgentRoleContractProvider`
  within the `SystemPromptPipeline` provider chain.** Spec
  fixes the constraint "after business providers" but the
  exact priority value is decided at implementation time by
  inspecting the existing provider priority scheme.
- **Whether `AgentRoleContractProvider` should support
  bot-supplied custom contract text for custom roles** (e.g.
  bot configures `"office-expert"` → custom contract string).
  Current decision: NOT supported in this feature. If a real
  need emerges, extend the provider constructor with a
  `custom_contracts: dict[str, str]` parameter in a follow-up.

### Risk profile

- **Framework change surface**: small and additive. One new
  StrEnum, one new `list[str]` field on three existing
  structures, one new `SystemPromptProvider` implementation.
  No existing runtime path is modified (only extended with
  new field透传).
- **Bot change surface**: medium. `pool.yml` rewrite, two
  `.md` files deprecated, one `.md` file renamed and
  rewritten, one `.md` file hardened. All isolated to
  `examples/bot_project/`.
- **Web UI change surface**: medium. Three places touched
  (TS types, two form components, save/load endpoints). No
  new component architecture; extends existing PoolEditor
  patterns.
- **Behavioral risk**: low. Default `roles=[]` means existing
  agents behave identically. The orchestrator pattern only
  activates when a bot explicitly opts in by configuring
  `roles` on its agents.
- **Prompt stability risk**: low. `AgentRoleContractProvider`
  output is byte-stable per agent instance. The
  orchestrator's decision tree is static text in a `.md`
  file. Neither introduces per-turn variance.

### Related deferred record

See `docs/design/external-coding-agent-integration/deferred.md`
for the full D1 record (external coding agent as subagent
backend). That document has been updated to reference this
feature as a prerequisite and to record the auxiliary-model-
routing priority drop.
