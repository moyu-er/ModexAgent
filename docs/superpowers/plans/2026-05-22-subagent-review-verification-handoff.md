# Subagent Review Verification Handoff

Date: 2026-05-22

## Current Scope

This handoff records the verification already performed for the subagent/session/communication review work, plus the remaining validation and code-review tasks.

## Follow-up Update

After the follow-up review, the current implementation was further checked and adjusted:

- `invocation_id` session suffixing is now target-policy based. It is applied only when the target agent is configured as an invocation-session subagent target, not by hardcoding `main` or by blindly suffixing any message carrying a UUID.
- Subagent replies to main may carry `invocation_id` in the payload for acknowledgement/tracking, while main still receives the message through the default communication session unless explicitly configured otherwise.
- `DispatchTaskTool` now preserves the additional `context` inside both `content` and `task_prompt`.
- `CommunicationTracker` sideband prompt sections are injected through input metadata and assembled into the system prompt.
- Dynamic subagent admission now namespaces runtime descriptors as `dyn.<template>.<suffix>` to avoid colliding with resident names.
- Session cap enforcement now returns when `excess <= 0`; this fixes the Python negative-slice bug that previously evicted sessions before the configured cap was exceeded.
- Session eviction now uses the same LRU policy path for cap enforcement and ensures manually tracked sessions have locks before eviction.
- Type annotations were tightened in touched multi-agent files; the remaining standard file/search tool constructors are isolated with local `no-untyped-call` ignores because those tool classes are still untyped.

Current working tree is dirty. Notable changed areas:

- `framework/multi_agent/`: session retention, communication tracker, pool dispatch, tools, subagent service.
- `framework/ioc/`: app config and descriptor memory/tool policy.
- `framework/agents/react/`, `framework/core/emitter.py`: runtime guard fixes found by tests.
- `examples/bot_project/`: bot service wiring, tool cleanup, config adaptation.
- `tests/`: unit, integration, and e2e tests migrated away from legacy `spawn_subagent`.

Untracked file present before/around this work:

- `docs/superpowers/specs/2026-05-21-subagent-refactoring-design.md`

No current diff is present for `examples/bot_project/config/mcp.json`.

## Verification Already Run

### Passing

```powershell
python -m pytest tests/unit/multi_agent/test_core_runtime.py tests/unit/multi_agent/test_tools_enhanced_validation.py -q
```

Result: `54 passed`.

```powershell
python -m pytest tests/integration/multi_agent/test_bus_pool_e2e.py -q
```

Result: `2 passed`.

```powershell
python -m pytest tests/integration/test_qq_bot_service.py -q
```

Result after fixes: `13 passed, 2 skipped`.

```powershell
python -m pytest tests/e2e/multi_agent/test_qq_bot_subagent.py -q
```

Result after e2e rewrite: `2 passed`.

```powershell
python -m pytest tests/unit/ioc/test_descriptor_factory.py examples/bot_project/tests/test_pending_memory_config.py -q
```

Result after compatibility fix: `6 passed`.

```powershell
python -m pytest tests/unit/multi_agent/test_core_runtime.py tests/unit/multi_agent/test_tools_enhanced_validation.py tests/unit/ioc/test_descriptor_factory.py examples/bot_project/tests/test_pending_memory_config.py tests/integration/multi_agent/test_bus_pool_e2e.py tests/integration/test_qq_bot_service.py tests/e2e/multi_agent/test_qq_bot_subagent.py -q
```

Result: `78 passed, 2 skipped`.

Follow-up combined targeted regression:

```powershell
python -m pytest tests/unit/multi_agent/test_core_runtime.py tests/unit/multi_agent/test_tools_enhanced_validation.py tests/unit/ioc/test_descriptor_factory.py examples/bot_project/tests/test_pending_memory_config.py tests/integration/multi_agent/test_bus_pool_e2e.py tests/integration/test_qq_bot_service.py tests/e2e/multi_agent/test_qq_bot_subagent.py -q
```

Result after follow-up fixes: `85 passed, 1 skipped`.

Focused follow-up regressions:

```powershell
python -m pytest tests/unit/multi_agent/test_tools_enhanced_validation.py::TestSendMessageAsyncInvocationRouting tests/unit/multi_agent/test_tools_enhanced_validation.py::TestDispatchTaskTool -q
```

Result: `11 passed`.

```powershell
python -m pytest tests/unit/multi_agent/test_core_runtime.py::test_agent_pool_tracks_and_caps_invocation_sessions tests/unit/multi_agent/test_core_runtime.py::test_agent_pool_session_cap_evicts_lru_after_touching_oldest tests/unit/multi_agent/test_core_runtime.py::test_agent_pool_injects_communication_sideband_metadata tests/unit/multi_agent/test_core_runtime.py::test_subagent_service_admit_dynamic_namespaces_descriptor -q
```

Result: `4 passed`.

```powershell
git -C F:\tool\pythonProject\ModexAgent diff --check
```

Result: passed. Only CRLF warnings were emitted.

Follow-up lint/type checks:

```powershell
$env:RUFF_CACHE_DIR='F:\tool\pythonProject\ModexAgent\.ruff_cache'
ruff check F:\tool\pythonProject\ModexAgent\framework\multi_agent F:\tool\pythonProject\ModexAgent\framework\pipeline\context_assembler.py F:\tool\pythonProject\ModexAgent\framework\ioc\factories\descriptors.py --select I,F,W293
```

Result: passed.

```powershell
mypy F:\tool\pythonProject\ModexAgent\framework\multi_agent\comm_tracker.py F:\tool\pythonProject\ModexAgent\framework\multi_agent\pool.py F:\tool\pythonProject\ModexAgent\framework\multi_agent\tools.py F:\tool\pythonProject\ModexAgent\framework\multi_agent\subagent_service.py F:\tool\pythonProject\ModexAgent\framework\pipeline\context_assembler.py F:\tool\pythonProject\ModexAgent\framework\ioc\factories\descriptors.py
```

Result: passed.

Config parse check was also run:

```powershell
python -c "from framework.ioc.configs.app import AppConfig; cfg=AppConfig.from_yaml(r'F:\tool\pythonProject\ModexAgent\examples\bot_project\config\bot_config.yml'); print(cfg.multi_agent.session_retention.max_sessions_per_subagent)"
```

Result: printed `10`.

### Not Counted As Clean Verification

Full `ruff check` on touched files with the repo's default rule set surfaced a large number of type-annotation (`ANN*`) violations, many from pre-existing test/example style. It also showed some fixable import/unused/blank-line issues.

A targeted `ruff check ... --select I,F,W293 --fix` was attempted, but the command output included Ruff cache/path warnings from the shell environment. Do not treat that attempt as a reliable clean lint pass. Re-run lint with an explicit workspace cache path before completion.

## Fixes Already Covered By Tests

- `SubagentService.create_and_wait()` no longer passes the wrong argument count to `pop_sync_future()`.
- `SpawnSubagentTool` legacy bot tool was removed from bot_project; bot now exposes `dispatch_task` plus `send_message_async`.
- Bot pool mode now wires `CommunicationTracker`, `SessionRetentionPolicy`, and retention config from `AppConfig`.
- Subagent sessions are tracked in `AgentPool` and capped by default at 10 per subagent.
- `CommunicationTracker` now:
  - records pending sends by owner agent,
  - acknowledges replies into the owner digest,
  - closes pending received brackets when the same owner sends a reply with matching `invocation_id`.
- Subagent memory config uses session memory plus session-scoped archive, no knowledge layer.
- Main memory now defaults to a real memory system instead of creating `MemorySystemContextManager(memory_system=None)`.
- ReAct runtime now preserves `context.identity` and guards `END` state writes when no ReAct state exists.
- `StreamingAwareEmitter` tolerates adapters without `streaming_mode`.
- QQ bot integration tests were migrated from legacy `spawn_subagent` expectations to current pool/dispatch behavior.

## Remaining Verification

Run full lint or the repo's agreed stricter subset:

```powershell
ruff check framework/ tests/
```

Expected risk: existing `ANN*` type-safety findings may require either broad cleanup or a narrower agreed rule selection.

Run broader type checking:

```powershell
mypy framework/multi_agent framework/ioc examples/bot_project/bot
```

The narrower touched-file mypy command has already passed.

Run broader test suites:

```powershell
python -m pytest tests/unit/ -q
python -m pytest tests/integration/ -q
python -m pytest examples/bot_project/tests/ -q
```

If environment allows external dependencies, also run the marked integration command:

```powershell
python -m pytest tests/integration/ -v -m integration
```

## Remaining Code Review

- Review all docs still mentioning `SubagentManager`, `SpawnSubagentTool`, or `spawn_subagent`; code path is migrated, but docs are stale.
- Review `framework/multi_agent/AGENTS.md` and `examples/bot_project/AGENTS.md`; both still describe legacy files/tools.
- Review `framework/multi_agent/inbox/producer.py` comment mentioning `SubagentManager`.
- Confirm `denied_tools=["spawn_subagent", "send_message", "dispatch_task"]` is the intended descriptor-level policy for subagents while allowing manually registered `send_message_async` to parent.
- Inspect `AgentPool` session cleanup behavior under broader real runtime load. Current tests cover task-request cap, LRU-after-touch, sideband injection, and default-vs-invocation session routing.
- Add or expand tests for:
  - communication tracker prompt section injection into pool dispatch,
  - `DispatchTaskTool` pending send record,
  - `SendMessageAsyncTool` closing pending received records with matching UUID,
  - compression not removing communication sideband memory,
  - dynamic subagent and resident subagent isolation staying separate.
- Manually exercise a pool-mode bot flow:
  - main calls `dispatch_task`,
  - subagent receives prompt with `invocation_id`,
  - subagent replies through `send_message_async(invocation_id=...)`,
  - main pending communication is acknowledged.

## Suggested Next Checkpoint

Before finalizing, run the full targeted regression again after lint/type fixes:

```powershell
python -m pytest tests/unit/multi_agent/test_core_runtime.py tests/unit/multi_agent/test_tools_enhanced_validation.py tests/unit/ioc/test_descriptor_factory.py examples/bot_project/tests/test_pending_memory_config.py tests/integration/multi_agent/test_bus_pool_e2e.py tests/integration/test_qq_bot_service.py tests/e2e/multi_agent/test_qq_bot_subagent.py -q
git -C F:\tool\pythonProject\ModexAgent diff --check
```
