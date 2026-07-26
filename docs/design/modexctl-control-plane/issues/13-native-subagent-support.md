# 13 — Native subagent support (control_origin, persistence, strategy)

**What to build:** Fix three issues that prevented native subagents from
using `modexctl`:

1. **`control_origin` on `AgentMaterializeDeps`**: Native subagents
   (materialized via the template path) did not receive
   `MODEX_CONTROL_ORIGIN` in their `ExternalEnvSpec`. Add a
   `control_origin` field to `AgentMaterializeDeps`, set it from
   `build_control_origin` at boot, and pass it to the subagent
   `ExternalEnvSpec` in `template.py`.

2. **`memory_store_registry` threading**: Subagent `MemorySystem` used
   the FILE backend while the facade queried the main agent's SQLite
   backend, making subagent history invisible. Thread
   `memory_store_registry` through `AgentMaterializeDeps` so subagents
   use the same SQLite backend as the main agent.

3. **`PoolInstance.main_execution_strategy`**: The facade read
   `pool.yml` on each request to get the execution strategy, which
   failed when the pool spec could not be loaded (blocking subagent
   history queries). Set `main_execution_strategy` on `PoolInstance` at
   boot instead.

**Blocked by:** 01 (control origin injection), 04 (native history).

**Status:** done (commit e414b304)

- [x] `AgentMaterializeDeps` has a `control_origin` field.
- [x] `control_origin` is set from `build_control_origin` at boot.
- [x] Subagent `ExternalEnvSpec` in `template.py` receives
      `control_origin`.
- [x] Native subagent `modexctl send` works (no empty
      `MODEX_CONTROL_ORIGIN`).
- [x] `memory_store_registry` threaded through `AgentMaterializeDeps`.
- [x] Subagent `MemorySystem` uses the same SQLite backend as the main
      agent.
- [x] Subagent history is visible through the control facade.
- [x] `PoolInstance.main_execution_strategy` is set at boot.
- [x] No per-request `pool.yml` disk read for execution strategy.
