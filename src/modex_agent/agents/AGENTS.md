<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# agents

Agent reasoning pattern implementations. Each sub-package implements a specific strategy following the `Agent[E]` generic contract.

## Purpose

The `agents/` module provides concrete agent implementations: the `ReActAgent` (Thought→Action→Observation loop with approval suspension/resume, built on the `modex_graph` engine), `ExternalAgent` (Pi/OpenCode CLI harness), and `ExperienceReviewAgent` (ReAct-based conversation review for experience creation/update). The deprecated `SummarizerAgent` was removed (ADR-0033 D10). New agent strategies go in new subdirectories.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Exports `ReActAgent`, `ReActEvent` |

## Subdirectories

| Directory | Files | Purpose |
|-----------|-------|---------|
| `react/` | 14 py (incl. `nodes/`) | `ReActAgent` — 4-node graph (START→LLM→TOOL→END) built on `modex_graph`, `TieredToolApprovalClassifier`, `ReActTurnState` (GraphState), `ReactGraphRuntime` adapter, approval suspend/resume (see `react/AGENTS.md`) |
| `external/` | 21 py (incl. `providers/`) | `ExternalAgent` — provider-neutral streaming harness, Pi/OpenCode adapters, session-map ABC, env/prompt/path/OS process seams (see `external/AGENTS.md`, ADR-0022) |
| `summarizer/` | 6 py | `ArchiveSummarizer` (MD archive generation), `CoreMemoryConsolidator` (ReAct-based core memory consolidation; renamed from `KnowledgeConsolidator` per ADR-0035), `ScopedFileAgent` base class (see `summarizer/AGENTS.md`). The deprecated `SummarizerAgent` was removed (ADR-0033 D10). |
| `experience/` | 2 py | `ExperienceReviewAgent` — ReAct agent that reviews conversations and creates/updates EXPERIENCE.md files using experience tools (see `experience/AGENTS.md`) |

### react/ Submodule Details

The ReAct module is the primary agent runtime. Key components:

| File | Description |
|------|-------------|
| `agent.py` | `ReActAgent(Agent[ReActEvent])` — event enum, turn context setup, constructs `Graph` + `GraphEngine` + `ReActGraphContext`, delegates to `engine.run_async()` |
| `graph.py` | `build_react_graph()` — builds `Graph[ReActTurnState]` with 4 nodes + 8 edges using `modex_graph.Graph` API |
| `context.py` | `ReActGraphContext(GraphContext[ReActTurnState])` — type-safe accessors (`agent_ctx`, `tool_manager`, `context_manager`) |
| `runtime.py` | `ReactGraphRuntime(GraphRuntime)` — AOP bridge: maps ReAct StrEnums to `HookPoint`/`InterceptorScope`/`ReActEvent`, bridges `GraphContext.user_data` → `AgentContext` for all AOP services |
| `state.py` | `ReActTurnState(GraphState)`, `ReActSnapshotPolicy` (simplified via `state.checkpoint()` per-channel path, ADR-0033 D14), `ReActRuntimeStateCodec` |
| `builder.py` | `ReActAgentBuilder` — `build_agent()` + `build_emitter_factory()` from `AgentDescriptor` |
| `approval.py` | `ApprovalRuntime` + `TieredToolApprovalClassifier` (NORMAL/DANGEROUS path-based) |
| `constants.py` | `ReActNode`, `ReActReason`, `ReActHookPoint`, `ReActScope`, `ReActEvent` StrEnums |
| `nodes/start.py` | `StartNode` — routes to LLM (fresh) or stored `current_node` (resume from suspended) |
| `nodes/llm.py` | `LLMNode` — calls LLM, handles streaming, dispatches hooks/interceptors via `ctx.runtime.*`, emits iteration events |
| `nodes/tool.py` | `ToolNode` — classify all → suspend for approval via `ctx.interrupt(tx)` → batch execute → route |
| `nodes/end.py` | `EndNode` — assembles `AgentResult` (normal/error/cancelled), writes `ctx.state.result` |

### summarizer/ Submodule Details

| File | Description |
|------|-------------|
| `scoped_file_agent.py` | `ScopedFileAgent` — ReAct agent base with scoped file tools, `SummarizerTrajectoryEmitter` for JSONL traces, 2-attempt retry |
| `archive_agent.py` | `ArchiveSummarizer(ScopedFileAgent, ArchiveGenerator)` — generates `context.md`/`knowledge.md`/`index.md` from pruned messages |
| `consolidator.py` | `CoreMemoryConsolidator(ScopedFileAgent, CoreMemoryConsolidatorBase)` (renamed from `KnowledgeConsolidator` per ADR-0035) — reads `knowledge.md` from archives, updates `SOUL.md`/`USER.md`/`MEMORY.md` via ReAct |
| `emitter.py` | `SummarizerTrajectoryEmitter` — JSONL trace file writer for agent observability |
| `abc.py` | `ArchiveGenerator` ABC, `CoreMemoryConsolidatorBase` ABC (renamed from `KnowledgeConsolidatorBase` per ADR-0035), `ArchiveSummarizerResult`, lazy prompt loader |

### experience/ Submodule Details

| File | Description |
|------|-------------|
| `review_agent.py` | `ExperienceReviewAgent(ScopedFileAgent)` — ReAct agent with 6 experience tools, 2-attempt retry, JSONL trace observability |

### external/ Submodule Details

| File | Description |
|------|-------------|
| `agent.py` | `ExternalAgent`, `StreamingProviderBackend`, stale-session retry, canonical `TurnEvent` projection, retryable `stop()` |
| `builder.py` | `ExternalAgentBuilder` — explicit backend/parser/session-store/env collaborator assembly |
| `session_store.py` | `ExternalSessionMapStore` ABC + local-file adapter; SQLite adapter lives under `persistence/adapters/` |
| `env_builder.py` / `runtime_config.py` | Per-turn `MODEX_*` environment and provider-visible AGENTS.md runtime block |
| `os_layer.py` | Cross-platform executable resolution, process-group spawn, and complete process-tree termination |
| `providers/opencode_server_backend.py` | Warm `opencode serve` SSE backend; transactional readiness and close-time reap |
| `providers/opencode_backend.py` | Per-turn `opencode run` fallback with active-child ownership |
| `providers/pi_backend.py` | Per-turn Pi backend with active-child ownership |

Lifecycle rule: upper layers call only `StreamingProviderBackend.close()`.
Persistent and per-turn differences stay inside adapters. Cleanup failure must
propagate so `ExternalAgent` and `AgentPool` retain the owner for retry;
never mark an agent stopped or remove it from a pool before close succeeds.

The `ExperienceReviewAgent`:
1. Receives a conversation snapshot + existing experiences XML
2. Builds system prompt from `experience/review` prompt template
3. Assembles a standalone `ReActAgent` with 6 experience tools: Read, Write, Edit, List, RenameDir, Delete
4. Runs up to 100 iterations (configurable) with `SummarizerTrajectoryEmitter` for tracing
5. Retries once on failure (2 attempts total)
6. All file operations are scoped to `experience_dir`

### Agent Hierarchy

```
Agent[E]
├── ReActAgent               (modex_graph-based, 4-node, with approval)
├── ExternalAgent      (external CLI harness, provider backend lifecycle)
└── ScopedFileAgent          (ReAct with scoped file tools)
    ├── ArchiveSummarizer    (pruned → archive files)
    ├── CoreMemoryConsolidator (archive → core memory files; renamed from KnowledgeConsolidator per ADR-0035)
    └── ExperienceReviewAgent (conversation → EXPERIENCE.md, in agents/experience/)
```

## For AI Agents

### Working In This Directory
- New agent strategies go in new subdirectories
- Each agent must inherit `Agent[E]` and define `event_enum`
- External coding teardown converges through `StreamingProviderBackend.close()`; do not add provider-kind branches to agent, pool, or workspace shutdown.
- `ScopedFileAgent._run_agent()` is the shared entry point for all ReAct-based summarizers
- Prompt templates come from `SummarizerPromptRegistry` loaded via `_get_registry()`

### ReAct Graph Edges
```
START --NORMAL_START--> LLM
START --RESUME_TOOLS--> TOOL
LLM   --HAS_TOOLS--> TOOL
LLM   --NO_TOOLS--> END
LLM   --MAX_ITERATIONS--> END
LLM   --LLM_ERROR--> END
TOOL  --TOOLS_DONE--> LLM
TOOL  --TURN_CANCELLED--> END
```

### Runtime Modes
- **clean**: plain ReAct graph, no hooks/interceptors/approval/control/state-store
- **full**: all services wired through `AgentRuntimeServices` (assembly lives in `AgentPipeline` / `TurnContextBuilder`; the old `RuntimeAssembler` was removed as dead code — approval runtime is built by `ioc.factories.approval.build_approval_runtime` and injected via `AgentPipeline.runtime_services`)

### Approval Flow
```
ToolNode._classify_all() → TieredToolApprovalClassifier
  → PENDING: _suspend_for_approval() → TurnSnapshot → GraphInterrupt
  → Pipeline: ApprovalRenderer.detect() → apply_decision()
  → StartNode detects SUSPENDED → routes to TOOL → _resume_suspended_batch()
  → PRE_APPROVED_TOOL_IDS set on ALLOWED tools, denied tools return error
```

### Common Patterns
```python
class MyAgent(Agent[MyEvent]):
    event_enum = MyEvent

    async def run(self, context: AgentContext, emitter: ContentEmitter[MyEvent]) -> AgentResult:
        ...
```

## Dependencies

### Internal
- `modex_agent.core.agent` — `Agent[E]`, `AgentContext`
- `modex_graph` — `Graph[S]`, `Node[S]`, `GraphEngine`, `GraphInterrupt`, `GraphContext`, `GraphRuntime` (ADR-0033)
- `modex_agent.core.tool_manager` — `InMemoryToolManager`, `Tool`, `ToolResult`
- `modex_agent.core.provider` — `LLMProvider`, `StreamingLLMProvider`
- `modex_agent.core.emitter` — `ContentEmitter`, `AgentResult`
- `modex_agent.core.types` — `MessageRole`, `MessageType`, `ToolCall`
- `modex_agent.core.session_id` — `SessionInfo`
- `modex_agent.runtime` — `AgentRuntime`, `AgentRuntimeServices`, `ReActTurnState`
- `modex_agent.memory.prompts` — `SummarizerPromptRegistry`
- `modex_agent.memory.tools.experience` — Experience tools (for ExperienceReviewAgent)
- `modex_agent.utils.helpers` — `strip_think`
- `modex_agent.hook` — `HookRunner`, `HookPoint`
- `modex_agent.interceptor` — `InterceptorChain`

### External
- None beyond framework core dependencies.

<!-- MANUAL -->
<!-- Additional manual entries can be added below this line. -->

