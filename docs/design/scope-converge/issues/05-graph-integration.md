# 05 图接缝：同一份点名册进 DAG，图的变量声明

Labels: wayfinder:deliberate
Status: closed (resolved 2026-08-18 per SPEC §8)
blocked-by: 03 (closed 2026-08-18)

## Question

modex_graph 引擎不动（这是本项目对参考项目的差异化能力，必须保留），只动两个消费接缝：

(a) **agent-as-node 同源**：BotAgentNodeFactory 直接消费统一装配器的产出——图节点上的 agent 与 pool 里的 agent 出自同一份点名册。这样"自定义 agent 跑在可中断恢复的 DAG 节点上"是免费的。

(b) **图变量声明**：GraphSpec YAML 里声明图的 state schema（字段名/类型/初值）与节点读写合同；状态持久化照旧走现有三层 store（NodeState/Deliver/GraphInstance）。

要决定：节点字段合同的校验时机（装载期校验即可？）；变量投影到 WebUI 的只读端点是否进本图；GraphSpec 的变量声明与 01 变量仓（若建）是否共用同一套类型登记。

## Comments

### Resolution (2026-08-18, deliberate Q13-Q17, per SPEC §8)

源码级确认：`GraphSpec.state_class: str`（`spec.py:103`）只引用预注册类；`BotAgentNodeFactory.create()`（`agent_node_factory.py:38`）从 pool 取预构建 agent，不构造。

#### Q13: agent-as-node 同源 — 已满足，保持当前模式

`BotAgentNodeFactory` 已从 pool 取预构建 agent（`_resolve_pool()` → `workspace.pools.get()` → `pool.pool.get()`）。不构造 agent，不绕过 pool。03 的 AssemblyPipeline 是 pool 级装配，图节点复用 pool 的预构建 agent——"同源"已满足。不引入第二条装配路径。当前无真实使用不阻碍设计。

`BotAgentNode`/`BotAgentNodeFactory` 留 BIZ（绑定 `WorkspaceResolverCell`/`KnowledgeNodeConfig`）。框架提供 `AgentNode` ABC。

#### Q14: GraphSpec state schema — 支持声明式，编译逻辑在 modex_agent 侧

modex_graph 的 `GraphSpec` 新增 `state_schema: dict[str, FieldSpec] | None`（与 `state_class` 互斥）。`FieldSpec` 是 modex_graph 的 frozen Pydantic model（`name`/`type`/`item_type`/`initial`）。`GraphSpecCompiler` 新增可选注入点 `state_schema_compiler: Callable[[dict[str, FieldSpec]], type[GraphState]] | None`。modex_agent 侧注入 compiler，从 ComponentRegistry 的 DATA_NAMESPACE 槽位解析自定义类型。

关键约束：modex_graph 是独立包（架构守卫强制不 import modex_agent），编译逻辑不能放在 modex_graph 里。这是"框架定信封（FieldSpec 形状），业务定信件（编译成什么 model）"的模式。

#### Q15: 变量投影到 WebUI — 不进本图

WebUI 是业务层，变量投影是 UI 关注点。WebUI 已有 graph REST API 可扩展。

#### Q16: GraphSpec 变量声明与 01 变量仓类型登记 — 共用

DATA_NAMESPACE 的 Pydantic model 既用于 KVStore 插件数据，也用于图 state schema。一个插件声明一个 DATA_NAMESPACE 类型，两处共用。统一了类型词汇（原则 4）。

#### Q17: 图装配代码搬家 — 框架级部分随 05 搬，业务级部分留 BIZ

| 代码 | 归属 |
|---|---|
| `GraphSpecLoader` YAML 解析 + GraphSpec 构造 | FW |
| `state_schema_compiler` 注入 + DATA_NAMESPACE 类型解析 | FW |
| NodeRegistry 注册（start/end/function/delay/human_input） | FW |
| GraphOrchestrator 装配 | FW |
| `BotAgentNodeFactory` 注册到 NodeRegistry | BIZ |
| `BotAgentNode` | BIZ |
| `WebUIGraphOutputAdapter` | BIZ |

完整设计见 `docs/design/scope-converge/SPEC.md` §8。
