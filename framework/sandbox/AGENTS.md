<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-05-31 -->

# sandbox

Sandboxed code execution — isolates tool execution from the agent runtime via multiple adapter backends.

## Key Files

| File | Description |
|------|-------------|
| `factory.py` | `SandboxFactory` — creates sandbox instances by config |
| `config.py` | `SandboxConfig` — adapter selection, resource limits |
| `types.py` | Sandbox result and execution types |
| `enums.py` | `SandboxBackend`, status enums |
| `exceptions.py` | `SandboxError`, `SandboxTimeoutError`, etc. |
| `isolation.py` | Isolation manager for execution boundaries |
| `platform.py` | Platform detection (OS, capabilities) |
| `validation.py` | Input/output validation before execution |
| `docker_utils.py` | Docker image and container utilities |

## Subdirectories

| Directory | Description |
|-----------|-------------|
| `adapters/` | Backend implementations: `base.py` (ABC), `docker.py`, `e2b.py`, `subprocess.py`, `landlock.py` |

## Notes
- Sandbox timeout is owned by ReAct tool execution, not the bot interceptor chain.
- `E2B` adapter requires `E2B_API_KEY`; `Docker` requires a running Docker daemon.
