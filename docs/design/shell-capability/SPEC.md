# Shell Capability And Atomic Tool Groups

Status: implemented. This is the living design and execution contract for the
shell/tool-group convergence. It refines ADR-0047 and supersedes the terminal
assembly contract previously described in scope-assembly SPEC section 5.1.

## 1. Outcome

One `shell` capability owns native shell composition. It contributes one
compile-time tool-group manifest anchored by `bash`; one TOOL-slot factory then
builds exactly one runtime group for one native agent. The group is atomic for
roster editing, registration, filtering, graph views, and lifecycle ownership.

The convergence removes the former parallel roads:

- no `AgentSpec.use_terminal` or root-level `terminal_visibility`;
- no independent `bash` / `bash_input` / `process` / `terminal` factories;
- no `ShellAssemblyDeps`, `build_bash_tool`, or `ensure_input_companion`;
- no `PoolInstance.terminal_manager` or business-layer terminal construction;
- no compatibility reader, alias, or migration shim for those deleted faces.

External agents remain structurally outside capability resolution and use their
provider's own tool surface.

## 2. Public Contract

### 2.1 Declaration

The framework capability is explicit opt-in:

```yaml
capabilities:
  shell:
    mode: persistent
    terminal_visibility: false
```

`mode` is `subprocess`, `persistent`, or `terminal`; its default is
`persistent`. `terminal_visibility` defaults to `false` and affects only a
root agent whose requested mode reaches the HOST terminal variant.

The bot registers `BotShellCapability` at project priority. That policy
auto-applies shell to absent/default, `full`, `read_write`, and `read_only`
toolsets, and to an incremental or wholesale tool declaration that selects
`bash`. `capabilities: {shell: false}` is the explicit capability veto;
`tools: [-bash]` is the component-level veto. The capability implementation and
mode semantics remain framework-owned.

### 2.2 Compile Product

`ToolGroupSpec(anchor="bash", variants=...)` is a frozen candidate manifest.
Each `ToolGroupVariant` gives the exact ordered member names for one legal
runtime result:

| Variant | Members |
|---|---|
| `subprocess` | `bash` |
| `persistent` | `bash`, `bash_input` |
| `terminal` | `bash`, `process`, `terminal` |

Compilation filters candidates by declaration context:

| Requested mode | Root candidates | Subagent candidates |
|---|---|---|
| `subprocess` | `subprocess` | `subprocess` |
| `persistent` | `subprocess`, `persistent` | `subprocess`, `persistent` |
| `terminal` | `subprocess`, `persistent`, `terminal` | `subprocess`, `persistent` |

The subagent `terminal` request is retained in the compiled capability config
but the impossible terminal candidate is omitted. This is an intentional,
silent downgrade contract: loading and canonical serialization preserve the
request rather than rewriting user configuration or failing boot.

The compiler validates non-empty unique variants, anchor membership, unique
members, non-overlapping groups, and absence of scalar companion
contributions. A declaration may edit only `bash`; direct edits of
`bash_input`, `process`, or `terminal` fail compilation. Vetoing `bash` drops
the manifest with the anchor.

The scope bill and `/api/scope/options` expose candidate manifests. They do not
claim which variant was assembled on a particular host.

## 3. Runtime Selection Matrix

`ShellToolGroupFactory` reads typed `ShellWiring` from the capability and
returns one `ToolGroup`. "PTY available" below means persistent bash support
and, for a non-HOST substrate, a non-empty selected shell argv.

| Effective substrate | Request / position | Runtime variant | Selection reason |
|---|---|---|---|
| HOST | `subprocess`, any native position | `subprocess` | `requested` |
| HOST | `persistent`, PTY available | `persistent` | `requested` |
| HOST | `persistent`, PTY unavailable | `subprocess` | `persistent_unavailable` |
| HOST | `terminal`, root, terminal manager available | `terminal` | `requested` |
| HOST | `terminal`, root, manager unavailable, PTY available | `persistent` | `terminal_unavailable` |
| HOST | `terminal`, root, manager and PTY unavailable | `subprocess` | `terminal_unavailable` |
| HOST | `terminal`, subagent, PTY available | `persistent` | `subagent_terminal_downgrade` |
| HOST | `terminal`, subagent, PTY unavailable | `subprocess` | `subagent_terminal_downgrade` |
| LOCAL or OCI | `subprocess`, any native position | sandboxed `subprocess` | `requested` |
| LOCAL or OCI | `persistent`, PTY available | sandboxed `persistent` | `requested` |
| LOCAL or OCI | `persistent`, PTY unavailable | sandboxed `subprocess` | `persistent_unavailable` |
| LOCAL or OCI | `terminal`, PTY available | sandboxed `persistent` | `sandbox_terminal_downgrade` |
| LOCAL or OCI | `terminal`, PTY unavailable | sandboxed `subprocess` | `sandbox_terminal_downgrade` |

LOCAL and OCI are preserved: persistent mode launches the selected substrate
argv through `SandboxBinding`, and subprocess mode uses
`ContainerShellExecutor` with the selected backend and one-shot prefix. A
selected non-HOST substrate with neither launch product is an assembly error,
never an implicit HOST shell. Confirmed pre-command startup unavailability may
still move one conversation's binding to HOST under the sandbox contract; no
possibly-submitted command is replayed.

Assembly emits one structured log record with `requested_mode`,
`effective_variant`, `selection_reason`, `shell_agent`, and
`shell_substrate`. This is the actual group selection, unlike compile-time
candidate manifests. Per-turn sandbox enforcement telemetry remains the
authority after any later per-session startup fallback.

## 4. Atomic Registration And Views

Native assembly resolves the roster anchor once, requires a `ToolGroup` when a
manifest exists, and verifies the returned anchor, variant, and exact ordered
member names against that manifest. A scalar result for an anchor, undeclared
group, impossible variant, or member mismatch fails assembly.

`ToolManager.register_group` is all-or-nothing. It applies normal origin
arbitration to every member, restores the complete prior tool/origin/audit
state on any collision, and records a resource-free group descriptor. A scalar
registration cannot overwrite a group member; unregistering any member removes
the whole group. `FilteredToolManager` expands an anchor policy to all members
and rejects partial allow/deny policy.

`GraphToolPreset` copies an allowed group with the same tool objects and
`resource=None`. The graph-scoped manager is a borrowed execution view: it
preserves group atomicity but never becomes a second lifecycle owner. Excluding
the anchor excludes the whole group; excluding only a companion is an error.

## 5. One Ownership Stack

`AssemblyResourceOwner` is created before declared interceptor assembly and is
the only provisional owner for that agent:

```text
AssemblyResourceOwner
  adopt sandbox guard prerequisite
  adopt shell group dependent
  transfer all resources -> AgentInstance
  AgentInstance.stop(): drain pipeline -> close resources in reverse order
```

Pool Stage 3 stores that same owner on the builder after adopting a local
sandbox guard. Stage 4 passes it through
`NativeAssemblyInputs.resource_owner`; native tool resolution adopts the shell
resource into it. Standalone and lazy subagent assembly create the same owner
at their entry point. Successful agent creation transfers, rather than copies,
the ordered resources to `AgentInstance`.

Cleanup closes from the tail and removes an item only after successful close.
If a dependent shell close fails, the failed shell and all earlier
prerequisites remain owned for retry; the sandbox guard is not closed beneath a
live shell. Pipeline drain must complete before instance resources close. On
assembly failure the original exception remains primary and a cleanup failure
is attached as a note. Resource `aclose()` implementations are idempotent.
Nested rollback callers may retry pending cleanup; only successful rollback is
treated as complete. A callback failure after pool registration unregisters
only the instance created by that assembly, never a later replacement.

The persistent variant owns its `PersistentShellManager`. The terminal variant
owns both its `TerminalWatchdog` and terminal manager, stops the watchdog first,
then attempts every tab close while retaining ownership on failure. The
terminal manager removes a tab only after its termination succeeds, so a failed
close remains discoverable by the next teardown attempt. The
subprocess variant has no group resource.

## 6. Configuration And UI Migration

| Deleted declaration/runtime face | Current declaration/owner |
|---|---|
| `use_terminal: true` | `capabilities: {shell: {mode: terminal}}` |
| `use_terminal: false` with stateful fallback | `capabilities: {shell: {}}` or `mode: persistent` |
| root `terminal_visibility` | `capabilities.shell.terminal_visibility` |
| independent `process` / `terminal` / `bash_input` roster entries | `bash` anchor selected through `shell` |
| `PoolInstance.terminal_manager` | shell group's resource on `AgentInstance` |
| business `build_bash_tool` / input-companion wiring | `ShellToolGroupFactory` returns the complete group |

The canonical serializer preserves capability override states and nested shell
config while emitting deviations only. It emits none of the deleted fields.
The WebUI Settings -> Pools agent form renders one Shell capability row, gets
mode choices and candidate groups from `/api/scope/options`, shows
`terminal_visibility` only for root terminal mode, and keeps a loaded subagent
terminal request with a neutral downgrade hint. The bill/API remain
compile-time views; runtime selection is reported by the structured shell log.

## 7. Test Migration Map

The convergence replaces old-path assertions rather than retaining split-brain
tests or compatibility behavior:

| Retired test concern | Current coverage |
|---|---|
| `use_terminal` and business terminal-trio registration | `tests/unit/plugins/test_shell_capability.py`; `examples/bot_project/tests/unit/plugins/test_bot_shell.py` |
| `ShellAssemblyDeps`, `build_bash_tool`, and companion mutation | shell selection/resource cases in `tests/unit/plugins/test_shell_capability.py` |
| independently registered `process` / `terminal` companions | `tests/unit/scope/test_tool_group_compiler.py`; `tests/unit/tools/test_tool_groups.py` |
| unvalidated factory fallbacks | manifest/variant/member failures in `tests/unit/plugins/test_native_core_tool_groups.py` |
| pool-owned terminal manager/watchdog | `tests/unit/plugins/test_sandbox_assembly_lifecycle.py`; `tests/unit/pipeline/test_pipeline_resource_shutdown.py` |
| graph tool-manager copies taking resource ownership | graph borrow/exclusion cases in `tests/unit/tools/test_tool_groups.py` |
| root-only shell config serialization and hardcoded UI choices | `examples/bot_project/tests/unit/service/test_scope_serialize.py`; `examples/bot_project/tests/webui/test_scope_routes.py`; `AgentForm.test.tsx`; `scopeModel.test.ts` |

The deleted `test_terminal_trio_registry_split_brain.py` and
`test_shell_execution_plan.py` suites described the superseded paths and are
not retained as compatibility obligations.

## 8. Execution Record

Implementation status and pending coordinator gates are tracked in
[`tasks.md`](tasks.md). That file records evidence scope; it does not turn
agent-reported or code-inspection evidence into a final verification claim.
