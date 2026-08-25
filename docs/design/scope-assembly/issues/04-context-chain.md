# 04 — Context chain: three layered carriers + factory signatures

**What to build:** Component factories receive a typed, layered, read-only context chain: `WorkspaceContext → PoolContext → AgentContext`, each a frozen typed carrier. A factory's `create()` signature declares which layers it may read — the type is the capability boundary (a tool factory that only needs pool-layer data cannot reach workspace-layer fields). This generalizes the existing `AssemblyContext` (which already carries a 3-layer shape) and absorbs the `SubagentInvocationContext` special case into the Agent layer. All framework-preset factories (bundled defaults) migrate to read from the chain; business factories migrate with their own tickets. Tool configs carry zero workspace/pool data-path fields — path knowledge enters only via the chain.

**Blocked by:** None — can start immediately.

**Status:** closed (resolved 2026-08-21)

- [x] Three frozen carriers exist with their layer-appropriate fields (paths at workspace layer; pool_runtime deps/memory handles/terminal manager/communication facilities at pool layer; agent identity/parent/invocation data at agent layer)
- [x] Factory `create()` signatures type-narrow the readable layer; a factory reading an undeclared layer is a type error (mypy strict)
- [x] Existing `AssemblyContext` construction flows produce the layered chain (compat during migration: both views available until W3 tickets complete)
- [x] All bundled FW-preset factories read from the chain (MCP factory reads workspace paths; todo factory reads pool-runtime todo store)
- [x] Unit tests: a workspace-scoped factory can read paths; a pool-scoped factory cannot (type-level)
- [x] Full suite green — this is carrier + signature work, no behavior change
