# Spec: Pi Alignment Enhancement

**Date**: 2026-06-02
**Status**: approved
**Scope**: Incremental enhancement to align current coding pool implementation with pi-reference

## 1. Goals

Close the gap between the current coding pool (6-role, preset-based tool assignment)
and pi's reference design (9-role, fork-aware memory, structured communication).
Focus on correctness fixes (P0), abstraction improvements (P1), and capability
expansion (P2). Intentionally defer framework-level chain/parallel orchestration
in favor of prompt-driven patterns.

## 2. Non-Goals

-   A new `subagent` orchestration tool (CHAIN/PARALLEL dispatch). Use
    `send_to_agent` with prompt-driven patterns instead.
-   `default_reads` framework injection. Context is passed through `content`
    strings and invocation_id session resumption.
-   `Inherit Project Context`. Subagent prompts are self-contained.
-   Full `web_search` implementation. Place stubs only.
-   `researcher` role. Defer to a later iteration.

## 3. Design Decisions

### 3.1 Communication — one tool, two dynamic descriptions

`send_to_agent` + `list_communication_targets` remain the only multi-agent
communication tools. Each tool inspects the caller's `AgentSessionMeta.comm_kind`
at schema-request time and returns a caller-appropriate description.

| Caller   | `send_to_agent` description                  | `list_communication_targets` description      |
|----------|----------------------------------------------|-----------------------------------------------|
| NORMAL   | Dispatch guide: invocation_id semantics, max-5 parallel, chain pattern | All templates + NORMAL agents                 |
| SUBAGENT | NEED_DECISION / PROGRESS_UPDATE structured prefixes, parent-only | Parent agent only (`visible_targets` filter)  |

No new tool class is introduced. `SendToAgentTool.get_dynamic_schema()` and
`ListCommunicationTargetsTool.get_dynamic_schema()` inspect `session_meta` on
each LLM request.

### 3.2 Dynamic parent agent (P0)

Today `parent_name` is hardcoded to `main_agent_name` in
`_wire_subagent_hooks`, `_build_subagent_tool_manager`, and
`SubagentAutoSendHook`. After this change the parent is always derived from
the actual sender:

-   `AgentCommunicationService._create_dynamic_subagent(source=...)` receives
    the real caller address.
-   `source.name` is threaded into `SubagentAutoSendHook`,
    `ListCommunicationTargetsTool(visible_targets=[source.name])`, and the
    subagent's system prompt guidance.
-   Fallback: if `source` is `None`, use `self._source` (the pool builder's
    own address) or `self._main_agent_name`.

### 3.3 Fork context — correct deep-copy lifecycle (P0)

#### 3.3.1 Copy only on initial creation

```
+------------------+        +-----------------------------+
| parent session   |        | subagent session (fork)     |
| conv:parent      |        | conv:subagent:invoc         |
+------------------+        +-----------------------------+
   |                                     |
   | 1. get_history(parent_ctx)          |
   | 2. governance (lossy compact)       |
   | 3. sanitize comm tool calls          |
   | 4. deep-copy every ChatMessage      |
   | 5. insert fork marker (user role)   |
   | 6. add_messages(sub_ctx, msgs)      |
   |------------------------------------>|
   |                                     | 7. subagent runs
   |                                     | 8. subagent completes
   |
   | 9. resume: send_to_agent(invocation_id="abc123")
   | → _create_dynamic_subagent checks get_history(sub_ctx)
   | → messages exist → SKIP deep-copy, reuse existing memory
```

#### 3.3.2 Fork context — two-layer isolation

Pi's approach: fork creates a branched session (physical copy), then uses
a **system prompt preamble** to declare the inherited messages as read-only
reference. We follow this two-layer pattern:

**Layer 1: System prompt Fork Preamble** — appended to the subagent's system
prompt, always visible regardless of message truncation:

```
You are a subagent running from a fork of agent '{parent_name}'.
The inherited conversation below is READ-ONLY reference context —
NOT a live thread to continue. Do NOT answer prior messages.
Your sole job is to execute the assigned task.
```

This is the authoritative instruction. It cannot be lost to compaction
because it lives in the system prompt, not in the message list.

**Layer 2: User-role fork marker** — inserted at index 0 of the
deep-copied message list, marking the boundary between inherited context
and the subagent's own work:

```xml
<fork_context>
  <source>This conversation is forked from agent '{parent_name}'.</source>
  <warning>All messages above this point are inherited reference context.
They do NOT belong to you. Your actual task starts below.</warning>
  <rules>
    <rule>Do NOT call send_to_agent or attempt to create subagents — you cannot.</rule>
    <rule>The codebase may have changed since the fork. Verify current state.</rule>
  </rules>
</fork_context>
```

-   **Role**: `"user"` (not `"system"`), so the LLM treats it as part of
    the conversation flow, not as framework metadata.
-   **Position**: inserted at index 0 of the sanitized message list
    before writing into subagent storage.
-   **`{parent_name}`**: interpolated from `source.name` at creation time.

#### 3.3.3 Abstract memory API

All fork memory operations go through the `MemorySystem` ABC, never through
private layers or file-system access:

```python
# ✅  Correct
parent_msgs = await parent_memory_system.get_history(parent_ctx)
await sub_memory_system.add_messages(sub_ctx, sanitized_msgs)

# ❌  Never
parent_memory_system._layers.session.get_all_messages(...)
subagent_memory._layers.session.replace_messages(...)
```

#### 3.3.4 Pre-fork governance

Before deep-copy, parent messages pass through the parent's configured governance
pipeline. Lossy compaction truncates oversized tool results and assistant content
to their configured `head_chars` limits, keeping structural completeness while
reducing token volume. This prevents the subagent from receiving 500+ messages
of which most are verbose tool output.

```python
from framework.memory.context_governance import CompositeGovernance

if parent_config.governance and parent_config.governance.lossy_compaction:
    governor = CompositeGovernance.from_config(parent_config.governance)
    parent_msgs, _stats = governor.apply(parent_msgs)
# Deep-copy the compacted result — not the raw full history
```

#### 3.3.5 Fork subagent memory capacity

Fork-mode subagents use an explicit `MemoryConfig` with larger limits than the
parent coding agent. The template YAML is the source of truth:

```yaml
# Fork agents (planner, worker, oracle) — larger capacity
memory:
  session:
    max_messages: 200      # parent: 100
    max_tokens: 200000     # parent: 150000
    keep_ratio_for_messages: 0.4
    keep_ratio_for_token: 0.4
  governance:
    tool_chain_repair: true
    # lossy_compaction: null (no immediate compression on inherited context)
```

Fresh-mode subagents keep the default smaller limits. Fork-mode agents
disable lossy compaction at startup so the inherited reference context is
not compressed on first load. Governance is re-enabled once the subagent
generates its own messages (after the first turn) or when the total
message count exceeds the configured `max_messages` threshold.

### 3.4 System prompt mode — `append` for delegate (P1)

New enum in `framework/tools/presets.py`:

```python
class SystemPromptMode(str, Enum):
    REPLACE = "replace"  # subagent uses its own complete prompt
    APPEND = "append"    # subagent prompt is appended after parent's

class ContextMode(str, Enum):
    FRESH = "fresh"
    FORK = "fork"

class ThinkingBudget(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
```

`AgentTemplate` gains `system_prompt_mode: SystemPromptMode = SystemPromptMode.REPLACE`.

When `system_prompt_mode == "append"`, `_create_dynamic_subagent` obtains the
parent agent's system prompt from `self._pool.get(parent_name).descriptor.system_prompt_template`
and concatenates:

```
{parent_system_prompt}

---

{delegate_system_prompt_from_agents/delegate.md}
```

`template_registry.py` parses `system_prompt_mode` from YAML with fallback to `"replace"`.

### 3.5 Oracle role (P2)

New template and prompt. Fork context, read-only, high thinking budget.

```yaml
# config/pools/coding/templates/oracle.yml
agent_type: oracle
description: "Decision-consistency oracle — prevents drift from inherited decisions"
tool_preset: read_only
context_mode: fork
thinking_budget: high
max_steps: 60
system_prompt_mode: replace
```

Oracle is a read-only inspector. It treats the forked context as the
authoritative contract and detects drift, contradictions, and hidden
assumptions in the current trajectory.

### 3.6 WebSearch + WebReader stubs (P2)

Stub tools in `framework/tools/web/`. Both return a message indicating they
are not yet implemented, suggesting alternative approaches. Full
implementation deferred.

```python
# framework/tools/web/search.py
class WebSearchTool(Tool):
    name = "web_search"
    # Stub — not yet implemented

# framework/tools/web/reader.py
class WebReaderTool(Tool):
    name = "web_reader"
    # Stub — not yet implemented
```

### 3.7 Progress tracking (P2)

Pure prompt-driven. When `AgentTemplate.progress_tracking == True`, the
framework appends a progress-tracking instruction to the subagent's system
prompt:

```
## Progress Tracking
Maintain a file called `progress.md` in the current working directory.
Update it after each significant step with:
- What was checked/done
- What was found
- What remains
Keep it concise — this is a scratch file for coordination, not documentation.
```

No framework code writes `progress.md`. The agent uses `write_file` to
maintain it.

## 4. Files Changed

### 4.1 Framework layer

| File | Change |
|------|--------|
| `framework/multi_agent/communication.py` | Dynamic parent threading; fork context lifecycle (init-only copy, pre-fork governance, correct API); append prompt mode; progress tracking injection |
| `framework/multi_agent/template.py` | Add `system_prompt_mode: SystemPromptMode` field |
| `framework/multi_agent/template_registry.py` | Parse `system_prompt_mode` from YAML |
| `framework/multi_agent/tools.py` | Dynamic descriptions via `get_dynamic_schema` based on `comm_kind` |
| `framework/tools/presets.py` | Add `SystemPromptMode` enum |
| `framework/tools/web/__init__.py` | New empty package |
| `framework/tools/web/search.py` | WebSearchTool stub |
| `framework/tools/web/reader.py` | WebReaderTool stub |
| `framework/hook/builtin/subagent_auto_send.py` | Accept dynamic `parent_name` (no longer hardcoded) |

### 4.2 Bot layer

| File | Change |
|------|--------|
| `examples/bot_project/config/pools/coding.yml` | Add oracle template; update summary table; fork agents get explicit memory config |
| `examples/bot_project/config/pools/coding/templates/oracle.yml` | New oracle template |
| `examples/bot_project/agents/coding.md` | Dispatch patterns (chain, parallel, invocation_id); NEED_DECISION recognition |
| `examples/bot_project/agents/scout.md` | Structured communication prefixes; progress tracking |
| `examples/bot_project/agents/context-builder.md` | Structured communication prefixes |
| `examples/bot_project/agents/planner.md` | Structured communication prefixes |
| `examples/bot_project/agents/worker.md` | Structured communication prefixes; progress tracking |
| `examples/bot_project/agents/reviewer.md` | Structured communication prefixes |
| `examples/bot_project/agents/delegate.md` | Append-mode concise prompt |
| `examples/bot_project/agents/oracle.md` | New oracle prompt |

### 4.3 Tests

| File | Change |
|------|--------|
| `tests/unit/multi_agent/test_template.py` | Test `system_prompt_mode` |
| `tests/unit/multi_agent/test_fork_context.py` | Test init-once lifecycle, fork marker format |
| `tests/unit/tools/web/test_search.py` | WebSearchTool stub behavior |
| `tests/unit/tools/web/test_reader.py` | WebReaderTool stub behavior |

## 5. Enum Summary

All configuration fields use their respective enums. No bare strings in
dispatch or comparison logic.

| Enum | Values | Usage |
|------|--------|-------|
| `ToolPreset` | `full`, `read_write`, `read_only`, `minimal`, `none` | Subagent tool assignment |
| `ContextMode` | `fresh`, `fork` | Memory inheritance |
| `ThinkingBudget` | `low`, `medium`, `high` | Prompt annotation |
| `SystemPromptMode` | `replace`, `append` | System prompt assembly |
| `AgentCommKind` | `NORMAL`, `SUBAGENT` | Dynamic description dispatch |

## 6. Implementation Order

1. **P0: Foundation fixes** — dynamic parent, fork context lifecycle, abstract API
2. **P1: Tool & prompt improvements** — dynamic descriptions, system prompt mode, structured prefixes
3. **P2: Capability expansion** — oracle role, web stubs, progress tracking
4. **Verification** — unit tests, bot integration smoke test
