# 07 — modexctl deliver 命令

Status: ✅ design closed (直接实现设计,无待确认项)
Labels: wayfinder:active
Blocking: 无

## Question

**modexctl deliver: 外部 agent CLI 投递内容到图节点。**

### 上下文(基于代码探索确认)

**底层已就绪**:
- `GraphOrchestrator.deliver_to_node(graph_instance_id, node_name, content)` 已实现(graph_orchestrator.py:259-277)
- 委托 `GraphControlService.handle(ControlCommandType.DELIVER_TO_NODE)` → `coordinator.route_deliver()` → `deliver_store.accumulate()`(持久化)
- 同时通知 engine controller(`control.notify_deliver(node_name)`)

**参考: modexctl send 命令**(send.py:31-167):
- Typer closure 模式: `build_deliver_command(ctx: ModexCtlContext) -> Callable`
- 输入方式: positional args / `--content` / `--content-file` / `--stdin`(四种,互斥)
- HTTP client: `fetch_send(request)` → POST to loopback `/api/control/*`
- 结构体: `SendRequest`(frozen Pydantic)
- ack 格式化: `format_send_ack(result)`

**参考: modexctl app.py**(app.py:59-116):
- `build_app()` 工厂,`build_<name>_command(ctx)` closure
- 现有命令: agents / send / history

## Discussion

### 1. 命令设计

```bash
modexctl deliver --graph <instance_id> --node <node_name> --content "投递内容"
modexctl deliver --graph 123456 --node researcher --content-file input.txt
echo "分析报告" | modexctl deliver --graph 123456 --node researcher --stdin
```

**参数**:
- `--graph` (required): graph_instance_id(int)
- `--node` (required): 目标节点名(node_name,人类可读)
- `--content` / `--content-file` / `--stdin`: 输入方式(四种,互斥,同 send 命令模式)

### 2. 结构体

> **H4 修复** (2026-08-08): 统一为 ticket 09 版本——graph_instance_id 在 URL path 中,不在 body 中。

```python
# bot/control/models.py (或新文件)
class GraphDeliverRequest(BaseModel):
    """modexctl deliver 请求结构体(和 ticket 09 共享)。"""
    node_name: str
    content: GraphPayload    # ticket 11 §5, content: str

    model_config = ConfigDict(frozen=True, extra="forbid")
```

**注**: graph_instance_id 在 URL path 中(`/api/graphs/instances/{id}/deliver`),不在请求体中。和 ticket 09 的 WebUI deliver 共享同一路由和请求体。

### 3. HTTP 路由

> **H5 修复** (2026-08-08): 统一为 `/api/graphs/instances/{id}/deliver`(和 ticket 09 一致)。

```
POST /api/graphs/instances/{graph_instance_id}/deliver
  body: { "node_name": "researcher", "content": { "content": "投递内容" } }
  response: { "deliver_id": 123456, "status": "delivered" }
```

REST handler 调 `orchestrator.deliver_to_node(graph_instance_id, node_name, GraphPayload(content=...))`。

**和 ticket 09 的 WebUI deliver 统一路由**: modexctl 和 WebUI 是同一个端点 `/api/graphs/instances/{id}/deliver` 的两个消费方。

### 4. Typer closure 实现

```python
# bot/cli/modexctl/commands/deliver.py
def build_deliver_command(ctx: ModexCtlContext) -> Callable[..., None]:
    def _deliver(
        graph: Annotated[int, typer.Option("--graph", help="Graph instance ID.")],
        node: Annotated[str, typer.Option("--node", help="Target node name.")],
        message: Annotated[list[str] | None, typer.Argument(...)] = None,
        content: Annotated[str | None, typer.Option("--content")] = None,
        content_file: Annotated[Path | None, typer.Option("--content-file")] = None,
        use_stdin: Annotated[bool, typer.Option("--stdin")] = False,
    ) -> None:
        """Deliver content to a node in a running graph."""
        # 输入处理(复用 send 命令的 4 种模式逻辑)
        ...
        request = GraphDeliverRequest(
            graph_instance_id=graph,
            node_name=node,
            content=GraphPayload(content=content_str),
        )
        result = fetch_graph_deliver(request)
        typer.echo(f"Delivered to node '{node}' in graph {graph} (deliver_id={result.deliver_id})")

    return _deliver
```

### 5. app.py 注册

```python
# build_app() 中新增
from bot.cli.modexctl.commands.deliver import build_deliver_command
app.command(name="deliver", help="Deliver content to a node in a running graph.")(
    build_deliver_command(ctx)
)
```

### 6. node_name → node_id 转换

`GraphOrchestrator.deliver_to_node` 当前接收 `node_name`(graph_orchestrator.py:262)。node_id 对齐后(ADR-0036),orchestrator 内部需要:
- 从 `graph_instance` 的 CompiledGraph 中查找 `nodes[node_name].node_id`
- 调 `coordinator.route_deliver(target_node=node_id, ...)`

**modexctl 传 node_name**: 外部 agent 可见的是 name(人类可读),orchestrator 内部转换。

## 归属

| 层 | 内容 | 归属 |
|----|------|------|
| `GraphDeliverRequest` | 请求结构体 | **bot_project** |
| `fetch_graph_deliver` | HTTP client | **bot_project** |
| `build_deliver_command` | Typer closure | **bot_project** |
| app.py 注册 | build_app 加 deliver 命令 | **bot_project** |
| REST 端点 | POST /api/graphs/instances/{id}/deliver | **bot_project**(和 ticket 09 共享) |
| `orchestrator.deliver_to_node` | 底层调用 | **modex_agent**(已就绪) |
| orchestrator 内部 name→node_id 转换 | node_id 对齐 | **modex_agent**(ADR-0036) |

## 不做什么

- 不支持 invocation_id / continuation——图节点不是 agent 通信
- 不做 deliver 的 ack 格式化(直接 echo 确认信息)——后续可统一 ack 结构体
- 不在框架层新增 REST 端点——REST 在 bot_project
