# Unified Security Contract

Status: implemented. Parent design: [sandbox execution](../sandbox-integration/PRD.md). See [tickets.md](tickets.md#validation-scope) for validation scope and remaining limits.

## Ownership

One decision implementation, one human approval channel, one audit timeline. `SecurityDecisionService` produces verdicts; `SecurityClassifier` maps them into the existing ToolNode tier/transaction flow; `SandboxGuardInterceptor` checks again immediately before execution. Consumers share the implementation and root semantics, not necessarily one service object.

Sandbox and human approval are independently switchable product features. This is not zero implementation coupling: sandbox-on/approval-off, native delegation and graph turns reuse guard-only `ApprovalRuntime` with escalation disabled. Execution fallback does not authorize an operation, and approval does not change its backend.

## Native Main Sessions

| Sandbox | Approval | Result |
|---|---|---|
| DEFAULT | Disabled | No sandbox interceptor or engine probes; ordinary HOST bash |
| DEFAULT | Enabled | Same execution, with independent per-tool approval rules; no outer sandbox guard |
| Explicit backend | Disabled | Active guard; BOUNDARY and hard findings return denial `ToolResult`s without suspension |
| Explicit backend | Enabled | Active guard; BOUNDARY becomes pending through existing approval, even with `tools: {}`; CLEAN reaches inner per-tool rules |

Explicit HOST activates guards without kernel isolation. Available LOCAL/OCI keeps its selected engine even under `write_surface: full`. The exclusive (read-write) class defaults to `workspace` (workspace + `writable_roots` writable); `roots` confines writes to the declared roots; `none` refuses file writes; `full` declares no file boundary. The parallel (read-only) class is unrestricted by default. The [execution PRD](../sandbox-integration/PRD.md) defines backend selection and pre-command fallback.

Its `default` and `coder` native roots opt into the sandbox and enable approval (`enabled: true`, no per-tool entries — the guard layer arbitrates: in-envelope writes are CLEAN, boundary/SSRF findings card); the `review` root declares the sandbox without approval (guard findings deny directly). Per-tool approval overrides (`approval.tools`) remain a framework capability the bot does not expose. This does not gate every command or read tool.

## Verdict Order

| Verdict | Classification and action |
|---|---|
| `DENY_RULE` | HARDLINE, direct error; includes `write_surface: none` refusals and protected-path writes |
| `SSRF` | APPROVABLE gray zone: escalates like BOUNDARY (card on main agents, direct denial on subagents) — network is not path-permission scope |
| `BOUNDARY`, native main with approval enabled | DANGEROUS; existing `ApprovalTransaction` / `GraphInterrupt` / renderer / resume |
| `BOUNDARY`, no human channel or native subagent | HARDLINE, direct denial; child errors name allowed roots and direct the request to the main session |
| `CLEAN` | Inner `TieredToolApprovalClassifier`; not a blanket safety claim |

Hard findings precede approvable boundaries. The built-in command deny rules (rm-style/destructive patterns) are DEPRECATED USAGE — off by default (`guard.deny_rules`), with interception owned by the path boundary and the kernel substrate; `guard.enabled` and `guard.network` control the advisory network layer, not declared file boundaries. A provably read-only command (`sandbox/readonly.py` — every pipeline segment on a read-only allowlist, no redirects, no substitutions, no assignments, fail-closed per executing shell family) returns CLEAN before the SSRF/boundary layers: the shell-world twin of the unrestricted parallel read, waiving cards for `cat`/`ls`/`grep`-shaped reads outside the envelope. `guard.read_only_bypass: false` restores the friction.

`approval.enabled` defaults to false. For enabled approval, tools absent from `approval.tools` are NORMAL at the inner tier. For a configured tool, `allowed_paths: []` requests approval unless a command exemption matches; `["*"]` skips that tier gate; `["./*"]` is workspace-relative. These are no-prompt rules, not permissions.

`allow_patterns` is validated, case-insensitive regex fullmatch against the complete command, not a shell glob or command-safety parser. It runs only after guard CLEAN and cannot waive deny or boundary findings. The default empty list adds no exemptions. A nonmatching command reaches normal per-tool path/tier rules, not an automatic hard deny.

## Canonical Roots

- Native main envelope: canonical workspace + explicit `writable_roots` (under `workspace` surface); `roots` confines writes to the declared roots alone; `full` has no main file envelope.
- Native child envelope: an undeclared subagent inherits the caller's settings wholesale (equal, never wider); a declared `sandbox` block is authoritative for the permission face while the substrate stays with the caller, and every declared path must fit the caller envelope — a delegation can only narrow, never amplify; violations fail assembly.
- Parallel-class (read-only) tools are unrestricted by default on main agents AND subagents; exclusive-class boundary checks cover file writes through the write surface and bash through command guards plus the kernel substrate.
- `workspace/boundary.py` expands home, anchors relative paths to workspace, resolves symlinks and checks path components. String prefixes are not containment; drives/case use host-native semantics.
- Main decisions use the current workspace provider; materialized native delegation uses a fixed canonical snapshot. Later pool configuration changes do not widen an existing child.

`validate_approval_envelope` validates concrete `allowed_paths` roots against the active sandbox envelope. It skips absent/disabled/empty approval configuration and `full`. Universal `*`/`**` and empty entries are skipped by this assembly check, but cannot bypass the runtime guard or expand mounts. Do not treat every allowance pattern as a concrete permission root.

## Multi-Root Example

This is an explicit opt-in example, not the shipped bot configuration. Assume the workspace is `/workspace/project` with existing sibling directories `/workspace/shared` and `/workspace/artifacts`. The root can access both extras; the child receives only shared:

```yaml
workspace:
  name: bot
  pools:
    coder:
      agents:
        orchestrator:
          approval:
            enabled: true
            tools:
              read:
                allowed_paths: ["./*", "../shared/*", "../artifacts/*"]
              write:
                allowed_paths: ["./*", "../shared/*", "../artifacts/*"]
              edit:
                allowed_paths: ["./*", "../shared/*", "../artifacts/*"]
              bash:
                allowed_paths: []
                allow_patterns: ['git status', 'pwd']
          interceptors: [+sandbox_guard]
          interceptor_configs:
            sandbox_guard:
              sandbox:
                backend: auto
                exclusive:
                  write_surface: workspace
                  writable_roots: ["../shared", "../artifacts"]
          agents:
            general:
              sandbox:
                exclusive:
                  writable_roots: ["../shared"]
```

Approval is root-only. Sandbox settings belong at `workspace.pools.<pool>.agents.<main>.interceptor_configs.sandbox_guard.sandbox`, paired with the interceptor roster, not at scope root. Subagents declare their own permission face through the same `sandbox:` shape on the agent spec (`workspace.pools.<pool>.agents.<name>.sandbox`) — an undeclared subagent inherits the caller wholesale, and a declared block is ceiling-checked against the caller envelope. No child approval block is needed or permitted to enable escalation.

Relative roots resolve against workspace, not the YAML file. Absolute equivalents are `/workspace/shared` and `/workspace/artifacts` on POSIX, or `F:/work/shared` and `F:/work/artifacts` for a Windows workspace at `F:/work/project`. Use the actual host's paths; Windows drive paths, WSL paths and container paths are not interchangeable strings. Runtime canonicalization precedes mount/profile compilation. Declaring roots does not create directories or prove engine mount availability.

The file rules are per-tool no-prompt allowances inside the outer envelope. The bash regexes match only whole `git status` or `pwd` commands, after CLEAN; other CLEAN write-capable bash commands request approval (provably read-only commands never reach approval — see Verdict Order). Changing approval to disabled keeps guard denial. The parallel (read-only) class — reads, listings, searches, web_reader — is unrestricted by default on main agents and subagents alike; a per-tool `parallel.boundaries` entry narrows one tool. A child's write access beyond its declared envelope remains denied despite the main's allowance.

## Tool Coverage

`tool_matrix.py` is the shared target vocabulary for decision, classification, execution backstop and approval anchoring.

| Effect | Registered targets |
|---|---|
| READ | `read.path`; `ls/glob/grep/ast_grep_search.path` (optional/empty defaults to `.`); `lsp_navigation/lsp_diagnostics.file` |
| WRITE | `write/edit/aci_edit/ast_grep_replace.path` |
| EXECUTE | `bash.command`; explicit `working_dir` is also checked and anchored |
| EXECUTION_INPUT | `bash_input.line`, `process.data`; best-effort command checks |
| WEB | `web_reader.url` |
| NONE | Unknown/MCP tools: no registered target coverage; existing tool policy/approval still applies |

Native main/subagent file and AST path tools share workspace wrappers. Nonempty whitespace spelling is preserved. Explicit relative cwd resolves to the same canonical target for checking, execution and approval anchoring. These checks do not eliminate every filesystem race or secondary side effect.

All three bash implementations receive the same name-based judgment. The terminal trio executes on HOST; when LOCAL/OCI is selected, the `bash` slot uses a sandboxed persistent PTY or subprocess instead of reusing that host terminal. `bash_input` follows the final persistent manager/session; `process` and `terminal` remain HOST controls. A bash enforcement report does not describe the entire roster.

Shell/input checks are best-effort strings, not interpretation or containment of arbitrary code. HOST scripts, interpreters, dynamic paths and inherited credentials can access beyond registered targets. Existing host environment behavior is preserved; there is no default secret filtering or syscall-level read-isolation promise. Toolset visibility is separate from guard policy and must not be described as kernel enforcement.

## Approval And Resume

```text
ToolNode classification -> ApprovalTransaction -> GraphInterrupt
  -> existing renderer/user decision -> persisted turn snapshot -> resume
  -> exact approved-call marker -> interceptor backstop -> bound tool
```

`approval_anchor` binds known canonical file targets, full command/input strings or URLs; explicit command cwd is included. The target is resolved when the approval decision is applied, not captured when the card was first rendered. Unknown tools have no anchor. Turn-state `HUMAN_APPROVED_CALLS` markers waive only BOUNDARY when call ID and recomputed anchor match. Hard findings are never waived. Resume skips classifier reruns and uses the execution backstop, not a second approval flow.

Human approval does not expand kernel mounts/profiles. An outside-envelope shell command may remain denied by the selected engine or OS. Report that result without switching to HOST or replaying it. Only confirmed pre-command startup unavailability permits per-session fallback; generic permission/configuration failures do not. A possibly-submitted command is never automatically replayed.

## Delegation And Graph Turns

`AgentTemplate.materialize` validates directories, captures `DelegationSnapshot`, builds the selected strategy, then records actual capabilities. Native children use guard-only classification with stable roots and no human escalation, even if parent approval is enabled. Known boundary errors name the target and allowed roots, explain that permissions are fixed, and direct the request to the main session; they do not create cards or suspend. DEFAULT installs these checks without substrate probes/interceptors.

Graph turns are noninteractive: `GraphApprovalConfigurator` replaces human approval with the assembled guard-only runtime. Without an active guard, it uses no approval runtime; DEFAULT is filtered before sandbox guard-only graph wiring.

Snapshots record requested and observed backend/enforcement, file guards, limitations and depth. Native DEFAULT/HOST reports NONE; selected substrates record resolved facts. Later session fallback is tracked by `SandboxBinding`, not by mutating the permission snapshot. Delegation depth defaults to a maximum of three; over-budget dispatch is rejected.

External main/subagent provider tools bypass framework ToolNode and its interceptor/approval flow. External roots/policy are metadata, with `file_guards=false` and unknown backend/enforcement. There is no generic external approval bridge or provider-neutral permission enforcement capability. This limitation does not prohibit external agents; it requires truthful reporting.

## Audit And Output

`ToolClassification` carries guard denial/escalation facts into the existing `ApprovalAuditStore` timeline. Native main/subagent calls use the same pool sink. ESCALATED means a requested human decision; APPROVED records actual approval. Delegation keeps its source through persistence. CLEAN adds no extra guard decision.

Preserve existing tool output and error wording rather than wrapping stdout/stderr or adding UI headers/language changes. Uncertain execution reports that the command was not replayed. Documentation describes child-denial semantics without changing runtime message language.

## Independent Web Safety

WebReader's [DNS/redirect protection](../sandbox-integration/PRD.md#independent-web-safety) also works under DEFAULT: all DNS answers are validated, dialing uses a validated IP with origin TLS identity, redirects are checked per hop, and environment proxy routing is disabled. This is not global shell/MCP/provider network containment. The 50,000-character converted-output limit is not a download-size bound.

## Validation And Non-Goals

[Security coverage](tickets.md#validation-scope) includes real call-site approval/delegation, canonical targets, session binding, no replay, PTY cancellation and SQLite audit migration/restart. [Suite evidence](../sandbox-integration/tickets.md#validation-evidence) records Windows/WSL results, real WSL bwrap/Docker execution and static checks. Live macOS/Podman execution remains unverified, and warnings remain.

No new security DSL, approval state machine, child card route, per-command HOST routing, replay-after-failure or default secret filter is introduced. New-backend research is outside this delivery; passing tests do not erase HOST/external coverage limits.
