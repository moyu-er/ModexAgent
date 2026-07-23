# 01 — modex_graph codec handles PEP 604 unions and stdlib dataclasses

**What to build:** The `modex_graph` channel codec (`encode_value` / `decode_value`) correctly handles two type categories it currently breaks on:

1. **PEP 604 unions** (`X | None` syntax). `decode_value` currently only checks `origin is typing.Union`, not `types.UnionType`, so `T | None` fields fall through to the as-is branch and lose type-coerced decode for non-`None` payloads.
2. **stdlib `@dataclass` field types.** `encode_value` currently recognises Pydantic `BaseModel` but not stdlib dataclasses, so they fall through to `str(value)` — lossy for dataclasses with nested `BaseModel` fields (e.g. `TurnIdentity` nesting `SessionInfo`).

The fix uses Pydantic `TypeAdapter` as the transition mechanism: `encode_value` gains a stdlib-dataclass branch (after the `BaseModel` check, before `_find_codec`) that calls `TypeAdapter(type(value)).dump_python(value, mode="json")`; `decode_value` gains a symmetric stdlib-dataclass branch that calls `TypeAdapter(field_type).validate_python(data)`. A module-level `_TYPE_ADAPTERS: dict[type, TypeAdapter]` cache avoids repeated construction.

`tests/unit/modex_graph/test_channels.py` is extended with two new test classes proving the codec round-trips these types in isolation:

- `TestPEP604UnionCheckpoint`: `T | None` field with a non-`None` value round-trips correctly (covers the `cancellation` / `llm_response` / `approval` / `result` field shapes used in `ReActTurnState`).
- `TestStdlibDataclassCheckpoint`: stdlib `@dataclass` field round-trips correctly; nested `BaseModel` inside a dataclass round-trips correctly; `list[dataclass]` round-trips correctly.

All existing `test_channels.py` tests continue to pass. No `modex_agent` code is touched in this ticket — the fix is purely at the `modex_graph` package level, verified by `modex_graph` unit tests.

This is the foundation ticket: it makes the per-channel codec path correct for every `ReActTurnState` field type, unblocking ticket 02 (which removes the `ReActTurnState` override that bypasses the per-channel path).

**Type-shape decision (from ADR-0033 D14 prototype):**

```python
# encode_value (conceptual — actual placement per channel.py structure)
import dataclasses
from pydantic import TypeAdapter

if isinstance(value, BaseModel):
    return value.model_dump(mode="json")
if dataclasses.is_dataclass(value) and not isinstance(value, BaseModel):
    adapter = _get_or_create_adapter(type(value))
    return adapter.dump_python(value, mode="json")
# ... existing _find_codec / primitive / str(value) fallback
```

```python
# decode_value (conceptual)
import types
if origin is Union or origin is types.UnionType:
    non_none_args = [a for a in args if a is not type(None)]
    # ... existing union handling, now covering both Union and UnionType
if isinstance(field_type, type) and dataclasses.is_dataclass(field_type) and not issubclass(field_type, BaseModel):
    adapter = _get_or_create_adapter(field_type)
    return adapter.validate_python(data)
# ... existing BaseModel / Enum / primitive handling
```

```python
# TypeAdapter cache
_TYPE_ADAPTERS: dict[type, TypeAdapter] = {}

def _get_or_create_adapter(python_type: type) -> TypeAdapter:
    adapter = _TYPE_ADAPTERS.get(python_type)
    if adapter is None:
        adapter = TypeAdapter(python_type)
        _TYPE_ADAPTERS[python_type] = adapter
    return adapter
```

This is the Stage 1 transition shape. Stage 2 (deferred, separate future ADR) deletes the `dataclasses.is_dataclass` branches once the six value objects are `BaseModel` subclasses.

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] `decode_value` handles `types.UnionType` (PEP 604 `X | None`) identically to `typing.Union` / `typing.Optional[X]`
- [ ] `encode_value` recognises stdlib `@dataclass` instances and serializes them via `TypeAdapter.dump_python(value, mode="json")`
- [ ] `decode_value` reconstructs stdlib `@dataclass` instances via `TypeAdapter(field_type).validate_python(data)`
- [ ] Module-level `_TYPE_ADAPTERS: dict[type, TypeAdapter]` cache avoids repeated construction
- [ ] `test_channels.py::TestPEP604UnionCheckpoint` proves `T | None` field with non-`None` value round-trips correctly
- [ ] `test_channels.py::TestStdlibDataclassCheckpoint` proves stdlib dataclass field, nested `BaseModel` inside dataclass, and `list[dataclass]` round-trip correctly
- [ ] All existing `test_channels.py` tests continue to pass
- [ ] No `modex_agent` code is touched in this ticket
