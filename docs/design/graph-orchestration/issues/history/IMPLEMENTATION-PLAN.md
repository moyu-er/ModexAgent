# Graph Orchestration Implementation Plan

> 基于 [PRD.md](PRD.md) 的 wayfinder map(11 个 ticket 全部关闭)制定。
> 各 ticket 决议详见 [issues/](issues/) 目录。

## 一、实现范围

### 纳入实现(本次)

| 来源 | 实现项 | 位置 |
|------|--------|------|
| ticket 07 | deliver/submit 投递模型,替代 transition/command/_compile_routing | modex_graph + modex_agent |
| ticket 08 | GraphSpec + NodeFactory/Registry + StateFactory/Registry + GraphSpecCompiler + TopologyValidator | modex_graph |
| ticket 04 | GraphInstance 抽象 + InterruptPolicy ABC | modex_graph |
| ticket 10 类别 1 | CheckpointData 新增字段 + load_latest 接通 + graph_instance_id | modex_graph |
| ticket 10 类别 2 | Node 级状态抽象 ABC + 通用实现 | modex_graph |
| ticket 10 类别 3 | 图定义持久化 + 生命周期状态机 + 外部控制接口 + bot 工厂 + 四表 schema | modex_graph + modex_agent |
| ticket 02 | 通用 Node 类型(FunctionNode/GraphAsNode/DelayNode/HumanInputNode)+ AgentNode | modex_graph + modex_agent |
| ticket 03 | 未投递检测(scheduler _submit 时检查 + max_retry 重跑) | modex_graph |
| ticket 11 | React 图适配新架构 | modex_agent/agents/react |
| ticket 05 | taskId = graph_instance_id(env 注入) | modex_agent |

### 延后实现(明确延后)

| 来源 | 实现项 | 延后原因 |
|------|--------|---------|
| ticket 06 | modexctl kb --by-task | 业务功能增强,框架+图实现完成后接入 bot 后再做 |
| ticket 09 | 预定义拓扑模板(Pipeline/Star/Supervisor/Swarm) | ROI 低,deliver/submit 让大部分模式可直接构造,实现过程中按需补 |

### 不实现(Out of scope)

- AdaptiveNode / LLM 自主生成图(后续 phase,只保留接口)
- KnowledgeBase 完整 RAG
- 替代 ReActTurnRunner / multi_agent star topology
- 图级 MVCC 轮次(待 Node 级 MVCC 落地后评估)
- hook 主动检测"未投递"(待 after_dispatch 事件)
- 动态图拓扑 v2(运行时修改已编译图)

## 二、依赖关系图

```
                    ┌─────────────────┐
                    │  P0: 框架基础    │
                    │  (modex_graph)   │
                    └────────┬────────┘
                             │
           ┌─────────────────┼─────────────────┐
           ▼                 ▼                  ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │ P1: deliver  │  │ P1: 工厂体系  │  │ P1: GraphInst│
    │ /submit 模型 │  │ (Node/State  │  │ + 持久化层   │
    │ + _execute   │  │  Factory)    │  │ (四表 schema)│
    └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
           │                 │                  │
           ▼                 ▼                  │
    ┌──────────────┐  ┌──────────────┐          │
    │ P2: 通用 Node│  │ P2: GraphSpec│          │
    │ 类型实现     │  │ Compiler     │          │
    │ (Function/   │  │ + Validator  │          │
    │  Delay/ etc) │  └──────┬───────┘          │
    └──────┬───────┘         │                  │
           │                 ▼                  │
           │          ┌──────────────┐          │
           │          │ P2: 生命周期 │◄─────────┘
           │          │ 状态机 + 外部│
           │          │ 控制 + 恢复  │
           │          └──────┬───────┘
           │                 │
           ▼                 ▼
    ┌──────────────────────────────┐
    │ P3: AgentNode + bot 工厂     │
    │ (modex_agent)                │
    └──────────────┬───────────────┘
                   │
                   ▼
    ┌──────────────────────────────┐
    │ P3: React 图适配 (ticket 11) │
    │ (modex_agent/agents/react)   │
    └──────────────┬───────────────┘
                   │
                   ▼
    ┌──────────────────────────────┐
    │ P4: 集成验证                  │
    │ (bot_project 端到端)         │
    └──────────────────────────────┘
```

## 三、执行阶段与任务分解

### Phase 0:前置准备

| 任务 | 说明 | 位置 |
|------|------|------|
| 0.1 Snowflake ID 引入 | 安装 `snowflake-id` 包,定义 ID 生成器 ABC + 默认实现 | modex_graph |
| 0.2 持久化 schema 定义 | 四表 DDL(graph_specs/graph_instances/node_states/deliver_states) + 迁移脚本 | modex_graph |

**依赖**:无
**产出**:ID 生成器 + 建表脚本

### Phase 1:框架核心(P0 + P1,并行)

#### 1A:deliver/submit 投递模型(来源 ticket 07)

| 任务 | 说明 | 位置 |
|------|------|------|
| 1A.1 Node._execute / execute 双方法接口 | `_execute`(框架固定):integrate → execute → _submit。`execute`(node 可覆盖) | modex_graph/node.py |
| 1A.2 _deliver / deliver 方法 | `_deliver`(框架):累积 + 持久化(ABC)。`deliver`(node 可覆盖):默认 append | modex_graph/node.py |
| 1A.3 _submit / submit 方法 | `_submit`(框架):按 next_node 分组派发。`submit`(node 可覆盖):默认分组整合 | modex_graph/node.py |
| 1A.4 InputIntegrator ABC + 默认实现 | integrate(list[IntegratedPayload]) → IntegratedInput。默认:list 拼接 | modex_graph |
| 1A.5 IntegratedPayload / IntegratedInput 结构体 | frozen Pydantic,含 source_node/content/metadata | modex_graph |
| 1A.6 deliver 持久化 ABC + 通用实现 | ABC:累积/查询/状态。通用实现:内存对象(LinearScheduler)+ SQLite 表(ParallelScheduler) | modex_graph |
| 1A.7 移除 transition/command/_compile_routing | NodeResult 移除 transition/command 字段。_compile_routing 移除。ctx.dispatch 移除。state_update 保留(只用于图级状态) | modex_graph |
| 1A.8 未投递检测(ticket 03) | _submit 时检查是否有累积 deliver。无累积 + 无默认下游 → 错误反馈重跑(max_retry per node 默认 3,超限 raise RoutingError) | modex_graph/scheduler/ |

**依赖**:Phase 0
**阻塞**:Phase 2 全部,Phase 3 全部

#### 1B:工厂体系(来源 ticket 02 + 08)

| 任务 | 说明 | 位置 |
|------|------|------|
| 1B.1 NodeSpec / EdgeSpec / GraphSpec | frozen Pydantic,完全可序列化。EdgeSpec 无 reason 字段 | modex_graph |
| 1B.2 NodeFactory ABC + NodeRegistry | register(type, factory, config_model) / create(type, config) → Node | modex_graph |
| 1B.3 StateFactory ABC + StateRegistry | create_state() / state_schema() / restore_state(data) | modex_graph |
| 1B.4 SimpleStateFactory | 预注册 GraphState 子类的简单包装 | modex_graph |
| 1B.5 DynamicStateFactory | 从内嵌 StateSchema 动态构建 GraphState 子类 | modex_graph |
| 1B.6 StateSchema / StateFieldSpec | 可序列化的 state 结构描述 | modex_graph |

**依赖**:Phase 0
**阻塞**:Phase 2(GraphSpecCompiler 需要 Factory/Registry)

#### 1C:GraphInstance + 持久化层(来源 ticket 04 + 10)

| 任务 | 说明 | 位置 |
|------|------|------|
| 1C.1 GraphInstance 抽象 | graph_instance_id / parent_instance_id / parent_node / graph_spec / compiled_graph / 运行状态 | modex_graph |
| 1C.2 CheckpointData 扩展 | 新增 activated_sources / instance_seq / iteration_count 字段 | modex_graph |
| 1C.3 graph_instance_id 管理 | 取代 run_id,外部传入,Snowflake ID,持久化 | modex_graph/scheduler/ |
| 1C.4 CheckpointStore.load_latest 接通 | 从 checkpoint 重建内存状态(main_state/pending/activated_sources/completed/instance_seq/iteration_count) | modex_graph |
| 1C.5 恢复流程 | load_latest → 重建 → _recheck_pending 推导 → 重新 dispatch。不倒推 completed | modex_graph/scheduler/ |
| 1C.6 GraphInstance 持久化 ABC + SQLite 实现 | save/load/load_by_status CRUD。四表统一 schema | modex_graph |
| 1C.7 Node 级状态抽象 ABC + 通用实现 | ABC:read/snapshot/restore/状态查询,内存缓存优先。通用实现:SimpleNodeState(单状态) | modex_graph |
| 1C.8 DispatchStore 恢复路径 | 已有持久化,接通恢复查询 | modex_graph |

**依赖**:Phase 0
**阻塞**:Phase 2(生命周期状态机需要 GraphInstance)

### Phase 2:编排层(P2,Phase 1 完成后)

| 任务 | 说明 | 位置 | 依赖 |
|------|------|------|------|
| 2.1 GraphSpecCompiler | resolve StateFactory → 构建 Graph 拓扑 → TopologyValidator → compile。不创建 state(state 在 GraphInstance 级别) | modex_graph | 1B |
| 2.2 TopologyValidator | 纯确定性:环检测 + node 白名单 + max_depth/max_nodes + START→END 可达性 + 结构完整性 | modex_graph | 1B |
| 2.3 生命周期状态机 | GraphInstanceStatus(StrEnum):running/paused/stopped/crashed/completed/failed。status 字段在 graph_instances 表 | modex_graph | 1C |
| 2.4 InterruptPolicy ABC + CrashPolicy | 图层面收到 GraphInterrupt 后的行为。默认 CrashPolicy:全部暂停 + checkpoint + 等外部恢复 | modex_graph | 1A, 1C |
| 2.5 外部控制接口 | ControlCommand 扩展(PAUSE_GRAPH/STOP_GRAPH/RESUME_GRAPH/DELIVER_TO_NODE)。REST + CLI 收敛同路径 | modex_agent | 1C, 2.3 |
| 2.6 恢复两种类型 | 故障恢复(自动,只捡 crashed)+ 手动恢复(resume(),适用于 paused/stopped) | modex_graph | 1C.5, 2.3 |
| 2.7 通用 Node 类型 — FunctionNode | 包装确定性函数(同步/异步)为 Node | modex_graph | 1A |
| 2.8 通用 Node 类型 — GraphAsNode | CompiledGraph 作为 Node(已有,适配 _execute/_deliver/_submit) | modex_graph | 1A |
| 2.9 通用 Node 类型 — DelayNode | 延迟/节奏控制 | modex_graph | 1A |
| 2.10 通用 Node 类型 — HumanInputNode | 等待人工输入(与 GraphInterrupt 相关) | modex_graph | 1A, 2.4 |

**依赖**:Phase 1 全部(1A + 1B + 1C)
**阻塞**:Phase 3

### Phase 3:业务层(P3,Phase 2 完成后)

| 任务 | 说明 | 位置 | 依赖 |
|------|------|------|------|
| 3.1 AgentNode + AgentNodeFactory | 包装 agent 完整调用为 Node。execute 内部阻塞 await agent.run / 可创建子图实例。双输入模型:submit 触发 + inbox agent 自己拉取 | modex_agent | 2.7-2.10 |
| 3.2 ReactStateFactory | ReActTurnState 的 StateFactory 业务实现 | modex_agent | 1B.3 |
| 3.3 taskId = graph_instance_id env 注入 | bot 工厂创建 GraphInstance 时 `MODEX_TASK_ID` env 注入。加注释说明约定 | modex_agent | 1C.3 |
| 3.4 React 图适配(ticket 11) | 4 节点 transition/Command → deliver。静态边 reason 移除。_compile_routing → _submit(Linear 简化)。GraphInterrupt/approval/resume_target 保留。ReactGraphRuntime 适配 _execute/_deliver/_submit | modex_agent/agents/react | 1A, 3.1 |
| 3.5 bot 图工厂 | 启动后加载 GraphSpec → GraphSpecCompiler → GraphInstance → 注入依赖 → GraphEngine 执行 → 提供外部控制接口 | examples/bot_project | 2.1, 2.5, 3.1 |

**依赖**:Phase 2 全部
**阻塞**:Phase 4

### Phase 4:集成验证(P4,Phase 3 完成后)

| 任务 | 说明 | 位置 | 依赖 |
|------|------|------|------|
| 4.1 React 图测试通过 | tests/unit/agents/react/ 全部通过。transition/command 不再使用。deliver/submit 正常。approval 不受影响 | tests/ | 3.4 |
| 4.2 modex_graph 架构守卫测试 | modex_graph 不 import modex_agent(架构守卫) | tests/ | Phase 1-2 |
| 4.3 端到端验证 | bot_project:前端配置图 → bot 工厂实例化 → 执行 → pause/resume → 恢复 | examples/bot_project | 3.5 |
| 4.4 持久化验证 | 图定义 CRUD + checkpoint 保存/恢复 + deliver_states 读写 | tests/ | 1C, 2.1 |

**依赖**:Phase 3 全部

## 四、关键约束

### 4.1 架构守卫

- modex_graph 不能 import modex_agent(已有架构守卫测试,Phase 1-2 所有新增代码必须遵守)
- ABC + 通用实现在 modex_graph,业务实现在 modex_agent

### 4.2 收敛规则(AGENTS.md convergence rule 1)

- deliver/submit 是唯一投递机制,不保留 transition/command/_compile_routing 作为并行路径
- _execute 是唯一节点执行入口,不新增其他执行路径
- GraphInstance 是唯一实例化路径,不新增其他实例化方式

### 4.3 类型安全(rules/type-safety.md)

- 所有跨模块结构化数据用 frozen Pydantic BaseModel + extra="forbid"
- 枚举用 StrEnum(便于扩展),不用裸字符串
- ABCs before implementations,zero Protocols

### 4.4 ADR 更新

实现完成后需要更新/新增的 ADR:
- 扩展 ADR-0034 D19:CheckpointData 新增字段 + load_latest + graph_instance_id + Node 级状态抽象
- 新增 ADR:deliver/submit 投递模型(替代 transition/command)
- 新增 ADR:GraphSpec + GraphInstance + 生命周期状态机
- 更新 ADR-0033 D9.1:Preset graphs 层已实现(GraphSpec + Compiler + Factory/Registry)

## 五、Not yet specified(实现阶段视情况 graduate)

| 待办项 | 说明 | 触发条件 |
|--------|------|---------|
| Node 级状态抽象 ABC 具体接口 | read/snapshot/restore/状态查询,具体方法签名待实现阶段细化 | Phase 1C.7 实现时 |
| transition/command 迁移 | 现有 modex_graph 代码迁移到 deliver/submit | Phase 1A.7 实现 |
| agent 自主生成图(AdaptiveNode) | 后续 phase,GraphSpec 已为 LLM 生成保留接口(str|StateSchema 可序列化) | 后续 |
| 图级 MVCC 轮次 | 待 Node 级 MVCC 落地后评估 | Node 级 MVCC 实现 |
| hook 主动检测"未投递" | after_dispatch 新事件 | 可选增强 |
| 动态图拓扑 v2 | 运行时修改已编译图 | 后续 |

## 六、预估工作量

| Phase | 任务数 | 预估 | 关键路径 |
|-------|--------|------|---------|
| Phase 0 | 2 | 0.5 天 | 前置,阻塞全部 |
| Phase 1 | 22(1A:8 + 1B:6 + 1C:8) | 5-7 天(三路并行,每路 2-3 天) | 最长路径 1C(8 任务) |
| Phase 2 | 10 | 3-4 天 | 依赖 Phase 1 全部完成 |
| Phase 3 | 5 | 2-3 天 | 依赖 Phase 2 |
| Phase 4 | 4 | 1-2 天 | 依赖 Phase 3 |
| **合计** | **43** | **11-16 天** | |

Phase 1 的三路(1A/1B/1C)可并行,关键路径取决于最慢的一路(1C 8 任务)。实际可分配给多个 agent 并行执行。

## 七、引用

- [PRD.md](PRD.md) — wayfinder map,11 个 ticket 决策摘要
- [issues/01-modex-graph-capability-assessment.md](issues/01-modex-graph-capability-assessment.md) — 能力评估
- [issues/02-node-abstraction-design.md](issues/02-node-abstraction-design.md) — Node 封装
- [issues/03-undelivered-detection-and-wakeup.md](issues/03-undelivered-detection-and-wakeup.md) — 未投递检测
- [issues/04-graph-nesting-execution-model.md](issues/04-graph-nesting-execution-model.md) — 图套图
- [issues/05-taskid-scope-and-semantics.md](issues/05-taskid-scope-and-semantics.md) — taskId
- [issues/06-shared-knowledge-base-interface.md](issues/06-shared-knowledge-base-interface.md) — 知识库(延后)
- [issues/07-long-running-node-execution.md](issues/07-long-running-node-execution.md) — deliver/submit
- [issues/08-declarative-graph-spec.md](issues/08-declarative-graph-spec.md) — GraphSpec
- [issues/09-predefined-topology-templates.md](issues/09-predefined-topology-templates.md) — 预定义拓扑(延后)
- [issues/10-graph-lifecycle-management.md](issues/10-graph-lifecycle-management.md) — 生命周期管理
- [issues/11-react-graph-adaptation.md](issues/11-react-graph-adaptation.md) — React 适配
- [../adr/0033-generalized-graph-engine.md](../adr/0033-generalized-graph-engine.md) — ADR-0033
- [../adr/0034-parallel-scheduling-engine.md](../adr/0034-parallel-scheduling-engine.md) — ADR-0034
