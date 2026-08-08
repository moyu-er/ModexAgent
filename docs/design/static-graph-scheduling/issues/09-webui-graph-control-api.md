# 09 — WebUI graph control API

Status: ✅ design closed (直接实现设计,无待确认项)
Labels: wayfinder:active
Blocking: 10-webui-graph-visual-config

## Question

**WebUI REST 端点: GraphSpec CRUD + 图控制(run/pause/resume/stop/deliver)+ 状态查询。**

### 上下文(基于代码探索确认)

**WebUI 框架**: aiohttp(非 FastAPI)。路由通过 `register_*_routes(server)` 函数注册。
**参考: PoolConfigController**: pools CRUD 的 Controller 模式。

**底层已就绪**:
- `GraphOrchestrator.create_and_run(spec_id, user_input=...)` — ticket 11
- `GraphOrchestrator.pause / stop / resume / deliver_to_node` — 已实现
- `GraphPersistenceCoordinator.get_graph_state(node_status_filter)` — 已实现(persistence_coordinator.py:338-364),**但 GraphOrchestrator 未委托暴露** → 需添加 `GraphOrchestrator.get_state(graph_instance_id)` 委托方法
- `GraphSpecStore` — spec 持久化(ticket 04)
- `GraphOutputAdapter` — 结果推送(ticket 11 §6)

## Discussion

### 1. REST 端点总览

```
# GraphSpec CRUD
GET    /api/graphs/specs                          → 列表
GET    /api/graphs/specs/{spec_id}                → 详情(含 YAML)
PUT    /api/graphs/specs/{spec_id}                → 更新 YAML(后端校验 parse+validate → 通过则写回文件,失败返回错误)

# 图控制
POST   /api/graphs/specs/{spec_id}/run            → 触发执行(user_input)
POST   /api/graphs/instances/{id}/pause           → 暂停
POST   /api/graphs/instances/{id}/resume          → 恢复
POST   /api/graphs/instances/{id}/stop            → 停止
POST   /api/graphs/instances/{id}/deliver         → 投递(modexctl 共享)

# 状态查询
GET    /api/graphs/instances/{id}                 → 实例状态(含节点状态)
GET    /api/graphs/instances                       → 实例列表(可选过滤 status)
```

### 2. 结构体(请求/响应,统一 frozen Pydantic 风格)

```python
# ── 请求 ──────────────────────────────────────────────────────────────

class GraphRunRequest(BaseModel):
    """POST /api/graphs/specs/{spec_id}/run"""
    user_input: GraphPayload | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")

class GraphDeliverRequest(BaseModel):
    """POST /api/graphs/instances/{id}/deliver (modexctl + WebUI 共享)"""
    node_name: str
    content: GraphPayload

    model_config = ConfigDict(frozen=True, extra="forbid")

class GraphSpecUpdateRequest(BaseModel):
    """PUT /api/graphs/specs/{spec_id}"""
    yaml_content: str

    model_config = ConfigDict(frozen=True, extra="forbid")

# ── 响应 ──────────────────────────────────────────────────────────────

class GraphSpecResponse(BaseModel):
    """GET /api/graphs/specs/{spec_id}"""
    spec_id: int
    name: str
    version: str  # L1 修复: 和 SQLite TEXT 一致,不用 int
    yaml_content: str
    created_at: int

    model_config = ConfigDict(frozen=True, extra="forbid")

class GraphSpecListResponse(BaseModel):
    """GET /api/graphs/specs"""
    specs: list[GraphSpecSummary]

    model_config = ConfigDict(frozen=True, extra="forbid")

class GraphSpecSummary(BaseModel):
    spec_id: int
    name: str
    version: str  # L1 修复: 和 SQLite TEXT 一致

    model_config = ConfigDict(frozen=True, extra="forbid")

class GraphInstanceResponse(BaseModel):
    """GET /api/graphs/instances/{id}"""
    graph_instance_id: int
    status: str  # running|completed|crashed|paused|stopped
    spec_id: int
    nodes: list[NodeStatusInfo]
    result: Any = None  # completed 时有值

    model_config = ConfigDict(frozen=True, extra="forbid")

class NodeStatusInfo(BaseModel):
    """图实例中单个节点的状态信息"""
    node_name: str
    node_id: str
    status: str  # PENDING|RUNNING|COMPLETED|CRASHED|SUSPENDED
    invocation_id: int | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")

class GraphRunResponse(BaseModel):
    """POST /api/graphs/specs/{spec_id}/run 立即返回"""
    graph_instance_id: int
    status: str = "running"

    model_config = ConfigDict(frozen=True, extra="forbid")
```

### 3. GraphSpec CRUD: YAML 编辑器后端

**GET /api/graphs/specs/{spec_id}**: 返回 YAML 内容(从文件或 store 读取)。

**PUT /api/graphs/specs/{spec_id}**: 更新 YAML。
- 接收 `yaml_content: str`
- **校验**: parse YAML → GraphSpec → `compiler.validate(spec)`(H9 修复: 需在 GraphSpecCompiler 添加 `validate(spec)` 方法,运行 TopologyValidator 但不创建 nodes/edges)
- 校验通过 → 写回 YAML 文件 + 更新 GraphSpecStore
- 校验失败 → 返回错误信息(不写回)

```python
async def update_spec(spec_id: int, request: GraphSpecUpdateRequest) -> Response:
    # 1. parse YAML
    try:
        spec_dict = yaml.safe_load(request.yaml_content)
    except yaml.YAMLError as e:
        return json_response({"error": f"YAML parse error: {e}"}, status=400)

    # 2. validate GraphSpec
    try:
        spec = GraphSpec.model_validate(spec_dict)
        compiler.validate(spec)  # 拓扑校验
    except ValidationError as e:
        return json_response({"error": f"Invalid graph spec: {e}"}, status=400)

    # 3. 写回文件 + 更新 store
    write_yaml_file(spec, request.yaml_content)
    spec_store.save(spec)

    return json_response(GraphSpecResponse(...).model_dump())
```

**创建/删除**: 本期不做。YAML 文件管理为主,新增图 = 新建 YAML 文件 + 重启加载。后续可支持 POST `/api/graphs/specs` 创建。

### 4. 图控制端点

```python
# POST /api/graphs/specs/{spec_id}/run
async def run_graph(spec_id: int, request: GraphRunRequest) -> Response:
    # asyncio.create_task: 图后台异步执行(ticket 11 §4)
    asyncio.create_task(
        orchestrator.create_and_run(spec_id, user_input=request.user_input)
    )
    # 立即返回 graph_instance_id(需要先 create + register,再后台 execute)
    # TODO: create_and_run 需要拆分为 create + execute 两步,以便立即返回 instance_id
    return json_response(GraphRunResponse(graph_instance_id=..., status="running").model_dump())

# POST /api/graphs/instances/{id}/pause
async def pause_graph(id: int) -> Response:
    await orchestrator.pause(id)
    return json_response({"status": "paused"})

# POST /api/graphs/instances/{id}/resume
async def resume_graph(id: int) -> Response:
    await orchestrator.resume(id)
    return json_response({"status": "running"})

# POST /api/graphs/instances/{id}/stop
async def stop_graph(id: int) -> Response:
    await orchestrator.stop(id)
    return json_response({"status": "stopped"})

# POST /api/graphs/instances/{id}/deliver (modexctl + WebUI 共享)
async def deliver_to_node(id: int, request: GraphDeliverRequest) -> Response:
    await orchestrator.deliver_to_node(id, request.node_name, request.content)
    return json_response({"status": "delivered"})
```

### 5. create_and_run 拆分(实现细节)

> **M1+M5+M6 修复** (2026-08-08): create_instance/run_instance 拆分 + _active_instances 终态移除 + GraphOutput 在 finally 中 emit。

`create_and_run` 当前是原子调用(创建 + 执行)。ticket 11 §4 要求立即返回 `graph_instance_id`。需要拆分:

```python
async def create_instance(self, spec_id: int, *, user_input: GraphPayload | None = None) -> int:
    """创建图实例(不执行),返回 graph_instance_id。

    M5 修复: 状态设为 PENDING(不是 RUNNING),run_instance 才设 RUNNING。
    user_input 存入 GraphInstance(M8 修复: create→run 之间 user_input 不丢失)。
    """
    spec = self._load_spec(spec_id)
    compiled = self._compiler.compile(spec)
    graph_instance_id = default_id_generator().generate()
    metadata = GraphMetadata(status=GraphInstanceStatus.PENDING)  # M5: PENDING 而非 RUNNING
    self._instance_store.save(metadata)
    coordinator = self._coordinator_factory.create(graph_instance_id, self._instance_store)
    for node_name in compiled.nodes:
        coordinator.register_node(...)
    instance = GraphInstance(metadata, coordinator, compiled, user_input=user_input)  # M8: user_input 存入 instance
    self._active_instances[graph_instance_id] = instance
    return graph_instance_id

async def run_instance(self, graph_instance_id: int) -> None:
    """后台执行图实例。

    M1 修复: 终态(COMPLETED/CRASHED/STOPPED)后从 _active_instances 移除。
    M6 修复: GraphOutput 在 finally 中 emit,保证 crash 也推送。
    """
    instance = self._active_instances[graph_instance_id]
    instance.metadata.status = GraphInstanceStatus.RUNNING  # M5: 正式设 RUNNING
    state = instance.compiled.state_class()
    ctx = GraphContext(state=state, ..., user_input=instance.user_input)  # M8: 从 instance 取 user_input
    try:
        await self._engine.run_async(ctx)
        output = GraphOutput(
            kind=GraphOutputKind.COMPLETED,
            graph_instance_id=graph_instance_id,
            result=state.result,
        )
    except Exception as e:
        output = GraphOutput(
            kind=GraphOutputKind.CRASHED,
            graph_instance_id=graph_instance_id,
            error=str(e),
        )
    finally:
        # M6: 在 finally 中 emit,保证 crash 也推送
        if self._output_adapter is not None:
            await self._output_adapter.emit(output)
        # M1: 终态后从 _active_instances 移除(PAUSED 保留)
        if output.kind in (GraphOutputKind.COMPLETED, GraphOutputKind.CRASHED):
            self._active_instances.pop(graph_instance_id, None)
```

REST handler:
```python
async def run_graph(spec_id, request):
    gid = await orchestrator.create_instance(spec_id, user_input=request.user_input)
    asyncio.create_task(orchestrator.run_instance(gid))
    return GraphRunResponse(graph_instance_id=gid, status="running")
```

**注**: `create_and_run` 保留为便捷方法(同步 await,测试用)。GraphInstance 加 `compiled` + `user_input` 字段(M8: create→run 之间 user_input 不丢失)。

### 6. 状态查询

```python
# GET /api/graphs/instances/{id}
async def get_instance(id: int) -> Response:
    # H7 修复: 需在 GraphOrchestrator 添加 get_state 委托方法
    # get_state 委托 coordinator.get_graph_state()
    snapshot = orchestrator.get_state(id)  # TODO: 添加此委托方法到 GraphOrchestrator
    # 从 GraphStateSnapshot 构造 GraphInstanceResponse
    nodes = [
        NodeStatusInfo(
            node_name=name,
            node_id=graph_metadata.node_id_map.get(name, name),  # L2 修复: 从 node_id_map 取
            status=str(latest.status),
            invocation_id=latest.invocation_id,
        )
        for name, versions in snapshot.nodes.items()
        for latest in [versions[-1] if versions else None]
        if latest is not None
    ]
    result = state.result if hasattr(state, 'result') else None
    return GraphInstanceResponse(
        graph_instance_id=id,
        status=str(snapshot.metadata.status),
        spec_id=snapshot.metadata.spec_id,
        nodes=nodes,
        result=result,
    )
```

### 7. 路由注册

```python
# bot/webui/routes/graph_routes.py
def register_graph_routes(server: web.Application, orchestrator: GraphOrchestrator) -> None:
    server.router.add_get("/api/graphs/specs", list_specs)
    server.router.add_get("/api/graphs/specs/{spec_id}", get_spec)
    server.router.add_put("/api/graphs/specs/{spec_id}", update_spec)
    server.router.add_post("/api/graphs/specs/{spec_id}/run", run_graph)
    server.router.add_post("/api/graphs/instances/{id}/pause", pause_graph)
    server.router.add_post("/api/graphs/instances/{id}/resume", resume_graph)
    server.router.add_post("/api/graphs/instances/{id}/stop", stop_graph)
    server.router.add_post("/api/graphs/instances/{id}/deliver", deliver_to_node)
    server.router.add_get("/api/graphs/instances/{id}", get_instance)
    server.router.add_get("/api/graphs/instances", list_instances)
```

## 归属

| 层 | 内容 | 归属 |
|----|------|------|
| REST 端点 + 路由注册 | aiohttp routes | **bot_project** |
| 请求/响应结构体 | frozen Pydantic | **bot_project** |
| GraphSpec YAML 校验 | parse + compiler.validate | **bot_project** |
| GraphSpec YAML 写回文件 | 文件写回 | **bot_project** |
| `create_instance` + `run_instance` 拆分 | GraphOrchestrator 方法 | **modex_agent** |
| `get_state` 状态查询 | 需添加委托方法到 GraphOrchestrator(调 coordinator.get_graph_state) | **modex_agent**(TODO) |
| GraphControlService 控制 | pause/resume/stop/deliver(已就绪) | **modex_agent** |

## 不做什么

- 不做 GraphSpec 的 POST 创建 / DELETE 删除——YAML 文件管理为主,后续增强
- 不做可视化编辑器前端——ticket 10
- 不做图执行的长轮询——WebSocket 推送(ticket 11 §4)
- 不做 GraphSpec 热更新——启动时加载,更新需重启或手动 reload
