# Terminal trio simplification — silence is not failure

The terminal trio (`bash`/CommandTool, `process`/ProcessTool, `terminal`/TerminalTool) judged
command health from output silence — STUCK after 30 s without output, LONG_RUNNING after 300 s,
PAGINATED, periodic YIELDED partial returns — producing false verdicts on legitimately slow
commands (a long `grep`, a build) and an eight-branch outcome machine spread over four status
vocabularies. Decision (settled in deliberation):

1. **Silence never judges failure.** `bash` returns exactly three outcomes: `completed`
   (prompt-stability or process-exit evidence), a `waiting_input` **advisory** (≥ ~10 s quiet
   AND positive stdin-wait evidence — Linux kernel `/proc` probe ∪ content markers, content-only
   on macOS/Windows; single soft wording, the agent judges), or `timed_out` (the 480 s command
   deadline).
2. **The 480 s deadline is manager-side and closes the tab** like `terminal close` (default tab
   reselects automatically). An in-flight `bash` call returns `timed_out` with partial output and
   a reset notice; if the tool already returned an advisory, a background watchdog closes the tab
   at the deadline instead. Mirrors PersistentBashTool's kill-and-reset model, with the deadline
   strictly below the executor's 540 s tool timeout so the graceful path always fires first.
3. **`process` collapses to write-only** — schema `{data: str, submit: bool = true}`, no action
   enum. `submit` owns newline semantics (true → strip trailing newline, send one `\r`; false →
   raw bytes verbatim). `^C` / `ctrl+c` / `\x03` are interrupt operators routed to a real
   Ctrl+C, never typed as text.
4. **`terminal` keeps `open` (no `cwd` parameter — new tabs start at the workspace directory) /
   `close` / `list` / `select`**; `interrupt` and `current` are deleted.

## Considered Options

- Keep the heuristic apparatus with better thresholds — rejected: thresholds cannot distinguish
  "slow" from "stuck"; only evidence or a hard deadline can.
- dsh-style tiered confidence labels (`stdin_read` vs `inferred_idle`) — rejected: single soft
  advisory wording; the agent judges, the tool does not claim confidence levels.
- Kernel-evidence-only advisory (no content fallback) — rejected: dsh itself has no macOS kernel
  stdin evidence; content detection stays as the portable floor with the 10 s quiet gate
  suppressing most false positives.

## Consequences

Pager handling converges into the input-wait path (a pager is a process blocked reading stdin);
STUCK/LONG_RUNNING/YIELDED/PAGINATED outcomes, the tiered idle thresholds, output-velocity
heuristics, and roughly ten `TerminalRuntimeConfig` fields (several already dead) are removed;
the four status vocabularies shrink accordingly. The persistent pair
(`PersistentBashTool`/`BashInputTool`) deliberately keeps its v2 semantics this round; converging
it to the same write/^C semantics is deferred.
