<!-- Updated: 2026-06-22 | Branch: develop_gyt -->

# framework

Core multi-agent framework package (336+ Python files). All abstractions, implementations, and the three-layer runtime model (Hook / Interceptor / Control) plus Approval and Experience.

> [!NOTE]
> "Hook / Interceptor / Control" names three packages, but they are not peers
> at runtime. **Hook and Interceptor are the live extension layers.** The
> control/ package is largely **vestigial**: its channels are constructed and
> threaded through the runtime but have no live producers/consumers - real
> cancellation is syncio.Task.cancel() in the pipeline. Only the
> AgentControlError exception hierarchy from control/ is widely used. See
> control/AGENTS.md before relying on the control channel.

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `core/` | ABCs — `Agent[E]`, `ContentEmitter[E]`, `Tool`, `ContextManager`, graph engine (`Graph[R]`/`Node[R]`), skills, experience, types (see `core/AGENTS.md`) |
| `agents/` | `ReActAgent` (graph-based 4-node), `SummarizerAgent`, `ExperienceReviewAgent` (see `agents/AGENTS.md`) |
| `approval/` | Tiered tool approval — tiers, decisions, response parsing (see `approval/AGENTS.md`) |
| `pipeline/` | `AgentPipeline` orchestration, I/O adapters, approval renderer, slash commands (see `pipeline/AGENTS.md`) |
| `input_pipeline/` | Extensible user-input stage pipeline — `UserInputEnvelope`, `InputStage` ABC, `Continue`/`Terminate`, `UserInputPipeline` (see `input_pipeline/AGENTS.md`) |

| `control/` | Control-plane data types + channels (mostly **vestigial**) — `InMemoryControlChannel`, `CallbackControlEventBus`, `ControlCommand`, `ControlScope`; `AgentControlError` exceptions (actively used) (see `control/AGENTS.md`) |
| `hook/` | Lifecycle hooks — `HookRunner`, `HookPoint`, 6 builtin hooks (see `hook/AGENTS.md`) |
| `interceptor/` | AOP interceptor chain — `InterceptorChain`; `interceptor/builtin/` has 1 interceptor + 1 helper; the 2 cancel interceptors live in `hook/builtin/control_drain.py` (see `interceptor/AGENTS.md`) |
| `memory/` | Three-layer memory — session/archive/knowledge, compaction, consolidation, governance, injection (see `memory/AGENTS.md`) |
| `multi_agent/` | Star-topology orchestration — `AgentPool`, inbox, `CommunicationTracker`, `AgentMessageBus` (see `multi_agent/AGENTS.md`) |
| `tools/` | Tool subsystem — registry, executor, MCP, terminal (pexpect/tmux/winpty), overflow, standard tools (see `tools/AGENTS.md`) |
| `plugins/` | Plugin system — `PluginManager`, `PluginContext`, `MemoryProvider` (see `plugins/AGENTS.md`) |
| `messaging/` | `MessageBroker`, `BrokerBridgeService` (see `messaging/AGENTS.md`) |
| `providers/` | LLM providers — LiteLLM, OpenAI implementations (see `providers/AGENTS.md`) |
| `ioc/` | `AppConfig` (Pydantic), 13 typed configs, 8 factory modules (see `ioc/AGENTS.md`) |
| `runtime/` | `AgentRuntime`, `AgentRuntimeServices`, `TurnStateStore`, `RuntimeCommandStore`, codec, snapshot policy (see `runtime/AGENTS.md`) |
| `commands/` | Slash command processor — parse, two-stage dispatch, approval/continue/transform actions (see `commands/AGENTS.md`) |
| `sandbox/` | Sandboxed execution — Subprocess, Docker, E2B, Landlock (see `sandbox/AGENTS.md`) |
| `security/` | `SecurityPolicy`, validators, handlers (see `security/AGENTS.md`) |
| `adapters/` | `PlatformAdapter` ABC, `AdapterRegistry`, `StreamingMode` (see `adapters/AGENTS.md`) |
| `registry/` | Shared registry utilities (see `registry/AGENTS.md`) |
| `utils/` | tokenizer, context_builder, deduplicator, sanitizer, media_utils, helpers (see `utils/AGENTS.md`) |
| `workspace/` | `WorkspaceContext` ABC, `DefaultWorkspaceContext` — cd/exit/restore workspace switching with callback notification and persistence (see `workspace/` directory) |

## For AI Agents

### Working In This Directory
- `from __future__ import annotations` in all modules
- Generic type bindings: `Agent[E]`, `ContentEmitter[E]` via `TypeVar("E", bound=AgentEvent)`
- Enums/constants over raw strings, dataclasses over dicts for config
- Every cross-cutting concern needs an ABC or Protocol — prefer ABC per project rules
- Frozen dataclasses for config/value objects; runtime objects hold state/connections

### Type Safety (from rules/type-safety.md)
1. Enums/constants over raw strings — `MessageRole`, `MessageType`, `FinishReason`, `DefaultValues`
2. Typed structures over loose dicts — `ChatMessage`, `ToolCall`, `LLMResponse`, `InputMessage`, `OutputMessage`
3. Typed signatures — no bare `Any`, `list`, `dict`, `object` in framework-facing APIs
4. ABCs/Protocols before implementations — no concrete dependency where pluggable contract exists
5. Framework vs examples separation — no example-specific config in framework
6. No dynamic access (`getattr`/`hasattr`) except at real extension boundaries

### Testing
- `pytest tests/unit/ -v` before committing
- Absolute imports (`from framework.xxx`) in tests

### Common Patterns
- `Protocol` for contracts, `@dataclass` for data, `ABC` + `@abstractmethod` for abstract classes
- `scopes: frozenset[InterceptorScope]` for declaring interceptor scope
- Per-turn state in `runtime.state` (typed `ReActTurnState`), not instance attributes
- Control: ControlChannel is constructed and threaded but **not fed** in the default runtime;
  ControlEventBus is never instantiated. Real cancellation is syncio.Task.cancel() in the
  pipeline pre-lock phase (see control/AGENTS.md)
- `GraphInterrupt` for approval suspension — never catch and swallow it

## Approval & Security Architecture

### Overview

The framework uses a **ToolNode-level approval system** — safety checks happen
*before* tool execution in the ReAct graph, not inside individual tools.
Tools are pure execution units with no knowledge of approval logic.

### Current Mechanism (What Works)

```
LLM returns tool_calls
    │
    ▼
ToolNode.execute()                                    ← framework/agents/react/nodes/tool.py
    │
    ├─ _classify_all()  ←── TieredToolApprovalClassifier   ← framework/agents/react/approval.py
    │                      (path-based: checks allowed_paths per tool)
    │
    │   NORMAL    → ALLOWED (execute immediately)
    │   DANGEROUS → PENDING (suspend for human approval)
    │   HARDLINE  → DENIED  (blocked, never executes)
    │
    ├─ PENDING → _suspend_for_approval()
    │     Capture TurnSnapshot → save to TurnStateStore → GraphInterrupt
    │     Pipeline sends approval prompt to user
    │     User replies /approve or /deny
    │     Pipeline._handle_snapshot_approval() restores state → resumes ToolNode
    │
    ▼
_execute_batch()
    │   ALLOWED   → _agent._execute_tool()  ← Tool.execute() runs here
    │   DENIED    → ToolResult(error=...)
    │   PREEMPTED → ToolResult(error=...)    ← one DENY cascades to entire batch
```

### Key Components

| Component | Location | Role |
|-----------|----------|------|
| `ToolNode` | `agents/react/nodes/tool.py` | Classify → suspend → execute |
| `TieredToolApprovalClassifier` | `agents/react/approval.py` | Path-based tier assignment |
| `ArgumentMatcher` | `interceptor/builtin/tool_approval.py` | Extracts path args, matches against allowed_paths |
| `ApprovalTransaction` | `runtime/models.py` | Per-batch approval state with cascade logic |
| `ApprovalRenderer` | `pipeline/approval_renderer.py` | Detects /approve /deny, auto-denies on unrelated input |
| `ReActSnapshotPolicy` | `agents/react/state.py` | Serializes/restores full agent state for suspend-resume |
| `AgentPipeline` | `pipeline/pipeline.py` | Orchestrates the user-facing approval interaction |

### Coverage — What IS Protected

- **File tools** (`write`, `edit`, `read`): classified by `allowed_paths` in config
- **Batch atomicity**: one DENY → all remaining PENDING become PREEMPTED
- **Unrelated input**: auto-denies pending request (prevents accidental approval)

### Coverage — What Is NOT Protected (Known Gaps)

1. **Command content**: the classifier only inspects file path arguments.
   Shell commands like `rm -rf /` are NOT intercepted — the tool executes them.
   There is no longer any command-level deny list (`_guard_command()` was removed).
   `framework/sandbox/` defines `CommandPatternGuard`/`PathTraversalGuard`/`GuardPipeline`
   for this, but they are **not wired** into tool execution (see `sandbox/AGENTS.md`).
   So in the shipped runtime command content is still unguarded.

2. **Subagents**: subagents are created via `DefaultAgentFactory` without
   `ApprovalRuntime`. Their `ToolNode._get_tier()` always returns `NORMAL`.
   All tool calls bypass approval.

3. **Pool mode main agent**: `pool_builder.py` does not use `RuntimeAssembler`,
   so the tiered approval system is not wired even for the main agent.
   The `approval` section in `main.yml` is only consumed in pipeline mode (`core.py`).

4. **SSRF / network safety**: no private-IP detection or URL validation exists.

5. **Workspace boundary**: no filesystem path confinement for shell commands.
   (`framework/sandbox/workspace_policy.py` + `guard_path.py` exist but are unwired.)

6. **Environment isolation**: subprocesses inherit the full parent environment
   (potential API key leakage).
   (`framework/sandbox/env_builder.py` (`EnvironmentBuilder`) exists but is unwired.)

### What NOT To Do

- **Do NOT add safety checks inside Tool subclasses.** Safety belongs at the
  ToolNode / agent level so it is uniformly applied regardless of tool implementation.
- **Do NOT confuse `TerminalGuard`** (`tools/terminal/guard.py`) with a security
  guard — it manages terminal *state* (is the terminal writable?), not command *content*.
- Note: `framework/security/` and `framework/tools/secure_wrapper.py` no longer
  exist in the tree (already removed); do not reintroduce them as safety surfaces.
- `TurnCustomKey` enum for per-turn custom state keys in `TurnStateBase.custom`
