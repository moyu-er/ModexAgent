# 10 — create_pool glue migration B: remaining modules + SubagentInvocationContext dies

**What to build:** The remaining `bot/service/pool/` modules complete the four-way classification (config / slot component / supplied infra / delete), finishing the create_pool takeover. `SubagentInvocationContext` is absorbed into the Agent layer of the context chain and deleted — per-invocation data (parent, invocation id) flows through `AgentContext` like every other agent-layer datum, one mechanism. Residual builders.py glue dies with the last module. After this ticket, a declaration-path pool's assembly touches zero business glue.

**Blocked by:** 07 (pivot), 05 (builders tool-construction block already dead — this clears the rest).

**Status:** closed (resolved 2026-08-21)

- [x] Remaining pool/ modules classified and migrated; the module list in the deletion ledger is fully struck through
- [x] `SubagentInvocationContext` deleted; subagent materialization reads per-invocation data from `AgentContext`
- [x] builders.py residual glue deleted (the file shrinks to channel-adapter wiring only, or dies entirely)
- [x] External-agent pools (opencode) boot on the declaration path — the external strategy's assembly differences live inside the strategy component (no caller-side if branches)
- [x] All migrated pools pass the full bot test suite
- [x] Architecture guard: no business-layer direct tool/resource construction on the declaration path (import-level check if feasible)

## Resolution notes

- **Ledger** (口径: declaration-path assembly surface — default + opencode both boot declared): per-module four-way table + grep verification in `.omo/evidence/task-11-scope-assembly-implementation.md` §5. Struck through: `builders.py` MCP segment (+ helper + legacy private-manager branch, N11 delete — both roads load MCP at Stage 4 through the FW loader reading the chain's workspace-layer handle), `pipeline_wiring.py` interceptor/command resolution (FW-ized into `PoolAssembleStage`, products ride `PoolRuntimeDeps`), `factory.py` external identity branches (converged onto strategy capability flags). Staying entries carry explicit classifications (supplied infra / legacy-only-dies-17 / →11).
- **builders.py evaluated as channel-adapter wiring, not dead**: the react strategy's glue tools (send_file_to_user/experience/opt-in kb) have no roster factories, and the persistence/inbox/todo builders serve both boot roads — the file shrinks to that wiring (10+/134−).
- **SubagentInvocationContext death**: `assemble_sub(ctx: AgentContext, deps: AgentMaterializeDeps)` — per-invocation data on the 04 chain carrier (one mechanism), per-pool connections on the explicit deps parameter. `grep -rn "SubagentInvocationContext" src/ examples/` → 0.
- **opencode declared boot**: `DECLARATION_BOOT_POOLS = {default, opencode}`; split-brain baseline frozen at `224fcbfb`, new road compared field-by-field with ONE allowlisted entry (the never-firing UserNoticeCleanupHook registration on the external main's memory runner — Stage-4-roster-dispatch shape); `shutil.which`-unavailable skip semantics preserved. Caller-side external 特判 converged: `requires_llm_provider` capability flag (ADR-0025 D5 pattern) + capability-driven registration — zero `AgentType.external*` branches in `create_pool`.
- **Architecture guard final form** (deterministic, in `test_scope_declaration_boot.py`): declaration modules keep the full 07 forbidden list (+ MCP symbols); tool-construction + MCP-loading symbols banned pool/-wide; communication-tool construction confined to the legacy-only `communication.py`; `AgentType.external*` identity branches banned in pool/.
- Gates: bot 2140 / unit 8221 zero failures, mypy 93 (error-set diff zero new), ruff zero new on changed files, slots 16/16, architecture 89.
