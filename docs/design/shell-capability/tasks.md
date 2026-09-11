# Shell Capability Convergence Tasks

Parent contract: [SPEC.md](SPEC.md).

## Status

Implementation and coordinator acceptance are complete. The implementation
commit and separate root handoff document are the remaining delivery steps.

## Completed Implementation

- [x] Add frozen compile-time `ToolGroupSpec` / `ToolGroupVariant` models and
  runtime `ToolGroup` / idempotent `ToolGroupResource` contracts.
- [x] Extend capability contributions and compiled assembly specs with atomic
  tool-group manifests; validate anchors, variants, members, overlaps, and
  companion edits.
- [x] Add the framework `shell` capability, typed per-agent `ShellWiring`, and
  one `ShellToolGroupFactory` with the complete HOST/LOCAL/OCI mode matrix.
- [x] Preserve requested subagent terminal configuration while compiling only
  reachable persistent/subprocess candidates and selecting a silent runtime
  downgrade.
- [x] Make `ToolManager.register_group`, filtering, unregister, and graph views
  preserve group atomicity; graph views borrow tools without resource ownership.
- [x] Converge sandbox guard and shell resources on one ordered
  `AssemblyResourceOwner`, transfer ownership to `AgentInstance`, and retain
  prerequisites when reverse cleanup fails.
- [x] Add bot project auto-application policy in `BotShellCapability`; move
  configuration to `capabilities.shell`; expose candidate manifests through
  scope bill/options and the Settings -> Pools capability UI.
- [x] Remove the production faces `ensure_input_companion`, `build_bash_tool`,
  `ShellAssemblyDeps`, `use_terminal`, and `PoolInstance.terminal_manager` with
  no legacy shim.
- [x] Replace old-path tests with tool-group, shell matrix, lifecycle,
  serializer/API, and frontend coverage mapped in SPEC section 7.
- [x] Synchronize the living design, ADR-0047 refinement, scope/sandbox docs,
  and targeted module AGENTS documentation.

## Evidence Scope

- **Code-inspected:** the implementation paths named in SPEC
  sections 2-6 were read from the current worktree, including core group
  models, compiler validation, native assembly, resource ownership, shell
  capability/factory, bot policy, serializer, REST models/routes, and frontend
  types/components.
- **Coverage-present, not executed here:** the test files in SPEC section 7
  contain focused cases for the recorded contract. Their presence is not a
  passing-suite claim.
- **Agent-reported evidence rule:** a coordinator may cite an implementation
  agent's result only as `reported` and with the exact command/platform/scope.
  Reported evidence does not close the final coordinator gate.

## Coordinator Acceptance

- [x] Review the complete runtime and test diff against [SPEC.md](SPEC.md),
  including deleted-path cleanup and unrelated-worktree separation.
- [x] Run the final scoped and repository-required verification commands on the
  coordinator's target platform; record exact commands and outcomes here.
- [x] Confirm local design links and removed production-symbol searches are clean.
- [ ] Commit the implementation and documentation together with only intended
   files staged.

### Executed checks (coordinator, macOS arm64 / Python 3.12.10)

Pytest commands use `PATH="$HOME/.local/share/opencode/bin:$PATH"` so the
existing ripgrep fixture can use the installed binary instead of downloading.
Framework and bot suites run in separate processes because both test trees
contain a `tests` Python package; combining both roots in one collection
collides on conftest and relative-import module names.

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests -q --disable-warnings` | 11,171 passed; 50 skipped; 145 integration tests deselected |
| `.venv/bin/python -m pytest examples/bot_project/tests -q --disable-warnings` | 2,599 passed; 6 skipped; 29 integration tests deselected |
| `.venv/bin/python -m pytest tests -m integration -q --disable-warnings` | 123 passed; 22 skipped |
| `.venv/bin/python -m pytest examples/bot_project/tests -m integration -q --disable-warnings` | 27 passed; 2 skipped |
| `npm test -- --run` in `examples/bot_project/webui` | 1,085 passed in 101 files |
| `npm run build` in `examples/bot_project/webui` | TypeScript and Vite passed; existing chunk-size advisory |
| Ruff on changed/new Python files | Passed |
| Mypy `--follow-imports=silent` on 14 group/shell/assembly/lifecycle source files | Passed |
| `.venv/bin/python scripts/verify_slot_gates.py` | 18/18 gates passed |
| `git diff --check` | Passed |

Platform/optional-dependency skips remain explicit; this is not evidence of a
live Windows or Linux kernel-sandbox run. No new skip/xfail was used to hide an
implementation failure. The POSIX terminal integration and fake-substrate
selection/ownership matrix exercise the local supported paths.

### Independent review fixes verified

- Drain admission closes before resource cleanup; incomplete drain retains
  owners in pool and standalone callers.
- Cassette wrappers and graph/filter views preserve tool-group identity.
- Mode/topology manifests reject forbidden runtime variants; mixed roster
  syntax cannot accidentally enable shell on `none`/`web`.
- Shell closes before its sandbox prerequisite; failed close retains the
  dependency stack for retry.
- The real terminal manager retains a tab until termination succeeds.
- Failed nested rollback remains retryable; successful cleanup is idempotent.
- Failed post-registration callbacks remove only their own resident, leaving
  a concurrent replacement untouched.

The final independent review rechecked the last three cleanup/publication
findings, ran six targeted regressions, and reported READY.
