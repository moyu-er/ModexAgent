<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# agents

Agent reasoning pattern implementations. Each sub-package implements a specific strategy following the `Agent[E]` generic contract.

## Purpose

The `agents/` module provides concrete agent implementations: the `ReActAgent` (Thought→Action→Observation loop with approval suspension/resume), `SummarizerAgent` (single-turn tool-free summarization), and `ExperienceReviewAgent` (ReAct-based conversation review for experience creation/update). New agent strategies go in new subdirectories.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Exports `ReActAgent`, `ReActEvent` |

## Subdirectories

| Directory | Files | Purpose |
|-----------|-------|---------|
| `react/` | 11 py (incl. `nodes/`) | `ReActAgent` — 4-node graph (START→LLM→TOOL→END), `RuntimeAssembler`, `TieredToolApprovalClassifier`, `ReActTurnState`, approval suspend/resume (see `react/AGENTS.md`) |
| `summarizer/` | 8 py | `SummarizerAgent` (single-turn, no tools), `ArchiveSummarizer` (MD archive generation), `KnowledgeConsolidator` (ReAct-based knowledge consolidation), `ScopedFileAgent` base class (see `summarizer/AGENTS.md`) |
| `experience/` | 2 py | `ExperienceReviewAgent` — ReAct agent that reviews conversations and creates/updates EXPERIENCE.md files using experience tools (see `experience/AGENTS.md`) |

### react/ Submodule Details

The ReAct module is the primary agent runtime. Key components:

| File | Description |
|------|-------------|
| `agent.py` | `ReActAgent(Agent[ReActEvent])` — event enum, turn context setup, delegates to graph |
| `graph.py` | `ReActGraph` — 4-node graph: START→LLM→TOOL→END with reason-based edges |
| `state.py` | `ReActTurnState`, snapshot payload keys, `ReActRuntimeStateCodec` |
| `builder.py` | `ReActAgentBuilder` — `build_agent()` + `build_emitter_factory()` from `AgentDescriptor` |
| `approval.py` | `ApprovalRuntime` + `TieredToolApprovalClassifier` (NORMAL/DANGEROUS path-based) |
| `assembler.py` | `RuntimeAssembler` — sole constructor of `AgentRuntime` from `RuntimeServicesConfig` |
| `constants.py` | `ReActNode`, `ReActReason` enums |
| `nodes/start.py` | `StartNode` — routes to LLM (fresh) or stored `current_node` (resume from suspended) |
| `nodes/llm.py` | `LLMNode` — calls LLM, handles streaming, emits iteration events |
| `nodes/tool.py` | `ToolNode` — classify all → suspend for approval → batch execute → route |
| `nodes/end.py` | `EndNode` — assembles `AgentResult` (normal/error/cancelled) |

### summarizer/ Submodule Details

| File | Description |
|------|-------------|
| `agent.py` | `SummarizerAgent(Agent)` — single-turn LLM call, no tools. Used for compression, fact extraction, memory update |
| `scoped_file_agent.py` | `ScopedFileAgent` — ReAct agent base with scoped file tools, `SummarizerTrajectoryEmitter` for JSONL traces, 2-attempt retry |
| `archive_agent.py` | `ArchiveSummarizer(ScopedFileAgent, ArchiveGenerator)` — generates `context.md`/`knowledge.md`/`index.md` from pruned messages |
| `consolidator.py` | `KnowledgeConsolidator(ScopedFileAgent, KnowledgeConsolidatorBase)` — reads `knowledge.md` from archives, updates `SOUL.md`/`USER.md`/`MEMORY.md` via ReAct |
| `emitter.py` | `SummarizerTrajectoryEmitter` — JSONL trace file writer for agent observability |
| `abc.py` | `ArchiveGenerator` ABC, `KnowledgeConsolidatorBase` ABC, `ArchiveSummarizerResult`, lazy prompt loader |
| `strategy.py` | `SummarizationStrategy` — configurable summarization approach |

### experience/ Submodule Details

| File | Description |
|------|-------------|
| `review_agent.py` | `ExperienceReviewAgent(ScopedFileAgent)` — ReAct agent with 6 experience tools, 2-attempt retry, JSONL trace observability |

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
├── ReActAgent               (graph-based, 4-node, with approval)
├── SummarizerAgent          (single-turn, no tools)
└── ScopedFileAgent          (ReAct with scoped file tools)
    ├── ArchiveSummarizer    (pruned → archive files)
    ├── KnowledgeConsolidator (archive → knowledge files)
    └── ExperienceReviewAgent (conversation → EXPERIENCE.md, in agents/experience/)
```

## For AI Agents

### Working In This Directory
- New agent strategies go in new subdirectories
- Each agent must inherit `Agent[E]` and define `event_enum`
- `SummarizerAgent` uses predefined prompt types: PROMPT_COMPRESSION, PROMPT_FACT_EXTRACTION, PROMPT_MEMORY_UPDATE, PROMPT_KNOWLEDGE_CONSOLIDATION
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
- **full**: all services wired through `AgentRuntimeServices` via `RuntimeAssembler`

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
- `framework.core.agent` — `Agent[E]`, `AgentContext`
- `framework.core.graph` — `Graph[R]`, `Node[R]`, `GraphEngine`, `GraphInterrupt`
- `framework.core.tool_manager` — `InMemoryToolManager`, `Tool`, `ToolResult`
- `framework.core.provider` — `LLMProvider`, `StreamingLLMProvider`
- `framework.core.emitter` — `ContentEmitter`, `AgentResult`
- `framework.core.types` — `MessageRole`, `MessageType`, `ToolCall`
- `framework.core.session_id` — `SessionInfo`
- `framework.runtime` — `AgentRuntime`, `AgentRuntimeServices`, `ReActTurnState`, `RuntimeAssembler`
- `framework.memory.prompts` — `SummarizerPromptRegistry`
- `framework.memory.tools.experience` — Experience tools (for ExperienceReviewAgent)
- `framework.utils.helpers` — `strip_think`
- `framework.hook` — `HookRunner`, `HookPoint`
- `framework.interceptor` — `InterceptorChain`

### External
- None beyond framework core dependencies.

<!-- MANUAL -->
<!-- Additional manual entries can be added below this line. -->

