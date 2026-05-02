<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# messaging

## Purpose
Message broker and bridge service for agent communication. Supports pub/sub pattern and star-topology inter-agent messaging.

## Key Files
| File | Description |
|------|-------------|
| `broker.py` | Message broker implementation |
| `broker_memory.py` | In-memory broker |

## For AI Agents

### Working In This Directory
- `MessageBroker`: pub/sub messaging backbone
- `BrokerBridgeService`: connects agents via broker in pool mode
- `InMemoryMessageBroker`: lightweight implementation for testing and single-process use
## Current Runtime Status

Messaging and broker code should pass runtime control messages into the control
plane instead of embedding ReAct-specific handling. Current boundaries are
summarized in `docs/current-runtime.md`.
