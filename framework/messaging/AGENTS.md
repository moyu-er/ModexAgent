<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-16 -->

# messaging

## Purpose
Message broker and bridge service for agent communication. Supports pub/sub pattern and star-topology inter-agent messaging.

## Key Files
| File | Description |
|------|-------------|
| `broker.py` | `MessageBroker` ABC — pub/sub messaging backbone |
| `broker_memory.py` | `InMemoryMessageBroker` — lightweight `asyncio.Queue` implementation |
| `broker_bridge.py` | `BrokerBridgeService` — adapter-to-broker bridge for pool mode |

## For AI Agents
- `MessageBroker` ABC defines the pub/sub contract; `InMemoryMessageBroker` is the default implementation
- `BrokerBridgeService` connects agents via broker in pool mode
- Runtime control messages should pass through the control plane, not embedded here
