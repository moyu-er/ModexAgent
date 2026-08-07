# modex_graph 现有能力 vs 图调度系统需求评估

Status: triage:closed
Resolved: 2026-08-02
Resolution: 见 ## Resolution 部分。3项可直接用(Node/Graph builder/DispatchStore)、6项需扩展(Scheduler/after_node/Interrupt/Graph-is-a-Node/CheckpointStore/routing)、3项需新增(taskId知识库/外部唤醒/worker治理)。关键风险:恢复缺口阻塞唤醒、after_node时序不足、并行状态约束强。
Type: wayfinder:research
Blocked by: none

## Question

评估 modex_graph 现有实现(ADR-0033 Phase a + ADR-0034 ParallelScheduler)的能力,对照图调度系统的需求,产出一份"可直接用 / 需要扩展 / 需要新增"的分类清单。

## Context

图调度系统的核心需求(来自 grilling 对齐):
1. **通用 node**:node 是任何执行单元,不绑定具体 agent
2. **图套图**:Graph-is-a-Node(ADR-0033 D8)的实际 exercise
3. **未投递检测与唤醒**:after_node hook 检测 + 重跑机制
4. **taskId 共享知识库**:多 node 共享知识库
5. **长时 node 执行**:node 可能持续几分钟到几小时

需要评估的 modex_graph 现有能力:
- `Node[S]` ABC + `NodeResult` + `Command` + `Task` — 是否足够支撑通用 node?
- `Graph` builder + `compile()` + `CompiledGraph` — 是否需要声明式配置层?
- `LinearScheduler` + `ParallelScheduler` — 哪个适合图调度?multi-instance model 能否支撑"唤醒"?
- `GraphRuntime` AOP 桥(8 钩子:before_node/after_node/dispatch_hook/around/apply_governance/drain_control/capture_snapshot/emit)— after_node 能否检测"未投递"?
- `GraphInterrupt` + `resume_target` — 能否用于"唤醒"?还是需要新机制?
- `Graph-is-a-Node`(D8,类型层面支持但未 exercise)— 实际嵌套执行需要什么?
- `CheckpointStore`(save 已实现,load_latest 零调用)— resume 未接通,是否阻塞?
- `DispatchStore` + `ConflictDetector` — 是否可直接用于图调度的 dispatch 审计?
- routing 机制(D6 四层优先级:Command.goto > transition > conditional > default edge)— "未投递"检测的判断逻辑如何与 routing 结合?

## Expected output

一份结构化报告:
1. **可直接用的能力**(无需改动):列出能力 + 在图调度中的用途
2. **需要扩展的能力**(已有但不够):列出能力 + 缺什么 + 扩展方向
3. **需要新增的能力**(不存在):列出能力 + 为什么需要 + 新增位置(modex_graph 还是 modex_agent)
4. **关键风险**:可能阻塞图调度设计的现有限制

## Resolution criteria

报告覆盖所有 9 项现有能力,每项有明确的"可直接用/需扩展/需新增"分类和理由。

## Resolution

### 结论与范围

`modex_graph` 已是与 agent 解耦的通用进程内图执行内核：`Node` 不依赖 agent 类型，`ParallelScheduler` 已具备连续多实例、扇出、扇入和派发审计。它尚不是可跨进程恢复、可由外部事件唤醒、带 taskId 知识库的图调度产品。ADR-0034 本身仍标为 proposed，以下以当前源码为能力事实，ADR 用于核对设计意图与已知延期项。[源：`docs/adr/0034-parallel-scheduling-engine.md:1-7`]

九项能力的分类为：1 `Node`/`NodeResult`/`Command`/`Task` 可直接用；2 `Graph` builder/`compile()`/`CompiledGraph` 可直接用；3 两个 Scheduler 需扩展；4 `GraphRuntime` AOP 桥需扩展；5 `GraphInterrupt`/`resume_target` 需扩展；6 Graph-is-a-Node 需扩展；7 `CheckpointStore` 需扩展；8 `DispatchStore`/`ConflictDetector` 可直接用；9 routing 机制需扩展。

### 可直接用的能力

1. **[1] 通用 `Node[S]` + `NodeResult` + `Command` + `Task`。** `Node` 只要求 `execute(ctx)`，可同步或异步执行；其接口没有 `Agent`、ReAct 或业务类型，因此可包装任意执行单元（agent 调用、脚本、人工步骤适配器、工作流动作）。`NodeResult` 同时承载状态更新和路由，`Command.goto` 可单点跳转或以 `Task` 扇出。[源：`src/modex_graph/node.py:35-74`；`src/modex_graph/result.py:44-132`] 图调度可直接把每一种实际执行器实现为 `Node`，以 `GraphState` 声明其可共享的调度状态。

2. **[2] 代码式 `Graph` builder、`compile()` 和不可变 `CompiledGraph`。** `add_node()`/`add_edge()` 提供清晰的拓扑构造面，`compile()` 校验唯一入口、边端点、循环，并在并行模式下校验 START/END 可达性。[源：`src/modex_graph/graph.py:80-105`；`src/modex_graph/graph.py:114-213`；`src/modex_graph/graph.py:251-307`] `CompiledGraph` 冻结拓扑并保存 Scheduler/trigger 选择。[源：`src/modex_graph/compiled_graph.py:42-69`] 对五项核心需求而言，代码式 builder 已足够，不应先增加声明式 DSL/YAML；如果以后需要让非代码调用方创建图，应在 `modex_agent` 的图调度业务层做配置到此 builder 的适配，不污染通用内核。

3. **[8] `DispatchStore` + `ConflictDetector` 的当前派发审计与并发写保护。** `ParallelScheduler` 每次 `ctx.dispatch()` 都校验目标为声明边、生成 `DispatchEvent` 并持久化到按 `run_id` 分区的 store。[源：`src/modex_graph/scheduler/parallel.py:477-530`] `DispatchStore` 已有内存和 SQLite 实现，支持按 source、target、run 查询，因此可直接做一次运行内的派发审计、追踪和诊断。[源：`src/modex_graph/dispatch_store.py:56-123`；`src/modex_graph/dispatch_store.py:183-245`] 并发实例在 fork 后只合并 `state_update`，并以 generation 写冲突检测保护 `LastValue` 字段。[源：`src/modex_graph/scheduler/parallel.py:350-383`] 图调度可立即采用 SQLite `DispatchStore` 保留“谁向哪个 node 发过什么”的可查询证据。

### 需要扩展的能力

1. **[3] `LinearScheduler` + `ParallelScheduler`：适合执行图，但不能唤醒既有运行。** `LinearScheduler` 是单 current-pointer 的顺序路径；`ParallelScheduler` 已用 READY 集合、`asyncio.create_task`、`FIRST_COMPLETED` 实现连续多实例调度，适合有并行与扇入的图调度。[源：`src/modex_graph/scheduler/linear.py:50-103`；`src/modex_graph/scheduler/parallel.py:140-233`] 但每次 `run_async()` 都重置实例状态并生成新的 UUID `run_id`，没有以既有 `run_id` 打开运行的入口。[源：`src/modex_graph/scheduler/parallel.py:164-175`] 扩展方向：在 `modex_graph` 增加显式 `run_id`、从检查点恢复 scheduler 内部实例/队列的入口和幂等重派发；在 `modex_agent` 增加将外部完成事件定位到该 run/node instance 的工作流执行服务。

2. **[4] `GraphRuntime` 的 `after_node`：可作为检测切点，但还不能判定“未投递”。** 引擎确实会在 node 返回后调用 `after_node`；线性路径在路由解析之前调用它。[源：`src/modex_graph/scheduler/linear.py:91-100`] 并行路径也在 merge 后、`_compile_routing()` 之前调用它。[源：`src/modex_graph/scheduler/parallel.py:385-399`] 因此 hook 此时只能看见 `NodeResult` 和合并后的 state，尚看不见实际产生的 dispatch、目标是否进入 READY/RUNNING、或外部执行是否回执。扩展方向：在 `modex_graph` 添加路由编译后的 `after_dispatch`/`delivery_outcome` 生命周期事件，并将“预期投递”建模为类型化策略；`modex_agent` 的业务 hook 再据此定义何为未投递、超时和重跑，避免把 agent 规则放入图内核。

3. **[5] `GraphInterrupt` + `resume_target`：支持暂停协议，不等于唤醒机制。** `ctx.interrupt()` 会抛出 `GraphInterrupt`，并要求调用者预先写入 `state.resume_target`；重入仍从 entry node 开始，由 entry node 再动态路由到目标。[源：`src/modex_graph/context.py:160-176`；`src/modex_graph/scheduler/linear.py:50-63`] 这可直接复用为人工审批或“等待外部完成”的暂停语义，但没有事件订阅、run 查找、去重回调或自动重跑。扩展方向：`modex_graph` 提供恢复既有 run 的原语；`modex_agent` 提供外部事件/定时器到该原语的可靠适配器、幂等键及重试策略。

4. **[6] Graph-is-a-Node：已经实际执行过，但只能共享同一上下文。** 严格说是 **`CompiledGraph`** 而非 mutable builder `Graph` 继承 `Node`；其 `execute()` 在同一个 `ctx.state`、`ctx.runtime`、`ctx.user_data` 上启动内层 engine，返回空 `NodeResult`，父图按自己的边继续路由。[源：`src/modex_graph/compiled_graph.py:42-92`] 单元测试已验证内图作为父图 node 执行及二者共享 context，所以不再只是类型层面“未 exercise”。[源：`tests/unit/modex_graph/test_subgraph.py:19-54`；`tests/unit/modex_graph/test_subgraph.py:56-79`] 不足是内外图必须共享同一 `GraphState` 类型和持久化边界，内图无法以类型化结果、独立状态、独立检查点或 `ParentCommand` 向父图表达跨图路由。扩展方向：在 `modex_graph` 明确子图输入/输出映射、父子 run/instance 身份、嵌套 checkpoint 与 bubble-up 路由；这正是 ADR-0033 所列待完成的 graph-of-graphs 项。[源：`docs/adr/0033-generalized-graph-engine.md:543-557`]

5. **[7] `CheckpointStore`：有保存介质，尚无恢复闭环。** `CheckpointData` 已保存 main state、待扇入队列、已完成实例和 dispatch 审计，`CheckpointStore` 有 SQLite 实现及 `load_latest()` 查询。[源：`src/modex_graph/checkpoint_store.py:70-112`；`src/modex_graph/checkpoint_store.py:115-176`；`src/modex_graph/checkpoint_store.py:233-255`] `ParallelScheduler` 也在每个实例 merge 后异步保存快照。[源：`src/modex_graph/scheduler/parallel.py:657-703`] 但 scheduler 没有调用 `load_latest()`，且 ADR 明确记录 crash recovery/resume 尚未接通。[源：`docs/adr/0034-parallel-scheduling-engine.md:489-498`] 扩展方向：先在 `modex_graph` 恢复 `CheckpointData` 至主 state、完成实例、pending dispatch 和 instance sequence，再由 `modex_agent` 持有可稳定定位的 workflow/run 身份与恢复命令。此项是“唤醒”和长时运行可靠性成立的前置条件。

6. **[9] 两层 routing：能执行既定路由，不能表达未投递判定。** 线性调度的优先级为 `Command.goto`、带 reason 的边、默认边；并行调度将同一规则编译成实际 `ctx.dispatch()`，并对同 reason 的全部边扇出。[源：`src/modex_graph/scheduler/linear.py:105-179`；`src/modex_graph/scheduler/parallel.py:403-473`] 这已经是图调度的控制流基础，且 `ctx.dispatch()` 会验证边白名单。[源：`src/modex_graph/scheduler/parallel.py:484-530`] 但“node 本应投递到何处、什么回执算送达、未送达多久重试”不在 `NodeResult`、`DispatchEvent` 或 routing API 中；手工 dispatch 的 payload 也目前只审计、不会折叠到下游 state。[源：`docs/adr/0034-parallel-scheduling-engine.md:180-191`] 扩展方向：以类型化 `DeliveryExpectation`/`DeliveryStatus` 补足预期、接受、完成、失败、超时等状态，并在路由完成后记录；不应通过读取字符串 transition 或猜测边来实现。

### 需要新增的能力

1. **taskId 共享知识库，位置：`modex_agent/memory/` + `modex_agent/persistence/`。** 当前 `task_id` 只作为外部 agent 的 `MODEX_TASK_ID` 环境变量元数据，没有知识库接口、记录模型、作用域或持久化查询契约。[源：`src/modex_agent/agents/external/types.py:167-185`] 该需求涉及业务知识、权限、检索和跨 node 一致性，且 `modex_graph` 的物理边界禁止依赖 `modex_agent`，因此不能放入图内核。[源：`docs/adr/0033-generalized-graph-engine.md:755-791`] 应新增 task-scoped `KnowledgeStore` ABC、以 `(workspace, task_id)` 为规范键的值对象、SQLite/文件适配器及给 node 的受控读写服务；GraphContext 只携带该业务服务，不拥有知识数据。

2. **外部唤醒入口与运行编排登记，位置：`modex_agent`。** “after_node 发现未投递后重跑”需要把检测结果持久化为可寻址的 workflow/run/node-instance，再由消息、回调或定时器恢复同一 run；这不是当前仅在进程内调用 handler 的 `GraphContext.dispatch()` 能力。[源：`src/modex_graph/context.py:178-230`] 应新增图调度运行登记、事件去重/关联、延迟重试与恢复触发器；它调用上节扩展后的 `modex_graph` resume API，而不是复制调度循环。

3. **长时执行的 worker 生命周期治理，位置：`modex_agent`。** 单个 async node 可以自然持续数小时，`ParallelScheduler` 不会主动设超时；但它也没有 heartbeat、lease、超时、持久化取消、断进程后接管或执行器级重试。当前任一实例抛错会取消同 run 的其他运行任务。[源：`src/modex_graph/scheduler/parallel.py:159-160`；`src/modex_graph/scheduler/parallel.py:220-230`] 应新增 worker owner、心跳/租约、可配置 timeout、取消和重试策略，以及外部执行器的完成回执模型。通用 scheduler 只需提供可恢复实例状态和状态变更事件，进程/agent 生命周期不应塞入 `modex_graph`。

### 关键风险

1. **恢复缺口会阻塞可靠唤醒。** 当前运行每次生成新 `run_id`，检查点只写不读；进程重启后既无法关联暂停实例，也无法跳过已完成 node。这会让“未投递后重跑”和数小时 node 在故障场景下退化为人工重启或重复执行。[源：`src/modex_graph/scheduler/parallel.py:164-175`；`docs/adr/0034-parallel-scheduling-engine.md:489-498`]

2. **`after_node` 的时序不足以充当投递成功证明。** 它先于自动路由编译，且外部执行完成更晚；若直接以它决定重跑，会产生重复投递或漏判。必须先定义路由后 dispatch 记录与外部 ack 的状态机。[源：`src/modex_graph/scheduler/parallel.py:385-399`]

3. **并行分支的状态约束很强。** 并行路径深拷贝整个 `GraphState`，fork 内的命令式修改不回写，只有 `NodeResult.state_update` 合并；同 generation 写同一 `LastValue` 会失败。[源：`src/modex_graph/scheduler/parallel.py:49-60`；`src/modex_graph/scheduler/parallel.py:292-302`] 长时、大状态或任意 node 直接写共享对象的设计会出现内存成本、丢失更新或冲突，需要先规定 reducer/声明式更新边界。

4. **子图目前没有隔离边界。** 内外图共享 state/runtime/user_data，适合可复用的同步片段，却不足以安全封装独立工作流、独立恢复或独立 taskId；嵌套图在并行 scheduler 下的父子 instance 归属也未建模。[源：`src/modex_graph/compiled_graph.py:71-92`；`tests/unit/modex_graph/test_subgraph.py:56-79`]

5. **调度审计不等于业务投递审计。** `DispatchStore` 证明调用了 scheduler 的 dispatch，不证明下游执行器已接收、完成或产生可见结果；同时手工 dispatch payload 尚未进入下游 state。[源：`src/modex_graph/dispatch_store.py:56-95`；`docs/adr/0034-parallel-scheduling-engine.md:180-191`]

6. **ADR 与实现的成熟度需要逐项验收。** ADR-0034 仍是 proposed，而源码已有部分实现；后续设计不能仅以 ADR 的“planned”语义假定 resume、远程 dispatch 或完整 payload fold 已可用，必须以端到端恢复/唤醒测试作为准入条件。[源：`docs/adr/0034-parallel-scheduling-engine.md:1-7`；`docs/adr/0034-parallel-scheduling-engine.md:361-370`]
