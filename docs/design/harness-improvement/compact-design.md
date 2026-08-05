# Session Compact 设计文档

> 创建时间：2026-08-04
> 基于 grilling 讨论中达成的全部决策

---

## 一、设计目标

用 session 级 compact（结构化 LLM 摘要）替代当前缺失的 session 级压缩能力。compact 与 archive（跨会话归档）正交，各自独立工作。

### 当前问题

ModexAgent 的 session 清理（`cleanup_session`）只有跨会话 archive 记忆和 pruned 原文存储，缺少 session 级的 compaction summary——压缩后 agent 丢失被裁剪内容的上下文。当前 URB（UserRetentionBuffer）试图补偿这个缺口，但注入方式有 cache 问题且未接线。本设计用 compact summary 彻底替代 URB。

### 核心数据流

```
用户消息到达
  → ScopedMessageHistory.append()
    → session.add_messages() (持久化)
    → _run_cleanup()
      → cleanup_session() 5 phases:
        1. trigger check (token 压力 85%) → sanitize → boundary 分割
        2. compact generation (LLM 单次调用生成结构化摘要)
        3. session commit (retain [compact_summary] + [tail])
        4. pruned catalog write (topic 从 compact summary 提取)
        5. archive generation (可配置, 默认关闭)
    → on_cleanup_finished listeners

下一次 LLM iteration:
  → MemorySystemContextManager.load()
    → SystemPromptProvider pipeline 组装 system prompt
    → AgentContext.to_messages()
      → normalize_agent_messages_for_llm(): COMPACT role → ASSISTANT
      → 返回非 system 消息列表: [compact_summary(as assistant), ...tail]
    → governance (LossyContent + ToolChainRepair) 作用于 LLM copy
    → LLM 调用
```

---

## 二、决策记录

### D1: compact summary 消息角色

**决策**：新增 `MessageRole.COMPACT = "compact"`，存储用 COMPACT role，LLM 调用前映射为 `ASSISTANT`（纯文本，无 tool_calls）。

**role 映射机制**：在 `normalize_agent_messages_for_llm()` 中加简单映射表：

```python
_ROLE_MAP: dict[MessageRole, MessageRole] = {
    MessageRole.AGENT:   MessageRole.USER,       # 已有：agent → user + XML 信封
    MessageRole.COMPACT: MessageRole.ASSISTANT,  # 新增：compact → assistant
}
```

- `AGENT` 映射保留现有 XML 信封包装逻辑
- `COMPACT` 映射为简单 role 替换，不改 content（纯文本）
- 未列出的 role（system/user/assistant/tool）原样通过
- 后续如需新增其他非标准 role（如 SYSTEM_REMINDER），在映射表中加一行即可

**tool chain 安全性**：COMPACT（映射为无 tool_calls 的 assistant）不参与 sanitizer 的 tool chain 分组。`_last_tool_call_assistant_index` 只找有 tool_calls 的 assistant。orphan tool result sweep 只检查 `role == TOOL`。COMPACT 消息后面跟新的 `ASSISTANT(tool_calls) → TOOL(result)` 序列是合法的，sanitizer 不会误删。已验证无冲突。

### D2: SessionCompactorAgent 架构

**决策**：新建 `session_compactor.py`，继承 `ScopedFileAgent`，不配备任何 tool。

**继承设计**：`ScopedFileAgent._run_agent()` 中 tool 构建提取为可继承方法（如 `_build_tool_manager()`），`SessionCompactorAgent` override 该方法返回空的 `InMemoryToolManager()`。

**LLM 调用机制**：复用现有 ReAct 图引擎。无 tool 时 `LLMNode` 检查 `response.tool_calls` 为空，直接路由 `LLM → END`，单轮完成。系统提示通过 `AgentContext.system_prompt` 直接注入（不走 pipeline），用户消息通过 `ListMessageHistory` 注入——均为现有机制。

**参数**：
- `max_iterations = 3`（无工具时一轮结束，3 为安全上限）
- `temperature = 0.2`（总结性任务，低温度更稳定）
- `max_output_tokens = min(model_max_output, 8192)`（结构化模板紧凑，8K 足够覆盖复杂多步任务；model 未声明 max_output 时 fallback 到 8192）
- 非流式调用
- 走 provider 自带的 retry（瞬时错误重试）
- `reasoning_content` 已在 `AgentResult.content` 中剥离（`AgentResult.reasoning` 单独持有），`LLMNode` 在 `reasoning_content is None` 时还会调 `strip_think()` 清除 `<thinking>` 标签，双保险

**prompt 加载**：通过 `PromptRegistry` 自动加载 `prompts/compact/agent_system.md` 和 `agent_user.md`。PromptRegistry 用 `rglob("*.md")` 递归加载，key 为相对路径去掉 `.md`——`get_system("compact/agent")` 查找 key `compact/agent_system`，`get_user("compact/agent")` 查找 key `compact/agent_user`。

**model**：沿用 archive 用的同一个 `LLMProvider`，后续可扩展为独立 model 配置。

### D3: compact summary 消息位置

**决策**：compact summary 在 session 消息流中位于 system 之后、tail 之前。

压缩后 session 结构：

```
[system]
[compact summary (COMPACT role)]    ← 新生成，纯文本
[tail messages]                     ← 保留的近期完整消息，tool chain 完整
```

compact summary 是 session 的第一条非 system 消息（当存在 prev compact summary 时，它替代旧 summary 成为新的第一条）。tail 在它后面，保持完整的消息结构和 tool chain。

### D4: compact 触发与纳入范围

**触发条件**：沿用当前 `_check_trigger`——non-system tokens > `(max_context - max_output) * 0.85`。

**boundary 分割**：沿用当前 `_compute_boundary`（从尾部向前累加到 `keep_target_tokens` = `max_context * keep_ratio`，默认 0.3 即 30%）+ `_adjust_boundary_for_tool_chains`（如果 boundary 切割了 tool chain，将孤儿 tool result 推入 compact zone）。

**boundary 策略 ABC**：boundary 分割逻辑抽象为 ABC，当前实现为 tail-only（不保留 head）。后续可替换为 head+tail 实现或其他策略。ABC 定义：

```python
class BoundaryStrategy(ABC):
    @abstractmethod
    def compute_boundary(
        self, messages: list[dict], keep_target_tokens: int, estimator: TokenEstimator
    ) -> tuple[list[dict], list[dict]]:
        """返回 (compact_zone, tail)。compact_zone 是被清理的消息，tail 是保留的消息。"""
```

**不保留 head**：当前实现只分割 compact zone 和 tail，不保留 head。这与 ModexAgent 当前行为一致。

**compact zone**：boundary 之前的所有消息（system 除外），即被清理的部分。

**prev compact summary 处理**：
- compact zone 中的 prev compact summary（COMPACT role）→ **从送入 summarizer 的消息流中移除**（避免重复总结自己的旧 summary）
- prev compact summary 的文本 → 作为 `previous_summary` 参数，用 `<previous-summary>` XML 标签包裹并加说明，和其他内容一起在同一个 user message 里传给 summarizer
- compact zone 中的其他消息（user/assistant/tool/agent）→ 正常序列化为纯文本送入 summarizer
- compact zone 中如果有多条 prev compact summary（多次压缩），只取最后一条的文本作为 `previous_summary`，其余的也从消息流中移除

**tail 的 tool chain 完整性**：沿用当前 `_resanitize_keep()` 第二遍 sanitize，保证 keep 区域自洽。具体行为：
- orphan tool result（无 matching assistant tool_call）→ 移除
- 旧的未完成 assistant（tool_calls 无 result，且后面有 plain assistant）→ 移除（`STALE_INCOMPLETE_ASSISTANT_TOOL_CALLS`）
- 末尾未完成的 assistant（tool_calls 无 result，是 session 最后一个 tool-assistant，后面无 plain assistant）→ **保留**（`preserve_incomplete_tail`），标记 `has_open_tail=True`。这是 mid-turn 状态——tool 结果还没生成，下次 LLM iteration 会补充。
- governance 层（`ToolChainRepairGovernance`，`MODEL_VISIBLE_CONTEXT` 模式）会为所有未完成的 assistant backfill 合成占位 tool result（`"[Tool result unavailable]"`），确保 provider 永远不会收到 dangling tool_calls。

### D5: compact summarizer 输入格式

**送什么消息**：只送 compact zone，不送 tail。tail 原样保留在 session 里，agent 直接能看到，summarizer 不需要总结它。

**序列化格式**：纯文本，每条消息一行：
```
[User]: <content>
[Assistant]: <content>
[Assistant tool calls]: tool_name(key=value, key=value)
[Tool result]: <content, truncated to 2000 chars>
```

- user/assistant/agent 消息：`[Role]: <content>`
- assistant 有 tool_calls 时：额外输出 `[Assistant tool calls]: name(args)`
- tool 消息：`[Tool result]: <content>`
- tool output 截断到 2000 chars（超出部分截断，附加 `[\n... N more characters truncated]`）
- COMPACT role 消息（prev compact summary）：不序列化，已从消息流中移除

**不传 tools**：空 `InMemoryToolManager()`。`ReactLlmClient` 检查 `ctx.tool_manager` 为空时 `tools=None` 传给 provider，LLM 无 tool 可调。

**previous_summary**：通过 `__PREV_SUMMARY__` 占位符注入 user message（绕开 PromptRegistry 的 `xml_attr` 转义，保留 XML 标签原文），构建逻辑：

```python
if previous_summary:
    prev_block = (
        "A previous compaction summary exists. Update it with the new conversation history above.\n"
        "<previous-summary>\n"
        f"{previous_summary}\n"
        "</previous-summary>"
    )
else:
    prev_block = ""
```

transcript 通过 `__TRANSCRIPT__` 占位符注入（同样绕开转义）。

### D6: compact prompt 设计

**system prompt**（`prompts/compact/agent_system.md`）：
- "You are an anchored context summarization assistant"
- 聚焦 older context（newest turns 保留在 summary 外）
- 如果有 `<previous-summary>` 则做迭代更新（preserve still-true + remove stale + merge new）
- 诚实约束（unverified 标记）
- 按对话语言回复
- proportional to task（长任务需要细节，短任务一两句即可）

**user template**（`prompts/compact/agent_user.md`）：
- `__PREV_SUMMARY__` 占位符（previous summary block 或空）
- 结构化模板，section 顺序固定：
  - `## Objective` — 一两句话描述用户目标
  - `## Work State` — Completed / Active / Blocked 三子 section
  - `## Next Move` — 有序下一步
  - `## Relevant Files` — 文件路径及重要性
  - `## Key Decisions` — 决策及理由
- Rules：保持每个 section（即使为空）、terse bullets、保留精确路径/命令/错误字符串
- `__TRANSCRIPT__` 占位符（序列化的对话文本）

### D7: topic 从 compact summary 提取

**section 选择**：`## Objective`

**提取规则**：
1. 从 compact summary 文本中找 `## Objective` 行
2. 取该行之后、下一个 `## ` 之前的所有内容
3. 去掉 markdown bullet 前缀（`- `）、strip 空白
4. 截断到 200 chars（`topic_max_chars`）
5. 如果找不到 `## Objective` section 或内容为空 → fallback 到时间范围（当前 `_resolve_topic` 的 fallback：`"2026-08-04 10:00 ~ 2026-08-04 11:00 (50 messages)"`）

**用途**：pruned catalog 的 `PrunedIndexEntry.topic` 字段（显示在 pruned catalog XML 中，帮助 agent 翻找历史记忆）。不用于文件名（文件名由 `_generate_filename()` 从时间范围生成）。

### D8: archive 与 compact 正交

**决策**：archive 和 compact 相互独立，无直接关系。compact 不作为 archive 的输入，archive 不依赖 compact。

**archive 变更**：
- 不再生成 `index.md`（topic 由 compact 的 `## Objective` 提取）
- 只生成 `context.md`（跨会话摘要，注入 system prompt）+ `knowledge.md`（DreamEngine 消化为 core memory）
- archive 可配置，默认关闭

**archive 关闭时**（默认）：
- compact 仍正常生成
- pruned catalog 的 topic 从 compact summary 提取
- ArchiveProvider 返回空内容（不注入 system prompt）
- 无 knowledge.md 生成（DreamEngine 无输入，不行动）

**archive 开启时**：
- compact 正常生成
- archive 独立生成 context.md + knowledge.md（输入为 pruned raw messages，与 compact 无关）
- pruned catalog 的 topic 仍从 compact summary 提取（不从 archive 生成）
- ArchiveProvider 注入 context.md 内容到 system prompt
- DreamEngine 消化 knowledge.md（如果 core memory 也开启）

### D9: compact 对所有 agent 开启，archive/core 对所有 agent 默认关闭

**配置**（per-pool，通过 `MemoryConfig`）：

| 配置项 | 默认值 | 适用范围 | 说明 |
|--------|--------|---------|------|
| `compact_enabled` | `True` | 所有 agent（含 subagent） | session 级压缩，必要手段，不可关闭 |
| `archive.enabled` | `False` | 所有 agent（含 subagent） | 跨会话归档，默认关闭 |
| `core.enabled` | `False` | 所有 agent（含 subagent） | core memory（SOUL/USER/MEMORY.md），默认关闭 |

**当前状态 vs 目标状态**：

| agent 类型 | 当前 archive | 当前 core | 目标 archive | 目标 core | 目标 compact |
|-----------|-------------|----------|-------------|----------|-------------|
| main agent | `enabled=True` | `enabled=True` | `enabled=False` | `enabled=False` | `enabled=True`（新增） |
| subagent | `None` | `None` | `None`/`False` | `None`/`False` | `enabled=True`（新增） |

变更点：
- `examples/bot_project/bot/config/memory_defaults.py` 中 `main_agent_memory()` 的 `archive=ArchiveConfig(enabled=True, ...)` 改为 `enabled=False`，`core=CoreMemoryConfig(enabled=True, ...)` 改为 `enabled=False`
- `subagent_memory()` 已经是 `archive=None, core=None`，不变
- 新增 compact 配置，对所有 agent 默认 `enabled=True`

**compact 对 subagent 的行为**：subagent 是短生命周期 task worker，但同样会在 token 压力达到 85% 时触发 compact。compact summary 作为 `COMPACT` role 存入 subagent session，subagent 结束后 session 清理。archive 和 core 对 subagent 仍保持关闭。

**core memory 与 archive 的 AND 关系**：
- core memory 严格依赖 archive：`core.enabled AND archive.enabled` 都为 True 时才生效
- `core.enabled = True` 且 `archive.enabled = False` → core memory 不生效（静默降级，不报错）
- 其他组合 → core memory 不生效

**core memory 关闭的含义**：
1. `CoreMemoryProvider` 不注入（SOUL/USER/MEMORY.md 不出现在 system prompt）
2. `CoreMemoryManager` 不创建文件、不初始化
3. DreamEngine 扫描时跳过该 pool

**DreamEngine 行为**（per-workspace 单例，从 default pool 的 memory system 构建，定时轮询触发）：

当前 DreamEngine 在 `_maybe_build_dream()` 中检查 `archive_manager is None or core_memory_manager is None` → 返回 None（不构建）。archive/core 默认关闭后，`_maybe_build_dream()` 返回 None，DreamEngine 不构建、不运行。

当某个 pool 开启了 archive + core 时，需要让 DreamEngine 能发现并处理它。变更：
- `_maybe_build_dream()` 不再只从 default pool 构建，改为扫描所有 pool 找到第一个 `archive_enabled and core_enabled` 的 pool 构建
- `scan_all()` 已经只处理 `MemoryAgentRole.MAIN` 的 archive scope，subagent 被排除
- DreamEngine 不需要改为 per-pool 实例，只需在构建时选择正确的 pool
- `on_archive_generated` 回调在生产中从未接线（DreamEngine 靠定时轮询），此设计不改变这一行为

### D9.1: archive/core 关闭后的 provider 注入控制

**SystemPromptProvider pipeline gating**——`MemorySystemContextManager.load()` 中各 provider 的条件加入逻辑：

| Provider | 当前 gating | archive/core 关闭后行为 |
|----------|------------|----------------------|
| `ArchiveProvider` | `archive_config.count > 0` | `count=0` 时不加入（已有逻辑，`ArchiveInjectionConfig(count=0)` 已处理） |
| `CoreMemoryProvider` | `result.system_prompt` 非空 | **需要修改**：当 core memory 关闭时，`FullInjectionPolicy.assemble()` 应返回空 `system_prompt`（不注入 disclaimer） |
| `PrunedProvider` | `pruned_manager is not None` | 不变（pruned 默认开启） |
| `ProviderBlocksProvider` | blocks 非空时加入 | 不变 |
| `ProviderPrefetchProvider` | query 非空时加入 | 不变 |

**FullInjectionPolicy 修改**：当前 `assemble()` 总是注入 disclaimer（即使 core memory 为空），导致 `result.system_prompt` 非空，`CoreMemoryProvider` 总是被添加。修改为：
- core memory 层为 `None` 时，`_inject_core_memory()` 返回空 sections
- `_inject_disclaimer()` 只在有 core memory 内容时注入 disclaimer
- sections 为空时 `system_prompt = ""`，`CoreMemoryProvider` 不被添加

这样 archive/core 关闭后，system prompt pipeline 中不会出现 ArchiveProvider 和 CoreMemoryProvider，system prompt 更短，节省 token。

**ArchiveProvider 的 `ArchiveInjectionConfig` gating**：`pool_data.py` 中已有条件：
```python
archive_injection_config=ArchiveInjectionConfig(
    count=memory_cfg.archive.max_archive_inject,
    ...
) if memory_cfg.archive is not None and memory_cfg.archive.enabled
else ArchiveInjectionConfig(count=0)
```
archive 关闭时 `count=0`，`load()` 中 `archive_config.count > 0` 为 False，ArchiveProvider 不加入。已有逻辑正确，不需要修改。

### D10: URB 完整清理

**决策**：移除 URB 的所有实现和使用。

**移除内容**：
- `UserRetentionBufferInjectionGovernance`（context_governance.py）
- `UserRetentionBuffer` / `ScopedUserRetentionBuffer` / `UserBufferEntry`（layers/user_buffer.py, user_buffer.py）
- cleanup Phase 4（retention extraction）和 Phase 5 的 URB 持久化逻辑
- `_urb_completion_hook`（default_system.py）
- `UserRetentionBufferConfig`（layers/config.py）
- `TurnCustomKey.URB_CONTENT`（runtime/enums.py）

**URB 职责由新机制覆盖**：
- 被裁剪的 user/assistant 内容 → compact summary 覆盖
- 未完成 user 消息 → tail 保留覆盖（如果未完成，说明在 tail 范围内）
- 跨 turn 注入 → compact summary 在消息流内（COMPACT role → ASSISTANT），每 turn 天然可见

---

## 三、cleanup 流程（新设计）

```
cleanup_session()
  │
  ├─ Phase 1: trigger check + boundary
  │   ├─ _check_trigger: non-system tokens > (max_context - max_output) * 0.85
  │   ├─ sanitize all messages (PERSISTENT_SESSION mode)
  │   ├─ boundary = BoundaryStrategy.compute_boundary(messages, keep_target_tokens)
  │   │   ├─ compact_zone = boundary 之前的消息 (system 除外)
  │   │   └─ tail = boundary 之后的消息
  │   ├─ _adjust_boundary_for_tool_chains (孤儿 tool result 推入 compact_zone)
  │   └─ _resanitize_keep (第二遍 sanitize, 保证 tail 自洽)
  │
  ├─ Phase 2: compact generation (同步, 阻塞)
  │   ├─ 从 compact_zone 中移除 COMPACT role 消息, 提取 prev compact summary 文本
  │   ├─ 序列化 compact_zone 剩余消息为纯文本 (tool output 截断 2000 chars)
  │   ├─ 构建 user message: __PREV_SUMMARY__ + 模板 + __TRANSCRIPT__
  │   ├─ SessionCompactorAgent.compact(system_prompt, user_message)
  │   │   └─ 返回 compact summary 纯文本
  │   ├─ 从 compact summary 提取 ## Objective 作为 topic
  │   └─ 构建 compact summary 消息: {"role": "compact", "content": <summary>}
  │
  ├─ Phase 3: session commit
  │   ├─ backup (已有逻辑)
  │   ├─ retain_messages([compact_summary] + tail_messages)
  │   │   └─ 乐观并发 (revision check)
  │   └─ compact_summary 是 session 的第一条非 system 消息
  │
  ├─ Phase 4: pruned catalog write
  │   ├─ 写 compact_zone raw messages 到 JSONL (已有逻辑)
  │   ├─ topic 从 compact summary 的 ## Objective 提取
  │   └─ topic fallback: 时间范围 (当 ## Objective 不可用时)
  │
  └─ Phase 5: archive generation (可配置, 默认关闭)
      ├─ 如果 archive_enabled:
      │   ├─ ArchiveSummarizer 生成 context.md + knowledge.md (不生成 index.md)
      │   ├─ 写入 archive 存储 (已有逻辑, 移除 index.md 写入)
      │   └─ 触发 DreamEngine (检查 core_memory_enabled AND archive_enabled)
      └─ 否则: 跳过
```

### 压缩前后消息流对比

**压缩前**（第二次 compact 触发时）：
```
[system]
[prev compact summary (COMPACT role)]    ← 上次 compact 生成
[assistant reply]
[user msg]
[assistant (tool_calls)] → [tool result] → [tool result]
[assistant reply]
[user msg]                                ← trigger 在此之后触发
```

**压缩后**：
```
[system]
[compact summary (COMPACT role)]          ← 新生成，替代 prev compact + compact zone
[tail messages]                           ← 30% token，完整 tool chain
```

**agent 看到的 LLM 消息**（role 映射后）：
```
[system prompt]
[compact summary (as ASSISTANT)]          ← COMPACT → ASSISTANT 映射
[tail messages (user/assistant/tool)]     ← 原样
```

---

## 四、文件变更清单

### 新增

| 文件 | 内容 |
|------|------|
| `src/modex_agent/agents/summarizer/session_compactor.py` | `SessionCompactorAgent` 类 |
| `src/modex_agent/memory/prompts/compact/agent_system.md` | compactor system prompt（已就位） |
| `src/modex_agent/memory/prompts/compact/agent_user.md` | compactor user template（已就位） |

### 修改

| 文件 | 变更 |
|------|------|
| `src/modex_agent/core/types.py` | 新增 `MessageRole.COMPACT = "compact"` |
| `src/modex_agent/core/message_utils.py` | `normalize_agent_messages_for_llm()` 加 COMPACT → ASSISTANT 映射 |
| `src/modex_agent/memory/cleanup.py` | 重构 cleanup_session：加 compact phase，移除 URB phase，pruned topic 从 compact 提取 |
| `src/modex_agent/agents/summarizer/scoped_file_agent.py` | tool 构建提取为可继承方法 `_build_tool_manager()` |
| `src/modex_agent/ioc/factories/memory.py` | 新增 SessionCompactorAgent 构造，传给 cleanup_session |
| `src/modex_agent/ioc/configs/memory.py` | 新增 `compact` 配置项（`CompactConfig`，默认 `enabled=True`） |
| `src/modex_agent/memory/injection/full_injection.py` | core memory 为空时不注入 disclaimer，`assemble()` 返回空 `system_prompt` |
| `src/modex_agent/memory/injection/archive.py` | archive 不再生成 index.md |
| `examples/bot_project/bot/config/memory_defaults.py` | `main_agent_memory()` 的 archive/core 改为 `enabled=False`；新增 compact 配置 |
| `examples/bot_project/bot/workspace/background.py` | `_maybe_build_dream()` 扫描所有 pool 找 archive+core 开启的 |

### 删除

| 文件/内容 | 原因 |
|----------|------|
| `src/modex_agent/memory/context_governance.py` 中 `UserRetentionBufferInjectionGovernance` | URB 清理 |
| `src/modex_agent/memory/user_buffer.py` | URB 清理 |
| `src/modex_agent/memory/layers/user_buffer.py` 中 `ScopedUserRetentionBuffer` | URB 清理 |
| `src/modex_agent/memory/layers/config.py` 中 `UserRetentionBufferConfig` | URB 清理 |
| `src/modex_agent/ioc/configs/memory.py` 中 `UserRetentionConfig` | URB 清理 |
| `src/modex_agent/runtime/enums.py` 中 `TurnCustomKey.URB_CONTENT` | URB 清理 |
| `src/modex_agent/memory/default_system.py` 中 `_urb_completion_hook` | URB 清理 |
| `MemoryLayerConfigSet` 中 `user_retention` 字段 | URB 清理 |
| `MemoryLayerFactory.subagent_session_isolated()` / `session_only()` 中 `user_retention` 参数 | URB 清理 |

---

## 五、待后续设计的问题

| 问题 | 优先级 | 备注 |
|------|--------|------|
| tool output 持久化预清理（50% 阈值中间层） | 中 | 廉价延迟全量压缩，可单独实施 |
| archive 异步化 | 中 | session commit 后异步触发 archive |
| 压缩后强制刷新 ArchiveProvider/PrunedProvider version | 中 | 清除 TTL 缓存，让 agent 立即看到新内容 |
| Anthropic cache_control 断点 | 高 | 独立于 compact，可单独实施 |
