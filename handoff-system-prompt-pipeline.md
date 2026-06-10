# Handoff: System Prompt Pipeline 设计与规划

> 生成时间: 2026-06-10
> 目的: 记录当前对话中的设计/规划过程和对现有实现的理解，供后续 session 继续

---

## 当前状态

**已完成**: 设计文档 (spec) + 实施计划 (plan) 均已写入项目，尚未开始编码实现。

**产出物**:
- 设计文档: `docs/superpowers/specs/2026-06-10-system-prompt-pipeline-design.md`
- 实施计划: `docs/superpowers/plans/2026-06-10-system-prompt-pipeline.md`

---

## 设计过程关键决策记录

以下是设计过程中经过讨论和权衡后的决策点，后续实现者需要理解这些"为什么"：

### 1. 为什么需要这个设计？

当前 ReAct 循环中，system prompt 在 `MemorySystemContextManager.load()` 中一次性拼接为 `str`，整个 turn 内冻结。如果 turn 中发生 memory cleanup/compression（清理/压缩），archive 摘要和 pruned 目录不会更新到 prompt 中。

### 2. 为什么是 Provider 模式而不是其他方案？

讨论了三种方案:
- **A) 各 provider 自行维护 dirty 状态** ← 选中
- B) Pipeline 统一管理 version 号 → 变更逻辑泄漏到 pipeline 层，耦合高
- C) 每次都重新获取无缓存 → 长 turn 中累积 I/O 成本

### 3. 为什么版本号检测不依赖外部触发？

用户明确要求: "不能依赖 cleanup 时触发状态修改"。Provider 必须能独立判断是否过期，通过内部 `_last_version` 与底层存储的当前版本号对比。

### 4. 刷新策略的划分依据

| Provider | 策略 | 理由 |
|---|---|---|
| Archive | **必须刷新** | cleanup/压缩的直接产物 |
| Pruned | **必须刷新** | cleanup/压缩的直接产物 |
| Knowledge | **永不刷新(react内)** | mid-turn 人格/事实突变会让 LLM 困惑 |
| Skill | **永不刷新** | 用户明确要求 |
| Experience | 可扩展(默认static) | 未来可能需要 |
| Runtime | 自动(日期变化) | 无状态 |
| Base | 永不 | 静态配置 |

### 5. Pipeline 为什么挂在 ctx_mgr 上？

讨论了三个位置:
- A) AgentContext → 太重
- **B) MemorySystemContextManager** ← 选中，ctx_mgr 已经是 prompt 组装负责人
- C) AgentRuntime → runtime 是 execution-oriented，pipeline 依赖 data 层

### 6. 每轮 Turn 都重建 Provider 对象

用户确认: "每次 react 进入时这些对象都是重新构建的"。版本号缓存只在 **同一 turn 的 iteration 之间** 有效，跨 turn 靠 `_last_version = None` → 首轮强制刷新保证正确性。

### 7. Subagent 兼容性

缺失的 provider (no archive, no knowledge) 根本不加进 pipeline 列表，无 null 问题。`RestrictedInjectionPolicy` 保留给 subagent 的 session messages。

---

## 对当前实现的理解

### System Prompt 构造流程 (当前代码)

```
MemorySystemContextManager.load()  (framework/memory/system.py:176)
  └── 5 层拼接为 system_prompt: str:
      1. Runtime metadata     ← _format_runtime_info()        每次动态生成
      2. Base system prompt   ← self.base_system_prompt        静态配置
      3. Memory layers        ← injection_policy.assemble()    knowledge/archive/pruned/providers
      4. Experiences          ← experience_manager.build_prompt()
      5. Skills               ← skill_manager.build_prompt()
  └── 返回 ContextState(system_prompt=str, history=MessageHistory)
```

第 3 层由 `FullInjectionPolicy` (主agent) 或 `RestrictedInjectionPolicy` (subagent) 负责。

### ReAct 循环中的消息构建 (当前代码)

```
LLMNode._build_messages()  (framework/agents/react/nodes/llm.py:148)
  1. if ctx.system_prompt: → 添加 system message (冻结字符串)
  2. messages.extend(await ctx.to_messages()) → 从 history 获取
  3. governance.apply(messages) → TokenBudget/Microcompact/URB 每轮重算
```

**ctx.system_prompt 是冻结的，整个 turn 内不变。**

### 各 Memory 层的版本号来源 (已确认)

| 层 | 版本号来源 | 现有能力 |
|---|---|---|
| DirArchiveStorage | `str(max_archive_id)` from `list_archives(limit=1)` | 已有，不需要新增 |
| PrunedManager | `str(max_entry_id)` from `read_index()` | 需要封装 `get_version()` |
| KnowledgeManager | 不使用版本号(react内永不刷新) | 不需要新增(除非非react场景) |
| ExperienceManager | 不使用版本号(默认static) | 不需要新增(除非未来扩展) |
| SkillManager | 不使用版本号(永不刷新) | 不需要新增 |

### URB (User Retention Buffer)

- **不在 system prompt 中**，在 governance 层 (`UserRetentionBufferInjectionGovernance`)
- 每轮 iteration **已经是实时刷新**的 (通过 `get_entries()` 从存储读取)
- 只需改进 XML 描述文字，让模型理解"近期被清理的历史对话"和"未回复的 user 消息"

### Cleanup 触发时机

- `cleanup_session()` 通过 `DefaultMemorySystem._run_cleanup()` 调用
- 触发路径: Turn 结束后 `on_session_end` 回调 / 后台 Dream Scan / Shutdown
- **当前不会在 react turn 内触发**
- `ensure_within_budget()` 是空操作 (no-op)

---

## 实施计划概览

详见 `docs/superpowers/plans/2026-06-10-system-prompt-pipeline.md`，10 个 Task 分 5 个 Phase:

1. **Foundation** (Task 1-2): SystemPromptProvider ABC + SystemPromptPipeline
2. **Static Providers** (Task 3): 9 个 Provider 实现
3. **Dynamic Dependencies** (Task 4-5): PrunedManager.get_version() + 集成测试
4. **Integration** (Task 6-8): ContextState → ctx_mgr.load() → LLMNode 全链路
5. **URB & Cleanup** (Task 9-10): URB XML 描述 + 端到端验证

---

## 后续 Session 的建议技能

1. **`/subagent-driven-development`** — 按计划逐步实现，每个 Task 独立 subagent
2. **`/test-driven-development`** — 每个 Task 都有 TDD 流程（先写测试，再实现）
3. **`/requesting-code-review`** — 完成 Phase 4 (Integration) 后做一次整体 review
4. **`/verification-before-completion`** — 实现完成后运行完整测试套件验证

---

## 关键文件索引

| 文件 | 角色 | 注意事项 |
|---|---|---|
| `framework/memory/system.py:176` | `MemorySystemContextManager.load()` — prompt 组装入口 | 主要改造点 |
| `framework/core/context.py:27` | `ContextState` — 需加 `system_prompt_pipeline` 字段 | 保持 backward compat |
| `framework/agents/react/nodes/llm.py:148` | `LLMNode._build_messages()` — 使用 pipeline | 需加 pipeline 优先逻辑 |
| `framework/core/agent.py` | `AgentContext` — 需加 `system_prompt_pipeline` 字段 | 在 pipeline.py 中赋值 |
| `framework/pipeline/pipeline.py:558` | `_build_runtime_and_context()` — 连接 context_state 到 agent_context | 赋值 pipeline 引用 |
| `framework/memory/context_governance.py:345` | `URBInjectionGovernance.apply()` — 改 XML 描述 | 独立于 pipeline |
| `framework/memory/pruned/manager.py` | `PrunedManager` — 需加 `get_version()` | 版本号封装 |
| `framework/memory/injection/full_injection.py` | `FullInjectionPolicy` — 拆解为 providers | 保留做渐进迁移 |
| `framework/memory/injection/restricted_injection.py` | `RestrictedInjectionPolicy` — subagent 用 | 不变 |

---

## 用户偏好提醒

- 用户使用中文沟通，代码/标识符/commit message 用英文
- 强调 type safety，使用 ABC 而非 Protocol
- 要求 plan 文档在每步完成后刷新 checkbox 状态
- 实现中如有变更需在 plan 文档中记录 Deviation
