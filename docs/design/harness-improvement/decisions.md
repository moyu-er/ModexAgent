# Harness 改进决策记录

> ModexAgent harness 改进——缓存命中率、上下文管理、Agent 引导
> 创建时间：2026-08-04
> 最后更新：2026-08-04（全部探索完成，第一批实施完成）

---

## 决策总览

| # | 改进项 | 决策 | 优先级 | 备注 |
|---|--------|------|--------|------|
| 1 | prompt_cache_key / cache_control | ✅ 已实施 | 高 | 区分 provider，注意 litellm 适配 |
| 2 | Version 操作简化 | ✅ 已实施 | 中 | TTL 缓存替代每次 SHA256/版本查询 |
| 3 | URB 插入位置改尾部追加 | ✅ 已实施 | 中 | 尾部追加 + system-reminder 包装 + turn 内缓存 |
| 4 | 媒体注入（用户附件路径） | 📋 待办 | 低 | 用户附件路径 MODIFIES 现有消息，cache-unfriendly；价值有限 |
| 5 | LossyContent 改稳定占位符 | ✅ 已实施 | 最高 | 固定常量 `[Old tool result content cleared]` |
| 6 | 压缩后 head+tail 结构 | 📋 待办（后续设计） | — | 重要但需大量决策 |
| 7 | 摘要迭代更新 | 📋 待办（后续设计） | — | 当前 archive 是独立多份注入，非全局一份 |
| 8 | 输出 token 预留 | ✅ 已实施 | 中 | threshold 减去 max_output_tokens |
| 9 | 压缩后重注入 background task | ✅ 已确认 | — | todo 已覆盖；活跃 subagent 状态是真正 gap |
| 10 | ChatMessage origin 字段 | ❌ 不需要 | — | 已有多 role 机制 |
| 11 | Goal 边界注入 | 📋 待办 | — | — |
| 12 | Max steps API 级工具禁用 | 📋 待办 | — | — |
| 13 | Per-Provider 提示变体 | 📋 待办 | — | — |
| 14 | 系统提示顺序调整 | 📋 待办 | — | — |
| 15 | 压缩提示加诚实约束 | 📋 待办 | — | — |
| 16 | Provider blocks/prefetch 重复注入修复 | ✅ 已实施（收敛） | 中 | 已通过收敛统一为单一组装路径 |

---

## 详细决策

### 1. prompt_cache_key / cache_control — ✅ 已实施

**决策**：在 LLM provider 调用中添加缓存参数。

**注意事项**：
- OpenAI/Kimi：`prompt_cache_key = session_id`（自动前缀缓存）
- Anthropic：`cache_control: {"type": "ephemeral"}` 断点（系统提示 + 最后消息 + 最后工具定义）
- **litellm 适配**：litellm 作为多 provider 抽象层，需确认其 cache 参数传递机制
- DeepSeek：支持自动前缀缓存，无需显式参数

**实施位置**：`src/modex_agent/agents/react/llm_client.py` + `src/modex_agent/providers/`

**实施结果**：`inject_cache_control(params, session_id)` 注入函数，三条 LLM 调用路径透传 `prompt_cache_key=str(ctx.session)`。OpenAI 和 LiteLLM provider 显式调用注入函数。

---

### 2. Version 操作简化 — ✅ 已实施

**决策**：TTL 缓存 version 检查结果，避免每 LLM iteration 做 I/O。

**当前问题**：
- `ArchiveProvider._fetch_version()` 每次调用都从存储读取并计算 SHA256
- `PrunedProvider._fetch_version()` 每次调用都查 manager 版本
- 这些版本检查本身是 I/O 操作

**实施结果**：5s TTL 缓存（`_VERSION_TTL_SECONDS = 5.0`），同一 provider 实例在 TTL 窗口内返回缓存版本。Archive 和 Pruned 使用相同模式（rule 15 收敛）。

---

### 3. URB 插入位置改尾部追加 — ✅ 已实施

**探索结论**：

尾部追加（append-only）是 cache-friendly 的注入模式——不修改消息前缀，只在末尾追加，prompt cache prefix 保持稳定。

**原始问题**：`UserRetentionBufferInjectionGovernance` 插入 position 1（system 后），所有历史消息移位 → cache-unfriendly。

**实施结果**：
- 尾部追加 `[*messages, urb_msg]`
- `<system-reminder>` XML 标签包装（user role，上下文补充而非新用户输入）
- 只注入未完成 entries（`completing_assistant_content is None`）
- Turn 内缓存：iteration 1 读 storage → 缓存到 `state.custom[URB_CONTENT]`；iteration 2+ 零 I/O
- 不持久化（governance 只操作 LLM 输入副本，`state.custom` turn 结束销毁）
- max_entries = 3（`UserRetentionBufferConfig` 默认）
- **待办**：mid-turn URB 重新注入（需 URB `get_version()` + 变化来源判断）

---

### 4. 媒体注入（用户附件路径） — 📋 待办

**探索结论**：

媒体注入有**两条路径**，行为不同：

| 路径 | 来源 | 行为 | Cache-friendly? |
|------|------|------|-----------------|
| Path 1: 用户附件 | `INLINE_ATTACHMENTS`（用户上传） | **MODIFY** — 替换最后一条 user 消息的 content（str→list[ContentPart]） | ❌ 否 |
| Path 2: 工具媒体 | `TOOL_MEDIA_CACHE`（工具产出） | **APPEND** — 追加新合成 user 消息到尾部 | ✅ 是 |

**改进方向**：
- **方案 A**：用户附件也改为追加新 user 消息——简单但改变了语义
- **方案 B**：在 input pipeline 阶段（持久化前）就注入图片到 user 消息——从根源消除问题
- **优先级低**：用户明确指示价值有限，时间紧张可跳过

---

### 5. LossyContent 改稳定占位符 — ✅ 已实施

**决策**：方案 A — governance 仅内存操作，固定占位符常量。

**原始问题**：
- `LossyContentCompactionGovernance`：截断到 `head_chars` + 添加变量 metadata → 每次截断产生**不同内容** → cache-unfriendly
- `MicrocompactGovernance`：替换为 `"[{name} result omitted: {len} chars]"` → **变量文本** → cache-unfriendly

**实施结果**：
- 固定常量 `_CLEARED_PLACEHOLDER = "[Old tool result content cleared]"`
- TOOL role 超过 limit 时直接替换为占位符（不做 head+suffix）
- `MicrocompactGovernance` 和 `_compact_xml_content` 也使用同一常量
- 其他 role（assistant/agent/user）保留现有 head-truncation + suffix 逻辑
- 不持久化 timestamp 标志（governance 每次在 fresh copy 上重新决定）
- step-based 设计（已有）保证同一 block 内消息集稳定

---

### 6. 压缩后 head+tail 结构 — 📋 待办（后续设计）

**决策**：记为待办。需要大量设计决策。

**待设计问题**：
- head 预算多少 token
- tail 预算多少 token
- elision 标记的格式和内容
- 与现有 5 阶段 cleanup 流程如何集成
- 与 archive 生成如何协作

---

### 7. 摘要迭代更新 — 📋 待办（后续设计）

**决策**：记为待办。当前 archive 是独立多份生成+注入，非全局一份。

**待设计问题**：
- 当前：每次 cleanup 生成一份独立 archive（context.md/knowledge.md/index.md），最多注入 3 份
- 迭代更新：全局一份摘要，每次压缩时更新
- 哪种更好？独立多份保留了历史粒度，迭代更新保留了连续性
- 设计决策：是否改为全局一份？还是保持多份但加迭代更新？

---

### 8. 输出 token 预留 — ✅ 已实施

**决策**：threshold 减去 max_output_tokens。

**实施结果**：
- `_check_trigger` threshold: `(max_context_tokens - max_output_tokens) * max_token_ratio`
- `max_output_tokens: int = 0` 添加到 `SessionConfig` 和 `ShortTermConfig`
- 通过 factory → `cleanup_config` → `cleanup_session` → `_check_trigger` 传递
- `max(1, ...)` 防御 max_output_tokens > max_context_tokens 的错误配置

---

### 9. 压缩后重注入 background task — ✅ 已确认

**探索结论**：

**已重注入（10 项）**：
1. 压缩通知文本（`TodoReorientationHook` `<system-reminder>`，event-driven via `MemoryHookRunner`）
2. Archive 摘要引用（条件性）
3. Pruned catalog 指针
4. 活跃 todo 列表（条件性）
5. 更新的 pruned catalog XML（`PrunedProvider` 版本刷新→系统提示）
6. 更新的 archive 摘要（`ArchiveProvider` 版本刷新→系统提示）
7. 裁剪的用户/agent 对话片段（`UserRetentionBufferInjectionGovernance`）
8. Core memory 文件
9. Memory disclaimer 头部
10. Inbox 消息（包括 subagent 回复，`InboxFlushHook`）

**未重注入（6 项 gap）**：
1. **活跃 subagent 状态** — `AgentPool` 追踪 `_active_session_counts` 但不注入到 agent 上下文
2. Background task 状态
3. Goal/objective
4. Tools diff
5. 运行中任务进度
6. 跨 turn 压缩检测（per-turn `state.custom` 被销毁）

**结论**：todo 注入体系已覆盖。**活跃 subagent 状态是真正的 gap**。

---

### 10. ChatMessage origin 字段 — ❌ 不需要

**决策**：ModexAgent 已有多 role 机制（`MessageRole` StrEnum: system/user/assistant/tool/agent），不止 user 会真正转换成 user role。不需要额外的 origin 字段。

---

### 16. Provider blocks/prefetch 重复注入修复 — ✅ 已实施（收敛）

**探索发现**：CoreMemory 未重复注入，但 provider blocks 和 provider prefetch **确实重复注入**。

**原始问题**：
- `system.py:load()` 创建清洁版 `FullInjectionPolicy`（跳过 archive/pruned）
- 但清洁版 policy 仍调用 `_inject_provider_blocks()` 和 `_inject_provider_prefetch()`
- 结果包在 `CoreMemoryProvider(result.system_prompt)` 中加入 pipeline
- 然后 `load()` 又添加独立的 `ProviderBlocksProvider` 和 `ProviderPrefetchProvider`
- → blocks 和 prefetch 出现两次

**实施结果**：通过收敛统一为单一组装路径（见下方收敛设计）。`FullInjectionPolicy` 只做 disclaimer + core memory，所有其他内容由 pipeline provider 唯一注入。

---

## 收敛设计：注入策略统一

### 问题

`FullInjectionPolicy.assemble()` 和 `SystemPromptProvider` pipeline providers 两条路径都注入相同内容（archive, pruned, blocks, prefetch），通过不同机制——policy 无缓存，pipeline 有 version 缓存。

### 收敛方案

`FullInjectionPolicy` 只保留它独有的能力（priority 排序 + token 预算裁剪），只用于需要预算控制的内容（disclaimer + core memory XML）。其他所有内容通过 pipeline provider 注入（有 version 缓存）。

**收敛前（发散）**：
```
FullInjectionPolicy.assemble()          ← 无缓存，6个_inject_*()
  + clean policy 补丁                    ← 条件跳过4个_inject_*()
  + pipeline providers                   ← 有缓存，独立provider
  = 两条路径，条件分支，skip flags
```

**收敛后（统一）**：
```
FullInjectionPolicy.assemble()          ← 只做 disclaimer + core memory（预算裁剪）
  + pipeline providers                   ← 所有其他内容，唯一源，有缓存
  = 一条路径，无条件分支
```

### 具体移除

- `FullInjectionPolicy`: 删除 `_inject_archive()`, `_inject_pruned_catalog()`, `_inject_provider_blocks()`, `_inject_provider_prefetch()` + 相关能力查询方法和构造参数
- `RestrictedInjectionPolicy`: 删除 pruned 注入逻辑
- `MemoryInjectionPolicy` ABC: 删除 `injects_archive()`, `injects_pruned()`, `get_archive_injection_config()` — 简化为单方法契约
- `system.py:load()`: 删除 clean/original 条件分支

---

## 探索结果详情

### 探索 1：CoreMemory 是否重复注入 — ❌ 未重复

CoreMemory（SOUL.md/USER.md/MEMORY.md）注入**一次**。路径：
1. `FullInjectionPolicy.assemble()` → `_inject_core_memory()`（priority 100）
2. 结果包装在 `CoreMemoryProvider(result.system_prompt)` 加入 pipeline
3. `LLMNode._build_messages()` 调 `pipeline.get_or_refresh()` 一次
4. `AgentContext.to_messages()` 只返回非 system 历史消息（不加 system 消息）

**发现的真正 bug**：provider blocks/prefetch 重复（见决策 #16）。

---

### 探索 2：压缩后状态重注入位置

尾部追加（append-only）是 cache-friendly 的注入模式——不修改消息前缀，只在末尾追加。

ModexAgent 原始实现：`UserRetentionBufferInjectionGovernance` 插入 position 1 → cache-unfriendly。

已通过 URB 重写修复（决策 #3）。

---

### 探索 3：压缩后重注入审计

详见决策 #9。

---

### 探索 4：工具输出占位符清理机制

详见决策 #5。

**关键发现**：
- 固定占位符 `"[Old tool result content cleared]"` 是 cache-friendly 的最佳选择
- 所有 compacted tool results 产生相同输出，不论原始内容长度

---

### 探索 5：媒体注入实现验证

详见决策 #4。

**关键发现**：
- **Path 1（用户附件）**：MODIFY — 替换最后一条 user 消息的 content（str→list），cache-unfriendly
- **Path 2（工具媒体）**：APPEND — 追加新合成 user 消息到尾部，cache-friendly
- 两条路径在同一个 `enrich_inline_media()` 调用中顺序执行
- Path 1 每轮 ReAct 迭代都重新注入（持久化历史存 text-only）
