<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# adapters

## Purpose

Sandbox backend implementations that execute code and commands in isolated environments. Each adapter wraps a specific isolation technology behind the common `SandboxAdapter` ABC. Supports local subprocess, Docker containers, E2B cloud sandboxes, and Linux Landlock LSM.

## Key Files

| File | Description |
|------|-------------|
| `base.py` | `SandboxAdapter` ABC — defines `execute()` (code), `execute_command()` (shell), `cleanup()`; optional hooks for `CommandPatternGuard`, `EnvironmentBuilder`, `WorkspacePolicy` |
| `subprocess.py` | `SubprocessSandbox` — local subprocess execution with `CommandPatternGuard`, `EnvironmentBuilder`, `WorkspacePolicy`, and `IsolationManager` for OS-level filesystem/network isolation |
| `docker.py` | `DockerSandbox` — Docker container execution with language-specific images (`python:3.11-slim`, `node:18-slim`); supports artifact extraction and command guard |
| `e2b.py` | `E2BSandbox` — E2B cloud sandbox execution with lazy artifact loading (metadata returned immediately, content fetched on-demand); supports auto-download patterns |
| `landlock.py` | `LandlockSandbox` — Linux Landlock LSM (kernel 5.13+) sandbox with `landlock.create_ruleset()` for filesystem access restrictions; Linux-only |
| `__init__.py` | Re-exports `SandboxAdapter`, `SubprocessSandbox`, `DockerSandbox`, `E2BSandbox`, `LandlockSandbox` |

## For AI Agents

### Working In This Directory
- All adapters implement `SandboxAdapter` and are created by `SandboxFactory.create(config)` based on `SandboxConfig.adapter` enum
- Each adapter has an `is_available` property that checks runtime prerequisites (Docker daemon, E2B API key, Linux kernel version, etc.)
- `SubprocessSandbox` is the simplest and always available; it wires up the full guard chain (command pattern, workspace policy, env builder)
- `DockerSandbox` requires `pip install docker` and a running Docker daemon
- `E2BSandbox` requires `pip install e2b_code_interpreter` and `E2B_API_KEY` environment variable
- `LandlockSandbox` requires Linux 5.13+ with Landlock support and `pip install landlock`
- The optional hooks (`_get_command_guard()`, `_get_env_builder()`, `_get_workspace_policy()`) are overridden by local adapters (subprocess, landlock) but return `None` for cloud adapters (docker, e2b) since those rely on container-level isolation

### Common Patterns
- Adaptors implement `async execute(code, language, config)` for code execution and `async execute_command(command, cwd, config)` for shell command execution
- All adapters return `SandboxResult(success, output, error, artifacts, ...)` — never raise on execution errors
- Availability checks are done at call-time, not import-time (lazy)
- Temporary directories are cleaned up in `cleanup()`; the E2B sandbox has a 5-minute default timeout

## Dependencies

### Internal
- `modex_agent/sandbox/` — `SandboxConfig`, `SandboxResult`, `SandboxArtifact`, `EnvironmentBuilder`, `CommandPatternGuard`, `WorkspacePolicy`, `IsolationManager`, `validate_code`
- `modex_agent/sandbox/exceptions.py` — `SandboxUnavailableError`, `CommandRejectedError`

### External
- `docker` (optional) — Docker SDK for Python
- `e2b_code_interpreter` (optional) — E2B cloud sandbox SDK
- `landlock` (optional) — Linux Landlock Python bindings

<!-- MANUAL -->
