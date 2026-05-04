# ModexAgent 收口路线 — 剩余 Phase 2-5 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 完成 runtime contract 中 Phase 2-5 的剩余任务, 将 ModexAgent 从当前 "Phase 1 收口完毕" 推进到 "Phase 5 Pipeline 拆解完成"。

**Architecture:** 按 contract Section 3 的 4 模块目标形态 (PipelineRunner / MessageHandler / RuntimeAssembler / ApprovalRenderer) 逐步落地。Phase 2 继续消重, Phase 3 补工程化, Phase 4 造示例, Phase 5 动 Pipeline 本身。

**Tech Stack:** Python 3.12+, pytest, ruff, mypy

---

## 状态总览

| Phase | 状态 | 主要产出 |
|---|---|---|
| 0 | ✅ | runtime-contract.md, doc sync, MessageRole 合并 |
| 1 | ✅ | RuntimeAssembler, StreamingMode 迁移, bot_project slim |
| 2 | ⏹ | AgentSession 接入 RuntimeAssembler, skills 去重, builders 消重 |
| 3 | ⏹ | integration test marks, test 归位, mypy disable_untyped=false, ruff ANN |
| 4 | ⏹ | 6 minimal_* 示例, README 更新 |
| 5 | ⏹ | Pipeline 4 模块拆解 |

---

## Phase 2: 深层消重 + AgentSession

### Task 2.1: AgentSession 接入 RuntimeAssembler

**Background:** `framework/session/agent_session.py:374-381` 手动构建 extensions dict 并在 `ReActAgent.run()` 内通过 `from_context()` 装配 runtime。现在 RuntimeAssembler 已可用, AgentSession 应该统一用 assembler。

**Files:**
- Modify: `framework/session/agent_session.py`

**Plan:** 将 `_build_extensions()` 或等效方法改为调用 RuntimeAssembler, 消除手写 ReActRuntime 装配代码。

**Verification:**
```bash
grep -rn "ReActRuntime(" framework/session/ --include='*.py'
# Expected: 0
pytest tests/unit/session/ -q
# Expected: all pass
```

---

### Task 2.2: skills/peers 与 skills/subagents 去重

**Background:** `skills/peers/{docx,pdf,pptx,xlsx}` 与 `skills/subagents/{docx,pdf,pptx,xlsx}` 内容几乎相同, 靠 `CompositeSkillSource(merge_strategy="last_wins")` 掩盖。bot_project `builders.py:254-258` 写死了两边都加载。

**Files:**
- Modify: `examples/bot_project/bot/service/builders.py`
- Delete: 4 对重复 SKILL.md 之一

**Approach:** 保留 `skills/peers/` 中的 4 个技能, 删除 `skills/subagents/` 中的同名副本, 改 `_get_subagent_skill_manager()` 从 `skills/peers/` 加载。如果 peer 和 subagent 必须用不同的技能集, 则只在 `skills/peers/` 中维护一份, subagent 通过覆写加载。

**Verification:**
```bash
diff -r skills/peers/docx skills/subagents/docx
# Expected: subagents/docx deleted or identical
pytest examples/bot_project/tests/ -v
# Expected: all pass
```

---

### Task 2.3: builders.py 消重 — Memory 构造统一

**Background:** `builders.py` 中 `_create_subagent_memory()` / `_create_peer_memory()` / `_get_context_manager()` 等 memory 构造方法有重复逻辑。应该统一为通过 `MemorySystemContextManager` 作为唯一生产推荐路径。

**Files:**
- Modify: `examples/bot_project/bot/service/builders.py`
- Verify: `examples/bot_project/bot/service/core.py`

**Approach:** 抽象一个 `_create_scoped_memory_system(agent_role)` 方法, subagent/peer/main 分别传不同 role enum, 消除三份重复的 create_memory_system + MemorySystemContextManager 装配。

**Verification:**
```bash
grep -c "create_memory_system" examples/bot_project/bot/service/builders.py
# Expected: 1 (single factory call, not 3)
pytest examples/bot_project/tests/ -v
# Expected: all pass
```

---

## Phase 3: 工程化补牙

### Task 3.1: Integration 测试打标

**Files:**
- Modify: `tests/integration/test_qq_bot_service.py`
- Modify: `tests/integration/multi_agent/test_bus_pool_e2e.py`
- Modify: `tests/integration/multi_agent/test_multi_agent_communication_and_intervention.py`
- Modify: `tests/integration/multi_agent/test_session_isolation.py`

**Approach:** 在 4 个文件的 `import` 区之后加 `pytestmark = pytest.mark.integration`。这是 pytest 推荐的模块级标记方式, 比每个 test 函数加 decorator 更简洁。

**Verification:**
```bash
grep -rln "@pytest.mark.integration\|pytestmark.*integration" tests/integration/
# Expected: 4
pytest -m "not integration" tests/ -q
# Expected: skips integration tests
```

---

### Task 3.2: 14 个顶层散测试归位

**Files to move:**
```
tests/unit/test_interceptor_chain.py        → tests/unit/interceptor/test_chain.py
tests/unit/test_hooks.py                    → tests/unit/hook/test_hooks.py
tests/unit/test_control_channel.py          → tests/unit/control/test_channel.py
tests/unit/test_control_channel_v2.py       → tests/unit/control/test_channel_v2.py
tests/unit/test_control_event_bus.py        → tests/unit/control/test_event_bus.py
tests/unit/test_control_event_bus_v2.py     → tests/unit/control/test_event_bus_v2.py
tests/unit/test_tiered_tool_approval.py     → tests/unit/approval/test_tiered.py
tests/unit/test_tool_approval_interceptor.py → tests/unit/interceptor/test_approval_interceptor.py
# ... (其余 6 个类似, 按模块归入对应子目录)
```

每个 move 后需更新 `__init__.py` (如目录无则创建)。

**Verification:**
```bash
ls tests/unit/test_*.py
# Expected: No such file (empty)
pytest tests/unit/ -q
# Expected: same pass count
```

---

### Task 3.3: mypy disallow_untyped_defs = true

**Background:** `pyproject.toml:97` 当前 `disallow_untyped_defs = false`, 软化了 mypy strict。需打开并修复暴露的裸函数。

**Files:**
- Modify: `pyproject.toml` (1 行)

**Approach:** 先打开, 跑 mypy 看报多少错, 然后分批修。预计 ~50-100 个 `no-untyped-def` 错误。按模块分批: 先修 `framework/core/`, 再修 `framework/agents/`, 等等。每批独立 commit。

**Verification:**
```bash
mypy framework/ --strict 2>&1 | grep "no-untyped-def" | wc -l
# Expected: 0
```

---

### Task 3.4: ruff 加 ANN 规则

**Files:**
- Modify: `pyproject.toml` (ruff.lint.select 加 ANN, 排除 ANN101/ANN102 for self/cls)

**Approach:** 加 `"ANN"` 到 ruff select 列表, 加 `"ANN101", "ANN102"` 到 ignore。先跑看报多少, 然后分批修。与 3.3 协同: mypy 管类型正确性, ruff ANN 管注解存在性。

**Verification:**
```bash
ruff check framework/ 2>&1 | grep ANN | wc -l
# Expected: 0
```

---

## Phase 4: 示例矩阵

### Task 4.1–4.6: 6 个 minimal_* 示例

每个示例 ≤ 100 行, 自包含, 零外部依赖。

| ID | 示例路径 | 核心展示 | 文件 |
|---|---|---|---|
| 4.1 | `examples/minimal_cli_react/` | 20-50 行跑通 ReActAgent (clean mode) | `main.py`, `README.md` |
| 4.2 | `examples/minimal_tool_call/` | ToolManager + ReAct tool node | `main.py`, `README.md` |
| 4.3 | `examples/minimal_memory/` | MemorySystemContextManager | `main.py`, `README.md` |
| 4.4 | `examples/minimal_approval/` | ApprovalRuntime + suspend/resume | `main.py`, `README.md` |
| 4.5 | `examples/minimal_interceptor/` | 4 个 scope 的 around_* | `main.py`, `README.md` |
| 4.6 | `examples/multi_agent_pool/` | AgentPool (不依赖 QQ) | `main.py`, `README.md` |

每个示例的 `main.py` 必须在 `< 5 秒` 内跑通 (用 mock LLM)。

**Verification:**
```bash
for d in examples/minimal_* examples/multi_agent_pool; do
    echo "=== $d ===" && python $d/main.py && echo "OK"
done
# Expected: all OK
```

---

### Task 4.7: README 更新

**Files:**
- Modify: `README.md`

**Approach:** 5 分钟 minimal → 15 分钟 tool_call → 30 分钟 memory → bot_project 完整集成。把 bot_project 从唯一入口降级为终极参考。

---

## Phase 5: Pipeline 拆解

### Task 5.1: 拆出 ApprovalRenderer

**Background:** `pipeline.py` 中 `_handle_approval_interrupt()` 等方法承载了 suspend → render → resume 流。拆为独立 `ApprovalRenderer` 类。

**Files:**
- Create: `framework/pipeline/approval_renderer.py`
- Modify: `framework/pipeline/pipeline.py`

**Approach:** 提取所有 GraphInterrupt handling + InputAdapter 等待 + RuntimeStateStore 写逻辑到 `ApprovalRenderer.handle_interrupt(interrupt, ctx) -> ApprovalDecision`。

---

### Task 5.2: 拆出 MessageHandler

**Background:** `pipeline.py` 中 `_process_message_locked()` 承载了单条消息的完整生命周期。拆为独立 `MessageHandler` 类。

**Files:**
- Create: `framework/pipeline/message_handler.py`
- Modify: `framework/pipeline/pipeline.py`
- Verify: `framework/session/agent_session.py` (复用 MessageHandler)

---

### Task 5.3: 重构 PipelineRunner

**Background:** 拆出 MessageHandler 和 ApprovalRenderer 后, `pipeline.py` 只剩下输入循环 + session 锁 + 全局兜底。重命名为 `PipelineRunner`。

**Files:**
- Modify: `framework/pipeline/pipeline.py` (大幅瘦身)
- Modify: `framework/pipeline/__init__.py` (导出 PipelineRunner)

**Verification:**
```bash
wc -l framework/pipeline/pipeline.py
# Expected: < 300
pytest tests/unit/pipeline/ -q
# Expected: all pass
```

---

### Task 5.4: 全量回归 + contract 闭合

**Verification:**
```bash
pytest tests/unit/ -q --tb=line
# Expected: 1236+ passed
pytest examples/bot_project/tests/ -v
# Expected: all pass
grep -rn "supports_streaming" framework/ --include='*.py'
# Expected: 0 (Phase 3 已全量清除)

# 更新 contract 对齐表
# Phase 2 rows → ✅
# Phase 3 rows → ✅
# Phase 4 rows → ✅
# Phase 5 rows → ✅
```

---

## 实施顺序 (依赖图)

```
Phase 2 ──→ Phase 3 (可并行)
   │            │
   └── Phase 4 ─┘
           │
           ▼
       Phase 5
```

- Phase 2 和 3 可并行 (一个改 bot_project/AgentSession, 一个改 tests/pyproject)
- Phase 4 依赖 Phase 2 (minimal_* 需要确认 AgentSession/ApprovalRenderer 接口稳定)
- Phase 5 依赖 Phase 2-4 全部完成 (Pipeline 拆解是所有消费方收拢后的最后一刀)

---

## 估计工作量

| Phase | 估时 | 风险 |
|---|---|---|
| 2 | 2-3 天 | 中 — AgentSession 改动可能影响 bot_project pool 模式 |
| 3 | 1-2 天 | 低 — 机械性工作为主, mypy 修错量不确定 |
| 4 | 2-3 天 | 低 — 全新文件, 无耦合 |
| 5 | 3-5 天 | 高 — 一次大重构, 需要全量测试护体 |
| **Total** | **8-13 天** | |
