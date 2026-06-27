<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-27 -->

# Token-based session compression (drop message-count, make tokens the sole budget)

## Context

Session (short-term) memory compression was triggered by **two** OR-ed conditions in
`_check_trigger`: a hard `max_messages` count and a `max_tokens` token estimate. Both
were unreliable in production:

- **Message count is not a budget.** One giant tool result and one short reply each count
  as "1 message." `max_messages=100` over-triggers on many small messages and silently
  under-triggers on a few huge ones. Worse, the keep/prune boundary (`_compute_boundary`)
  was computed by *walking message count*, so even a token-triggered compression kept an
  unpredictable amount of context.
- **The token estimate was crude and blind.** `estimate_token_count` (char heuristic:
  ASCII 4:1, CJK 1:1) counted only session message content, ignoring the system prompt,
  injected knowledge, and — critically — tool definitions and tool-call JSON. The
  `max_tokens=100000` threshold was a hand-set constant disconnected from the model's real
  context window or completion budget. The trigger could believe "under budget" while the
  actual prompt (tools + knowledge + history) already overflowed.
- **Two disconnected token systems.** The compression trigger (`estimate_token_count`) and
  the request-time safety net (`TokenBudgetGovernance`, which does head-truncation and
  counts more) each estimated tokens independently with different scopes, so compression
  fired late and the lossy governance truncation became the de-facto primary mechanism.

The example bot (`examples/bot_project`) is the reference consumer; its compression is
what must become trustworthy.

## Decision

Make **token count the sole compression budget** and remove message count entirely.
Both ratios are applied to a single reference base `max_tokens`.

### Token estimator (swappable seam)

- Framework defines a `TokenEstimator` ABC. **Every** site that counts tokens (the
  compression trigger and `TokenBudgetGovernance`) receives an injected estimator
  instance — there is no direct `tiktoken`/heuristic call scattered in framework code.
- Framework default: a char-based `CharTokenEstimator` (zero-dependency fallback).
- Business layer (`examples/bot_project`) provides `TiktokenTokenEstimator`, estimating
  tokens over all message fields (content, name, tool_call_id, tool_calls JSON,
  reasoning_content, plus per-message overhead) and encoding with the `cl100k_base`
  tokenizer. The encoding blob is **vendored** so the bot is fully offline (no first-run
  download). `tiktoken` is added to the bot's `pyproject.toml`.
- Counting scope: every session message role **except system** — user / assistant / tool /
  agent, including assistant `tool_calls` JSON and tool `name`/`tool_call_id`. System and
  the `UserRetentionBuffer` are not in the session message stream, so they are naturally
  excluded.

### Threshold model (both ratios act on `max_tokens`)

| Field | Meaning | Default |
|---|---|---|
| `max_tokens` | Reference base (nominal context window) | 200000 |
| `max_token_ratio` | Trigger line = `max_tokens × this` | 0.8 (clamped 0.4–0.9) |
| `keep_ratio` | Post-compression retention target = `max_tokens × this`; **hard cap** | 0.3 |

Compression fires when `Σ message tokens > max_tokens × max_token_ratio`. After
compression the kept region must not exceed `max_tokens × keep_ratio`.

`max_messages` is **removed** from `SessionMemoryConfig`. Config migration and the pool
YAML configs are hard-cut (no compatibility shim, per the project's clean-over-compat
rule).

### Boundary: token-accumulated, single cut, tool chains evicted forward

- `_compute_boundary` walks backward from the tail **accumulating tokens** until the
  next message would exceed the keep target, instead of counting messages.
- `_adjust_boundary_for_tool_chains` is **flipped**: when the boundary splits a tool
  chain, it moves the boundary **forward** (toward the tail), evicting the incomplete
  chain into the pruned region so it is archived — never backward into the kept region.
  This is the one piece of original logic that changes direction: the previous code
  moved the boundary backward to preserve chains, which violated the hard keep cap.
- `keep_ratio` is a **hard** cap; tool-chain integrity is satisfied by evicting, not by
  exceeding the cap. The sanitizer (`_resanitize_keep`) remains as a final safety net for
  any residual orphan; it is not the primary eviction path (it silently drops orphans
  rather than archiving them).
- Single cut, not a multi-round loop — the framework's archive is an
  archive-and-summarize step, and one cut is simpler and sufficient.

### Per-message token cache

- `ChatMessage` gains `token_count: int | None`. It is computed with the injected
  estimator at append/extend time (the natural write point) and persisted.
- The trigger (sum) and the boundary walk (tail accumulation) read cached values — no
  re-encoding per turn.
- On read, a missing `token_count`, or one that fails a cheap tamper sanity check
  (`cached < len(str(content)) / 5`), is recomputed **transiently** (not written back).
  This avoids the awkward single-field rewrite of the whole message file while still
  detecting corruption/tampering.
- Estimator swap (e.g. char → tiktoken across runs) leaves cached values approximately
  correct (same field coverage, ~20% magnitude drift); the bounded error is accepted, and
  `TokenBudgetGovernance` remains the request-time backstop. No per-message version stamp.

### Reused unchanged

The six-phase cleanup orchestration, archive generation, backup, `UserRetentionBuffer`
extraction, and the sanitizer's final pass are reused as-is.

### Retrieval: no message-count caps (token compression is the sole governor)

A parallel legacy mechanism hard-truncated retrieved session messages by **count**,
independently of compression, silently dropping messages without archiving them. This
broke the memory structure: the whole point of compression is to decide what stays via
tokens, but these caps pruned the visible context on a different axis. They are removed.

- `SessionMemoryConfig.max_messages` is removed. `get_recent_messages(limit=None)`
  returns **all** stored messages when no explicit limit is given (no fallback to a
  config default). `ScopedMessageHistory.to_list()` therefore returns everything token
  compression chose to keep — for both the main agent and subagents.
- `MemoryBudget.max_history_messages` and the `max_messages` argument on the full
  injection policy's history read are removed (the field defaulted to `None` and was
  never set, so this is dead-parameter cleanup).
- `RestrictedInjectionPolicy.max_session_messages` (the subagent 50-message cap) is
  removed. This is safe because subagent memory is built with a cleanup config, so
  subagent sessions compress by tokens just like the main agent — they do not need a
  separate count cap to stay bounded.

The one count-based control that **stays** is `fork_max_messages` on the subagent
template: it governs how much parent context is copied when **forking** a subagent's
seed memory (a volume-of-seed concern), not retrieval of a session's own messages. It is
a different semantic and is out of scope for this ADR; it may be token-ized later.

## Considered options

- **Estimator**: tiktoken `cl100k_base` (chosen) vs. refined char heuristic vs.
  provider-side counter. tiktoken chosen for accuracy and mature-OSS fit; char kept only
  as the framework fallback; provider counter rejected as too much per-provider work.
- **max_tokens meaning**: a nominal reference base (chosen) vs. a pre-deducted history
  budget vs. completion-derived. Nominal base chosen — the ratio is the tuning knob and
  governance is the hard backstop.
- **keep_ratio base**: a fraction of `max_tokens` (chosen) vs. a fraction of the trigger
  threshold. Same base for both ratios keeps the mental model clean.
- **Cut strategy**: single token-accumulated cut (chosen) vs. a multi-round loop that
  archives one chunk per round. Single cut for simplicity given the archive-and-summarize
  flow.
- **Tool-chain conflict**: evict forward into pruned, keep cap hard (chosen) vs. preserve
  the chain and exceed the cap. The cap is the budget contract; orphan chains must not
  break it.
- **Per-message cache tamper handling**: transient recompute on read (chosen) vs.
  persist-on-recompute vs. no guard. Transient recompute detects tampering without the
  whole-file rewrite.
- **Retrieval count caps**: remove all of them — `SessionMemoryConfig.max_messages`,
  `MemoryBudget.max_history_messages`, and `RestrictedInjectionPolicy.max_session_messages`
  (chosen) — so token compression is the sole size governor; keep only `fork_max_messages`,
  which is fork-seed volume, not retrieval. Verified safe because subagent sessions carry
  a cleanup config and compress by tokens.

## Consequences

- Positive: the compression budget reflects real prompt weight (all non-system roles);
  the keep region size is predictable in tokens; trigger and governance share one
  estimator so compression fires at the right time instead of relying on lossy governance.
- Positive: estimator is swappable at one seam; the bot can later move to a precise
  provider counter without touching framework call sites.
- Negative / cost: `max_messages` removal is a breaking config change — pool YAMLs,
  migration code, and the cleanup/config test suites must be rewritten. The existing
  cleanup tests almost universally drove compression via `max_messages` with
  `max_tokens=None`, so the token path was effectively untested; the rewrite uses a
  deterministic test `TokenEstimator` fake and TDD.
- Negative: a vendored ~1.5 MB tokenizer blob ships with the bot.
- Positive: removing the legacy retrieval count caps ends silent message loss; what the
  model sees is exactly what token compression chose to keep — for main agents and
  subagents alike.
- Negative / cost: removing `SessionMemoryConfig.max_messages`,
  `MemoryBudget.max_history_messages`, and `RestrictedInjectionPolicy.max_session_messages`
  touches the injection policies, the memory budget, the session layer, the subagent
  memory factory, and their tests. `fork_max_messages` is intentionally kept (fork-seed
  volume, different semantic).
- Open assumption (to confirm): compression is still evaluated after each
  append/extend — now cheap because the trigger sums cached per-message tokens. If a
  coarser cadence is wanted, that is an orchestration change layered on top of this ADR.
- CONTEXT.md gains domain terms: **Session Compression**, **Token Estimator**,
  **Compression Trigger Ratio** (`max_token_ratio`), **Keep Ratio**.
