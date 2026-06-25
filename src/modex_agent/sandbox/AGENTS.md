<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# sandbox

## Purpose

Sandboxed code execution — isolates tool execution from the agent runtime via multiple adapter backends. Provides a full guard chain (command pattern, path traversal, device, network) and environment sanitization, though these are currently unwired from the live tool-execution path.

## Key Files

| File | Description |
|------|-------------|
| `factory.py` | `SandboxFactory` — creates sandbox instances by configuration |
| `config.py` | `SandboxConfig` — adapter selection, resource limits, and backend-specific options |
| `types.py` | Sandbox result and execution types — `SandboxResult`, `SandboxExecution`, `SandboxResourceLimits` |
| `enums.py` | `SandboxBackend` enum + status enums |
| `exceptions.py` | `SandboxError`, `SandboxTimeoutError`, `SandboxExecutionError`, `SandboxConnectionError` |
| `isolation.py` | Isolation manager — execution boundary enforcement and sandbox lifecycle |
| `platform.py` | Platform detection (OS, capabilities) for sandbox backend selection |
| `validation.py` | Input/output validation before execution — schema checks and sanitization |
| `docker_utils.py` | Docker image and container utilities — pull, build, cleanup |
| `env_builder.py` | `EnvironmentBuilder` — sanitized environment dicts excluding secrets, preserving OS-critical entries (`EnvBuilderConfig`, `EnvPolicy`) |
| `workspace_policy.py` | `WorkspacePolicy` — workspace boundary enforcement for file paths (`WorkspacePolicyConfig`) |
| `guard.py` | `CommandPatternGuard` — regex deny/allow rules to block dangerous command patterns (`CommandSeverity`, `GuardMatch`, `GuardResult`) |
| `guard_path.py` | Command-string path boundary guard — extracts absolute paths, checks they stay within workspace root |
| `guard_traversal.py` | `PathTraversalGuard` — path-traversal detection (`PathTraversalConfig`) |
| `guard_device.py` | Device-path allowlist — `BENIGN_DEVICE_PATHS`, `is_benign_device_path()` |
| `guard_network.py` | Network guard — private-IP detection and URL validation to prevent SSRF |
| `guard_pipeline.py` | `GuardPipeline` — composite guard running multiple guards in sequence |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `adapters/` | Backend implementations — `base.py` (ABC), `docker.py` (Docker containers), `e2b.py` (E2B cloud sandboxes), `subprocess.py` (local subprocess), `landlock.py` (Linux Landlock LSM) |

## Status

The guards/policies above (command pattern, path boundary, traversal, device, network, sanitized env) are fully implemented and exported from `sandbox/__init__.py`, but **not wired into the live tool-execution path**: no code in `modex_agent/tools`, `modex_agent/agents`, or `examples/bot_project/bot` imports `modex_agent.sandbox`. They are available for integration but currently inert. The root `framework/AGENTS.md` "Known Gaps" (command content, workspace boundary, environment isolation) therefore still describe the shipped runtime — the guards to close those gaps exist here but are not connected.

## For AI Agents

- The sandbox subsystem is a **standalone library** — it must be explicitly wired into the tool execution chain.
- `SandboxFactory.create(config)` returns the appropriate backend based on `SandboxConfig.adapter`.
- `GuardPipeline.run_all(guards, context)` runs the full guard chain and returns the first failure.
- To integrate: inject a `GuardPipeline` into `ToolNode` or the tool execution layer, and configure `EnvironmentBuilder` for subprocess environments.
- `docker_utils.py` provides Docker-specific utilities but the Docker adapter itself lives in `adapters/docker.py`.
- E2B adapter requires `E2B_API_KEY` environment variable; Docker requires a running Docker daemon.
- Sandbox timeout is owned by ReAct tool execution, not the bot interceptor chain.

## Dependencies

- `modex_agent.core` — base types and exceptions
- `modex_agent.tools` — potential integration point for guarded tool execution (currently unwired)
- External: `docker` SDK (Docker adapter), `e2b` SDK (E2B adapter), `landlock` (Linux-only)
