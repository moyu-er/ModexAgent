<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-07-12 -->

# messaging

## Purpose
Message broker and bridge service for agent communication. Supports pub/sub pattern and inter-agent messaging. Owns the `Address` / `AddressKind` addressing types shared across the framework.

## Key Files
| File | Description |
|------|-------------|
| `broker.py` | `MessageBroker` ABC + `InMemoryMessageBroker` — pub/sub backbone. Also defines `Address` (frozen Pydantic `BaseModel` with `kind: AddressKind` + `name: str`, B5B), `AddressKind` StrEnum (`AGENT`, `USER`, `CHANNEL`, `SYSTEM`, `GROUP`), and `BrokerMessage` (BaseModel, mutable runtime envelope, B5B). `AddressKind` compares equal to its string value, so existing `kind="agent"` call sites work but new code should use the enum (type-safety rule 1). |
| `broker_memory.py` | `InMemoryMessageBroker` — lightweight `asyncio.Queue` implementation |
| `broker_bridge.py` | `BrokerBridgeService` + `BrokerInputAdapter` / `BrokerOutputAdapter` — adapter-to-broker bridge for pool mode; `OutputRoute` for routing agent output to the right channel |

## For AI Agents
- `MessageBroker` ABC defines the pub/sub contract; `InMemoryMessageBroker` is the default implementation
- `BrokerBridgeService` connects agents via broker in pool mode
- `Address.kind` is typed `AddressKind` — use `AddressKind.AGENT` / `.CHANNEL` / `.SYSTEM` etc. instead of raw strings
- Runtime control messages should pass through the control plane (`modex_agent.control`), not embedded here

## Dependencies
- `modex_agent.core.types` — `AgentEvent` types used in broker signatures
- `asyncio.Queue` — backing transport for `InMemoryMessageBroker`

<!-- MANUAL: -->
