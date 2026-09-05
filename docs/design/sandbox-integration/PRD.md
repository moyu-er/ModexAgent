# Sandbox Execution Contract

Status: implemented. Validation scope and platform gaps are recorded in [tickets.md](tickets.md#validation-evidence).

## Scope

The execution substrate answers where native commands run. Guards and approval answer whether a tool call may proceed. Sandbox and human approval are independently switchable product features; guard-only classification still reuses `ApprovalRuntime`. Fallback is not authorization, and approval does not reselect execution location.

Read the [permission contract](../unified-security/PRD.md) for verdicts, approval and delegation; use the [sandbox README](../../../src/modex_agent/sandbox/README.md#configuration) for YAML and the [module map](../../../src/modex_agent/sandbox/AGENTS.md) for implementation owners.

## Backend Selection

| Declaration | Current behavior |
|---|---|
| `default` | Unconfigured: no sandbox interceptor, engine probes or runtime selection; ordinary HOST bash |
| `auto` / `local` | Linux selects bwrap; macOS selects Seatbelt; unavailable LOCAL falls back to HOST. Native Windows has no LOCAL engine |
| `oci` | Probes Docker, then Podman; passes the selected engine to the runtime; unavailable OCI falls back to HOST |
| `host` | Explicit HOST execution with active guard checks and no kernel isolation |

`selection.py` owns `resolve_selection` and `select_runtime`. Typed selection records requested/effective backend, platform, concrete engine and fallback reason. DEFAULT is rejected at this layer; AUTO is resolved away. LOCAL never substitutes OCI, or vice versa.

Available LOCAL/OCI must execute through its selected engine for native main agents and subagents, including under DANGER_FULL_ACCESS. Both roles retain ordinary HOST bash on genuine engine unavailability; shell removal is not the fallback policy.

## Policy And Configuration

`SandboxSettings` is frozen and rejects unknown fields. Its fields are `backend`, `policy`, `network`, `writable_roots`, `protected_subpaths`, `image` and `guard`.

- Policy defaults to `danger-full-access`, with no main file boundary. Set `workspace-write` or `read-only` for known-file workspace restrictions; READ_ONLY hard-denies file writes.
- `network: false` is independent of file policy. HOST cannot enforce kernel network isolation.
- Restricted-policy roots are workspace + `writable_roots`. Default `.git` protection applies to supported file checks/engine write protection, not arbitrary HOST scripts.
- `guard.enabled` gates advisory traversal/network checks, not built-in command denies or declared file boundaries.
- bwrap DANGER_FULL_ACCESS keeps LOCAL with a writable host-root bind, without restricted-policy read-only shadows/private tmpfs; the network setting still applies.

Configure `workspace.pools.<pool>.agents.<main>.interceptors: [+sandbox_guard]` together with `interceptor_configs.sandbox_guard.sandbox`. There is no scope-root sandbox field. A roster-enabled guard with DEFAULT settings fails configuration. Approval is a separate main-agent declaration; the [four-combination matrix](../unified-security/PRD.md#native-main-sessions) defines its interaction.

The shipped bot scope does not enable sandbox. Its `default` and `coder` native roots enable approval only for configured write/edit rules (`./*`); the `review` root does not declare approval. Factory registration does not enable a sandbox instance.

## Paths And Delegation

`workspace/boundary.py` canonicalizes workspace-relative/home paths and symlinks before component-wise containment. `resolve_available` canonicalizes workspace and extra roots before mount/profile compilation. Native file/AST wrappers and explicit relative cwd use the same targets for checking, execution and approval anchors; nonempty whitespace spelling is preserved.

Native subagents capture workspace + `allowed_dirs` at materialization. Every child directory must fit pool workspace + `writable_roots`; absent `allowed_dirs` means workspace only. Parent READ_ONLY is preserved; other policies narrow to WORKSPACE_WRITE. Later configuration changes do not mutate that snapshot. Known boundary violations return direct errors with allowed roots, even when the parent has human approval enabled.

Approval `allowed_paths` creates no permission or mount. Concrete no-prompt roots are validated against the active sandbox envelope; universal patterns do not waive guard checks. See the [multi-root example](../unified-security/PRD.md#multi-root-example), including `../shared` delegation and host-native absolute path notes.

## Execution Binding

```text
scope roster/config -> SandboxGuardInterceptorFactory
  -> resolve_selection -> select_runtime -> resolve_available
  -> ResolvedSandbox -> SandboxBinding -> shell_plan.build_bash_tool
  -> selected persistent argv OR one-shot ShellExecutor
```

The factory resolves the substrate before constructing bash. `ResolvedSandbox` carries effective backend/enforcement, persistent argv, one-shot prefix, mounts and fallback reason. A selected non-HOST substrate without either argv product is an error, never a silently reused HOST shell.

All three bash implementations share the same tool-name judgment. The terminal trio is HOST; persistent PTY uses selected shell argv; one-shot subprocess uses the selected LOCAL/OCI prefix. HOST retains the existing terminal/persistent/subprocess choices. One-shot execution passes the complete command as one `bash --noprofile --norc -c` argument, preserving pipes and redirection inside the selected execution environment.

`SandboxBinding` is shared by execution and telemetry and tracks pre-command fallback per session. Other sessions retain their substrate, cwd, environment and pending input. `ensure_input_companion` binds `bash_input` to the final persistent manager/session. PTY cancellation waits for the reader to exit before reuse. `process` and `terminal` remain HOST controls, not companions of the sandbox shell.

## Availability And Recovery

- `resolve_available` handles confirmed initialization unavailability only. Missing executables and recognized namespace unavailability may fall back; generic launcher `PermissionError`, configuration and programming failures propagate.
- Local startup validation runs a constant no-op through compiled argv before binding. No agent command is submitted during this check.
- OCI uses selected-engine lifecycle/config hashing and a mount-consistency probe. Permission/configuration errors are not classified as a missing engine.
- A possibly-submitted command is never automatically replayed, including after container death or uncertain partial execution. Report uncertainty and inspect side effects before restoring the container or shell.
- Approval changes neither mounts/profiles nor the bound execution engine. An approved outside-envelope shell call may still fail at the OS boundary; that failure cannot trigger HOST replay.
- Seatbelt profiles are cleaned up after bound shells close. Shared OCI containers are not destroyed by an individual agent runtime.

## Environment And Limits

LOCAL preserves the host filesystem/toolchain/environment view, with actual engine write/network restrictions. Broad host reads are not secret isolation. OCI uses image tools and bind mounts; host CLI environment is not automatically injected into container commands. Existing host credentials remain available where inherited; there is no new default secret filter. Image/build entry points remain `scripts/docker/sandbox/`; missing dependencies require image/workspace changes or explicit agent backend selection, not per-command HOST routing.

HOST command/input checks are best-effort string analysis, not containment of scripts, interpreters, dynamic paths or secondary tool effects. Known file targets have canonical-root checks; unknown/MCP tools have no registered target coverage. External main/subagent provider tools bypass framework ToolNode and its approval/interceptor flow. External delegation is metadata-only (`file_guards=false`, backend/enforcement unknown), not a generic external approval bridge.

`SandboxEnforcementSnapshot` under `TurnCustomKey.SANDBOX_ENFORCEMENT` is telemetry only. FULL/PARTIAL/NONE describes the reported execution surface, not every tool, arbitrary syscall isolation or all-platform validation.

## Independent Web Safety

WebReader validates all DNS answers before dialing a validated IP, retaining origin TLS identity. Manual redirects receive per-hop checks, with a default five-hop limit; its client disables environment proxy routing. This works under DEFAULT independently of sandbox. Static `NetworkGuard` string checks do not perform DNS connection validation.

This protection is specific to WebReader, not shell/MCP/provider networking or all WebSearch traffic. Static text/HTML output is converted and truncated to 50,000 characters after receipt; that is not a download byte limit. Existing body/ATX-heading and `Error:` output conventions remain unchanged.

## Validation And Non-Goals

[Validation evidence](tickets.md#validation-evidence) covers Windows/WSL unit, conformance and architecture suites, bot pool wiring, and scoped static checks. WSL includes real bwrap/Docker execution; macOS and Podman have simulations, not live execution evidence. Warnings remain. Passing suites do not remove HOST or external-provider limits.

Legacy adapters remain dormant; E2B/Landlock are not current selector backends. No new security DSL, filesystem bridge, default environment filter, subagent approval-card route or failure-triggered replay is introduced. Windows-native isolation and other new-backend research remain outside this delivery.
