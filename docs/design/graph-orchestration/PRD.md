# Graph Orchestration System

> **Note**: This PRD is the planning-stage record. Design authority now
> lives in `distributed-persistence.md` (the current authoritative design
> document). Closed design tickets (22-33) and the wayfinder map are
> archived to `issues/history/`. ADR-0033 and ADR-0034 are updated to
> reflect the current contracts. The decision items below are the
> historical record of what was decided during planning; issue links
> point to `issues/history/`.

Status: wayfinder:map

## Destination

设计一个图调度编排系统,基于 modex_graph 引擎,支持:
- **通用 node**:每个 node 是一个通用执行单元(ReAct 图、外部 agent、确定性函数、子图均可)
- **图套图**:每个 node 内部可以是完整的图调度(Graph-is-a-Node, ADR-0033 D8 的实际 exercise)
- **未投递检测**:scheduler 在 _submit 时检查是否有累积的 deliver;无累积+无默认下游则错误反馈重跑(max_retry 防无限循环)
- **taskId 知识库分区**:modexctl kb --by-task 按 taskId(=graph_instance_id)分区,补充 deliver/submit 不够表现上下文的场景

产出:ADR + 设计文档,为后续实现提供明确路径。为 agent 自主生成图保留接口(但不在本 map 范围内实现)。

## Notes

- **Domain**: modex_graph 图引擎(ADR-0033/0034)+ modex_agent 框架
- **Skills every session should consult**: architecture-patterns, codebase-design, domain-modeling, python-type-safety, python-design-patterns
- **参考项目**: dify(workflow engine + Layer 系统 + 暂停恢复)、openclaw(TaskFlow 状态机 + tool-loop-detection + detached-task-runtime)、multica(task scheduling + session 恢复)
- **概念参考**: graph engineering(org graph vs work graph 二分)、Lár AdaptiveNode + TopologyValidator(validator 纯确定性,不委托 LLM)
- **规则**: 收敛而非新增并行路径(AGENTS.md convergence rule 1);ABCs before implementations, zero Protocols;frozen Pydantic;ADR-0007(两个用例才提升 seam);modex_graph 是独立包,不能 import modex_agent(架构守卫测试)
- **现有 ADR**: ADR-0033(Generalized Graph Engine, Phase a 已实现)、ADR-0034(Parallel Scheduling Engine, proposed)
- **Grilling 对齐结果**(charting 阶段):
  - node = 通用概念(任何执行单元,不绑定具体 agent)
  - 唤醒场景 = Bug 防护(节点忘了投递,不是质量检查或完整性检查)
  - 与现有系统 = 独立,不替代 ReAct/multi_agent;ReAct 可作为 node;图套图是核心模式

## Decisions so far

- [modex_graph 现有能力评估](issues/history/01-modex-graph-capability-assessment.md) — 3项可直接用(Node/Graph builder/DispatchStore)、6项需扩展(Scheduler缺resume/after_node时序不足/Interrupt缺唤醒闭环/Graph-is-a-Node缺隔离/CheckpointStore只写不读/routing缺投递判定)、3项需新增(taskId知识库/外部唤醒入口/worker治理)。Graph-is-a-Node已被test_subgraph验证但内外图共享state是缺口。恢复缺口是唤醒可靠性的阻塞项。

- [Node 封装:通用执行单元映射](issues/history/02-node-abstraction-design.md) — 配置驱动+工厂层抽象+动态组合。抽象在modex_graph(NodeFactory ABC+NodeRegistry+NodeSpec),通用实现在modex_graph(FunctionNode/GraphAsNode/ConditionNode/RetryNode/MapReduceNode/DelayNode/HumanInputNode),业务实现在modex_agent(AgentNode)。AgentNode双输入模型:图状态(dispatch触发node)+inbox(execute内部agent自己拉取,不触发node)。依赖注入:bot工厂启动后直接注入(无循环依赖)。审批关闭复用ADR-0008/0020。inbox复用ADR-0015 InboxFlushHook。

- [图生命周期管理(类别1+2)](issues/history/10-graph-lifecycle-management.md) — 状态分层+职责分离:modex_graph维护图级状态(main_state/pending/activated_sources/completed/instance_seq/iteration_count/graph_instance_id)+持久化+重建+重新dispatch。不判断节点内部恢复。Node级状态抽象ABC在modex_graph(内存缓存优先),通用实现在modex_graph,业务实现在modex_agent(node自决MVCC/单状态/无状态)。CheckpointData需新增activated_sources/instance_seq/iteration_count字段。graph_instance_id持久化唯一key(取代run_id)。不倒推completed(有向环+条件路由+MVCC使倒推危险),复用_recheck_pending推导。节点幂等完全移出modex_graph。图级MVCC轮次记为待办。

- [图生命周期管理(类别3)](issues/history/10-graph-lifecycle-management.md) — 图定义持久化(GraphSpec独立表,SQLite,跨workspace)+ 生命周期状态机(running/paused/stopped/crashed/completed/failed枚举,paused/stopped不被故障恢复自动捡起,crashed可自动捡起)+ 外部控制接口(异常控制链统一,复用ControlCommand模式,REST+CLI收敛同路径,pause/stop/resume/deliver针对节点)+ node._execute(框架固定)调node.execute(node自定义),异常全部退出 + 恢复两种类型(故障恢复只捡crashed/手动恢复适用于paused/stopped) + bot工厂(启动后构建图实例+注入依赖+提供外部控制)+ 三表持久化schema(graph_specs/graph_instances/node_states,Snowflake ID非UUID,node_states每版本一行支持MVCC版本链)。

- [声明式图配置 GraphSpec](issues/history/08-declarative-graph-spec.md) — frozen Pydantic,完全可序列化。GraphSpec(name/nodes/edges/state_schema/scheduler/version/metadata)。NodeSpec(name/node_type/config/trigger),config注册时声明schema编译时验证。state_schema引入StateFactory工厂类ABC(在modex_graph)+StateRegistry,通用实现SimpleStateFactory/DynamicStateFactory(modex_graph),业务实现ReactStateFactory等(modex_agent)。预注册名(str)或内嵌schema(StateSchema)两种引用方式。GraphSpecCompiler在modex_graph,TopologyValidator纯确定性(环检测+node白名单+max_depth+可达性)。完整链路:GraphSpec→Compiler→CompiledGraph→GraphInstance(实例化,graph_instance_id)→GraphEngine执行。GraphSpec就是ADR-0033 D9.1 deferred的Preset graphs层。

- [图套图执行模型](issues/history/04-graph-nesting-execution-model.md) — 框架提供原语(GraphInstance/GraphEngine/StateFactory/持久化层/checkpoint-resume),节点自由组合。引入GraphInstance抽象(graph_instance_id持久化唯一key,parent_instance_id递归关联,统一持久化schema)。节点自行决定:如何创建子图/如何处理interrupt(catch消化或抛出)/如何桥接state/嵌套深度(无限制)。图层面InterruptPolicy ABC(默认CrashPolicy:全部暂停等重试,业务可自定义)。GraphEngine松耦合(ABC,可替换,基于Graph抽象)。修正:02 AgentNode execute可创建子图实例;08 完整链路补实例化步骤;10 run_id全部改为graph_instance_id。

- [未投递检测与唤醒机制](issues/history/03-undelivered-detection-and-wakeup.md) — "未投递"=情况E(transition不匹配且无default edge)。scheduler层面处理:_compile_routing发现无效路由→不raise RoutingError→创建新instance+state注入错误信息→重跑→agent看到错误自行修正。max_retry per node(默认3次)超限后raise RoutingError(安全网)。情况G(silent skip)保持不变(可能旁挂/END收尾)。hook主动检测"忘了投递"留作待办(非必备,after_node时序不足需after_dispatch新事件)。

- [taskId scope和语义](issues/history/05-taskid-scope-and-semantics.md) — 图调度场景中taskId=graph_instance_id(值相同,概念不同)。taskId是业务/external层概念(env注入),graph_instance_id是图调度层概念(Snowflake,持久化)。bot工厂创建GraphInstance时把graph_instance_id作为taskId通过env注入给ExternalEnvSpec。不统一概念,不改代码结构,加注释/文档说明。scope=图级(所有node共享),节点级标识是node_state_id。

- [长时node执行模型](issues/history/07-long-running-node-execution.md) — 复用现有阻塞await。核心设计加强:deliver(累积)→submit(投递)拆分,完全替代transition/command/state_update-as-payload/manual-dispatch/_compile_routing。三层方法拆分:_deliver(框架)/deliver(node)/_submit(框架)/submit(node)。InputIntegrator ABC整合多上游输入。deliver_states表持久化。state_update分离(只用于图级状态,不作为dispatch payload)。

- [预定义拓扑模板](issues/history/09-predefined-topology-templates.md) — 推迟实现(ROI低)。deliver/submit让大部分模式(conditional/map_reduce/pipeline)可直接用通用node构造,不需要单独通用实现。首要任务:React图适配新架构(ticket 11)。

- [边界:与现有系统的共存](原 issues/10,已移除) — 伪命题,所有问题已被其他ticket交叉回答。结论:图调度是ReAct的上层编排(不是新路径,收敛规则自动满足);与multi_agent分层共存(通信vs编排,不收敛);入口是并列的(bot工厂显式触发图调度,ReAct是默认路径)。无独立ticket,结论散落在02/04/07/10中。

- [共享知识库接口](issues/history/06-shared-knowledge-base-interface.md) — 业务功能增强,非框架级设计。modexctl kb CRUD加--by-task参数按taskId(=graph_instance_id)划区。带参数=task私有,不带=公共。补充deliver/submit不够表现上下文的场景。优先级低,框架+图实现完成后接入bot后再做。

## Not yet specified

<!-- fog of war — in-scope but not yet sharp enough to ticket -->

- **agent 自主生成图(AdaptiveNode)**:用户明确"先做手动配置图,agent 生成图是后续"。需要保留接口,但接口形态还不清晰——取决于 GraphSpec(GraphSpec 是否可序列化?是否 LLM 可生成?)。待 08-declarative-graph-spec 决策后可能 graduate。
- **taskId 可观测性贯穿**:taskId 作为 graph_id/run_id/node_id 可观测性 backbone。待 05-taskid-scope 决策后可能 graduate。注意:ticket 10 的 graph_instance_id 管理决策会影响 taskId 与 graph_instance_id 的关系(05 与 10 联动)。
- **动态图拓扑(运行时修改已编译图)**:用户选了"分层:预定义+自定义",v1 预定义,v2 开放任意图。但"开放任意图"的边界还不清晰。待 08 + 09 决策后可能 graduate。
- **知识库作为图复用仓库(wGraph)**:研究级,远期。把成功执行过的图 spec 存入知识库,新任务检索/生成子图。待 05 + 06 + 08 决策后可能 graduate。
- **图级 MVCC 轮次**:每轮循环是一个事务,轮次内所有节点看到同一版本。ticket 10 类别 2 中记为待办,优先级低,暂不设计。待 Node 级 MVCC 落地后评估是否需要图级版本。
- **hook 主动检测"未投递"**:ticket 03 中留作待办。after_node 时序不足(在路由编译之前调用),检测"未投递"需要路由后的新事件(after_dispatch / delivery_outcome)。未来实现可提高 agent 能力(主动提示"你还没投递")。
- ~~**transition/command 迁移**~~: ✅ 已完成 — deliver/submit 完全替代 transition/command。`NodeResult.transition` / `command` / `_compile_routing` 已移除,`execute` 是 async void,路由通过 `deliver()` / `submit()` + `ctx.dispatch(target, state_update={deliver payload})`。详见 `distributed-persistence.md` §8-9。
- **Node 级状态抽象 ABC 具体接口**:ticket 10 类别 2 中标记"待 ticket 02 落地后细化",但 02 已关闭未细化。接口需求(read/snapshot/restore/状态查询)已初步定义,具体方法签名待实现阶段细化。

## Out of scope

<!-- ruled beyond the destination -->

- **替代 ReActTurnRunner**:用户明确"没有必然关联"。ReAct 是图调度系统的一种 node,不是被替代的对象。
- **替代 multi_agent star topology**:同上。star topology 可以作为预定义拓扑模板,但不是被替代。
- **AdaptiveNode / LLM 自主生成图**:后续 phase。本 map 只保留接口,不实现。接口需求在 tickets 中体现。
- **KnowledgeBase 完整 RAG**:知识库是 modexctl kb 的业务功能增强(--by-task 参数),不是框架级 RAG 系统。完整 RAG(向量检索/embedding)超出本 map 范围。
