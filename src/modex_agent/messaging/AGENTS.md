<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-09-02 -->

# messaging

## Purpose
Message transport models, broker, and bridge service for agent communication. Owns `InputMessage`, `OutputMessage`, their transport enums, approval-decision input transport, and the `Address` / `AddressKind` addressing types shared across the framework.

## Key Files
| File | Description |
|------|-------------|
| `models.py` | Typed transport boundary: `InputMessage`, `OutputMessage`, `MessageType`, `OutputMessageType`, `ReminderKind`, `ApprovalAction`, `ApprovalDecisionInput`, `BrokerInputPayload`, and `BrokerOutputPayload`. |
| `broker.py` | `MessageBroker` ABC + `InMemoryMessageBroker` — pub/sub backbone. Also defines `Address` (frozen Pydantic `BaseModel` with `kind: AddressKind` + `name: str`, B5B), `AddressKind` StrEnum (`AGENT`, `USER`, `CHANNEL`, `SYSTEM`, `GROUP`), and `BrokerMessage` (BaseModel, mutable runtime envelope, B5B). `AddressKind` compares equal to its string value, so existing `kind="agent"` call sites work but new code should use the enum (type-safety rule 1). |
| `broker_memory.py` | `InMemoryMessageBroker` — lightweight `asyncio.Queue` implementation |
| `broker_bridge.py` | `BrokerBridgeService` + `BrokerInputAdapter` / `BrokerOutputAdapter` — adapter-to-broker bridge for pool mode; `OutputRoute` for routing agent output to the right channel |

## For AI Agents
- `MessageBroker` ABC defines the pub/sub contract; `InMemoryMessageBroker` is the default implementation
- `BrokerBridgeService` connects agents via broker in pool mode
- Import `InputMessage`, `OutputMessage`, and approval input DTOs from `modex_agent.messaging`; pipeline and approval do not own these transport values
- `Address.kind` is typed `AddressKind` — use `AddressKind.AGENT` / `.CHANNEL` / `.SYSTEM` etc. instead of raw strings
- Runtime control messages should pass through the control plane (`modex_agent.control`), not embedded here

## Dependencies
- `modex_agent.core.media` — `Attachment` carried by input/output transport models
- `modex_agent.core.message` — `ContentFormat` used by `InputMessage`
- `modex_agent.core.session_id` — `SessionInfo` and `SessionIdFactory`
- `asyncio.Queue` — backing transport for `InMemoryMessageBroker`

<!-- MANUAL: -->
