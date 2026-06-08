# ModexAgent 冗余代码清理 — 执行路线图

> 生成日期: 2026-06-08
> 依据: `issue/redundant_full_analysis.md` 全量分析报告
> 当前分支: `cleanup/redundant-code`
> 原则: UT / 文档 / 注释 / `__init__.py` 导出全部同步更新，不留孤儿引用

---

## 〇、前置准备（Pre-flight）

### 0.1 创建备份分支（不切换）

```bash
# 在当前分支上打 tag，记录清理前的状态
git tag backup/pre-cleanup-$(date +%Y%m%d)

# 创建备份分支（不切换，仅作保险）
git branch backup/redundant-code-pre-cleanup
```

### 0.2 基线验证

```bash
# 确保当前测试全部通过
pytest tests/unit/ -v --tb=short 2>&1 | tee issue/baseline_tests.log

# 记录当前测试数量
pytest tests/unit/ --co -q | wc -l > issue/baseline_test_count.txt
```

### 0.3 确认清理范围

| 类别 | 数量 | 风险等级 |
|------|------|---------|
| D 系列（死代码） | 30 | 🟢 零风险 |
| T 系列（仅测试） | 20 类+方法 | 🟢 低风险 |
| F 系列（死字段） | 14 | 🟢 低风险 |
| E 系列（仅 Example） | 3 | 🟡 需确认 |
| R 系列（冗余设计） | 10 | 🟡 需确认 |
| Q 系列（需讨论） | 9 | 🟡 需确认 |

---

## 一、迁移规划（先于删除执行）

> **原则**: 删除前先规划"需要搬迁的组件"。删除不是目的，清理冗余、理清关系才是。

### 1.1 AgentSession 删除 → 工厂分支迁移

**当前**: `factory.py` 有 `mode="session"` 分支创建 `AgentSession`，但零生产调用。
**迁移**: 删除 `mode="session"` 分支，如工厂仅剩 `mode="pipeline"` 路径，则删除 `mode` 参数。
**影响文件**:
- `framework/multi_agent/factory.py` — 删除 `AgentSession(...)` 创建代码、删除 `mode` 参数
- `framework/multi_agent/descriptor.py` — 删除 `session: AgentSession | None` 字段

### 1.2 SubagentService 删除 → bot_project 引用清理

**当前**: bot_project 的 `builders.py` 和 `core.py` import SubagentService 但从不实例化。
**迁移**: 删除 import 行，无需功能替代。
**影响文件**:
- `examples/bot_project/bot/service/builders.py` — 删除 import
- `examples/bot_project/bot/service/core.py` — 删除 import

### 1.3 `_dream_locks` 提取 → 独立模块

**当前**: `agent_session.py:33` 定义，`pipeline.py:51` import。
**迁移**:
1. 创建 `framework/runtime/dream_locks.py`
2. 迁移 `_dream_locks` 定义
3. 修改 `pipeline.py` import 路径
4. 删除 `agent_session.py`（包含在 Phase 1 整文件删除中）
**影响文件**:
- `framework/pipeline/pipeline.py` — 修改 import 来源

### 1.4 CLI/HTTP OutputAdapter + InMemoryStoreRegistry 降级

**当前**: 有测试覆盖但零生产使用。
**迁移**: 保留文件，从公开 `__init__.py` 移除导出，添加注释说明未来可如何实现。
**影响文件**:
- `framework/pipeline/__init__.py` — 移除导出
- `framework/memory/__init__.py` — 移除导出
- `framework/__init__.py` — 移除导出

---

## 二、Phase 0 — 先修复测试引用（关键！）

> **原则**: 先修测试，再删文件。避免大面积测试失败破坏 CI。

### 0.1 清理 dead-file 相关的测试文件

| 步骤 | 操作 | 目标 | 说明 |
|------|------|------|------|
| 0.1.1 | 删除测试文件 | `tests/unit/session/test_agent_session.py` | AgentSession 方法全死，测试无意义 |
| 0.1.2 | 删除测试文件 | `tests/unit/session/test_agent_session_skills.py` | 同上 |
| 0.1.3 | 删除测试文件 | `tests/unit/pipeline/test_cli_output_adapter.py` | CLIOutputAdapter 无生产使用，降级为内部实现后可保留测试 |
| 0.1.4 | 删除测试文件 | `tests/unit/pipeline/test_http_output_adapter.py` | HTTPOutputAdapter 同上 |
| 0.1.5 | 删除测试文件 | `tests/unit/pipeline/test_pipeline_timeout.py` | safe_send_output() 测试 |

### 0.2 清理测试中对死代码的引用

修改 `tests/unit/multi_agent/test_core_runtime.py`，删除以下引用：
- `InterruptibleRunner` (line 231)
- `ReActStrategy` (line 250)
- `SingleTurnStrategy` (line 261)
- `InMemoryTaskCoordinator` (lines 157, 169, 222)
- `NullTaskCoordinator` (line 184)
- `AgentDirectory` (lines 90, 98)
- `SubagentService` (lines 303, 383, 417, 446, 474)

修改 `tests/unit/core/test_context.py`，删除对 `FileContextManager`、`EphemeralContextManager`、`InMemoryContextManager` 的测试。

修改 `tests/unit/memory/test_history_search.py`，删除对 `KeywordHistorySearch` 的测试。

### 0.3 验证

```bash
pytest tests/unit/ -v --tb=short
# 必须全部通过
```

---

## 二、Phase 1 — 删除整文件死模块（零风险）

> 9 个整文件，全库零引用。删除后运行测试确认零影响。

### 执行清单

| 步骤 | 文件 | 验证命令 |
|------|------|---------|
| 1.1 | `rm framework/tools/executor.py` | `grep -r "ToolExecutor\|from framework\.tools\.executor" framework/ tests/ --include="*.py"` |
| 1.2 | `rm framework/utils/log_fmt.py` | `grep -r "log_fmt\|from framework\.utils\.log_fmt" framework/ tests/ --include="*.py"` |
| 1.3 | `rm framework/utils/tokenizer.py` | `grep -r "from framework\.utils\.tokenizer\|import tokenizer" framework/ tests/ --include="*.py"` |
| 1.4 | `rm framework/core/strategy.py` | `grep -r "ExecutionStrategy\|ReActStrategy\|SingleTurnStrategy" framework/ tests/ --include="*.py"` |
| 1.5 | `rm framework/core/runner.py` | `grep -r "InterruptibleRunner" framework/ tests/ --include="*.py"` |
| 1.6 | `rm framework/multi_agent/coordinator.py` | `grep -r "TaskCoordinator\|NullTaskCoordinator\|InMemoryTaskCoordinator\|TaskRecord" framework/ tests/ --include="*.py"` |
| 1.7 | `rm framework/multi_agent/event_bus.py` | `grep -r "TaskEventBus\|TaskEventReporter\|CompositeTaskEventReporter\|LoggingTaskEventReporter" framework/ tests/ --include="*.py"` |
| 1.8 | `rm framework/session/agent_session.py` | `grep -r "AgentSession\|from framework\.session" framework/ tests/ --include="*.py"` |
| 1.9 | `rm framework/multi_agent/subagent_service.py` | `grep -r "SubagentService" framework/ tests/ --include="*.py"` |

### 同步清理 `__init__.py` 导出

| 文件 | 需移除的导出 |
|------|-------------|
| `framework/core/__init__.py` | `ExecutionStrategy`, `ReActStrategy`, `SingleTurnStrategy`, `InterruptibleRunner` |
| `framework/multi_agent/__init__.py` | `InterruptibleRunner`, `ReActStrategy`, `SingleTurnStrategy`, `ExecutionStrategy`, `InMemoryTaskCoordinator`, `NullTaskCoordinator`, `TaskCoordinator`, `TaskRecord`, `TaskEventBus`, `TaskEventReporter`, `CompositeTaskEventReporter`, `LoggingTaskEventReporter`, `TaskEventType`, `SubagentService`, `SessionMeta` |
| `framework/__init__.py` | `AgentSession`, `SubagentService`（如有） |

### 同步清理 AGENTS.md / 文档

| 文件 | 需更新内容 |
|------|-----------|
| `framework/core/AGENTS.md` | 移除 `strategy.py`、`runner.py` 的文档条目 |
| `framework/pipeline/AGENTS.md` | 移除 `LoggingOutputAdapter` 的文档条目 |
| `framework/multi_agent/` 相关 docs | 移除 `coordinator.py`、`event_bus.py`、`subagent_service.py` 的文档条目 |

### 验收

```bash
pytest tests/unit/ -v --tb=short
# 必须全部通过，且测试数量与 Phase 0 后一致
```

---

## 三、Phase 2 — 删除存活文件中的死类

> 从存活文件中移除类定义，同步清理所有 `__init__.py` 导出和测试引用。

### 执行清单

| 步骤 | 类名 | 所在文件 | 操作 |
|------|------|---------|------|
| 2.1 | `NoOpEmitter` | `core/emitter.py:440` | 删除 class 定义 |
| 2.2 | `LoggingEmitter` | `core/emitter.py:461` | 删除 class 定义 |
| 2.3 | `ShortTermMessageHistory` | `memory/history.py:101` | 删除 class 定义 |
| 2.4 | `LoggingOutputAdapter` | `pipeline/adapters.py:313` | 删除 class 定义 |
| 2.5 | `TaskProgressHook` | `multi_agent/hooks.py` | 删除 class 定义 |
| 2.6 | `BufferingEmitter` | `core/emitter.py:350` | 删除 class 定义 |
| 2.7 | `CompositeOutputAdapter` | `pipeline/adapters.py:377` | 删除 class 定义 |
| 2.8 | `InMemoryContextManager` | `core/context.py:146` | 删除 class 定义 |
| 2.9 | `FileContextManager` | `core/context.py:234` | 删除 class 定义 |
| 2.10 | `EphemeralContextManager` | `core/context.py:224` | 删除 class 定义（注意：继承 InMemoryContextManager） |
| 2.11 | `AgentDirectory` | `multi_agent/registry.py:69` | 删除 class 定义 |

### 同步修改 factory.py

`factory.py:147` 中的 `EphemeralContextManager(...)` 分支需改为使用 `MemorySystemContextManager` 或直接使用 `InMemoryContextManager`（如果保留）。

### 同步清理 `__init__.py`

| 文件 | 需移除的导出 |
|------|-------------|
| `framework/core/__init__.py` | `NoOpEmitter`, `LoggingEmitter`, `BufferingEmitter`, `InMemoryContextManager`, `FileContextManager`, `EphemeralContextManager` |
| `framework/pipeline/__init__.py` | `LoggingOutputAdapter`, `CompositeOutputAdapter` |
| `framework/memory/__init__.py` | `ShortTermMessageHistory` |
| `framework/multi_agent/__init__.py` | `AgentDirectory`, `TaskProgressHook` |
| `framework/__init__.py` | `NoOpEmitter`, `LoggingEmitter`, `BufferingEmitter`, `FileContextManager`, `LoggingOutputAdapter`, `CompositeOutputAdapter` |

### 降级导出（保留文件，仅降级）

| 类名 | 所在文件 | 操作 |
|------|---------|------|
| `CLIOutputAdapter` | `pipeline/adapters.py` | 保留文件，从 `pipeline/__init__.py` 和 `framework/__init__.py` 移除导出 |
| `HTTPOutputAdapter` | `pipeline/adapters.py` | 同上 |
| `InMemoryStoreRegistry` | `memory/registry/in_memory.py` | 保留文件（48+ 测试依赖），从 `memory/__init__.py` 移除导出 |

### 验收

```bash
pytest tests/unit/ -v --tb=short
grep -r "NoOpEmitter\|LoggingEmitter\|BufferingEmitter\|FileContextManager\|EphemeralContextManager\|AgentDirectory" framework/ --include="*.py" | grep -v "test_" | grep -v "__pycache__"
# 第二个命令应输出为空（除了类定义已被删除的文件）
```

---

## 四、Phase 3 — 删除死方法和死字段

> 从存活类中移除从未被调用的方法和从未被读取的字段。

### 4.1 死方法删除

#### `core/emitter.py`

| 方法 | 行号 | 宿主类 |
|------|------|--------|
| `filter_content()` | 67 | `EmitterConfig` |
| `truncate_tool_result()` | 80 | `EmitterConfig` |
| `emit_tool_error()` | 160 | `ContentEmitter` |

#### `core/tool_manager.py`

| 方法/属性 | 行号 | 宿主类 |
|-----------|------|--------|
| `clone()` | 142 | `Tool` |
| `validate_params()` | 154 | `Tool` |
| `execution_time_ms` | 250 | `ToolResult` (property) |
| `get_tools_section()` | 582 | `ToolManager` |
| `list_tool_instances()` | 643 | `InMemoryToolManager` |

#### `core/agent.py`

| 方法 | 宿主类 |
|------|--------|
| `add_attachment()` | `AgentContext` |

#### `pipeline/pipeline.py`

| 函数 | 行号 |
|------|------|
| `safe_send_output()` | 86 |

#### `pipeline/adapters.py`

| 方法/属性 | 行号 | 宿主类 |
|-----------|------|--------|
| `supports_streaming` | 264 | `OutputAdapter` |
| `send_stream()` | 274 | `OutputAdapter` |

#### `multi_agent/pool.py`

| 方法 | 宿主类 |
|------|--------|
| `get_status()` | `AgentPool` |
| `close()` | `AgentPool` |
| `register_directory()` | `AgentPool` |
| `find_profiles()` | `AgentPool` |
| `register_sync_future()` | `AgentPool` |
| `pop_sync_future()` | `AgentPool` |

#### `multi_agent/communication.py`

| 方法 | 宿主类 |
|------|--------|
| `send_sync()` | `AgentCommunicationService` |

#### `memory/default_system.py`

| 方法 | 宿主类 |
|------|--------|
| `close()` | `DefaultMemorySystem` |
| `search_memories()` | `DefaultMemorySystem` |
| `get_history_entries()` | `DefaultMemorySystem` |
| `get_knowledge()` | `DefaultMemorySystem` |

### 4.2 死字段删除

#### `multi_agent/descriptor.py` — AgentInstance

```python
# 删除以下字段：
agent: Agent              # 零读取 — pipeline 通过 self.agent 访问
session: AgentSession     # 零读取 — AgentSession 整个类已删除
emitter_config: EmitterConfig  # 零读取
tool_manager: ToolManager # 零读取 — pipeline 内部自有引用
hooks: list               # 零读取 — hooks 通过 runtime 传递
```

#### `multi_agent/descriptor.py` — AgentDescriptor

```python
# 删除以下字段：
governance_config: ContextGovernanceConfig  # 零读取
context_window_tokens: int | None           # 零读取
fail_on_tool_error: bool                    # 零读取
streaming_to_user: bool                     # 零读取
internal_streaming: bool                    # 零读取
inbox_max_messages_per_turn: int            # 零读取
max_tools_per_turn: int                     # 零读取（pipeline.py:691 设为 None）
```

#### `core/emitter.py` — AgentResult

```python
# 删除以下字段：
usage: dict[str, Any]     # 零读取（LLM response.usage ≠ AgentResult.usage）
metadata: dict[str, Any]  # 零读取
```

### 4.3 同步修改赋值代码

| 文件 | 需修改 |
|------|--------|
| `framework/multi_agent/factory.py` | 删除对 AgentInstance 死字段的赋值（`agent`/`session`/`emitter_config`/`tool_manager`/`hooks`） |
| `framework/ioc/factories/descriptors.py` | 删除对 AgentDescriptor 死字段的赋值 |
| `framework/core/emitter.py` | 删除 AgentResult 构造中的 `usage`/`metadata` |

### 验收

```bash
pytest tests/unit/ -v --tb=short
# 如有失败，同步修复测试中对死方法/字段的调用
```

---

## 五、Phase 4 — 架构修复

> 消除隐式耦合，修复代码质量问题。不涉及删除。

### 4.1 提取 `_dream_locks` 到独立模块

```bash
# 创建新文件
touch framework/runtime/dream_locks.py
```

**迁移内容**:
```python
# framework/runtime/dream_locks.py
"""DreamEngine 并发锁，确保同一 session 不会被同时处理。"""
import asyncio

_dream_locks: dict[str, asyncio.Lock] = {}
```

**修改引用**:
- `framework/session/agent_session.py` → 文件已删除（Phase 1），无需修改
- `framework/pipeline/pipeline.py:51` → 改为 `from ..runtime.dream_locks import _dream_locks`

### 4.2 修复重复赋值 Bug

`session/agent_session.py` 已在 Phase 1 删除，此 Bug 自然消除。

### 4.3 删除 `command_interceptor` deprecated 参数

`pipeline/pipeline.py:198-202` — 参数已被标记为 deprecated 且完全忽略。删除参数定义和 warning 代码。

### 验收

```bash
pytest tests/unit/ -v --tb=short
# 确认 _dream_locks import 路径正确
grep -r "_dream_locks" framework/ --include="*.py"
```

---

## 六、Phase 5 — 清理导出和文档

### 6.1 清理 `__init__.py` 死导出

已在 Phase 1~3 中同步清理。此阶段做最终验证：

```bash
# 验证：所有导出的符号必须存在且可访问
python -c "import framework; print(dir(framework))"
python -c "import framework.core; print(dir(framework.core))"
python -c "import framework.multi_agent; print(dir(framework.multi_agent))"
```

### 6.2 清理 `AGENTS.md` 文档

| 文件 | 移除内容 |
|------|---------|
| `framework/core/AGENTS.md` | `runner.py`, `strategy.py` 的文件描述 |
| `framework/pipeline/AGENTS.md` | `LoggingOutputAdapter`, `CompositeOutputAdapter` 的类描述 |
| `framework/memory/AGENTS.md` | `SessionMeta`/`SessionRetentionPolicy` 的混淆条目 |

### 6.3 清理代码注释

```bash
# 全局搜索对已删除类的注释引用
grep -r "NoOpEmitter\|LoggingEmitter\|ShortTermMessageHistory\|FileContextManager\|EphemeralContextManager\|AgentDirectory\|TaskCoordinator\|NullTaskCoordinator\|InMemoryTaskCoordinator" framework/ examples/ --include="*.py" | grep "#"
```

---

## 七、Phase 6 — security/sandbox 处理（用户已确认：保留 + 标记）

### ✅ 用户决策：保留，添加暂不推荐使用的注释

| 模块 | 决策 | 操作 |
|------|------|------|
| `framework/security/` | **保留** | 在 `security/__init__.py` 顶部添加 `# EXPERIMENTAL: 暂不推荐生产使用，待后续完善测试和文档后开放` |
| `framework/sandbox/` | **保留** | 同上 |
| `framework/tools/secure_wrapper.py` | **保留** | 同上 |

### 执行

```python
# 在每个文件的模块 docstring 后添加注释
# security/__init__.py, sandbox/__init__.py, tools/secure_wrapper.py
```

### 同步清理 examples/ 中对这些模块的引用说明

在 examples 对应的 README 或文件头部添加"实验性模块"说明。

---

## 八、Phase 7 — 最终验收

### 8.1 运行全量测试

```bash
# 单元测试
pytest tests/unit/ -v --tb=short 2>&1 | tee issue/cleanup_tests.log

# 集成测试（如有）
pytest tests/integration/ -v --tb=short 2>&1 | tee -a issue/cleanup_tests.log

# bot_project 测试
cd examples/bot_project && pytest tests/ -v --tb=short 2>&1 | tee ../../issue/cleanup_bot_tests.log
```

### 8.2 Import 完整性验证

```bash
python -c "
import framework
print('framework import OK')
# 验证关键类可访问
from framework import AgentPipeline, InMemoryToolManager, LLMProvider, ReActAgent
print('Key classes OK')
"
```

### 8.3 搜索残留引用

```bash
# 所有被删除的类名不应再出现（除 __pycache__ 和历史文档）
for name in NoOpEmitter LoggingEmitter ShortTermMessageHistory LoggingOutputAdapter \
    FileContextManager EphemeralContextManager AgentDirectory SubagentService \
    TaskCoordinator NullTaskCoordinator InMemoryTaskCoordinator TaskEventBus \
    TaskEventReporter TaskProgressHook CompositeOutputAdapter BufferingEmitter; do
    hits=$(grep -r "$name" framework/ tests/ --include="*.py" | grep -v __pycache__ | wc -l)
    if [ "$hits" -gt 0 ]; then
        echo "WARNING: $name still referenced ($hits hits)"
    fi
done
echo "Residual check complete"
```

### 8.4 Commit

```bash
git add -A
git diff --cached --stat
git commit -m "chore: remove ~3500 lines of dead code across framework/

Targets:
- 9 whole files deleted (strategy, runner, executor, log_fmt, tokenizer,
  coordinator, event_bus, agent_session, subagent_service)
- 13 dead classes removed from live files
- 23 dead methods removed from live classes
- 14 dead fields removed from dataclasses
- _dream_locks extracted to framework/runtime/dream_locks.py
- All __init__.py exports synchronized

Verified: pytest passes, zero residual imports

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## 十、争议项处理（用户已确认）

| # | 争议项 | 用户决策 | 执行 |
|---|--------|---------|------|
| Q-01 | `security/` + `sandbox/` + `secure_wrapper.py` | **保留 + 标记** | 添加 `# EXPERIMENTAL` 注释，暂不推荐使用 |
| Q-02 | `CLIOutputAdapter` / `HTTPOutputAdapter` / `InMemoryStoreRegistry` | **降级** | 保留文件，从公开导出移除。添加注释说明后续实现方式 |
| Q-03 | `AgentSession` + `session/` | **删除** | Phase 1 整文件删除，工厂分支迁移 |
| Q-04 | `SubagentService` | **删除** | Phase 1 整文件删除，bot_project import 清理 |
| Q-05 | `_dream_locks` 提取 | **提取** | 创建 `framework/runtime/dream_locks.py` |

---

## 十、执行概览

| Phase | 内容 | 预计删除行数 | 风险 | 用户确认 |
|-------|------|-------------|------|---------|
| **0** | 修复测试引用 | 0 (修改) | 🟢 | 否 |
| **1** | 删除 9 个整文件 | ~2200 | 🟢 | 否 |
| **2** | 删除 13 个死类 | ~500 | 🟢 | 否 |
| **3** | 删除 23 死方法 + 14 死字段 | ~400 | 🟢 | 否 |
| **4** | 架构修复 | ~20 (修改) | 🟢 | 否 |
| **5** | 清理导出/文档 | ~50 | 🟢 | 否 |
| **6** | security/sandbox 评估 | ~1500 | 🟡 | **是** |
| **7** | 最终验收 | 0 | 🟢 | 否 |

**总计预计删除**: ~3500+ 行 Python 代码 + ~200 行文档

---

## 十一、回滚方案

如清理后发现问题：

```bash
# 方案 A: 恢复到 tag
git checkout backup/pre-cleanup-$(date +%Y%m%d)

# 方案 B: 从备份分支恢复单个文件
git checkout backup/redundant-code-pre-cleanup -- framework/core/strategy.py

# 方案 C: 完全回滚
git reset --hard backup/redundant-code-pre-cleanup
```
