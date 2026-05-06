# Framework 组件清理与重组设计

> 日期: 2026-05-06
> 目标: 移除冗余实现、消除目录语义混淆、统一运行时控制平面

---

## 1. 背景与动机

当前框架存在以下问题：

1. **`extensions/` 目录语义不明** — 包含已被完全替代的旧 memory/session 实现，以及被大量使用的 LLM Provider
2. **intervention 与 control 功能重叠** — `multi_agent/intervention.py` 的任务监督功能应合并到 `control/` 统一运行时控制平面
3. **组件位置错误** — skill 过滤、工具过滤、内容清洗等通用组件放在 `multi_agent/` 下
4. **重复实现** — `multi_agent/governance.py` 与 `memory/context_governance.py` 功能高度重叠
5. ** dead code** — 多个 multi_agent 组件无任何生产代码使用

---

## 2. 审计结果

### 2.1 Memory/Session 旧系统已完全死亡

| 组件 | 状态 | 说明 |
|------|------|------|
| `framework/memory/` (新系统) | 活跃 | BotService、Pipeline、Plugin 均使用 |
| `framework/core/memory.py` (`MemoryStore`, `MemoryEntry`) | 死亡 | 零生产使用，仅作为 extensions/memory/* 的基类 |
| `framework/extensions/memory/*` | 死亡 | 零导入，全部基于旧的 `MemoryStore` |
| `framework/core/session.py` (`SessionStore`, `Session`) | 死亡 | 零生产使用 |
| `framework/extensions/session/*` | 死亡 | 零导入，全部基于旧的 `SessionStore` |

`DefaultMemorySystem` 源码声明：*"This is the single concrete memory system. There is no legacy compatibility path."*

### 2.2 Multi_Agent 组件使用情况

| 组件 | 生产使用 | 决策 |
|------|----------|------|
| `intervention.py` | subagent_manager, coordinator | 合并到 control/ |
| `agent_skill_manager.py` | factory | 移到 core/skills/ |
| `filtered_tool_manager.py` | factory, dynamic_tool_filter hook | 移到 tools/ |
| `sanitizer.py` | pipeline | 移到 utils/ |
| `commands.py` | pipeline, agent_session (仅作为协议) | 删除 |
| `context_builder.py` | pipeline, agent_session | 移到 utils/ |
| `deduplicator.py` | pipeline, agent_session | 移到 utils/ |
| `governance.py` | 仅 context_builder (内部) | 删除（与 memory/context_governance.py 重复） |
| `assembly_kit.py` | ❌ 无 | 删除 |
| `toolset.py` | ❌ 无 | 删除 |
| `rpc_broker.py` | ❌ 无 | 删除 |
| `discovery.py` | ❌ 无 | 删除 |
| `peer_validator.py` | builders | 保留 |
| `coordinator.py` | subagent_manager | 保留（解除 intervention 耦合后） |
| `event_bus.py` | coordinator, intervention, hooks | 保留（随 intervention 移入 control/） |
| `router.py` | pipeline | 保留 |
| `pool.py` | core.py | 保留 |
| `subagent_manager.py` | core.py | 保留（改用 control/task_supervision） |
| `factory.py` | core.py | 保留（更新导入） |
| `bus.py` | core.py, factory | 保留 |
| `descriptor.py` | 广泛使用 | 保留 |
| `address.py` | 广泛使用 | 保留 |
| `envelope.py` | bus, tools | 保留 |
| `tools.py` | core.py | 保留 |
| `hooks.py` | core.py | 保留 |
| `inbox/` | core.py | 保留 |
| `registry.py` | pool | 保留 |
| `session_id.py` | pool | 保留 |
| `state.py` | pool | 保留 |
| `governance.py` | ❌ bot_project 和 pipeline 完全不使用 | 删除 |

---

## 3. 目标目录结构

```
framework/
├── core/
│   ├── skills/
│   │   ├── manager.py
│   │   ├── source.py
│   │   ├── builder.py
│   │   ├── filter.py              ← 从 multi_agent/agent_skill_manager.py 移入
│   │   └── models.py
│   ├── agent.py
│   ├── emitter.py
│   ├── tool_manager.py
│   └── ...
│
├── providers/                      ← 新增
│   ├── __init__.py
│   └── litellm_provider.py        ← 从 extensions/llm/ 移入
│
├── control/                        ← 吸收 intervention 功能
│   ├── channel.py
│   ├── runtime.py
│   ├── types.py
│   ├── exceptions.py
│   ├── checkpoint.py
│   ├── store.py
│   ├── preset.py
│   ├── event_bus.py
│   ├── ui/
│   ├── task_supervision.py        ← 新增：从 multi_agent/intervention.py 合并
│   └── policy_registry.py         ← 新增：从 multi_agent/policy_registry.py 合并
│
├── tools/
│   ├── filter.py                  ← 新增：从 multi_agent/filtered_tool_manager.py 移入
│   ├── standard/
│   ├── mcp/
│   └── ...
│
├── utils/
│   ├── sanitizer.py               ← 从 multi_agent/sanitizer.py 移入
│   ├── context_builder.py         ← 从 multi_agent/context_builder.py 移入
│   ├── deduplicator.py            ← 从 multi_agent/deduplicator.py 移入
│   └── ...
│
├── multi_agent/                    ← 清理后仅保留多 Agent 特有组件
│   ├── __init__.py
│   ├── subagent_manager.py         ← 改用 control.task_supervision
│   ├── coordinator.py              ← 解除 TaskInterventionPolicy 耦合
│   ├── factory.py                  ← 改用 core.skills.filter 和 tools.filter
│   ├── pool.py
│   ├── bus.py
│   ├── descriptor.py
│   ├── address.py
│   ├── envelope.py
│   ├── tools.py
│   ├── hooks.py
│   ├── inbox/
│   ├── registry.py
│   ├── router.py
│   ├── peer_validator.py
│   ├── rpc_broker.py               ← 删除
│   ├── session_id.py
│   ├── state.py
│   ├── discovery.py                ← 删除
│   ├── assembly_kit.py             ← 删除
│   ├── toolset.py                  ← 删除
│   ├── governance.py               ← 删除（与 memory/context_governance.py 重复）
│   ├── context_builder.py          ← 删除（移到 utils/）
│   ├── deduplicator.py             ← 删除（移到 utils/）
│   ├── sanitizer.py                ← 删除（移到 utils/）
│   ├── filtered_tool_manager.py    ← 删除（移到 tools/）
│   ├── agent_skill_manager.py      ← 删除（移到 core/skills/）
│   ├── intervention.py             ← 删除（合并到 control/）
│   ├── policy_registry.py          ← 删除（合并到 control/）
│   └── commands.py                 ← 删除（与 control 重复）
│
# extensions/ 目录完全删除
# core/memory.py 删除（旧 MemoryStore ABC）
# core/session.py 删除（旧 SessionStore ABC）
```

---

## 4. 具体变更

### 4.1 删除旧 Memory/Session 系统

**删除文件：**
- `framework/core/memory.py` — `MemoryStore`, `MemoryEntry` 旧 ABC
- `framework/core/session.py` — `SessionStore`, `Session` 旧 ABC
- `framework/extensions/` 整个目录：
  - `extensions/__init__.py`
  - `extensions/llm/__init__.py`
  - `extensions/llm/litellm_provider.py`
  - `extensions/memory/__init__.py`
  - `extensions/memory/chroma.py`
  - `extensions/memory/faiss_store.py`
  - `extensions/memory/archive.py`
  - `extensions/memory/lifecycle.py`
  - `extensions/memory/config.py`
  - `extensions/memory/embedding_config.py`
  - `extensions/session/__init__.py`
  - `extensions/session/memory_store.py`
  - `extensions/session/sqlalchemy_store.py`
  - `extensions/session/sqlite_store.py`

**更新导出：**
- `framework/core/__init__.py` — 移除 `MemoryStore`, `MemoryEntry`, `SessionStore`, `Session` 的导出
- `framework/__init__.py` — 同上

### 4.2 移动 LiteLLM Provider

**新增文件：**
- `framework/providers/__init__.py`
- `framework/providers/litellm_provider.py` — 从 `extensions/llm/litellm_provider.py` 原样移动

**更新导入：**
| 原导入 | 新导入 |
|--------|--------|
| `from framework.extensions.llm.litellm_provider import LiteLLMProvider` | `from framework.providers.litellm_provider import LiteLLMProvider` |
| `from framework.extensions.llm import LiteLLMProvider` | `from framework.providers import LiteLLMProvider` |

**受影响的文件：**
- `examples/bot_project/bot/service/core.py`
- `framework/multi_agent/factory.py`
- `docs/multi-agent-guide.md`
- `tests/unit/extensions/llm/*` → 移到 `tests/unit/providers/`

### 4.3 Intervention → Control 合并

**删除文件：**
- `framework/multi_agent/intervention.py`
- `framework/multi_agent/policy_registry.py`

**新增文件：**
- `framework/control/task_supervision.py`

从 intervention 提取并重命名：
- `TaskInterventionPolicy` → `TaskSupervisionPolicy`
- `InterventionAction` → `SupervisionAction`
- `InterventionResult` → `SupervisionResult`
- `TimeoutCancellationPolicy` → `TimeoutSupervisionPolicy`
- `TaskSupervisor` — 保持名称
- `NoOpInterventionPolicy` → `NoOpSupervisionPolicy`

- `framework/control/policy_registry.py`
  - `PolicyRegistry` → `SupervisionPolicyRegistry`
  - `TaskInterventionPolicySpec` → `SupervisionPolicySpec`

**更新引用：**
- `multi_agent/subagent_manager.py`：
  - `from .intervention import TaskSupervisor, TimeoutCancellationPolicy` → `from framework.control.task_supervision import TaskSupervisor, TimeoutSupervisionPolicy`
  - `TimeoutCancellationPolicy.from_duration(...)` → `TimeoutSupervisionPolicy.from_duration(...)`

- `multi_agent/coordinator.py`：
  - `TaskInterventionPolicy` TYPE_CHECKING 引用 → `TaskSupervisionPolicy`
  - `TaskRecord.policies: list[TaskInterventionPolicy]` → `list[TaskSupervisionPolicy]`

- `multi_agent/__init__.py`：移除 intervention 和 policy_registry 的导出

### 4.4 移动通用组件

| 原位置 | 新位置 | 重命名 |
|--------|--------|--------|
| `multi_agent/agent_skill_manager.py` | `core/skills/filter.py` | `AgentSkillManager` → `SkillWhitelistFilter` |
| `multi_agent/filtered_tool_manager.py` | `tools/filter.py` | 保持 `FilteredToolManager` |
| `multi_agent/sanitizer.py` | `utils/sanitizer.py` | 保持 `ContentSanitizer` |
| `multi_agent/context_builder.py` | `utils/context_builder.py` | 保持 `MultiAgentContextBuilder` |
| `multi_agent/deduplicator.py` | `utils/deduplicator.py` | 保持 `MessageDeduplicator` |

**更新引用：**
- `multi_agent/factory.py`：
  - `from .agent_skill_manager import AgentSkillManager` → `from framework.core.skills.filter import SkillWhitelistFilter`
  - `from .filtered_tool_manager import FilteredToolManager` → `from framework.tools.filter import FilteredToolManager`
  - `AgentSkillManager(...)` → `SkillWhitelistFilter(...)`

- `pipeline/pipeline.py`：
  - `from framework.multi_agent.sanitizer import ContentSanitizer` → `from framework.utils.sanitizer import ContentSanitizer`
  - `from framework.multi_agent.deduplicator import MessageDeduplicator` → `from framework.utils.deduplicator import MessageDeduplicator`
  - `from framework.multi_agent.context_builder import MultiAgentContextBuilder` → `from framework.utils.context_builder import MultiAgentContextBuilder`

- `hook/builtin/dynamic_tool_filter.py`：
  - `from framework.multi_agent.filtered_tool_manager import FilteredToolManager` → `from framework.tools.filter import FilteredToolManager`

- `session/agent_session.py`：
  - `from framework.multi_agent.deduplicator import MessageDeduplicator` → `from framework.utils.deduplicator import MessageDeduplicator`

- `multi_agent/__init__.py`：移除上述组件的导出

### 4.5 删除重复/无用组件

**删除文件：**
- `multi_agent/governance.py` — 功能完全被 `memory/context_governance.py` 覆盖
- `multi_agent/commands.py` — `SystemCommandInterceptor` 与 control 系统重复
- `multi_agent/assembly_kit.py` — 无任何生产使用
- `multi_agent/toolset.py` — 无任何使用
- `multi_agent/rpc_broker.py` — 无任何生产使用
- `multi_agent/discovery.py` — 无任何使用

**关于 `commands.py` 的 `CommandInterceptor` ABC：**
`pipeline.py` 和 `agent_session.py` 中 `command_interceptor` 参数类型为 `Any`，从未实际调用 `CommandInterceptor.handle()` 的抽象方法签名。删除 `commands.py` 后，该参数可保持为 `Any` 或改为 `Callable[[InputMessage], str \| None] \| None`。

---

## 5. 测试变更

### 5.1 删除测试文件

- `tests/unit/extensions/` 整个目录 → 删除
- `tests/unit/multi_agent/test_assembly_kit.py` → 删除
- `tests/unit/multi_agent/test_core_runtime.py` 中 intervention/policy_registry 相关测试 → 移到 `tests/unit/control/test_task_supervision.py`
- `tests/integration/multi_agent/test_multi_agent_communication_and_intervention.py` → 更新导入或删除 intervention 相关测试
- `tests/unit/multi_agent/test_governance_security.py` 中 governance/CommandsInterceptor/Sanitizer 测试 → 拆分并移到对应新位置

### 5.2 新增/更新测试文件

- `tests/unit/providers/` — 从 `tests/unit/extensions/llm/` 移入并更新导入
- `tests/unit/control/test_task_supervision.py` — 新增
- `tests/unit/core/skills/test_filter.py` — 新增（原 agent_skill_manager 测试）
- `tests/unit/tools/test_filter.py` — 新增（原 filtered_tool_manager 测试）
- `tests/unit/utils/test_sanitizer.py` — 从 `test_governance_security.py` 拆分
- `tests/unit/utils/test_context_builder.py` — 新增
- `tests/unit/utils/test_deduplicator.py` — 新增

---

## 6. 文档变更

- `CLAUDE.md` — 更新目录结构描述，移除 `extensions/` 条目
- `docs/architecture.md` — 更新架构图和组件列表
- `docs/multi-agent-guide.md` — 更新导入示例
- `framework/*/AGENTS.md` — 更新各模块的 AGENTS.md

---

## 7. 风险与注意事项

1. **循环导入风险** — `multi_agent/factory.py` 从 `extensions.llm` 导入改为从 `providers` 导入，需确认无循环依赖
2. **测试覆盖** — 大量测试文件需要更新导入路径，建议使用搜索替换批量处理
3. **Plugin 系统** — `extensions/memory/*` 虽未被核心代码使用，但需确认无 plugin 通过动态导入使用它们（审计显示插件集成走的是新 memory 系统）
4. **Type Checking** — `coordinator.py` 中的 `TYPE_CHECKING` 引用需要同步更新
5. **__init__.py 导出** — `multi_agent/__init__.py` 和 `framework/__init__.py` 的 `__all__` 列表需要仔细清理
