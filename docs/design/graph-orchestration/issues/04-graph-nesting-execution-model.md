# 图套图的执行模型

Status: triage:closed
Assignee: sisyphus
Started: 2026-08-02
Resolved: 2026-08-02

## Question

用户明确:"每个 node 内部都可以是一个完整的图调度,react 就只是其中一种图,可以作为一个 node"。

**01 research 修正**:Graph-is-a-Node **已经被 `test_subgraph.py` 验证**(不是 ADR 说的"未 exercise")。严格说是 `CompiledGraph`(非 mutable `Graph`)继承 `Node`。其 `execute()` 在**同一个 `ctx.state`/`ctx.runtime`/`ctx.user_data`** 上启动内层 engine,返回空 `NodeResult`,父图按自己的边继续路由。

**02 决议确认**:隔离边界(内外图共享同一 GraphState)是本 ticket 的范围。ReAct 作为 node 通过 AgentNode 包装(不是直接 add_node CompiledGraph),因为需要依赖注入。

实际使用需要决策:

1. **内层图的 GraphEngine 生命周期** — CompiledGraph.execute 已实现:每次 execute 创建内层 GraphEngine,共享 ctx。问题不是"是否创建",而是"是否应该独立"(当前共享,是否需要隔离?)。

2. **内层图的 GraphRuntime** — 用什么?
   - 默认 no-op `GraphRuntime()`?那内层图就没有 hook/interceptor/emitter/snapshot。
   - 外层的 `GraphRuntime`?那内层图的 hook 会与外层混淆。
   - 需要一个"桥接 runtime"把内层图的事件转发到外层?

3. **state 传递** — 内层图的 state 与外层图的 state 如何关联?
   - 独立:内层图有自己的 state,execute 返回时把结果写到 `NodeResult.state_update`。
   - 共享:内层图直接操作外层图的 state(通过 `ctx.fork(state=None)` 共享)。
   - 映射:内层图有独立 state,但有字段映射(内层 state.field_a → 外层 state.field_b)。

4. **interrupt 传播** — 内层图的 `GraphInterrupt` 如何传播?
   - 直接传播到外层(外层也 interrupt)?
   - 内层自己处理(interrupt 是内层图的内部事务)?
   - 可配置(某些 interrupt 传播,某些不传播)?

5. **嵌套深度限制** — 图套图可以无限嵌套。是否需要深度限制(防 stack overflow)?

6. **ReAct 作为 node 的具体路径** — 02 决议已回答:通过 AgentNode 包装(业务层,modex_agent),因为 ReAct 图需要 llm_client/tool_executor 等依赖注入。`build_react_graph().compile()` 返回的 CompiledGraph 可直接用于 GraphAsNode(通用,不需要依赖),但 ReAct 作为 node 需要 AgentNode 包装。此问题已基本解决,聚焦于 AgentNode 如何持有和复用 CompiledGraph。

## Context

- grilling 对齐:图套图是核心模式,ReAct 可作为 node
- 01 research 修正:Graph-is-a-Node 已被 test_subgraph.py 验证,CompiledGraph.execute 共享父 ctx.state/runtime/user_data
- 02 决议:ReAct 作为 node 通过 AgentNode 包装;隔离边界是本 ticket 范围
- ADR-0033 D8:Graph-is-a-Node(类型层面 wired,已 exercise 但隔离不足)
- ADR-0033 D5.2:ctx.fork() 的 shared/isolated 语义(runtime shared, user_data shared, state isolated)
- ADR-0033 D7:GraphInterrupt 传播(engine never swallows GraphBubbleUp)
- ADR-0034 D7:multi-instance model + fork-based state isolation

## Resolution criteria

明确以下决策:
- 内层 GraphEngine/CompiledGraph 的生命周期
- 内层 GraphRuntime 策略(no-op / 外层 / 桥接)
- state 关联策略(独立 / 共享 / 映射)
- interrupt 传播策略
- 嵌套深度限制
- ReAct 作为 node 的具体使用路径(直接用 vs 包装)

## Resolution

### 核心原则:框架提供原语,节点自由组合

图套图是节点的内部行为。框架提供"图实例 + engine + 持久化"的原语,节点在自己的 execute 内部自由组合。框架不规定节点如何创建子图、如何处理 interrupt、如何桥接 state、嵌套多深。

### 新增核心抽象:GraphInstance

当前 modex_graph 有 CompiledGraph(图定义编译产物)和 GraphEngine(执行器),但缺少"图实例"概念。引入 GraphInstance:

- **GraphSpec** → 图定义(静态,可序列化)
- **CompiledGraph** → 编译产物(冻结拓扑)
- **GraphInstance** → 运行态实例(持久化,有 graph_instance_id,可恢复)

GraphInstance 属性:
- `graph_instance_id: str` —— 持久化的唯一 key,重启后通过它关联同一实例
- `parent_instance_id: str | None` —— 父图实例(递归嵌套时关联)
- `parent_node: str | None` —— 父图中的节点名(该节点创建了此子图实例)
- `graph_spec: GraphSpec` —— 图定义
- `compiled_graph: CompiledGraph` —— 编译产物
- 运行状态(checkpoint/dispatch/activated_sources/completed_instances 等,ticket 10 覆盖)
- 节点状态(ticket 10 的 Node 级状态抽象)

**持久化**:统一 schema,所有图实例(外层 + 内层)在同一张表/同一个 schema 中,通过 graph_instance_id 区分,通过 parent_instance_id 关联。不建零散的 SQL 表。

**实例化**:节点在 execute 内部自行决定如何创建子图实例。框架提供原语(GraphInstance/GraphEngine/StateFactory/持久化层),节点自由组装。框架不规定创建方式。

### 框架提供的原语

| 原语 | 职责 | 位置 |
|------|------|------|
| GraphInstance | 图实例(graph_instance_id,持久化,parent 关联,运行状态) | modex_graph |
| GraphEngine | 执行器(松耦合,可替换,基于 Graph 抽象) | modex_graph(已有) |
| StateFactory | 创建/恢复独立 state | modex_graph(ticket 08 决议) |
| 持久化层 | 统一 schema,graph_instance_id 关联 | modex_graph |
| checkpoint/resume | 图实例级 + 节点级 | modex_graph(ticket 10 覆盖) |

### 框架不做的(节点自行决定)

- **如何创建子图实例**:node 的 execute 内部逻辑。简单执行(直接 GraphEngine.run_async)或持久化实例(创建 GraphInstance + checkpoint)都行。
- **如何处理 interrupt**:node 的 execute 自己 try/except。catch 消化(自己处理恢复)/ 不 catch(抛到图层面)/ 转换(包装成其他异常)都行。
- **如何桥接 state**:内层图实例有独立 state(可能与外层不同类型)。node 负责从内层结果提取数据,转成 NodeResult.state_update 写回外层 state。
- **嵌套深度**:无限制。node 内部可以无限递归创建子图实例。
- **内层 GraphRuntime**:内层图实例有独立 runtime,不共享外层。node 自己决定用什么 runtime。

### 图层面 interrupt 处理策略(抽象 + 默认 crash)

当一个节点的 execute 抛出 GraphInterrupt(节点没消化,传播到图层面),图的 GraphEngine 收到后的行为:

**抽象**:
- `InterruptPolicy` ABC(或 Scheduler 的可重写方法),定义图层面收到 GraphInterrupt 后的行为
- 图实例可配置 interrupt 策略
- 业务可自定义实现

**默认实现:CrashPolicy**
- GraphInterrupt 传播到 GraphEngine → 图实例暂停(crash)
- 持久化 checkpoint(含其他正在执行节点的状态)
- 其他正在执行的节点被中断(asyncio task cancelled)
- 等待外部恢复(用 graph_instance_id 重新加载 + 从 checkpoint 恢复)
- 恢复时:重入中断的节点 + 重新 dispatch 其他被中断的节点

**其他可配置策略**(框架提供抽象,业务可自行实现):
- WaitOthersPolicy:等其他正在跑的节点完成再暂停
- NodeOnlyPolicy:只影响产生 interrupt 的节点,其他节点继续
- 业务自定义

### GraphEngine 松耦合

- Engine 接口是 ABC(已有 Scheduler ABC)
- 可以有不同实现(LinearScheduler/ParallelScheduler/业务自定义)
- 接口上支持多种,实际执行能力可能受限(如 linear 无法执行并行图)
- Engine 必须基于 Graph 抽象,不能脱离图
- 同一个图定义可以用不同 Engine 调度(如配置切换 linear/parallel)

### 修正其他 ticket 的记录(04 关闭后统一修正)

本次决议引入了 GraphInstance 概念,影响以下 ticket:

- **02(AgentNode)**:AgentNode execute 内部"调 TurnRunner/agent.run"需补充"可创建子图实例"。AgentNode 可以选择在 execute 内部创建 GraphInstance(子图实例)交给 engine 执行,而不是直接调 TurnRunner。TurnRunner 本身也是一种 engine。
- **08(GraphSpec)**:GraphSpec → CompiledGraph 之间需补充"实例化"步骤。完整链路:GraphSpec(定义) → GraphSpecCompiler → CompiledGraph(编译) → GraphInstance(实例化,分配 graph_instance_id)。
- **11(图生命周期管理)**:run_id 全部改为 graph_instance_id。CheckpointData / DispatchStore / activated_sources 等全部挂在 GraphInstance 上,通过 graph_instance_id 关联。GraphInstance 是持久化的载体。

### deliver/submit 修正(来自 ticket 07)

- node._execute 框架调用的职责扩展:
  1. InputIntegrator.integrate(上游 submits) → 整合输入(execute 之前)
  2. execute(node 自定义逻辑,执行期间 deliver 可被多次调用)
  3. _submit(框架自动,按 next_node 分组派发,execute 之后)
- 节点内部创建子图实例时,子图的 deliver/submit 独立于外层(子图有自己的 deliver_states)
