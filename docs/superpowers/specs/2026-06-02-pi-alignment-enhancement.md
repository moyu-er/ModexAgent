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

### 3.3 Fork context — system-prompt injection with persistence (P0)

**Design**: Forked parent context is NOT deep-copied into the subagent's
session message history. Instead it is injected into the subagent's
**system prompt** and persisted to a durable file. This gives three
benefits:

1. **Immune to compaction** — system prompt content cannot be lost to
   message-history governance.
2. **Clean subagent session** — the subagent's conversation history
   starts empty; inherited context is clearly separated as reference.
3. **Survives restarts** — the forked context file is loaded on resume.

#### 3.3.1 Lifecycle

```
+----------------------+          +-----------------------------------+
| parent session       |          | subagent (fork mode)              |
| conv:parent          |          | conv:subagent:invoc               |
+----------------------+          +-----------------------------------+
   |                                   |
   | 1. get_history(parent_ctx)        |
   | 2. count truncation (last N)      |
   | 3. governance (lossy compact)     |
   | 4. format as XML                  |
   | 5. persist to fork_context file   |
   |                                   |
   | 6. build system prompt:           |
   |    base_prompt + fork_preamble    |
   |    + fork_context_xml             |
   |                                   |
   | 7. create subagent session        |
   |    (empty — no inherited msgs)    |
   |---------------------------------->|
   |                                   | 8. subagent runs
   |                                   | 9. subagent completes
   |
   | 10. resume: send_to_agent(invocation_id="abc123")
   |   → fork_context file exists → SKIP steps 1-5
   |   → load from file → inject into system prompt
   |   → reuse subagent's own session messages
```

#### 3.3.2 Two-stage truncation

Stage 1 — **count-based** (executed first):

```python
# Keep only the most recent N messages from the parent session.
# Default: fork_max_messages = 80 (configurable per template).
parent_msgs = parent_msgs[-fork_max_messages:]
```

Count truncation runs first because lossy compaction on messages that
will be discarded anyway is wasted work.

Stage 2 — **lossy governance** (on the kept messages):

```python
from framework.memory.context_governance import CompositeGovernance

if parent_config.governance and parent_config.governance.lossy_compaction:
    governor = CompositeGovernance.from_config(parent_config.governance)
    parent_msgs, _stats = governor.apply(parent_msgs)
```

Pi does NOT compact at fork time. It relies on the parent's prior
auto-compaction. Our two-stage approach is stricter and guarantees a
manageable subagent system prompt regardless of parent state.

#### 3.3.3 Persistence and injection

**Injection**: The framework already has a system-prompt assembly pipeline in
`MemorySystemContextManager.load()` (line 214-233 of `framework/memory/system.py`).
No new mechanism is introduced. The `base_system_prompt` parameter, which
currently holds the raw content from `agents/{agent_type}.md`, is extended to
include the fork preamble and forked context:

```python
# In _create_dynamic_subagent — system prompt assembly for fork mode:
if template.context_mode == ContextMode.FORK:
    fork_xml = _build_fork_context_xml(truncated_parent_msgs, parent_name)
    fork_preamble = (
        f"\n\n---\n\n"
        f"## Fork Context\n"
        f"You are a subagent running from a fork of agent '{parent_name}'.\n"
        f"The context below is READ-ONLY reference. Do NOT continue the\n"
        f"prior conversation. Your task starts now.\n\n"
        f"<forked_context>\n{fork_xml}\n</forked_context>"
    )
    system_prompt = agent_md_content + fork_preamble
else:
    system_prompt = agent_md_content

# Pass through existing mechanism — no new code paths:
subagent_ctx = build_session_only_memory(
    cfg=template.memory,
    workspace=memory_workspace,
    agent_id=name,
    agent_role=MemoryAgentRole.SUBAGENT,
    system_prompt=system_prompt,  # ← already contains fork context
)
```

This flows into `MemorySystemContextManager.base_system_prompt` and is injected
on every `load()` call via the standard `parts.append(self.base_system_prompt)`
line. Zero new injection mechanism.

**Persistence**: The forked context XML is written to a durable file so the
system prompt can be rebuilt after a bot restart.

```
data/memory/{pool_name}/fork_contexts/{agent_name}_{invocation_id}.xml
```

```python
# Resume detection — check persisted file first
fork_file = workspace / "fork_contexts" / f"{name}_{invocation_id}.xml"
if fork_file.exists():
    # Resume: load from persisted file, no re-truncation
    fork_xml = fork_file.read_text(encoding="utf-8")
else:
    # Initial creation or recovery: two-stage truncate + persist
    fork_xml = _build_and_persist_fork_context(
        parent_memory_system, parent_ctx, fork_file, ...
    )
# Either way, assemble base_system_prompt the same way
system_prompt = agent_md_content + _build_fork_preamble(parent_name, fork_xml)
```

-   Written **once** at initial creation (after two-stage truncation).
-   **Not rewritten** on resume — the file is the authoritative source.
-   Recovery: if the fork context file is lost after restart,
    re-truncate from parent session (same two-stage pipeline).

#### 3.3.4 Abstract memory API

Fork context persistence goes through file I/O (the context is a system
prompt component, not session messages). Session messages use the
`MemorySystem` ABC as normal — the subagent starts with an empty session.

```python
# ✅  Correct — fork context is a file, not session messages
fork_file = workspace / "fork_contexts" / f"{agent_name}_{invocation_id}.xml"
fork_file.parent.mkdir(parents=True, exist_ok=True)
fork_file.write_text(fork_xml, encoding="utf-8")

# ✅  Correct — subagent session starts empty
# No add_messages() call with parent history
```

#### 3.3.5 Fork subagent memory capacity

Fork-mode subagents use larger memory limits because the forked context
occupies system-prompt tokens. The subagent's own session messages need
ample headroom:

```yaml
# Fork agents (planner, worker, oracle) — larger capacity
memory:
  session:
    max_messages: 200
    max_tokens: 200000
    keep_ratio_for_messages: 0.4
    keep_ratio_for_token: 0.4
  governance:
    tool_chain_repair: true

# fork_max_messages controls the count-truncation window
fork_max_messages: 80
```

#### 3.3.6 Template field

`AgentTemplate` gains `fork_max_messages: int = 80` — the N in
count-based truncation. Default 80. Overridable per template.

```python
@dataclass
class AgentTemplate:
    # ... existing fields ...
    fork_max_messages: int = 80  # only meaningful when context_mode == FORK
```

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

### 3.8 Old implementation cleanup

The following code paths are **removed or replaced** by this spec. No
compatibility shim is kept — delete old code directly.

| Location | Old code | Disposition |
|----------|----------|-------------|
| `communication.py:_create_dynamic_subagent` | Fork: deep-copy parent messages into subagent session via `_layers.session.replace_messages()` | **Removed**. Replaced by system-prompt injection (3.3.3). |
| `communication.py:_create_dynamic_subagent` | Fork: `ChatMessage(role="system", content="..." )` fork marker | **Removed**. Replaced by Fork Preamble in system prompt + user-role marker in base_system_prompt. |
| `communication.py:_wire_subagent_hooks` | `parent_name=self._main_agent_name` (hardcoded) | **Replaced** by `source.name` from `_create_dynamic_subagent(source=...)`. |
| `communication.py:__init__` | `parent_memory_system: MemorySystem \| None` parameter | **Removed**. Fork context no longer reads from parent memory. |
| `communication.py:_build_subagent_tool_manager` | `visible_targets = [self._main_agent_name]` (hardcoded) | **Replaced** by dynamic `parent_name` parameter. |
| `template.py:AgentTemplate` | `standard_tools: bool = True` (deprecated field) | **Kept** for backward compat. Deprecation note stays. |
| `SubagentAutoSendHook.__init__` | `parent_name: str = "main"` (hardcoded default) | **Replaced** by explicit `parent_name` from `_wire_subagent_hooks`. |

## 4. Files Changed

### 4.1 Framework layer

| File | Change |
|------|--------|
| `framework/multi_agent/communication.py` | Dynamic parent threading; fork context lifecycle (count truncation → governance → XML → persist to file → inject into system prompt; resume skips truncation, loads from file); append prompt mode; progress tracking injection |
| `framework/multi_agent/template.py` | Add `system_prompt_mode: SystemPromptMode`; add `fork_max_messages: int = 80` |
| `framework/multi_agent/template_registry.py` | Parse `system_prompt_mode`, `fork_max_messages` from YAML |
| `framework/multi_agent/tools.py` | Dynamic descriptions via `get_dynamic_schema` based on `comm_kind` |
| `framework/tools/presets.py` | Add `SystemPromptMode` enum |
| `framework/tools/web/__init__.py` | New empty package |
| `framework/tools/web/search.py` | WebSearchTool stub |
| `framework/tools/web/reader.py` | WebReaderTool stub |
| `framework/hook/builtin/subagent_auto_send.py` | Accept dynamic `parent_name` (no longer hardcoded) |

### 4.2 Bot layer

#### 4.2.1 Configuration

| File | Change |
|------|--------|
| `config/pools/coding.yml` | Add oracle agent entry + template registration; update agent summary table (add oracle row); fork agents (planner, worker) get explicit `memory` block with `fork_max_messages: 80` |
| `config/pools/coding/templates/oracle.yml` | New: `agent_type: oracle`, `tool_preset: read_only`, `context_mode: fork`, `thinking_budget: high`, `max_steps: 60` |
| `config/pools/coding/templates/planner.yml` | Add `system_prompt_mode: replace`, `fork_max_messages: 80`, explicit `memory` block |
| `config/pools/coding/templates/worker.yml` | Add `system_prompt_mode: replace`, `fork_max_messages: 80`, explicit `memory` block, `progress_tracking: true` |
| `config/pools/coding/templates/delegate.yml` | Change `system_prompt_mode: append` (was implicit replace) |

#### 4.2.2 Agent prompts

| File | Change |
|------|--------|
| `agents/coding.md` | Add dispatch patterns section: chain (sequential send_to_agent + invocation_id handoff), parallel (same-turn send_to_agent ×N, max 5); NEED_DECISION recognition; invocation_id semantics |
| `agents/scout.md` | Communication rules: use `send_to_agent` with `NEED_DECISION:` / `PROGRESS_UPDATE:` prefixes; list_communication_targets to discover parent; progress tracking section |
| `agents/context-builder.md` | Communication rules: structured `NEED_DECISION:` / `PROGRESS_UPDATE:` prefixes; `web_search` conditional usage note |
| `agents/planner.md` | Communication rules: structured prefixes; output writes to working directory (plan.md) |
| `agents/worker.md` | Communication rules: structured prefixes; progress tracking; output format (changed files, validation, risks) |
| `agents/reviewer.md` | Communication rules: structured prefixes; review output format |
| `agents/delegate.md` | Short prompt (append mode — parent prompt is the primary context); communication rules |
| `agents/oracle.md` | New: oracle role prompt based on `docs/pi-reference/oracle/prompt.md`; fork-aware; read-only inspection; inherited decisions → diagnosis → drift → recommendation → risks format |

#### 4.2.3 Service wiring

| File | Change |
|------|--------|
| `bot/service/pool_builder.py` | Remove `parent_memory_system=memory_system` from `AgentCommunicationService(...)` — fork context no longer reads parent memory. Keep `extra_tools` registration unchanged. |

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

1. **P0: Foundation fixes** — dynamic parent, fork context rewrite (two-stage truncation + system-prompt injection + persistence), old fork code removal
2. **P1: Old implementation cleanup** — remove `parent_memory_system` param, remove `_layers.session` fork code, update `pool_builder.py`, update `SubagentAutoSendHook`
3. **P2: Tool & prompt improvements** — dynamic descriptions (normal/subagent), `SystemPromptMode` + `fork_max_messages`, template_registry parsing
4. **P3: Bot layer adaptation** — template YAMLs, agent prompts (coding + 6 subagents + oracle), coding.yml config
5. **P4: Capability expansion** — oracle role, web stubs, progress tracking injection
6. **Verification** — unit tests, bot integration smoke test
