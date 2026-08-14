# Static Graph Scheduling Design — Structure Map

## Data items

| # | Name | Location | Role |
|---|------|----------|------|
| D1 | GraphPayload | 11§5 | deliver content + user_input 统一载体(frozen Pydantic, content: str) |
| D2 | IntegratedInput | 01§120 | 下游 node 消费的上游 delivers 聚合(payloads: list[IntegratedPayload]) |
| D3 | IntegratedPayload | 01§122 | 单条 deliver 的 payload(source_node: node_id, content: GraphPayload) |
| D4 | GraphOutput | 11§6 | 图执行结果结构体(kind: GraphOutputKind, graph_instance_id, result, error) |
| D5 | GraphDeliverTarget | 06§2 | deliver 目标结构体(name: node_name, description: str) |
| D6 | NodeSpec | 04§3 | 图节点规格(name, node_type, config, trigger) |
| D7 | EdgeSpec | 04§3 | 图边规格(source, target) |
| D8 | GraphSpec | 04§3 | 图规格(name, version, nodes, edges, scheduler, state_class) |
| D9 | GraphMetadata | 03§237 | 图实例元数据(graph_instance_id, spec_id, status, node_id_map) |
| D10 | GraphRunRequest | 09§2 | POST /run 请求(user_input: GraphPayload) |
| D11 | GraphDeliverRequest | 07§2 / 09§2 | POST /deliver 请求(node_name, content: GraphPayload) |
| D12 | GraphSpecUpdateRequest | 09§2 | PUT /specs/{id} 请求(yaml_content: str) |
| D13 | GraphRunResponse | 09§2 | POST /run 响应(graph_instance_id, status) |
| D14 | GraphInstanceResponse | 09§2 | GET /instances/{id} 响应(graph_instance_id, status, nodes, result) |
| D15 | NodeStatusInfo | 09§2 | 节点状态信息(node_name, node_id, status, invocation_id) |
| D16 | GraphSpecResponse | 09§2 | GET /specs/{id} 响应(spec_id, name, version, yaml_content) |
| D17 | auto-deliver content | 05§3.8 | 从 AgentResult 提取的文本(反向扫描 messages → 剥 think) |
| D18 | IntegratedInput system-reminder | 05§3.6 | 格式化的 system-reminder 消息(role=SYSTEM_REMINDER, content=分章节文本) |
| D19 | deliver_states (SQLite table) | 03§237 | 持久化 deliver 记录(node_id, content, source_node, consumption_status) |
| D20 | node_states (SQLite table) | 03§237 | 持久化 node 状态(node_id, invocation_id, status) |
| D21 | graph_instances (SQLite table) | 03§237 | 持久化图实例(graph_instance_id, spec_id, status, metadata) |
| D22 | graph_specs (SQLite table) | 04§7 | 持久化 GraphSpec(spec_id, name, version, yaml_content) |
| D23 | _pending_delivers (in-memory list) | 01§88 / 06§4 | execute 期间临时累积的 delivers(submit 后 dispatch) |
| D24 | ctx.state.result | 11§2 | END 节点聚合的图结果(list[GraphPayload]) |
| D25 | ctx.user_input | 11§1 | GraphContext 中的用户输入(GraphPayload) |
| D26 | agent_context.graph_context | 05§3.7 | 图调度上下文标记(GraphContext | None) |

## States

| # | Name | Location | Role |
|---|------|----------|------|
| S1 | NodeInstanceStatus | 01§88 | PENDING / RUNNING / COMPLETED / CRASHED / SUSPENDED |
| S2 | GraphInstanceStatus | 03§237 | RUNNING / COMPLETED / CRASHED / PAUSED / STOPPED |
| S3 | DeliverConsumptionStatus | 01§104 | PENDING / CONSUMED_PENDING / CONSUMED |
| S4 | GraphOutputKind | 11§6 | COMPLETED / CRASHED |
| S5 | SessionStrategy | 05§3.4 | CACHED / PER_INVOCATION |
| S6 | SchedulerInstanceStatus | 01§88 | (清理项 C5: 和 NodeInstanceStatus 重复) |
| S7 | GraphSpec validation state | 04§10 | loaded / validated / compile_error |
| S8 | workspace lifecycle | 08§4 | active / evicting / stopped |

## Interfaces

| # | Name | Location | Role |
|---|------|----------|------|
| I1 | Node.execute(ctx, integrated_input) | 01§88 | 节点业务执行(ABC) |
| I2 | Node.deliver(content, target, ctx) | 01§90 | 累积 deliver 到 _pending_delivers(target: node_id) |
| I3 | Node._submit(ctx) | 01§90 | submit _pending_delivers → dispatch → route_deliver |
| I4 | Node._resolve_default_target(ctx) | 01§90 | None target → 所有下游 node_id 列表 |
| I5 | Node.run(graph=compiled) | 01§88 | 节点完整生命周期(begin→integrate→execute→submit→complete) |
| I6 | NodeRegistry.create(spec) | 05§3.1 | 收敛注入 node_id + 创建 Node |
| I7 | NodeRegistry.register(node_type, factory) | 04§8 | 注册 node type → factory |
| I8 | GraphSpecCompiler.compile(spec) | 11§2.1 | GraphSpec → CompiledGraph(含 START/END 实例化) |
| I9 | GraphPersistenceCoordinator.route_deliver | 01§104 | target_node_id → deliver_store.accumulate |
| I10 | GraphPersistenceCoordinator.register_node | 11§2 | 注册 node 的 deliver_store |
| I11 | GraphPersistenceCoordinator.collect_consumable_delivers | 01§104 | 下游 node 消费上游 delivers |
| I12 | GraphOrchestrator.create_and_run | 11§1 | 创建+执行图(含 user_input 注入) |
| I13 | GraphOrchestrator.create_instance | 09§5 | 创建图实例(不执行),返回 instance_id |
| I14 | GraphOrchestrator.run_instance | 09§5 | 后台执行图实例 |
| I15 | GraphOrchestrator.deliver_to_node | 07§6 | 外部 deliver(graph_instance_id, node_name, content) |
| I16 | GraphOrchestrator.pause / stop / resume | 09§4 | 图控制 |
| I17 | GraphOutputAdapter.emit(output) | 11§6 | 图结果推送 ABC |
| I18 | GraphToolPreset.build_tool_manager(base) | 05§3.7 | 拷贝 base tools + graph preset → 独立 ToolManager |
| I19 | GraphDeliverTool.execute(**kwargs) | 06§4 | agent self-deliver(name→node_id 转换) |
| I20 | GraphDeliverTargetStore.list() | 06§2 | 从拓扑提取下游 targets |
| I21 | GraphDeliverTargetStore.resolve_node_id(name) | 06§2 | name → node_id 转换 |
| I22 | AgentNode.resolve_description() | 06§2 | 返回节点业务描述(默认 [not found]) |
| I23 | AgentNode._ensure_session(ctx) | 05§3.4 | session 映射(nodeId.agentName) |
| I24 | AgentNode._resolve_agent_instance() | 05§3.2 | lazy 获取 pool 的 AgentInstance |
| I25 | BotAgentNode.execute(ctx, integrated_input) | 05§3.5 | 完整 execute 流程 |
| I26 | BotAgentNodeFactory.create(spec) | 05§3.10 | 从 spec.config 创建 BotAgentNode |
| I27 | StartNode.execute(ctx, integrated_input) | 11§1 | 从 ctx.user_input deliver 到下游 |
| I28 | EndNode.execute(ctx, integrated_input) | 11§2 | 收集 delivers → 聚合 → state.result |
| I29 | REST: POST /api/graphs/specs/{id}/run | 09§1 | 触发图执行 |
| I30 | REST: POST /api/graphs/instances/{id}/deliver | 07§3 / 09§1 | modexctl + WebUI 共享 deliver |
| I31 | REST: PUT /api/graphs/specs/{id} | 09§1 | 更新 YAML(校验+写回) |
| I32 | REST: GET /api/graphs/instances/{id} | 09§1 | 状态查询 |
| I33 | modexctl deliver command | 07§4 | Typer closure CLI |

## Objects

| # | Name | Location | Role |
|---|------|----------|------|
| O1 | GraphOrchestrator | 08§2 | per-workspace, 持有 spec_store/instance_store/coordinator_factory/output_adapter |
| O2 | GraphPersistenceCoordinator | 01§104 | per-instance, 持有 deliver_stores/node_state_stores |
| O3 | CompiledGraph | 11§2.1 | 编译产物,持有 nodes dict + edges |
| O4 | GraphInstance | 03§237 | 运行时实例(metadata + coordinator) |
| O5 | BotAgentNode | 05§3.2 | per-node, 持有 agent_name/pool_name/workspace_resolver/session |
| O6 | GraphDeliverTool | 06§4 | per-execution(通过 GraphToolPreset 创建) |
| O7 | GraphDeliverTargetStore | 06§2 | per-execution, 从 graph_ref + current_node 构建 |
| O8 | GraphToolPreset | 05§3.7 | per-execution, 创建独立 ToolManager |
| O9 | InMemoryToolManager (graph 专用) | 05§3.7 | per-execution, 拷贝 base + preset, GC 随 agent_context |
| O10 | PoolWorkspaceResources | 08§1 | per-workspace, 持有 pools/persistence/graph_orchestrator |
| O11 | WorkspaceResolverCell | 08§2 | per-workspace, late-binding holder |
| O12 | SessionInfo (graph node session) | 05§3.4 | per-node(CACHED)或 per-invocation, 注册到 pool.session_registry |
| O13 | AgentContext (graph 调度) | 05§3.5 | per-execution, graph_context 非 None + 独立 ToolManager |
| O14 | WebUIGraphOutputAdapter | 11§6 | per-workspace, WebSocket 推送 |
| O15 | GraphSpecStore (SQLite) | 04§7 | per-workspace, 持久化 GraphSpec |
| O16 | GraphInstanceStore (SQLite) | 04§7 | per-workspace, 持久化 GraphMetadata |
| O17 | DeliverStore (SQLite/InMemory) | 01§104 | per-node per-instance, 持久化 delivers |
| O18 | NodeStateStore (SQLite/InMemory) | 01§104 | per-node per-instance, 持久化 node 状态 |
| O19 | StartNode / EndNode 实例 | 11§1/§2 | per-graph-compile, 走 NodeRegistry.create |

## Concerns

| # | Name | Location | Paths |
|---|------|----------|-------|
| C1 | deliver 路径 | 01§88 | 3 条: deliver tool / modexctl deliver / auto-deliver → 收敛到 route_deliver |
| C2 | deliver target 标识 | 06§4 / 01§122 | agent 可见用 node_name(局部安全), 持久化层用 node_id(全局唯一) → name→node_id 转换 |
| C3 | ToolManager 隔离 | 05§3.7 | GraphToolPreset 创建独立 ToolManager(拷贝+preset), 不修改共享 ToolManager |
| C4 | 图调度 vs 常规会话区分 | 05§3.7 | agent_context.graph_context 字段(方案 A) + GraphToolPreset 是否存在 |
| C5 | session 映射 | 05§3.4 | nodeId.agentName → SessionIdFactory → CACHED(默认) / PER_INVOCATION(未来) |
| C6 | emitter 路径 | 05§3.3 | 复用 TurnContextBuilder.build_runtime_and_context(不造第三条路径) |
| C7 | START/END 实例化 | 11§1/§2 | 始终实例化, 默认框架基类, GraphSpec 可覆盖, 不做向后兼容 |
| C8 | GraphSpec 校验 | 04§10 | 加载时 model_validate + 编译时 TopologyValidator + START/END 校验 |
| C9 | 图执行模式 | 11§4 | 后台异步(asyncio.create_task), HTTP 立即返回 instance_id |
| C10 | desc 获取 | 06§2 | AgentNode.resolve_description() 多态, BotAgentNode 从 AgentInstance.descriptor 取 |
| C11 | deliver content 持久化 | 06§4 / PRD§6 | 临时累积 in-memory(_pending_delivers) + submit 后走 store(持久化策略可选) |
| C12 | node_id 注入 | 05§3.1 | NodeRegistry.create 收敛注入(不改 ABC) |
| C13 | END delivers 处理 | 11§2 | END 正常走 deliver_store, 统一路径(不特殊处理) |
| C14 | create_and_run 拆分 | 09§5 | create_instance(立即返回 ID) + run_instance(后台执行) |
