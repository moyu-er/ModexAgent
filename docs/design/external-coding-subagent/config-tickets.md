# Tickets: Subagent Native/External Config in WebUI + worker.yml Conversion

Enable bot operators to configure subagents as native (react) or external (opencode/pi) from the WebUI PoolEditor, and convert the coder pool's `worker` subagent to an opencode external implementation as a live proof.

Source: ADR-0027 framework (T1-T9 complete); this is the bot_project config-layer follow-up.
Parent ADR: `docs/adr/0027-external-agent-as-subagent.md`

Work the **frontier**: any ticket whose blockers are all done. For this breakdown that means S1 can start immediately; S2 blocks on S1; S3 blocks on S2; S4 blocks on S3.

---

## S1 — Backend: persist subagent execution_strategy + provider_kind in YAML  ✅ DONE

**What to build:** When a subagent's `execution_strategy` or `provider_kind` is set via the WebUI (or hand-edited in `templates/*.yml`), the backend `PoolConfigStore.write_pool` round-trips both fields to disk. Today `SubagentSpec` already carries these fields (T1), but `_SUBAGENT_YAML_FIELDS` and `_SUBAGENT_DEFAULTS` in `store.py` omit them, so `write_pool` silently drops them. This ticket closes that gap so the framework's T1-T9 work is actually reachable from bot_project config.

**Blocked by:** None — can start immediately.

- [ ] `_SUBAGENT_YAML_FIELDS` in `store.py` includes `"execution_strategy"` and `"provider_kind"`
- [ ] `_SUBAGENT_DEFAULTS` in `store.py` includes `"execution_strategy": "react"` and `"provider_kind": None` (so defaults are stripped from YAML, no noise)
- [ ] Existing subagent templates (worker, scout, reviewer, planner, oracle, office-expert) still round-trip without spurious `execution_strategy: react` lines — regression check
- [ ] A subagent spec with `execution_strategy: external` + `provider_kind: opencode` round-trips: write → read → fields preserved
- [ ] A subagent spec with `execution_strategy: react` + `provider_kind: null` round-trips: write → read → `provider_kind` is None (not a string "null")
- [ ] Unit test in `tests/unit/multi_agent/pool_config/` (or `tests/unit/multi_agent/test_pool_config.py`) covers the round-trip for both external and native subagent specs

## S2 — Frontend types: SubagentNode gains execution_strategy + provider_kind  ✅ DONE

**What to build:** The TypeScript `SubagentNode` interface in `pool.ts` gains optional `execution_strategy` and `provider_kind` fields, mirroring `MainAgentNode`. The `defaultSubagent()` factory in `PoolEditor.tsx` defaults `execution_strategy` to `"react"` so new subagents are native by default. This is a pure type extension — no UI behavior change.

**Blocked by:** S1 (backend round-trip must work before frontend can send the fields)

- [ ] `SubagentNode` in `pool.ts` has `execution_strategy?: ExecutionStrategy` and `provider_kind?: ProviderKind | null`
- [ ] `defaultSubagent()` in `PoolEditor.tsx` sets `execution_strategy: "react"` (and leaves `provider_kind` undefined, which the backend treats as null)
- [ ] `normalizeTree` in `PoolEditor.tsx` also normalizes subagent `provider_kind` (not just main) so an unsupported provider doesn't get silently lost on read+save
- [ ] TypeScript compiles with no new errors (`npm run build` in `webui/`)
- [ ] Existing frontend tests pass (`npm test` in `webui/`)

## S3 — Frontend UI: SubagentCard native/external toggle + conditional field rendering  ✅ DONE

**What to build:** The `SubagentCard` component in `PoolEditor.tsx` gains a native/external implementation toggle at the top (reusing `IMPLEMENTATION_DEFS` + `DropdownPanel`). When a subagent is `external`:
- A Provider dropdown appears (reusing `PROVIDER_OPTIONS` + `selectProvider` + `PROVIDER_BRAND_ICONS`)
- The external-runtime panel (brand icon + provider + "managed by provider" helper) renders, mirroring `ExternalMainAgentFields`
- Native-only fields are hidden: `tool_preset`, `tool_supplements`, `context_mode`, `system_prompt_mode`, `fork_max_messages`, `mcp`, `prompt_name`, `AgentSkillSelector`
- Identity fields remain visible: `agent_name`, `description`, `max_steps`, `roles`
  (**NOTE**: `max_steps` and `roles` are visually retained and backend-persisted, but at runtime they are inert for external subagents — the external CLI owns its own iteration limit and role contract. The UI keeps them for configuration continuity / future use, not because the framework consumes them.)
- Switching native→external sets `execution_strategy: "external"` + `provider_kind: DEFAULT_EXTERNAL_PROVIDER` (preserving native field values in form state — they're hidden but not cleared, same as main agent)
- Switching external→native sets `execution_strategy: "react"` + `provider_kind: null` (hidden native fields reappear with their preserved values)
- NO confirm dialog needed (unlike main agent, a subagent switching to external does not clear other subagents — only itself is affected)

When a subagent is `react` (default), the card renders exactly as today — byte-for-byte unchanged.

**Blocked by:** S2 (SubagentNode type must have the fields before the UI can toggle them)

- [ ] `SubagentCard` renders an Implementation `DropdownPanel` at the top of the expanded card (before agent_name)
- [ ] When `execution_strategy === "external"`: Provider `DropdownPanel` + external-runtime panel render; native-only fields hidden
- [ ] When `execution_strategy === "react"` (or undefined): card renders byte-for-byte as today
- [ ] Switching native→external: `execution_strategy` + `provider_kind` set; native field values preserved in form state (hidden, not cleared)
- [ ] Switching external→native: `execution_strategy` reset to `"react"`; `provider_kind` reset to `null`; native fields reappear with preserved values
- [ ] The card summary line (collapsed) reflects the implementation: e.g. `external · opencode` for external, existing `read_write · mcp·0 · fork` for native
- [ ] No confirm dialog on subagent implementation switch (subagent switch is non-destructive to siblings)
- [ ] i18n: reuses existing `settings.external.*` keys (native/external/provider/managedByProvider/providerRunHelper) — no new keys needed
- [ ] `npm run build` succeeds; `npm test` passes

## S4 — Config: convert coder/worker to opencode external subagent  ✅ DONE

**What to build:** The coder pool's `worker` subagent template (`config/pools/coder/templates/worker.yml`) is converted from a native react subagent to an external opencode subagent. This is the live proof that the S1-S3 config layer works end-to-end — the main `orchestrator` agent delegates implementation work to an opencode-powered worker via `send_to_agent`, and the worker replies via `modexctl send`.

The current `worker.yml`:
```yaml
agent_name: worker
description: Implementation agent — the single writer thread
max_steps: 150
context_mode: fork
fork_max_messages: 60
tool_preset: read_write
roles: [implementer]
```

Becomes:
```yaml
agent_name: worker
description: Implementation agent — the single writer thread (OpenCode-powered)
max_steps: 150
execution_strategy: external
provider_kind: opencode
roles: [implementer]
```

Native-only fields (`context_mode`, `fork_max_messages`, `tool_preset`) are removed — they're meaningless for an external subagent (the provider CLI manages its own tools, context, and system prompt). `max_steps` and `roles` are also removed — like native-only fields, they are inert for external subagents (the external CLI owns its own iteration limit and role contract; the framework does not consume these fields in the external path). Keeping them in the YAML would mislead readers into thinking they affect runtime behavior.

**Blocked by:** S3 (the WebUI must be able to render + edit external subagents before we ship one in the default config — otherwise opening the coder pool in the WebUI would show a broken card)

- [ ] `config/pools/coder/templates/worker.yml` has `execution_strategy: external` + `provider_kind: opencode`
- [ ] Native-only fields (`context_mode`, `fork_max_messages`, `tool_preset`) removed from worker.yml
- [ ] `description` updated to mention OpenCode
- [ ] `max_steps` and `roles` removed (inert for external — external CLI owns iteration limit + role contract)
- [ ] Coder pool boots successfully (opencode CLI availability gated — if opencode not on PATH, the subagent fails gracefully at materialize time, not at pool boot)
- [ ] Verify in WebUI: opening coder pool → worker subagent card shows external runtime panel + provider = opencode; native fields hidden
- [ ] Verify: orchestrator can `send_to_agent(worker, ...)` and worker's turn runs via opencode (manual or scripted test)

---

## Dependency Graph

```
S1 (Backend store.py) ──→ S2 (Frontend types) ──→ S3 (Frontend UI) ──→ S4 (worker.yml)
```

### Frontier waves

- **Wave 1 (no blockers)**: S1
- **Wave 2**: S2 (←S1)
- **Wave 3**: S3 (←S2)
- **Wave 4**: S4 (←S3)

Strictly serial — each ticket gates the next. Work the frontier one ticket at a time with `/implement`, clearing context between tickets.
