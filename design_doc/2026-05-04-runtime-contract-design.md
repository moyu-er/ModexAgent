# Runtime Contract — ModexAgent v0.4 Target Architecture

## 0. 文档属性

| 属性 | 值 |
|---|---|
| Status | **Target contract** — 描述 Phase 5 完成后的目标状态 |
| 产出 Phase | Phase 0 (文档与契约同步) |
| 对齐 Phase | Phase 1–5 |
| Current-state doc | `docs/current-runtime.md` (并存, Phase 5 落地后合并) |
| 前置文档 | `design_doc/recommend.md` (2026-05-02) |
| 最后更新 | 2026-05-04 |

**更新规则**: Phase 每推进一个, 更新 Section 5 对齐表中对应行的状态并 commit。

---

## 1. 七个所有权问题 (The 7 Ownership Claims)

| 谁 | 拥有什么 | 不拥有什么 |
|---|---|---|
| **Pipeline** | 平台 I/O、session 定位、runtime 装配、错误兜底 | turn 内部语义、tool 审批逻辑 |
| **ReActAgent** | turn、iteration、LLM call、tool call、resume 节点、cancel/end metadata | 平台 I/O、用户记忆 |
| **Interceptor** | 4 个 scope (TURN/ITERATION/LLM_STREAM/TOOL_CALL) 的 around_* 执行边界 | 业务状态持久、替代 Control |
| **Control** | 命令通道: cancel / inject / steer / resume / pause | 普通 message history、业务记忆 |
| **Approval** | 策略检查 (classify → solicit) + suspend / resume 流 | 自身不做持久化 (RuntimeStateStore 负责) |
| **MemorySystem** | 对话记忆、用户/agent 知识、归档 | Runtime checkpoint / approval state / control command |
| **RuntimeStateStore** | suspend / resume 恢复快照、approval pending state | Memory / conversation history |

### 1.1 Invariants (每条 = 一个不可违反的约束)

**Pipeline**:
- Must never own turn-level semantics (iteration count, tool batch routing, resume node)
- Must never directly call `MemorySystem.add_message()` / `MemorySystem.add_messages()` — always through `ContextManager` protocol
- Must never construct `ReActRuntime` / `ApprovalRuntime` directly — always through `RuntimeAssembler`

**ReActAgent**:
- Must never touch I/O adapters — only reads/writes via `AgentContext` and `Emitter`
- Must never persist approval decision — writes to `ApprovalState` (in-memory), delegates persist to `RuntimeStateStore`

**Interceptor**:
- Must never persist business memory — only transforms/meters/blocks at execution boundaries
- Must never initiate control commands — only drains them (delegates to `ControlRuntime.drain()`)

**Control**:
- Must never appear in LLM conversation history (unless explicit audit mode)
- Must never duplicate approval logic — sends commands, doesn't classify tools

**Approval**:
- Must never write to `MemorySystem` — approval state lives in `RuntimeStateStore`
- Must never re-enter `agent.run()` — resume is handled by `MessageHandler` / Pipeline
- Must converge to a single code path: `ApprovalPolicy + RuntimeStateStore + ControlCommand` — no second approval system

**MemorySystem**:
- Must never be called directly by business code (Pipeline / Agent / bot_project) — always through `ContextManager` protocol
- Layer names in CODE: `Session` / `Archive` / `Knowledge` — exactly three layers, not variable
- Alias "短期/中期/长期" may ONLY appear in doc comments and prose, never in identifiers (variable names, class names, method names, enum values)
- Must never store approval state or runtime checkpoint — those go to `RuntimeStateStore`

**RuntimeStateStore**:
- Must never feed data into LLM context — not a memory system
- Must never mix with `MemorySystem` persistence — separate storage, separate lifecycle

---

## 2. 四层运行时: 边界确认

本节是对**已落地代码**的职责复述, 不是新设计。

| 层 | 角色 | 允许 | 禁止 |
|---|---|---|---|
| **Hook** | 生命周期观察者 | 观察 event、轻量修改 payload、注入 metadata | 包裹执行、长阻塞、审批、控制流 |
| **Interceptor** | AOP 洋葱链 | 围绕 4 个 scope 包装执行: timeout、result transform、policy enforcement、control drain | 持久业务记忆、替代 Control |
| **Control** | 运行时命令面 | cancel / inject / steer / resume / pause, 在 5 个 `ControlPhase` 边界排空 | 当作 message history |
| **Approval** | 策略 + suspend/resume | classify → solicit → suspend → resume, 通过 `ControlChannel` 接收 approve/deny | 发展为第二套 checkpoint 系统 |

### 2.1 Clean / Full 模式

- **clean**: 仅 `ReActGraph + ToolManager + LLMProvider + ContextManager`。`RuntimeAssembler.assemble("clean", ...)` 移除 5 个 extension key。
- **full**: clean + `Hook + Interceptor + Control + Approval + RuntimeStateStore`。`RuntimeAssembler.assemble("full", ...)` 显式装配并 `validate()`。

禁止在节点代码中写 `if runtime.approval_workspace:` / `if runtime.interceptor_chain:` 的散落分支 — 入口 sanitize 一步到位, 节点只做 `if runtime.xxx:` no-op 短路。

---

## 3. Pipeline 目标形态: 4 模块

```
                        InputAdapter.receive()
                              │
                              ▼
┌─────────────────────────────────────────────────────────┐
│ PipelineRunner                    ~150 行               │
│ - 输入循环: await InputAdapter.receive()                │
│ - 每个 session_id 一把 asyncio.Lock                     │
│ - 委托 MessageHandler.handle()                          │
│ - 全局 error fallback / graceful shutdown               │
│ - 永不主动退出, 除非 shutdown() 或 SIGTERM              │
│                                                         │
│ async run() -> None                                     │
│ async shutdown() -> None  (idempotent)                  │
└────────────────────────┬────────────────────────────────┘
                         │ per-message delegate
                         ▼
┌─────────────────────────────────────────────────────────┐
│ MessageHandler                     ~200 行              │
│ - ContextManager.load(session_id)                       │
│ - agent.run(ctx)                                        │
│ - 捕获 GraphInterrupt → 委托 ApprovalRenderer           │
│ - 捕获 AgentControlError / CancelledError → log + return│
│ - ContextManager.save(session_id, messages, metadata)   │
│ - OutputAdapter.flush()                                 │
│                                                         │
│ async handle(ctx: AgentContext, msg: InputMessage)      │
│   -> None                                               │
└──────┬──────────────────────────────┬───────────────────┘
       │ uses (assembled once)        │ on GraphInterrupt
       ▼                              ▼
┌──────────────────────┐  ┌──────────────────────────────┐
│ RuntimeAssembler     │  │ ApprovalRenderer  ~150 行    │
│ ~150 行              │  │                              │
│                      │  │ async handle_interrupt(      │
│ 唯一 Runtime service │  │   interrupt: GraphInterrupt, │
│ 装配入口 ≡ Phase 1   │  │   ctx: AgentContext          │
│ RuntimePresetBuilder │  │ ) -> ApprovalDecision        │
│                      │  │                              │
│ async assemble(      │  │ 1. 写 RuntimeStateStore      │
│   mode, services     │  │ 2. 渲染审批提示到 Output     │
│ ) -> AgentContext    │  │ 3. 等待 InputAdapter 响应    │
│                      │  │ 4. 解析 + 发 ControlChannel  │
│ - clean: sanitize    │  │ 5. 返回 decision             │
│ - full: 装配+validate│  │                              │
│                      │  │ Invariants:                  │
│ Invariants:          │  │ - 只写 RuntimeStateStore     │
│ - 全仓唯一构造       │  │ - 不重入 agent.run()         │
│   ReActRuntime/      │  │ - 幂等写                     │
│   ApprovalRuntime    │  │                              │
│   的地方              │  │                              │
└──────────────────────┘  └──────────────────────────────┘
```

### 3.1 模块依赖方向

```
PipelineRunner
  ├── MessageHandler          (per-message delegate)
  │     ├── ApprovalRenderer  (on GraphInterrupt)
  │     └── ContextManager    (load/save, protocol only)
  └── RuntimeAssembler        (assemble once at startup)
```

`MessageHandler` 不依赖 `PipelineRunner` — 可独立单元测试。
`ApprovalRenderer` 不依赖 `MessageHandler` — 可独立单元测试。
`RuntimeAssembler` 无内部依赖。

### 3.2 复用

- `RuntimeAssembler` 同时被 Pipeline 初始化、`AgentSession`、bot_project 消费
- `MessageHandler` 同时被 PipelineRunner 和 `AgentSession.process_message()` 消费

---

## 4. 消费方对齐表 (活页)

每个 Phase 完成后更新状态: `⏹ pending` → `✅ aligned` / `⚠️ partial (see notes)`.

| Phase | 消费方 | 对齐项 | 状态 |
|---|---|---|---|
| 0 | — | contract 产出, 本文 | ✅ |
| 1 | `bot_project/core.py` | 使用 RuntimeAssembler 替代手写 ReActRuntime/ApprovalRuntime 装配 | ⏹ |
| 1 | `framework/session/` | AgentSession 使用 RuntimeAssembler | ⏹ |
| 1 | `framework/pipeline/` | supports_streaming 全量清除 | ⏹ |
| 2 | `bot_project/core.py` | builders 通过 RuntimeAssembler 装配, 不直接操作 MemorySystem | ⏹ |
| 2 | `bot_project/skills/` | skills/peers 与 skills/subagents 去重 | ⏹ |
| 3 | `tests/integration/` | 4 文件 @pytest.mark.integration 打标 | ⏹ |
| 3 | `tests/unit/` | 14 个顶层散测试归位到子目录 | ⏹ |
| 3 | `pyproject.toml` | mypy disallow_untyped_defs=true, ruff target=py312 | ⏹ |
| 4 | `examples/` | 6 个 minimal_* 示例存在并可跑 | ⏹ |
| 4 | `README.md` | 更新为 minimal 优先 + bot_project 完整集成 | ⏹ |
| 5 | `framework/pipeline/` | 4 模块拆解完成, pipeline.py < 300 行 | ⏹ |

---

## 5. 验证方法

### 5.1 禁止模式 (grep checks)

每个 Phase 结束时执行, 0 命中 = 通过。

```text
# P0 — 全阶段生效
# 业务代码禁止直接构造 ReActRuntime (必须在 RuntimeAssembler 内)
grep -r "ReActRuntime(" framework/pipeline/ framework/session/ examples/ tests/ \
  --include='*.py' | grep -v 'runtime.py'

# 业务代码禁止直接构造 ApprovalRuntime
grep -r "ApprovalRuntime(" framework/pipeline/ framework/session/ examples/ tests/ \
  --include='*.py' | grep -v 'runtime.py' | grep -v 'approval/'

# 业务代码禁止直接写 MemorySystem.add_message / add_messages
grep -r "\.add_message\|\.add_messages" framework/pipeline/ examples/ \
  --include='*.py' | grep -i memory

# Phase 1 — supports_streaming 全量清除
grep -r "supports_streaming" framework/ --include='*.py'

# Memory 层命名规范 — 禁止代码标识符使用 short_term / long_term
grep -rE '\b(short_term|long_term|shortterm|longterm)\b' \
  framework/memory/ --include='*.py'
```

### 5.2 结构性测试断言

在每个模块的单元测试中加入:

```python
# 示例: RuntimeAssembler 的唯一性
def test_runtime_assembler_is_only_constructor():
    """ReActRuntime 只能在 RuntimeAssembler.assemble() 内构造。"""
    # 此测试放在 RuntimeAssembler 的测试文件中
    pass

# 示例: MessageHandler 不直接写 MemorySystem
async def test_message_handler_uses_context_manager_only():
    """MessageHandler.handle() 只通过 ContextManager 协议访问 memory。"""
    pass
```

### 5.3 活页表驱动

- Section 4 对齐表是 truth source
- 禁止模式 grep 结果是证据
- 两者交叉验证: 状态 `✅` = grep 0 命中 + 对应代码已改

---

## 6. 与 docs/current-runtime.md 的关系

| 文档 | 角色 | 生命周期 |
|---|---|---|
| `docs/current-runtime.md` | 描述**当前**代码行为 | 维持到 Phase 5 完成 |
| `design_doc/2026-05-04-runtime-contract-design.md` | 定义**目标**架构 | Phase 1-5 对齐基准 |
| Phase 5 后 | current-runtime.md 与 contract 合并为 `docs/runtime.md` | 单一真源 |

---

## 7. 消费指引

| 角色 | 该读什么 |
|---|---|
| 想 5 分钟跑通 Agent | Phase 4 `examples/minimal_cli_react/` |
| 想接新平台 (如飞书/Slack) | Section 3 (Pipeline + Adapter) + `framework/pipeline/adapters.py` |
| 想看今天代码怎么跑 | `docs/current-runtime.md` |
| 想看目标形态 | 本文 |
| 想改 Runtime / 加新拦截器 | 本文 Section 1 + Section 2 + `framework/agents/react/runtime.py` |
| 想加 Memory provider | `framework/memory/README.md` |
| 想理解 bot_project 怎么用框架 | Phase 2 完成后读 `examples/bot_project/AGENTS.md` |

---

## 8. Phase 0 附带修复清单 (0.2–0.5)

同 PR #1 提交, 纯文档修复:

| ID | 内容 | 验证 |
|---|---|---|
| 0.2 | `CLAUDE.md`: `framework/memory/managers/` → `framework/memory/layers/`; `agent_docs/` 引用标记 `[removed]` | grep 0 命中 |
| 0.3 | `AGENTS.md:84-90` 编号 4-4 bug → 4/5 | 肉眼 |
| 0.4 | `pyproject.toml`: ruff `target-version = "py312"` | 与 `requires-python` 一致 |
| 0.5 | `design_doc/recommend.md` 末尾追加 status 备注表 | Section 9 |

### 8.1 Phase 0.5 (拆出, 独立 PR)

| ID | 内容 | 验证 |
|---|---|---|
| 0.6 | `MessageRole` 双定义合并到 `core/types.py`, 删除 `core/constants.py` 版本, 改全仓 import | mypy + pytest 全绿 |

---

## 9. recommend.md Status 备注

> 以下为对 `design_doc/recommend.md` 的状态审计 (2026-05-04), 仅标注产出物现状, 不修改原文。

| recommend.md 主张 | 当前状态 | 证据 / commit |
|---|---|---|
| §1 Pipeline 不拥有 turn 语义 | ✅ 已基本落地 | ReActAgent 拥有 turn/iteration; `pipeline.py` 仍待拆 (Phase 5) |
| §2.1 Hook/Interceptor/Control/Approval 职责边界 | ✅ 已落地 | `framework/hook/`, `framework/interceptor/`, `framework/control/`, `framework/approval/` 四层独立 |
| §2.2 Approval = policy + suspend/resume + runtime state + control command | ✅ 已落地 | `22f1749` ApprovalClassifier + ApprovalRuntime; `6df89a6` 移除 ReActRuntime.suspend_strategy |
| §2.3 clean / full 模式 | ✅ 已落地 | `825896f` ReActRuntime; `runtime.py:42-127` |
| §3.1 MemorySystem 唯一入口 | ✅ 已落地 | `MemorySystemContextManager` 存在, 22 文件引用 |
| §3.2 WorkingMemory 移除 | ✅ 已清空 | 全仓 grep 仅本文命中, 代码 0 残留 |
| §3.3 Memory 三层重命名 Session/Archive/Knowledge | ✅ 已落地 | `framework/memory/layers/{session,archive,knowledge}.py` |
| §3.4 Memory ≠ RuntimeStateStore | ✅ 已落地 | `e7fb8b3` 移除 checkpoint alias 冗余 |
| §5.1 ToolManager 不负责审批 | ✅ 已落地 | `3185df6` 删除 ToolPolicyGuardHook |
| §6.1 supports_streaming → StreamingMode | ⚠️ 半迁移 | `StreamingMode` 枚举已存在; `pipeline/adapters.py` 仍有 7 处 `supports_streaming` — Phase 1 收尾 |
| §6.2 Memory adapter → compat/ | ⏹ 不做 | 代码中无旧 adapter 需要 compat, 取消此建议 |
| §7.2 bot_project 默认 pipeline | ⏹ 待 Phase 1 | 当前 `bot_service.py:89` 写死 `pool` |
| Phase 4 minimal examples | ⏹ 待 Phase 4 | `examples/` 仅 `bot_project/`, `sandbox/`, `security/` |
| TURN/ITERATION interceptor inert | ✅ 已过时 (已落地) | `dc49b80` / `6832fc0` |
| Control drain 5 安全边界 | ✅ 已落地 | `f20a15f` |
| Pipeline decomposition | ⏳ 部分 | `a173bca` 提取了 6 个私有方法, 未拆类 (Phase 5) |
