# MCP connections are a shared, concurrent, service-scoped registry (optional overlay)

- **Status:** Implemented (2026-07). The registry, `SharedMcpBackend`, drop
  recovery, and the `MCPClientManager` slim-down ship behind the `sharedRegistry`
  flag (default on in the bot). Decision 6 deliberately deviates from the
  refcounting this ADR originally proposed — see below.

## Context

MCP tool connection is eager, per-pool, sequential, and workspace-scoped:

- `MCPClientManager.initialize()` (`src/modex_agent/tools/mcp/manager.py:73`)
  connects every configured server **sequentially**, each under a 20s timeout.
  For N stdio servers (npx/uvx subprocess + handshake + `list_tools`) startup
  blocks for the **sum** of per-server latencies.
- The manager is instantiated independently by three call sites — the framework
  `connect_mcp` (`ioc/factories/tools.py:35`), the bot main-agent path
  `_load_agent_mcp_tools` (`bot/service/builders.py:96`), and the subagent path
  `load_per_agent_mcp` (`tools/mcp_loader.py`). Each `new
  MCPClientManager(...) + initialize()` spawns its **own** subprocess per
  server, so two pools referencing the same server run two subprocesses.
- `mcp_manager` lives inside `PoolInstance` (`bot/service/pool_builder.py:346`),
  inside the per-workspace `PoolWorkspaceResources`. Workspace teardown
  (`_stop_resources`, `bot/workspace/wiring.py:441`) calls
  `mcp_manager.disconnect_all()`, killing the subprocesses. So an MCP
  connection's lifetime is bound to one pool in one workspace.

Net effect: slow startup (sequential, multiplied by pool), duplicated
subprocesses, and a **first visit to each new workspace** pays the full
connect cost again.

Two facts that shape (and bound) the decision, verified in code:

- **Switching between already-opened workspaces does NOT reconnect.**
  `WorkspaceRegistry.materialize` (`workspace/registry.py:118`) returns the
  cached bundle on a target-path hit; `max_materialized` is never set by the
  bot (`workspace/wiring.py:101`), so LRU eviction never fires in production;
  no eviction runs on switch. Multi-live keeps every opened workspace alive.
  The only cold-reconnect triggers are process start and the first open of a
  previously-unseen workspace. Do not "fix" switching — it is already free.
- **All three MCP call sites register tools once and snapshot, never
  refreshing.** `_load_agent_mcp_tools` does `initialize() →
  adapter.register_tools(registry) → snapshot registry.list_tools()`
  (`builders.py:96-115`); `_build_tools` then `tm.register(tool)` each
  (`pool_builder.py:713-724`). The framework and subagent paths are
  isomorphic. Any design that registers tools *progressively after* the
  snapshot is invisible to the pool's `ToolManager`.

## Decision

Introduce a **shared, concurrent, service-scoped** MCP connection registry as
an **optional overlay** on top of the existing, unchanged `MCPClientManager`.
Projects that do not wire the registry keep today's behavior exactly.

1. **ABC over the consumed surface, with default implementations.** Extract
   `McpBackend` (provisional name) as the surface `MCPToolAdapter` and
   `MCPTool` call. The ABC declares abstract only what differs in semantics
   between implementations — `connected_servers`, `_client_for(name)`, and
   `release()`/teardown — and **provides default implementations** for the
   pure-delegation family (`list_tools`, `list_resources`, `list_prompts`,
   `execute_tool`, `read_resource`, `get_prompt`): each is
   `c = self._client_for(name); return await c.<x>() if c else <empty>`.
   Both implementations inherit these defaults instead of duplicating the
   identical delegation body.

   `MCPClientManager` becomes one implementation (self-managing: owns its
   `clients` dict, `initialize`/`reconnect`/`disconnect` machinery,
   `release()` = `disconnect_all()`), **elegantly slimmed** by deleting the six
   now-inherited delegation methods — behavior unchanged. The new
   `SharedMcpBackend` facade is the other implementation (a Ready subset of
   shared clients from the registry; `release()` only detaches the facade —
   see Decision 6). `MCPToolAdapter` is retyped to depend on the ABC. Construction
   (`MCPClientManager(config)` + `initialize()` vs `registry.acquire(selection)`)
   stays **outside** the ABC — it is the resolved/usable surface, not the
   construction protocol.

   The per-server connect primitive is extracted to a module-level
   `connect_single_server(name, cfg, *, injector, stack)` (body identical to
   the current `MCPClientManager._connect_single`); both `MCPClientManager`'s
   machinery and the registry's supervisors call it, so the connect logic has
   one source without either calling the other's private methods. The facade
   does not connect at all — only `MCPClientManager` and the registry do.

2. **`McpConnectionRegistry` — a service-level singleton.** Owned by
   `BotService`, started once at `initialize`, shut down at `stop` (replacing
   the scattered per-pool `disconnect_all`). It deduplicates `BaseMCPClient`
   by a **canonical config-hash** (transport + command + args + env + url +
   headers), so workspace-specific configs (e.g. a filesystem MCP rooted at a
   per-workspace dir) dedupe to **different** connections correctly, while the
   same server referenced by multiple pools/agents/workspaces shares **one**
   subprocess. The dedup key is the **SHA-256** of the canonical form — stable
   for dedup, and, unlike the raw canonical string (which carries `env`/
   `headers` secrets), secret-free if the entry is ever logged. Including
   secrets in the canonical form is the *safe* direction: two configs that
   differ only by a secret get distinct keys and are not merged. Connections
   outlive any single workspace bundle — which is the correct semantics once
   they are shared.

3. **Concurrent connect via per-server supervisor tasks.** Each server is
   owned by one long-lived asyncio task that creates its `AsyncExitStack`,
   connects, then idles until asked to close — closing the stack in the **same
   task that created it** (the anyio cancel-scope constraint already
   documented at `manager.py:407`). All supervisors start together, so
   connection is genuinely parallel (total ≈ slowest server, not the sum).
   The per-server connect **reuses `MCPClientManager._connect_single`** as the
   primitive; `MCPClientManager`'s implementation is not rewritten, only
   driven differently.

4. **`acquire(selection)` blocks until resolved, before one-shot
   registration.** `acquire` waits for the selected servers' supervisors to
   reach a terminal state (`Ready` or `Failed`) within a deadline, then returns
   a facade over the **Ready subset** (it does **not** refcount — see Decision
   6). Because the registry is started **early** (at `BotService.initialize`,
   over the union of all configured selections), by the time any pool build
   calls `acquire` the connections are almost always already `Ready` —
   `acquire` is effectively instant and the existing connect →
   `register_tools` → snapshot sequence at all three call sites is unchanged.

5. **Gating by absence, not by a flag.** A server that is not `Ready`
   (still connecting, or failed) is absent from the facade's
   `connected_servers`, so its tools are never registered, so the LLM cannot
   call them. `ToolConfig.enabled` is **not** overloaded — it keeps its
   meaning ("configured on/off"), not "connected."

6. **No reference counting; `release()` only detaches.** This ADR's original
   wording proposed reference-counted per-acquisition connections. Service-
   scoped sharing makes that wrong: close-on-release-to-zero would kill a
   connection that other workspaces still use. So a facade's `release()` only
   empties its subset — the underlying connection is untouched — and
   connections close **only at registry shutdown** (each supervisor closes its
   own stack in its own task, anyio-safe). This trades the refcount mechanic
   for simplicity and is the one deliberate deviation from the proposal above.

7. **Drop recovery is passive, detected at the facade.** Because the shared
   connection is long-lived across workspace materializations, it can DROP
   between two of them — and a dropped session is silent: the mcp SDK never
   notifies the idle supervisor (`state` stays `Ready`), `list_tools` returns
   `[]` without raising, and `execute_tool` returns `{"success": False,
   "error": ""}`. Neither matches a naive "not connected" check, so without
   recovery a workspace materializing after a drop would cache an empty tool
   list forever ("MCP missing after workspace switch"). The facade detects the
   drop at the operation boundary and reconnects once, then retries:

   - **Registration path** (`SharedMcpBackend.list_tools`): a READY entry that
     *previously served tools* returning `[]` is a dropped session. A
     `had_tools` guard avoids reconnect storms on genuinely-empty servers.
   - **Call path** (`execute_tool` / `read_resource` / `get_prompt`): a
     non-success result whose error is empty or contains "not connected". A
     genuine tool error carries a non-empty message and is not mistaken for a
     drop.

   The reconnect runs in-supervisor (in-place stack + client swap, anyio-safe)
   with exponential backoff mirroring `MCPClientManager.reconnect_with_retry`;
   concurrent requests coalesce onto one attempt. The reconnect *execution* is
   thus borrowed wholesale from `MCPClientManager`; only the *detection* is
   new — `MCPClientManager` (short-lived, re-created per pool) never needed to
   spot a dropped-but-still-present session. There is no active health polling.

8. **Connect failure and shutdown are safe.** A `streamable_http` 4xx/5xx
   makes the mcp SDK tear down its anyio TaskGroup, surfacing as a
   cancel-scope `CancelledError` whose real cause is trapped in a
   `BaseExceptionGroup` visible only on stack close. A **non-shutdown** cancel
   is treated as a connect failure — the entry goes `Failed`, the half-entered
   stack is closed in-task, the root cause is unwrapped from the group and
   logged, and the supervisor stays alive and recoverable (parity with
   `MCPClientManager._connect_single`); only a genuine shutdown cancel
   propagates. Reconnect backoff is **shutdown-responsive**: it bails at the
   loop top and wakes from its inter-retry sleep when shutdown begins, so a
   reconnect in flight never delays registry teardown.

## Considered Options

- **Per-workspace registry (shared only within a workspace).** Rejected: it
  fixes pool-within-workspace duplication but not the new-workspace cold
  reconnect — the user's primary pain. Service scope is required for that.
- **Gate via `ToolConfig.enabled` (register all tools, flip the flag on
  ready).** Rejected: overloads one field with two meanings ("configured" and
  "connected"), violates the concept-over-impl naming rule, and makes the
  LLM's tool menu churn mid-conversation as connections drop/reconnect.
  Absence expresses "not usable" cleanly and needs no new state.
- **Progressive registration via an `on_server_ready` callback.** Rejected:
  all three call sites take a one-shot snapshot after `register_tools`; tools
  registered by a later callback never reach the pool's `ToolManager`.
  Blocking `acquire` before the snapshot preserves existing call-site
  semantics with no call-site rewrite.
- **Naive `asyncio.gather` over `_connect_single`.** Rejected: each
  `_connect_single` would enter its `AsyncExitStack` in a child task that then
  exits, leaving no live task able to `aclose()` it safely (the cross-task
  cancel-scope `RuntimeError`). The per-server supervisor task (create + idle
  + close in one task) is the required form.

## Consequences

- **MCP connections outlive workspace bundles.** Workspace teardown
  (`_stop_resources`) calls `mcp_manager.release()` uniformly — on the legacy
  path that closes the connection (`disconnect_all`), on the shared path it
  only detaches the facade (Decision 6); real shared connections close at
  registry shutdown. This is the correct semantics for a shared resource, but
  it is a visible change to teardown wiring.
- **A dropped shared connection self-heals** (Decision 7). The
  "MCP-missing-after-workspace-switch" failure mode — a connection dropping
  between two materializations and the later workspace caching an empty tool
  list — is closed: the facade detects the silent drop and reconnects once.
  The one residual gap is a genuinely-empty server on its very first
  materialization (no `had_tools` evidence yet), which stays empty rather than
  reconnecting — acceptable, since there is no prior signal it should have
  tools.
- **Failed or slow servers are silently absent**, not blocking: their tools
  simply do not appear. A UI status hook (separate from the ABC) should
  surface "MCP connecting / failed" so users do not mistake missing tools for
  misconfiguration.
- **First process boot pays one concurrent connect window** (≈ slowest
  server); every subsequent `acquire` — across pools, agents, and newly-opened
  workspaces — is instant against already-`Ready` connections.
- **The registry is opt-in.** Not wiring it leaves `MCPClientManager` as the
  sole, unchanged path; the ABC is the only mandatory artifact, and it adds no
  behavior on its own.
- The ABC satisfies the "two implementations make a real seam" rule
  (`MCPClientManager` eager + the shared facade); it is not a speculative
  abstraction.
- **Implementation consolidation.** The connect → adapt → register → collect
  dance shared by the bot main-agent path, the framework subagent path, and
  the framework `connect_mcp` factory lives in one helper
  (`acquire_mcp_tools`). `JsonFileMCPTransportInjector` gained an optional
  top-level `servers` map so one server's injected secret is not propagated to
  the others (backward compatible).
