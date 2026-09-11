<!-- Parent: ../../../AGENTS.md -->
<!-- Updated: 2026-09-10 -->

# shell

Framework-owned native shell capability and its single atomic TOOL-slot group.
The living contract is
[`docs/design/shell-capability/SPEC.md`](../../../../../../docs/design/shell-capability/SPEC.md).

## Public Surface

| Type | File | Contract |
|---|---|---|
| `ShellCapability`, `ShellCapabilityConfig`, `ShellMode` | `capability.py` | Explicit-opt-in capability. Config uses `mode: subprocess|persistent|terminal` (default `persistent`) and `terminal_visibility` (default false). Contributes only the `bash` anchor plus context-valid candidate variants. |
| `SHELL_TOOL_GROUP_SPEC` | `capability.py` | Full candidate universe: subprocess=`bash`, persistent=`bash,bash_input`, terminal=`bash,process,terminal`. Compilation removes unreachable variants by position/request. |
| `ShellWiring` | `capability.py` | Typed per-agent artifact carrying config, live `SandboxBinding`, initial cwd, native main/sub position, and agent name. It is the only dependency channel into the group factory. |
| `ShellToolGroupFactory` | `factory.py` | The only `bash` TOOL-slot factory. Creates exactly one per-agent `ToolGroup`, reports requested/effective/reason/substrate, and owns construction rollback. |

## Invariants

- Companion names are group members, never independent TOOL registrations or declaration controls. Edit/veto the `bash` anchor.
- A subagent terminal request is retained and silently selects persistent or subprocess. A LOCAL/OCI terminal request downgrades within that substrate; it never selects the HOST terminal group.
- Persistent resources own one `PersistentShellManager`. Terminal resources own one manager plus watchdog. Both implement idempotent `ToolGroupResource.aclose()` and are adopted by assembly's single resource owner.
- `terminal_visibility` is read only when a native main agent actually attempts the HOST terminal variant.
- Missing capability wiring is an assembly error with the declaration repair path; no bare `bash` compatibility construction exists.

## Tests

- `tests/unit/plugins/test_shell_capability.py` covers config, contextual manifests, selection matrix, structured reporting, rollback, and per-agent resource isolation.
- `tests/unit/plugins/test_native_core_tool_groups.py` covers native manifest validation and resource transfer.
- `examples/bot_project/tests/unit/plugins/test_bot_shell.py` covers project-priority auto-application policy.
