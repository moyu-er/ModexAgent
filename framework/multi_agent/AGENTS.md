<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-10 -->

# multi_agent

## Purpose

Star-topology multi-agent orchestration. All agents live in `AgentPool`; there
are no separate execution queues. Pool mode uses resident agents with
`BrokerBridgeService`, inbox wakeups, per-session locks, and TTL/LRU cleanup.

## Key Files

| File | Description |
| --- | --- |
| `pool.py` | Resident agent lifecycle, consumer loop, inbox wakeup polling, per-session locks, task-session eviction |
| `communication.py` | `AgentCommunicationService`; central target validation, session id construction, envelope construction, sync/async delivery |
| `tools.py` | `SendToAgentTool`, `CommunicationTargetStore`, `CommunicationTargetsProvider` |
| `router.py` | `DefaultMeshRouter`; session identity resolved via `InputMessage.session` (no string parsing) |
| `comm_tracker.py` | Sideband communication tracker for send/ack bracket matching |
| `bus.py` | `AgentMessageBus`, `LocalAgentMessageBus`; inbox persistence and wakeup signaling |
| `envelope.py` | `AgentMessageEnvelope` with source, target, conversation id, session id, invocation id |
| `descriptor.py` | `AgentDescriptor`, `AgentInstance`, `AgentLLMConfig`, `AgentCommKind` integration |
| `factory.py` | Creates `AgentInstance` and injects descriptor metadata into pipelines |

## Communication Contract

- `AgentCommKind.NORMAL`: one stable receiver session per conversation.
- `AgentCommKind.SUBAGENT`: task-scoped receiver sessions with `invocation_id`.
- Session id format: `{conversation_id}:{agent_name}[:{invocation_id}]`.
- `send_to_agent` and `send_to_agent_async` accept `target_agent`, `content`, and required nullable `invocation_id`.
- `invocation_id=None` targets a normal agent.
- `invocation_id=""` creates a new subagent task session.
- A concrete `invocation_id` continues an existing subagent task session.

The old `send_message`, `send_message_async`, and `dispatch_task` tools are
removed. Do not add compatibility wrappers for them.

## Routing Boundary

Session id assembly belongs in routing/communication layers:

- `DefaultMeshRouter` builds fallback main-agent sessions for external input.
- `AgentCommunicationService` builds target sessions for inter-agent messages.
- Inbox/bus layers persist messages under the complete session key they receive.
- `AgentPipeline` uses the provided complete session id for locking and memory scope.
