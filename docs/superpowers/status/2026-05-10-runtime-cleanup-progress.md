# Runtime State Governance — 执行进度与待办

Date: 2026-05-10（已执行） + 后续规划

对照文档：
- 设计：`docs/superpowers/specs/2026-05-09-runtime-state-governance-design.md`（7 个 Phase）
- 执行计划：`docs/superpowers/plans/2026-05-10-complete-approval-migration-and-cleanup.md`（12 个 Task）

---

## 已完成（4 commits）

```
380ad2a refactor: complete approval migration to TurnSnapshot
2f056de refactor: migrate hooks from ctx.metadata to runtime state custom
a4fb97b refactor: migrate hooks and interceptors from ctx.metadata to typed state
855fa97 refactor: remove AgentContext.metadata, migrate remaining consumers to typed state
6f0c77d refactor: retire ReActRuntime, use AgentRuntime throughout
```

### Phase 1: Approval Migration Docs & Cleanup（Task 1-5）

| Task | 内容 | 状态 |
|------|------|:--:|
| 1 | `AGENTS.md` deny_as_cancel → ApprovalDenyPolicy.CANCEL_TURN | ✅ |
| 2 | EOF 空行/尾空格修复 | ✅ |
| 3 | approval + runtime + react 回归测试 | ✅ |
| 4 | 遗留符号扫描（未系统执行） | ❌ 跳过 |
| 5 | commit approval migration | ✅ |

### Phase 2: Remove metadata/extensions（Task 6-9）

| Task | 内容 | 状态 |
|------|------|:--:|
| 6 | 全量 `ctx.metadata`/`ctx.extensions` 消费者清点（54+15） | ✅ |
| 7 | 迁移 hooks → typed state（6 个 hook） | ✅ |
| 7b | 类型安全整改：`TurnCustomKey(StrEnum)` 替代裸字符串，移除 `getattr()` | ✅ |
| 8 | 迁移 interceptors → typed state（4 个 interceptor） | ✅ |
| 9 | 删除 `AgentContext.metadata` 字段 | ✅ |
| 9 | 删除 `AgentContext.extensions` 字段 | ❌ 保留——见下 |

**为什么 `extensions` 没删？** 当时 `ReActRuntime.from_context()` 仍从 `ctx.extensions.pop()` 读数据，所以分两阶段处理。现在 ReActRuntime 已删除，可以删了。

### Phase 3: Retire ReActRuntime（Task 10）

| 子任务 | 内容 | 状态 |
|--------|------|:--:|
| | 删除 `framework/agents/react/runtime.py` | ✅ |
| | `agent.py` 不再 import ReActRuntime | ✅ |
| | `assembler.py` 返回 `AgentRuntime` | ✅ |
| | `nodes/*.py` 全部迁完 | ✅ |
| | `llm.py` 移除 `pending_injector`/`memory_context` 使用 | ✅ |
| | `agent_session.py` | ❌ 仍有 ReActRuntime 注释引用和旧模式代码 |
| | `control/runtime.py` | ❌ 未检查 |
| | 3 个测试文件被 `--ignore` | ❌ 见下 |

**被跳过的测试文件：**

| 文件 | 原因 |
|------|------|
| `tests/unit/agents/react/test_runtime.py` | 直接测试已删除的 `ReActRuntime` 类，编译失败 |
| `tests/unit/agents/react/test_assembler.py` | 测试 `RuntimeAssembler` → 返回类型变为 `AgentRuntime`，需更新 |
| `tests/unit/bot_project/test_bot_project_runtime_wiring.py` | 集成测试使用旧 API |

### Phase 4: Remove Compat Properties（Task 11）

| 子任务 | 内容 | 状态 |
|--------|------|:--:|
| | 删除 `checkpoint_store` property | ✅ |
| | 删除 `memory_context` property | ✅ |
| | 删除 `pending_injector` property | ✅ |
| | `assembler.py` `checkpoint_store` → `turn_store` | ✅ |
| | `pipeline/approval_renderer.py` 检查 | ❌ |
| | `core/agent_runtime_config.py` 检查 | ❌ |

### Phase 5: Final Verification（Task 12）

| 步骤 | 内容 | 状态 |
|------|------|:--:|
| 1 | unit tests — 673 passed, 1 skipped, 0 failures（不含 3 个 `--ignore`） | ⚠️ |
| 2 | bot project tests | ❌ |
| 3 | `ruff check` + `mypy` | ❌ |
| 4 | integration/e2e tests | ❌ |
| 5 | legacy symbol scan | ❌ |

---

## 设计文档 Phase 对照

| 设计 Phase | 设计意图 | 实际进度 |
|------------|----------|:--:|
| P1 | Runtime Models & Store | ✅ 完整 |
| P2 | ReAct State Migration | ✅ ctx.metadata→typed state |
| P3 | Approval Transaction Migration | ✅ 旧 store 删，走 TurnSnapshot |
| P4 | Pipeline Runtime Integration | ⚠️ pipeline 接 AgentRuntime，session 仍是旧模式 |
| P5 | Memory Checkpoint Cleanup | ❌ 重复路径可能还在 |
| P6 | Bot Project Rewire | ❌ 没碰 |
| P7 | Historical Cleanup | ⚠️ 部分完成（见下残留） |

---

## 残留/待办（按紧急度分）

### 立刻可收 — ~20 分钟

这些全部是 ReActRuntime 删除后的死代码 / 无消费者字段，可以直接删：

| # | 动作 | 文件 | 原因 |
|---|------|------|------|
| A1 | 删除 `AgentContext.extensions` 字段 | `framework/core/agent.py` | ReActRuntime 已删，最后消费者 `ctx_ext()` 已删 |
| A2 | 删除 `ctx_ext()` 函数残留 | `framework/core/agent.py` | 已经在提交 855fa97 中删了 |
| A3 | 删除 `context_extensions.py` 整个文件 | `framework/core/context_extensions.py` | 所有 ExtensionKey 常量无消费者 |
| A4 | 删除 `GraphMetaKey` 常量 | `framework/core/graph/constants.py` | 消费者全迁到 `TurnCustomKey.GRAPH_RESULT` |
| A5 | 删除 pipeline `_prebuilt_runtime` legacy 路径 | `framework/pipeline/pipeline.py` | `RuntimeAssembler` 返回 `AgentRuntime`，旧 `ReActRuntime` 路径已废弃 |
| A6 | 清理 `agent_session.py` 旧 `extensions` 构建 | `framework/session/agent_session.py` | 旧 `extension=` 传入 `AgentContext` 的代码 |
| A7 | 清理 `pipeline.py` extensions dict 残余 + `on_checkpoint` 死代码 | `framework/pipeline/pipeline.py` | `RUNTIME_CTX_MGR` 和 `ON_CHECKPOINT` 不再被读取 |
| A8 | 跑 ruff check + lint | 全局 | 确保无新增 warning |
| B1 | 删除 `test_assembler.py` 中的 ReActRuntime 导入 | `tests/unit/agents/react/test_assembler.py` | 编译错误 |
| B2 | 重写 `test_runtime.py` | `tests/unit/agents/react/test_runtime.py` | 测试已删除的类 |
| B3 | 更新 `test_bot_project_runtime_wiring.py` | `tests/unit/bot_project/test_bot_project_runtime_wiring.py` | 旧 API |

### 下次会话 — ~1-2 小时

| # | 动作 | 涉及 | 说明 |
|---|------|------|------|
| C1 | 迁移 `agent_session.py` → `AgentRuntimeServices` | `framework/session/` | 最后的旧模式消费者，需要创建 `TurnIdentity` + `AgentRuntimeServices` |
| C2 | 检查并清理 memory checkpoint 重复路径 | `framework/agents/react/agent.py`, `framework/memory/` | P5 设计意图：用 snapshot `message_delta` 替代记忆 checkpoint |
| C3 | 全量单元测试 + bot project 测试 | 全局 | 确保无回归 |
| C4 | mypy 类型检查 | 全局 | 确保 `AgentRuntime` 类型安全 |

### 需独立 plan — P6 Bot Rewire

| 动作 | 涉及文件 |
|------|----------|
| bot project 全链路接 `AgentRuntimeServices` | `examples/bot_project/bot/service/core.py` |
| bot approval 走 `TurnStateStore.list_active_turns()` | bot service, approval handler |
| 移除 bot 独立审批 workspace/store 特殊处理 | bot service, `runtime_assembler.py` |
| 更新 bot 测试 | `examples/bot_project/tests/` |

### 需独立 plan — P7 最终清理

| 动作 | 说明 |
|------|------|
| 删除所有临时 migration adapter | 设计文档要求 |
| 统一 AGENTS.md 中的旧命名 | `checkpoint_store` → `turn_store`，`metadata` → `typed state` |
| legacy symbol scan（全量） | 按 Task 4/12 的扫描列表 |

---

## 当前测试状态

```
Unit tests（不含 3 个 --ignore）:  673 passed, 1 skipped, 0 failures
Approval tests:                        全通过
Runtime tests:                         全通过
ReAct agent tests:                     全通过
Hook tests:                            全通过
Interceptor tests:                     全通过
Core tests:                            全通过

Bot project tests:    未跑
ruff check:           未跑
mypy:                 未跑
```

## 文件变更汇总

| 类别 | 数量 | 说明 |
|------|:---:|------|
| 删除 | 4 | `runtime.py`, `approval/store.py`, `approval/state.py`, `agents/react/strategy.py` |
| 新增 | ~10 | `TurnCustomKey`, `AgentRuntime`, `AgentRuntimeServices`, typed state models |
| 框架代码修改 | ~15 | agent, assembler, nodes, hooks, interceptors, pipeline, services |
| 测试修改 | ~12 | 所有 metadata/extensions 消费者 + runtime fixture |
