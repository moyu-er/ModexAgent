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
12. **Config and structured value objects use Pydantic `BaseModel` with
    `frozen=True`.** The default schema for any structured object that
    crosses a module boundary — including all config in `ioc/configs/`,
    cross-module messages, memory records, runtime snapshots, hook payloads,
    and approval payloads — is `pydantic.BaseModel` configured with
    `model_config = ConfigDict(frozen=True, extra="forbid")`. This gives
    immutable semantics plus runtime field validation and schema
    round-tripping.
    - Runtime objects that hold state and connections are NOT covered by
      this rule — they remain regular classes with mutable attributes.
    - Plain frozen `@dataclass` is allowed only as the leaf value-object
      escape hatch described in `rules/type-safety.md` rule 11.
13. Use `GraphInterrupt` for approval suspension. Never catch and swallow it.
    Approval state belongs in `ApprovalTransaction` inside `ReActTurnState`.
14. Centralize domain constants, enums and typed constants replace raw
    strings.
15. **Converge, don't patch.** When multiple existing paths serve the
    same concern (e.g. native vs external subagent wiring, main-agent vs
    subagent emitter injection), do NOT add a third branch or an if-else
    special case. Find the shared path and make ALL existing paths flow
    through it. The fix is correct only when every caller uses the same
    mechanism — no provider-specific or path-specific branches. If
    convergence requires touching more files than a minimal patch, that
    is the cost of correctness in a high-complexity codebase; a minimal
    patch that leaves divergent paths alive guarantees the next change
    will diverge further. Do NOT add backward-compatibility shims,
    deprecation aliases, or "fall back to old behavior if X is None"
    guards for code you just wrote — if the old path is wrong, remove it;
    if it's right, converge to it.
