# Harness 改进决策记录

> ModexAgent harness 改进——缓存命中率、上下文管理、Agent 引导
> 创建时间：2026-08-04
> 最后更新：2026-08-25（新增决策 #18：usage 锚定，待 LLMProvider 重构后实施）

---

## 决策总览

| # | 改进项 | 决策 | 优先级 | 备注 |
|---|--------|------|--------|------|
| 1 | prompt_cache_key / cache_control | ✅ 已实施 | 高 | 区分 provider，注意 litellm 适配 |
| 2 | Version 操作简化 | ✅ 已实施 | 中 | TTL 缓存替代每次 SHA256/版本查询 |
| 3 | URB 插入位置改尾部追加 | ✅ 已实施 | 中 | 尾部追加 + system-reminder 包装 + turn 内缓存 |
| 4 | 媒体注入（用户附件路径） | 📋 待办 | 低 | 用户附件路径 MODIFIES 现有消息，cache-unfriendly；价值有限 |
| 5 | LossyContent 改稳定占位符 | ✅ 已实施→🔄 已重构 | 最高 | 固定常量 `[Old tool result content cleared]`；详见决策 #17 |
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
| 17 | Governance 收敛：ContextBudgetGovernance | ✅ 已实施 | 最高 | 3 策略→1 策略；token-window prune；详见下方 |
| 18 | 压缩触发计量与真实请求对齐（usage 锚定） | 📋 待办（LLMProvider 重构后） | 高 | 真实回执锚定 + 增量投影；详见下方 |

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

---

### 17. Governance 收敛：ContextBudgetGovernance — ✅ 已实施

**问题**：原 governance 链有三个内容修改策略（`LossyContentCompactionGovernance`、`MicrocompactGovernance`、`TokenBudgetGovernance`），存在以下系统性问题：

1. **char 阈值截断盲目触发**：统一 1200 chars 阈值，不评估总 token 量是否超预算，不评估截断收益
2. **step-based 分块导致批量突变**：消息数跨 step 边界时 50 条消息同时从原始变为截断——巨大的前缀突变
3. **多策略串联叠加修改**：Lossy + Microcompact 对同一批 tool result 做两遍操作
4. **与 ToolResultLimitInterceptor 职责重叠**：overflow 机制已在 50K chars 处处理单消息过大，governance 的 1200 chars 截断是重复操作
5. **TokenBudgetGovernance 未接入工厂**：缺少 proactive 硬预算防线
6. **per-call tool args 截断**：每次 LLM call 重新截断，修改 assistant 消息的 tool_calls 字段（缓存前缀的一部分）

**参考项目方案**：
- **opencode** `prune`：token 窗口（保留最近 40K tokens tool output）+ 最小收益门槛（< 20K 不执行）+ 持久化标记防重复
- **kimi-code** `contextProjector`：per-call 完全不修改内容，只做结构修复；内容截断在 append 时完成

**决策**：借鉴 opencode 的 token 窗口 + 最小收益门槛，去掉持久化标记（ModexAgent 的 governance 不修改持久化，确定性保证跨 call 一致）。

**实施**：

移除 3 个旧策略 + `_compact_xml_content`，新增 `ContextBudgetGovernance`：

```
Per-call governance 链 (2 个策略):
  ContextBudgetGovernance
    ├── 零修改路径 (total ≤ governance_ratio × max_context_tokens → return copy)
    ├── Phase 1: token-window 占位符替换
    │   ├── 从尾部向前累加 tool result tokens 到 protect_tokens → 窗口边界
    │   ├── 窗口外的 tool result 一次性替换为 _CLEARED_PLACEHOLDER
    │   ├── min_gain_tokens 门槛：可替换量 < min_gain → 不执行
    │   ├── keep_recent 结构保护：至少保留最近 N 条 tool result
    │   └── idempotency guard: meta_context_lossy → 已修改则 SKIP
    └── 不丢弃消息（尾部保留/硬截断交给 cleanup_session + EmergencyCompactionGovernance）
  →
  ToolChainRepairGovernance (结构修复, 不动)
```

**关键设计点**：

| 维度 | 旧设计 | 新设计 |
|------|-------|-------|
| 触发依据 | char 阈值 + step 位置 | token 预算 (governance_ratio=0.60) |
| 选择依据 | step-based 位置 | token 窗口 (protect_tokens=40K) |
| 收益评估 | ❌ 无 | ✅ min_gain_tokens=20K 门槛 |
| 替换方式 | 逐条+多遍 | 一次性单遍 |
| 消息丢弃 | TokenBudgetGovernance 硬截断 | ❌ 不丢弃（交给 cleanup） |
| char 阈值截断 | ✅ 1200 chars | ❌ 移除（overflow 机制已处理） |
| XML-aware 截断 | ✅ truncate_xml_safe | ❌ 移除（固定占位符替代） |
| tool args 截断 | ✅ per-call | ❌ 移除（后续可移到 build_assistant_message） |

**前缀稳定性保证**：
- governance_ratio (0.60) < max_token_ratio (0.85)：governance 在 compact 之前渐进介入
- compact 周期内 total 单调增长 → 窗口只扩展不收缩 → 已有占位符不变
- 占位符是固定常量 → 相同消息→相同替换 → 跨 call 确定性
- idempotency guard → 修改后的消息不会被同一 governance 的后续判断命中
- 不丢弃消息 → 消息条数和顺序稳定 → 前缀只扩展不突变

**配置变化**：
```python
# 旧: LossyConfig (6 个 char 阈值 + step 参数)
# 新: BudgetConfig (3 个语义化参数)
class BudgetConfig(BaseModel):
    governance_ratio: float = 0.60    # governance 介入阈值
    protect_tokens: int = 40_000      # 保护最近 N tokens 的 tool output
    min_gain_tokens: int = 20_000     # 最小替换收益
    keep_recent: int = 10             # 结构保护
    whitelist_tools: set[str] = set() # 不裁剪的工具
```

**compact_msg token_count 打戳**（cleanup.py `_commit_session_phase`）：compact summary 消息现在在 commit 时通过 estimator 打 `token_count`，下次 boundary 计算不再需要临时重算。

---

### 18. 压缩触发计量与真实请求对齐（usage 锚定）— 📋 待办（LLMProvider 重构后）

**问题**：压缩触发线（`check_cleanup_trigger`）只对 session 非系统消息做估算求和，存在系统性盲区与漂移：

- **不计数**：system prompt（15-provider 组装，含 pruned catalog / experience / skills，可达数十 K tokens）、tool definitions、media 注入
- **估算 tokenizer 偏差**：cl100k 估算与 provider 真实 tokenizer 存在偏差（CJK 下更明显）
- **请求级裁剪不改计数**：`ContextBudgetGovernance` 占位符替换、media 注入、XML 截断均不修改任何 token 计数 → 计量与真实请求大小 drift
  - 高估方向（如 governance 替换后仍按原值计数）：浪费窗口余量、过早压缩、多余压缩调用、打断前缀缓存
  - 低估方向（system+tools 盲区、tokenizer 偏差、media 注入）：真实请求先于触发线打满窗口 → provider 400 context-overflow → 落入 `EmergencyCompactionGovernance` 请求级硬裁剪（无摘要、不落 pruned、不持久化、每迭代重复触发）

**实现方式**（LLMProvider 重构落地后实施）：

- **真实回执锚定 + 增量投影**：每次 LLM 调用返回的 usage（input tokens）作为压力锚点；压力值 = 最近一次真实 input_tokens + 此后新增消息的估算 token 增量
- 触发判定优先使用锚定值；无锚点（新会话 / resume / 流式响应无 usage）回退现有估算求和
- 锚点带 model 维度：模型切换即失效，回退估算
- 落点：`ScopedMessageHistory` 增加内存态 `note_usage()`（零持久化，不改 DB 字段），LLM 调用侧回填；`_is_trigger_condition_met` 优先锚定判定
- per-message `token_count` 缓存保留（boundary 走查只需相对大小）
- 该方式一次性覆盖全部 drift 来源（system prompt、tools、governance 替换、media 注入、tokenizer 偏差）——计量对象从"消息理论上多大"变为"真实请求实际多大"

**关联项**（usage 锚定落地后重估）：`EmergencyCompactionGovernance` 触发频率应大幅下降；届时评估将其收敛为「强制触发 `cleanup_session`（真压缩：compact 摘要 + session commit + pruned 落盘）+ 重试」，替代请求级硬裁剪（当前路径裁掉的内容无摘要、不进 pruned catalog、下一迭代全量重建后再次触发，形成 400→裁剪→重试循环）。
