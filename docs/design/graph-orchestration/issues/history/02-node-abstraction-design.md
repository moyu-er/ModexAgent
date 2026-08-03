# Node 封装:通用执行单元如何映射到 modex_graph 的 Node[S]

Status: triage:closed
Assignee: sisyphus
Resolved: 2026-08-02
Type: wayfinder:grilling
Blocked by: 01-modex-graph-capability-assessment

## Question

modex_graph 的 `Node[S]` ABC 已经是通用的(一次 `execute(ctx) -> NodeResult`)。但"图调度系统"中的 node 有几种模式需要明确:

1. **是否需要 Node 子类?** — `Node[S]` 已经通用。图调度是否需要定义 `AgentNode[S]` / `GraphNode[S]`(Graph-as-Node 的便捷包装)/ `FunctionNode[S]` 等子类?还是保持 `Node[S]` 通用,由调用方直接实例化?

2. **"agent 完整调用"作为 node 的契约是什么?** — 一个 agent(ReAct 或 External)作为 node 时:
   - `execute` 如何启动 agent?如何等待完成?
   - agent 的输入(state 中的什么字段?)和输出(写回 state 什么字段?)是什么?
   - agent 的 interrupt(approval 等)如何传播到图层面?

3. **Graph-as-Node 的实际使用** — ADR-0033 D8 说 `Graph` 是 `Node` 的子类(类型层面支持),但从未 exercise。实际使用时:
   - 内层图的 `GraphEngine` 如何创建?每次 execute 都创建新的?
   - 内层图的 `GraphRuntime` 用什么?默认 no-op?还是外层的 runtime?
   - 内层图的 state 如何与外层图的 state 关联?独立?共享?

4. **node 的"自主结束"语义** — 用户描述"agent 可能自主结束"。在 modex_graph 中,`execute` 返回 `NodeResult` 就是"结束"。这个"自主结束"是否就是 `execute` 返回?还是有别的语义?

## Context

- grilling 对齐:node = 通用概念(任何执行单元)
- grilling 对齐:ReAct 图可以作为 node(Graph-as-Node)
- grilling 对齐:与现有系统独立,不替代 ReAct/multi_agent
- ADR-0033 D2:Node 接口是单方法 `execute(ctx) -> NodeResult`
- ADR-0033 D8:Graph-is-a-Node(类型层面,未 exercise)
- ADR-0033 D5.1:GraphContext 可子类化,业务模块加类型安全访问器

## Resolution criteria

明确以下决策:
- Node 子类策略(是否定义 AgentNode/GraphNode/FunctionNode,还是保持通用)
- agent 作为 node 的输入/输出/interrupt 传播契约
- Graph-as-Node 的内层 GraphEngine/Runtime/state 关联方案
- "自主结束"的语义定义

## Resolution

### 1. Node 子类策略:配置驱动 + 工厂层抽象 + 动态组合

**分层原则**:抽象在 modex_graph,实现在 modex_agent,常用通用实现在 modex_graph。

| 层 | 内容 |
|---|------|
| **modex_graph(抽象)** | `NodeFactory` ABC + `NodeRegistry` + `NodeSpec`(frozen Pydantic,可序列化数据载体) |
| **modex_graph(通用实现)** | FunctionNode / GraphAsNode(CompiledGraph 已是) / ConditionNode / RetryNode / MapReduceNode / DelayNode / HumanInputNode + 对应 Factory |
| **modex_agent(业务实现)** | AgentNode(包装 agent 完整调用)+ AgentNodeFactory |

`graph_patterns/` 的三个示例(conditional/retry/map_reduce)提升为 modex_graph 通用实现——图调度系统是第二个消费者,满足 ADR-0007。

### 2. AgentNode 输入输出契约:双输入模型

**输入 1(触发层)**:图状态(ctx.state),上游 submit 写入。多上游:ParallelScheduler 的 ON_ALL_PREDS 等所有激活上游。条件路由:deliver 指定 next_node,跳过的上游不 submit。**submit 触发 node 执行**(deliver/submit 修正后,见末尾修正小节)。

**输入 2(执行层)**:inbox 消息,execute 内部 agent 自己通过 `InboxFlushHook.before_iteration` 拉取(fold-in)。**inbox 不触发 node 执行**——图调度层面无感知。上游 agent 重复提交的消息进 inbox,当前 agent 在 react iteration 中拉取。

**输出**:emitter 流式输出(内容流)+ AgentResult(终止信号)→ NodeResult.state_update 写回图状态。

**execute 内部**:AgentNode 在 execute 内部可创建子图实例(ticket 04 决议:GraphInstance)。AgentNode 可以选择在 execute 内部创建 GraphInstance(子图实例,如 ReAct 图实例)交给 engine 执行,而不是直接调 TurnRunner。TurnRunner 本身也是一种 engine。框架提供原语(GraphInstance/GraphEngine/StateFactory/持久化层),AgentNode 自由组装。

### 3. AgentNode 依赖注入:bot 工厂模式

**时机**:bot 启动后所有依赖就绪(TurnRunner/AgentPool/Provider 等已构建),bot 图工厂构建图对象时直接注入。**无循环依赖**——不需要 ResolverCell。

**路径**:AgentNode 持有 PoolInstance 或从 PoolWorkspaceResources 获取 TurnRunner(`pool._agents[name].pipeline._turn_runner`)。与现有 `_wire_pool_to_resources` 装配模式一致。

**图对象是临时的**(每次调度重建),图定义和执行状态是持久的(见 ticket 10)。

### 4. Graph-as-a-Node:已 exercise 但隔离不足

CompiledGraph 已是 Node 子类,`test_subgraph.py` 已验证。但内外图共享同一 GraphState / GraphRuntime / user_data——隔离边界是缺口。**隔离方案(state 独立/共享/映射、Runtime 桥接、interrupt 传播)是 ticket 04 的范围。**

### 5. "自主结束"语义

`execute` 返回 `NodeResult` 即"自主结束"。唤醒(未投递检测 + 重跑)是 Bug 防护场景,**ticket 03 的范围**。

### 6. 审批关闭(探索补充确认)

ADR-0008(默认关闭 + main-only)+ ADR-0020(subagents 永远无审批)。长程任务中关闭审批:长程 node 走 subagent 路径自动无审批,或 main agent 用 `enabled: false`。**无需新 ADR。**

### 7. inbox 机制(探索补充确认)

ADR-0015 + `InboxFlushHook` + `InboxPoller` 完整实现。AgentNode 内 agent 的 react loop 天然支持 fold-in——复用现有机制,无需新设计。

### deliver/submit 修正(来自 ticket 07)

- 双输入模型修正:上游 submit → 下游 InputIntegrator 整合 → execute。不再是"dispatch 触发 node 执行",而是"上游 _submit 派发 → 下游 InputIntegrator 整合多上游输入 → execute"
- 输出修正:deliver 累积 → _submit 派发。不再用 NodeResult.transition/command 做 dispatch。AgentNode 的 execute 内部 agent 可通过 cli deliver 累积结果,execute 完成后框架自动 _submit 派发
- 新增三层方法拆分:_deliver(框架固定,累积+持久化)/ deliver(node 自定义,默认 append) / _submit(框架固定,按 next_node 分组派发) / submit(node 自定义,默认分组整合)
- InputIntegrator ABC:整合多上游 submit 的 IntegratedPayload 为单一 IntegratedInput,框架给默认实现,node 可自定义

### 设计修正(2026-08-03,见 `distributed-persistence-design.md` §9)

实现检视后,GraphAsNode 和 FunctionNode 的定位调整:

**GraphAsNode(P2.8)**: 不作为特殊 wrapper 机制。Node 天生可以在 execute 内部创建子图 GraphInstance + GraphEngine 执行。框架提供原语(GraphInstance / GraphEngine / coordinator / NodeState),node 自由组合。state 隔离是 node 自己的事。当前 `GraphAsNode` wrapper 保留为示范实现。

**FunctionNode(P2.7)**: callable 注入模式与声明式 config 驱动设计矛盾。通用 node 是声明式,不注入 callable。如果需要可配置逻辑,用户自定义 Node(继承 Node ABC,实现 execute)是标准模式。当前 `FunctionNode` + `FunctionNodeFactory` 保留为示范。

**保留的通用 Node**: DelayNode(声明式 config 驱动)、HumanInputNode(声明式 + GraphInterrupt 框架原语)、AgentNode(业务层,TurnRunner 注入有 ADR 支撑)。
