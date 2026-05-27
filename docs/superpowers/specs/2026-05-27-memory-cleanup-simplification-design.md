# Memory Cleanup & Archival Simplification

## 1. 目标

精简 session 清理和归档流程，清理必定执行，归档可选且可失败。

## 2. 核心原则

1. **清理优先**：session 清理必定执行，与归档解耦
2. **归档可选**：`archive` 为 `None` 时跳过归档（不算失败）
3. **连续失败跳过**：归档连续失败 ≥ N 次（默认 3）后跳过归档，计数器置零
4. **不配置 ≠ 失败**：不配置 archive 层时计数器不增加
5. **不改变归档逻辑**：`DualLLMArchiveGenerationStrategy` 双通道 SummarizerAgent 调用不变

## 3. 架构变更

### 3.1 入口：直接调用，不再有回调注入或 interceptor

```
ScopedMessageHistory.append() / extend()
  └─ cleanup_session(session, archive, context, archive_strategy, ...)
```

`cleanup_session()` 是 `framework/memory/cleanup.py` 中的独立函数，硬编码调用，不通过 `MemoryLifecyclePolicy` 回调注入，不使用 `InterceptorChain`。

### 3.2 流程

```
cleanup_session()
  ├─ Step 1: 检查触发条件
  │     └─ 消息数 > max_messages 或 token > max_tokens → 继续，否则 return
  │
  ├─ Step 2: 清理 Session（必定执行）
  │     ├─ sanitize: 移除无效 tool-chain 记录
  │     ├─ plan: 计算 keep/prune 边界
  │     └─ commit: session.replace_messages(keep_messages)
  │
  └─ Step 3: 归档 Archive（可选）
        ├─ archive is None → return
        ├─ archive is not None:
        │     └─ archive_strategy.generate(pruned_messages)
        │          ├─ 成功 → 写 archive，fail_counter = 0
        │          └─ 失败 → fail_counter += 1
        │               └─ fail_counter ≥ fail_threshold → 跳过，fail_counter = 0
```

### 3.3 ArchiveGenerationStrategy: Protocol → ABC

```python
class ArchiveGenerationStrategy(ABC):
    @abstractmethod
    async def generate(
        self,
        messages: Sequence[ArchiveInputMessage],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> ArchiveGenerationResult: ...
```

### 3.4 ArchiveInputMessage: 归档前消息裁剪

每类 role 只保留归档必需字段，减少 LLM 负担（参考 hermes-agent `_serialize_for_summary`）：

| 原始 role   | 保留字段                        | 丢弃              |
|-------------|--------------------------------|-------------------|
| `user`      | role, content                  | metadata          |
| `assistant` | role, content                  | **tool_calls**    |
| `tool`      | role, content, tool_call_id    | name              |
| `system`    | role, content                  | —                 |

`assistant.tool_calls` 在转换时丢弃，不纳入归档输入。

### 3.5 默认实现：滑窗归档

`DualLLMArchiveGenerationStrategy` 内部新增分段逻辑：

1. user-turn 分段
2. 滑窗合并（每段 ≤ `max_segment_tokens=12000`）
3. 逐段调 `SummarizerAgent.summarize()` (CONTEXT + KNOWLEDGE)
4. 合并 writes 返回

对外接口不变，分段为内部优化。

## 4. 文件变更清单

### 4.1 新增

| 文件 | 内容 |
|---|---|
| `framework/memory/cleanup.py` | `cleanup_session()` + 内部固定流程（trigger, sanitize, plan, commit, archive） |

### 4.2 修改

| 文件 | 变更 |
|---|---|
| `framework/memory/archive_generation.py` | `Protocol`→`ABC`；新增 `ArchiveInputMessage`；`DualLLMArchiveGenerationStrategy` 加滑窗 |
| `framework/memory/default_system.py` | 移除 `on_messages_added` 回调注入；`ScopedMessageHistory` 直接调 `cleanup_session` |
| `framework/memory/system.py` | `create_memory_system()` 移除 `lifecycle_policy` 参数 |
| `framework/memory/lifecycle.py` | 仅保留 `MemoryMaintenancePolicy` + retention policies；删除 `MemoryLifecyclePolicy` |
| `framework/memory/__init__.py` | 更新导出 |
| `framework/memory/core/models.py` | 清理仅被删除模块使用的数据类 |
| `framework/memory/core/system.py` | 移除 `get_auto_compact_summary` 等无用接口 |
| `framework/interceptor/abc.py` | 移除 `MEMORY_OPERATION` 枚举值 |
| `framework/ioc/configs/memory.py` | 移除 `auto_compact` 字段 |
| `framework/ioc/factories/memory.py` | 移除 `auto_compact` 判断，无条件创建 `DualLLMArchiveGenerationStrategy`；改为创建 `cleanup_session` 所需参数 |
| `framework/ioc/factories/compression.py` | 简化或删除 |
| `framework/ioc/factories/descriptors.py` | 移除对 `compression_coordinator` 相关引用 |
| `framework/agents/summarizer/strategy.py` | 移除对 `compaction.policy` 的引用 |
| `framework/memory/context_governance.py` | 移除对 `retention` 的引用 |
| `framework/memory/injection/filter.py` | 移除对 `compaction.policy` 的引用 |
| `framework/memory/layers/config.py` | 移除 lifecycle 相关注释 |

### 4.3 删除

| 文件/目录 |
|---|
| `framework/memory/compression/` (整个目录: policies.py, planner.py, tool_chain.py, semantic_filter.py, `__init__.py`) |
| `framework/memory/compaction/` (整个目录: policy.py, boundary.py, `__init__.py`) |
| `framework/memory/retention/` (整个目录: policy.py, default.py, config.py, types.py, `__init__.py`) |

`tool_chain_sanitizer.py` 移入 `framework/memory/sanitizer.py`，去 Protocol。

### 4.4 Bot project 变更

| 文件 | 变更 |
|---|---|
| `examples/bot_project/bot/service/core.py` | `_auto_compact_task` → `_maintenance_task`；移除 lifecycle policy 创建 |
| `examples/bot_project/bot/service/builders.py` | `_create_subagent_memory` 移除 `DefaultMemoryLifecyclePolicy` |
| `examples/bot_project/config/pools/main.yml` | 移除 `auto_compact: false` |
| `examples/bot_project/plugins/mem0_memory/provider.py` | 移除对 `compaction.policy` 引用 |

### 4.5 测试变更

| 文件/目录 | 变更 |
|---|---|
| `tests/unit/memory/test_compression_policies.py` | 重写，覆盖 `cleanup_session` |
| `tests/unit/memory/test_lifecycle.py` | 重写，仅覆盖 `MemoryMaintenancePolicy` |
| `tests/unit/memory/test_bot_project_memory_pipeline.py` | 更新已移除 lifecycle 引用 |
| `tests/unit/memory/test_summarizer_integration.py` | 更新已移除 `MemoryCompressionCoordinator` 引用 |
| `tests/unit/ioc/test_memory_factory.py` | 移除 `auto_compact` 测试 |
| `tests/unit/memory/retention/` | 删除 |
| `tests/unit/memory/compression/` | 删除 |
| `tests/unit/memory/core/test_default_system.py` | 更新 lifecycle 相关测试 |

## 5. 配置变更

`auto_compact` 字段从 `ShortTermConfig` 删除。归档是否执行仅由 `long_term.enabled` 决定：

```yaml
memory:
  short_term:
    max_messages: 200
    max_tokens: 100000
    keep_ratio_for_messages: 0.4
    keep_ratio_for_token: 0.4
    # auto_compact: false   ← 删除
  long_term: {enabled: true}
```

## 6. 风险与兼容性

1. 删除 `auto_compact` 后，之前 `auto_compact: false` 的场景（archive 存在但不调 LLM）将变为 archive 存在就调 LLM。如需避免 LLM 调用，应设置 `long_term.enabled: false`。
2. `MemoryLifecyclePolicy` 作为回调被删除，如有外部实现依赖它，需改为直接调 `cleanup_session`。
3. 保留的 `tool_chain_sanitizer` 逻辑从 `compression/tool_chain_sanitizer.py` 移到 `memory/sanitizer.py`，import 路径变更。
