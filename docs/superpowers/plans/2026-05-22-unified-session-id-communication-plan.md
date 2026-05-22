# Unified Session ID Communication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current split `send_message` / `send_message_async` / `dispatch_task` communication model with a unified `send_to_agent` abstraction that uses receiver-owned session IDs and explicit `AgentCommKind` + `uuid` routing semantics.

**Architecture:** Introduce a first-class communication kind on agent descriptors, runtime session metadata on `AgentContext`, a unified session ID strategy, UUID propagation on envelopes and inbox metadata, and one internal `AgentCommunicationService` shared by sync and async tools. Remove old dispatch-task abstractions instead of preserving compatibility.

**Tech Stack:** Python 3.12, dataclasses, existing `framework.core` typed structures, `framework.multi_agent` broker/inbox/pool stack, pytest, ruff, mypy.

---

## Source Documents

- Spec: [2026-05-22-unified-session-id-communication-design.md](../specs/2026-05-22-unified-session-id-communication-design.md)
- Progress tracker: [2026-05-22-unified-session-id-communication-status.md](2026-05-22-unified-session-id-communication-status.md)
- Primary example: `examples/bot_project/`

## Implementation Principles

- This is a breaking migration. Do not keep `SendMessageTool`, `SendMessageAsyncTool`, `DispatchTaskTool`, `invocation_id`, or peer-pair helpers as compatibility shims.
- `AgentCommKind` expresses communication/session topology only:
  - `NORMAL`: resident receiver session, no task UUID.
  - `SUBAGENT`: task-scoped receiver session, requires UUID semantics.
- Memory persistence is not encoded in `AgentCommKind`. Persistent and ephemeral memory are memory configuration choices.
- All agents, including subagents, have memory. `office-expert` and `query-12306` are `SUBAGENT` targets in `bot_project` and should keep session/archive memory scoped by full session ID.
- Unified receiver-owned session ID format:
  - NORMAL: `{conversation_id}:{agent_name}`
  - SUBAGENT: `{conversation_id}:{agent_name}:{uuid}`
- Tool UUID semantics:
  - `uuid = None`: send to `NORMAL`.
  - `uuid = ""`: create a new `SUBAGENT` task session.
  - `uuid = "<value>"`: route to an existing `SUBAGENT` task session.
- Tool descriptions must list available target agents and whether each target is `normal` or `subagent`.

## File Structure

Create:

- `framework/multi_agent/comm_kind.py`
- `framework/multi_agent/communication.py`
- `tests/unit/multi_agent/test_comm_kind_session_id.py`
- `tests/unit/multi_agent/test_communication_service.py`
- `tests/unit/multi_agent/test_send_to_agent_tools.py`

Modify:

- `framework/core/agent.py`
- `framework/multi_agent/descriptor.py`
- `framework/multi_agent/registry.py`
- `framework/multi_agent/session_id.py`
- `framework/multi_agent/envelope.py`
- `framework/multi_agent/bus.py`
- `framework/multi_agent/inbox/producer.py`
- `framework/messaging/broker_bridge.py`
- `framework/multi_agent/tools.py`
- `framework/multi_agent/pool.py`
- `framework/multi_agent/subagent_service.py`
- `framework/multi_agent/subagent_auto_send_hook.py`
- `framework/multi_agent/__init__.py`
- `framework/ioc/configs/agent.py`
- `framework/ioc/factories/descriptors.py`
- `examples/bot_project/bot/service/builders.py`
- `examples/bot_project/config/bot_config.yml`
- Existing tests under `tests/unit/multi_agent/`, `tests/unit/messaging/`, and `examples/bot_project/tests/`

Remove or empty after replacing imports:

- `framework/multi_agent/utils.py` peer-pair helpers
- Tests whose only purpose is old `dispatch_task` / `invocation_id` compatibility

## Task 1: Add Communication Kind and Session Metadata

- [ ] Add `AgentCommKind` in `framework/multi_agent/comm_kind.py`.

```python
from __future__ import annotations

from enum import StrEnum


class AgentCommKind(StrEnum):
    NORMAL = "normal"
    SUBAGENT = "subagent"
```

- [ ] Add session metadata to `framework/core/agent.py`.

```python
@dataclass(frozen=True)
class AgentSessionMeta:
    conversation_id: str
    agent_name: str
    comm_kind: AgentCommKind
    uuid: str | None = None

    @property
    def session_id(self) -> str:
        return DefaultSessionIdStrategy().format(
            conversation_id=self.conversation_id,
            agent_name=self.agent_name,
            uuid=self.uuid,
        )
```

- [ ] Extend `AgentContext` with `session_meta: AgentSessionMeta | None = None`.
- [ ] Keep `conversation_id` on `AgentContext` during the migration step only if existing callers still need it inside this task. The final code should read full session identity from `ctx.session_meta`.
- [ ] Add `comm_kind: AgentCommKind = AgentCommKind.NORMAL` to `AgentDescriptor` in `framework/multi_agent/descriptor.py`.
- [ ] Add `comm_kind: AgentCommKind = AgentCommKind.NORMAL` to `AgentProfile` in `framework/multi_agent/registry.py`.
- [ ] Update descriptor/profile creation so agent role maps to kind:
  - main/default/peer roles become `AgentCommKind.NORMAL`.
  - subagent role becomes `AgentCommKind.SUBAGENT`.
- [ ] Write `tests/unit/multi_agent/test_comm_kind_session_id.py` covering enum values and descriptor/profile defaults.

Validation command:

```powershell
Set-Location -LiteralPath 'F:\tool\pythonProject\ModexAgent'
python -m pytest tests/unit/multi_agent/test_comm_kind_session_id.py -q
```

Expected result before implementation: failing imports or missing attributes.
Expected result after implementation: all tests pass.

Commit after this task:

```powershell
git -C F:\tool\pythonProject\ModexAgent add framework/core/agent.py framework/multi_agent/comm_kind.py framework/multi_agent/descriptor.py framework/multi_agent/registry.py framework/ioc/configs/agent.py framework/ioc/factories/descriptors.py tests/unit/multi_agent/test_comm_kind_session_id.py
git -C F:\tool\pythonProject\ModexAgent commit -m "feat: add agent communication kind metadata"
```

Update the progress tracker after committing.

## Task 2: Replace Session ID Strategy With Unified Receiver Sessions

- [ ] Replace `SessionIdStrategy.main_session()`, `agent_session()`, and `target_session()` with a single format/parse API in `framework/multi_agent/session_id.py`.

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AgentSessionParts:
    conversation_id: str
    agent_name: str
    uuid: str | None = None


class SessionIdStrategy(Protocol):
    def format(
        self,
        *,
        conversation_id: str,
        agent_name: str,
        uuid: str | None = None,
    ) -> str:
        raise NotImplementedError

    def parse(self, session_id: str) -> AgentSessionParts:
        raise NotImplementedError


class DefaultSessionIdStrategy:
    def format(
        self,
        *,
        conversation_id: str,
        agent_name: str,
        uuid: str | None = None,
    ) -> str:
        if not conversation_id:
            raise ValueError("conversation_id is required")
        if not agent_name:
            raise ValueError("agent_name is required")
        if uuid is None:
            return f"{conversation_id}:{agent_name}"
        if not uuid:
            raise ValueError("uuid must be non-empty when provided")
        return f"{conversation_id}:{agent_name}:{uuid}"

    def parse(self, session_id: str) -> AgentSessionParts:
        parts = session_id.split(":")
        if len(parts) == 2:
            conversation_id, agent_name = parts
            uuid = None
        elif len(parts) == 3:
            conversation_id, agent_name, uuid = parts
        else:
            raise ValueError(f"Invalid agent session id: {session_id!r}")
        if not conversation_id or not agent_name:
            raise ValueError(f"Invalid agent session id: {session_id!r}")
        if uuid == "":
            raise ValueError(f"Invalid agent session id: {session_id!r}")
        return AgentSessionParts(
            conversation_id=conversation_id,
            agent_name=agent_name,
            uuid=uuid,
        )
```

- [ ] Update all callers of old session strategy methods.
- [ ] Remove peer-pair session formatting/parsing helpers from `framework/multi_agent/utils.py`.
- [ ] Replace tests that assert `{conversation}:{source}:{target}` peer-pair formats with receiver-owned session tests.
- [ ] Add tests for:
  - normal session formatting.
  - subagent session formatting.
  - parse round trips.
  - empty UUID rejected at session strategy level.
  - tool-level empty UUID remains allowed only as "create new subagent session" input before UUID generation.

Validation command:

```powershell
Set-Location -LiteralPath 'F:\tool\pythonProject\ModexAgent'
python -m pytest tests/unit/multi_agent/test_comm_kind_session_id.py -q
```

Commit after this task:

```powershell
git -C F:\tool\pythonProject\ModexAgent add framework/multi_agent/session_id.py framework/multi_agent/utils.py tests/unit/multi_agent/test_comm_kind_session_id.py
git -C F:\tool\pythonProject\ModexAgent commit -m "feat: unify multi-agent session ids"
```

Update the progress tracker after committing.

## Task 3: Propagate UUID Through Envelopes, Inbox, and Broker Bridge

- [ ] Add `uuid: str | None = None` to `AgentMessageEnvelope` in `framework/multi_agent/envelope.py`.
- [ ] Include `uuid` in envelope serialization/deserialization paths.
- [ ] Update `framework/multi_agent/bus.py` so inbox metadata includes `uuid` and restores it when consuming.
- [ ] Update `framework/multi_agent/inbox/producer.py` if metadata is built there.
- [ ] Update `framework/messaging/broker_bridge.py` to carry `uuid` between broker messages and envelopes.
- [ ] Replace remaining `invocation_id` payload use with `uuid` on the envelope.
- [ ] Add tests covering async round trip and broker bridge round trip.

Test cases to add:

```python
def test_envelope_preserves_uuid_in_metadata_round_trip() -> None:
    envelope = AgentMessageEnvelope(
        payload={"content": "hello"},
        source="main",
        target="office-expert",
        conversation_id="conv-1",
        agent_session_id="conv-1:office-expert:task-1",
        uuid="task-1",
    )

    assert envelope.uuid == "task-1"
```

Validation commands:

```powershell
Set-Location -LiteralPath 'F:\tool\pythonProject\ModexAgent'
python -m pytest tests/unit/multi_agent -q
python -m pytest tests/unit/messaging -q
```

Commit after this task:

```powershell
git -C F:\tool\pythonProject\ModexAgent add framework/multi_agent/envelope.py framework/multi_agent/bus.py framework/multi_agent/inbox/producer.py framework/messaging/broker_bridge.py tests/unit/multi_agent tests/unit/messaging
git -C F:\tool\pythonProject\ModexAgent commit -m "feat: propagate agent task uuid in messages"
```

Update the progress tracker after committing.

## Task 4: Add Internal AgentCommunicationService

- [ ] Create `framework/multi_agent/communication.py`.
- [ ] Move all target validation, UUID semantics, session ID construction, and sync/async send behavior into this service.
- [ ] Service inputs:
  - caller `AgentContext`
  - target agent name
  - message content
  - `uuid: str | None`
  - mode: sync or async
- [ ] Service outputs:
  - sync: target response text or structured result already used by broker callers.
  - async: queued message acknowledgement containing target, session ID, and UUID.
- [ ] Generate new UUID only when target kind is `SUBAGENT` and tool input `uuid == ""`.
- [ ] Use the generated UUID in both envelope `uuid` and receiver-owned `agent_session_id`.
- [ ] Reject invalid combinations with precise user-facing errors:
  - target kind `NORMAL`, uuid is `""`.
  - target kind `NORMAL`, uuid is non-empty string.
  - target kind `SUBAGENT`, uuid is `None`.
  - unknown target.
  - missing `ctx.session_meta`.
- [ ] Use `AgentRegistry`/descriptor data for target kind lookup. Do not infer subagent behavior from target name.
- [ ] Add tests in `tests/unit/multi_agent/test_communication_service.py`.

Required test matrix:

| Target kind | Tool uuid | Result |
| --- | --- | --- |
| NORMAL | `None` | routes to `{conversation}:{agent}` |
| NORMAL | `""` | validation error |
| NORMAL | `"abc"` | validation error |
| SUBAGENT | `None` | validation error |
| SUBAGENT | `""` | generates UUID and routes to `{conversation}:{agent}:{uuid}` |
| SUBAGENT | `"abc"` | routes to `{conversation}:{agent}:abc` |

Validation command:

```powershell
Set-Location -LiteralPath 'F:\tool\pythonProject\ModexAgent'
python -m pytest tests/unit/multi_agent/test_communication_service.py -q
```

Commit after this task:

```powershell
git -C F:\tool\pythonProject\ModexAgent add framework/multi_agent/communication.py tests/unit/multi_agent/test_communication_service.py
git -C F:\tool\pythonProject\ModexAgent commit -m "feat: centralize agent communication routing"
```

Update the progress tracker after committing.

## Task 5: Replace LLM Tools With send_to_agent

- [ ] Replace old tool classes in `framework/multi_agent/tools.py`.
- [ ] Remove `SendMessageTool`, `SendMessageAsyncTool`, and `DispatchTaskTool`.
- [ ] Add `SendToAgentTool` and `SendToAgentAsyncTool`.
- [ ] Both tools call `AgentCommunicationService`.
- [ ] Both tools expose the same LLM-facing parameter contract:
  - `target_agent: str`
  - `message: str`
  - `uuid: str | None`
- [ ] Tool schema must make `uuid` present and nullable. The description must state:
  - Use `null` for normal agents.
  - Use `""` to start a new subagent task.
  - Use a previous UUID to continue a subagent task.
- [ ] Tool description must list targets as `agent-name (normal)` or `agent-name (subagent)`.
- [ ] Async tool response must include UUID for subagent task messages so the LLM can continue the same task.
- [ ] Update exports in `framework/multi_agent/__init__.py`.
- [ ] Update tool tests in `tests/unit/multi_agent/test_send_to_agent_tools.py`.
- [ ] Remove or rewrite tests that assert old tool names.

Example target description output:

```text
Available targets:
- assistant-a (normal)
- office-expert (subagent)
- query-12306 (subagent)
```

Validation command:

```powershell
Set-Location -LiteralPath 'F:\tool\pythonProject\ModexAgent'
python -m pytest tests/unit/multi_agent/test_send_to_agent_tools.py -q
```

Commit after this task:

```powershell
git -C F:\tool\pythonProject\ModexAgent add framework/multi_agent/tools.py framework/multi_agent/__init__.py tests/unit/multi_agent/test_send_to_agent_tools.py tests/unit/multi_agent
git -C F:\tool\pythonProject\ModexAgent commit -m "feat: replace multi-agent tools with send_to_agent"
```

Update the progress tracker after committing.

## Task 6: Update Pool, Pipeline, and Subagent Runtime Metadata

- [ ] Update `framework/pipeline/pipeline.py` to populate `AgentContext.session_meta` for main/root turns.
- [ ] Update `framework/multi_agent/pool.py` so resident sessions and dynamic task sessions parse session IDs through `DefaultSessionIdStrategy`.
- [ ] Replace task routing checks that read `payload["invocation_id"]` with `envelope.uuid`.
- [ ] Update pool dynamic-session tracking so `SUBAGENT` sessions are identified by descriptor/profile `comm_kind` and non-null `uuid`.
- [ ] Update final-result routing from subagent to parent to preserve `uuid`.
- [ ] Update `framework/multi_agent/subagent_service.py` to use unified UUID semantics.
- [ ] Update `framework/multi_agent/subagent_auto_send_hook.py` to call `send_to_agent_async` service path or emit a compatible envelope with UUID.
- [ ] Ensure per-session locks and eviction use the complete receiver-owned session ID.
- [ ] Add regression tests for:
  - parent normal session has `uuid is None`.
  - subagent task session has concrete UUID in `AgentContext.session_meta`.
  - auto-send final output returns to parent with the same UUID.
  - no code path requires `invocation_id`.

Validation commands:

```powershell
Set-Location -LiteralPath 'F:\tool\pythonProject\ModexAgent'
python -m pytest tests/unit/multi_agent/test_core_runtime.py -q
python -m pytest tests/unit/multi_agent/test_subagent_auto_send_hook.py -q
python -m pytest tests/unit/multi_agent -q
```

Commit after this task:

```powershell
git -C F:\tool\pythonProject\ModexAgent add framework/pipeline/pipeline.py framework/multi_agent/pool.py framework/multi_agent/subagent_service.py framework/multi_agent/subagent_auto_send_hook.py tests/unit/multi_agent
git -C F:\tool\pythonProject\ModexAgent commit -m "feat: route subagent sessions by uuid"
```

Update the progress tracker after committing.

## Task 7: Adapt bot_project to Async send_to_agent

- [ ] Update `examples/bot_project/bot/service/builders.py` to register only `SendToAgentAsyncTool` for multi-agent communication.
- [ ] Remove registration of old sync send and dispatch tools from `bot_project`.
- [ ] Keep framework sync `SendToAgentTool` available for other users, but do not register it in `bot_project`.
- [ ] Ensure `office-expert` and `query-12306` descriptors are built as `AgentCommKind.SUBAGENT`.
- [ ] Ensure config remains persistent-memory capable for those subagents:
  - session memory scope uses complete session ID.
  - archive memory, when enabled, also scopes by complete session ID.
- [ ] Update prompts/tool descriptions used by `bot_project` so the main bot knows:
  - `office-expert` is `subagent`.
  - `query-12306` is `subagent`.
  - use `uuid=""` for a new task.
  - reuse returned UUID for follow-up messages to the same task.
- [ ] Add or update bot project tests covering async sends to both subagents.

Validation commands:

```powershell
Set-Location -LiteralPath 'F:\tool\pythonProject\ModexAgent'
python -m pytest examples/bot_project/tests -q
python -m pytest tests/unit/multi_agent -q
```

Commit after this task:

```powershell
git -C F:\tool\pythonProject\ModexAgent add examples/bot_project/bot/service/builders.py examples/bot_project/config/bot_config.yml examples/bot_project/tests framework/ioc/factories/descriptors.py
git -C F:\tool\pythonProject\ModexAgent commit -m "feat: adapt bot project to send_to_agent async"
```

Update the progress tracker after committing.

## Task 8: Remove Old Names and Complete Migration Sweep

- [ ] Search for old API names and remove all production references.

```powershell
Set-Location -LiteralPath 'F:\tool\pythonProject\ModexAgent'
rg "SendMessageTool|SendMessageAsyncTool|DispatchTaskTool|dispatch_task|send_message_async|send_message|invocation_id|format_peer_session_id|parse_peer_session_id|reverse_peer_session_id|is_peer_session_id"
```

- [ ] Any remaining old names must be in:
  - historical spec text.
  - migration notes in the new spec.
  - tests that assert old names are absent.
- [ ] Remove obsolete tests or rewrite them to the new API.
- [ ] Update import barrels and documentation snippets that advertise old tools.
- [ ] Run static checks.

Validation commands:

```powershell
Set-Location -LiteralPath 'F:\tool\pythonProject\ModexAgent'
ruff check framework/ tests/ examples/bot_project/
mypy framework/
python -m pytest tests/unit/ -q
python -m pytest examples/bot_project/tests -q
git diff --check
```

Commit after this task:

```powershell
git -C F:\tool\pythonProject\ModexAgent add framework tests examples docs
git -C F:\tool\pythonProject\ModexAgent commit -m "chore: remove legacy multi-agent communication APIs"
```

Update the progress tracker after committing.

## Task 9: Final Integration Verification

- [ ] Run the end-to-end verification set.

```powershell
Set-Location -LiteralPath 'F:\tool\pythonProject\ModexAgent'
python -m pytest tests/unit/ -q
python -m pytest tests/integration/ -v -m integration
python -m pytest examples/bot_project/tests -q
ruff check framework/ tests/ examples/bot_project/
mypy framework/
git status --short
```

- [ ] Manually inspect `git status --short` before final response. Preserve unrelated user changes.
- [ ] Confirm the status tracker has an entry for each completed task with date and short explanation.
- [ ] Confirm `bot_project` no longer registers old communication tools.
- [ ] Confirm `office-expert` and `query-12306` are advertised as subagent targets.
- [ ] Confirm subagent memory uses full session IDs including UUID.

Final commit:

```powershell
git -C F:\tool\pythonProject\ModexAgent add docs/superpowers/plans/2026-05-22-unified-session-id-communication-status.md
git -C F:\tool\pythonProject\ModexAgent commit -m "docs: update unified communication migration status"
```

## Risk Controls

- Keep framework-generic logic in `framework/`; only business wiring belongs in `examples/bot_project/`.
- Do not use target names to infer communication kind.
- Do not represent memory persistence with `AgentCommKind`.
- Do not store turn/session identity in loose metadata when `AgentSessionMeta` is available.
- Do not split tool-call chains when updating tests around memory or governance.
- Do not reintroduce peer-pair session IDs.
- Do not leave old tools as aliases.

## Completion Criteria

- `SendToAgentTool` and `SendToAgentAsyncTool` are the only LLM-facing multi-agent communication tools.
- `bot_project` registers only the async version.
- `DispatchTaskTool`, `SendMessageTool`, and `SendMessageAsyncTool` are removed from production code.
- `uuid` is a first-class field on communication envelopes and is preserved across broker/inbox paths.
- `AgentContext.session_meta` is populated for normal and subagent turns.
- Session IDs follow `{conversation_id}:{agent_name}` or `{conversation_id}:{agent_name}:{uuid}`.
- `office-expert` and `query-12306` are configured and advertised as `subagent`.
- Persistent subagent memory scopes by full session ID.
- Unit tests, bot project tests, ruff, mypy, and diff checks pass.
