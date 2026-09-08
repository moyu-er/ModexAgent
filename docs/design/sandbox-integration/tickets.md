# Sandbox Integration Tickets

Parent design: [PRD.md](PRD.md). Implemented scope is mapped below using the existing ticket numbers; validation is bounded by the evidence and gaps recorded here.

| Ticket | Implemented scope | Coverage and limits |
|---|---|---|
| 01 Guards and types | Typed verdicts, canonical boundaries, built-in deny independent of advisory switches | Hard-deny priority, known file targets, HOST best-effort limits, independent SSRF checks |
| 02 Configuration and DEFAULT | `SandboxSettings`, scope roster and nested sandbox configuration | DEFAULT has no probe/interceptor; independent approval/delegation remain; bot sandbox stays off |
| 03 Selection/runtime | Typed engines, canonical runtime roots, `resolve_available`, shared `SandboxBinding` | Main/subagent HOST fallback, per-session telemetry, generic launcher PermissionError propagation |
| 04 Shell binding | Common persistent/one-shot bash construction and input companion | cwd/env/markers, no-PTY path, companion identity, reader cancellation before reuse |
| 05 bwrap | Policy argv and pre-command no-op; full-access retains LOCAL writable host bind | Real WSL bwrap execution; network setting and initialization/command-failure distinction |
| 06 Seatbelt | Profile compilation, startup validation and cleanup | Simulated macOS selection/profile behavior only; live execution unverified |
| 07 OCI/executor | Docker/Podman selection, lifecycle/config hash, mount probe, full bash `-c` argv | Real WSL Docker execution and no-replay coverage; Podman selection simulations only |
| 08 Guard assembly/feedback | Shared decision implementation, execution backstop, approval anchors and uncertainty | ToolNode/transaction/resume, role-specific permission actions, truthful HOST terminal identity |
| 09 Base image | Existing `scripts/docker/sandbox/` image/build entry points | Real Docker path; no inferred Podman/macOS execution guarantee |
| 10 Documentation | Existing README, module guides, PRDs, tickets and relevant index lines | Current source/configuration semantics, English-only owned feature docs, no additional document hierarchy |

## Validation Evidence

Full regression results rerun before regrouping the implementation commits:

| Check | Reported result | Scope |
|---|---|---|
| Windows full suite | 10,499 passed; 119 skipped | Unit, conformance and architecture |
| WSL full suite | 10,585 passed; 49 skipped | Unit, conformance and architecture; real bwrap/Docker cases |
| Bot pool wiring | 21 passed on Windows; 21 passed on WSL | Real pool assembly/wiring patterns |
| mypy | Passed for 65 source files | Scoped source checks |
| Ruff | Passed for changed files | Scoped lint checks |

Warnings remain. Live macOS and installed Podman execution were unavailable; their coverage is simulated selection/profile behavior, not live validation. These results are not a bug-free or all-platform guarantee. [Security tickets](../unified-security/tickets.md#validation-scope) describe approval/delegation/audit coverage without duplicating suite counts.

Run the suite from the repository root in the appropriate Windows or Linux environment:

```sh
python -m pytest tests/unit tests/conformance tests/architecture -q -n 6 --tb=short --disable-warnings --timeout=120
```

## Retained Constraints

- Available LOCAL/OCI uses the selected engine; genuine unavailability preserves ordinary HOST bash for both native roles. Fallback does not authorize a call.
- Native main approval is independent of sandbox. Active BOUNDARY becomes pending when enabled, even with an empty tools map; disabled approval returns denial. Hard findings never escalate.
- Native subagents keep fixed known-file read/write roots and no human escalation. Parent READ_ONLY is preserved; later config changes do not alter the snapshot.
- Only confirmed pre-command startup unavailability permits fallback. Possibly-submitted commands are never automatically replayed or retried on HOST.
- All bash implementations share judging, not execution identity. `process`/`terminal` remain HOST; `bash_input` follows the selected persistent manager.
- HOST string checks do not confine arbitrary code or filter inherited credentials. External provider tools remain outside framework enforcement; metadata must not imply it.

## Outside Delivery

Windows-native isolation, microVMs and other new-backend research are not implemented by these tickets. Per-command HOST routing, approval-triggered replay and default secret-environment filtering are not part of the contract.
