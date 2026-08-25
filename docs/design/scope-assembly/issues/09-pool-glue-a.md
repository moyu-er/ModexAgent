# 09 — create_pool glue migration A: memory/approval/experience/notifications

**What to build:** For pools on the declaration path, the position-derived defaults take over from caller-side branches: memory presets (`main_agent_memory`/`subagent_memory` two-preset split) die in favor of position-derived defaults with override + policy validation (V9 already in place); experience wiring becomes supplied infrastructure referenced from the declaration; notification hooks become slot components. The corresponding `bot/service/pool/` module glue dies. This is the first half of the four-way classification migration (become config / become slot components / become supplied infra / delete).

**Blocked by:** 07 (the pivot pool proves the declaration boot path).

**Status:** closed (resolved 2026-08-21)

- [x] Memory configuration for a declaration-path pool comes entirely from position-derived defaults + node override; the two-preset functions are gone from that path
- [x] Experience components assemble via supplied infrastructure (the bot-global provider dependency becomes an explicit supplied-infra reference in the declaration/config surface)
- [x] Notification hooks register via HOOK-slot components referenced from the declaration
- [x] The corresponding pool/ module glue lines are deleted (deletion ledger: named per module)
- [x] Behavior parity: memory/experience/notification behavior identical across the bot test suite for migrated pools
- [x] Non-root approval remains a startup error (V9) — regression test present

## Resolution notes (2026-08-21)

- **(a)** `scope/defaults.memory_config_for_position(defaults, *, session_max_context_tokens)`
  is the single derivation point (position family → preset family, resolved
  archive/core toggles + session threshold). `resources.py` partitions the
  pools by boot road: declared pools get deps via
  `stack._declared_assembly_deps(root)`; `_build_assembly_deps_for_pools`
  (the two-preset branch) feeds only the legacy road. `declaration.py`'s
  `subagent_memory()` template seeding died — templates carry `memory=None`
  and `assemble_native_agent`'s `_merge_memory(None, spec.memory_overrides)`
  derives the session-only config at materialization (identical product,
  one derivation point). Grep proof in evidence.
- **(b)** `ExperienceReviewHookFactory` (FW defaults) now assembles from the
  chain: the review agent is built on
  `PoolRuntimeDeps.experience_review_provider` (the bot-global default
  provider, resolved once in `create_pool`'s declaration branch and carried
  through `SupplyInfra`), the memory system + experience dir come from
  `pool_assembly_ctx.pool_data`, the meta store is derived from the dir.
  Missing supply raises loudly (never silently skipped). The declaration
  references `experience_review` from the root's roster (bot.yml); the
  six early exits + construction in `_wire_pool_to_resources` are gated off
  for declaration pools by the resources.py loop.
- **(c)** `UserNoticeCleanupHook` registers via the existing BIZ
  `user_notice_cleanup` MemoryHookFactory (plugins/bot_hooks.py), referenced
  from the root's roster. The factory.py code-wired construction is gated
  `declared is None` — dead for the declaration road. The notification
  service is now constructed before the pipeline and supplied through
  `SupplyInfra.notification_service` (the strategies always left the
  StrategyAssembly field `None`, so the roster dispatch path had never been
  production-exercised — fixed here). `TodoReorientationHook`'s code-wiring
  stays (not a notification hook; its `has_archive` derives from the
  declared memory config through the deps channel — ticket 10/11 migrate it).
- **(d)** Deletion ledger (default declaration assembly path): declaration.py
  preset calls (2 sites) removed; stack.py two-preset branch excluded for
  declared pools (structural partition); factory.py UserNoticeCleanupHook
  construction gated; pool_wiring experience construction gated off. Greps
  in evidence.
- **(e)** Split-brain (fixtures/split_brain_09, both roads driven WITH
  pool_data): memory_config byte-identical, memory cleanup hooks identical
  pair + order, react hook SET identical (only experience_review_hook's
  position differs — Stage-4 dispatch vs post-boot wiring, stale-guarded).
  Full bot suite green.
- **(f)** V9 boot regression: `test_boot_v9_non_root_approval_fails_phase2`
  (a non-root approval declaration aborts `boot_scope_declaration` at
  phase 2 — the boot-wiring half; the pure-function halves live in
  tests/unit/scope/test_validator.py).
