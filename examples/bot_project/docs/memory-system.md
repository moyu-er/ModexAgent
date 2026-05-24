# Memory System — bot_project 记忆系统详解

## Current bot_project memory behavior

- Default archive compression now writes paired archive records with one shared
  `archive_id`: `context_archive.jsonl` for prompt injection and
  `knowledge_archive.jsonl` for Dream consolidation.
- `ArchiveStrategy` and single-summary `archive.jsonl` are no longer part of
  the default implementation. Existing `archive.jsonl` files are not read,
  migrated, or injected.
- `FullInjectionPolicy` reads only the context archive channel. `DreamEngine`
  reads only the knowledge archive channel, commits the shared archive cursor,
  and then runs paired cleanup while retaining the configured recent consumed
  pairs.

- Main memory uses full session/archive/knowledge layers. Subagent
  memory uses session-only storage by configuring `archive=None` and
  `knowledge=None`.
- Subagent compression reuses the standard
  `DefaultMemoryCompressionCoordinator` and `DefaultCommitPolicy`; there is no
  subagent-specific truncation or commit strategy.
- With `archive=None`, compression still applies the same trigger, retention
  priority, planner, and keep-ratio hard caps as main memory. It replaces
  session messages only and skips archive writes/summary generation.
- Governance runs only before LLM calls on a context copy. The bot project
  governance chain is `ToolChainRepairGovernance`,
  `PriorityBudgetGovernance`, optional `LossyContentCompactionGovernance`, and
  `FinalContextLegalityGovernance`.
- Compression is checked after session append and delegated to the shared
  coordinator. A truly unmatched open `assistant(tool_calls)` tail is kept as a
  protected suffix, while earlier complete assistant/tool history can still be
  planned, archived, and pruned. A trailing `tool` result whose call id is fully
  matched is a legal compression boundary and does not wait for the final
  assistant message.
- Consecutive user messages and assistant messages with multiple tool calls are
  supported by the shared retention/planner/governance rules.
- Subagent session memory is temporary and should be cleared when the subagent finishes.

> 基于 ModexAgent 框架代码逐项验证。本文档覆盖 bot_project 中各级记忆的
> **生成/更新/压缩/归档/检索/注入/老化清理** 全流程，以及各环节的可替换抽象层。

## 1. 架构总览

```
┌──────────────────────────────────────────────────────────────────┐
│  短期记忆 (Session)          中期记忆 (Archive)      长期记忆 (Knowledge) │
│  messages.jsonl        context_archive.jsonl   SOUL.md              │
│  全部会话消息 → 触发压缩 → LLM 双通道摘要 → ArchiveBundle → DreamEngine → USER.md │
│  旧前缀替换为摘要              条目追加写入              事实提取写入 MEMORY.md │
│  近后缀保留 (~50条)                                                      │
└──────────────────────────────────────────────────────────────────┘

检索 (每 turn 一次):
  FullInjectionPolicy.assemble()
    → Knowledge (优先100/90) → Archive (70) → Compression summary (40)
    → Session messages (经 ToolMessageFilterStrategy 过滤)
    → 注入 system prompt，ReAct 循环中固定不变
```

## 2. 可替换抽象层（全部有 ABC）

下方每一行均可通过构造函数注入替换。bot_project 使用的是 `→` 右侧的实现：

| 组件 | ABC (抽象) | bot_project 实际使用 | 替换途径 |
|------|-----------|-------------------|---------|
| Session 层 | `SessionMemoryManager` | `ScopedSessionMemoryManager` | 手动构造 `MemoryLayerSet` |
| Archive 层 | `ArchiveMemoryManager` | `ScopedArchiveMemoryManager` | 同上 |
| Knowledge 层 | `KnowledgeMemoryManager` | `ScopedKnowledgeMemoryManager` | 同上 |
| 压缩协调器 | `MemoryCompressionCoordinator` | `DefaultMemoryCompressionCoordinator` | `lifecycle_policy` 参数 |
| 压缩触发 | `CompressionTriggerPolicy` | `DefaultCompressionTriggerPolicy` | `trigger=` 参数 |
| 归档生成 | `ArchiveGenerationStrategy` | **`DualLLMArchiveGenerationStrategy`** (LLM) | `archive_generation=` 参数 |
| 消息分类 | `MessageCompactionPolicy` | `ConservativeCompactionPolicy` | `compaction=` 参数 |
| 边界策略 | `BoundaryPolicy` | `ToolChainBoundaryPolicy` | `boundary=` 参数 |
| 提交策略 | `CommitPolicy` | `DefaultCommitPolicy` | `commit=` 参数 |
| 知识整合 | `ConsolidationEngine` | `DreamEngine` | 整个 engine 替换 |
| 注入策略 | `MemoryInjectionPolicy` | Main:`FullInjectionPolicy` Peer/Sub:`RestrictedInjectionPolicy` | `injection_policy=` |
| 注入过滤 | `InjectionFilterStrategy` | `ToolMessageFilterStrategy` | `filter_strategy=` |
| 生命周期 | `MemoryLifecyclePolicy` | `DefaultMemoryLifecyclePolicy` | `lifecycle_policy=` |
| 上下文治理 | `ContextGovernance` | `CompositeGovernance` (4 个策略链) | `governance` 参数 |
| 知识检索 | `KnowledgeSearchStrategy` | `FullDumpKnowledgeStrategy` | Knowledge 层 `search_strategy=` |
| 历史检索 | `HistorySearchStrategy` | `RecentFirstHistorySearch` | Archive 层 `search_strategy=` |

**唯一硬编码点**：`MemoryLayerFactory` 直接实例化了上面三个 `Scoped*` 类。但可绕过 Factory，手动构造 `MemoryLayerSet` 传给 `DefaultMemorySystem`。

## 3. 短期记忆 (Session) — 生成与压缩

### 3.1 生成：消息如何写入

```
用户发送消息 → Pipeline._process_message_locked()
  → 构建 ContextState (含 ScopedMessageHistory)
  → ReActAgent.run() 执行推理循环：
      每次 append(assistant/tool 消息) → ScopedMessageHistory.append()
        → session.add_messages()       ← 写入 messages.jsonl (追加)
        → lifecycle.on_messages_added() ← 检查是否需要压缩
  → turn 结束后 context_manager.save()
```

### 3.2 压缩触发

```
DefaultCompressionTriggerPolicy.should_compress():
  all_msgs_count = len(session.get_all_messages())   ← 全量存储消息数
  if all_msgs_count > max_messages (50) → 触发 MESSAGE_COUNT
  冷却: all_msgs_count - last_compression < cooldown_messages → 跳过
```

触发比较的是**全量存储消息数**（不是可见窗口大小）。`SessionMemoryConfig.max_messages` 仅控制 `get_visible_messages()` 的尾部窗口大小，不影响触发。

### 3.3 压缩执行

```
maybe_compress():
  1. 读取 all_msgs (全量，非 visible 子集)
  2. ConservativeCompactionPolicy.decide_all() → 每条消息分类为 SUMMARIZE
     (包括 tool/tool_call，保留工具上下文)
  3. prune_count = len(all_msgs) - trigger_max_messages
  4. ToolChainBoundaryPolicy.find_prune_boundary()
     → 三层保护: KEEP_RAW 不裁剪 + tool 链不切割 + min_tail_keep
     → boundary 可能因保护而小于 prune_count (保留更多)
  5. prefix = all_msgs[:boundary] → DualLLMArchiveGenerationStrategy.generate() → LLM 双通道摘要
  6. suffix = all_msgs[boundary:] → 保留在 session
  7. DefaultCommitPolicy.commit():
     a. 完整 pair → archive.append_bundle() 写入 context_archive.jsonl + knowledge_archive.jsonl
     b. 空摘要/"(nothing)"/"(no semantic content)" → 跳过 archive 写入
     c. 始终调用 replace_messages_if_revision(suffix) → **物理替换全量**
        → messages.jsonl 变为 ~50 条 (suffix)
     d. 更新 .last_compression 冷却标记
```

### 3.4 Tool chain 完整性保证

```
ToolChainBoundaryPolicy 第 80-95 行:
  逐条扫描消息:
    如果是 assistant+tool_calls → 找到对应 tool results 链
    如果 boundary 落在链中间 → boundary 缩小到链开始位置
    从头重新扫描 (缩小后可能影响前面的链)
  结果: 工具链要么完整保留，要么完整压缩，永不被切割。
```

### 3.5 压缩比例

无固定比例。由 `max_messages` 直接决定：
- `keep ≈ max_messages` (受 tool chain 保护可能略多)
- `prune ≈ total - max_messages`
- `budget_ratio` 仅用于 `TokenBudgetGovernance` (上下文 token 预算)，**与压缩触发无关**

## 4. 中期记忆 (Archive) — 归档与检索

### 4.1 生成

```
压缩过程中:
  DefaultCommitPolicy.commit():
    generation 返回完整 pair → ArchiveWrite(CONTEXT) + ArchiveWrite(KNOWLEDGE)
                          → ScopedArchiveMemoryManager.append_bundle()
                            → context_archive.jsonl + knowledge_archive.jsonl
    generation 返回空 writes → 跳过写入 (不产生垃圾 Archive 条目)
```

### 4.2 检索

```
FullInjectionPolicy._inject_archive():
  entries = system.get_history_entries(context, limit=20, query=user_message)
    → 有 query: RecentFirstHistorySearch.search() → 关键词评分排序
    → 无 query: archive.get_recent(limit) → 最近条目
  → 过滤空标记 (nothing, no semantic content, source=empty, semantic_count=0)
  → PromptSection("历史对话摘要", priority=70)
```

**注意**：旧的 `ArchiveStrategy` / `SummaryStrategy` ABC 已移除。压缩路径现在使用 `ArchiveGenerationStrategy` 协议，默认实现为 `DualLLMArchiveGenerationStrategy`。

### 4.3 老化清理

```
DefaultMemoryMaintenancePolicy.scan_once():
  Archive retention:
    - max_entries 超限 → 保存最新 N 条 (save_logs 覆盖)
    - max_age_days 超限 → 删除过期条目
  触发: 后台扫描循环 (默认每 300s)
```

## 5. 长期记忆 (Knowledge) — 整合与更新

### 5.1 DreamEngine (Archive → Knowledge)

```
触发: _dream_background_loop(interval=300s)
  1. archive.get_unprocessed(cursor="dream") → 未处理 Archive 条目
  2. 过滤空条目: _is_meaningful_entry() 拒绝 "(nothing)" / "(no semantic content)"
     以及 source="empty" / semantic_count=0
  3. Phase 1: SummarizerAgent.analyze(PROMPT_FACT_EXTRACTION)
     → 提取 [FILE] 格式的事实声明
     → [SKIP] 表示无新信息
  4. Phase 2: SummarizerAgent.summarize(PROMPT_MEMORY_UPDATE)
     → 生成 JSON MemoryUpdate 指令数组
  5. apply_update() 写入 SOUL.md / USER.md / MEMORY.md
  6. commit_cursor() 推进处理标记 (始终推进，避免毒丸批次)
```

### 5.2 Knowledge 更新模式

| 模式 | 操作 | 说明 |
|------|------|------|
| `append` | 文件末尾追加新行 | 添加全新事实 |
| `replace_text` | 按原文匹配替换 | 精确修正 (如位置变更) |
| `section_replace` | 替换整个章节 | 重写某一部分 |
| `incremental` | 小增量添加 | 无法精确匹配时使用 |

**更新是全量评估**：DreamEngine Phase 1 同时收到 `existing_memories` (当前文件内容) + `new_entries` (新 Archive 条目)。LLM 基于两者差异生成更新指令，而非仅追加新事实。

### 5.3 检索

```
FullInjectionPolicy._inject_knowledge():
  knowledge = system.retrieve_knowledge(context, query)
    → FullDumpKnowledgeStrategy.retrieve()
      → 全量返回 SOUL/USER/MEMORY (截断到 max_tokens=2000)
      → 当前实现忽略 query 参数
  → SOUL/USER → PromptSection(priority=100)
  → MEMORY   → PromptSection(priority=90)
```

### 5.4 整合 (文件过大时)

```
KnowledgeMemoryManager._consolidation_fn:
  → SummarizerAgent.summarize(PROMPT_KNOWLEDGE_CONSOLIDATION)
  → 精简文件内容，合并重复，去冗余
```

## 6. 上下文注入 (每 turn 一次)

### 6.1 Main Agent 注入顺序 (FullInjectionPolicy)

```
Priority 100: SOUL.md, USER.md      ┐
Priority  90: MEMORY.md             │ 从 Knowledge 层读取
Priority  70: Archive 历史摘要       │ 按 query 搜索或最近条目
Priority  60: Provider blocks       │ 插件静态块 (mem0 等, 当前未启用)
Priority  50: Provider prefetch     │ 插件预取 (当前无 provider)
Priority  40: Compression summary   │ 本 session 压缩摘要
Priority  30: Auto-compact summary  │ 空闲压缩摘要
消息列表:     Session visible messages (经 ToolMessageFilterStrategy 过滤)
```

- 预算裁剪: 若 `max_system_prompt_tokens` 设值 → 从低 priority 开始丢弃
- 默认 `max_system_prompt_tokens=None` → 不裁剪
- 注入发生在 `MemorySystemContextManager.load()` 中 → **每 turn 一次**
- ReAct 循环中 system prompt 固定，不重复查询 Knowledge/Archive

### 6.2 Peer/Subagent 注入 (RestrictedInjectionPolicy)

只注入 Session messages，无 Knowledge/Archive/Provider。

## 7. 上下文治理 (每次 LLM 调用前)

`CompositeGovernance` 链，按序在消息列表**副本**上执行 (不修改持久化数据):

```
1. ToolChainRepairGovernance
   → 删除孤儿 tool result (无对应 tool_calls 声明)
   → 补全缺失 tool result (有 tool_calls 声明但无对应 result)

2. PriorityBudgetGovernance
   → 基于 retention priority 排名选择消息
   → 高优先级消息（user_input > agent_input > assistant_final > ...）优先保留
   → 总 token 超 budget 时丢弃低优先级消息

3. LossyContentCompactionGovernance
   → 对超长消息进行确定性截断，保留头部内容
   → 标记 `meta_context_lossy=True`，记录原始字符数和截断原因
   → 仅作用于 LLM 输入副本，不写入持久化存储

4. FinalContextLegalityGovernance
   → 删除孤儿 tool result（无对应 tool_calls 声明）
   → 确保消息格式符合 LLM API 要求
```

## Bot Project Defaults

The bot project uses `compaction.boundary: priority_input`. Human user messages
are the highest task anchors. `role=agent` messages from subagent
communication are also anchors, but rank below human user input.

Agent messages are stored with their source prefix, for example:

`[From Agent office-expert]`

The internal role remains `agent`; the framework converts it to `user` only at
the final LLM API boundary.

## 8. 老化清理汇总

| 层 | 机制 | 触发 |
|----|------|------|
| Session | 压缩后 `replace_messages_if_revision` 物理替换 | 消息数 > max_messages |
| Session | 空闲压缩 (auto_compact) | 空闲 > idle_threshold_seconds |
| Archive | `save_logs` 截断到 max_entries | maintenance.scan_once() 定时扫描 |
| Archive | 按 max_age_days 删除过期条目 | 同上 |
| Knowledge | `stale_days` 阈值 (MEMORY.md) | maintenance 扫描 |
| Knowledge | SOUL/USER 为永久文件，不清除 | 不适用 |
| Subagent | `MemorySystem.clear()` | 任务完成后 |

## 9. 与参考项目的关键差异

| 行为 | nanobot | hermes-agent | ModexAgent |
|------|---------|-------------|------------|
| 旧消息处理 | cursor 跳过，保留在 session | 替换为摘要 | 物理替换 (replace_messages_if_revision) |
| 工具入摘要 | 格式化全部消息给 LLM | Phase1 压缩后 Phase3 LLM 总结 | SUMMARIZE + _format_messages 压缩 |
| Archive 生成 | Consolidator LLM → archive | 结构化 summary 替换中间消息 | DualLLMArchiveGenerationStrategy LLM → context_archive.jsonl + knowledge_archive.jsonl |
| 上下文注入 | 系统 prompt + 近期 history | 系统 prompt + compressed list | Priority 排序 + 预算裁剪 |
| 长期记忆 | Dream Phase1/2 → SOUL/USER/MEMORY | MemoryManager + Provider | DreamEngine Phase1/2 → SOUL/USER/MEMORY |

## 10. 配置参考 (bot_config.yml memory 段)

```yaml
memory:
  main:
    short_term:
      max_messages: 50           # 超过此值触发压缩
      auto_compact: true # DualLLMArchiveGenerationStrategy (LLM) 生成 Archive 摘要
    retention:
      priority_order:
        - system_critical
        - user_input
        - agent_input
        - assistant_final
        - tool_chain_structure
        - tool_result_recent
        - assistant_intermediate
        - tool_result_old
        - low_value_noise
      recent_tool_result_count: 3

    compaction:
      policy: "conservative"     # 消息分类: 全部 SUMMARIZE
      boundary: "priority_input" # 优先保留用户/Agent 输入锚点
      high_value_tools: [...]    # 高价值工具结果可纳入摘要
    long_term:
      enabled: true
      init_defaults: true        # 自动创建 SOUL/USER/MEMORY
    governance:                  # 上下文预处理链
      enabled: true
      tool_chain_repair: true
      token_budget:
        enabled: true
        budget_ratio: 0.5
        safety_buffer: 1024
      lossy_compaction:
        enabled: true
        tool_result_head_chars: 1200
        assistant_head_chars: 1200
        agent_head_chars: 2000
        user_head_chars: 4000
    auto_compact:
      idle_threshold_seconds: 1800
      keep_recent_messages: 8
    dream_engine:
      interval: 300
  subagents:
    short_term: {max_messages: 20, max_tokens: 4000, auto_compact: false}
```
