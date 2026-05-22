# Unified Session ID and Agent Communication Design

> 2026-05-22 | Design revision based on current code and product intent

## 1. Goal

Unify agent session ownership and inter-agent communication so that:

- Session IDs identify the receiver agent's context, never the sender.
- Task-scoped subagents get isolated invocation sessions.
- Normal agents use one stable session per conversation.
- The LLM no longer chooses among legacy communication tools.
- `bot_project` exposes only the async send-to-agent tool.
- Old communication tool design is removed, not kept as a compatibility surface.

This design keeps the current resident-agent model. A `SUBAGENT` can be resident
and persistent. `SUBAGENT` means task-scoped communication and session routing,
not ephemeral lifecycle and not a memory policy.

This is a breaking cleanup. The implementation should replace the old
communication/session design completely, clean up old code paths and tests, and
finish with `examples/bot_project` correctly adapted and usable.

## 2. Current Problems

### 2.1 Session semantics are spread across code paths

Current code is partly converged on `{conversation_id}:{agent_name}`:

- `DefaultSessionIdStrategy.agent_session()` already returns `{conv}:{agent}`.
- `target_session()` currently defaults to receiver-owned sessions.
- `dispatch_task` already creates `{conv}:{target}:{invocation_id}` sessions.

The remaining problem is not only session string format. The real problem is
that session semantics are still split across:

- `SessionIdStrategy.main_session()` and `target_session()`.
- `framework.multi_agent.utils` peer-pair helpers.
- `DispatchTaskTool` hardcoded task-session logic.
- `SendMessageAsyncTool.invocation_session_targets`.
- `AgentMessageEnvelope.payload["invocation_id"]` and `correlation_id`.
- `AgentPool` dispatch branches for task requests, agent messages, and results.

### 2.2 Three LLM-facing communication tools leak framework concerns

Current tools:

- `send_message`
- `send_message_async`
- `dispatch_task`

These are different transport/session primitives, not concepts the LLM should
choose among. The LLM should express intent: send content to an agent, optionally
with a task uuid. The framework should handle routing.

The old tools should not remain as compatibility APIs. If useful pieces remain,
they should move behind an internal communication service.

### 2.3 Invocation identity is not first-class

`invocation_id` currently appears in payloads and sometimes in `correlation_id`.
This makes it easy to leak task identity into the wrong session, especially
when a subagent replies to `main`.

Invocation identity must become a first-class routing field and be interpreted
with explicit semantics.

## 3. Core Concepts

### 3.1 AgentCommKind

Add a communication/session kind:

```python
class AgentCommKind(StrEnum):
    NORMAL = "normal"
    SUBAGENT = "subagent"
```

Meaning:

- `NORMAL`: one stable session per conversation and agent.
- `SUBAGENT`: task-scoped sessions. Every delivered task session has a uuid.

Important: `SUBAGENT` does not mean "temporary memory", does not mean
"non-resident", and does not decide whether memory is persistent or ephemeral.
It means "communication and session routing are scoped by task uuid".

Configuration mapping:

| Agent config role | AgentCommKind | Meaning |
| --- | --- | --- |
| `main` | `NORMAL` | user-facing main agent |
| `peer` | `NORMAL` | resident peer with stable conversation session |
| `subagent` | `SUBAGENT` | task-scoped agent with uuid sessions |

`examples/bot_project` should treat `office-expert` and `query-12306` as
`SUBAGENT`. They must still have memory. Their default intended memory is
persistent session/archive memory scoped by the complete task session id:

```text
{conversation_id}:{agent_name}:{uuid}
```

Ephemeral memory is a separate implementation option for some future or
specialized subagent. It must not be represented by `AgentCommKind`; it belongs
in existing context/memory strategy fields or a separate memory configuration.

### 3.2 AgentSessionMeta

Add framework-maintained metadata to `AgentContext`.

```python
@dataclass(frozen=True)
class AgentSessionMeta:
    conversation_id: str
    agent_name: str
    comm_kind: AgentCommKind
    uuid: str | None = None
```

Add it to `AgentContext`:

```python
@dataclass
class AgentContext:
    ...
    session_meta: AgentSessionMeta | None = None
```

Rules:

- The framework populates `session_meta` when creating a turn.
- The LLM does not provide `conversation_id`, `agent_name`, or `comm_kind`.
- Tools read session metadata from `AgentContext`.
- For `NORMAL`, `uuid` is always `None`.
- For a running `SUBAGENT` task session, `uuid` is the current task uuid.
- `session_meta.comm_kind` is not a memory lifetime selector.

The existing `current_conversation_id` contextvar can be removed after migration
or kept only as a short-lived bridge while every dispatch path is converted to
`AgentSessionMeta`.

## 4. Unified Session ID Format

Use one owner-session format:

```text
{conversation_id}:{agent_name}[:{uuid}]
```

Meaning:

- `conversation_id`: external conversation scope.
- `agent_name`: the agent that owns this context. This is always the receiver.
- `uuid`: task id for `SUBAGENT` sessions only.

Examples:

| Agent kind | Session id |
| --- | --- |
| `NORMAL` main | `user123:main` |
| `NORMAL` peer | `user123:reviewer` |
| `SUBAGENT` office task | `user123:office-expert:a1b2c3d4e5f6` |
| `SUBAGENT` 12306 task | `user123:query-12306:bb77ee003311` |

Sender information belongs in `AgentMessageEnvelope.source`, not in the
session id.

### 4.1 SessionIdStrategy

Replace the current strategy surface with:

```python
class SessionIdStrategy(ABC):
    @abstractmethod
    def agent_session(self, conversation_id: str, agent_name: str) -> str:
        ...

    def task_session(
        self,
        conversation_id: str,
        agent_name: str,
        uuid: str,
    ) -> str:
        return f"{self.agent_session(conversation_id, agent_name)}:{uuid}"

    def parse(self, session_id: str) -> AgentSessionParts:
        ...
```

Suggested parsed type:

```python
@dataclass(frozen=True)
class AgentSessionParts:
    conversation_id: str
    agent_name: str | None
    uuid: str | None = None
```

Remove:

- `main_session()`
- `target_session()`
- `format_peer_session_id()`
- `parse_peer_session_id()`
- `is_peer_session_id()`
- `reverse_peer_session_id()`

`agent_session(conversation_id, "main")` replaces `main_session()`.

## 5. Communication API

### 5.1 New LLM-facing tools

Introduce two high-level tools:

- `SendToAgentTool`: synchronous broker/wakeup delivery.
- `SendToAgentAsyncTool`: inbox-based async delivery.

Both use the same routing semantics. `bot_project` registers only
`SendToAgentAsyncTool`.

The old tools are removed from the LLM-facing tool set:

- remove `SendMessageTool`
- remove `SendMessageAsyncTool`
- remove `DispatchTaskTool`

If their lower-level behavior is still useful, extract it into an internal
communication service rather than preserving old tool classes.

### 5.2 LLM-facing parameters

Use `uuid`, not `invocation_id`, as the tool parameter name.

```python
parameters = {
    "target_agent": {
        "type": "string",
        "description": "Name of the target agent.",
    },
    "content": {
        "type": "string",
        "description": "Message content.",
    },
    "uuid": {
        "type": ["string", "null"],
        "description": (
            "Routing selector. Use null for normal-agent delivery. "
            "Use an empty string to start a new subagent task. "
            "Use a concrete uuid to continue an existing subagent task."
        ),
    },
}
```

Required:

- `target_agent`
- `content`
- `uuid`

The `uuid` field is required because omission is ambiguous. The LLM must make
the intended routing explicit:

| `uuid` value | Meaning |
| --- | --- |
| `None` / JSON `null` | send to a `NORMAL` agent |
| `""` | create a new `SUBAGENT` task session |
| `"a1b2c3d4e5f6"` | send to an existing `SUBAGENT` task session |

The framework must not reinterpret these values. In particular, `""` always
means "create a new subagent task", even when the caller is already running in
a subagent task session. When a subagent replies to a normal parent such as
`main`, the tool call uses `uuid=None`; the framework preserves the caller's
current task uuid on the outgoing envelope for tracking.

### 5.3 Target kind validation

The tool must validate `uuid` against the target's `AgentCommKind`.

| Target kind | `uuid=None` | `uuid=""` | `uuid="abc"` |
| --- | --- | --- | --- |
| `NORMAL` | valid | error | error |
| `SUBAGENT` | error | valid, create new task | valid, route existing task |

This is intentionally strict. It prevents silent task id loss and prevents
normal-agent sessions from accidentally carrying task identifiers.

### 5.4 Dynamic tool description

The tool description must list available targets and their kind.

Example:

```text
Available agents:
- main: normal
- reviewer: normal
- office-expert: subagent
- query-12306: subagent
```

Prompt guidance:

```text
Use uuid=null when sending to a normal agent.
Use uuid="" when starting a new task for a subagent.
Use uuid="<existing uuid>" when continuing a subagent task.
When replying from a subagent to a normal parent about the current task, use
uuid=null. The framework keeps the current task uuid on the envelope metadata.
```

## 6. Internal Communication Service

Introduce an internal service so tool classes do not duplicate routing logic.

```python
class AgentCommunicationService:
    async def send_sync(
        self,
        *,
        source: AgentAddress,
        target_agent: str,
        content: str,
        uuid: str | None,
        context: AgentContext,
    ) -> AgentSendResult:
        ...

    async def send_async(
        self,
        *,
        source: AgentAddress,
        target_agent: str,
        content: str,
        uuid: str | None,
        context: AgentContext,
    ) -> AgentSendResult:
        ...
```

The service owns:

- reading `AgentSessionMeta`
- looking up target `AgentCommKind`
- validating `uuid`
- creating task uuids
- building session ids
- building envelopes
- recording communication tracker events
- choosing broker or inbox delivery based on sync/async method

Suggested result type:

```python
@dataclass(frozen=True)
class AgentSendResult:
    target_agent: str
    target_kind: AgentCommKind
    session_id: str
    uuid: str | None
    created_new_task: bool
```

Tool classes become thin wrappers around the service.

## 7. Routing Rules

### 7.1 NORMAL target

Input:

```text
target_agent = "main"
uuid = None
```

Route:

```text
session_id = {conversation_id}:main
envelope.uuid = None
```

Return:

```text
Message sent to main.
```

### 7.2 SUBAGENT new task

Input:

```text
target_agent = "office-expert"
uuid = ""
```

Route:

```text
new_uuid = uuid4().hex[:12]
session_id = {conversation_id}:office-expert:{new_uuid}
envelope.uuid = new_uuid
```

Return:

```text
Message sent to office-expert. uuid: a1b2c3d4e5f6
```

### 7.3 SUBAGENT existing task

Input:

```text
target_agent = "office-expert"
uuid = "a1b2c3d4e5f6"
```

Route:

```text
session_id = {conversation_id}:office-expert:a1b2c3d4e5f6
envelope.uuid = a1b2c3d4e5f6
```

Return:

```text
Message sent to office-expert. uuid: a1b2c3d4e5f6
```

### 7.4 SUBAGENT reply to NORMAL main

Input:

```text
caller session_meta.uuid = "a1b2c3d4e5f6"
target_agent = "main"
uuid = None
```

Route:

```text
session_id = {conversation_id}:main
envelope.uuid = a1b2c3d4e5f6
```

The target is normal, so its session id does not include the uuid. The envelope
still carries the uuid as reply metadata so `CommunicationTracker` can match the
task response.

Return:

```text
Message sent to main.
```

## 8. AgentMessageEnvelope

Make task uuid first-class.

```python
@dataclass
class AgentMessageEnvelope:
    payload: dict[str, Any]
    source: AgentAddress
    target: AgentAddress | None
    message_type: str
    conversation_id: str
    agent_session_id: str
    uuid: str | None = None
    message_id: str = field(default_factory=lambda: uuid4().hex)
    correlation_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    hop_count: int = 0
    in_reply_to: str | None = None
```

Serialization:

- `uuid` is written to broker headers and inbox metadata.
- `from_broker_message()` restores `uuid`.
- During migration, payload key `invocation_id` may be read only as a legacy
  fallback in tests or data produced before the migration. New code must write
  `uuid`.

## 9. AgentPool and Pipeline Changes

### 9.1 Populate AgentSessionMeta

Every dispatched turn must populate `AgentContext.session_meta`.

For an external user message to main:

```python
AgentSessionMeta(
    conversation_id=input_msg.session_id,
    agent_name="main",
    comm_kind=AgentCommKind.NORMAL,
    uuid=None,
)
```

For a normal resident agent:

```python
AgentSessionMeta(
    conversation_id=conversation_id,
    agent_name=target_agent,
    comm_kind=AgentCommKind.NORMAL,
    uuid=None,
)
```

For a subagent task:

```python
AgentSessionMeta(
    conversation_id=conversation_id,
    agent_name=target_agent,
    comm_kind=AgentCommKind.SUBAGENT,
    uuid=envelope.uuid,
)
```

### 9.2 AgentPool dispatch

`AgentPool` should use parsed session parts and descriptor `comm_kind`, not
payload conventions.

Rules:

- `agent_session_id` is always receiver-owned.
- dynamic session tracking uses `uuid is not None`, not payload keys.
- `_send_subagent_result()` sends to the parent normal session but preserves
  `uuid` on the envelope.
- inbox keys remain the target agent's session id.

### 9.3 Pipeline

`AgentPipeline._build_runtime_and_context()` receives enough metadata to set
`AgentSessionMeta`. This may require passing target descriptor information from
`AgentPool` or including normalized metadata on `InputMessage`.

The framework should avoid exposing `conversation_id` as a tool parameter. It is
framework-owned metadata.

## 10. bot_project Behavior

`examples/bot_project` should:

- register only `SendToAgentAsyncTool` for LLM use.
- stop registering `send_message`, `send_message_async`, and `dispatch_task`.
- mark `main` as `NORMAL`.
- mark `office-expert` as `SUBAGENT`.
- mark `query-12306` as `SUBAGENT`.
- show target kinds in the tool schema/description.
- keep subagent memory persistent at the full task-session scope.

Subagent memory for `office-expert` and `query-12306` should be session/archive
memory scoped by:

```text
{conversation_id}:{agent_name}:{uuid}
```

This lets each task keep its own persistent working history and compressed
archive without mixing unrelated invocations.

## 11. Files to Change

| File | Change |
| --- | --- |
| `framework/multi_agent/comm_kind.py` | New `AgentCommKind` enum |
| `framework/multi_agent/descriptor.py` | Add `comm_kind` to `AgentDescriptor` |
| `framework/core/agent.py` | Add `AgentSessionMeta`; add `session_meta` to `AgentContext` |
| `framework/multi_agent/session_id.py` | Remove `main_session` and `target_session`; add task session parsing |
| `framework/multi_agent/utils.py` | Remove peer-pair session helpers |
| `framework/multi_agent/envelope.py` | Add first-class `uuid` field |
| `framework/multi_agent/communication.py` | New internal `AgentCommunicationService` |
| `framework/multi_agent/tools.py` | Replace old three tools with `SendToAgentTool` and `SendToAgentAsyncTool` |
| `framework/multi_agent/pool.py` | Route by owner session and `AgentCommKind`; populate session metadata |
| `framework/multi_agent/subagent_service.py` | Use task session and `uuid` terminology |
| `framework/pipeline/pipeline.py` | Populate `AgentSessionMeta` on contexts |
| `framework/messaging/broker_bridge.py` | Pass through first-class `uuid` |
| `framework/multi_agent/bus.py` | Preserve first-class `uuid` through inbox |
| `framework/multi_agent/__init__.py` | Export new enum, service, and tools |
| `examples/bot_project/bot/service/builders.py` | Register only async send-to-agent for bot_project |
| `examples/bot_project/bot/service/core.py` | Use new descriptor/session metadata setup |
| `examples/bot_project/config/bot_config.yml` | Ensure office and 12306 are modeled as subagents |

## 12. Removals

Remove old LLM-facing tools:

- `SendMessageTool`
- `SendMessageAsyncTool`
- `DispatchTaskTool`

Remove old session helpers:

- `format_peer_session_id`
- `parse_peer_session_id`
- `is_peer_session_id`
- `reverse_peer_session_id`

Remove old strategy methods:

- `main_session`
- `target_session`

If any old behavior is still needed internally, reimplement it behind
`AgentCommunicationService` with the new session and uuid semantics.

## 13. Breaking Migration Notes

This is a breaking internal cleanup. Tests should be updated to the new API
rather than preserving old tool behavior. The implementation should not keep
compatibility wrappers for the old LLM-facing tools or old session strategy
surface.

Allowed only while editing a single patch series:

- temporary local helper code may exist during implementation, but it must not
  remain in the final committed design.
- tests should be rewritten to the new semantics rather than asserting old
  behavior.
- runtime data written before this change is not guaranteed to resume if it
  depends on legacy payload fields.

Do not leave both old and new LLM tools registered. That would recreate the tool
selection problem this design removes.

Final implementation state:

- no old communication tools are exported or registered.
- no old peer-pair session helpers remain.
- no `main_session()` or `target_session()` strategy methods remain.
- new first-class `uuid` is used throughout routing, envelopes, bus, broker
  bridge, pool dispatch, and tracker matching.
- `examples/bot_project` starts and works with the new async send-to-agent tool.

## 14. Verification Matrix

| # | Caller | Target kind | uuid argument | Expected session | Expected result |
| --- | --- | --- | --- | --- | --- |
| 1 | main | NORMAL main/reviewer | `None` | `conv:target` | sent, no uuid in return |
| 2 | main | SUBAGENT office | `""` | `conv:office:<new>` | sent, new uuid returned |
| 3 | main | SUBAGENT office | `"abc123"` | `conv:office:abc123` | sent, same uuid returned |
| 4 | subagent office | NORMAL main | `None` | `conv:main` | sent, uuid preserved in envelope |
| 5 | subagent office | SUBAGENT 12306 | `""` | `conv:query-12306:<new>` | sent, new uuid returned |
| 6 | any | NORMAL target | `""` | none | parameter error |
| 7 | any | NORMAL target | `"abc123"` | none | parameter error |
| 8 | any | SUBAGENT target | `None` | none | parameter error |
| 9 | any | nonexistent | any | none | agent-not-found error |

## 15. Test Plan

Add or update tests for:

- `SessionIdStrategy` parsing two-part and three-part sessions.
- `AgentMessageEnvelope` serializes and restores `uuid`.
- `SendToAgentAsyncTool` validates uuid semantics by target kind.
- `SendToAgentTool` uses the same validation as async.
- new task sends create raw `uuid4().hex[:12]` values with no `inv_` prefix.
- normal sends reject empty or concrete uuid.
- subagent sends reject `None`.
- subagent reply to main routes to `conv:main` and preserves uuid on envelope.
- `AgentPool` dynamic session tracking uses first-class uuid.
- `CommunicationTracker` matches replies using envelope uuid.
- `bot_project` registers only async send-to-agent.
- `office-expert` and `query-12306` use task-scoped persistent memory.
- every subagent has a configured memory strategy; ephemeral memory is allowed
  only as a separate memory/context strategy, not through `AgentCommKind`.
- old tools and peer-pair helpers are no longer imported or exported.

Run:

```text
pytest tests/unit/multi_agent/ -v
pytest examples/bot_project/tests/ -v
pytest tests/unit/pipeline/ -v
ruff check framework/ examples/bot_project/ tests/
mypy framework/
```

## 16. Non-Goals

- Do not change the resident agent lifecycle model.
- Do not remove `AgentPool`.
- Do not change star topology.
- Do not redesign memory layers.
- Do not require subagent memory to be ephemeral.
- Do not encode memory persistence or ephemerality in `AgentCommKind`.
- Do not expose `conversation_id` as an LLM tool parameter.
- Do not keep old communication tools as compatibility APIs.
