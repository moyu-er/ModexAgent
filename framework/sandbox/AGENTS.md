<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

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
| `guard.py` | `CommandPatternGuard` — regex deny/allow rules to block dangerous command patterns (`CommandSeverity`, `GuardMatch`, `GuardResult`) |
| `guard_path.py` | Command-string path boundary guard — extracts absolute paths, checks they stay within workspace root |
| `guard_traversal.py` | `PathTraversalGuard` — path-traversal detection (`PathTraversalConfig`) |
| `guard_device.py` | Device-path allowlist (`BENIGN_DEVICE_PATHS`, `is_benign_device_path`) |
| `guard_pipeline.py` | `GuardPipeline` — composite guard running multiple guards in sequence |
| `env_builder.py` | `EnvironmentBuilder` — sanitized env dicts excluding secrets, preserving OS-critical entries (`EnvBuilderConfig`, `EnvPolicy`) |
| `workspace_policy.py` | `WorkspacePolicy` — workspace boundary enforcement for file paths (`WorkspacePolicyConfig`) |

## Subdirectories

| Directory | Description |
|-----------|-------------|
| `adapters/` | Backend implementations: `base.py` (ABC), `docker.py`, `e2b.py`, `subprocess.py`, `landlock.py` |

## Status

The guards/policies above (command pattern, path boundary, traversal, device,
sanitized env) are fully implemented and exported from `sandbox/__init__.py`,
but **not wired into the live tool-execution path**: no code in `framework/tools`,
`framework/agents`, or `examples/bot_project/bot` imports `framework.sandbox`.
They are available for integration but currently inert. The root
`framework/AGENTS.md` "Known Gaps" (command content, workspace boundary,
environment isolation) therefore still describe the shipped runtime — the
guards to close those gaps exist here but are not connected.

## Notes
- Sandbox timeout is owned by ReAct tool execution, not the bot interceptor chain.
- `E2B` adapter requires `E2B_API_KEY`; `Docker` requires a running Docker daemon.
