# 声明式图配置(GraphSpec)

Status: triage:closed
Assignee: sisyphus
Started: 2026-08-02
Resolved: 2026-08-02

## Question

当前 modex_graph 只有编程式构建(`add_node` / `add_edge` / `compile`)。用户确认"前端点击配置手动构造图"——**需要 GraphSpec**。

**02 决议已回答的部分**:
- NodeSpec(type + config + name)在 modex_graph(可序列化数据载体)
- NodeFactory ABC + NodeRegistry 在 modex_graph(抽象)+ modex_agent(业务实现)
- 通用 Node 工厂在 modex_graph,AgentNodeFactory 在 modex_agent
- 编译器位置:NodeFactory/Registry 抽象在 modex_graph,GraphSpecCompiler 也在 modex_graph(通用)

**仍需决策**:

1. **GraphSpec 的结构** — 
   - nodes: list[NodeSpec](node_name + node_type + config)
   - edges: list[EdgeSpec](source + target + reason)
   - state_schema: 如何引用 GraphState 子类?(02 grilling 中"稍后考虑"的问题,仍未解决)
   - scheduler: "linear" | "parallel"
   - 如何序列化/反序列化?Pydantic frozen model?

2. **NodeSpec 的 config 如何表达?** — 02 决议倾向"注册时声明 config schema"。需确认:
   - 工厂注册时声明 config 的 Pydantic model class
   - GraphSpec 用 dict 存 config,编译时用对应 model 验证
   - 未注册的 node_type 如何处理(报错 / 跳过)?

3. **state_schema 问题(02 未解决)** — GraphSpec 如何引用 GraphState 子类?
   - 字符串类路径动态 import(如 `'mypkg.MyState'`)?
   - GraphSpec 不含 state schema,调用方传 state 实例?
   - 内嵌 state schema 定义(字段名 + 类型 + channel 声明)?

4. **GraphSpec → CompiledGraph 的编译器** — 
   - 编译器在 modex_graph(02 已确认)
   - 需要 TopologyValidator?(纯确定性校验:环检测 + node 白名单 + max_depth)
   - 编译器如何获取 NodeFactory/Registry?(传入 / 全局注册表?)

5. **与 ticket 10 联动** — GraphSpec 既是"配置载体"又是"持久化格式"。ticket 10 的图定义持久化(类别 3)依赖 GraphSpec 的可序列化性。两个 ticket 需要协同:GraphSpec 的结构决定持久化格式,持久化需求反过来影响 GraphSpec 的设计(如版本号字段、生命周期状态字段)。

6. **与 ADR-0033 D9.1 "Preset graphs (deferred)" 的关系** — ADR-0033 说"只有 ReAct 一个消费者,预设库是投机抽象"。现在图调度系统是第二个消费者,满足了 ADR-0007 的条件。GraphSpec 是否就是 D9.1 中 deferred 的 "Preset graphs" 层?

## Context

- 用户之前:"先实现整个手动配置图+图调度实现,为上层调度保留接口和开放性"
- 用户之前:agent 生成图"也是一种图,只是从用户自定义变成了 agent 生成,它们都依赖完整的图配置+图实现"
- ADR-0033 D9.1:三层分离(Core engine / Preset graphs deferred / Business graphs)
- ADR-0007:两个用例才提升 seam。图调度系统是第二个消费者。
- Lár AdaptiveNode:LLM 产出 JSON GraphSpec → TopologyValidator → 确定性执行
- dify:workflow canvas(JSON 定义)+ 节点工厂注册表

## Resolution criteria

明确以下决策:
- GraphSpec 结构(nodes/edges/state_schema/scheduler/序列化方式)
- NodeSpec config 表达方式(注册时声明 config schema + 编译时验证)
- state_schema 引用方式(字符串类路径 / 调用方传实例 / 内嵌定义)
- 编译器设计(NodeFactory/Registry 获取方式 + TopologyValidator)
- 与 ticket 10 的协同(GraphSpec 作为持久化格式 + 版本号/生命周期状态字段)
- 与 D9.1 "Preset graphs" 的关系

## Resolution

### 1. GraphSpec 结构

frozen Pydantic BaseModel,完全可序列化(JSON/dict)。

```python
class GraphSpec(BaseModel, frozen=True, extra="forbid"):
    name: str                              # 图名称
    nodes: list[NodeSpec]                  # 节点定义
    edges: list[EdgeSpec]                  # 边定义
    state_schema: str | StateSchema        # state 工厂引用(预注册名)或内嵌 schema
    scheduler: Literal["linear", "parallel"] = "linear"
    version: str = "1"                     # GraphSpec 版本号(与 ticket 10 持久化联动)
    metadata: dict[str, Any] = {}          # 自定义元数据(生命周期状态等,与 ticket 10 联动)

class NodeSpec(BaseModel, frozen=True, extra="forbid"):
    name: str                              # 节点名称(图中唯一)
    node_type: str                         # 注册的节点类型(如 "function" / "agent" / "graph")
    config: dict[str, Any]                 # 节点配置(编译时用对应 config model 验证)
    trigger: Literal["on_receive", "on_all_preds"] | None = None  # 触发模式,None 用默认

class EdgeSpec(BaseModel, frozen=True, extra="forbid"):
    source: str                            # 源节点名
    target: str                            # 目标节点名

class StateSchema(BaseModel, frozen=True, extra="forbid"):
    """可序列化的 state 结构描述,用于内省/校验/传输/agent 生成。"""
    fields: list[StateFieldSpec]

class StateFieldSpec(BaseModel, frozen=True, extra="forbid"):
    name: str
    type: str                              # 类型表达式(如 "str", "list[str]", "int | None")
    channel: Literal["last_value", "reducer"] = "last_value"
    reducer: str | None = None             # channel="reducer" 时的 reducer 名(注册名)
    default: Any | None = None             # 默认值(JSON 兼容)
```

### 2. NodeSpec config 表达:注册时声明 config schema

02 决议已确认。细化:

- NodeFactory 注册时同时声明 `config_model: type[BaseModel] | None`
- GraphSpec 中 NodeSpec.config 是 `dict[str, Any]`(JSON 兼容)
- 编译时:GraphSpecCompiler 用对应 node_type 的 config_model 验证 config dict
- 未注册的 node_type → 编译报错(`UnknownNodeTypeError`)
- config_model 为 None → config dict 不验证(透传给工厂)

### 3. state_schema 引用方式:StateFactory 工厂类 + 预注册/内嵌混合

**核心设计**:引入 `StateFactory` 工厂类(ABC),外部传入实现。GraphSpec 通过两种方式引用:

- **预注册名**(`state_schema: str`):常用 state 类预注册,简单场景直接引用
- **内嵌 schema**(`state_schema: StateSchema`):自定义 state,从字段定义动态构建

```python
class StateFactory[S: GraphState](ABC):
    """state 工厂抽象。外部传入实现,负责创建/恢复 state 实例。"""

    @abstractmethod
    def create_state(self) -> S:
        """创建默认 state 实例。"""

    @abstractmethod
    def state_schema(self) -> StateSchema:
        """返回可序列化的 state 结构描述(用于内省/校验/传输)。"""

    @abstractmethod
    def restore_state(self, data: dict[str, Any]) -> S:
        """从 checkpoint 数据恢复 state 实例。"""
```

**通用实现(modex_graph)**:
- `SimpleStateFactory`:预注册 GraphState 子类的简单包装。`create_state` = `cls()`,`restore_state` = `cls.from_checkpoint(data)`
- `DynamicStateFactory`:从内嵌 StateSchema 动态构建 GraphState 子类(用 Pydantic 动态模型创建 + `_setup_channels` 机制)

**业务实现(modex_agent)**:
- `ReactStateFactory`:ReActTurnState 的工厂
- 其他业务 state 工厂按需实现

**分层**(与 NodeFactory 一致):

| 层 | 内容 |
|---|------|
| modex_graph(抽象) | StateFactory ABC + StateRegistry |
| modex_graph(通用实现) | SimpleStateFactory / DynamicStateFactory |
| modex_agent(业务实现) | ReactStateFactory 等 |

**GraphSpec 可序列化**:
- `state_schema: str` → 预注册名,通过 StateRegistry 查找 StateFactory
- `state_schema: StateSchema` → 内嵌 schema,用 DynamicStateFactory 动态构建
- 两者都是 JSON 兼容(str 或嵌套 dict)

**注意**:`str | StateSchema` union 类型在 Pydantic v2 反序列化时需要验证自动解析是否正确工作。如有问题,加 discriminator 或用 `Annotated` 标注。实现时验证。

**StateRegistry 与 NodeRegistry 的关系**:
- 独立的两个 registry(各管各的):`NodeRegistry` 注册 node factory,`StateRegistry` 注册 state factory
- 统一的注册入口(可选):`ComponentRegistry` 聚合两者,提供 `register_defaults()` 批量注册内置组件

### 4. GraphSpecCompiler 设计

**位置**:modex_graph(通用)

```python
class GraphSpecCompiler:
    def __init__(self, node_registry: NodeRegistry, state_registry: StateRegistry): ...

    def compile(self, spec: GraphSpec) -> CompiledGraph:
        # 1. 解析 state_schema → StateFactory(不创建 state 实例,state 在 GraphInstance 实例化时创建)
        state_factory = self._resolve_state_factory(spec.state_schema)

        # 2. 构建 Graph builder(不绑定具体 state,state 在 GraphInstance 级别)
        graph = Graph()
        for node_spec in spec.nodes:
            node = self.node_registry.create(node_spec.node_type, node_spec.config)
            graph.add_node(node_spec.name, node, trigger=node_spec.trigger)
        for edge_spec in spec.edges:
            graph.add_edge(edge_spec.source, edge_spec.target)

        # 3. 校验拓扑(TopologyValidator)
        self._validate_topology(graph)

        # 4. 编译
        return graph.compile()
```

**TopologyValidator**(纯确定性,不委托 LLM):
- 环检测(有向环合法,但需 max_depth 防无界递归)
- node 白名单(所有 node_type 已注册)
- max_depth / max_nodes 限制
- START → END 可达性(至少一条路径)
- 结构完整性(边的 source/target 都在 nodes 中)

### 5. 与 ticket 10 联动

GraphSpec 既是"配置载体"又是"持久化格式":

- **`version` 字段**:GraphSpec 版本号,持久化时存储,恢复时校验兼容性
- **`metadata` 字段**:自定义元数据,ticket 10 的图生命周期状态(designed/created/executing/paused/completed/failed)存这里
- **`state_schema` 持久化**:GraphSpec 持久化时,state_schema(预注册名或内嵌 schema)一起序列化,恢复时通过 StateRegistry/DynamicStateFactory 重建
- **checkpoint 与 GraphSpec 的关系**:GraphSpec 是"图定义"(持久),checkpoint 是"执行状态"(每次 run 产生)。完整链路:GraphSpec(定义) → GraphSpecCompiler(编译拓扑,不创建 state) → CompiledGraph(编译产物) → GraphInstance(实例化,分配 graph_instance_id,StateFactory.create_state() 创建 state) → GraphEngine 执行 → checkpoint 挂在 GraphInstance 上。恢复时:GraphSpec → CompiledGraph + GraphInstance(graph_instance_id) → StateFactory.restore_state() → 重建状态 + 重新 dispatch

### 6. 与 ADR-0033 D9.1 "Preset graphs" 的关系

GraphSpec + GraphSpecCompiler + NodeFactory/Registry + StateFactory/Registry **就是** D9.1 中 deferred 的 "Preset graphs" 层的实现。图调度系统是第二个消费者(第一个是 ReAct),满足 ADR-0007 的"两个用例才提升 seam"条件。

**三层分离(ADR-0033 D9.1)的最终形态**:

| 层 | 状态 | 内容 |
|---|------|------|
| Core engine | ✅ 已实现 | modex_graph(Graph/Node/Engine/Scheduler/Channel/State) |
| **Preset/Config layer** | ✅ 本 ticket 决议 | GraphSpec + GraphSpecCompiler + NodeFactory/Registry + StateFactory/Registry |
| Business graphs | ✅ 已有 + 扩展 | ReAct(build_react_graph)+ 图调度系统(bot_project) |

### deliver/submit 修正(来自 ticket 07)

- NodeSpec 可增加 timeout 配置(如 `timeout_seconds: int | None`)
- EdgeSpec 的 reason 字段已移除(deliver 显式指定 next_node,静态边只定义拓扑)
- NodeSpec 可增加 input_integrator 配置(指定用哪个 InputIntegrator,默认通用实现)
