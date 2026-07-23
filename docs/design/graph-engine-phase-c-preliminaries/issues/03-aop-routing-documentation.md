# 03 — AOP routing documentation and pass-through deletion

**What to build:** ReAct's AOP routing becomes honest: `ReactGraphRuntime.around` routes `ITERATION` only, and the dead `TOOL_CALL` / `LLM_STREAM` / `LLM_CALL` pass-through branches are deleted. The AOP split — `ITERATION` goes through `ctx.runtime.around`; `TOOL_CALL` and `LLM_STREAM` go directly to `InterceptorChain` — is documented at the call sites and in the domain glossary, so a reader no longer sees three scope branches in `around` and mistakenly assumes all three are wired.

This is a documentation + dead-code-removal ticket. The pass-through branches (`return await body()` for `TOOL_CALL` / `LLM_STREAM` / `LLM_CALL`) are never invoked because `tool_executor.py` and `llm_client.py` call `InterceptorChain.around_tool_call` / `around_llm_stream` directly. Deleting them is zero behavior change — the existing ReAct test suite is the regression gate.

The split is a design fact, not a defect to unify (ADR-0033 D5):

- `ToolCallContext` requires `tool_call` / `tool_name` / `arguments` — data that exists only inside `ToolNode`'s execution scope, not in `GraphContext.user_data` or `GraphContext.state`.
- `LLMStreamNext = Callable[[], AsyncIterator[LLMStreamChunk]]` is an async iterator, not a coroutine — `around`'s `body: Callable[[], Awaitable[Any]]` signature cannot express it.
- Lifting these typed contexts to the graph-runtime layer would require `GraphRuntime.around` to accept `modex_agent.interceptor.abc` types — violating invariant 1 (`modex_graph` has zero `modex_agent` imports) — or adding a pure forwarding shell that fails the ADR-0007 deletion test.

`ReActScope.TOOL_CALL` / `LLM_STREAM` / `LLM_CALL` enum values are **retained as informational** — they document that these AOP scopes exist as concepts even though they are not routed through `around`. Deleting them would lose vocabulary without simplifying anything.

ADR-0033 D5.2 refinement note ("`around` routes `ITERATION` only; `TOOL_CALL` and `LLM_STREAM` are node-local") was already applied to the ADR's Status line during ADR-0033 writing — no further ADR edit needed.

**Blocked by:** None — can start immediately. Technically independent of Stage A (the pass-through branches are dead code regardless of the per-channel checkpoint state).

**Status:** ready-for-agent

- [ ] `ReactGraphRuntime.around` `TOOL_CALL` / `LLM_STREAM` / `LLM_CALL` pass-through branches deleted — only `ITERATION` remains
- [ ] `ReactGraphRuntime.around` docstring updated: "`around` routes `ITERATION` only. `TOOL_CALL` and `LLM_STREAM` are node-local AOP invoked directly via `InterceptorChain` because their typed contexts are not constructible from `GraphContext`."
- [ ] Comment added at `tool_executor.py` `InterceptorChain.around_tool_call` call site: "Canonical AOP path for `TOOL_CALL`. `ctx.runtime.around` is for `ITERATION` only — see ADR-0033 D5."
- [ ] Comment added at `llm_client.py` `InterceptorChain.around_llm_stream` call site: "Canonical AOP path for `LLM_STREAM`. `ctx.runtime.around` is for `ITERATION` only — see ADR-0033 D5."
- [ ] `CONTEXT.md` `GraphRuntime` entry updated to reflect `around` routes `ITERATION` only
- [ ] `ReActScope.TOOL_CALL` / `LLM_STREAM` / `LLM_CALL` enum values retained (not deleted) — informational
- [ ] Existing `tests/unit/agents/react/` suite passes as regression gate (zero behavior change expected)
