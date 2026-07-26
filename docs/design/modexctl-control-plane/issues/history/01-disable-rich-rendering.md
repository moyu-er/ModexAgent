# 01 — Disable rich rendering across all modexctl commands

**What to build:** From the agent's perspective, every `modexctl` invocation — `--help`, any subcommand's `--help`, and any exception trace — produces plain-text output free of box-drawing characters (`─│┌┐└┘├┤┬┴┼`) and rich panels. ANSI color highlights remain (harmless to agents reading `2>&1`, useful for human debugging). This is the foundational ticket: subsequent tickets' `--help` assertions depend on this output shape being stable first.

The change is at the Typer app construction layer: disable rich markup mode and pretty exceptions. No command behavior changes; only the rendering of help and errors. Existing `send` and `agents` commands continue to function identically.

**Blocked by:** None — can start immediately.

**Status:** done

- [x] `modexctl --help` output contains no box-drawing characters (`─│┌┐└┘├┤┬┴┼`).
- [x] `modexctl send --help` output contains no box-drawing characters.
- [x] `modexctl agents --help` output contains no box-drawing characters.
- [x] `modexctl send --invalid-flag` (or any unknown-argument invocation) produces a plain-text exception trace with no rich panels.
- [x] `modexctl <unknown-command>` produces a plain-text error with no rich panels.
- [x] Existing `send` command stdout is byte-identical to before (the change affects only help/error rendering, not command output).
- [x] Existing `agents` command stdout is byte-identical to before.
- [x] All existing tests in `tests/unit/cli/modexctl/` pass (`TestUnifiedCommGate`, `TestSendCommand`, `TestAgentsCommand`, `TestStaleAppFailClosed`, `TestWorkflowCommandGate`, `TestParsePoolMap`, `TestBuildInboxLine`, `TestComputeTargetSessionId`, `TestResolveTargetPool`, `TestParseTargets`, `test_sqlite_persistence_unification.py`, `test_parent_session_id_propagation.py`, `test_external_coding_communication.py`, `test_cross_pool_peer_messaging.py`).
- [x] ANSI color codes may still appear in `--help` / error output (Typer default) — this is acceptable and not a failure.

## Comments

### Env-gating note (applies to all tickets in this feature)

The modexctl CLI's command surface is env-gated by construction (ADR-0022). The five `MODEX_COMM_*` env vars (`MODEX_SESSION_ID`, `MODEX_AGENT_NAME`, `MODEX_INBOX_ROOT`, `MODEX_AGENT_POOL_MAP`, `MODEX_TARGETS`) gate `send` and `agents`. This ticket does NOT change env-gating — it only changes rendering. The `--help` output shape assertions above apply to whatever commands are registered under the current env (i.e., the test fixture `comm_env` satisfies the gate so `send`/`agents` are visible in `--help`).

The `ready-for-agent` label means: an AFK agent can pick this up and implement it without further triage. The acceptance criteria are the contract.
