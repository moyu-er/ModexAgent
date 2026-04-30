<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

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
| `filtered_tool_manager.py` | `FilteredToolManager` — allowed/denied tool list per agent |
| `peer_validator.py` | `PeerAgentValidator` — enforces star-topology (peers through main only) |
| `agent_skill_manager.py` | `AgentSkillManager` — per-agent skill set management |
| `context_builder.py` | `MultiAgentContextBuilder` — builds agent context from message history |
| `address.py` | `AgentAddress` — agent addressing (kind + name) |
| `commands.py` | Agent control command types |
| `coordinator.py`, `deduplicator.py`, `discovery.py`, `policy_registry.py` | Mesh coordination |
| `governance.py`, `intervention.py`, `sanitizer.py` | Message governance |
| `event_bus.py`, `bus.py` | Task event bus and agent message bus |
| `assembly_kit.py`, `registry.py` | Assembly helpers |
| `rpc_broker.py`, `state.py` | RPC and state management |
| `session_id.py`, `tools.py`, `toolset.py`, `utils.py` | Support utilities |

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
