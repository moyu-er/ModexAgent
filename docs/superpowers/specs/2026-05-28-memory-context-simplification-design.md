# Memory Context Construction Simplification (Revised)

Date: 2026-05-28
Status: Draft (revised after investigation)
Scope: Deep simplification — delete dead code, remove redundant types, converge extension mechanisms

## Problem

Session stores ~70 messages but only ~10-20 reach the LLM. Root causes and redundancies:

1. **ToolMessageFilterStrategy** drops ALL tool messages during injection, making governance's MicrocompactGovernance useless.
2. **Triple assemble**: `assemble_context()` triggers `injection_policy.assemble()` 3 times per request.
3. **Dead code**: Crash recovery (checkpoint save/load/recover) write path is never wired. `pending_user_turn` has no consumer.
4. **Redundant types**: `MemoryContextBundle` and `PromptSection` are intermediate containers whose fields are mostly never read downstream.
5. **10 extension mechanisms** where 2 would suffice.

## Investigation Findings

### Crash Recovery — Dead Code

- `on_checkpoint` closure in `pipeline.py:557` is defined but **never passed to any component**. No checkpoint is ever saved.
- `recover_checkpoint()` in `assemble_context()` runs every turn but is always a no-op.
- `pending_user_turn` is written and cleared but **never consumed** by any code.
- **Verdict**: Safe to delete entirely.

### MemoryContextBundle / PromptSection — Redundant Public Types

- `compression_summary`, `metadata` — never set, never read.
- `dropped_sections` — only read in tests.
- Every consumer extracts exactly two things: joined `.content` string → system_prompt, `.messages` list → history.
- `PromptSection` is useful **internally** in FullInjectionPolicy (priority sorting + budget trimming), but no downstream consumer reads `.key`, `.source`, `.metadata`.
- **Verdict**: Eliminate as public types. Injection policy returns `(system_prompt: str, messages: list[ChatMessage])`. PromptSection becomes internal to FullInjectionPolicy.

### Extension Mechanisms — 10 Categories, Should Converge to 2

Current: 10 distinct mechanisms (filter, injection policy, provider, governance, interceptor, hook, memory_system_modifier, injection queue, INJECT_STEER, INJECT_USER_MESSAGE).

**Converge to 2:**

| Mechanism | Responsibility | Why Keep |
|-----------|---------------|----------|
| **Governance** | Modify context before LLM (trim/compact/inject/supplement) | The single "pre-LLM modification" entry point |
| **Provider** | External memory plugin content injection (mem0 etc.) | External system content injection |

Everything else either deletes (filter, crash recovery) or is a different concern (hooks = lifecycle events, interceptors = AOP, control = runtime commands — these are not context modification mechanisms).

## Design

### 1. Delete Filter Strategy

**Delete:** `framework/memory/injection/filter.py` (entire file)

**Modify:**
- `FullInjectionPolicy`: remove `filter_strategy` parameter, remove filter logic
- `RestrictedInjectionPolicy`: same
- `__init__.py`: remove filter exports

### 2. Delete Crash Recovery / pending_user_turn

**Delete from `MemorySystemContextManager` (`system.py`):**
- `save_checkpoint()` / `load_checkpoint()` / `clear_checkpoint()` methods
- `recover_checkpoint()` method
- `add_assistant_placeholder()` method
- `_ERROR_PLACEHOLDER` constant (line 31, only used by add_assistant_placeholder)
- All `pending_user_turn` set/clear logic in `save()`

**Delete from `assemble_context()` (`context_assembler.py`):**
- All checkpoint recovery code (lines 57-84)
- The first `load_with_metadata()` call (was only for recovery, then discarded)

**Delete from `agent_session.py`:**
- Checkpoint recovery block (lines 230-257)
- `_sanitize_recovered_messages()` method (line 461+, only called from recovery block)

**Delete from `pipeline.py`:**
- `on_checkpoint` closure (line 557, dead code)
- `_safe_clear_checkpoint()` helper
- `turn_clean` variable and all references (lines 724, 764, 788) — only used for checkpoint gate

**Delete from models/protocols:**
- `CheckpointMemorySystem` protocol in `core/system.py` — but inline its 4 core methods (`create_message_history`, `add_messages`, `get_history`, `clear`) into `ContextManagedMemorySystem`
- `get/set_last_recovered_checkpoint_id` from `SessionMemoryManager` ABC
- `save/load/clear_checkpoint` abstract methods from `SessionMemoryManager` ABC
- Related implementations in `DefaultMemorySystem` and `ScopedSessionMemoryManager`
- `checkpoint_key` and `last_recovered_key` config fields (become dead after method removal)

**Delete from `ToolCallAwareSessionManager` (bot_project):**
- `save_checkpoint` / `load_checkpoint` / `get_checkpoint_id` / `clear_checkpoint` delegation methods (manager.py:66-83) — empty pass-through to inner session, no callers

**Keep on `ContextManager` ABC:**
- `save_checkpoint` / `load_checkpoint` / `clear_checkpoint` remain on the ABC (no-op defaults) for backward compatibility, but `MemorySystemContextManager` no longer overrides them.

### 3. Eliminate MemoryContextBundle as Public Type

Injection policies change their return type from `MemoryContextBundle` to a simple tuple/dataclass:

```python
@dataclass
class InjectionResult:
    """Output of injection policy — no intermediate containers."""
    system_prompt: str
    messages: list[ChatMessage]
```

**In `FullInjectionPolicy`:**
- `PromptSection` becomes a private internal class (or just a local tuple) for priority sorting
- `assemble()` returns `InjectionResult(system_prompt=joined_sections, messages=session_msgs)`
- `_trim_by_priority()` remains as internal logic

**In `RestrictedInjectionPolicy`:**
- `assemble()` returns `InjectionResult(system_prompt="", messages=msgs)`

**Delete from `core/models.py`:**
- `MemoryContextBundle` class
- `PromptSection` class (move to internal use in full_injection.py)
- Related `__all__` entries

**Update consumers:**
- `MemorySystemContextManager._bundle_to_state()` → directly uses `InjectionResult`
- `bundle_to_context_state()` function → deleted (inlined)
- All tests updated

### 4. Converge Triple Assemble to Single

`MemorySystemContextManager.load()` becomes the **only** assemble entry point:

```python
async def load(self, session_id, runtime_info=None, metadata=None,
               tool_manager=None, skill_manager=None):
    ctx = self._build_context(session_id, runtime_info, metadata)
    query = runtime_info.get("message", "") if runtime_info else ""

    # Single assemble
    result = await self.injection_policy.assemble(
        context=ctx, memory_system=self.memory_system, query=query,
    )

    # Build complete system_prompt in one pass
    parts = [self.base_system_prompt] if self.base_system_prompt else []
    if result.system_prompt:
        parts.append(result.system_prompt)
    if skill_manager is not None:
        skill_prompt = await skill_manager.build_prompt(...)
        if skill_prompt:
            parts.append(skill_prompt)
    if runtime_info:
        runtime_text = self._format_runtime_info(runtime_info)
        if runtime_text:
            parts.append(runtime_text)

    system_prompt = "\n\n---\n\n".join(parts) if parts else ""
    history = self.memory_system.create_message_history(
        context=ctx, initial_messages=result.messages,
    )
    return ContextState(system_prompt=system_prompt, history=history)
```

`build_system_prompt()` on `MemorySystemContextManager` becomes a thin wrapper that calls `load()` and returns only the system_prompt — or is removed entirely if `assemble_context()` no longer needs it.

### 5. Simplify assemble_context()

```python
async def assemble_context(...):
    # 1. Build user message
    user_message = ...

    # 2. Single load — THE ONLY ASSEMBLE
    #    Produces complete ContextState with system_prompt + messages
    context_state = await ctx_mgr.load(
        session_id,
        metadata={"input_metadata": input_metadata},
        tool_manager=tool_manager,
        skill_manager=skill_manager,
        runtime_info=runtime_info,
    )

    # 3. Append user message to history
    if append_user_message and not _is_approval_cmd:
        await context_state.history.append(user_message)

    # 4. Sideband prompt overlay (if present)
    ...

    # 5. MultiAgentContextBuilder (unchanged)
    ...

    return context_state
```

**Removed:**
- First `load_with_metadata()` call
- Crash recovery block
- Separate `build_system_prompt()` call (merged into load)

### 6. ChatMessage Structural Extension

Three new fields on `ChatMessage` to support structured content handling:

```python
class ContentFormat(StrEnum):
    PLAIN = "plain"
    XML = "xml"

class ChatMessage:
    # ... existing fields ...
    created_at: datetime | None              # message creation time (YYYY-MM-DD HH:MM:SS)
    content_format: str | ContentFormat      # default "plain"
    truncatable_paths: list[str] | None      # XML only: element names safe to truncate
```

**`content_format`** — drives governance truncation and archive compression strategy:

| Format | Governance Truncation | Archive Compression |
|--------|----------------------|---------------------|
| `"plain"` | Full-text truncation (existing logic) | Full-text summarization |
| `"xml"` | `truncate_xml_safe()` — only truncate text inside `truncatable_paths` elements | Preserve XML skeleton, compress only truncatable element contents |

**`truncatable_paths`** — for XML, lists which child elements' text content can be safely truncated (typically `["content"]`). All other elements and attributes are preserved.

**`created_at`** — timestamp for memory storage ordering, XML timestamp attributes, archive entry `created_at`.

### 7. XML-Safe Truncation with Parse-Failure Fallback

```python
def truncate_xml_safe(content: str, max_chars: int,
                      truncatable_paths: list[str] | None = None) -> str:
    """Truncate XML content preserving structure. Only modifies text inside
    truncatable_paths elements. Closes open tags on boundary cut.
    
    Falls back to plaintext truncation on XML parse failure.
    """
    if len(content) <= max_chars:
        return content
    
    try:
        # Try to parse as XML; truncate only truncatable elements
        return _truncate_xml_structured(content, max_chars, truncatable_paths or [])
    except (ET.ParseError, Exception):
        # XML parse failure → fallback: plaintext truncation
        # Still try to close any open tags on the cut boundary
        prefix = content[:max_chars]
        open_tags = []
        for m in re.finditer(r'<(/?)(\w+)(?:[^>]*/?)>', prefix):
            if m.group(1) == '/':
                if open_tags and open_tags[-1] == m.group(2):
                    open_tags.pop()
            else:
                open_tags.append(m.group(2))
        for tag in reversed(open_tags):
            prefix += f'</{tag}>'
        return prefix + '\n<!-- Content truncated -->'
```

**Key behavior:**
- For well-formed XML: truncate only text inside `truncatable_paths` elements. XML structure, attributes, and non-truncatable elements stay intact.
- For malformed XML: fall back to plaintext cut with tag-closing, never crash.

### 8. XML Message Formats

All supplementary/injected content uses XML with explicit `truncatable_paths`.

**Agent communication** (from other agents via `normalize_agent_messages_for_llm`):
```xml
<agent_message source="planner" timestamp="2026-05-28 14:30:00">
  <thinking>需要查询数据库确认用户信息</thinking>
  <content>用户 ID 12345 的完整资料...</content>
</agent_message>
<!-- content_format: "xml", truncatable_paths: ["content"] -->
```

**Pending injection** (PendingInjectionGovernance, system role):
```xml
<supplementary-context type="pending-input" entries="1" timestamp="2026-05-28 14:31:00">
  <entry source="user">
    <content>用户之前被中断的输入内容...</content>
  </entry>
</supplementary-context>
<!-- content_format: "xml", truncatable_paths: ["content"] -->
```

**Provider prefetch** (system role):
```xml
<supplementary-context type="memory-prefetch" source="mem0" timestamp="2026-05-28 14:30:05">
  <content>用户之前讨论过的相关话题记忆...</content>
</supplementary-context>
<!-- content_format: "xml", truncatable_paths: ["content"] -->
```

### 9. Governance Integration

**`LossyContentCompactionGovernance`** — updated apply():
```python
async def apply(self, messages):
    for msg in messages:
        updated = dict(msg)
        role = str(updated.get("role", ""))
        
        # system messages: never truncated
        if role == "system":
            result.append(updated)
            continue
        
        # content truncation
        limit = self._limits.get(role)
        if limit and limit > 0 and isinstance(content, str) and len(content) > limit:
            fmt = updated.get("content_format", "plain")
            if fmt == "xml":
                paths = updated.get("truncatable_paths") or []
                updated["content"] = truncate_xml_safe(content, limit, paths)
            else:
                updated["content"] = self._truncate_content(content, limit, role)
        
        # tool_args truncation (unchanged)
        ...
```

**`MicrocompactGovernance`** — for XML tool results, only compact truncatable content:
```python
# XML tool result: replace only <content> text with summary
# Plain tool result: replace entire content with summary (existing)
```

**Governance chain order (unchanged):**
```
LossyCompaction → ToolChainRepair → Microcompact → TokenBudget → PendingInjection
```

**Protection matrix (updated):**

| Message Role | content_format | Lossy behavior | TokenBudget | Microcompact |
|-------------|---------------|----------------|-------------|-------------|
| system | any | **Skip (explicit)** | **Preserve** | Skip |
| user | plain | Skip (disabled default) | Drop from head | Skip |
| user | xml | `truncate_xml_safe` | Drop from head | Skip |
| assistant | plain | truncate_content | Drop from head | Skip |
| assistant | xml | `truncate_xml_safe` | Drop from head | Skip |
| tool | plain | truncate_content | Drop from head | Summarize |
| tool | xml | `truncate_xml_safe` | Drop from head | Summarize only truncatable content |

### 10. Archive Compression Rules

When compressing messages for archive:

| content_format | Rule |
|---------------|------|
| `"plain"` | Full-text LLM summarization |
| `"xml"` | Preserve XML skeleton (all tags + attributes). Summarize only text inside `truncatable_paths` elements. Non-truncatable elements' text stays intact. Fallback to full-text on XML parse failure. |

## Files Changed

### Deleted
| File | Reason |
|------|--------|
| `framework/memory/injection/filter.py` | Entire mechanism redundant |

### Modified
| File | Change |
|------|--------|
| `framework/memory/core/message.py` | ChatMessage: add `created_at`, `content_format`, `truncatable_paths`; add `ContentFormat` enum |
| `framework/memory/core/models.py` | Delete MemoryContextBundle, PromptSection; add `InjectionResult` |
| `framework/memory/core/__init__.py` | Remove MemoryContextBundle, PromptSection exports; add InjectionResult |
| `framework/memory/core/system.py` | Delete `CheckpointMemorySystem` protocol; inline core methods into `ContextManagedMemorySystem`; remove pending_user_turn from ContextManagedMemorySystem |
| `framework/memory/core/layers.py` | Remove `save/load/clear_checkpoint`, `get/set_last_recovered_checkpoint_id` from SessionMemoryManager ABC |
| `framework/memory/layers/session.py` | Remove checkpoint method implementations; remove `checkpoint_key`/`last_recovered_key` config |
| `framework/memory/layers/config.py` | Remove `checkpoint_key`/`last_recovered_key` from SessionMemoryConfig |
| `framework/memory/__init__.py` | Remove MemoryContextBundle, PromptSection, filter class exports |
| `framework/memory/injection/__init__.py` | Remove filter exports; add InjectionResult export |
| `framework/memory/injection/policy.py` | Return type: MemoryContextBundle → InjectionResult |
| `framework/memory/injection/full_injection.py` | Remove filter param/logic; PromptSection → internal; return InjectionResult; delete bundle_to_context_state() |
| `framework/memory/injection/restricted_injection.py` | Remove filter param/logic; return InjectionResult |
| `framework/memory/system.py` | Remove checkpoint methods, recover_checkpoint, add_assistant_placeholder, _ERROR_PLACEHOLDER, pending_user_turn; single assemble in load(); _bundle_to_state → InjectionResult |
| `framework/memory/context_governance.py` | Add `truncate_xml_safe()`; LossyCompaction: content_format dispatch + system skip; Microcompact: XML tool compact; PendingInjectionGovernance: system role + XML format |
| `framework/memory/pending.py` | DefaultPendingPrunedInputInjector: XML format + content_format metadata + system role |
| `framework/memory/default_system.py` | Remove checkpoint delegations, remove pending_user_turn methods |
| `framework/core/message_utils.py` | normalize_agent_messages_for_llm(): agent messages → XML `<agent_message>` format with truncatable_paths |
| `framework/pipeline/context_assembler.py` | Remove first load, crash recovery block; single load with complete system_prompt |
| `framework/pipeline/pipeline.py` | Remove on_checkpoint closure, _safe_clear_checkpoint, turn_clean variable, checkpoint clear block |
| `framework/session/agent_session.py` | Remove checkpoint recovery block; remove _sanitize_recovered_messages() |
| `examples/bot_project/plugins/tool_call_cleanup/manager.py` | Remove checkpoint delegation methods (66-83) |

### Not Changed
- Provider mechanism (MemoryProviderRegistry, etc.)
- Governance chain structure (CompositeGovernance pattern remains)
- Interceptors, Hooks, Control system (different concerns)
- Three-layer memory (Session/Archive/Knowledge)
- examples/bot_project/ business logic
- ContextManager ABC: checkpoint no-op defaults kept for backward compat

## Extension Mechanism Summary (After Simplification)

| Mechanism | Entry Point | What It Does |
|-----------|-------------|-------------|
| **Governance** | `runtime.governance = CompositeGovernance([...])` | Modify messages before LLM: compact, trim, inject supplements (XML), repair chains |
| **Provider** | `memory_system.add_provider(provider)` | Inject external content into system prompt (static blocks + semantic prefetch) |

Everything else (hooks, interceptors, control) serves different concerns (lifecycle observation, AOP, runtime commands) and is not a "context modification" mechanism.

## Design Closure: End-to-End Verification

### Data Flow (Single Request)

```
User Input
  │
  ▼ Pipeline ← assemble_context()
  │
  ├─ [No crash recovery] — dead code removed
  │
  ├─ ctx_mgr.load(session_id, tool_manager, skill_manager, runtime_info)
  │    │  ← THE ONLY ASSEMBLE
  │    ├─ Three-layer memory read (knowledge + archive + session)
  │    ├─ Provider injection (static blocks + prefetch)
  │    ├─ → InjectionResult(system_prompt, messages)
  │    ├─ + skill_prompt + runtime_info
  │    └─ → ContextState(system_prompt, history)
  │
  ├─ User message appended to history
  │
  ├─ MultiAgentContextBuilder (if needed)
  │
  ▼ ContextState → AgentContext
  │
  ▼ ReActAgent.run() → LLMNode._build_messages()
  │
  ├─ ctx.system_prompt → system message (protected: skips all governance truncation)
  ├─ ctx.to_messages() → history messages (each has content_format metadata)
  │
  ▼ governance.apply() (single modification entry point)
  │
  ├─ LossyCompaction:  skip system, dispatch by content_format
  ├─ ToolChainRepair:  skip system
  ├─ Microcompact:     compact tool results, XML-aware for xml-format messages
  ├─ TokenBudget:      preserve system, trim from head
  └─ PendingInjection: inject XML system messages (always last)
  │
  ▼ Messages[] → LLM
```

### XML Safety Guarantees

| Content | Location | Protection |
|---------|----------|-----------|
| Knowledge (SOUL/USER/MEMORY) | system prompt (role=system) | Skip Lossy + Preserve TokenBudget |
| Archive summaries | system prompt (role=system) | Skip Lossy + Preserve TokenBudget |
| Provider blocks | system prompt (role=system) | Skip Lossy + Preserve TokenBudget |
| Pending injection | system msg (role=system, inserted last in chain) | 2-layer: system role + position-at-end |
| Agent communication | user msg (role=user, content_format=xml) | truncate_xml_safe by content_format |
| XML tool results | tool msg (role=tool, content_format=xml) | Microcompact: compact truncatable_paths only |

### Truncation Fallback Guarantees

For any XML-formatted message (`content_format == "xml"`):
1. If XML is valid → `truncate_xml_safe()` truncates ONLY text inside `truncatable_paths` elements, preserves all tags and attributes
2. If XML is malformed → falls back to plaintext cut with tag-closing, never raises an exception

### Archive Compression Guarantees

Plain messages → full-text LLM summarization (unchanged).
XML messages → preserve XML skeleton (tags + attributes), LLM-summarize only `truncatable_paths` content. Fallback to full-text on parse failure.

## Test Impact

### Tests to Delete (test dead code)
- `tests/unit/memory/test_checkpoint_dedup.py` — entire file
- `tests/unit/memory/test_error_placeholder.py` — entire file
- `tests/unit/memory/test_injection_message_loss.py` — repurpose or delete (tests removed filter logic)

### Tests to Update
- `test_context_construction_issues.py` — remove filter references, MemoryContextBundle imports
- `test_bot_project_memory_pipeline.py` — remove bundle.dropped_sections access
- `test_pending_injection_correctness.py` — remove MemoryContextBundle import
- `tests/unit/agents/react/test_nodes.py` — remove checkpoint mock tests
- `tests/unit/session/test_agent_session.py` — remove checkpoint recovery tests
- `tests/unit/pipeline/test_slash_commands.py` — remove load_checkpoint mock
- `tests/unit/pipeline/test_pipeline_subagent_emitter.py` — remove checkpoint mocks
- `tests/unit/memory/core/test_layers.py` — remove checkpoint method mocks
- `tests/unit/memory/core/test_default_system.py` — remove checkpoint tests
- `tests/unit/memory/test_tool_call_cleanup_manager.py` — remove checkpoint mock methods
- `tests/unit/core/test_context.py` — remove checkpoint tests

### New Tests to Add
- Add: verify single assemble per request
- Add: verify InjectionResult replaces MemoryContextBundle
- Add: verify ChatMessage content_format default is "plain"
- Add: verify ChatMessage truncatable_paths on XML messages
- Add: verify agent messages use XML `<agent_message>` format with truncatable_paths: ["content"]
- Add: verify pending injection uses XML + system role + content_format: "xml"
- Add: verify truncate_xml_safe preserves XML structure, only truncates truncatable_paths content
- Add: verify truncate_xml_safe falls back to plaintext on malformed XML
- Add: verify LossyContentCompaction skips system messages
- Add: verify Microcompact only compacts truncatable_paths content for XML tool results
- Add: verify supplementary XML survives full governance chain intact (integration)
