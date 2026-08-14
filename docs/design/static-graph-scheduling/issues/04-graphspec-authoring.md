# 04 — GraphSpec 创作方式

Status: closed
Labels: wayfinder:resolved
Blocking: 08-webui-graph-control-api, 09-webui-graph-visual-config

## Question

**用户如何创建和编辑 GraphSpec？静态图调度的图定义从哪来？**

## Resolution

### 1. 创作入口：YAML + WebUI，和 pool config 模式对齐

目录结构（和 `config/pools/<name>/pool.yml` 一致）：

```
config/graphs/
├── research-pipeline/
│   └── graph.yml       # GraphSpec YAML 序列化
└── coder-workflow/
    └── graph.yml
```

链路：
```
开发者手编 YAML → bot 启动扫描 config/graphs/ → 加载到 GraphSpecStore (SQLite) 作为运行时副本
WebUI 编辑 → REST → GraphConfigController → 写回 YAML → 重新解析 → GraphSpecStore.save()
触发图执行 → GraphOrchestrator.create_and_run(spec_id) → 从 SQLite 读 spec_json → 编译 → GraphInstance
```

### 2. 两阶段 source of truth

| 阶段 | Source of truth | 说明 |
|------|----------------|------|
| **创作/编辑阶段** | YAML 文件（`config/graphs/<name>/graph.yml`） | 开发者手编 / WebUI 编辑写回 YAML |
| **实例化后** | SQLite（`graph_specs` + `graph_instances` + `node_states` + `deliver_states`） | 图实例独立存在，不依赖 YAML |

**关键语义**：graph instance 创建时（`create_and_run()`）从 SQLite 的 `graph_specs` 行编译，不从 YAML 直接编译。YAML 修改后**已存在的 graph instance 不受影响**——它的 spec_id 指向 SQLite 中那行 spec_json，是实例化时的拓扑快照。避免修改 YAML 后原图被破坏。

### 3. spec_id 和 version 的语义

| 字段 | 语义 | 生成方式 |
|------|------|---------|
| `spec_id` (Snowflake PK) | YAML 版本的标识，每次保存生成新的 | 后端 `default_id_generator()` |
| `name` | 图名，用户可改 | 用户定义 |
| `version` | 保存次数计数器 | 前端 WebUI 修改保存后后端自动 +1；开发者手编 YAML 首次创建时 version = "1" |

**不做 UNIQUE 约束**——`name` + `version` 不构成唯一性。可以删除 + 新建同名 graph，每次新建都是新 spec_id + version 从 1 开始。spec_id 和 version 都不用做身份，也不用于唯一性功能。

**graph_instances 表的唯一性是 graph_instance_id**（已有的 Snowflake），不依赖 spec_id。spec_id 只是 graph_instances 里的引用字段——"这个 instance 是从哪个 YAML 版本编译来的"。

### 4. version 自动递增逻辑

```
WebUI 编辑 graph → 保存 → GraphConfigController:
  1. 查 graph_specs WHERE name=? ORDER BY version DESC → 最新 version（如 "3"）
  2. version + 1 = "4"
  3. 解析 YAML/JSON → GraphSpec(name=name, version="4", ...)
  4. GraphSpecStore.save() → 新行 spec_id=新Snowflake, name=name, version="4"
```

### 5. 加载策略

bot 启动 / YAML 修改后重载时：
1. 扫描 `config/graphs/<name>/graph.yml` → 解析为 GraphSpec（含 name + version）
2. 查 `graph_specs WHERE name=? AND version=?`
3. **存在且 spec_json hash 一致** → 跳过（复用 spec_id）
4. **不存在** → 插入新行
5. **存在但 hash 不一致** → 报错（YAML 被手改但 version 没递增，提示重新通过 WebUI 保存或手改 version）

### 6. 触发图执行时

```
用户选图名 → 查 graph_specs WHERE name=? ORDER BY version DESC → 取最新 version 的 spec_id
→ create_and_run(spec_id) → graph_instances.spec_id = 该 spec_id（快照引用）
```

### 7. Schema 改动

当前 `graph_specs` 表：
```sql
CREATE TABLE graph_specs (
    spec_id         BIGINT  PRIMARY KEY,
    name            TEXT    NOT NULL,
    version         TEXT    NOT NULL DEFAULT '1.0',
    spec_json       TEXT    NOT NULL CHECK (json_valid(spec_json)),
    UNIQUE (name, version)   -- 去掉
);
```

改为：
```sql
CREATE TABLE graph_specs (
    spec_id         BIGINT  PRIMARY KEY,
    name            TEXT    NOT NULL,
    version         TEXT    NOT NULL DEFAULT '1',
    spec_json       TEXT    NOT NULL CHECK (json_valid(spec_json))
    -- 无 UNIQUE 约束
);
```

索引调整：`idx_graph_specs_name` 保留（查最新 version 用），去掉 `UNIQUE(name, version)`。

### 8. 节点类型注册

> **修正** (2026-08-08): START/END 始终实例化为 Node,默认用框架基类,GraphSpec 可显式覆盖。

bot 启动时注册 `NodeRegistry`：
- `agent` → `AgentNodeFactory`（持有 workspace 的 pool 引用，通过 `WorkspaceResolverCell` 晚绑定）
- `function` → `FunctionNodeFactory`（确定性函数节点）
- `delay` → `DelayNodeFactory`
- `human_input` → `HumanInputNodeFactory`
- `graph` → `GraphAsNodeFactory`（子图嵌套）
- `start` → 默认 `StartNode`（框架基类,GraphSpec 未显式定义 START 时使用）
- `end` → 默认 `EndNode`（框架基类,GraphSpec 未显式定义 END 时使用）

**START/END 实例化规则**(ticket 11 §1/§2 修正):
- 所有 graph 有且仅有一个 START 和一个 END Node 实例
- GraphSpec 可显式定义 START/END 的 NodeSpec(node_type + config),业务继承重写
- 未显式定义时,GraphSpecCompiler 用默认 `start` / `end` node_type 创建框架基类
- 不做向后兼容: sentinel 常量模式(无 Node 实例)废除

WebUI 编辑器中用户从已注册的 node_type 列表中选择；agent 节点的 `config.agent` / `config.pool` 从 workspace 的可用 pool/agent 列表中选（和 PoolEditor 选 agent 一致）。START/END 节点也可在 WebUI 中选择自定义 node_type。

### 9. YAML 示例

#### 默认 START/END(不显式定义)

```yaml
name: research-pipeline
version: "1"
scheduler: parallel
default_trigger: on_all_preds
max_iterations: 25
state_class: GraphState

nodes:
  - name: research
    node_type: agent
    config:
      agent: default
      pool: default
    trigger: null

  - name: planner
    node_type: agent
    config:
      agent: default
      pool: coder

  - name: writer
    node_type: agent
    config:
      agent: default
      pool: default

edges:
  - { source: __START__, target: research }
  - { source: research, target: planner }
  - { source: planner, target: writer }
  - { source: writer, target: __END__ }
```

未显式定义 `__START__` / `__END__` 节点,GraphSpecCompiler 用默认 `start` / `end` node_type 创建框架基类。START 默认 fan-out,END 默认聚合。

#### 自定义 START(路由)

```yaml
name: smart-router
version: "1"
scheduler: parallel
default_trigger: on_all_preds
max_iterations: 25
state_class: GraphState

nodes:
  - name: __START__
    node_type: router_start
    config:
      routes:
        - keywords: ["代码", "bug"]
          target: coder
        - keywords: ["研究", "分析"]
          target: researcher

  - name: coder
    node_type: agent
    config:
      agent: default
      pool: coder

  - name: researcher
    node_type: agent
    config:
      agent: default
      pool: default

edges:
  - { source: __START__, target: coder }
  - { source: __START__, target: researcher }
  - { source: coder, target: __END__ }
  - { source: researcher, target: __END__ }
```

显式定义 `__START__` 为 `router_start` 类型,业务层注册 `RouterStartNodeFactory` 到 NodeRegistry。

### 10. 校验

- **加载时**：YAML → `GraphSpec.model_validate()` → 结构校验（duplicate names / entry edge / edge endpoints）
- **编译时**：`GraphSpecCompiler.compile()` → `TopologyValidator`（环检测 + 可达性 + node 白名单）
- **START/END 校验**：
  - 有且仅有一个 `name == "__START__"` 的 NodeSpec(如果显式定义)
  - 有且仅有一个 `name == "__END__"` 的 NodeSpec(如果显式定义)
  - 显式定义的 START/END 的 `node_type` 必须在 NodeRegistry 中注册
  - 未显式定义时,compiler 自动用默认 `start` / `end` node_type 创建
- **WebUI 保存校验**：前端编辑 YAML → 点击保存 → PUT `/api/graphs/specs/{id}` → 后端 parse + validate → 通过则写回文件,失败返回错误(不保存)。校验在保存时触发,不是单独端点。

### 归属

| 层 | 内容 | 归属 |
|----|------|------|
| GraphSpec YAML 加载/扫描 | bot 启动扫描 `config/graphs/` → GraphSpecStore | **bot_project**（像 PoolStore 扫描 `config/pools/`） |
| GraphConfigController | REST 端点：GraphSpec CRUD + 写回 YAML | **bot_project** |
| GraphSpecStore | SQLite 持久化（运行时副本） | **modex_graph** 框架层（已存在 SqliteGraphSpecStore） |
| 节点类型注册 | NodeRegistry + NodeFactory 注册 | **bot_project**（bot 启动时注册） |

### 不做什么

- 不从 YAML 直接编译图——总是经过 SQLite GraphSpecStore 中转
- 不让 graph_instances 直接引用 YAML 文件——通过 spec_id 引用 SQLite 行
- 不做 UNIQUE(name, version) 约束——允许删除+新建同名 graph
- 不让 version 用于身份判断——它只是保存次数计数器
- 不做代码构建作为用户入口（`build_react_graph()` 仅框架内部）
