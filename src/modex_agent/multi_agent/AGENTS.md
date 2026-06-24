<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# multi_agent

## Purpose

Star-topology multi-agent orchestration. All agents live in `AgentPool`; there are no separate execution queues. Pool mode uses resident agents with `BrokerBridgeService`, inbox wakeups, per-session locks, and TTL/LRU cleanup. Provides communication primitives (`AgentMessageBus`, `CommunicationTracker`), subagent lifecycle management, and framework-layer message routing.

## Key Files

| File | Description |
|------|-------------|
| `pool.py` | `AgentPool` — resident agent lifecycle, consumer loop, inbox wakeup polling, per-session locks, task-session eviction |
| `pool_reuse.py` | `SubagentPool` — LRU instance reuse for dynamic subagents; `send_to_agent` to a subagent type routes here |
| `bus.py` | `AgentMessageBus`, `LocalAgentMessageBus` — inbox persistence and wakeup signaling |
| `communication.py` | `AgentCommunicationService` — central target validation, session id construction, envelope construction, sync/async delivery |
| `comm_tracker.py` | `CommunicationTracker` — sideband communication tracker for send/ack bracket matching |
| `comm_kind.py` | `AgentCommKind` — communication/session topology kind (NORMAL/SUBAGENT), topology only |
| `tools.py` | `SendToAgentTool`, `CommunicationTargetStore`, `CommunicationTarget` |
| `router.py` | `DefaultMeshRouter` — session identity resolved via `InputMessage.session` (no string parsing) |
| `envelope.py` | `AgentMessageEnvelope` — source, target, conversation id, session id, invocation id |
| `descriptor.py` | `AgentDescriptor`, `AgentInstance`, `AgentLLMConfig` — agent metadata + `AgentCommKind` integration |
| `factory.py` | Agent instance factory — creates `AgentInstance` and injects descriptor metadata into pipelines |
| `hooks.py` | Multi-agent lifecycle hooks — e.g. `SubagentAutoSendHook` (safety net for orphaned messages) |
| `subagent_validator.py` | Framework-layer subagent constraint validation — star-topology enforcement at registration |
| `template.py` / `template_registry.py` | `AgentTemplate` (preset definition for dynamically-created subagents) + `AgentTemplateRegistry` (scans/loads per-pool templates) |
| `state.py` | Enum state types for multi-agent coordination |
| `address.py` | Agent addressing dataclasses |
| `message_xml.py` | XML serialization for agent messages |
| `registry.py` / `utils.py` | Registry utilities + shared helpers |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `inbox/` | Inbox subsystem — `InboxServer` ABC + `LocalInboxServer`/`MemoryInboxServer`, `InboxProducer`/`InboxConsumer` (local-cache dedup), `InboxTracker` — deferred async delivery + wakeup signaling |

## Communication Contract

- `AgentCommKind.NORMAL`: one stable receiver session per conversation.
- `AgentCommKind.SUBAGENT`: task-scoped receiver sessions with `invocation_id`.
- Session id format: `{conversation_id}:{agent_name}[:{invocation_id}]`.
- `send_to_agent` is the single communication tool exposed to the LLM. It accepts `target_agent`, `content`, and a nullable `invocation_id`. The framework routes the call through the broker, the async inbox, or an isolated subagent session depending on target state — this is not visible as separate LLM tools.
- `invocation_id=None` targets a normal agent.
- `invocation_id=""` creates a new subagent task session.
- A concrete `invocation_id` continues an existing subagent task session.

The old `send_message`, `send_message_async`, and `dispatch_task` tools are removed. Do not add compatibility wrappers for them.

## Routing Boundary

Session id assembly belongs in routing/communication layers:

- `DefaultMeshRouter` builds fallback main-agent sessions for external input.
- `AgentCommunicationService` builds target sessions for inter-agent messages.
- Inbox/bus layers persist messages under the complete session key they receive.
- `AgentPipeline` uses the provided complete session id for locking and memory scope.

## For AI Agents

- All multi-agent modes use `AgentPool` with resident agents; there is no separate queue-per-agent model.
- Subagents are dynamically created/destroyed; use `SubagentPool` (LRU) for reuse efficiency.
- Per-session locks prevent concurrent execution within the same conversation session.
- `SubagentAutoSendHook` acts as a safety net — it forwards unacknowledged subagent output to the parent.
- The `inbox/` layer provides deferred delivery: messages can be queued when the target agent is busy.
- Star-topology is enforced by `subagent_validator.py` at registration time — subagents cannot spawn subagents.

## Dependencies

- `framework.core.agent` — `Agent[E]`, `ContentEmitter[E]` for agent lifecycle
- `framework.runtime` — `AgentRuntime`, `TurnStateStore` for per-agent runtime state
- `framework.pipeline` — `AgentPipeline` for execution orchestration
- `framework.messaging` — `MessageBroker`, `BrokerBridgeService` for inter-agent message transport
- `framework.hook` — `HookRunner` for lifecycle hooks
