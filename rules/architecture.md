# Architecture Rules

1. Design for **deep modules** — modules whose public interface is much simpler
   than their implementation. A reader should understand what a module does from
   its interface without opening many small files.
2. Use the shared architecture vocabulary: **module**, **interface**, **depth**,
   **seam**, **adapter**, **leverage**, **locality**. Prefer these terms over
   generic words like "component", "service", or "boundary".
3. Prefer deep modules over shallow ones. If a module's interface is nearly as
   complex as its implementation, merge it inward, delete it, or redesign its
   seam.
4. Apply the **deletion test**. Before extracting or keeping a module, ask: "If
   I delete this, does complexity concentrate in a better place, or does it just
   move around?" Only keep it when deletion would concentrate complexity.
5. **The interface is the test surface.** A module is well-designed when it can
   be tested through its public interface. If real bugs hide in how callers
   compose many small functions, the seam is wrong.
6. **One adapter is a hypothetical seam; two adapters make a real seam.** Do not
   introduce abstraction layers for a single caller. A second concrete use case
   justifies the seam.
7. Preserve **locality**. Keep related decisions, data, and behavior in the same
   module. Avoid forcing readers to cross multiple files to understand one
   concept.
8. Name modules after domain concepts, not machinery. Avoid names like
   `FooBarHandler` or `FooBarService`; prefer names that describe what the
   module means in the problem domain.
9. Separate framework code from example business code. `framework/` contains
   reusable behavior; `examples/` contains usage examples and business wiring.
   Do not hard-code example-specific assumptions in the framework.
10. Use ABCs for interfaces and extension points. Avoid Protocols.
11. Keep per-turn state in `runtime.state`. Do not store mutable turn state in
    instance attributes or `ctx.metadata`. Use typed `ReActTurnState` and
    `TurnCustomKey` for custom keys.
12. Use frozen dataclasses for config and value objects. Runtime objects may
    hold state and connections; config objects must be immutable.
13. Use `GraphInterrupt` for approval suspension. Never catch and swallow it.
    Approval state belongs in `ApprovalTransaction` inside `ReActTurnState`.
14. Centralize domain constants. `MessageRole` lives in
    `framework.core.types.MessageRole`; enums and typed constants replace raw
    strings.
