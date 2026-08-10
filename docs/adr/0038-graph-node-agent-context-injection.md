# Graph Node Agent Context Injection

Inject full graph topology, Origin Request linkage, upstream/downstream node descriptions, missing-upstream explanation, END aggregation semantics, and a shared knowledge base into agent context when an agent runs inside a graph node. Injection flows through two existing convergent paths — `GraphWorkflowProvider` (system prompt) and SYSTEM_REMINDER (via `_format_integrated_input` per-invocation and `KnowledgeSummaryHook` per-turn-attempt) — plus the `GraphDeliverTool` and `GraphKnowledgeBaseTool` descriptions; no third parallel injection path is introduced. The knowledge base adds a new tool and three hooks, all converging on existing mechanisms.

## Context

When `BotAgentNode.execute` runs a ReAct agent turn inside a graph node, the agent sees only: (a) a generic "you are a node in a graph workflow" prompt, (b) upstream payloads as `[Input from graph node 'X']` messages, (c) available deliver targets in the deliver tool. The agent does **not** know: which graph it is in, its own node name/identity, the full topology, its position (upstream/downstream), the Origin Request's source and nature, whether missing upstream nodes will deliver later, or how END aggregates multiple deliveries into the final reply. This causes agents to confuse Origin Request with node delivers, wait indefinitely for inputs from non-activated paths, and produce deliver content in formats misaligned with END's aggregation semantics.

## Decision

Inject structured graph context across three layers, all via existing convergent paths:

1. **System prompt (GraphWorkflowProvider)**: a `## Graph Node Context` section (single H2) with `### Topology` subsection carrying the full DAG (node names without type labels, edges, current node highlighted, END aggregation label, Origin Request linkage). Node descriptions are **not** included here — only structural skeleton, to avoid noise from irrelevant nodes.

2. **SYSTEM_REMINDER (_format_integrated_input)**: upstream node descriptions alongside delivered content (only for nodes that actually delivered input), plus an `[Upstream Status]` block explaining which upstream nodes delivered and which paths were not activated (with explicit "no further input expected, proceed" guidance).

3. **Deliver tool (GraphDeliverTool.description)**: current node identity ("You are node: X") + enhanced END description with aggregation semantics (delivery-order concatenation into reply list, input expectations for direct upstream).

4. **Deliver content template**: two patterns — **Producer** (Task/Result/Status, existing) and **Relay** (Source/Selection/Summary/Omitted, new). Relay is for nodes that selectively pass summarized upstream content downstream; verbatim forwarding is discouraged because it defeats the node's filtering role.

5. **Agent node input idempotency**: `AgentNode` overrides `Node._integrate_upstream` to always filter `CONSUMED_PENDING` delivers (the engine default only filters them on GraphInterrupt resume). Agent session memory (ReAct `MessageStore`) persists upstream input across invocations — crash recovery must not re-inject already-consumed delivers. `BotAgentNode.execute` detects re-execution (session has existing messages) and skips `[Origin Request]` duplication, injecting only new upstream input.

6. **Knowledge base shared context**: a per-graph-instance markdown knowledge base that nodes read and write via a dedicated `GraphKnowledgeBaseTool`, with automatic changelog recording and optional per-node usage enforcement via hooks. This provides a broadcast-write/shared-read context layer complementary to the point-to-point `deliver` mechanism.

   **Directory layout** (under workspace `.modex/`, containment-checked via `WorkspacePaths`):

   ```
   .modex/graphs/
   ├── instances/<graph_instance_id>/    # per-run shared blackboard
   │   ├── findings.md                     # facts discovered (append-only by convention)
   │   ├── decisions.md                    # decisions made by any node (append-only by convention)
   │   ├── open_questions.md               # unresolved questions (overwrite OK)
   │   ├── context.md                      # stable context for this run (overwrite OK)
   │   └── changelog.md                    # auto-recorded modification log (READ-ONLY)
   └── knowledge/<spec_name>/             # cross-instance shared experience (Phase 2)
       ├── project_context.md
       └── lessons_learned.md
   ```

   **GraphKnowledgeBaseTool** (tool `name` property: `"knowledge_base"`) — single multi-action tool injected via `GraphToolPreset` alongside `GraphDeliverTool`. Both are graph-scoped tools constructed fresh in each `BotAgentNode.execute()` call and packed into a single `GraphToolPreset`:

   ```python
   # In BotAgentNode.execute(ctx):
   deliver_tool = self._ensure_deliver_tool()      # cached (depends on topology, stable)
   knowledge_tool = GraphKnowledgeBaseTool(         # fresh per execute (depends on graph_instance_id)
       knowledge_dir=knowledge_dir,
       node_name=self.name,
       spec_name=self._graph_ref.name,
       capabilities=derived_from_tool_preset,
   )
   preset = GraphToolPreset(graph_tools=[deliver_tool, knowledge_tool])
   agent_context.tool_manager = preset.build_tool_manager(agent_context.tool_manager)
   ```

   - **Construction timing — not cached on the node instance**: unlike `_ensure_deliver_tool` (which caches `self._deliver_tool` because topology is stable across runs of the same spec), `GraphKnowledgeBaseTool` is constructed fresh in each `execute()` call. `BotAgentNode` instances are reused across graph instances (same compiled graph, different `graph_instance_id`); caching a knowledge tool would route writes to a stale directory. The `GraphToolPreset.build_tool_manager` already creates a fresh `InMemoryToolManager` per call, so the tool instance lifecycle is naturally bounded to one `execute()` invocation.

   - **Path resolution**: the knowledge directory is resolved from `ctx.graph_instance_id` + the workspace root via the existing `WorkspaceResolverCell` (the same lazy resolver `BotAgentNode` already holds). The path is `<workspace_root>/.modex/graphs/instances/<graph_instance_id>/`. Directory is created with `mkdir(parents=True, exist_ok=True)` at `execute()` entry; no files are pre-created — the agent creates them via `write`/`edit` actions.

   - **Node identity at execute time**: obtained from `current_agent_context` → `agent_context.graph_context` (same contextvar pattern as `GraphDeliverTool.execute`). The tool does not store node identity as a constructor parameter that could go stale — it reads it live from the contextvar on each `execute()` call. Constructor parameters are: `knowledge_dir` (per-run path), `node_name` (stable per node instance), `spec_name` (stable per spec), `capabilities` (stable per agent tool preset).

   - Parameters: `action` (read/write/edit/ls), `pattern` (enum: `findings` / `decisions` / `open_questions` / `context` / `changelog`), plus the same parameters each action shares with the standard read/write/edit tools (`content`, `old_string`, `new_string`, `replace_all`, `offset`, `limit`, `mode`). The `pattern` enum is the same for all agents; the `action` enum is filtered by `get_dynamic_schema()` based on the agent's tool capabilities (see below).
   - `pattern` values map internally to markdown files (e.g. `findings` → `findings.md`); the `.md` suffix is an implementation detail never exposed in the schema or description. The tool description states that patterns simulate markdown file read/write semantics.
   - `changelog` pattern rejects `write` and `edit` actions (read-only, enforced by the tool).
   - `write` has a `mode` parameter: `"create"` (default — fails if file exists) or `"overwrite"`. This prevents accidental overwrites of append-only patterns.
   - Every successful `write` or `edit` auto-appends an entry to the `changelog` pattern with timestamp, node name, action, and unified diff. The agent cannot call `write`/`edit` on `changelog` — it is tool-maintained metadata, not agent-authored content.
   - The tool reuses I/O helpers from `tools/standard/file_tool.py` (`_read_file`, `_write_file`, `_build_unified_diff`, `_find_actual_string`, `_paginate_file`) — it is a thin containment + changelog + pattern-validation shell over existing file I/O, not a parallel implementation.
   - The tool description is dynamic: includes current node name, graph name, and a per-pattern guide explaining what each pattern is for and how to write to it. This description is the primary guidance for agent behavior — the schema is minimal (parameters mirror standard tools) and the description carries the usage semantics.
   - **Capability-aware dynamic schema**: the tool's `action` enum and `parameters` are generated dynamically based on the agent's `ToolPreset`, via `get_dynamic_schema()` override (same `DynamicSchemaProvider` pattern used by `ReadFileTool.get_dynamic_schema_for(caps)` and `GraphDeliverTool.get_dynamic_schema()`). An agent with `ToolPreset.READ_ONLY` (no `write`/`edit` standard tools) sees only `action: ["read", "ls"]`; an agent with `ToolPreset.MINIMAL` (has `write` but no `edit`) sees `action: ["read", "write", "ls"]` but no `old_string`/`new_string`/`replace_all` parameters. The constructor receives a `KnowledgeToolCapabilities` value (derived from the agent's `ToolPreset` at `BotAgentNode.execute` injection time) that gates which actions and parameters the schema exposes. At runtime, the tool's `execute()` also rejects actions outside the declared capability set — defense in depth, in case the LLM hallucinates a disallowed action.

   **Tool description contract** (English, dynamic):

   ```
   You are node: {node_name} (graph: {spec_name})
   Shared knowledge base — cross-node context that persists across turns.
   Patterns simulate markdown file read/write; each pattern is a named
   knowledge slot with specific semantics.

   ## Patterns

   Every write/edit is auto-recorded in the changelog pattern with your
   node identity and diff — you cannot modify changelog directly.

   ### findings
   Facts discovered during this run. Append-only by convention — use
   action='edit' to add new findings at the end, do NOT overwrite with
   action='write'. Each entry should be a self-contained fact.
   Format: `- [node_name] fact description`

   ### decisions
   Choices made by any node that affect downstream work. Append-only.
   Use action='edit' to add. Each entry: what was decided, by whom, why.
   Format: `- [node_name] decision — rationale`

   ### open_questions
   Unresolved questions needing attention. Overwrite OK — update or
   remove resolved questions. Use action='write' mode='overwrite'.

   ### context
   Stable context for this graph run — project info, constraints,
   conventions. Overwrite OK. Set once at the start, update if needed.

   ### changelog
   Auto-maintained modification log. READ-ONLY — action='write' and
   action='edit' are rejected. Use action='read' to audit changes.

   ## Actions
   - read: same as read tool (offset/limit pagination)
   - write: same as write tool (mode='create' fails on existing,
     mode='overwrite' replaces)
   - edit: same as edit tool (old_string/new_string/replace_all)
   - ls: list available patterns with sizes

   ## When to use
   - At turn start: read findings + open_questions to build on prior work.
   - When you discover something: edit into findings.
   - When you make a choice: edit into decisions.
   - When you have a question: write to open_questions.
   ```

   **KnowledgeSummaryHook** (`StartNodeTurnHook`) — injects a truncated summary at each turn attempt start, before the LLM call:

   - Injects `findings` pattern tail (last ~800 chars, snapped to line boundary) and `open_questions` pattern (if non-empty, ~400 chars) as a single SYSTEM_REMINDER with `<knowledge_base>` XML tags.
   - Does NOT inject `decisions`, `context`, or `changelog` — agent reads these on-demand via the tool. Findings is the primary channel; decisions is referenced in the summary tail ("use knowledge_base read pattern='decisions' for decision history").
   - Truncation notice: `[truncated — use knowledge_base action='read' for full content]`.
   - Takes tail (not head) for append-only files — most recent findings are most relevant to the current turn.
   - Uses the same SYSTEM_REMINDER mechanism as `_format_integrated_input`, at a different timing (per-turn-attempt vs per-invocation). This is not a third parallel injection path — same mechanism, different schedule.

   **KnowledgeCounterResetHook** (`BeforeTurnHook`) — resets per-turn knowledge counters at each turn attempt start:

   - Resets `GRAPH_KNOWLEDGE_READ_COUNT` and `GRAPH_KNOWLEDGE_WRITE_COUNT` in `state.custom`.
   - Does NOT reset `GRAPH_DELIVER_COUNT` (once delivered, no need to re-deliver).
   - Rationale: each turn attempt should re-read knowledge (other nodes may have written new content since the last attempt), but deliver is a one-shot completion signal.

   **KnowledgeRetryHook** (`AfterTurnHook`) — optionally enforces knowledge usage per node:

   - Per-node config via `NodeSpec.config.knowledge`:
     ```yaml
     knowledge:
       require_write: false  # default false; true = request continuation if no write/edit
       require_read: false   # default false; true = request continuation if no read
     ```
   - `require_write` checks `GRAPH_KNOWLEDGE_WRITE_COUNT > 0` (incremented by both `write` and `edit` actions on successful execution — see Implementation Reference §2 for the counter mechanism).
   - `require_read` checks `GRAPH_KNOWLEDGE_READ_COUNT > 0` (incremented by `read` action).
   - Both default to `false` — without config, knowledge usage is guided by the tool description only, not enforced.
   - Shares the `CONTINUATION_REQUEST` one-shot flag with `DeliverRetryHook`. Hook registration order: `DeliverRetryHook` first (hard requirement), `KnowledgeRetryHook` second (soft requirement). If `CONTINUATION_REQUEST` is already set (consumed by the first hook that sets it), `KnowledgeRetryHook` skips — the flag is `pop`-ed by `AfterTurnNode` on consumption.
   - Bounded by `MAX_TURNS` (same as `DeliverRetryHook`).
   - **Reminder content** (SYSTEM_REMINDER, English):
     - `require_write` unmet: `"You ended without writing to the knowledge base. Use the knowledge_base tool (action='edit' or action='write') to record your findings or decisions before finishing."`
     - `require_read` unmet: `"You ended without reading the knowledge base. Use the knowledge_base tool (action='read' pattern='findings') to check what other nodes have discovered before continuing."`
     - Both unmet: combine into a single reminder covering both.

   **Injection path summary (three paths, no parallel divergence):**

   | Path | Timing | Content | Mechanism |
   |---|---|---|---|
   | `GraphWorkflowProvider` | system prompt build | `### Knowledge Base` usage guide (pattern list + write conventions) | system prompt |
   | `_format_integrated_input` | `BotAgentNode.execute` start (per-invocation) | upstream delivers + `[Origin Request]` | SYSTEM_REMINDER |
   | `KnowledgeSummaryHook` | `StartNodeTurnHook` (per-turn-attempt) | findings + open_questions summary (truncated) | SYSTEM_REMINDER (XML) |

   **Relationship to deliver:** deliver is point-to-point (this turn's output to a specified downstream node); knowledge base is broadcast-write/shared-read (cumulative findings all nodes can read). They are complementary, not substitutes. Both are injected via `GraphToolPreset`; both use `current_agent_context` for node identity.

## Implementation Reference

This section provides implementation-level detail for the knowledge base design (§6). It is reference material — not the final implementation, but the key data flows, mappings, and helper reuse that an implementer needs to understand the design fully.

### §1 — TurnCustomKey new enum values

Add to `src/modex_agent/runtime/enums.py` `TurnCustomKey`:

```python
GRAPH_KNOWLEDGE_READ_COUNT = "graph_knowledge_read_count"
GRAPH_KNOWLEDGE_WRITE_COUNT = "graph_knowledge_write_count"
```

These follow the existing `GRAPH_DELIVER_COUNT` pattern. Counters live in `state.custom` (the per-invocation ReActTurnState dict), not on the tool instance.

### §2 — Counter mechanism (why not scan tool-call history)

**Problem with history scanning:** the LLM's context contains tool-call records from prior turn attempts and prior invocations. Scanning `ctx.history` for `knowledge_base` tool calls would find stale calls from previous attempts — a `read` in attempt 1 should not satisfy `require_read` in attempt 2 (other nodes may have written new content since).

**Correct mechanism — per-attempt counter incremented on successful tool execution:**

```python
# Inside GraphKnowledgeBaseTool.execute, after a successful action:
agent_context = current_agent_context.get(None)
if agent_context is not None and agent_context.runtime is not None:
    state = agent_context.runtime.state
    if state is not None:
        if action in ("write", "edit"):
            count = state.custom.get(TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT, 0)
            state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT] = count + 1
        elif action == "read":
            count = state.custom.get(TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT, 0)
            state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = count + 1
```

This is the exact same pattern as `GraphDeliverTool.execute` (line 224-229) incrementing `GRAPH_DELIVER_COUNT`. The counter reflects what the framework observed (actual successful execution), not what the LLM sees (which includes stale history).

`KnowledgeCounterResetHook` (`BeforeTurnHook`) resets both counters at each turn attempt start, so each attempt independently requires fresh knowledge interaction. `GRAPH_DELIVER_COUNT` is NOT reset — once delivered, the node has produced its output and no re-deliver is needed.

### §3 — KnowledgeToolCapabilities mapping

Derived from the agent's `ToolPreset` at `BotAgentNode.execute` injection time:

| ToolPreset | Standard tools present | Knowledge actions allowed | Rationale |
|---|---|---|---|
| `FULL` | read + write + edit + ls + grep + glob + bash | `read, write, edit, ls` | Full file capability → full knowledge capability |
| `READ_WRITE` | read + write + edit + grep + glob + bash | `read, write, edit, ls` | Same as FULL (bash irrelevant) |
| `READ_ONLY` | read + ls + grep + glob + bash | `read, ls` | No write/edit → knowledge is read-only |
| `MINIMAL` | read + write + ls + grep (no edit, no bash) | `read, write, ls` | Has write but no edit → knowledge can create/overwrite but not patch |
| `NONE` | no standard tools | `read, ls` | No file capability, but knowledge read is always useful (even communication-only agents benefit from reading findings) |
| `WEB` | web_search + web_reader | `read, ls` | No file tools → knowledge is read-only |

Design decision: `read` and `ls` are always available regardless of preset — every graph node benefits from reading shared knowledge. `write`/`edit` are gated by the corresponding standard tool presence.

### §4 — NodeSpec.config.knowledge parsing

`BotAgentNodeConfig` (in `examples/bot_project/bot/graph/agent_node_factory.py`) extends with an optional `knowledge` field:

```python
class KnowledgeNodeConfig(BaseModel):
    """Per-node knowledge enforcement config."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    require_write: bool = False
    require_read: bool = False

class BotAgentNodeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    agent: str
    pool: str = "default"
    knowledge: KnowledgeNodeConfig = KnowledgeNodeConfig()  # default: no enforcement
```

`BotAgentNodeFactory.create()` passes `config.knowledge` to `BotAgentNode`, which stores it. `KnowledgeRetryHook` reads the config from `state.custom` (set by `BotAgentNode.execute` before the turn starts) — same pattern as `DeliverRetryHook` reading `GRAPH_DELIVER_COUNT` from `state.custom`. This keeps `KnowledgeRetryHook` a shared hook (no per-node registration), consistent with `DeliverRetryHook`.

### §5 — WorkspacePaths new accessors

Add to `src/modex_agent/workspace/paths.py` `WorkspacePaths`:

```python
SUBDIR_GRAPHS = "graphs"
SUBDIR_GRAPH_INSTANCES = "instances"
SUBDIR_GRAPH_KNOWLEDGE = "knowledge"

def graph_instance_knowledge_dir(self, graph_instance_id: int) -> Path:
    """Per-instance knowledge blackboard: .modex/graphs/instances/<gid>/"""
    return self._child(SUBDIR_GRAPHS, SUBDIR_GRAPH_INSTANCES, str(graph_instance_id))

def graph_spec_knowledge_dir(self, spec_name: str) -> Path:
    """Cross-instance shared knowledge: .modex/graphs/knowledge/<spec_name>/"""
    return self._child(SUBDIR_GRAPHS, SUBDIR_GRAPH_KNOWLEDGE, spec_name)
```

Both route through `_child` → `safe_segment` + `is_relative_to` containment check. `str(graph_instance_id)` is a Snowflake ID (numeric string), safe.

### §6 — KnowledgeSummaryHook path resolution chain

`StartNodeTurnHook` receives `AgentContext`, not `GraphContext`. The resolution chain:

```
hook.start_node_turn(ctx: AgentContext)
  → graph_ctx = ctx.graph_context        # set by BotAgentNode.execute
  → if graph_ctx is None: return         # non-graph context, skip
  → gid = graph_ctx.graph_instance_id
  → workspace_root = ctx.runtime.services.workspace_manager.resolve_workspace().workspace_root
  → knowledge_dir = workspace_root / ".modex" / "graphs" / "instances" / str(gid)
  → if not knowledge_dir.exists(): return # fresh instance, no knowledge yet
  → read findings pattern tail + open_questions pattern tail
  → inject SYSTEM_REMINDER
```

Guard conditions: `graph_context is None` (non-graph agent), `knowledge_dir` does not exist (fresh instance with no writes yet), files do not exist (no findings written yet) — all silently skip injection.

### §7 — Tool execute internal action dispatch

Each action reuses existing helpers from `tools/standard/file_tool.py`:

| Action | Helpers reused | Changelog recorded? |
|---|---|---|
| `read` | `_paginate_file(path, offset, limit)` | No (read-only operation) |
| `write` | `_write_file(path, content, encoding, line_endings)` + `_build_unified_diff(old, new)` | Yes — diff between old (if existed) and new |
| `edit` | `_read_file(path)` → `_find_actual_string(content, old_string)` → `_write_file(path, updated, ...)` + `_build_unified_diff(old, updated)` | Yes — diff between old and updated |
| `ls` | `knowledge_dir.glob("*.md")` + file sizes | No (read-only operation) |

Pattern → path mapping: `pattern + ".md"` (e.g. `"findings"` → `knowledge_dir / "findings.md"`). Validated against `KNOWLEDGE_PATTERNS` frozenset before file I/O.

### §8 — Dynamic schema construction (_PARAM_SCHEMAS + _ACTION_PARAMS)

The `get_dynamic_schema()` override builds the schema by filtering a base definition against `KnowledgeToolCapabilities`, rather than hand-writing each variant:

```python
# Define each parameter's schema once
_PARAM_SCHEMAS: dict[str, dict] = {
    "action": {"type": "string", "enum": ["read", "write", "edit", "ls"]},
    "pattern": {"type": "string", "enum": [...PATTERNS...], "description": "..."},
    "content": {"type": "string", "description": "Content to write."},
    "old_string": {"type": "string", "description": "Text to find and replace."},
    "new_string": {"type": "string", "description": "Replacement text."},
    "replace_all": {"type": "boolean", "default": False},
    "offset": {"type": "integer", "default": 0},
    "limit": {"type": "integer", "default": 200},
    "mode": {"type": "string", "enum": ["create", "overwrite"], "default": "create"},
}

# Which params each action needs
_ACTION_PARAMS: dict[str, list[str]] = {
    "read":  ["offset", "limit"],
    "write": ["content", "mode"],
    "edit":  ["old_string", "new_string", "replace_all"],
    "ls":    [],
}
```

`get_dynamic_schema()` does three things:
1. Filter `action` enum: `["read", "write", "edit", "ls"]` ∩ allowed actions from `capabilities`.
2. Filter `properties`: keep `pattern` (always) + union of params from all allowed actions.
3. Keep `required: ["action", "pattern"]` (both always required).

The description is also compositional: pattern guide section (static, all agents see all patterns) + actions section (dynamic, only allowed actions listed) + when-to-use section (dynamic, only relevant guidance for allowed actions).

### §9 — Instance lifecycle and knowledge persistence

| Graph status | Instance knowledge | Behavior |
|---|---|---|
| `PENDING` → `RUNNING` | Created at `create_instance` (directory only, no files) | Agent writes via tool |
| `COMPLETED` | Retained on disk | Accessible via REST API for audit; not auto-archived |
| `CRASHED` | Retained on disk | Recovery: agent reads prior findings via tool (informs retry) |
| `PAUSED` | Retained on disk | Resume: agent reads accumulated findings |
| `STOPPED` | Retained on disk | Terminal — no cleanup |

No auto-deletion. Instance knowledge persists until manual cleanup or workspace eviction. This is deliberate — crash recovery requires the knowledge to survive, and post-run audit benefits from retention.

### §10 — Hook wiring in system provider assembly

In `examples/bot_project/bot/workspace/wiring/resources.py`, alongside `DeliverRetryHook` registration:

```python
# Knowledge hooks — shared across all graph instances in this workspace
shared_hooks.extend([
    KnowledgeSummaryHook(),           # StartNodeTurnHook — inject findings summary
    KnowledgeCounterResetHook(),      # BeforeTurnHook — reset per-turn counters
    # KnowledgeRetryHook reads per-node config from state.custom (set by
    # BotAgentNode.execute), so it is a shared hook like DeliverRetryHook —
    # no per-node registration needed.
    KnowledgeRetryHook(),
])
```

`KnowledgeRetryHook` reads `require_write` / `require_read` from `state.custom` (set by `BotAgentNode.execute` before the turn starts from `NodeSpec.config.knowledge`). If both are `false` (default), the hook is a no-op. This follows the `DeliverRetryHook` pattern — shared hook, per-node behavior driven by `state.custom`.

## Considered Options

- **Topology as a separate SYSTEM_REMINDER message**: rejected — topology is stable per-turn metadata, not dynamic input; system prompt is the correct home for "who am I" context, and SYSTEM_REMINDER interleaving would bury it among input messages.
- **Per-node context only in deliver tool**: rejected — the agent needs topology *before* deciding what to deliver; tool description is evaluated at tool-call time, too late for reasoning context.
- **Full node descriptions in topology**: rejected as noise — an agent only interacts with its direct upstream (with input) and downstream (via deliver). Descriptions of unrelated branch nodes add tokens without actionable value.
- **Third injection path (new provider + new message type)**: rejected per convergence rule — existing two paths (GraphWorkflowProvider + _format_integrated_input) cover stable-metadata and dynamic-input use cases; a third path would diverge the injection mechanism. The knowledge base summary uses the same SYSTEM_REMINDER mechanism at a different timing (per-turn-attempt via `KnowledgeSummaryHook`), not a new message type.
- **Knowledge base via standard file tools (read/write/edit) + system prompt guidance only**: rejected — standard tools have no path containment (agent can write anywhere), no changelog auto-recording (agent self-reporting is unreliable), no write-protection for existing files (default overwrite), and no node identity awareness. A dedicated `GraphKnowledgeBaseTool` with pattern validation, changelog, and read-only enforcement is the minimum viable surface.
- **Knowledge base as a parallel `deliver`-like routing mechanism**: rejected — knowledge base is a shared blackboard (broadcast-write/shared-read), not a routing mechanism. It does not participate in graph topology routing; `deliver` remains the sole routing path (ADR-0033 D6). Adding a second routing mechanism would violate the convergence rule.
- **Injecting all patterns at turn start**: rejected — token cost. `findings` and `open_questions` are the most actionable; `decisions` overlaps with findings; `context` is covered by `[Origin Request]` + system prompt; `changelog` is metadata, not decision input. Agent reads the rest on-demand via the tool.
- **Forcing knowledge write on every node**: rejected — not every node produces knowledge worth recording (e.g., a coder node may just execute). Per-node `require_write` / `require_read` config (default false) lets spec authors opt in where enforcement adds value.
- **Three separate tools (knowledge_read / knowledge_write / knowledge_edit)**: rejected — agent tool lists are already long; a single multi-action tool with an `action` parameter is easier for the LLM to select and use.
- **Static schema with all actions always visible**: rejected — an agent with `ToolPreset.READ_ONLY` has no `write`/`edit` standard tools, so exposing `action: ["read", "write", "edit", "ls"]` in the knowledge tool schema creates a mismatch: the agent is told it can write knowledge but has no file-writing capability to learn the write mental model from. Worse, the LLM may attempt `action="write"` and the tool must reject it at runtime — a confusing failure. The dynamic schema (`get_dynamic_schema()` override gated by `KnowledgeToolCapabilities`) aligns the knowledge tool's surface with the agent's actual tool preset, following the existing `DynamicSchemaProvider` pattern already used for model-capability-aware (`ReadFileTool.get_dynamic_schema_for(caps)`) and topology-aware (`GraphDeliverTool.get_dynamic_schema()`) schema generation.

## Consequences

- The `## Graph Node Context` / `### Topology` section format becomes a contract with the agent: changing the format (node listing order, END description wording, Origin Request linkage text) may require re-tuning agent prompts that depend on specific phrasing.
- `_format_integrated_input` gains a dependency on `self._graph_ref` for upstream description lookup and missing-upstream computation — it must guard `None` (test/no-scheduler scenarios).
- The Relay deliver pattern adds a second content template; agents must self-select the appropriate pattern based on their role. This is guidance, not enforcement — the deliver tool accepts any string content.
- END's description in `GraphDeliverTool` becomes semantically detailed (aggregation behavior, input expectations) — this replaces the current hardcoded one-liner and is the primary guidance for END's direct upstream nodes.
- `AgentNode._integrate_upstream` diverges from the engine's `Node._integrate_upstream` — it always filters `CONSUMED_PENDING` delivers instead of only on resume. This is a justified override: agent nodes have a second persistence layer (session memory) that the engine does not know about. Re-consuming already-injected delivers would duplicate `SYSTEM_REMINDER` messages in the agent's session history.
- The `GraphKnowledgeBaseTool` description format (pattern names, usage guide, action descriptions) becomes a contract with the agent — changing pattern names or action semantics may require re-tuning agent prompts. The pattern set (`findings` / `decisions` / `open_questions` / `context` / `changelog`) is stable; adding new patterns is additive, removing or renaming is a breaking change.
- `KnowledgeSummaryHook` gains a dependency on the knowledge directory path, resolved from `graph_context.graph_instance_id` + workspace root. It must guard `None` (non-graph contexts) and missing directories (fresh instance with no knowledge written yet).
- The per-turn-attempt counter reset (`KnowledgeCounterResetHook`) creates a deliberate asymmetry: `GRAPH_DELIVER_COUNT` persists across turn attempts (once delivered, no re-deliver needed); `GRAPH_KNOWLEDGE_READ_COUNT` / `GRAPH_KNOWLEDGE_WRITE_COUNT` reset each attempt (each attempt should re-read for new content and re-write if new findings emerge). This reflects the different semantics: deliver is a completion signal, knowledge is an ongoing accumulation.
- `KnowledgeRetryHook` with `require_write: true` or `require_read: true` can cause continuation loops if the agent consistently ignores the reminder. The `MAX_TURNS` bound (default 3 in `BotAgentNode`) is the safety net. Spec authors should enable enforcement only for nodes where knowledge participation is critical to the workflow's value.
- The changelog is the integrity foundation of the knowledge base — if it is corrupted or missing, the audit trail is lost. The tool writes changelog with `O_APPEND` (atomic on POSIX under asyncio single-thread); concurrent writes from `ParallelScheduler` instances are serialized by the single-thread event loop. Multi-process safety (multiple bot instances) is not addressed in Phase 1 — the SQLite persistence layer handles that for graph state, but knowledge files are plain markdown.
- The capability-aware dynamic schema couples `GraphKnowledgeBaseTool` to the agent's `ToolPreset` at injection time. `BotAgentNode.execute` derives `KnowledgeToolCapabilities` from the pool config's tool preset before constructing the tool. This is a per-`execute()` binding (not cached on the node instance) — the tool is reconstructed every invocation with a fresh `knowledge_dir` resolved from `ctx.graph_instance_id`, because `BotAgentNode` instances are reused across graph instances and a cached tool would route writes to a stale directory. This differs from `GraphDeliverTool` (which caches on `self._deliver_tool`) because deliver depends on topology (stable across runs of the same spec) while knowledge depends on `graph_instance_id` (unique per run). A read-only agent's knowledge tool always rejects `write`/`edit` at both schema (action not in enum) and runtime (execute rejects) levels — defense in depth.

## Phase 2 (Deferred)

The instance-level knowledge blackboard (Phase 1) is the MVP. Cross-instance shared knowledge (the `knowledge/<spec_name>/` directory) is Phase 2 — deferred until Phase 1 validates that agents actually use the knowledge base productively.

### What Phase 2 adds

- **`knowledge/<spec_name>/` directory** — persists across graph instances of the same spec. Contains `project_context.md` (stable project conventions) and `lessons_learned.md` (accumulated lessons from past runs).
- **Read access from instance-level tools** — `GraphKnowledgeBaseTool` gains a `scope` parameter (`instance` / `spec`) or a pattern prefix convention (`knowledge/project_context` → spec-level file). Instance-level patterns remain the default.
- **Lesson extraction (optional)** — on graph `COMPLETED`, an optional post-processing step (similar to `ExperienceReviewAgent`) reads the instance's `findings` + `decisions` and appends reusable lessons to `knowledge/<spec_name>/lessons_learned.md`. This is an LLM-powered extraction, not a mechanical copy — the goal is to distill transferable insights, not duplicate run-specific findings.

### Why deferred (ADR-0007 — two real use cases before promoting a seam)

- Phase 1 has one consumer: the graph workflow. A cross-instance knowledge layer abstracted from one use case is speculative.
- The `Experience` system (`experiences/` + `ExperienceReviewAgent`) already provides cross-conversation learning. Phase 2 knowledge must justify its existence alongside Experience, not duplicate it. The distinction: Experience is general-purpose (all conversations), knowledge is graph-spec-specific (one workflow type).
- Lesson extraction requires LLM calls (cost + latency) and prompt engineering — not justified until Phase 1 proves agents write worthwhile findings.

### Phase 1 → Phase 2 migration path

The directory layout already reserves `knowledge/<spec_name>/`. Phase 2 activation is additive:
1. `WorkspacePaths.graph_spec_knowledge_dir(spec_name)` accessor (already designed in Implementation Reference §5).
2. `GraphKnowledgeBaseTool` gains read access to spec-level patterns (write remains instance-level only — spec-level knowledge is curated, not freely written by any agent).
3. Lesson extraction hook (optional, `AfterGraphHook` or `FinallyGraphHook`).

No Phase 1 code is rewritten — Phase 2 extends.
