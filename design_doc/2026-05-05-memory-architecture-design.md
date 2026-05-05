# ModexAgent Multi-Level Memory Architecture Design

> Date: 2026-05-05
> Scope: framework memory architecture and examples/bot_project memory policy
> References: `references/nanobot`, `references/hermes-agent`

## 1. Background

The current memory implementation already has a flexible multi-layer shape:

- `framework/memory/core/`: message, scope, layer, storage, and system contracts.
- `framework/memory/layers/`: session, archive, and knowledge layer implementations.
- `framework/memory/compression/`: compression coordinator, semantic filter, tool-chain repair.
- `framework/memory/compaction/`: message compaction decisions and boundary policy.
- `framework/memory/archive/`: archive strategies.
- `framework/memory/consolidation/`: DreamEngine for archive-to-knowledge consolidation.
- `framework/memory/injection/`: prompt assembly and retrieval injection.
- `examples/bot_project/`: application-level QQ bot memory configuration and plugins.

The framework positioning is broader than both reference projects. It must support single-agent,
multi-agent, peer/subagent scopes, plugins, multiple stores, and different agent styles. The right
direction is therefore not to copy nanobot or hermes-agent directly, but to extract their durable
memory invariants into pluggable framework contracts.

Current observed problems:

1. `SemanticArchiveStrategy` writes useless archive entries with summary
   `"(no semantic content)"`.
2. `DreamEngine` and injection filtering do not consistently reject all empty archive markers.
3. `DefaultMemoryCompressionCoordinator` has compaction policy types, but does not wire decisions
   into boundary and summary planning.
4. Tool-call and tool-result messages can leak into summary/archive paths in ways that are not
   clearly policy-driven.
5. Short-term compression is mostly count/token-triggered and does not consistently model complete
   user turns, safe suffix retention, or archiveable prefixes.
6. `examples/bot_project` has a useful `tool_call_cleanup` plugin, but it is disabled by default
   and is not integrated with a larger memory policy story.
7. `DefaultCompressionTriggerPolicy` can repeatedly trigger on `len(all_msgs) > len(visible)` even
   when the visible window is already within budget, causing no-op compression attempts.
8. Compression state and result semantics are ambiguous: `.last_compression` is used as a cooldown
   counter, while empty-summary paths can return `committed=True` even when no archive write or
   session mutation happened.
9. Context-only microcompaction replaces old tool results with very generic placeholders, which is
   safe for persistence but weak for LLM context quality.

## 2. Reference Lessons

### 2.1 nanobot

Relevant files:

- `references/nanobot/nanobot/agent/memory.py`
- `references/nanobot/nanobot/session/manager.py`
- `references/nanobot/nanobot/agent/autocompact.py`
- `references/nanobot/nanobot/agent/loop.py`

Useful ideas:

1. Consolidation has a cursor-like concept, `last_consolidated`, so old messages are not repeatedly
   summarized.
2. Compression boundaries prefer complete user turns and legal suffixes, not arbitrary message
   indexes.
3. Empty archive input is skipped. A memory system should not persist "nothing happened" records.
4. Tool metadata can be represented as compact breadcrumbs, while raw tool output is capped and
   usually not used as long-term semantic material.
5. Auto-compact keeps a recent suffix and archives only an older, complete, non-empty prefix.
6. Session loading avoids orphan tool results and avoids starting history from illegal protocol
   boundaries.

### 2.2 hermes-agent

Relevant files and behaviors:

- `references/hermes-agent/agent/anthropic_adapter.py`
- `references/hermes-agent/tools/tool_output_limits.py`
- `/compact` behavior found through command handling references.
- session search and memory tool references.

Useful ideas:

1. `/compact` refuses empty history with a clear "nothing to compress" behavior.
2. Compression has cooldown/failure pause semantics, so failed compaction does not repeatedly hit
   the hot path.
3. A cheap pre-pass can replace large tool outputs with placeholders before LLM summarization.
4. Provider-specific protocol safety matters: orphan `tool_use` and `tool_result` blocks must be
   stripped or repaired before LLM calls.
5. Tool output limits should be configurable and centralized instead of scattered hardcoded caps.
6. Search over past sessions is separate from blindly injecting all old summaries.

## 2.3 Issue Report Triage

The existing report `design_doc/memory-system-issues-and-recommendations.md` was reviewed and
folded into this design selectively.

Already covered by this design:

- Empty archive entries should not be persisted.
- Assistant messages with `tool_calls` and `tool` messages should not enter semantic summaries by
  default.
- `MessageCompactionPolicy` must be wired into the coordinator.
- Injection and DreamEngine need defensive empty-marker filtering for legacy archive records.
- bot_project should adapt through existing memory construction and plugin hooks.

Accepted additional issues:

- `len(all_msgs) > len(visible)` is not a reliable compression trigger by itself. In the current
  session manager, `visible` is a capped tail of `all_messages`; once stored history exceeds the
  visible cap, this condition can become permanently true while `prune_count` remains zero.
- `.last_compression` currently mixes cooldown and retained-message count semantics. It is usable
  as an incremental fix, but should be made coherent before adding more cursor concepts.
- Empty summary handling should not report a successful commit when no archive write and no session
  mutation happened. The result should communicate "skipped/nothing to archive".
- Default heuristic summaries are acceptable for tests and fallback, but should not become the
  primary semantic compression path for bot_project.
- Microcompact placeholders should carry basic tool context such as tool name, approximate size,
  and status when available.

Not adopted as first-slice problems:

- A new `EnvAwareSummaryStrategy` or separate compression architecture. bot_project already wires
  `SummarizerStrategy` through `_build_compression_coordinator(...)`; improve this wiring before
  adding strategy families.
- A new archive/session search API. Existing `get_history_entries(...)`, provider prefetch, and
  plugin memory tools should be reused first.
- Reworking `_find_tool_chain()` around tool-result-start lookup as a standalone change. Current
  boundary scanning starts from assistant tool-call messages; add regression tests first and only
  change the helper if those tests expose a real split/orphan bug.

## 3. Design Goals

1. Preserve framework flexibility: policies must be swappable and scoped by agent role, memory
   layer, session, or application.
2. Make memory flow explicit: short-term, archive, knowledge, and retrieval each have different
   responsibilities.
3. Keep framework code generic: bot-specific tool names, QQ behavior, and 12306 rules stay in
   `examples/bot_project`.
4. Avoid useless memory writes: empty summaries, placeholders, and metadata-only tool noise should
   not become archive entries.
5. Protect tool-chain legality in active context, while preventing raw tool dumps from polluting
   long-term memory.
6. Prefer typed decisions and structured data over raw strings and ad hoc dict handling.
7. Keep compression safe under pool mode and concurrent agents.
8. Maintain existing runtime shape where possible, but allow architecture changes when they remove
   ambiguity.

## 4. Non-Goals

1. Do not turn ModexAgent into a nanobot clone.
2. Do not hardcode bot_project memory behavior into framework modules.
3. Do not require vector memory or mem0 for the base framework.
4. Do not guarantee perfect semantic fact extraction in the first implementation phase.
5. Do not store raw shell, file, browser, or MCP outputs in long-term memory by default.

## 5. Proposed Memory Flow

```text
turn messages
  -> append/session storage
  -> optional application cleanup and tool-chain repair
  -> compression trigger
  -> legal boundary selection
  -> message classification
  -> summary input projection
  -> archive decision using existing ArchiveStrategy/CommitPolicy
  -> archive write
  -> session mutation or cursor advance
  -> DreamEngine archive-to-knowledge consolidation
  -> retrieval/injection for next turn
```

The important change is that "what is visible in active short-term context" and "what is eligible
for semantic memory" become separate policy decisions.

## 6. Layer Responsibilities

### 6.1 Short-Term Session Memory

Purpose:

- Preserve the current session's conversational continuity.
- Preserve provider-legal tool-call chains while they are still needed.
- Keep a recent legal suffix after compression.

Rules:

- Never cut through an active or incomplete tool-call chain.
- Prefer pruning at complete user-turn boundaries.
- Keep recent messages as a legal suffix, not just the last N raw rows.
- Empty assistant messages without tool calls should not be persisted.
- Very large tool results should be capped before persistence or projected before summary.

Reference mapping:

- nanobot: `last_consolidated`, legal suffix, complete user-turn boundary.
- hermes-agent: orphan tool block repair and protocol safety.

### 6.2 Archive Memory

Purpose:

- Store compressed, meaningful session history.
- Provide searchable medium-term history.
- Serve as DreamEngine input for long-term knowledge extraction.

Rules:

- Do not write archive entries for empty summaries.
- Do not write known placeholders such as `"(no semantic content)"`.
- Archive entries should carry structured metadata:
  - compression reason
  - source strategy
  - pruned count
  - semantic message count
  - tool breadcrumb list
  - source session and cursor range where available
- Raw fallback is allowed only for configured safe cases and must be capped.

Reference mapping:

- nanobot: no empty archive, raw archive only when there is content.
- hermes-agent: compact refuses empty input and caps tool outputs.

### 6.3 Long-Term Knowledge

Purpose:

- Store stable facts, user preferences, agent behavior notes, and durable task conclusions.
- Keep slow-changing knowledge separate from chat transcript summaries.

Rules:

- DreamEngine should only process meaningful archive entries.
- Tool outputs should not directly become long-term facts unless converted into user-visible,
  stable conclusions.
- Peer/subagent archives should not become main user knowledge unless explicitly promoted through
  main-agent communication.

Reference mapping:

- nanobot: archive first, then summarize durable facts.
- hermes-agent: memory snapshot and important fact flush before context reset.

### 6.4 Retrieval and Injection

Purpose:

- Assemble bounded context for LLM calls.
- Retrieve only relevant historical memory.

Rules:

- Session recent suffix is injected as messages.
- Archive summaries are injected as bounded prompt sections.
- Knowledge files/provider memories are injected separately with higher priority.
- Empty archive markers are filtered at injection as a defensive layer.
- Tool-call messages are filtered from prompt injection by default unless a provider/agent mode
  explicitly requires them.

Reference mapping:

- hermes-agent: session search is separate from memory injection.
- nanobot: recent active history is distinct from archived history.

## 7. Architecture Constraint: Reuse Existing Extension Points

The memory subsystem is already complex. This design should not introduce a new parallel
architecture layer for classification, projection, or archival. The implementation should reuse
and tighten existing extension points:

- `MessageCompactionPolicy`
- `MessageCompactionDecision`
- `BoundaryPolicy`
- `SummaryStrategy`
- `ArchiveStrategy`
- `CompressionTriggerPolicy`
- `CommitPolicy`
- `InjectionFilterStrategy`

When behavior is missing, prefer adding a concrete implementation of an existing ABC/Protocol or
fixing an existing implementation. Only add a new type when the current public contract cannot
express a necessary behavior safely.

### 7.1 Keep and Fix

Existing units worth keeping:

- `ConservativeCompactionPolicy`: keep, but make it the actual default path in the coordinator.
- `ToolChainBoundaryPolicy`: keep, but feed it real compaction decisions.
- `SemanticArchiveStrategy`: keep only after changing it to skip empty writes before persistence.
- `ToolMessageFilterStrategy`: keep as the default injection filter.
- `DefaultCommitPolicy`: keep the optimistic two-phase commit shape.

### 7.2 Delete or Replace

Implementations that are misleading or unsafe should be removed or replaced rather than preserved
for compatibility at all costs.

Candidates:

- Any archive strategy that writes placeholder-only archive entries.
- Any semantic archive fallback that turns "no useful content" into persisted history.
- Placeholder strategy classes that claim semantic handling but only delegate without adding value,
  unless tests show they are needed as compatibility aliases.

Concrete recommendation:

- Remove the `"(no semantic content)"` write path from `SemanticArchiveStrategy`.
- If `SemanticToolCompactionPolicy` remains only a placeholder, either implement it properly as a
  concrete policy or remove it and use `ConservativeCompactionPolicy(high_value_tools=...)`.
- Keep `HeuristicSummaryStrategy` only as a fallback/test strategy. bot_project should continue to
  use its LLM-backed `SummarizerStrategy` path for real compression.

### 7.3 Cursor State

Do not introduce a new cursor subsystem in the first implementation. Reuse current session state
keys such as `.compression_summary` and `.last_compression`, and document their semantics. A future
cursor dataclass can be considered only after the existing behavior is correct and tested.

## 8. Coordinator Changes

Current implementation:

- `DefaultMemoryCompressionCoordinator` triggers compression.
- It reads visible messages.
- It computes `prune_count`.
- It calls `ToolChainBoundaryPolicy.find_prune_boundary(messages, [], target_prune)`.
- It summarizes `visible[:boundary_idx]`.
- It keeps `visible[boundary_idx:]`.

Problem:

- The `decisions` argument is empty, so `MessageCompactionPolicy` is not applied.
- Tool calls/results are protected only by chain detection, not by semantic policy.
- Summary input is not projected before LLM or heuristic summary.

Proposed flow:

```text
visible messages
  -> compaction_policy.decide_all(...)
  -> boundary_policy.find_prune_boundary(messages, decisions, target)
  -> archiveable_prefix = messages[:boundary]
  -> keep_suffix = messages[boundary:]
  -> summary_input = messages whose decision is SUMMARIZE
  -> summary_strategy.summarize(summary_input, ...)
  -> commit_policy.commit(plan, ...)
```

Required behavior:

1. `KEEP_RAW` messages remain in the suffix unless the whole chain is safely outside the active
   area and policy allows projection.
2. `DROP_FROM_SUMMARY` messages are removed from summary input.
3. Assistant `tool_calls` messages and `tool` messages are not summary body by default.
4. If the final summary and semantic fallback are empty, `ArchiveStrategy`/`CommitPolicy` performs
   no archive write.
5. Commit still uses optimistic revision checking before mutating storage.

## 9. Boundary Policy

The boundary policy should combine three constraints:

1. Target pressure: remove enough old context to get below message/token budget.
2. User-turn boundary: cut before a user message or after a completed assistant final.
3. Tool-chain legality: never split assistant tool calls from corresponding tool results.

Keep the existing `BoundaryPolicy` contract first. A user-turn-aware implementation can still return
the current integer boundary and record details through logging/result reason metadata. Do not add a
new boundary object until the existing boundary behavior is correct and tested.

## 10. Archive Strategy Changes

### 10.1 Empty archive handling

`SemanticArchiveStrategy` should not append an `ArchiveEntry` when no semantic content exists.

Current bad behavior:

```text
summary = "(no semantic content)"
metadata.source = "empty"
metadata.semantic_count = 0
```

Desired behavior:

```text
no archive write
CompressionResult.reason = "empty_semantic_archive_skipped"
```

This must happen before persistence. Empty archive entries should not be created and later hidden
at read time. Read-time filters remain only as defensive compatibility for old data already on disk.

Defensive compatibility filters should also be updated in:

- `framework/memory/consolidation/dream_engine.py`
- `framework/memory/injection/__init__.py`
- any archive retrieval formatting path.

### 10.2 Empty Summary Commit Semantics

When a summary is empty or a known placeholder, compression should be reported as skipped, not as a
successful commit. A successful commit should mean at least one durable effect happened:

- archive entry written; or
- session messages replaced after archive write; or
- explicit state-only cooldown marker written.

Recommended behavior:

```text
empty summary + no semantic fallback -> CompressionResult(committed=False, retryable=False, reason="nothing_to_archive")
```

This avoids misleading observability and prevents callers from treating a no-op as a completed
archive/session transition.

### 10.3 Archive metadata

Archive entries should standardize metadata keys:

```text
source
compression_reason
pruned_count
semantic_count
tool_breadcrumbs
session_id
agent_role
cursor_start
cursor_end
```

`tool_breadcrumbs`, `cursor_start`, and `cursor_end` are optional metadata keys only. They should
not require new core structs in the first implementation. This keeps history useful for debugging
while avoiding new architectural surface area.

## 11. Tool Memory Policy

Tool handling should be explicit and layered.

### 11.1 Active short-term context

Default:

- Keep raw tool-call chains only while needed for protocol/legal context.
- Repair or filter orphan tool messages before injection.
- Application cleanup can remove completed intermediate tool steps after a final assistant answer.

### 11.2 Summary input

Default:

- Assistant messages with `tool_calls`: excluded from semantic summary input.
- Tool result messages: excluded from semantic summary input by default.
- Plain final assistant messages after tool execution: included, because they are the user-visible
  conclusion.
- Whitelisted tools: may contribute a capped, sanitized projection only through a concrete
  `MessageCompactionPolicy` or `SummaryStrategy` implementation.

Rationale:

- The assistant `tool_calls` message is an execution instruction, not user-facing memory.
- The `tool` message is usually raw observation/log output and often too noisy for long-term memory.
- The final assistant answer is the correct semantic carrier for task result, because it is already
  expressed in conversational form.

### 11.3 Archive

Default:

- Archive user intent and assistant final answer.
- Do not archive raw tool outputs.
- Do not archive assistant messages whose purpose is only tool invocation.
- Do not archive tool result messages unless a project-specific policy explicitly converts them to
  safe summary text.

Optional:

- For configured tools, archive a capped short summary.
- Raw capped archive only when explicitly enabled for audit/debug use.

### 11.4 Knowledge

Default:

- DreamEngine ignores raw tool details.
- DreamEngine may extract stable facts from user-visible final answers.
- Tool results become knowledge only if the assistant final answer makes a stable conclusion.

### 11.5 Context-Only Tool Compaction

`MicrocompactGovernance` is a context-governance feature, not a persistence feature. It operates on
copies of messages before LLM calls, so it should not be treated as archive memory. The issue report
correctly notes that its current placeholder is too vague:

```text
[tool_name result omitted from context]
```

Recommended improvement:

- Keep the same governance abstraction.
- Replace old large tool results with a compact one-line description containing:
  - tool name
  - approximate original character count or line count
  - whether output was omitted/truncated
  - status if available in metadata/content
- Do not feed these placeholders into long-term semantic memory.

Example:

```text
[shell result omitted from context: 47 lines, 12034 chars, status unknown]
```

## 12. Trigger Policy

Compression should be triggered by a combination of:

1. Message count.
2. Token budget pressure.
3. Hidden/unavailable messages in cursor mode, but only when there is an archiveable prefix.
4. Idle auto-compact.
5. Explicit user/system command.

Reference-derived improvements:

- Use cooldown after failed LLM summary attempts.
- Avoid repeated compaction when the last attempt found no safe boundary.
- Prefer real prompt budget estimation where available, including system sections and tool schemas.

This can be incremental. The first phase can keep current token estimation, but should add result
reasons that distinguish:

```text
not_needed
empty
within_budget
no_safe_boundary
empty_semantic_archive_skipped
summary_failed_cooldown
archive_failed
committed
```

### 12.1 Trigger Fixes From Issue Report

`len(all_msgs) > len(visible)` should not independently return `TOKEN_PRESSURE`. With the current
`ScopedSessionMemoryManager`, `visible` is the capped tail of `all_messages`, so this condition
becomes true once persisted history grows beyond `max_messages`. If visible history is otherwise
within budget, the coordinator computes `prune_count <= 0` and returns `within_budget`, then the
next write can repeat the same no-op check.

Recommended correction:

- Trigger on hidden history only when the coordinator can identify a non-empty archiveable prefix.
- Alternatively, remove this trigger from `DefaultCompressionTriggerPolicy` and let message/token
  pressure plus idle auto-compact drive compression.
- If kept, record a cooldown/no-op state when hidden-history trigger produces `within_budget`, so it
  does not re-run on every subsequent append.

The design should not add a new cursor architecture to solve this. It should first make the current
trigger and `.last_compression` state coherent.

### 12.2 Cooldown State

`.last_compression` currently stores `len(plan.keep_messages)`, while trigger cooldown compares
`len(visible) - last`. This works only as a rough retained-window counter. It should be documented
and adjusted so no-op compression attempts also update a lightweight cooldown marker, or the state
key should be renamed internally in a later cleanup.

Do not mutate session history just to update cooldown. Use existing session state APIs where
available, and keep storage mutation separate from archive/session message replacement.

## 13. Retrieval and Injection

Current `FullInjectionPolicy` already separates:

- knowledge
- archive
- provider blocks
- provider prefetch
- compression summaries
- session messages

Recommended changes:

1. Use a shared empty-marker helper instead of local marker sets.
2. Filter archive entries by metadata as well as summary text:
   - reject `source == "empty"`
   - reject `semantic_count == 0`
3. Keep archive injection bounded and query-sensitive.
4. Keep session messages filtered by `InjectionFilterStrategy`.
5. Add a search-oriented API for archive/session history instead of increasing prompt injection.

The framework should support both:

- automatic context injection
- explicit history search tools

This matches hermes-agent's distinction between memory context and session search.

## 14. DreamEngine Changes

DreamEngine should become stricter about inputs.

Required:

- Reject empty known markers, including `"(no semantic content)"`.
- Reject entries with `metadata.source == "empty"`.
- Reject entries with `metadata.semantic_count == 0`.
- Continue advancing cursor when all entries are meaningless.

Recommended:

- Prefer main-agent archive scopes by default, as currently done.
- Allow explicit promotion policies for peer/subagent outputs.
- Track consolidation source entry IDs in knowledge update metadata where storage supports it.

## 15. bot_project Policy

`examples/bot_project` should use framework extension points instead of custom framework behavior.

### 15.1 Main agent

Recommended:

- `compression_mode: "cursor"`
- `auto_llm_compression: true`
- enable semantic archive policy
- enable tool cleanup after complete final assistant answers
- allow selected high-value tools to produce short summaries
- keep DreamEngine enabled for stable facts

Tool categories:

```text
search/fetch/deepwiki: breadcrumb plus capped summary
12306 query: capped structured result summary
file read/edit/write: breadcrumb plus file path/status, raw content excluded
shell: breadcrumb plus exit status, raw output excluded by default
playwright/browser: breadcrumb plus page title/url/status, raw DOM excluded
send_message_async: final delivered content may be semantic because it is user-visible
```

### 15.2 Peer agents

Recommended:

- `compression_mode: "delete"` or bounded cursor mode.
- `auto_llm_compression: false` by default.
- no direct long-term knowledge writes.
- only messages sent back to main through broker/tool become candidates for main archive.

### 15.3 Subagents

Recommended:

- short recent suffix only.
- no DreamEngine.
- no raw tool archive.
- promote only final task result to parent context.

### 15.4 Plugin defaults

`tool_call_cleanup` should be considered for enabling in bot_project, but the policy must remain
application-level:

- It can remove completed ReAct intermediate steps.
- It must not remove incomplete tool chains.
- It should preserve final assistant output and user-visible delegated results.

### 15.5 Existing Wiring Points

bot_project already has the right extension points. The adaptation should use them instead of
adding a parallel memory assembly path:

- `examples/bot_project/bot/service/core.py::_build_compression_coordinator(...)`
  - Parse `memory.main.compaction`.
  - Construct `DefaultMemoryCompressionCoordinator` with the existing `SummaryStrategy`.
  - Pass the selected existing `MessageCompactionPolicy` and boundary implementation after the
    framework coordinator accepts them.
- `examples/bot_project/bot/service/core.py` memory initialization
  - Keep `create_memory_system(...)`, `DefaultMemoryLifecyclePolicy`, and
    `MemorySystemContextManager`.
  - Keep plugin memory-system modifiers after `memory_system.initialize()`.
- `examples/bot_project/bot/service/builders.py::_create_peer_memory(...)`
  - Keep peers session-only by default.
  - Keep `RestrictedInjectionPolicy`.
  - Do not attach DreamEngine or archive-to-knowledge flow for peers.
- `examples/bot_project/bot/service/builders.py::_create_subagent_memory(...)`
  - Keep subagents session-only by default.
  - Keep small `RestrictedInjectionPolicy` windows.
  - Promote only final task output through parent/main communication.
- `examples/bot_project/plugins/tool_call_cleanup/`
  - Use as the application-level cleanup layer for completed ReAct turns.
  - Add tests before enabling by default in `config/bot_config.yml`, because incomplete tool chains
    must never be removed.

The default bot_project policy should be:

- Main agent: semantic compression enabled, empty archive skipped, assistant `tool_calls` and
  `tool` messages excluded from summary by default.
- Peer agents: session-only memory, no archive/DreamEngine, restricted injection.
- Subagents: session-only memory, smaller restricted injection, no archive/DreamEngine.

## 16. Configuration Shape

Proposed bot_project-oriented config shape. The framework should map these values onto existing
policy constructors; it should not introduce a separate runtime architecture.

```yaml
memory:
  main:
    short_term:
      max_messages: 50
      budget_ratio: 0.5
      compression_mode: "cursor"
      auto_llm_compression: true
      keep_recent_messages: 8
    compaction:
      policy: "conservative"
      boundary: "user_turn_tool_chain"
      raw_tool_archive: false
      tool_result_max_chars: 800
      empty_archive_action: "skip"
      high_value_tools:
        - "fetch"
        - "mcp-deepwiki"
        - "query_12306"
    archive:
      strategy: "semantic"
      skip_empty: true
      max_summary_chars: 4000
    dream_engine:
      enabled: true
      threshold: 5
      skip_empty: true
```

The bot_project adapter should parse this into existing framework policy objects. Avoid passing raw
dicts deep into coordinator logic.

## 17. Safety and Type Rules

Implementation should follow repository type-safety rules:

- Use `MessageRole` from `framework.core.types`.
- Use `StrEnum` for decisions and reasons.
- Use dataclasses for plan/result/metadata structures.
- Do not use bare `Any`, `list`, or `dict` in new public APIs.
- Use Protocols or ABCs for policy contracts.
- Keep bot_project-specific tool names and thresholds in bot_project config or plugin code.

Suggested changed units:

```text
framework/memory/compaction/policy.py
framework/memory/compaction/boundary.py
framework/memory/compression/policies.py
framework/memory/archive/__init__.py
framework/memory/consolidation/dream_engine.py
framework/memory/injection/__init__.py
examples/bot_project/config/bot_config.yml
examples/bot_project/plugins/tool_call_cleanup/
```

## 18. Implementation Phases

### Phase 1: Stop bad writes

Goal:

- Eliminate useless empty archive entries.
- Make no-op compression visible as skipped rather than committed.

Changes:

- Make `SemanticArchiveStrategy` skip empty semantic entries before persistence.
- Add defensive old-empty archive filtering for existing data.
- Update DreamEngine and injection filtering only as compatibility handling for old records.
- Change empty-summary/no-semantic-result handling to return a skipped result reason such as
  `nothing_to_archive` instead of a successful commit.
- Add unit tests proving empty archives are not written.

### Phase 2: Fix trigger/cooldown no-op behavior

Goal:

- Prevent repeated compression attempts when visible history is already within budget.

Changes:

- Remove or constrain the `len(all_msgs) > len(visible)` trigger.
- Add tests where `all_messages` exceeds visible cap but `visible` is within message/token budget.
- Keep using current state APIs; do not add a new cursor subsystem.
- Ensure skipped/no-op compression can set cooldown state without replacing session messages.

### Phase 3: Wire compaction decisions into coordinator

Goal:

- Make existing `MessageCompactionPolicy` meaningful.

Changes:

- Add compaction policy dependency to `DefaultMemoryCompressionCoordinator`.
- Call `decide_all()` before boundary selection.
- Pass decisions to `ToolChainBoundaryPolicy`.
- Exclude `DROP_FROM_SUMMARY` messages from summary input.
- Ensure assistant messages with `tool_calls` and `tool` messages are not summarized by default.
- Add tests for assistant tool call, tool result, user message, and final assistant message cases.

### Phase 4: Add concrete tool-aware policy implementation

Goal:

- Replace raw tool inclusion with a concrete implementation of existing policy interfaces.

Changes:

- Implement or remove `SemanticToolCompactionPolicy`.
- Add whitelist-based summarized tool behavior using current `MessageCompactionPolicy` and
  `SummaryStrategy`.
- Add bot_project high-value tool config.

### Phase 5: Improve boundary semantics

Goal:

- Move closer to nanobot's complete-turn and legal-suffix model while preserving framework modes.

Changes:

- Add user-turn-aware boundary policy.
- Reuse existing compression state instead of adding a new cursor architecture.
- Keep delete mode for simple scopes.
- Add tests for no-safe-boundary, legal suffix, and repeated compression.

### Phase 6: Context governance and retrieval refinement

Goal:

- Improve context-only compaction quality and avoid over-injecting old summaries.

Changes:

- Improve `MicrocompactGovernance` placeholders with tool name and approximate output size.
- Reuse existing `get_history_entries(...)`, provider prefetch, and memory tool patterns before
  adding any new retrieval API.
- Keep injection bounded and priority-based.
- Add bot_project history search tool only if useful for the example workflow.

## 19. Recommended First Implementation Slice

The safest first slice is:

1. Fix empty archive skip before persistence.
2. Make empty/no-semantic compression report `nothing_to_archive` rather than committed success.
3. Fix the `all_msgs > visible` no-op trigger path.
4. Wire compaction decisions into coordinator.
5. Drop assistant `tool_calls` messages and `tool` messages from summary input by default.
6. Add tests around those behaviors.
7. Update bot_project config to enable tool cleanup only after tests confirm complete-turn safety.

This slice directly addresses the reported issues while laying the path for richer multi-level
memory flow.

## 20. Open Decisions

1. Default tool policy:
   - Recommended: assistant `tool_calls` and `tool` messages are excluded by default; only
     project-whitelisted tools may contribute capped summary text.
2. Cursor migration:
   - Recommended: reuse current compression state first; do not add a new cursor architecture now.
3. bot_project cleanup default:
   - Recommended: enable after confirming no incomplete ReAct turn loses protocol state.
4. Raw archive fallback:
   - Recommended: disabled by default; allow capped opt-in for debugging/audit.
5. Tool-chain helper changes:
   - Recommended: do not rewrite `_find_tool_chain()` as a standalone task. Add regression tests
     around assistant tool-call chains, orphan tool results, and boundary truncation first.
6. Heuristic summary role:
   - Recommended: keep for tests/fallback only; bot_project uses LLM-backed `SummarizerStrategy`.

## 21. Summary

The framework should learn from nanobot's safe consolidation boundaries and hermes-agent's protocol
and tool-output safety, but keep ModexAgent's more flexible architecture. The main architectural
shift is to make the existing compression, archive, and injection extension points behave
consistently instead of adding a new memory architecture.

In practical terms:

- Short-term memory protects active conversation and legal tool chains.
- Archive stores meaningful compressed history; empty archive entries are not persisted at all.
- Long-term knowledge is distilled only from meaningful, stable archive content.
- Tool-call assistant messages and raw tool messages are excluded from summary/archive by default.
- bot_project supplies stricter application policy through config/plugins, not framework hardcoding.
