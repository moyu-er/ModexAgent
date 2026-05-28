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
- All `pending_user_turn` set/clear logic in `save()`

**Delete from `assemble_context()` (`context_assembler.py`):**
- All checkpoint recovery code (lines 57-84)
- The first `load_with_metadata()` call (was only for recovery, then discarded)

**Delete from pipeline:**
- `on_checkpoint` closure in `pipeline.py` (dead code)
- `_safe_clear_checkpoint` helper
- Checkpoint-related code in `_execute_turn` finally block

**Delete from models:**
- `CheckpointMemorySystem` protocol in `core/system.py`
- `get/set_last_recovered_checkpoint_id` from `SessionMemoryManager` ABC
- `save/load/clear_checkpoint` from `SessionMemoryManager` ABC
- Related methods in `DefaultMemorySystem` and `ScopedSessionMemoryManager`

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

### 6. Pending Injection → XML Format in Governance

`PendingInjectionGovernance` changes from prefix/suffix to structured XML:

```xml
<supplementary-context type="pending-input">
  <description>Previous user input interrupted by context compression</description>
  <content>用户被中断的输入内容...</content>
</supplementary-context>
```

This applies to all supplementary injections — clear labeling for the LLM to understand what each block is.

## Files Changed

### Deleted
| File | Reason |
|------|--------|
| `framework/memory/injection/filter.py` | Entire mechanism redundant |

### Modified
| File | Change |
|------|--------|
| `framework/memory/injection/full_injection.py` | Remove filter, return InjectionResult, PromptSection becomes internal |
| `framework/memory/injection/restricted_injection.py` | Remove filter, return InjectionResult |
| `framework/memory/injection/__init__.py` | Remove filter exports, update exports |
| `framework/memory/core/models.py` | Delete MemoryContextBundle, PromptSection (move PromptSection internal) |
| `framework/memory/system.py` | Remove checkpoint/pending_user_turn methods, single assemble in load(), simplify build_system_prompt |
| `framework/memory/context_governance.py` | PendingInjectionGovernance → XML format |
| `framework/memory/default_system.py` | Remove checkpoint delegations, remove pending_user_turn methods |
| `framework/memory/core/layers.py` | Remove checkpoint methods from SessionMemoryManager ABC |
| `framework/memory/core/system.py` | Remove CheckpointMemorySystem protocol |
| `framework/memory/layers/session.py` | Remove checkpoint method implementations |
| `framework/pipeline/context_assembler.py` | Remove crash recovery, single load |
| `framework/pipeline/pipeline.py` | Remove on_checkpoint closure, clear_checkpoint calls |

### Not Changed
- Provider mechanism (MemoryProviderRegistry, etc.)
- Governance chain structure (CompositeGovernance pattern remains)
- Interceptors, Hooks, Control system (different concerns)
- Three-layer memory (Session/Archive/Knowledge)
- examples/bot_project/ business logic

## Extension Mechanism Summary (After Simplification)

| Mechanism | Entry Point | What It Does |
|-----------|-------------|-------------|
| **Governance** | `runtime.governance = CompositeGovernance([...])` | Modify messages before LLM: compact, trim, inject supplements (XML), repair chains |
| **Provider** | `memory_system.add_provider(provider)` | Inject external content into system prompt (static blocks + semantic prefetch) |

Everything else (hooks, interceptors, control) serves different concerns (lifecycle observation, AOP, runtime commands) and is not a "context modification" mechanism.

## Test Impact

- `test_injection_message_loss.py` — all 4 TDD tests should pass
- `test_context_construction_issues.py` — update to reflect new flow
- Remove crash recovery tests
- Add: verify single assemble per request
- Add: verify InjectionResult instead of MemoryContextBundle
- Add: verify pending injection uses XML format
