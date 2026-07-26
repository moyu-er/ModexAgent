# 11 — History ungating and target authorization

**What to build:** Remove the `MODEX_COMM_KIND=subagent` gate from the
`history` command so all agents can read their own history. Add
`--agent` and `--invocation-id` as optional parameters for self-history.
When both are omitted, the CLI reads the caller's own session history.

Add bot-side target authorization: a caller may read only its own
session history or the history of a subagent registered under the
caller's session. Unauthorized reads return `403 forbidden_target`.
Empty `session_id` returns `400 invalid_request`.

Also remove the unnecessary session validation in `history()` that was
blocking subagent history queries (the validation was too strict and
rejected valid subagent sessions).

**Blocked by:** 04 (native history), 05 (external history).

**Status:** done (commit e414b304)

- [x] `history` command is registered for all agents (not gated on
      `MODEX_COMM_KIND=subagent`).
- [x] `--agent` and `--invocation-id` are optional. When both omitted,
      the CLI reads the caller's own session history.
- [x] When `--agent` and `--invocation-id` are provided, the CLI
      constructs the subagent session id and queries that session.
- [x] Bot enforces target authorization: caller may read own sessions
      or registered subagents' sessions only.
- [x] Unauthorized reads return `403 forbidden_target`.
- [x] Empty `session_id` returns `400 invalid_request`.
- [x] 9 history validation tests covering normal/subagent
      perspectives, forbidden target, empty session, peer.
- [x] 3 facade authorization tests (forbidden target, empty session,
      peer).
