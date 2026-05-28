# Memory Context Construction Simplification

Date: 2026-05-28
Status: Approved
Scope: Core simplification — delete filter strategy, converge triple assemble to single

## Problem

Session stores ~70 messages but only ~10-20 reach the LLM. Root causes:

1. **ToolMessageFilterStrategy** drops ALL tool_call + tool_result messages during injection (before governance). In ReAct conversations, tool messages are the majority. Governance's MicrocompactGovernance then has nothing to compact — contradictory design.

2. **Triple assemble**: `assemble_context()` triggers `injection_policy.assemble()` three times per request (reading all memory layers three times):
   - `ctx_mgr.load_with_metadata()` → assemble #1
   - `ctx_mgr.load()` → assemble #2 (overwrites #1)
   - `ctx_mgr.build_system_prompt()` → assemble #3 (only uses sections)

3. **Redundant filter + governance**: Both try to manage tool messages. Filter wins by deleting everything; governance becomes useless.

## Design

### 1. Delete Filter Strategy

Remove `framework/memory/injection/filter.py` entirely (InjectionFilterStrategy, ToolMessageFilterStrategy, NoopFilterStrategy).

In `FullInjectionPolicy`:
- Delete `filter_strategy` constructor parameter
- Delete `self._filter` field
- `assemble()` returns session messages as-is: `messages=session_msgs` (no filter line)

In `RestrictedInjectionPolicy`:
- Same changes

Rationale: Filtering tool messages is a presentation-layer concern. Governance (MicrocompactGovernance, ToolChainRepairGovernance) handles this correctly — compacting old tool results while keeping recent ones.

### 2. Converge Triple Assemble to Single

Add `_cached_bundle` to `MemorySystemContextManager`:

```python
class MemorySystemContextManager(ContextManager):
    def __init__(self, ...):
        ...
        self._cached_bundle: MemoryContextBundle | None = None
        self._cached_bundle_session_id: str | None = None

    async def load(self, session_id, runtime_info=None, metadata=None,
                   tool_manager=None, skill_manager=None):
        ctx = self._build_context(session_id, ...)
        query = ...

        # Single assemble
        bundle = await self.injection_policy.assemble(
            context=ctx, memory_system=self.memory_system, query=query,
        )
        self._cached_bundle = bundle
        self._cached_bundle_session_id = session_id

        # Build complete system_prompt in one pass
        parts = []
        if self.base_system_prompt:
            parts.append(self.base_system_prompt)
        for section in (bundle.system_sections or []):
            parts.append(section.content)
        # Skills
        if skill_manager is not None:
            skill_prompt = await skill_manager.build_prompt(...)
            if skill_prompt:
                parts.append(skill_prompt)
        # Runtime info
        if runtime_info:
            runtime_text = self._format_runtime_info(runtime_info)
            if runtime_text:
                parts.append(runtime_text)

        system_prompt = "\n\n---\n\n".join(parts) if parts else ""
        history = self.memory_system.create_message_history(
            context=ctx, initial_messages=bundle.messages,
        )
        return ContextState(system_prompt=system_prompt, history=history)

    async def build_system_prompt(self, tool_manager, skill_manager=None,
                                   runtime_info=None):
        # Reuse cached bundle — NO re-assemble
        session_id = ...  # from _cached_bundle_session_id
        bundle = self._cached_bundle
        if bundle is None:
            bundle = await self.injection_policy.assemble(...)  # fallback only
        # Build prompt from cached sections + skills + runtime
        ...
```

### 3. Simplify assemble_context()

In `framework/pipeline/context_assembler.py`:

```python
async def assemble_context(...):
    # 1. Crash recovery (no full assemble needed)
    recovered, was_recovered = await _recover_if_needed(ctx_mgr, session_id)

    # 2. Single load — THE ONLY ASSEMBLE
    context_state = await ctx_mgr.load(
        session_id,
        metadata={"input_metadata": input_metadata},
        tool_manager=tool_manager,
        skill_manager=skill_manager,
        runtime_info=runtime_info,
    )

    # 3. Append user message
    if append_user_message and not _is_approval_cmd:
        await context_state.history.append(user_message)

    # 4. Restore multimodal content
    ...

    # 5. Sideband prompt overlay
    ...

    # 6. MultiAgentContextBuilder (unchanged)
    ...

    return context_state
```

Key changes:
- Remove first `load_with_metadata()` call (was only for crash recovery, then discarded)
- Crash recovery happens before load, using lightweight checkpoint methods
- Single `load()` call with all parameters produces complete ContextState
- No separate `build_system_prompt()` call needed (already in system_prompt)

## Files Changed

| File | Action | Change |
|------|--------|--------|
| `framework/memory/injection/filter.py` | DELETE | Entire file |
| `framework/memory/injection/full_injection.py` | MODIFY | Remove filter param/logic |
| `framework/memory/injection/restricted_injection.py` | MODIFY | Remove filter param/logic |
| `framework/memory/injection/__init__.py` | MODIFY | Remove filter exports |
| `framework/memory/system.py` | MODIFY | Cache bundle, simplify build_system_prompt |
| `framework/pipeline/context_assembler.py` | MODIFY | Single load, crash recovery first |

## Files NOT Changed

- Provider mechanism (MemoryProviderRegistry, etc.) — untouched
- Governance chain (MicrocompactGovernance, TokenBudgetGovernance, etc.) — untouched
- ContextManager ABC — build_system_prompt kept, only MemorySystemContextManager impl changes
- MemoryContextBundle, PromptSection models — untouched
- examples/bot_project/ — no changes needed

## Test Impact

- `tests/unit/memory/test_injection_message_loss.py` — 4 TDD tests should now pass (default NoopFilter → no filter at all)
- `tests/unit/memory/test_context_construction_issues.py` — update to reflect new flow
- Add new test: verify single assemble per request
- Add new test: verify build_system_prompt reuses cache
