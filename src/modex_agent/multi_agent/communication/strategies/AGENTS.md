<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-12 | Updated: 2026-07-12 -->

# strategies

`SendStrategy` ABC + three concrete strategy classes, one per routing
topology. Each strategy owns its full vertical slice: session construction,
invocation_id semantics, envelope shape, delivery target, and result shaping.

## Purpose

Isolate the three routing topologies (subagent dispatch, parent reply,
peer-normal) into independently testable classes. The `SendStrategy.execute`
template method fixes the orchestration sequence; concrete strategies
override individual hooks (`normalize_invocation_id`, `build_session`,
`build_envelope`, `deliver`, `build_result`).

## Key Files

| File | Description |
|------|-------------|
| `base.py` | `SendStrategy` ABC + `SendDeps`/`SendRequest` frozen dataclasses + `SendStrategyKind` StrEnum. The `execute` template method (normalize → session → register → envelope → deliver → build_result). Shared helpers: `_resolve_source`, `_deliver` (bus or broker fallback), `_subagent_output_path`/`_subagent_trace_dir`. |
| `subagent_dispatch.py` | `SubagentDispatchStrategy` — NORMAL→SUBAGENT. Mints fresh invocation_id when none provided, creates task-scoped subagent session (`create_with_prefix(prefix=invocation_id, parent=sender)`), builds `TASK_REQUEST` envelope, surfaces invocation_id in ack, registers session in sender's `SessionRegistry`. `build_result` selects ack field shape per `req.target.execution_strategy` (ADR-0025 D5 runtime per-target site): native targets get trace/output paths appended; external targets (ADR-0027) get a trimmed result without them. |
| `parent_reply.py` | `ParentReplyStrategy` — SUBAGENT→parent NORMAL. Reuses parent session (via `parent_session_id`), builds `AGENT_MESSAGE` envelope, hides invocation_id from ack. Fallback for in-pool NORMAL→NORMAL (effectively unreachable in v1 — each pool has one main agent). |
| `peer_normal.py` | `PeerNormalStrategy` — NORMAL→peer-NORMAL cross-pool (ADR-0019). Reuses sender's session prefix as receiver's prefix (root session, no parent), hides invocation_id from ack and XML, delivers to `target.bus_ref` (peer pool's bus) with fallback to local bus, uses `build_agent_comm_message` with `SourceLabel.PEER_AGENT` + `reply_contract` block, marks result `is_peer_send=True`. |
| `__init__.py` | Re-exports all public types. |

## SendStrategy Contract

```python
class SendStrategy(ABC):
    def __init__(self, deps: SendDeps) -> None: ...

    async def execute(self, req: SendRequest) -> AgentSendResult:
        # Template method (final-shaped):
        # 1. invocation_id = self.normalize_invocation_id(req)
        # 2. session = self.build_session(req, invocation_id)
        # 3. if self.should_register_session(): register
        # 4. envelope = self.build_envelope(req, session, invocation_id)
        # 5. err = await self.deliver(envelope, target)
        # 6. return self.build_result(req, session, invocation_id)

    @abstractmethod
    def normalize_invocation_id(self, req: SendRequest) -> str | None: ...
    @abstractmethod
    def build_session(self, req: SendRequest, invocation_id: str) -> SessionInfo: ...
    @abstractmethod
    def build_envelope(self, req, session, invocation_id) -> AgentMessageEnvelope: ...

    async def deliver(self, env, target) -> str | None: ...  # default: local bus
    def should_register_session(self) -> bool: ...            # default: False
    def result_invocation_id(self, invocation_id) -> str | None: ...  # default: pass-through
    def build_result(self, req, session, invocation_id) -> AgentSendResult: ...
```

## Strategy Comparison

| Aspect | SubagentDispatch | ParentReply | PeerNormal |
|---|---|---|---|
| Sender kind | NORMAL | SUBAGENT | NORMAL |
| Target kind | SUBAGENT | NORMAL | NORMAL (with `bus_ref`) |
| Session | fresh `prefix=invocation_id`, parent=sender | reuse `parent_session_id` | `prefix=sender_prefix`, no parent (root) |
| Register session | True (sender's pool) | False | False (receiver's poller registers) |
| invocation_id in ack | surfaced | hidden (None) | hidden (None) |
| invocation_id in message | surfaced | hidden | hidden |
| message_type | `TASK_REQUEST` | `AGENT_MESSAGE` | `AGENT_MESSAGE` |
| Message builder | `build_agent_comm_message` (AGENT) | `build_agent_comm_message` (AGENT) | `build_agent_comm_message` (PEER_AGENT + reply_contract) |
| Delivery | local bus | local bus | `target.bus_ref` (fallback: local bus) |
| Result flags | `created_new_task`, trace/output paths | — | `is_peer_send=True` |

## For AI Agents

### Working In This Directory
- Each strategy is **independently testable** with mocked `SendDeps` — verify
  observable outputs (envelope shape, session shape, result fields), not
  internal method call counts.
- `execute` is the only orchestration entry point — do not call `build_session`
  /`build_envelope`/`deliver` directly from outside the strategy.
- To add a new routing topology: add a `SendStrategyKind` enum value, a
  `SendStrategy` subclass, and one dispatch branch in `service._send`.
- `PeerNormalStrategy` passes `source_label=SourceLabel.PEER_AGENT` and a
  `reply_contract` to `build_agent_comm_message` because peer receivers need
  an explicit reply-contract block telling them they MUST call `send_to_agent`
  to reply (their normal output is invisible to the sender). Dispatch and
  parent-reply strategies omit the `reply_contract` for native targets
  (SubagentAutoSendHook delivers replies automatically).
- `should_register_session` defaults to `False` — peer-normal sessions are
  registered by the **receiver's** `InboxPoller._ensure_session_registered`,
  not by the sender.

### Common Patterns
- `SendDeps` is a frozen dataclass shared across all strategies — pass it to
  the base `__init__`.
- `SendRequest` is the input bundle (target, content, invocation_id, context).
- `_resolve_source(req)` reads `context.session.agent_name` with fallback to
  `deps.source`.
- `_deliver(env)` is the default delivery: `bus.send` when wired, else
  `broker.send_to` fallback. PeerNormal overrides `deliver` to use `target.bus_ref`.

## Dependencies

### Internal
- `modex_agent.multi_agent.communication.result` — `AgentSendResult`
- `modex_agent.multi_agent.communication.topology` — `TopologyPolicy` (called by service before dispatch)
- `modex_agent.multi_agent.envelope` — `AgentMessageEnvelope`
- `modex_agent.multi_agent.message_format` — `build_dispatch_message`, `build_agent_comm_message`, `SourceLabel`, `ResultMeta`
- `modex_agent.multi_agent.message_type` — `AgentMessageType`
- `modex_agent.multi_agent.address` — `AgentAddress`
- `modex_agent.multi_agent.tools` — `CommunicationTarget`
- `modex_agent.core.agent` — `AgentCommKind`, `AgentContext`
- `modex_agent.core.session_id` — `SessionIdFactory`, `SessionInfo`
- `modex_agent.core.session_registry` — `SessionRegistry`
- `modex_agent.messaging.broker` — `MessageBroker`
- `modex_agent.multi_agent.bus` — `AgentMessageBus`
- `modex_agent.multi_agent.workspace_paths` — `WorkspacePathResolver`

<!-- MANUAL -->
