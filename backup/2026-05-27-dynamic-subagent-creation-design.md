# Dynamic Subagent Creation & Unified Communication

**Date**: 2026-05-27
**Status**: draft

## 1. Goals

- LLM can dynamically create subagents at runtime by selecting from predefined templates
- One unified tool (`send_to_agent`) replaces create + communicate
- XML-structured messages replace `[From Agent xxx]` text prefix
- invocation_id isolates concurrent subagent calls and enables session resumption
- Clean migration: delete old mechanisms, no backward compatibility

## 2. Agent Template System

### 2.1 File layout

```
config/pools/{pool_name}/templates/{agent_type}.yml   # template definition
agents/{pool_name}/{agent_type}.md                     # system prompt (same convention as main)
```

### 2.2 Template YAML schema

Fields align with existing `AgentConfig` (framework/ioc/configs/agent.py). `name`, `role`, `llm` are omitted — they are not template properties.

```yaml
# config/pools/main/templates/office-expert.yml
agent_type: office-expert
description: "处理 Office 文档（Word/Excel/PPT/PDF），支持格式转换、内容提取、编辑"
max_steps: 30
standard_tools: true
use_terminal: false
mcp_filter: []
memory:
  short_term: {max_messages: 80, max_tokens: 30000, keep_ratio_for_messages: 0.5, keep_ratio_for_token: 0.5}
  governance: {}
skills:
  roots: ["skills/main/office-expert"]
```

```yaml
# config/pools/main/templates/query-12306.yml
agent_type: query-12306
description: "查询 12306 火车票，余票、车次时刻表"
max_steps: 50
standard_tools: false
use_terminal: false
mcp_filter: ["12306-mcp"]
memory:
  short_term: {max_messages: 80, max_tokens: 50000}
```

Communication tools (`send_to_agent` + `list_communication_targets`) are **mandatory and auto-injected by the framework** — they must not appear in template tool configuration.

### 2.3 AgentTemplate data model

```python
@dataclass
class AgentTemplate:
    agent_type: str
    description: str
    max_steps: int = 20
    standard_tools: bool = True
    use_terminal: bool = True
    mcp_filter: list[str] | None = None
    memory: MemoryConfig | None = None
    skills: SkillsConfig | None = None
```

### 2.4 AgentTemplateRegistry

```python
class AgentTemplateRegistry:
    def __init__(self, project_dir: Path):
        # Scans config/pools/*/templates/*.yml
        # Isolated by pool_name

    def list_templates(self, pool_name: str) -> list[AgentTemplate]
    def get_template(self, pool_name: str, agent_type: str) -> AgentTemplate
```

### 2.5 System prompt resolution

```
agents/{pool_name}/{agent_type}.md  →  read as system_prompt_template
```

Same convention as `resolve_system_prompt()` in `builders.py`: `.md` file wins, falls back to template YAML (if present), else empty.

## 3. Unified `send_to_agent` Tool

### 3.1 LLM-facing parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `target_agent` | string | yes | Template type name (e.g. "office-expert") or existing agent name |
| `content` | string | yes | Message or task content |
| `invocation_id` | string\|null | yes | `null`/`""` = create new subagent or send to normal; `"<id>"` = continue existing subagent session |

### 3.2 Description (dynamic part appended by framework)

```
Send a message to another agent asynchronously.
The agent processes the message and results arrive via inbox — this tool does NOT return the actual result directly.
Call list_communication_targets FIRST to see available targets and their invocation_id requirements.
```

### 3.3 Internal routing

```
send_to_agent(target_agent, content, invocation_id)
  │
  │  1. Resolve target_agent identity:
  │     - Registered agent name in AgentPool → use its descriptor.comm_kind
  │     - Matches template type name → comm_kind = SUBAGENT
  │     - Neither → error
  │
  ├─ comm_kind == SUBAGENT
  │   ├─ invocation_id is empty (null / "" / whitespace)
  │   │   → Create new instance from template
  │   │   → name = "{type}.{uuid8}", invocation_id = uuid8
  │   │   → AgentPool.register_resident(descriptor, memory_ctx)
  │   │   → Send initial task via broker
  │   │   → Return {agent_name, invocation_id, status: "launched"}
  │   │
  │   └─ invocation_id has value
  │       → Route to session "{conv}:{target}:{invocation_id}"
  │       → broker.send_to(target, envelope)
  │       → If session expired: pool re-creates pipeline (memory inherits context)
  │       → Return {status: "sent", agent_name, invocation_id}
  │
  └─ comm_kind == NORMAL
      → Discard invocation_id (ignore if provided)
      → broker.send_to(target, envelope)
      → Normal is always resident; consumer loop handles it
      → Return {status: "sent"}
```

### 3.4 Dynamic creation flow

```
1. AgentTemplateRegistry.get_template(pool_name, agent_type)
2. Generate name = "{agent_type}.{uuid8}", invocation_id = uuid8
3. Load system prompt from agents/{pool_name}/{agent_type}.md
4. Build AgentDescriptor:
     address = AgentAddress(name=name, comm_kind=SUBAGENT)
     system_prompt_template = md content
     allowed_tools = standard_tools + communication tools
     context_strategy = "persistent"
     max_iterations = template.max_steps
5. Create MemorySystemContext with invocation_id-scoped session
6. AgentPool.register_resident(descriptor, memory_ctx, tool_manager, ...)
7. Send initial task via broker
8. Return result to LLM
```

### 3.5 Resumption behavior

When `send_to_agent` targets an existing subagent with a known `invocation_id`:

- Session `{conv}:{xxx}:{invocation_id}` is routed via broker
- If the session's pipeline is still active → message delivered immediately
- If the session expired / pipeline cleaned up → pool detects, re-creates pipeline from memory checkpoint
- The subagent inherits prior conversation context (memory is scoped to that exact session ID)
- No structural difference between "first message" and "resumed message" from the pipeline's perspective

## 4. XML Communication Format

### 4.1 Agent-to-agent message (`<agent_message>`)

Generated when LLM actively calls `send_to_agent`. Framework fills `source` and `invocation_id` from `AgentContext.session_meta`; LLM only provides `content`.

```xml
<agent_message source="office-expert" invocation_id="a1b2c3d4">
  <content>PDF 转换完成，共 12 页，已保存为 result.docx。</content>
</agent_message>
```

- `source`: auto-filled from `session_meta.agent_name`
- `invocation_id`: auto-filled from `session_meta.invocation_id` (omitted for NORMAL agent)
- `content`: verbatim from LLM's `content` parameter

### 4.2 Hook-generated turn result (`<agent_result>`)

Generated by `SubagentAutoSendHook` or `MaxIterationNotifyHook` when the subagent's turn ends without the LLM calling `send_to_agent`.

```xml
<!-- Hook caught: LLM finished but forgot to call send_to_agent -->
<agent_result source="office-expert" invocation_id="a1b2c3d4" status="completed">
  <stop_reason>missed_communication</stop_reason>
  <content>任务完成。PDF 已转为 Word，文件路径：/output/result.docx</content>
</agent_result>

<!-- Hook caught: max_iterations reached -->
<agent_result source="office-expert" invocation_id="a1b2c3d4" status="max_iterations">
  <stop_reason>max_iterations</stop_reason>
  <content>正在处理第 5 页...</content>
</agent_result>
```

### 4.3 Status / stop_reason semantics

| Scenario | XML root | status | stop_reason |
|----------|---------|--------|-------------|
| LLM called `send_to_agent` | `<agent_message>` | — | — |
| SubagentAutoSendHook: LLM didn't call comm tool | `<agent_result>` | `completed` | `missed_communication` |
| MaxIterationNotifyHook: hit max_iterations | `<agent_result>` | `max_iterations` | `max_iterations` |

### 4.4 Framework module

New module `framework/multi_agent/message_xml.py`:

```python
def build_agent_message(*, source: str, invocation_id: str | None, content: str) -> str
def build_agent_result(*, source: str, invocation_id: str | None, status: str, stop_reason: str, content: str) -> str
```

Both return the full XML string. Callers do not construct XML manually.

## 5. Hook Changes

### 5.1 SubagentAutoSendHook

Current: forwards `result.content` as raw text with `[From Agent xxx]` prefix.

New:
- If RuntimeContext shows `send_to_agent` was called this turn → skip (LLM handled it)
- Otherwise → wrap content via `build_agent_result(status="completed", stop_reason="missed_communication")`, deliver via agent_bus

### 5.2 MaxIterationNotifyHook

Current: builds old `<agent_notification>` XML.

New: wrap content via `build_agent_result(status="max_iterations", stop_reason="max_iterations")`.

### 5.3 InboxFlushHook

Current: prefixes messages with `[From Agent xxx]\n`.

New: messages are already self-describing XML (`<agent_message>` or `<agent_result>`). Strip `ensure_agent_source_prefix` call. Keep `meta_inbox: true` for compaction policy.

### 5.4 Reply target: dynamic, not hardcoded

Current `SubagentAutoSendHook.__init__(parent_name="main")` and `AgentNotificationService.__init__(parent_map=...)` hardcode reply targets. With dynamic subagents, the reply target is the agent that sent the task — encoded in the session's `conversation_id`.

The hook and notification service derive the reply target from `AgentContext.session_meta`:

- `session_meta.conversation_id` → the common conversation scope
- Parse the session_id `{conv}:{agent}:{invocation_id}` → the `conv` prefix identifies which agent owns this conversation (the one that initiated the task)

This removes the need for a static `parent_name` constructor parameter on both hooks.

### 5.5 AgentNotificationService

Current `_build_xml()` produces `<agent_notification>` format — replaced by the two new XML formats. The service's routing logic (NORMAL→output_adapter, SUBAGENT→agent_bus) is preserved. The `parent_map` dict is removed — reply target is derived from `session_meta`.

## 6. Deletions

| Item | Location | Reason |
|------|----------|--------|
| `SendToAgentTool` (sync) | `framework/multi_agent/tools.py` | Never used; unified into async `send_to_agent` |
| `agent_source_prefix()` + `ensure_agent_source_prefix()` | `framework/core/message_utils.py` | Replaced by XML |
| `<agent_notification>` XML format | `framework/hook/notification.py:_build_xml()` | Replaced by `<agent_message>` / `<agent_result>` |
| `PoolConfig.subagent_configs` property | `framework/ioc/configs/pool.py` | Template system replaces YAML-declared subagents |

## 7. Renames

| Old | New | Location |
|-----|-----|----------|
| `send_to_agent_async` | `send_to_agent` | `framework/multi_agent/tools.py` |
| `SendToAgentAsyncTool` | `SendToAgentTool` | `framework/multi_agent/tools.py` |

## 8. Existing Subagent Migration

4 subagents across 2 pools migrate to template YAML + separate `.md` system prompt files:

| Pool | Old subagent name | Template YAML | System prompt MD |
|------|-------------------|---------------|------------------|
| main | office-expert | `config/pools/main/templates/office-expert.yml` | `agents/main/office-expert.md` |
| main | query-12306 | `config/pools/main/templates/query-12306.yml` | `agents/main/query-12306.md` |
| coding | reviewer | `config/pools/coding/templates/reviewer.yml` | `agents/coding/reviewer.md` (exists) |
| coding | planner | `config/pools/coding/templates/planner.yml` | `agents/coding/planner.md` (exists) |

Inline system prompts in `main.yml` (office-expert, query-12306) are extracted to `.md` files. `coding.yml` subagents already have `.md` files. `PoolConfig.agents` drops all `role: subagent` entries.

## 9. AgentPool Changes

### 9.1 Remove subagent_configs from PoolConfig

`PoolConfig.agents` will only contain agents with `role: main`. Subagent definitions live in templates.

### 9.2 Template loading in pool_builder

`create_pool()` switches from iterating `pool_cfg.subagent_configs` to loading from `AgentTemplateRegistry`:

```python
templates = template_registry.list_templates(pool_name)
for template in templates:
    # Build descriptor from template (without registering)
    # Registration happens dynamically when send_to_agent first creates an instance
```

Templates are loaded for `list_communication_targets` to list available types. Actual `AgentPool.register_resident()` only happens at runtime when LLM calls `send_to_agent` with a template type name.

### 9.3 list_communication_targets extension

The discovery tool now shows template types alongside existing agents:

```
Available targets:
  - main (normal)
  - office-expert (subagent)
  - [template] office-expert (subagent)    ← can be created
  - [template] query-12306 (subagent)      ← can be created
```

Template entries show `invocation_id: "" (creates new instance)` guidance.

## 10. Key Design Decisions

1. **Template-driven, not free-form**: LLM selects from predefined templates; cannot invent arbitrary agent types or tool sets at runtime. This is the Claude Code model.

2. **Communication tools are non-configurable**: `send_to_agent` + `list_communication_targets` are always present on every agent. Templates cannot remove them.

3. **invocation_id is framework-owned**: Subagents never see or construct their own invocation_id. The framework fills it from `AgentContext.session_meta` when sending. The subagent's system prompt instructs it to use `send_to_agent` naturally; the tool internally attaches the correct invocation_id.

4. **Same pipeline path for all agents**: NORMAL and SUBAGENT both process messages through the same `AgentPipeline.process_message()` path, including slash command handling. The only difference is how they are constructed (static vs template-driven dynamic).

5. **XML is framework-assembled**: `send_to_agent` tool wraps LLM's `content` in `<agent_message>` XML internally. Hooks wrap turn results in `<agent_result>` XML. The LLM never sees or writes XML tags directly — it just provides the `content` string.

## 11. Implementation Scope Summary

### New files
- `framework/multi_agent/template.py` — `AgentTemplate` dataclass
- `framework/multi_agent/template_registry.py` — `AgentTemplateRegistry`
- `framework/multi_agent/message_xml.py` — `build_agent_message()`, `build_agent_result()`
- `examples/bot_project/config/pools/main/templates/office-expert.yml`
- `examples/bot_project/config/pools/main/templates/query-12306.yml`
- `examples/bot_project/config/pools/coding/templates/reviewer.yml`
- `examples/bot_project/config/pools/coding/templates/planner.yml`
- `examples/bot_project/agents/main/office-expert.md`
- `examples/bot_project/agents/main/query-12306.md`

### Modified files
- `framework/multi_agent/tools.py` — delete `SendToAgentTool`, rename `SendToAgentAsyncTool`→`SendToAgentTool`, rename `send_to_agent_async`→`send_to_agent`, add template-aware routing
- `framework/multi_agent/communication.py` — add template lookup + dynamic creation in `_send()`
- `framework/multi_agent/__init__.py` — drop `SendToAgentTool` export, update `SendToAgentAsyncTool`→`SendToAgentTool`
- `framework/hook/builtin/inbox_flush.py` — remove `ensure_agent_source_prefix` call
- `framework/hook/builtin/subagent_auto_send.py` — XML output via `build_agent_result`; reply target derived from `session_meta`, not hardcoded `parent_name`
- `framework/hook/notification.py` — `MaxIterationNotifyHook` uses `build_agent_result`; `AgentNotificationService._build_xml()` replaced; `parent_map` removed
- `framework/core/message_utils.py` — remove `agent_source_prefix()` and `ensure_agent_source_prefix()`
- `framework/ioc/configs/pool.py` — remove `subagent_configs` property
- `examples/bot_project/bot/service/pool_builder.py` — switch from `subagent_configs` to `AgentTemplateRegistry`
- `examples/bot_project/config/pools/main.yml` — remove `office-expert` and `query-12306` subagent entries
- `examples/bot_project/config/pools/coding.yml` — remove `reviewer` and `planner` subagent entries

### Deleted code
- `SendToAgentTool` class (sync version in `tools.py`)
- `agent_source_prefix()` function (`message_utils.py`)
- `ensure_agent_source_prefix()` function (`message_utils.py`)
- `<agent_notification>` XML format (`notification.py`)
