<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-22 | Updated: 2026-05-22 -->

# multi_agent

## Purpose
Star-topology multi-agent orchestration. All agents live in `AgentPool` — no separate execution queues. Supports Pipeline mode (per-agent `AgentPipeline`) and Pool mode (resident agents with `BrokerBridgeService`).

## Key Files
| File | Description |
|------|-------------|
| `pool.py` | `AgentPool(AgentRegistry)` — resident agent lifecycle, consumer loop, inbox wakeup polling, per-session locks, TTL+LRU session eviction |
| `subagent_service.py` | `SubagentService` — resident (startup), dynamic (runtime), sync (`create_and_wait`) subagent lifecycle |
| `factory.py` | `AgentFactory` ABC, `DefaultAgentFactory` — creates `AgentInstance` with full wiring |
| `descriptor.py` | `AgentDescriptor`, `AgentInstance`, `AgentLLMConfig`, `ContextGovernanceConfig` |
| `tools.py` | `SendMessageTool` (sync broker), `SendMessageAsyncTool` (inbox-based async), `DispatchTaskTool` (isolated invocation session) |
| `comm_tracker.py` | `CommunicationTracker` — sideband memory for send/ack bracket matching, prevents memory compression from dropping pending comms |
| `bus.py` | `AgentMessageBus` ABC, `LocalAgentMessageBus` — producer+consumer message bus with `send()` (wakeup) and `send_silent()` (inbox-only) |
| `envelope.py` | `AgentMessageEnvelope` — typed message wrapper with source/target/conversation_id/hop_count |
| `address.py` | `AgentAddress` — agent identity (kind, name, capabilities) |
| `session_id.py` | `SessionIdStrategy` ABC, `DefaultSessionIdStrategy` — encodes session as `{conversation_id}:{agent_name}` |
| `router.py` | `AgentMessageRouter` ABC, `DefaultMeshRouter` — input message → agent session routing |
| `registry.py` | `AgentRegistry` ABC, `AgentProfile` — agent discovery with capability/skill/tool filtering |
| `hooks.py` | `TaskProgressHook` — event reporting for subagent task progress |
| `peer_validator.py` | `SubagentAgentValidator` — enforces star topology at registration |
| `event_bus.py` | `TaskEventBus`, `TaskEventReporter` — task lifecycle event reporting |
| `coordinator.py` | `TaskCoordinator` ABC, `InMemoryTaskCoordinator`, `NullTaskCoordinator` |
| `context.py` | `current_conversation_id` context var — set per-turn by pipeline, read by communication tools |
| `state.py` | `AgentState` enum (INITIALIZING/IDLE/WORKING/ERROR/SHUTTING_DOWN/SHUTDOWN) |
| `utils.py` | Session ID formatting/parsing helpers |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `inbox/` | Message queue — `InboxServer` ABC + file/memory implementations, `InboxProducer` (send+dedup), `InboxConsumer` (consume+dedup), `TrackerServer` |

## Communication Architecture

```
Agent A (LLM calls tool)
  → SendMessageAsyncTool.execute()
    → AgentMessageBus.send_silent(inbox_key, envelope)   # persist to inbox
    → wakeup task (delayed broker signal if still pending)
      → BrokerMessage to AgentPool
        → AgentPool._consume_messages()
          → _handle_inbox_wakeup() → InboxConsumer.consume()
            → _dispatch_agent_message() → AgentPipeline.process_message()
              → Agent runs, result sent back via _send_subagent_result()
```

Three communication tools for LLM-facing use:
- `send_message` — direct broker delivery, immediate target wakeup. No inbox, no invocation tracking.
- `send_message_async` — inbox delivery + delayed wakeup. Supports `invocation_id` for session-scoped routing. `CommunicationTracker` records send/ack brackets.
- `dispatch_task` — creates isolated `{conv_id}:{agent}:{inv_id}` session, sends as `task_request`. Returns `invocation_id` for follow-up via `send_message_async`.

## For AI Agents
- `DefaultAgentFactory.create_agent()` is the main entry for agent construction
- Per-agent `InterceptorChain` and `HookRunner` copies prevent cross-agent state leakage
- Star topology enforced at registration by `SubagentAgentValidator`
- `SubagentAutoSendHook` auto-forwards final output to parent if LLM forgets communication tools
- `CommunicationTracker` is in-memory only (not persisted); provides `build_prompt_section()` for system prompt injection
- `AgentPool._poll_inbox_for_idle_agents()` runs background polling to ensure IDLE agents process pending inbox messages
- Session eviction: TTL-based + per-subagent LRU cap via `SessionRetentionPolicy`
- `current_conversation_id` context var must be set per-turn; communication tools read it for routing
