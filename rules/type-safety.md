# Type Safety Rules

1. Use enums and constants instead of raw strings for categories, roles, states, and
   protocol values. Examples include `MessageRole`, `MessageType`, `FinishReason`, and
   `DefaultValues`.
2. Use typed structures instead of loose dictionaries. Prefer existing
   Pydantic `BaseModel` types such as `ChatMessage`, `ToolCall`, `LLMResponse`,
   `InputMessage`, and `OutputMessage`. (See rules 10–16 for the full
   Pydantic-first structured-data policy.)
3. Function signatures must declare parameter and return types. Avoid bare `Any`,
   `list`, `dict`, `object`, and `list[Any]` in framework-facing APIs.
4. Design abstractions before depending on implementations. Use ABCs for
   cross-cutting concerns and extension points, rather than directly binding framework
   code to concrete implementations. Protocols are prohibited (rule 7).
5. Keep framework code and example business code separated. `framework/` contains
   reusable framework behavior; `examples/` contains usage examples and business wiring.
   Do not hard-code example-specific configuration or business assumptions into the
   framework.
6. Avoid dynamic access patterns such as `getattr`, `hasattr`, `*attr` unless they are necessary for a real
   extension boundary or compatibility layer. Prefer explicit typed attributes and
   method calls so contracts stay visible and checkable.
7. Use ABC (Abstract Base Classes) for defining interfaces and extension points, avoid using Protocols.
8. Function or field called by an OBJECT must has the exact function/field in the type annotated on OBJECT.
9. Avoid isinstance.

## Structured / Class-based Type Rules (Pydantic-First)

10. **Internal data structures MUST use Pydantic `BaseModel` first.** All
    cross-module, framework-internal data structures (messages, tool calls,
    LLM responses, memory records, runtime state, hook payloads, I/O adapter
    payloads, approval payloads, etc.) MUST be defined as `pydantic.BaseModel`
    subclasses, not loose `dict`/`TypedDict`/`@dataclass`. This guarantees
    runtime field validation, serialization round-trip safety, and clear
    schema documentation.
    - Subclasses declare fields with explicit `Field(..., description=...)`
      where the meaning is non-obvious.
    - Field types must be precise: prefer `Literal[...]`, `Enum`, typed
      aliases, and nested `BaseModel` over `str`, `dict`, `Any`,
      `list[dict[str, Any]]`.
    - Use `model_config = ConfigDict(frozen=True, extra="forbid", ...)` for
      immutable, strict-shape value objects.
11. **Frozen `@dataclass` is allowed only as a leaf value-object escape hatch.**
    Use it ONLY when:
    - The structure is purely internal to a single module AND not
      serialized/deserialized across module boundaries; AND
    - It has no nested fields requiring runtime validation; AND
    - It is genuinely a tuple-like record (no behavior beyond field access).
    When any of these conditions stops being true, promote it to
    `BaseModel`.
    **Never use `@dataclass(frozen=True)` on classes with behavior** (methods,
    ABC inheritance, mutable per-execution state). Frozen dataclasses raise
    `FrozenInstanceError` on legitimate attribute assignment in subclasses —
    this is a class-system conflict, not a feature. If a class has `execute()`,
    `run()`, or any method that writes `self.x = y`, it is a runtime object,
    not a value object (rule 12 exception). Use a regular class with an
    explicit `__init__`. **Never work around `FrozenInstanceError` with
    `object.__setattr__`** — that hides the design error and scatters
    low-level protocol manipulation across the codebase. Fix the root cause:
    remove `frozen=True` (or remove `@dataclass` entirely).
12. **No bare `dict`, `TypedDict`, or `list[dict[...]]` for structured data in
    framework-facing APIs.** Wire formats, LLM message payloads, tool I/O,
    hook/interceptor signals, approval requests/responses, and control-channel
    messages MUST be a `BaseModel` (or an `Enum` for closed-set values).
    Loose dicts hide field names, lose types at runtime, and break schema
    evolution.
13. **Serialization boundaries MUST go through Pydantic.** When data crosses
    a serialization boundary (JSON, JSONL, file, network, broker message,
    snapshot, IPC), the producer and consumer MUST agree on a `BaseModel`
    schema. Use `model_dump()` / `model_validate()` (or
    `model_dump_json()` / `model_validate_json()`) — never hand-rolled
    `json.dumps(...)` / `dict(...)` round-trips on structured data.
14. **Nested structured fields MUST be typed models, not raw dicts.** If a
    field conceptually holds a structured record (e.g. `metadata`, `usage`,
    `extra_headers`, `tool_result`), its type must be a `BaseModel` subclass
    or at minimum a `TypedDict` declared in a typed module. `dict[str, Any]`
    is reserved for genuinely open extension payloads and must be justified
    by a comment at the field declaration.
15. **Discriminated unions over `Union[...]` of models.** When a field can
    hold one of several `BaseModel` variants, declare a discriminated union
    with `Field(discriminator="kind")` rather than an unconstrained
    `Union[A, B, C]`. The discriminator literal must be a closed
    `Literal`/`Enum`.
16. **All public framework types MUST be importable and documented.** Every
    `BaseModel` exposed across module boundaries lives under
    `modex_agent.core.types` (or its owning module's public surface) and is
    referenced by name in the relevant `AGENTS.md`. A field, payload, or
    parameter that exists only as an anonymous `dict` is a rule violation.
17. **No `object.__setattr__`, `object.__getattribute__`, `__dict__` manipulation,
    or other metadata-protocol bypasses.** These are low-level escapes that
    hide design errors (frozen dataclass on a behavior class, class-level
    mutable defaults, missing `__init__`). If you reach for `object.__setattr__`,
    stop — the root cause is one of:
    - The class should not be `frozen=True` (rule 11) — remove `frozen`.
    - The class should not be a `@dataclass` at all — use a regular class
      with explicit `__init__`.
    - The state belongs on a different object (e.g. `ctx`, not `self`) —
      move it.
    Fix the root cause. Never scatter `object.__setattr__` across a codebase.
