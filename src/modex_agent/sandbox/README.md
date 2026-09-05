# Sandbox Execution And Approval

Sandbox selects where native commands run; approval decides whether a tool call needs a human decision. They are independently switchable product features, not dependency-free implementations: guard-only checks reuse `ApprovalRuntime` with escalation disabled.

## Native Main Sessions

| Sandbox | Approval | Behavior |
|---|---|---|
| `DEFAULT` (unconfigured) | Disabled | Ordinary HOST bash; no sandbox interceptor or engine probes |
| `DEFAULT` (unconfigured) | Enabled | Same execution; independent approval rules apply only to configured tools |
| Explicit backend | Disabled | Guard checks remain; boundary findings return a denial `ToolResult`, without suspension |
| Explicit backend | Enabled | Guard runs first; BOUNDARY enters pending approval even with `tools: {}`; CLEAN reaches per-tool approval rules |

Hard deny, traversal and SSRF findings are never approvable. CLEAN means no registered guard finding, not proof that an arbitrary operation is safe. Independent WebReader safety and native delegation checks also remain active with DEFAULT.

## Backends And Policies

| Backend value | Selection and execution |
|---|---|
| `default` | Unconfigured; never enters runtime selection |
| `auto` / `local` | Linux uses bwrap; macOS uses Seatbelt; genuine unavailability falls back to HOST. Windows has no native LOCAL engine |
| `oci` | Probes Docker, then Podman; uses the selected engine or falls back to HOST if unavailable |
| `host` | Explicit guard-enabled host execution; no kernel isolation (`enforcement=NONE`) |

Available LOCAL/OCI keeps its selected engine, including under `danger-full-access`. LOCAL and OCI never substitute for each other. Both native main agents and subagents retain ordinary HOST bash when the selected engine is genuinely unavailable; fallback never grants permission.

`policy` defaults to `danger-full-access`, which declares no main file boundary. Set `workspace-write` or `read-only` to restrict known file targets to workspace + `writable_roots`; `read-only` hard-denies file writes. `network` defaults to `false` independently of policy, but HOST has no kernel network isolation. `protected_subpaths` defaults to `[".git"]` for restricted-policy file checks and supported engine write protection, not arbitrary HOST script containment. bwrap full-access retains its writable host-root bind and the network setting.

## Configuration

The actual entry point is `workspace.pools.<pool>.agents.<main>`. Sandbox requires both `interceptors: [+sandbox_guard]` and `interceptor_configs.sandbox_guard.sandbox`; there is no scope-root `sandbox:` field. Enabling the roster entry with DEFAULT settings is a configuration error. To disable sandbox, remove its enabling roster entry and corresponding configuration, leaving approval independently configured.

This opt-in example configures reads, writes and full-command bash regex exemptions. It is not the shipped bot default:

```yaml
workspace:
  name: bot
  pools:
    default:
      agents:
        default:
          approval:
            enabled: true
            tools:
              read:
                allowed_paths: ["./*"]
              write:
                allowed_paths: ["./*"]
              edit:
                allowed_paths: ["./*"]
              bash:
                allowed_paths: []
                allow_patterns: ['git status', 'pwd']
          interceptors: [+sandbox_guard]
          interceptor_configs:
            sandbox_guard:
              sandbox:
                backend: auto
                policy: workspace-write
                network: false
                writable_roots: []
```

`allowed_paths` names per-tool no-prompt directories, not filesystem permissions or mounts. Concrete allowance roots must fit the active sandbox envelope. Universal patterns do not waive the runtime guard. `allow_patterns` uses case-insensitive regex fullmatch, not shell globs, and is considered only after guard CLEAN. The bash entry above requests approval for other CLEAN commands; tools omitted from the map have no inner approval gate.

The shipped `examples/bot_project/config/scopes/bot.yml` does not enable sandbox. The `default` and `coder` pools' native roots enable approval with only `write`/`edit` `allowed_paths: ["./*"]`; the `review` root does not declare approval. This is not blanket approval for reads or commands.

`SandboxSettings` also accepts `image` (OCI defaults to `modex-sandbox:latest`), `protected_subpaths`, and `guard`. `guard.enabled` gates advisory traversal/network checks only; built-in command deny rules and declared file boundaries remain. See the [multi-root example](../../../docs/design/unified-security/PRD.md#multi-root-example) for extra roots and child `allowed_dirs: ["../shared"]`.

## Native Delegation

Native subagents have a fixed known-file read/write envelope: canonical workspace + explicit `allowed_dirs`. Those directories must fit pool workspace + `writable_roots`; invalid declarations fail rather than being clipped. Children do not implicitly inherit every extra pool root. Parent READ_ONLY is preserved; otherwise delegated policy narrows to WORKSPACE_WRITE. The materialized snapshot is stable across later configuration changes.

Native subagents never escalate to humans, even when parent approval is enabled. A known file or extracted command-path boundary returns an error naming allowed roots and directing the request to the main session, without a card or suspension. DEFAULT still installs delegation's guard-only classification without starting a sandbox interceptor or probe. Graph turns are also noninteractive: active guards remain, human escalation does not.

Paths use `workspace/boundary.py`: home expansion, workspace-relative resolution, symlink resolution and component-wise containment. Native main/subagent file and AST path tools share the workspace wrapper; nonempty whitespace spelling is preserved. Explicit relative `working_dir` uses the same canonical target for checks, execution and approval anchoring. Use host-native absolute paths, such as `/workspace/shared` on POSIX or `F:/work/shared` on Windows; drive paths and WSL paths are not interchangeable.

## Shell And Failure Handling

`shell_plan.build_bash_tool` is the common native main/subagent construction path. All three bash implementations receive the same name-based security judgment:

| Bash implementation | Execution identity |
|---|---|
| Terminal trio (`bash`, `process`, `terminal`) | HOST terminal manager; a selected LOCAL/OCI substrate replaces the `bash` slot with one of the implementations below |
| Persistent PTY | Selected substrate's shell argv, or ordinary HOST shell |
| One-shot subprocess | Selected LOCAL/OCI argv prefix, or ordinary HOST executor |

`bash_input` follows the final persistent bash manager and session through `ensure_input_companion`. `process` and `terminal` remain HOST controls, not sandbox-shell companions. PTY cancellation drains the active reader before session reuse.

Only confirmed pre-command initialization unavailability permits HOST fallback. Local startup checks execute a constant no-op, not the agent command. Missing executables or recognized namespace unavailability can degrade; generic launcher `PermissionError`, configuration and programming errors propagate. `SandboxBinding` shares execution and telemetry state per session; one fallback does not change other sessions.

A possibly-submitted command is never automatically replayed or retried on HOST. Container death or uncertain partial execution requires checking side effects before recovery. Human approval waives only the matching call's BOUNDARY backstop; it does not widen kernel mounts/profiles, bypass hard findings, or authorize replay. An approved shell command outside the envelope may still be denied by the selected engine or OS.

## Coverage Limits

HOST command/input guards are best-effort string checks, not arbitrary-code containment. Scripts, interpreters, dynamic paths, inherited credentials and secondary tool effects can remain host-capable. Existing host environment behavior is preserved; no default secret filtering is added. LOCAL exposes the host toolchain and broad host reads. OCI uses image tools and mounts; the host CLI environment is not automatically the container command environment. Fix missing dependencies in the image/workspace or explicitly choose another agent backend, rather than routing individual commands to HOST.

External main and subagent provider tools bypass framework `ToolNode` and its interceptor/approval flow. Delegation roots/policy are metadata only: `file_guards=false`, backend/enforcement unknown, and no generic external approval bridge is promised. Unknown/MCP tools also have no registered target coverage. `SandboxEnforcementSnapshot` is diagnostic telemetry, not proof that every tool or syscall is isolated.

WebReader independently validates every DNS answer, dials a validated IP with origin TLS identity, checks redirects and disables environment proxy routing. This also applies under DEFAULT, but not to arbitrary shell/MCP/provider networking. Its 50,000-character converted-output limit is not a download-size bound.

## References

- [Module map](AGENTS.md)
- [Execution contract](../../../docs/design/sandbox-integration/PRD.md) and [validation evidence](../../../docs/design/sandbox-integration/tickets.md#validation-evidence)
- [Permission contract](../../../docs/design/unified-security/PRD.md) and [security coverage](../../../docs/design/unified-security/tickets.md)
- OCI image/build entry points: `scripts/docker/sandbox/`
