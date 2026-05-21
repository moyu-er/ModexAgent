<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-16 -->

# multi_agent

## Purpose
Multi-agent orchestration — factory, pool, inbox, subagent management, and star-topology communication. Supports Pipeline (per-agent `AgentPipeline`) and Pool (resident agents via `BrokerBridgeService`) runtime modes.

## Key Files
| File | Description |
|------|-------------|
| `factory.py` | `AgentFactory` ABC, `DefaultAgentFactory` — creates `AgentInstance` with full wiring |
| `pool.py` | `AgentPool` — manages resident agents with `BrokerBridgeService` |
| `subagent_manager.py` | `SubagentService` — spawn/spawn_and_wait/cancel subagent lifecycle |
| `coordinator.py` | `TaskCoordinator` — task coordination across agents |
| `hooks.py` | `SubagentAutoSendHook`, `SubagentMemoryCleanupHook` |
| `descriptors.py` | `AgentDescriptor`, `build_peer_descriptor` — agent configuration |
| `instances.py` | `AgentInstance` — runtime agent wrapper |
| `state.py` | Agent state types |
| `directory.py` | `AgentDirectory` — agent lookup registry |
| `router.py` | `MessageRouter` — routes messages to target agents |
| `bus.py` | `LocalAgentMessageBus` — producer + consumer message bus |
| `tools.py` | `SendMessageTool`, `SendMessageAsyncTool`, `DispatchTaskTool` |
| `utils.py` | Utility functions |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `inbox/` | MQ system — `LocalFileInboxServer`, `Producer`, `Consumer`, `InboxFlushHook` |

## For AI Agents
- `DefaultAgentFactory.create_agent()` is the main entry for agent construction
- Per-agent `InterceptorChain` and `HookRunner` copies prevent cross-agent state leakage
- Star topology: peers communicate only through main agent (`peer_validator.py` enforces)
- `SubagentAutoSendHook` auto-forwards messages if LLM forgets `send_message_async`
- `SubagentMemoryCleanupHook` cleans session memory on subagent exit
