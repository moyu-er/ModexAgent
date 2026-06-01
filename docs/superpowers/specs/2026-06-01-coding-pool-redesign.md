# Coding Pool Redesign — Pi-Aligned Multi-Agent Architecture

> **Date**: 2026-06-01
> **Status**: Approved
> **Scope**: framework layer (primary) + bot_project coding pool (secondary)

---

## 1. Goal

Redesign the bot_project coding pool from 3 roles (coding + planner + reviewer) to a pi-aligned 6-role subagent architecture, enabling structured coding workflows: scout → context-builder → planner → worker → reviewer, with delegate as a catch-all.

## 2. Architecture

### 2.1 Role Topology

```
                    coding (main agent)
                    orchestrator + direct execution
                   /    |    |    |     \
                  /     |    |    |      \
           scout  ctx-  planner reviewer worker
                  builder                + delegate
```

### 2.2 Role Definitions

| Role | Tool Preset | Context | Thinking | Output | Description |
|------|-------------|---------|----------|--------|-------------|
| scout | read_only | fresh | low | context.md | Fast codebase reconnaissance |
| context-builder | read_only | fresh | medium | context.md + meta-prompt.md | Deep requirements analysis |
| planner | minimal | fork | high | plan.md | Implementation planning (no code changes) |
| worker | full | fork | high | code changes | Single writer thread, minimal correct changes |
| reviewer | read_write | fresh | high | review report | 5 review types (diff/plan/solution/health/PR) |
| delegate | full | fresh | — | task result | Lightweight generic executor |

### 2.3 Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Config format | Layered: template YAML + prompt .md | Reuses existing infrastructure, minimal disruption |
| Tool permission | Preset tool sets | Simple enum, maps cleanly to pi's tool lists |
| AST tools | tree-sitter Python + Java | Pragmatic first-language support |
| LSP tools | Stub only | Placeholder for future implementation |
| Communication | Reuse send_to_agent | No new comm tools; pi semantics via prompt |
| Context mode | Mixed fresh+fork | Fork for worker/planner, fresh for others |

---

## 3. Framework Layer Changes

### 3.1 ToolPreset Enum + Preset Tool Sets

**New file**: `framework/tools/presets.py`

```python
class ToolPreset(str, Enum):
    FULL = "full"             # all tools + bash + terminal
    READ_WRITE = "read_write"  # read + write + edit + search (no bash/terminal)
    READ_ONLY = "read_only"    # read + search + bash (prompt-constrained read-only)
    MINIMAL = "minimal"        # read + write + list + search (no edit/bash)
```

Tool matrix per preset:

| Preset | ReadFile | WriteFile | EditFile | ListDir | SearchFiles | FindFiles | Bash | Terminal |
|--------|----------|-----------|----------|---------|-------------|-----------|------|----------|
| full | Y | Y | Y | Y | Y | Y | Y | Y |
| read_write | Y | Y | Y | Y | Y | Y | N | N |
| read_only | Y | N | N | Y | Y | Y | Y(sub) | N |
| minimal | Y | Y | N | Y | Y | N | N | N |

AST tools (`ast_grep_search`, `ast_grep_replace`) are separate from presets. They register only for the main coding agent via `extra_tools` config.

### 3.2 Bash Tool Unification

**Modified files**: `SubprocessTool`, `CommandTool`

Both tools register with name `"bash"`. Descriptions clearly distinguish behavioral semantics without exposing implementation details:

- `CommandTool` (terminal-backed): "Execute a command in a **persistent shell session**. Working directory, environment variables, and background processes persist between calls."
- `SubprocessTool` (subprocess-backed): "Execute a shell command. Each invocation runs independently in a fresh shell."

Runtime registration: only one `"bash"` is registered — `CommandTool` if terminal_manager exists, else `SubprocessTool`. In `read_only` preset, bash always uses `SubprocessTool` (no terminal overhead).

### 3.3 AgentTemplate Extension

**Modified file**: `framework/multi_agent/template.py`

Add 5 fields to `AgentTemplate` dataclass:

```python
@dataclass
class AgentTemplate:
    # existing fields unchanged
    agent_type: str
    description: str = ""
    max_steps: int = 20
    standard_tools: bool = True
    use_terminal: bool = True
    terminal_visibility: bool = True
    memory: MemoryConfig | None = None
    skills: SkillsConfig | None = None

    # new pi-aligned fields
    tool_preset: ToolPreset = ToolPreset.FULL
    context_mode: str = "fresh"        # "fresh" | "fork"
    thinking_budget: str = "medium"    # "low" | "medium" | "high" — prompt annotation only
    default_reads: list[str] = field(default_factory=list)
    progress_tracking: bool = False
    visible_targets: list[str] | None = None  # None=all NORMAL agents visible; list=restrict
```

Backward compatibility: `standard_tools=true` without `tool_preset` falls back to legacy behavior. `tool_preset` present takes precedence.

### 3.4 Template Registry Update

**Modified file**: `framework/multi_agent/template_registry.py`

Parse new YAML fields: `tool_preset`, `context_mode`, `thinking_budget`, `default_reads`, `progress_tracking`. Validate `tool_preset` against enum. Ignore unknown fields gracefully.

### 3.5 Communication Service Update

**Modified file**: `framework/multi_agent/communication.py`

Update `_build_subagent_tool_manager` to accept template and register tools based on `template.tool_preset`:

1. Look up preset from `TOOL_PRESETS` registry
2. Register preset tools
3. Register bash (SubprocessTool or CommandTool based on terminal availability and preset)
4. Always append `SendToAgentTool` + `ListCommunicationTargetsTool` (communication tools)
5. When `context_mode="fork"`, inject fork preamble into system prompt: "You are a subagent running from a fork of the parent session. Treat inherited conversation as reference-only context, not a live thread to continue. Your sole job is to execute the assigned task."
6. **New**: When `context_mode="fork"`, deep-copy the parent agent's conversation history into the subagent's memory (see 3.5a below)

### 3.5a Fork Context — Memory Deep-Copy

**Why**: `context_mode="fork"` must give the subagent a deep copy of the parent's conversation history, not just a preamble. Parent and subagent must have independent memory — they cannot share the same storage. The subagent treats inherited history as read-only reference.

**Implementation**:

1. `AgentCommunicationService.__init__` receives a new `parent_memory_system` parameter (the main agent's `MemorySystem`).
2. In `_create_dynamic_subagent`, when `template.context_mode == "fork"`:
   a. Build a `MemoryContext` for the parent's session (using parent's `conversation_id` + main agent name).
   b. Call `parent_memory_system._layers.session.get_all_messages(parent_context)` to retrieve all messages.
   c. Convert messages to `ChatMessage` list (deep copy — each message is a new dict/dataclass).
   d. Pass `initial_messages=parent_messages` into the subagent's `create_message_history()`.
3. The subagent's `RestrictedInjectionPolicy` will inject these messages as context on load.
4. Parent and subagent memory systems remain completely independent — the subagent writes to its own session scope.

**Key constraint**: Subagent must NOT mutate the parent's messages. The deep copy ensures isolation.

### 3.5b Dynamic Communication Targets

**Why**: Subagents should only see communication targets relevant to their task context, not all NORMAL agents in the pool. The list must be configurable at creation time.

**Implementation**:

1. `ListCommunicationTargetsTool.__init__` accepts an optional `visible_targets: list[str] | None` parameter.
2. When `visible_targets` is set, `execute()` only returns agents whose names are in the list (in addition to existing comm_kind filtering).
3. When `visible_targets` is None (default), behavior is unchanged (all NORMAL agents visible).
4. `AgentCommunicationService._create_dynamic_subagent` passes `visible_targets=["coding"]` (or the main agent name) when creating subagent tools — subagents only see their parent.
5. `AgentTemplate` gains an optional `visible_targets: list[str] | None` field to allow YAML override per template type.

### 3.6 AgentConfig Extension

**Modified file**: `framework/ioc/configs/agent.py`

Add `extra_tools` field for main agent to declare additional tools beyond presets:

```python
class AgentConfig(BaseModel):
    # ... existing fields ...
    extra_tools: list[str] = Field(default_factory=list)
    # e.g. ["ast_grep_search", "ast_grep_replace", "lsp_diagnostics", "lsp_navigation"]
```

`pool_builder` reads this and registers the named tools into the main agent's tool_manager.

### 3.7 AST Tools (tree-sitter)

**New files**: `framework/tools/ast/`

```
framework/tools/ast/
├── __init__.py
├── engine.py           # tree-sitter pattern matching engine
├── ast_search.py       # ast_grep_search tool
└── ast_replace.py      # ast_grep_replace tool
```

**ast_grep_search**:

| Parameter | Type | Description |
|-----------|------|-------------|
| `pattern` | string | AST pattern with `$NAME` (single node) and `$$$ARGS` (multi node) |
| `language` | string | `"python"` or `"java"` |
| `path` | string? | File or directory, default CWD |

Output format (plain text):
```
framework/core/agent.py:45:async def run(self, context)
framework/multi_agent/pool.py:120:async def register_resident(self, descriptor)
Found 2 matches.
```

**ast_grep_replace**:

| Parameter | Type | Description |
|-----------|------|-------------|
| `pattern` | string | AST pattern to match |
| `replacement` | string | Replacement template, references `$VAR` |
| `language` | string | `"python"` or `"java"` |
| `path` | string | Target file (required) |
| `dry_run` | bool? | Default true |

Output:
```
--- framework/core/agent.py
- async def run(self, context)
+ async def run(self, ctx: AgentContext)
1 replacement (dry run). Set dry_run=false to apply.
```

**Languages**: Python (`tree_sitter_python`) + Java (`tree_sitter_java`).

**Graceful degradation**: when `tree_sitter` not installed, `execute()` returns: "AST tools require `pip install ModexAgent[ast]`"

**Dependency**: `pyproject.toml` optional group `ast = ["tree-sitter>=0.24", "tree-sitter-python>=0.23", "tree-sitter-java>=0.23"]`

### 3.8 LSP Tool Stubs

**New files**: `framework/tools/lsp/__init__.py`, `framework/tools/lsp/lsp_diagnostics.py`, `framework/tools/lsp/lsp_navigation.py`

`__init__.py` exports both tools. Each stub is ~30 lines, registered by name, returns "not yet implemented" message. Not included in any tool preset — only registered via `extra_tools` on the main agent.

### 3.9 Hook Adaptations

**Modified file**: `framework/hook/builtin/subagent_auto_send.py`

`SubagentAutoSendHook` forwarding includes `agent_type` metadata in the envelope, so the receiving agent can distinguish which role sent the message.

**Modified file**: `framework/hook/notification.py`

`MaxIterationNotifyHook` notification message includes agent_type/role information:
```
"Subagent 'worker' reached max iterations (150) — task may be incomplete"
```

---

## 4. Bot Layer Changes

### 4.1 Template YAML Files (6 new)

Located in `config/pools/coding/templates/`:

| File | max_steps | tool_preset | context_mode | thinking |
|------|-----------|-------------|-------------|----------|
| scout.yml | 40 | read_only | fresh | low |
| context-builder.yml | 60 | read_only | fresh | medium |
| planner.yml | 80 | minimal | fork | high |
| worker.yml | 150 | full | fork | high |
| reviewer.yml | 100 | read_write | fresh | high |
| delegate.yml | 50 | full | fresh | — |

All subagent memory configs use `archive: {enabled: true}` (session-scoped archival only), no knowledge/dream-engine. Short-term limits scale with role needs.

### 4.2 Agent Prompt Files

| File | Action | Source |
|------|--------|--------|
| agents/scout.md | New | pi scout.md body, Chinese, send_to_agent comms |
| agents/context-builder.md | New | pi context-builder.md body, Chinese, send_to_agent comms |
| agents/worker.md | New | pi worker.md body, Chinese, send_to_agent comms |
| agents/delegate.md | New | pi delegate.md body, Chinese, send_to_agent comms |
| agents/planner.md | Replace | pi planner.md body, Chinese, send_to_agent comms |
| agents/reviewer.md | Replace | pi reviewer.md body, Chinese, send_to_agent comms |

Communication rules in each prompt translate pi's `contact_supervisor`/`intercom` to `send_to_agent` semantics:
- Need decision → `send_to_agent(target_agent="coding", content="NEED_DECISION: ...", invocation_id=<current>)`
- Complete task → `send_to_agent(target_agent="coding", content="<results>", invocation_id=null)`
- No routine completion handoffs — return normally when done

### 4.3 Main Agent Updates

**agents/coding.md** — update Available Subagents section to list all 6 roles with usage guidance.

**config/pools/coding.yml** — add `extra_tools` to main agent config for AST + LSP tool registration.

### 4.4 Cleanup

Delete old `config/pools/coding/templates/planner.yml` and `reviewer.yml` (replaced by new versions).

---

## 5. Complete File Change List

| # | Layer | File | Type |
|---|-------|------|------|
| 1 | framework | `framework/tools/presets.py` | New |
| 2 | framework | `framework/tools/ast/__init__.py` | New |
| 3 | framework | `framework/tools/ast/engine.py` | New |
| 4 | framework | `framework/tools/ast/ast_search.py` | New |
| 5 | framework | `framework/tools/ast/ast_replace.py` | New |
| 6 | framework | `framework/tools/lsp/__init__.py` | New |
| 7 | framework | `framework/tools/lsp/lsp_diagnostics.py` | New |
| 8 | framework | `framework/tools/lsp/lsp_navigation.py` | New |
| 9 | framework | `framework/multi_agent/template.py` | Modify |
| 10 | framework | `framework/multi_agent/template_registry.py` | Modify |
| 11 | framework | `framework/multi_agent/communication.py` | Modify |
| 12 | framework | `framework/multi_agent/tools.py` | Modify |
| 13 | framework | `framework/ioc/configs/agent.py` | Modify |
| 14 | framework | `framework/tools/terminal/subprocess_tool.py` | Modify |
| 15 | framework | `framework/tools/terminal/command_tool.py` | Modify |
| 16 | framework | `framework/hook/builtin/subagent_auto_send.py` | Modify |
| 17 | framework | `framework/hook/notification.py` | Modify |
| 18 | framework | `pyproject.toml` | Modify (add ast optional dep) |
| 19 | bot | `config/pools/coding/templates/scout.yml` | New |
| 20 | bot | `config/pools/coding/templates/context-builder.yml` | New |
| 21 | bot | `config/pools/coding/templates/worker.yml` | New |
| 22 | bot | `config/pools/coding/templates/delegate.yml` | New |
| 23 | bot | `config/pools/coding/templates/planner.yml` | Replace |
| 24 | bot | `config/pools/coding/templates/reviewer.yml` | Replace |
| 25 | bot | `agents/scout.md` | New |
| 26 | bot | `agents/context-builder.md` | New |
| 27 | bot | `agents/worker.md` | New |
| 28 | bot | `agents/delegate.md` | New |
| 29 | bot | `agents/planner.md` | Replace |
| 30 | bot | `agents/reviewer.md` | Replace |
| 31 | bot | `agents/coding.md` | Modify |
| 32 | bot | `config/pools/coding.yml` | Modify |

---

## 6. Implementation Order

Follow "framework first, then bot" strategy:

### Phase 1: Framework foundations
1. ToolPreset enum + preset tool sets (`presets.py`)
2. Bash tool unification (SubprocessTool + CommandTool name change)
3. AgentTemplate extension (5 new fields)
4. Template registry update (parse new fields)
5. AgentConfig extra_tools field

### Phase 2: Framework tool infrastructure
6. AST tools (engine + search + replace)
7. LSP tool stubs

### Phase 3: Framework wiring
8. Communication service update (preset-based tool registration)
9. Pool builder update (extra_tools registration)
10. Hook adaptations (SubagentAutoSendHook + MaxIterationNotifyHook)

### Phase 4: Bot configuration
11. Delete old planner/reviewer templates
12. Create 6 new template YAML files
13. Create/replace 6 agent prompt .md files
14. Update coding.md and coding.yml

### Phase 5: Verification
15. Unit tests for ToolPreset, AgentTemplate, AST engine
16. Integration test: coding pool with dynamic subagent creation
17. Manual verification: bot startup + subagent dispatch
