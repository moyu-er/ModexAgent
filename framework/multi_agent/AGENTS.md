<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-06 -->

# multi_agent

## Purpose
Multi-agent orchestration — factory, pool, inbox, subagent management, and star-topology communication. Supports both Pipeline (per-agent `AgentPipeline`) and Pool (resident agents via `BrokerBridgeService`) runtime modes.

## Key Files
| File | Description |
|------|-------------|
| `factory.py` | `AgentFactory` ABC, `DefaultAgentFactory` — creates `AgentInstance` with full wiring |
| `pool.py` | `AgentPool` — manages resident agents with `BrokerBridgeService` |
| `subagent_manager.py` | `SubagentManager` — spawn/spawn_and_wait/cancel subagent lifecycle |
| `hooks.py` | `TaskProgressHook` — per-session task progress reporting to `TaskEventBus` |
| `descriptor.py` | `AgentDescriptor`, `AgentInstance` — agent configuration and instance |
| `router.py` | `AgentMessageRouter` — routes messages to target agents |
| `envelope.py` | `AgentMessageEnvelope` — typed message format for agent communication |
| `peer_validator.py` | `PeerAgentValidator` — enforces star-topology (peers through main only) |
| `address.py` | `AgentAddress` — agent addressing (kind + name) |
| `coordinator.py` | `InMemoryTaskCoordinator`, `NullTaskCoordinator` — task coordination |
| `event_bus.py` | `TaskEventBus`, `CompositeTaskEventReporter` — event reporting |
| `bus.py` | Agent message bus |
| `registry.py` | Agent registry |
| `state.py` | Agent state management |
| `session_id.py` | Session ID strategy |
| `tools.py` | `SendMessageTool` — agent-to-agent messaging tool |
| `utils.py` | Support utilities |

## Moved Components
The following components were previously in this package but have been relocated:

| Component | New Location |
|-----------|-------------|
| `FilteredToolManager` | `framework/tools/filter.py` |
| `SkillWhitelistFilter` (was `AgentSkillManager`) | `framework/core/skills/filter.py` |
| `MultiAgentContextBuilder` | `framework/utils/context_builder.py` |
| `MessageDeduplicator` | `framework/utils/deduplicator.py` |
| `ContentSanitizer` | `framework/utils/sanitizer.py` |
| `TaskSupervisor`, `TimeoutSupervisionPolicy` | `framework/control/task_supervision.py` |
| `SupervisionPolicyRegistry` | `framework/control/policy_registry.py` |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `inbox/` | MQ system — `LocalFileInboxServer`, `Producer`, `Consumer`, `InboxFlushHook`, `Tracker` |

## For AI Agents

### Working In This Directory
- `DefaultAgentFactory.create_agent()` is the main entry for agent construction
- Per-agent `InterceptorChain` and `HookRunner` copies created to prevent cross-agent state leakage
- Factory resolves `context_strategy` (ephemeral/persistent) and `execution_strategy` (react/pipeline)
- Star topology: peers communicate only through main agent (`PeerAgentValidator` enforces)
- Auto-injected `InboxFlushHook` for pipeline-mode agents
- `SpawnSubagentTool` for dynamic subagent creation

### Testing Requirements
- Tests in `tests/unit/multi_agent/`
- Test session isolation across agent instances
- Test inbox producer/consumer/tracker/flush-hook

## Dependencies

### Internal
- All `framework.*` sub-packages
- `framework.agents.react` — `ReActAgentBuilder`

## Current Runtime Status

Pool and multi-agent code may share hook/interceptor instances. Keep per-turn
state in `ctx.metadata` and follow the current ReAct runtime boundaries in
`docs/current-runtime.md`.
