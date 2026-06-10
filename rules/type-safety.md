# Type Safety Rules

1. Use enums and constants instead of raw strings for categories, roles, states, and
   protocol values. Examples include `MessageRole`, `MessageType`, `FinishReason`, and
   `DefaultValues`.
2. Use typed structures instead of loose dictionaries. Prefer existing dataclasses and
   typed models such as `ChatMessage`, `ToolCall`, `LLMResponse`, `InputMessage`, and
   `OutputMessage`.
3. Function signatures must declare parameter and return types. Avoid bare `Any`,
   `list`, `dict`, `object`, and `list[Any]` in framework-facing APIs.
4. Design abstractions before depending on implementations. Use ABCs or Protocols for
   cross-cutting concerns and extension points, rather than directly binding framework
   code to concrete implementations.
5. Keep framework code and example business code separated. `framework/` contains
   reusable framework behavior; `examples/` contains usage examples and business wiring.
   Do not hard-code example-specific configuration or business assumptions into the
   framework.
6. Avoid dynamic access patterns such as `getattr`, `hasattr`, `*attr` unless they are necessary for a real
   extension boundary or compatibility layer. Prefer explicit typed attributes and
   method calls so contracts stay visible and checkable.
7. Use ABC (Abstract Base Classes) for defining interfaces and extension points, avoid using Protocols.
8. Avoid isinstance.
