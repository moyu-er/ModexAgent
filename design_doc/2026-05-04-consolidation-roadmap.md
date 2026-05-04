# ModexAgent Consolidation Roadmap (v0.4)

> **读者定位**: 不熟悉当前实现细节的新开发者。
> **粒度**: 战略级 — 每个 Phase 说清楚目标、理由、核心交付物。具体实施细节在各自 design doc 和 plan 中。
> **前置阅读**: `design_doc/recommend.md` (原始建议)、`design_doc/2026-05-04-runtime-contract-design.md` (目标契约)。

---

## 背景: 为什么要做收口

ModexAgent 当前有大量能力 (Agent / Tool / Memory / Pipeline / Hook / Interceptor / Control / Approval / Plugin / Multi-Agent), 每个子系统都已独立实现。但在快速迭代中出现了几个问题:

1. **多套实现并存** — bot_project 内部有两段几乎相同的 Runtime 装配代码, Pipeline 内部和 AgentSession 内部有重复的 load/save/error 逻辑。
2. **文档与代码脱节** — CLAUDE.md 写的 `memory/managers/` 实际目录是 `memory/layers/`; `agent_docs/` 目录不存在但还被引用。
3. **部分迁移半途** — `supports_streaming` 到 `StreamingMode` 迁移做了一半, docs 说完成了但代码还有旧字段。
4. **规范有设计、无牙齿** — mypy `disallow_untyped_defs = false` 让类型检查松了一档; `@pytest.mark.integration` 规则写了但全部测试没打标。

**好消息**: recommend.md 提出的 P0 项 (四层运行时、clean/full mode、ApprovalRuntime 独立、Control drain、Memory 三层重命名、RuntimeStateStore 分离) 已经在最近 30 个 commit 中落地。现在的工作是把余下的碎片收完、文档统一、示例瘦身、Pipeline 精简化。

**核心原则**: 不向后兼容, 不留 compat 包袱, 旧错误代码直接删, bot_project 功能不退化但内部 wiring 要换。

---

## Phase 总览

```
Phase 0  文档与契约同步          0.5d  纯文档, 零风险
      ↓
Phase 1  框架层收口              2-3d  消重复, 补半迁移
      ↓
Phase 2  示例项目瘦身            2-3d  上交框架 → bot_project 只留 QQ 特化
      ↓
Phase 3  工程化补牙              1-2d  与 Phase 1/2 可并行
      ↓
Phase 4  示例矩阵                3-5d  新用户 5 分钟上手
      ↓
Phase 5  Pipeline 拆解           5-7d  1078 行 → 4 个类
```

### 依赖关系

- Phase 1 依赖 Phase 0 (contract 定义了 RuntimeAssembler 的接口)
- Phase 2 依赖 Phase 1 (bot_project 调用 RuntimeAssembler 替代手写装配)
- Phase 3 可与 Phase 1/2 并行 (改测试标记和 mypy 不冲突)
- Phase 4 依赖 Phase 1/2 (minimal 示例用稳定后的 API)
- Phase 5 依赖 Phase 1 (RuntimeAssembler 抽走后 Pipeline 变化面缩小)

---

## Phase 0: 文档与契约同步

**目标**: 写一份权威的"谁管什么"契约 + 修掉文档中的过时路径和错误。

**理由**: 后续所有 Phase 需要一份基准文档来对齐。contract 定义了 7 个 ownership claim + Pipeline 目标形态的 4 个模块分解, Phase 1-5 全部按这份 contract 验证自己是否正确。

**核心交付物**:
- `design_doc/2026-05-04-runtime-contract-design.md` — 目标架构契约
- `CLAUDE.md` / `AGENTS.md` / `pyproject.toml` / `recommend.md` 修复

**不做什么**: 不动任何代码 (除 Phase 0.5 的 MessageRole 合并)

---

## Phase 1: 框架层收口

**目标**: 消除 bot_project 与 framework 之间的最大重复源 + 完成 supports_streaming 迁移。

**理由**: bot_project 的 `core.py` 里有两段几乎相同的 `ReActRuntime + ApprovalRuntime + ControlRuntime` 手写装配代码。它们本质是框架能力, 应该下沉为 `RuntimeAssembler` (即 contract 中 Pipeline 4 模块之一)。这样 bot_project、AgentSession、测试 helpers 三个消费方统一用同一个入口。

**核心交付物**:
- `framework/agents/react/runtime.py` 新增 `RuntimeAssembler` 类
- bot_project `core.py` 删 ~150 行重复装配, 改用 RuntimeAssembler
- `framework/pipeline/adapters.py` 等 8 个文件的 `supports_streaming` → `StreamingMode` 全量迁移
- bot_project 默认 mode 不改 (保留 pool), 仅 wiring 清洁

**成功标准**: `grep -r "ReActRuntime(" examples/ framework/session/ --include='*.py' | grep -v runtime.py` 返回空。

---

## Phase 2: 示例项目瘦身

**目标**: bot_project 只保留 QQ 特化代码, 框架可复用逻辑上交。

**理由**: 当前 `bot/service/core.py` 1209 行, `builders.py` 645 行, 里面有大量与框架重复的装配/校验/错误处理。Phase 1 上交了 RuntimeAssembler 后, 继续把剩余的通用多 agent / approval / inbox / 插件装配下沉, bot_project 缩到只保留 QQ Adapter + 业务配置 + 业务插件。

**核心交付物**:
- `bot/service/core.py` 1050 → ~600 行
- `AgentBuilderMixin` Mixin → 组合模式降低耦合
- `skills/peers/` 与 `skills/subagents/` 4 对重复 SKILL.md 合并

**约束**: bot_project 外部行为不变 (pool 模式、QQ 收发、approval flow 全部保持), 只改内部 wiring。

**成功标准**: bot_project 里找不到"不是 QQ 特化"的通用框架装配代码。

---

## Phase 3: 工程化补牙

**目标**: 让规范有 CI 牙齿 — 写的规则能被机器检查。

**理由**: 当前 `@pytest.mark.integration` 写了没人打标、mypy `disallow_untyped_defs = false` 让类型检查松了一档、14 个测试文件散落在顶层没有遵循 mirror 规则。这些修复不改变功能, 但让后续维护者不敢乱写。

**核心交付物**:
- 4 个 integration 测试文件打 `@pytest.mark.integration`
- 14 个顶层散测试归位到 `tests/unit/{control,hook,interceptor,...}/`
- `mypy disallow_untyped_defs = true` + 附属的无注解函数补齐
- ruff 加 `ANN` 规则 (排除 `ANN101/102`)
- `pyproject.toml` ruff target 统一 py312

**成功标准**: `pytest -m "not integration"` 真的不跑 integration; `mypy framework/ --strict` 全绿。

---

## Phase 4: 示例矩阵

**目标**: 新用户不用读 bot_project 也能在 5 分钟内跑通 Agent。

**理由**: 当前 `examples/` 只有 bot_project (QQ Bot 集成) + sandbox + security, 学习曲线极陡。需要一组最小示例覆盖框架的核心能力。

**核心交付物**:

| 示例 | 目的 | 目标行数 |
|---|---|---|
| `examples/minimal_cli_react/` | 跑通 ReActAgent | 20-50 |
| `examples/minimal_tool_call/` | ToolManager + tool node | 50-80 |
| `examples/minimal_memory_system/` | MemorySystemContextManager | 50-80 |
| `examples/minimal_approval/` | ApprovalRuntime + suspend/resume | 80-120 |
| `examples/minimal_interceptor/` | 4 scope around_* 用法 | 50-80 |
| `examples/multi_agent_pool/` | AgentPool 不绑定 QQ | 100-150 |

`README.md` 更新为 minimal 优先 + bot_project 完整集成。

**成功标准**: 新用户 clone 后 `pip install -e . && python examples/minimal_cli_react/main.py` 能跑。

---

## Phase 5: Pipeline 拆解

**目标**: `framework/pipeline/pipeline.py` 1078 行 → 4 个类。

**理由**: 当前 Pipeline 混合了输入循环、消息处理、Runtime 装配、审批渲染四类职责。contract Section 3 已经定义了 4 模块的接口和职责, Phase 5 是实现。

**核心交付物**:
- `PipelineRunner` — 输入循环 + session 锁 (从 pipeline.py 拆出)
- `MessageHandler` — 单条消息生命周期 (从 pipeline.py 拆出)
- `RuntimeAssembler` — Phase 1 已写好 (Phase 5 只是确认接口)
- `ApprovalRenderer` — suspend → render → resume (从 pipeline.py 拆出)

**成功标准**: `pipeline.py` < 300 行; 4 个新模块各自可独立单元测试; bot_project 和所有现有测试继续通过。

---

## 设计决策记录

| 决策 | 结论 | 理由 |
|---|---|---|
| bot_project pool 模式 | 保留, 不降级 | 用户明确要求 |
| bot_project 外部功能 | 不变 | 用户明确要求 |
| `framework/compat/` 目录 | 不做 | WorkingMemory 已清空; 不存在需要 compat 的旧 adapter |
| Old Memory adapter | 不处理 | 代码库中不存在, recommend.md 提的但后来已清掉 |
| Memory 三层命名 | 代码: Session/Archive/Knowledge; 短期/中期/长期仅在注释使用 | 用户明确要求, 写入 contract invariant |
| mypy strict 开放节奏 | 渐进: Phase 3 打开, 按模块补齐 | 用户确认 |
| 向后兼容 | 不做, 不留 shim | 项目早期, CLAUDE.md 也明确"prefer breaking changes over accumulating cruft" |
| Phase 0.5 MessageRole 合并 | 独立 PR, 与 contract 分开 | 涉及多文件 import 改名, 单独便于 review 和回滚 |
