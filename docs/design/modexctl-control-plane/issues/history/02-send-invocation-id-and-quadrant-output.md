# 02 — send gains `--invocation-id` and quadrant-differentiated output

**What to build:** From the agent's perspective, invoking `modexctl send --to <name> [--content <text> | --content-file <path> | --stdin] [--invocation-id <id>]` produces output that tells me exactly what happened: whether I dispatched a new subagent task or resumed one, what the invocation_id and session_id are, and (for native subagents) where the output and trace files live. The output format is differentiated across four quadrants indexed by `MODEX_COMM_KIND` and target kind, so I only see fields meaningful for my situation. The CLI does not lecture me about how the recipient will reply.

The `--invocation-id` parameter mirrors the in-process `send_to_agent` tool: omitted/empty → mint a fresh 8-byte hex uuid (new subagent session); non-empty → attempt to resume `{id}.{agent_name}`. Before dispatching on the subagent path, the CLI queries the workspace's `state.db` `sessions` table to decide `new_task` vs. `resumed` vs. the "not found, created new" case. The not-found case mints a fresh uuid (NOT the provided id) and reports it prominently. `parent_session_id` mismatch is treated identically to "not found" (not deepened — cannot legitimately occur in single-main-per-pool topology).

Env-gating is unchanged: `send` continues to register under the five `MODEX_COMM_*` env vars. `--invocation-id` is silently ignored (not an error) when `MODEX_COMM_KIND=normal` (peer path has no invocation_id concept).

The four quadrant output templates (exact formatting) are defined in the spec under "D2.2 — Quadrant-differentiated output". The "not found" status form is `new_task (provided '{provided_id}' not found, created new)`, with the `invocation_id:` line showing the newly minted uuid.

**Blocked by:** 01 — Disable rich rendering across all modexctl commands.

**Status:** done

- [x] `send --to X --content Y` (no `--invocation-id`, `MODEX_COMM_KIND=subagent`, target = NATIVE) → quadrant ② output with `status: new_task`, plus `invocation_id`, `session_id`, `output_path`, `trace_dir`, and the "wait for `<replied>`" footer.
- [x] `send --to X --content Y` (no `--invocation-id`, `MODEX_COMM_KIND=subagent`, target = EXTERNAL) → quadrant ③ output with `status: new_task`, plus `invocation_id`, `session_id`, NO `output_path`/`trace_dir`, and the "wait for `<replied>`" footer.
- [x] `send --to X --content Y --invocation-id foo123` (session `foo123.X` exists in `sessions` table) → `status: resumed`, `invocation_id: foo123`.
- [x] `send --to X --content Y --invocation-id foo123` (session `foo123.X` does NOT exist in `sessions` table) → mints a NEW uuid (not `foo123`), `status: new_task (provided 'foo123' not found, created new)`, and the `invocation_id:` line shows the new uuid (not `foo123`).
- [x] `send --to X --content Y` (`MODEX_COMM_KIND=normal`, cross-pool peer) → quadrant ① output: `Message delivered to '{to}'.\nPeer will process asynchronously. No wait needed.` — no `session_id`, no `invocation_id`.
- [x] `send --to X --content Y` (subagent → parent reply path, `MODEX_COMM_KIND=subagent` targeting parent) → quadrant ④ output: `Reply delivered to parent (session: {parent_sid}).\nParent will continue its turn.`
- [x] No output template (any quadrant) mentions how the recipient will reply — no "the peer will use `modexctl send`" or similar statements about the recipient's reply mechanism.
- [x] `--invocation-id ""` (empty string) behaves identically to omitting the flag (mints new uuid, `status: new_task`).
- [x] `--invocation-id` is silently ignored (not an error, no effect on output) when `MODEX_COMM_KIND=normal`.
- [x] The session-existence check uses a short-lived stdlib `sqlite3` connection to `<workspace>/.modex/state.db` with `SELECT 1 FROM sessions WHERE session_id = ?` (same cross-process pattern as `SqliteInboxMQ.deliver()`).
- [x] Existing same-pool subagent dispatch behavior (mint invocation_id, write `task_request` to inbox with `parent_session_id` + `invocation_id` metadata) is preserved — only the CLI's stdout reporting changes, not the inbox write.
- [x] Existing cross-pool peer send behavior (prefix-reuse, `build_agent_comm_message` XML with `<reply_contract>`) is preserved — only the CLI's stdout reporting changes.
- [x] Existing subagent → parent reply behavior (`MODEX_PARENT_SESSION_ID` verbatim as target_sid) is preserved — only the CLI's stdout reporting changes.
- [x] The `TestStaleAppFailClosed` suite (fail-closed when env vars removed/emptied after build) still passes for `send`.

## Comments

### Why this is blocked by 01

The D2 acceptance criteria include assertions about `send --help` output shape (the new `--invocation-id` parameter must appear in help without box-drawing noise). Ticket 01 stabilizes the help rendering first so D2's help assertions have a stable baseline.

### Quadrant differentiation inputs

The CLI determines the quadrant from two inputs already available without new env vars:
1. `MODEX_COMM_KIND` env var (`normal` vs. `subagent`) — already set by `ExternalEnvBuilder`.
2. Target agent kind (NATIVE vs. EXTERNAL) — derivable from `MODEX_AGENT_POOL_MAP` + the existing pool/agent registry, OR from whether `MODEX_TARGETS` lists the agent as external. The implementation should reuse whatever signal the existing `send` command already uses to decide `build_dispatch_message` vs. `build_agent_comm_message`.

If the target kind cannot be determined at CLI time (e.g., the agent is not in `MODEX_TARGETS`), the implementation should default to NATIVE (the more informative quadrant — includes `output_path`/`trace_dir`). This is a safe default because external subagents are a configured minority.

### Not-found race (accepted)

The session-existence check has a TOCTOU window: a second `send --invocation-id X` invoked before the bot has registered session X from the first send will mint a new uuid. This is documented in the spec ("only pass `--invocation-id` for sessions you have received a `<replied>` for") and is NOT a bug to fix. The acceptance criteria above do not require race-free behavior.
