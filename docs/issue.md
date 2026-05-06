# 记忆压缩问题分析文档

## 1. 问题现象描述

**预期行为：**
- 配置 `max_messages=60`, `keep_ratio=0.5`
- 当消息数超过 60 条时触发压缩
- 压缩后保留 60 × 0.5 = 30 条消息

**实际观察到的现象：**
- 消息数不到 35 条就触发了压缩
- 压缩后保留了约 30 条消息

**测试条件：**
- 从启动到关闭服务，没有超过 30 分钟（auto_compact 未触发）
- 使用正常的对话流程

---

## 2. 分析过程

### 2.1 初步假设：Cooldown 机制导致提前触发

**分析内容：**
```python
# 压缩提交时存储
".last_compression": len(plan.keep_messages)  # = 30

# 触发检查时
last = 30
if all_msgs_count - last < self._cooldown:  # cooldown = 5
    return None  # 不触发
```

**初步结论：** cooldown 使用错误的基准值（保留消息数而非压缩前总数），可能提前触发压缩。

**后续重新审视：**（此分析可能不正确）
- cooldown 只是一个"不触发"的过滤条件
- 它不会主动让压缩提前触发
- 如果 `all_msgs_count - 30 >= 5`，即消息数达到 35 条，会检查是否满足触发条件
- 但触发条件是 `all_msgs_count > max_messages`（60），所以 35 条时不会触发

### 2.2 发现真正问题：TOKEN_PRESSURE 触发

**分析内容：**
```python
# 触发检查逻辑
if self._max_messages and all_msgs_count > self._max_messages:
    return CompressionTrigger(reason=CompressionReason.MESSAGE_COUNT)

# 使用了 get_visible_messages，只返回最后 max_messages 条
visible_messages = await session.get_visible_messages(context)
estimated_tokens = self._estimate_tokens(visible_messages)
if self._max_tokens and estimated_tokens > self._max_tokens:
    return CompressionTrigger(reason=CompressionReason.TOKEN_PRESSURE)
```

**问题 1：`get_visible_messages` 会限制返回消息数**
```python
# session.py 中的实现
def get_visible_messages(self, context, limit=None):
    messages = await self.get_all_messages(context)
    effective_limit = limit if limit is not None else self._config.max_messages
    return messages[-effective_limit:]  # 只返回最后 max_messages 条
```
- 如果 `max_messages=60`，它只返回最后 60 条
- 用这 60 条来计算 token，判断是否超过 `max_tokens`
- **这造成了循环判断：用受限制的消息数来计算 token**

**问题 2：即使 TOKEN_PRESSURE 触发，保留逻辑也不合理**
```python
# 当前实现
trigger_max_messages = getattr(self._trigger, "_max_messages", self._max_messages)
keep_target = max(1, int((trigger_max_messages or 100) * self._keep_ratio))
```
- 当 TOKEN_PRESSURE 触发时，仍然使用 `max_messages * keep_ratio` 计算保留数
- 这不合理！应该基于 token 数量来计算保留目标

### 2.3 其他发现

**问题 3：Cooldown 机制设计有缺陷（但可能不是问题的直接原因）**
- 使用"保留的消息数"作为 cooldown 基准，而不是"压缩前的消息数"
- 虽然不影响当前问题，但增加了不必要的复杂性

**问题 4：Auto-Compact 绕过 cooldown**
- 后台扫描直接调用 `maybe_compress`，没有 cooldown 检查
- 但在当前场景下（服务运行 < 30 分钟），auto_compact 未触发

---

## 3. 安全性要求：Tool 与 Tool_Call 匹配

### 3.1 问题描述

消息序列中存在以下对应关系：
```
assistant (with tool_calls: [id="call_123", ...]) 
  → tool (tool_call_id="call_123", content="...")
```

**关键约束：**
- assistant 消息中的 `tool_calls` 包含 `id` 字段
- tool 消息中的 `tool_call_id` 必须与 assistant 中的 `tool_calls[].id` 匹配
- LLM 需要同时看到 tool_call 和对应的 tool result 才能正确推理

**压缩时的问题：**
如果我们简单地从前往后按数量移除消息，可能会破坏这种对应关系：

```
❌ 错误示例：
[assistant(tool_calls:[id="call_1"]), tool(id="call_1"), assistant(tool_calls:[id="call_2"]), tool(id="call_2"), ...]
                                                                              ↑
                                                                        移除到这里

结果：
[assistant(tool_calls:[id="call_1"]), tool(id="call_1"), assistant(tool_calls:[id="call_2"])]
                                                                              ↑
                                                                        tool(id="call_2") 被移除了！

→ call_2 的 tool_call 没有对应的 result
→ LLM 会看到不完整的 tool 调用链
```

### 3.2 正确的压缩策略

**原则：保持 tool_call 和 tool result 的完整性**

1. **按"轮次"或"工具链"边界保留**
   - 保留完整的 assistant → tool 配对
   - 不保留孤立的 tool_call 或 tool result

2. **两种压缩方式都需要遵守此约束**
   - MESSAGE_COUNT 压缩：按消息数保留
   - TOKEN_PRESSURE 压缩：按 token 数保留

3. **实现方式**
   - 使用 BoundaryPolicy（如 ToolChainBoundaryPolicy）来找到安全的截断点
   - 截断点必须位于完整的 tool 链之后

### 3.3 当前代码中的保护机制

框架中已有 `BoundaryPolicy` 来处理这个问题：

```python
# boundary.py
class ToolChainBoundaryPolicy:
    """Find boundary after complete tool chains."""
    
    def find_prune_boundary(self, messages, decisions, target_prune):
        # 从后往前扫描，找到最近的 tool 链边界
        # 确保不会在 tool_call 和 tool result 之间截断
```

**结论：** 当前框架已经有保护机制（BoundaryPolicy），但需要确保：
1. 所有压缩路径都使用 BoundaryPolicy
2. 日志中明确显示是否找到了安全边界

---

## 4. 修复建议

### 4.1 已完成的修复

#### 修复 1：移除 Cooldown 机制
- 简化触发逻辑，移除不必要的冷却机制
- 移除 `.last_compression` 状态存储
- 位置：`framework/memory/compression/policies.py`

#### 修复 2：修正 token 计算使用全部消息
- 将 `get_visible_messages` 改为 `get_all_messages`
- 确保 token 计算基于所有消息，而不是受限制的消息
- 位置：`framework/memory/compression/policies.py`

#### 修复 3：添加触发原因日志
- 区分 MESSAGE_COUNT 和 TOKEN_PRESSURE 两种触发原因
- 显示具体的计算参数
- 位置：`framework/memory/compression/policies.py`

#### 修复 4：添加 token 比例保留逻辑
- 新增 `keep_ratio_for_token` 参数
- TOKEN_PRESSURE 触发时，使用 token 比例计算保留目标
- 从后往前保留消息直到达到目标 token 数
- **注意：此逻辑使用 BoundaryPolicy 确保 tool/tool_call 匹配**
- 位置：`framework/memory/compression/policies.py`

#### 修复 5：Auto-Compact 默认关闭
- 将 `auto_compact` 配置注释掉
- 避免后台扫描干扰正常逻辑
- 位置：`examples/bot_project/config/bot_config.yml`

### 4.2 待验证的问题

**问题：真正触发压缩的原因仍需日志验证**

当前日志已添加，需要运行验证：
- 是 MESSAGE_COUNT 触发（msgs > 60）？
- 还是 TOKEN_PRESSURE 触发（tokens > max_tokens）？

如果观察到：
```
Compression triggered by TOKEN_PRESSURE: tokens=xxx > max_tokens=100000
```
说明 token 计算是真正的触发原因。

---

## 5. 配置说明

### 5.1 完整配置项

```yaml
memory:
  main:
    short_term:
      max_messages: 60            # 消息数触发阈值，超过此数量触发压缩
      max_tokens: 100000          # Token 触发阈值，超过此数量触发压缩
      keep_ratio: 0.5             # 消息数触发时保留 max_messages × 此比例
      keep_ratio_for_token: 0.5   # Token 触发时保留 target_tokens × 此比例
      auto_llm_compression: true  # 是否启用 LLM 生成 Archive 摘要
```

### 5.2 配置说明

| 配置项 | 说明 | 推荐值 |
|--------|------|--------|
| `max_messages` | 消息数触发阈值，超过才触发压缩 | 60 |
| `max_tokens` | Token 触发阈值，超过才触发压缩 | 100000（约100K） |
| `keep_ratio` | 消息数触发时保留的比例 | 0.5 |
| `keep_ratio_for_token` | Token 触发时保留的比例 | 0.5 |

### 5.3 触发条件

压缩会在以下任一条件满足时触发：
1. **MESSAGE_COUNT**：`消息数 > max_messages`
2. **TOKEN_PRESSURE**：`token数 > max_tokens`

### 5.4 保留策略

- MESSAGE_COUNT 触发：保留 `max_messages × keep_ratio` 条消息
- TOKEN_PRESSURE 触发：保留 `当前token数 × keep_ratio_for_token` 的消息（从后往前保留）

**安全性保证**：无论哪种触发原因，都会通过 BoundaryPolicy 找到安全截断点，确保 tool_call 和 tool result 完整匹配。

---

## 6. 设计改进建议

### 6.1 统一触发条件（可选）

**建议原则：** 只使用消息数作为触发条件，移除 token 压力触发

**理由：**
1. token 计算是估算，不精确
2. 使用 `get_visible_messages` 会导致逻辑不一致（已修复为 get_all_messages）
3. 增加了系统复杂度

**如果保留双触发条件：**
- 确保 max_tokens 值足够大，避免过早触发
- keep_ratio_for_token 可以根据实际需求调整

### 6.2 安全性设计

**所有压缩操作必须遵守：**
1. 使用 BoundaryPolicy 找到安全截断点
2. 不破坏 tool_call 和 tool result 的对应关系
3. 日志中显示边界查找结果

---

## 7. 修改的文件清单

| 文件 | 修改内容 | 状态 |
|------|----------|------|
| `framework/memory/compression/policies.py` | 移除 cooldown，修正 token 计算，添加日志，添加 token 保留逻辑 | ✅ 已完成 |
| `framework/memory/lifecycle.py` | 移除 cooldown 描述 | ✅ 已完成 |
| `examples/bot_project/config/bot_config.yml` | 注释掉 auto_compact 配置 | ✅ 已完成 |
| `examples/bot_project/bot/service/core.py` | 修改 auto_compact 默认行为 | ✅ 已完成 |

---

## 7. 验证方法

### 7.1 日志验证

观察日志输出，确认触发原因：
```
# 消息数触发
Compression triggered by MESSAGE_COUNT: msgs=65 > max_messages=60
Compression triggered by MESSAGE_COUNT: keep_target=30 (max_messages=60 * keep_ratio=0.50), prune_count=35

# Token 压力触发
Compression triggered by TOKEN_PRESSURE: tokens=120000 > max_tokens=100000
Compression triggered by TOKEN_PRESSURE: keep_target=18 (based on token ratio), prune_count=47
```

### 7.2 功能验证

1. 对话到 35 条消息，确认不触发压缩
2. 对话到 60 条消息，确认触发压缩
3. 确认压缩后保留约 30 条消息
4. **确认 tool_call 和 tool result 配对完整**（通过检查日志或输出）

### 7.3 边界安全验证

检查以下场景：
- 压缩后保留的最近消息中，tool 调用是否有对应的 tool result
- 被归档的消息中，tool 调用是否有对应的 tool result

---

## 8. 注意事项

1. **本文档中的分析可能不完全正确** - 需要通过日志验证来确认真正的触发原因
2. **TOKEN_PRESSURE 触发可能是真正原因** - 因为之前的代码使用 `get_visible_messages`，只计算最后 60 条消息的 token
3. **修复后需要实际运行验证** - 确认行为符合预期
4. **安全性要求** - 压缩时必须保持 tool_call 和 tool result 的完整匹配