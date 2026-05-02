<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# sandbox

## Purpose
Sandboxed code execution — isolates tool execution from the agent runtime. Multiple adapter backends.

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `adapters/` | Sandbox adapters — `LocalPython`, `E2B`, `Docker`, `Subprocess` |

## For AI Agents

### Working In This Directory
- All sandbox adapters implement a common sandbox interface
- `LocalPython`: in-process sandbox
- `E2B`: cloud-based sandbox (requires `E2B_API_KEY`)
- `Docker`: container-based isolation
- `Subprocess`: process-level isolation
## Current Runtime Status

Sandbox tool execution is invoked from the ReAct tool node path. Tool timeout is
owned by ReAct tool execution/runtime safety policy, not by the default bot
interceptor chain. See `docs/current-runtime.md`.
