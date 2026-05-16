<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-16 -->

# session

## Purpose
Request/response session mode — lightweight single-request counterpart to `AgentPipeline`. Designed for HTTP API style usage where each call is an independent request/response cycle.

## Key Files
| File | Description |
|------|-------------|
| `agent_session.py` | `AgentSession` — processes a single message and returns a response |

## For AI Agents
- `AgentSession` is the entry point for request/response usage (vs `AgentPipeline` for long-running services)
- Call `await session.process_message(...)` for each incoming request
- Does not manage its own event loop — caller controls the lifecycle
