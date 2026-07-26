# 12 — CLI output redesign (kind labels, cleaned output, positional args)

**What to build:** Redesign the CLI output vocabulary to remove internal
terms and add agent-friendly features:

- `agents` shows `(subagent)` / `(normal)` kind labels with behavioral
  docs explaining what each kind means. When the caller is a subagent,
  the view shows only the parent agent (not the full target list).
- `send` accepts positional message arguments as the primary input
  method: `modexctl send "hello world"` instead of
  `modexctl send --content "hello world"`. This eliminates Windows CMD
  single-quote quoting failures.
- `send --to` defaults to the parent for subagents, so a subagent can
  reply with `modexctl send "continue"` without specifying the target.
- All user-facing output is cleaned of internal terms: no "peer",
  "control server", "ReAct", "session_id", "output_path", "trace_dir",
  or env var names in error messages (error messages include the
  missing env var name for diagnostics, but not in success output).
- Main-agent fallback in `send()`: when the parent is not in the
  `CommunicationTargetStore`, synthesize the target so the subagent can
  still reply.

**Blocked by:** 10 (ModexCtlContext provides the smart defaults).

**Status:** done (commit e414b304)

- [x] `agents` output shows `(subagent)` / `(normal)` kind labels.
- [x] `agents` output includes behavioral docs for each kind.
- [x] Subagent `agents` view shows only the parent agent.
- [x] `send` accepts positional message arguments as primary input.
- [x] `--content`, `--content-file`, `--stdin` remain as fallbacks.
- [x] `send --to` defaults to parent for subagents.
- [x] All user-facing output is free of internal terms.
- [x] Error messages include missing env var name for diagnostics.
- [x] Main-agent fallback: `send()` synthesizes target when parent not
      in `CommunicationTargetStore`.
- [x] 3 send subagent view tests (`--to` default, override, required).
- [x] 2 agents subagent view tests (parent-only, empty).
