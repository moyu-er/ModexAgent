<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-09-05 -->

# sandbox

## Purpose

Opt-in execution substrate plus backend-independent permission judgments. Read [README.md](README.md) for configuration and the [substrate PRD](../../../docs/design/sandbox-integration/PRD.md) / [permission PRD](../../../docs/design/unified-security/PRD.md) before changing selection, approval or delegation. Implementation validation and platform gaps are recorded in [sandbox tickets](../../../docs/design/sandbox-integration/tickets.md#validation-evidence).

## Settled Semantics

- DEFAULT leaves the substrate dormant: no sandbox interceptor instance, no probe, ordinary host bash. The bot scope opts in on every native pool root with `backend: host` + `exclusive.write_surface: workspace` (the bot's basic permission logic: workspace-only writes, unrestricted reads, extra paths declarable per pool); the external opencode root stays outside framework enforcement. Independent approval, WebReader safety and native delegation checks still apply.
- Two permission classes (``tool_matrix.permission_class``): ``parallel`` (read/ls/glob/grep/ast_grep_search/lsp_*/web_reader) is UNRESTRICTED by default — a per-tool ``parallel.boundaries`` entry narrows one tool; ``exclusive`` (write/edit/aci_edit/ast_grep_replace/bash/bash_input/process) is bounded by ``exclusive.write_surface`` (none/workspace/roots/full). bash-class members have no path argument — the kernel substrate and command-text guards bound them; per-tool path boundaries do not apply. Reads stay unrestricted while bash command checks remain (bash can write; commands cannot be intent-classified) — a documented asymmetry.
- Sandbox and approval are independently switchable product features, not import-independent implementations: guard-only checks reuse `ApprovalRuntime` without human escalation. The [native-main matrix](README.md#native-main-sessions) covers DEFAULT/explicit backend with approval enabled/disabled.
- Explicit HOST activates guards without kernel isolation. Available LOCAL/OCI uses its selected engine even under `write_surface: full`. LOCAL selects bwrap on Linux or Seatbelt on macOS; OCI probes Docker then Podman. Neither family falls into the other. Genuine unavailability falls back to ordinary HOST bash for native main agents and subagents, with a reason.
- Downgrade is not authorization. Main-agent BOUNDARY uses the existing transaction/GraphInterrupt channel when approval is enabled, even with `tools: {}`; disabled approval returns denial. CLEAN reaches inner per-tool rules. Native subagents return errors with allowed roots, never cards, even when parent approval is on. Hard deny findings (write-surface none refusals, protected-path writes) cannot be approved;; SSRF escalates like BOUNDARY.
- HOST guards are best-effort checks on known targets/command text, not kernel read/write containment. Dynamic scripts and inherited credential environment remain host-capable. No syscall read-isolation claim or default secret-env filtering.
- Only confirmed pre-command initialization unavailability permits HOST fallback. Generic OS launcher PermissionError propagates; recognized namespace unavailability or a missing executable can degrade. A possibly-submitted command is never replayed.
- `process` and `terminal` are host-terminal controls. `bash_input` belongs to the final persistent bash manager/session, not a discarded host shell.
- External provider tools bypass framework ToolNode. Current delegation metadata records limits, not provider enforcement: no provider-neutral permission capability is propagated, file guards are false, kernel enforcement is unknown. Do not invent enforcement or prohibit external agents.

## Key Files

| File | Responsibility |
|---|---|
| `settings.py` | Frozen `SandboxSettings` with the two-class permission face — `parallel` (read-only family, unrestricted by default; per-tool `boundaries` narrow) and `exclusive` (read-write family; `write_surface` none/workspace/roots/full + `writable_roots`) — plus `GuardSettings` and engine enums. DEFAULT is unconfigured; network defaults false. All declared paths are RELATIVE and anchor to the live workspace root on every evaluation; the workspace root itself is never declared |
| `selection.py` | `SandboxSelection`, `resolve_selection`, `select_runtime`: single typed probe/factory path; DEFAULT rejected, AUTO resolved away, reasons on fallback |
| `engine_probe.py` | Cached bwrap/Seatbelt/Docker/Podman probes; `clear_probe_cache` test seam |
| `runtime.py` | `SandboxRuntime.resolve_available` canonicalizes workspace/extra roots before mount/profile compilation; pre-command availability, startup no-op, telemetry and cleanup contract |
| `shell_plan.py` | `SandboxBinding`, `ShellAssemblyDeps`, `resolved_binding`, `resolved_substrate`, `build_bash_tool`: common native bash binding; per-session pre-command HOST fallback shared with telemetry |
| `bwrap_runtime.py` | Linux argv compilation and startup validation. `full` retains LOCAL with writable host root bind; `roots` keeps the workspace ro-bound and rw-binds only declared roots; network flag still applies |
| `seatbelt_runtime.py` | macOS profile compilation, `sandbox-exec -f`, profile lifetime/cleanup and startup validation; simulated tests only, live execution unverified |
| `oci_runtime.py`, `oci_lifecycle.py`, `oci_support.py` | Selected engine, container lifecycle/config hash, mount compilation/probe, argv products and initialization failure classification |
| `container_executor.py` | LOCAL/OCI one-shot `ShellExecutor`; passes complete bash command as one `-c` argv argument; no host replay after failure |
| `decision.py`, `verdict.py` | `SecurityDecisionService` and typed verdict: one judgment implementation for classifier and execution backstop; live root projection for main, frozen provider for delegated native instances |
| `tool_matrix.py` | Typed effect/target catalog and `approval_anchor`; known file targets, `bash.command`, `bash_input.line`, `process.data`, `web_reader.url`; unknown tools claim no boundary coverage |
| `security_classifier.py` | `SecurityClassifier` fixed tier mapping; `guard_only_runtime` preserves checks without human escalation |
| `approval_envelope.py` | `validate_approval_envelope`: concrete no-prompt allowance roots checked against canonical workspace + writable roots; universal patterns never waive runtime guards |
| `delegation.py` | `DelegationSnapshot`, canonical allowed roots, delegation settings and direct-denial copy; actual backend/enforcement/file-guard capability and limitations, depth budget |
| `interceptor.py`, `guard_presentation.py` | Execute-time judgment, exact approval-marker waiver for approvable categories (BOUNDARY / SSRF), snapshot and uncertainty reporting; no command retry |
| `readonly.py` | Provably read-only command classification (shell-family profiles, fail-closed) — the parallel fast path for bash-class tools; approval experience, not containment |
| `guard*.py`, `workspace_policy.py` | Command-path extraction, advisory network checks and known file path boundaries; command parsing is best-effort. The command deny rules are DEPRECATED USAGE (`guard.deny_rules`, off by default — interception owned by the path boundary + kernel substrate) |
| `env_builder.py` | Existing environment policy utility; not a new mandatory filter for all execution paths |
| `types.py`, `exceptions.py`, `platform.py` | Enforcement/result vocabulary, typed failures and platform/shell helpers |

## Configuration And Wiring

The actual YAML shape is `workspace.pools.<pool>.agents.<root>` with `approval`, `interceptors: [+sandbox_guard]` and `interceptor_configs.sandbox_guard.sandbox`. There is no scope-root sandbox field. A roster-enabled guard with DEFAULT settings is a configuration error; registered factory availability does not mean an instance is enabled.

`plugins/defaults/interceptors.py` eagerly resolves the selected substrate before bash construction. `shell_plan.py` passes the shared `SandboxBinding` to execution and telemetry; a pre-command fallback changes only the affected session. `tools/terminal/persistent_bash.py:ensure_input_companion` binds the companion to the resulting persistent manager. PTY cancellation drains the active reader before session reuse. Guard/classifier consumers share `SecurityDecisionService` implementation and settings/root semantics, not necessarily one object instance.

`multi_agent/template.py:materialize` derives the effective settings through `delegation.resolve_agent_sandbox` — the ONE derivation: an undeclared subagent inherits the caller wholesale (a dormant caller normalizes to guard-only HOST), a declared block is authoritative for the permission face while the substrate (backend/network/image) stays with the caller, and every declared path must fit the caller envelope (a delegation can only narrow, never amplify — violations fail assembly). DEFAULT still skips substrate construction/probing but native delegation installs a guard-only classifier. External strategies receive truthful metadata, not framework tool enforcement.

Path normalization/containment belongs to `workspace/boundary.py`. Runtime compilation canonicalizes workspace/extra roots first; native main/subagent file and AST path tools use the same workspace wrapper. Known child file writes use the caller workspace + validated declared roots; child reads (parallel class) are unrestricted by default. Nonempty whitespace spelling is preserved, and explicit relative cwd shares canonical permission/approval-anchor semantics. Additional roots are not authorized by approval, and changing configuration does not mutate an existing delegation snapshot. See the [multi-root example](../../../docs/design/unified-security/PRD.md#multi-root-example) for relative and host-native absolute paths.

All three bash implementations share tool-name judging: terminal trio execution is HOST, persistent PTY uses selected argv, and subprocess uses the selected prefix. Approval waives only the matching BOUNDARY backstop, never kernel bounds; approved outside-envelope shell calls may still fail at the OS boundary. Graph turns are noninteractive and retain active guard-only classification; DEFAULT is filtered before that wiring. Native delegation receives the pool audit sink, keeping ESCALATED and APPROVED distinct.

## Independent Web Safety

`tools/web/guarded_transport.py` resolves and checks all DNS answers before dialing a validated IP, retaining origin TLS identity. `guarded_http.py` checks manual redirect hops and disables environment proxy routing. `reader.py` consumes this independently of sandbox enablement. Do not extend that claim to arbitrary shell/MCP/provider networking or to response-size containment.

## Legacy And Dependencies

The package facade still exposes the dormant adapter API (`config.py`, `enums.py`, `factory.py`, `adapters/`). E2B and Landlock are legacy backend technologies, not current substrate selections. Do not revive legacy isolation providers or treat their docs as current enforcement evidence. New substrate consumers import their owning modules directly.

This package is not import-isolated from the framework: decision/shell integration uses tools/workspace contracts; classifier/interceptor integration uses approval/core/interceptor contracts; telemetry uses runtime state. Optional adapter SDKs are not prerequisites for the CLI substrate.

## Validation

See [ticket evidence](../../../docs/design/sandbox-integration/tickets.md#validation-evidence) for parent-reported Windows/WSL suites, bot pool wiring and scoped static checks. WSL includes real bwrap/Docker; macOS/Podman coverage is simulated, with no live execution validation. Warnings remain. These results do not imply all-platform or arbitrary-code containment; Windows-native isolation/new-backend research is outside this delivery.
