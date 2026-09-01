# SPEC: 插件化统一 Agent 装配系统

> **Status**: Authoritative — supersedes individual ticket decisions where conflicts exist (see §12 冲突决议记录).
> **Date**: 2026-08-18 (revised 2026-08-18 — 闭包检查后补齐 F1-F4/C1-C4/G1-G9; 2026-08-19 — 补 §19 Errata 实现期修订)
> **Tickets**: 01-08 (all closed, see `issues/`)
> **Related ADRs**: ADR-0041, ADR-0020, ADR-0025, ADR-0033, ADR-0039

---

## 1. 目标

**用户写 YAML + 注册插件，就能定义行为不同的 agent，跑在 session 模式或 graph 模式——所有 agent 走同一条装配路径。**

完成判据：
- 不改框架代码、只写 YAML + 装插件，就能定义出新 agent（工具/钩子/模型/提示词/拦截器/命令全部可注册，重启生效）
- 插件能安全存取自己的会话数据（类型化 KVStore + 命名空间层）
- 图工作流不回退——同一份点名册能进 DAG 节点，图变量照常持久、可恢复
- main/sub/external/graph 四种 agent 类型走同一条 AssemblyPipeline，不同 stage 子集

---

## 2. 五条统一原则

| # | 原则 | 覆盖范围 | 例外 |
|---|---|---|---|
| 1 | **一个 ComponentRegistry** | 全局单例，12 槽位，所有组件按名解析。来源：bundled > project > user > entry_points | v1 external provider 硬编码 `provider_kind`（EXTERNAL_PROVIDER 槽位延后） |
| 2 | **一套 YAML 配置** | pool.yml + agent template + graph spec，都引用 registry 组件名 | 无 |
| 3 | **一条 AssemblyPipeline** | main/sub/external 全走同一管道，不同 stage 子集 | 特殊 agent（§9）——构造 inline，触发 plugin 化；graph node 复用 pool 预构建 agent（§8.1） |
| 4 | **一套类型登记** | DATA_NAMESPACE 的 Pydantic model 既用于 KVStore 插件数据，也用于图 state schema | 无 |
| 5 | **骨架固定** | workspace/pool 的 lifecycle/routing/paths/session-mapping 不可替换 | 无 |

读写两侧同一条原则（与参考项目一致）：**框架定信封（接缝形状），插件定信件（流过接缝的数据形状）**。

---

## 3. 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                      YAML 配置层                             │
│  pool.yml           templates/*.yml         graphs/*.yml     │
│  (pool 级)           (per-agent 级)         (图级)           │
│  agents+strategy     tools/hooks/memory     拓扑+state_schema│
│  peers               toolPreset+合并         nodes/edges     │
│                      system_prompt                           │
│                      llm_provider                            │
└──────────┬──────────────────┬────────────────────┬──────────┘
           │                   │                    │
           ▼                   ▼                    ▼
    RosterLoader         RosterLoader         RosterLoader
    → PoolRoster         → AgentRoster        → GraphRoster
    (frozen Pydantic)    (frozen Pydantic)    (frozen Pydantic)
           │                   │                    │
           └─────────┬─────────┴────────────────────┘
                     │ 构建 AssemblySpec
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                ComponentRegistry (全局单例)                   │
│  12 槽位 × N 工厂 | 来源: bundled > project > user > entry_points │
│  每个条目 = ComponentFactory + frozen config_model           │
│  DATA_NAMESPACE 工厂的 Pydantic model = 统一类型词汇          │
└──────────┬──────────────────────────────────────────────────┘
           │ 装配时工厂 create(config, ctx) → 实例
           ▼
┌─────────────────────────────────────────────────────────────┐
│                AssemblyPipeline                              │
│                                                              │
│  输入: AssemblySpec (frozen, 携带组件名+config)              │
│  累积: AssemblyBuilder (可变, 持有构建的实例)                 │
│  输出: AssembledAgent                                        │
│                                                              │
│  Stage 1: WorkspaceMaterialize  ← ResourceFactory ABC       │
│  Stage 2: InfraAssemble          ← broker/inbox/bus/graph   │
│  Stage 3: PoolAssemble           ← ExecutionStrategy ABC    │
│  Stage 4: AgentAssemble          ← memory/tools/hooks       │
│  Stage 5: SubagentAssemble       ← 懒加载, 首次 turn 时      │
│                                                              │
│  Stage 子集见 §6.3                                           │
│  AssemblyContext 分层见 §6.5                                 │
└──────────┬──────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────┐
│                框架骨架 (不可替换)                            │
│  WorkspaceRegistry | PoolRouter | AgentPool                  │
│  SessionTreeManager | InboxPoller | AgentMessageBus          │
│  WorkspacePaths | session_workspace_map                      │
│  GraphOrchestrator (在 InfraAssembleStage 构建)              │
│  (插件控制"构建什么", 骨架控制"如何路由/持久化/生命周期")      │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. ComponentRegistry

### 4.1 定位

全局单例。启动时从四源加载所有可用组件工厂：
1. **bundled**：框架自带的默认组件工厂
2. **project**：项目 `plugins/` 目录的 Python 包
3. **user**：用户插件目录的 Python 包
4. **entry_points**：PyPI 安装的 `modex_agent.plugins` entry group

优先级：bundled > project > user > entry_points（同源重名炸响 `ValueError`，跨源 first-seen-wins）。

### 4.2 统一存储形态：ComponentFactory

ComponentRegistry 的每个条目都是 `ComponentFactory`——**统一为工厂形**，不区分实例和工厂：

```python
class ComponentFactory(ABC):
    """统一组件存储形态。create 在装配时被调用，返回组件实例。"""
    
    config_model: ClassVar[type[BaseModel]]  # frozen=True, extra="forbid"
    
    @abstractmethod
    def create(self, config: BaseModel, ctx: AssemblyContext) -> Any:
        """装配时调用，返回组件实例。config 已由 config_model 校验。"""
        ...
```

单例 vs 原型是**工厂的私有实现决策**，两个现成形态：

- `SimpleFactory` —— 返回预构建单例。共享是有意语义（无状态 hook、
  execution-strategy 类、测试注入 provider）。
- `PrototypeFactory` —— 每次 `create()` 经零参 builder 新建实例。
  TOOL 槽位的框架默认工具用它：共享的可变 `Tool` 实例会让
  `register(tool, config)` 的改写跨 agent/pool/workspace 泄漏。

需要 config/上下文差异化构造的组件写专属工厂子类（内部可自行复用
pool 级资源，如 `BashToolFactory` 返回 pool 共享的 persistent shell）：

```python
# 无依赖组件（单例语义）
registry.register_tool("current_time", SimpleFactory(CurrentTimeTool(), CurrentTimeConfig))

# 无依赖组件（原型语义——每次装配新实例）
registry.register_tool("read", PrototypeFactory(ReadFileTool, ToolConfig))

# 工厂形状组件（需 per-pool 参数）
registry.register_hook("todo_continuation", TodoContinuationFactory())
# TodoContinuationFactory.create(config, ctx) 从 ctx.pool_runtime.session_tree_manager 获取 tree
```

ComponentRegistry 存储：`dict[ComponentSlot, dict[str, ComponentFactory]]`。

### 4.3 12 槽位（StrEnum 闭集）

> **Errata-8 (2026-08-20)**: 槽位集收敛为 10 (13→10, 删 `MEMORY_PROVIDER`/`SKILL_SOURCE`/`MEMORY_SYSTEM_MODIFIER`; `MEMORY_SYSTEM` 经 Errata-7 加入后保留) — 本表与 §4.5 的 register_* 计数被取代, 权威清单与迁移指引见 §19 Errata-8 (a)。

| 槽位 | 注册方法 | 现状/动作 | 备注 |
|---|---|---|---|
| `TOOL` | `register_tool` | ✅ 已有 | |
| `HOOK` | `register_hook` | ✅ 已有 | |
| `MEMORY_PROVIDER` | `register_memory_provider` | ✅ 已有 | |
| `SKILL_SOURCE` | `register_skill_source` | ✅ 已有 | |
| `MEMORY_SYSTEM_MODIFIER` | `register_memory_system_modifier` | ✅ 已有 | |
| `LLM_PROVIDER` | `register_provider` | 新增 | LLM 工厂 2 分支降格为默认插件 |
| `SYSTEM_PROMPT_PROVIDER` | `register_prompt_provider` | 新增 | 硬编码 11 步列表开插入缝 |
| `INTERCEPTOR` | `register_interceptor` | 新增 | 注册时校验 scope，拒绝未接线值 |
| `COMMAND_HANDLER` | `register_command` | 新增 | `CommandHandler` ABC 已有 |
| `EXECUTION_STRATEGY` | `register_execution_strategy` | 新增 | `ComponentRegistry` 为唯一注册源；启动后从其中的 `SimpleFactory` 派生 `ExecutionStrategyRegistry` |
| `INPUT_STAGE` | `register_input_stage` | 新增 | `InputStage` ABC 已有 |
| `DATA_NAMESPACE` | `register_namespace` | 新增 | 01 票的类型命名空间层 + `resolve_bundle` 一等访问面 |

延后（不在 v1）：
- `ADAPTER` + `EMITTER` → 业务层，per-instance 工厂构造，需要装配回迁才有框架落点
- `EXTERNAL_PROVIDER` → v1 保留 `provider_kind` 硬编码在 pool.yml（不通过 ComponentRegistry）；v2 落槽位时 `provider_kind` 变为组件名引用
- `GOVERNANCE` / `APPROVAL_CLASSIFIER` → v1 governance 从 MemoryConfig 派生（现有机制）；v2 开槽位时 roster 可配置
- `SANDBOX` → 先以 `TOOL_CALL` interceptor 形态接线再议

### 4.4 旧 plugin 体系处置：全删

当前 plugin 体系（`PluginManager` + `PluginContext` + `PluginLoader` + `PluginIntegration`）是空壳（`enabled: False` 初始化，`inject_*` 零外部调用者）且设计形状与新决议不兼容。全删，从 `Plugin(ABC)` + `ComponentRegistry` 从零实现。

删除清单：
- FW：`src/modex_agent/plugins/`（5 文件）
- BIZ：`bot/plugins/integration.py`
- 引用清理：`resources.py:226-233`、`_runtime_builders.py:28-46`、`core.py:366`、`builders.py:188`

02 票 Q2 的 "collect-then-inject" 被推翻——旧体系的 inject 路径从未实现。新设计：`ComponentRegistry` 是唯一信任边界，`AssemblyPipeline` 的 stage 从 registry 按名解析工厂，调用 `factory.create(config, ctx)` 得到实例，直接注入到正在构建的 manager/agent。

### 4.5 Plugin(ABC) 契约 + ComponentRegistryLoader

```python
class Plugin(ABC):
    """类型化插件入口（零 Protocol，ABC 优先）。"""
    
    config_model: ClassVar[type[BaseModel]]  # frozen=True, extra="forbid"
    api_version: ClassVar[int] = 1            # 契约常量
    
    @abstractmethod
    def register(self, ctx: PluginRegistrationContext) -> None:
        """向 ComponentRegistry 注册组件工厂。ctx 提供 12 个 register_* 方法。"""
        ...
```

`PluginRegistrationContext` 是 collecting facade，作为 context manager 使用——`__exit__` 时将收集的工厂 flush 到 ComponentRegistry：

```python
class PluginRegistrationContext:
    """collecting facade。with 语句结束后 flush 到 ComponentRegistry。"""
    
    def __init__(self, registry: ComponentRegistry) -> None: ...
    
    # 12 个 register_* 方法，每个接收 (name: str, factory: ComponentFactory)
    def register_tool(self, name: str, factory: ComponentFactory) -> None: ...
    def register_hook(self, name: str, factory: ComponentFactory) -> None: ...
    # ... 其余 10 个同理
    
    def __enter__(self) -> PluginRegistrationContext: return self
    def __exit__(self, *exc) -> None:
        """flush 所有收集的工厂到 ComponentRegistry。如果有异常，丢弃本插件的所有工厂（原子性）。"""
        if exc[0] is None:
            self._flush()
        # 异常时不 flush，丢弃本插件的收集
```

`ComponentRegistryLoader` 是启动时的加载器——发现、实例化、调用 Plugin.register()：

```python
class ComponentRegistryLoader:
    """启动时从四源加载插件，fault-isolated per plugin。"""
    
    @classmethod
    def load(cls, registry: ComponentRegistry) -> None:
        """按优先级加载：bundled > project > user > entry_points。单个插件失败不阻塞其他。"""
        for source in ("bundled", "project", "user", "entry_points"):
            plugins = cls._discover(source)
            for plugin_cls in plugins:
                try:
                    plugin = plugin_cls()
                    with PluginRegistrationContext(registry) as ctx:
                        plugin.register(ctx)
                except Exception as e:
                    logger.error(f"Plugin {plugin_cls.__name__} from {source} failed: {e}")
                    # 继续加载其他插件（fault-isolated）
```

调用者：`BotService.initialize()`（或等效启动序列），在 pool 创建之前。

**故障隔离**：单个插件加载失败只影响该插件——其他插件的工厂已 flush 到 registry，不受影响。失败插件的组件不可用，roster 引用它们时在装配时报 `ComponentNotFoundError`。

**原子性**：每个插件获得自己的 `PluginRegistrationContext`。`register()` 异常时，该插件的收集被丢弃（`__exit__` 检测异常不 flush）。不会出现半注册状态。

### 4.6 内置组件降格

| 组件 | v1 降格 | 理由 |
|---|---|---|
| LLM 工厂两分支（openai/litellm） | ✅ 现在 | 无依赖零风险 |
| LoopDetection、CurrentTime、Knowledge、RunLogging | ✅ 现在 | 无依赖，用 SimpleFactory 包装 |
| TodoContinuation、ControlDrain、ExperienceReview、TurnOutcomeNotify、SubagentAutoSend | ⏳ 随 AssemblyPipeline | 工厂形状（需 per-pool tree/control channel/memory+provider/notification/tree+names），管道支持 `factory.create(config, ctx)` 从 ctx 获取 per-pool 参数后一次收敛 |
| `ToolTimeoutInterceptor` | ❌ 永不降 | 正确性不变量（`ToolExecutor` 内层组合，工具超时死线） |

排序契约：`TodoContinuationHook priority=-1000` 必须最先于 AfterTurnHook；trace 钩子 Root→Tool→Handoff 顺序——registry 尊重 priority。

---

## 5. YAML 配置层

### 5.1 两层结构

```yaml
# config/pools/coder/pool.yml — pool 级 roster
main_agent_name: orchestrator       # 隐式引用 templates/orchestrator.yml
execution_strategy: react           # 引用 EXECUTION_STRATEGY 槽位组件名（pool 级）
peers: [default]
subagents:
  - name: explore
    template: explore               # → templates/explore.yml
    execution_strategy: react       # subagent 可独立选择（不强制跟随 main）
    # provider_kind: opencode       # external subagent 可选
  - name: general
    template: general

# pool 级槽位（不在 agent template）
interceptors: [tool_timeout, +my_custom_interceptor]
interceptor_configs:
  my_custom_interceptor:
    threshold: 5000
commands: [/cd, /stop, /pool]
```

```yaml
# templates/explore.yml — per-agent 级 roster
tools: [fs, search, lsp]           # 引用 TOOL 槽位组件名
toolPreset: read_write             # 预置点名组宏，与 tools 取并集
tool_configs:                      # per-tool config（可选）
  search:
    max_results: 50
hooks: [todo]                      # 引用 HOOK 槽位组件名
hook_configs:                      # per-hook config（可选）
  todo:
    priority: -500
memory:
  session: { max_context_tokens: 32000 }  # MemoryOverrides
  providers: [code_context]        # MEMORY_PROVIDER 组件名（可选）
  modifiers: [compaction]          # MEMORY_SYSTEM_MODIFIER 组件名（可选）
system_prompt: agents/explore.md   # 文件路径语法糖（见 §5.6）
llm_provider: default              # 引用 LLM_PROVIDER 槽位组件名
skills: [coding_skills]            # SKILL_SOURCE 组件名（可选）
```

> **Errata-8 (2026-08-20)**: 上例的 `providers`/`modifiers`/`skills` 三键已随槽位移除而死 — 现为 warning（`memory:` 死子键）/ 硬拒绝（`skills:`）, 迁移指引见 §19 Errata-8 (a)。

main agent 的 template 引用：`main_agent_name: orchestrator` 隐式引用 `templates/orchestrator.yml`。需不同名时加可选 `main_template: <name>` 字段。如果 template 文件不存在，用框架默认 template。

### 5.2 Roster 类型与解析时机

`PoolRoster`/`AgentRoster`/`GraphRoster` 都是 frozen Pydantic model。`RosterLoader` 在启动时加载所有 YAML 并解析为 roster 对象。

```python
class RosterLoader:
    """启动时加载 YAML roster 文件。"""
    
    @staticmethod
    def load(config_dir: Path) -> RosterBundle:
        """加载 config_dir 下所有 roster YAML。
        
        - config_dir/pools/*/pool.yml → PoolRoster
        - config_dir/templates/*.yml → AgentRoster (by name)
        - config_dir/graphs/*.yml → GraphRoster
        """
        ...
```

`RosterBundle` 持有所有已解析的 roster 对象。`SpecBuilder.from_roster()`（§6.6）从 `RosterBundle` 取出特定 pool/agent 的 roster 构建 `AssemblySpec`。

roster 解析阶段只做结构校验（字段类型、必填项）。组件名是否存在于 ComponentRegistry 的校验在装配时做（late binding）——这样 roster 可以引用尚未注册的组件（只要装配前注册了即可）。

**GraphRoster vs GraphSpec 的关系**：`RosterLoader` 加载 `graphs/*.yml` → `GraphRoster`（roster 级，携带拓扑 + state_schema 引用）。`GraphSpecLoader`（§8.5）在 `InfraAssembleStage` 内从 `GraphRoster` 构建 `GraphSpec`（引擎级，编译后）。两者是不同层级：`GraphRoster` 是配置层（frozen Pydantic，启动时加载），`GraphSpec` 是引擎层（编译后，per-workspace）。`GraphSpecLoader` 消费 `GraphRoster` + `state_schema_compiler` → 产出 `GraphSpec` + `CompiledGraph`。

### 5.3 toolPreset 合并

`ToolPreset`（当前 4 值枚举）降为"预置点名组"：新增 `toolPreset` 配置字段，与 `tools` 字段取并集。

合并顺序（§5.4 G7 解决）：
1. 展开 `toolPreset` 为组件名列表（预置集，如 `read_write` → `[fs, search, read, write]`）
2. 解析 `tools` 列表：
   - 全部无前缀 → **替换**预置集（不合并 toolPreset）
   - 含 `+`/`-` → 在预置集基础上**增删**
3. 最终 = 合并后的组件名列表

```
# 替换（不合并 toolPreset）
tools: [fs, search, lsp]
→ {fs, search, lsp}

# 增删（基于 toolPreset 预置集）
tools: [fs, +custom, -bash]
toolPreset: read_write
→ {fs, search, read, write} + {custom} - {bash}
→ {fs, search, read, write, custom}
```

### 5.4 增删语法

```yaml
tools: [fs, +my_plugin_tool, -bash]   # + 增加, - 移除, 无前缀 = 基准
hooks: [+todo, -loop_detection]       # 必须配合 toolPreset/preset 使用
```

`+` = 增加到预置集，`-` = 从预置集移除。纯无前缀列表 = 替换（不与 preset 合并）。

### 5.5 memory 字段与 MemoryConfig 的映射

roster 的 `memory` 字段是 `MemoryOverrides`（frozen Pydantic，全字段可选），装配时与框架默认 `MemoryConfig` merge：

- 默认 `MemoryConfig` 来自 `memory_defaults.py`（`main_agent_memory()` / `subagent_memory()`）
- `MemoryOverrides` 覆盖其中的可配置项（如 `max_context_tokens`、`archive_enabled`、`core_enabled`）
- 不可配置项（如 `ArchiveConfig`/`CoreMemoryConfig` 内部结构、governance 派生链）不在 overrides 里

v1 governance 从 MemoryConfig 派生（现有机制），不在 roster 里配置。v2 开 GOVERNANCE 槽位时加入 `governance` 字段。

### 5.6 system_prompt 字段

`system_prompt: agents/explore.md` 是文件路径语法糖。RosterLoader 解析时展开为：

```yaml
system_prompt_provider: file_prompt     # 框架默认组件名
system_prompt_config:
  path: agents/explore.md
```

框架提供默认 `file_prompt` SYSTEM_PROMPT_PROVIDER 组件（`FilePromptFactory`，config 是 `{path: str}`）。用户也可注册自定义 SYSTEM_PROMPT_PROVIDER 组件，用组件名直接引用：

```yaml
system_prompt_provider: my_custom_prompt
system_prompt_config: { theme: dark }
```

### 5.7 llm_provider 字段

`llm_provider: default` 引用名为 "default" 的 LLM_PROVIDER 组件。v1 框架提供默认 "default" 组件，其 config 从 `model.yml` 加载（API key、model name 等）。用户可以在 roster 里覆盖为自定义 provider。

v1 保留 `model.yml` 作为默认 LLM provider 的 config 来源。

---

## 6. AssemblyPipeline

### 6.1 三层分离：Spec → Builder → Agent

管道有三个层，不混在一起：

| 层 | 类型 | 可变性 | 内容 |
|---|---|---|---|
| **输入** | `AssemblySpec` | frozen Pydantic | 组件名引用 + config 值 + workspace context 引用。可序列化、可审计 |
| **累积** | `AssemblyBuilder` | 可变 Python 对象 | 构建中的实例（ToolManager、HookRunner、MemorySystem、governance 等） |
| **输出** | `AssembledAgent` | 不可变 | 最终组装的 agent 实例 |

Stage 签名：

```python
class AssemblyStage(ABC):
    @abstractmethod
    def process(self, spec: AssemblySpec, builder: AssemblyBuilder, ctx: AssemblyContext) -> None:
        """修改 builder（累积构建结果）。spec 只读，ctx 提供 ComponentRegistry + 运行时依赖。"""
        ...
```

Stage 不返回值——直接修改 builder。Runner 流程：

```python
class AssemblyPipeline:
    def run(self, spec: AssemblySpec, ctx: AssemblyContext) -> AssembledAgent:
        builder = AssemblyBuilder()
        try:
            for stage in self._stages_for(spec.agent_type):
                stage.process(spec, builder, ctx)
            return builder.build_agent()
        except Exception:
            builder.cleanup()
            raise
```

`builder.cleanup()` 按逆序销毁已累积的资源（先 agent → pool → infra → workspace_resources），释放 DB 连接/文件句柄/线程，确保装配失败不泄漏资源。

#### AssembledAgent

```python
class AssembledAgent:
    """装配产出。不可变——构建后字段不修改。"""
    
    agent: Any                           # agent 实例（ReActAgent / ExternalAgent）
    pool: AgentPool | None               # pool 实例（main agent 有，subagent 无）
    strategy_result: StrategyAssembly | None  # 策略产出（turn_runner/pipeline 等）
    workspace_resources: WorkspaceResources | None  # per-workspace 基础设施（main agent 首次构建，subagent 共享）
    infra: dict[str, Any] | None         # GraphOrchestrator 等（同 workspace_resources）
    subagent_slot: dict[str, Any] | None # subagent 懒加载槽（main agent 有，subagent 无）
```

消费者：
- `pool.register_resident(agent_name, assembled.agent)` — 注册到 pool
- `TurnRunner` 消费 `strategy_result.turn_runner`
- 后续 subagent 装配通过 `ctx` 访问 `workspace_resources` 和 `infra`（共享）

#### AssemblyBuilder

```python
class AssemblyBuilder:
    """可变累积器。stage 修改 builder，build_agent() 组装最终输出。"""
    
    workspace_resources: WorkspaceResources | None = None
    infra: dict[str, Any] | None = None
    pool: AgentPool | None = None
    strategy_result: StrategyAssembly | None = None
    agent: Any = None
    subagent_slot: dict[str, Any] | None = None
    _built: list[str] = []  # 已构建字段名（逆序 cleanup 用）
    
    def build_agent(self) -> AssembledAgent:
        """从累积的部分组装 AssembledAgent。"""
        return AssembledAgent(
            agent=self.agent,
            pool=self.pool,
            strategy_result=self.strategy_result,
            workspace_resources=self.workspace_resources,
            infra=self.infra,
            subagent_slot=self.subagent_slot,
        )
    
    def cleanup(self) -> None:
        """装配失败时按逆序销毁已构建资源。"""
        for field_name in reversed(self._built):
            obj = getattr(self, field_name, None)
            if obj is not None and hasattr(obj, 'teardown'):
                obj.teardown()
            setattr(self, field_name, None)
```

### 6.2 Stage 列表（5 个）

| Stage | 职责 | 扩展点 ABC | 产出 |
|---|---|---|---|
| `WorkspaceMaterializeStage` | 调用 `workspace_registry.materialize(ctx.workspace_ctx)`（**不直接调 factory**——经 registry 获得缓存/LRU/单飞保护） | `ResourceFactory[R]` | builder.workspace_resources |
| `InfraAssembleStage` | 构建 per-workspace broker/inbox/bus/interceptor/**GraphOrchestrator**。**跳过检查**：如果 `ctx.workspace_resources` 已存在（同一 workspace 的后续 pool 装配），跳过本 stage | — | builder.infra |
| `PoolAssembleStage` | 调用 `ExecutionStrategy.assemble(ctx)` | `ExecutionStrategy` | builder.pool + builder.strategy_result |
| `AgentAssembleStage` | 从 ComponentRegistry 解析 tools/hooks/memory/governance/approval，构建 agent | — | builder.agent |
| `SubagentAssembleStage` | 懒加载 subagent（首次 turn 时调用 `assemble_agent(sub_spec, ctx)`） | — | builder.subagent |

**删除的 stage**（闭包检查修正）：
- ~~`SpecialAgentStage`~~——特殊 agent 不走管道（§9），完全在管道外
- ~~`GraphAssembleStage`~~——图装配是 `InfraAssembleStage` 的职责（workspace 级，不是 agent 级）

### 6.3 Stage 子集与调用时机

AssemblyPipeline 是**可复用函数** `assemble_agent(spec, ctx)`，不同时机调用不同 stage 子集：

| 调用时机 | Agent 类型 | Stage 子集 | 备注 |
|---|---|---|---|
| **Pool 创建时** | Native main | 1→2→3→4 | `create_pool` + authoritative Stage 4 |
| **Pool 创建时** | External main | 1→2→3 | `ExternalExecutionStrategy.assemble`（main 分支） |
| **首次 turn 时** | Native sub | 4→5 | `AgentTemplate.materialize`（native 分支）→ 管道 |
| **首次 turn 时** | External sub | 3 | `ExternalExecutionStrategy.assemble`（sub 分支，Q6 收敛） |
| **不调用** | Graph node | — | 复用 pool 预构建 agent（§8.1） |
| **不调用** | Special agent | — | inline 构造（§9 例外） |

AssemblyContext 在 pool 创建时构建，挂在 pool 上持久持有——首次 turn 时 subagent 装配可重用同一 ctx。

### 6.4 external main/sub 收敛

当前 external main 走 `ExternalExecutionStrategy.assemble`，external sub 走 `BotSubagentExternalBuilder.build`——两条路径。收敛到 `ExternalExecutionStrategy.assemble`，sub 的 `BotSubagentExternalBuilder` 逻辑吸收进 strategy。strategy 根据 `spec.agent_type`（`external_main` vs `external_sub`）分支构建 main 或 sub 的 `ExternalAgent`。

v1 external provider 的 `provider_kind`（pi/opencode）保留硬编码在 pool.yml，不通过 ComponentRegistry。v2 落 EXTERNAL_PROVIDER 槽位时改为组件名引用。

### 6.5 AssemblyContext 分层

```python
class AssemblyContext:
    """装配上下文，分层持有依赖。"""
    
    # 全局层（不随 workspace 驱逐销毁）
    registry: ComponentRegistry               # 全局单例
    workspace_registry: WorkspaceRegistry     # workspace 生命周期管理（缓存/LRU/单飞）
    
    # workspace 层（随 workspace 驱逐销毁，重新 materialize 时重建）
    workspace_ctx: WorkspaceContext            # 身份（target/paths/is_home）
    workspace_resources: WorkspaceResources | None  # per-workspace 基础设施（首个 pool 装配时构建，后续 pool 共享）
    
    # pool 层（per-pool，在 PoolAssembleStage 构建后填充）
    pool_runtime: PoolRuntimeDeps              # tree_manager, control_channel, 
                                               # notification_service, binding_store,
                                               # pool_assembly_ctx 等
```

`workspace_resources` 在 workspace 层——首个 pool（main agent）装配时由 `WorkspaceMaterializeStage` + `InfraAssembleStage` 构建，放入 `ctx.workspace_resources`。后续 pool（subagent）装配时，`WorkspaceMaterializeStage` 和 `InfraAssembleStage` 检测到 `ctx.workspace_resources is not None` 后跳过——subagent 共享同一 workspace 的基础设施。

`PoolRuntimeDeps` 新增 `pool_assembly_ctx: PoolAssemblyContext` 字段——由 `PoolAssembleStage` 从 spec + workspace_resources + infra 构建，传给 `ExecutionStrategy.assemble(pool_assembly_ctx)`。这让现有策略签名（`async assemble(self, ctx: PoolAssemblyContext)`）不需要改动——新装配系统通过 `AssemblyContext` 层层传递，在 `PoolAssembleStage` 内适配为现有 `PoolAssemblyContext`。

**workspace 驱逐/重新 materialize 的处理**：
- 全局层（ComponentRegistry）不销毁——工厂全局共享，无状态
- workspace 层和 pool 层随驱逐销毁——重新 materialize 时 AssemblyPipeline 重新跑，从全局 ComponentRegistry 重新调用 `factory.create(config, ctx)` 得到新实例
- 工厂 `create()` 的输出是 per-pool 实例（如 TodoContinuationHook 绑定特定 pool 的 tree），但工厂本身无状态、全局共享

`PoolRuntimeDeps` 携带的 per-pool 运行时对象（tree_manager、control_channel 等）是 Python 对象引用，不序列化——它们在 `PoolAssembleStage` 内构建后放入 ctx，供后续 stage 和工厂使用。

### 6.6 AssemblySpec 内容

> **Errata-8 (2026-08-20)**: 本节的字段清单/槽位层级表/默认组件包含 3 个已删槽位（`MEMORY_PROVIDER`/`SKILL_SOURCE`/`MEMORY_SYSTEM_MODIFIER`）的行与字段组 — 权威 10 槽清单见 §19 Errata-8 (a); LLM 解析单一机制见 (e)。

AssemblySpec 是 frozen Pydantic，按关注点设计，携带组件名引用（字符串）+ config 值：

```python
class AssemblySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    
    agent_type: AgentType                      # native_main / native_sub / external_main / external_sub
    agent_name: str
    pool_name: str
    
    # ── per-agent 槽位（组件名 + per-component config）──
    tools: list[str]                           # TOOL 槽位组件名
    tool_configs: dict[str, dict[str, Any]] = {}   # per-tool config
    hooks: list[str]                           # HOOK 槽位组件名
    hook_configs: dict[str, dict[str, Any]] = {}   # per-hook config
    llm_provider: str                          # LLM_PROVIDER 组件名
    llm_provider_config: dict[str, Any] = {}   # LLM provider config
    system_prompt_provider: str                # SYSTEM_PROMPT_PROVIDER 组件名
    system_prompt_config: dict[str, Any]       # provider config
    memory_overrides: MemoryOverrides          # 与默认 MemoryConfig merge
    memory_providers: list[str] = []           # MEMORY_PROVIDER 组件名（自定义 memory provider）
    memory_provider_configs: dict[str, dict[str, Any]] = {}
    skill_sources: list[str] = []              # SKILL_SOURCE 组件名
    skill_source_configs: dict[str, dict[str, Any]] = {}
    memory_system_modifiers: list[str] = []    # MEMORY_SYSTEM_MODIFIER 组件名
    memory_system_modifier_configs: dict[str, dict[str, Any]] = {}
    
    # ── pool 级槽位（从 PoolRoster 填充，不在 agent template）──
    execution_strategy: str                    # EXECUTION_STRATEGY 组件名
    provider_kind: str | None = None           # v1 external 硬编码（v2 改组件名）
    
    # ── context 引用（Python 对象，不序列化——arbitrary_types_allowed）──
    workspace_ctx: WorkspaceContext
```

#### 槽位配置层级

| 槽位 | 配置层级 | AssemblySpec 字段 | roster YAML 位置 | 代码依据 |
|---|---|---|---|---|
| TOOL | per-agent | `tools` + `tool_configs` | agent template | `template.py:227` per-agent tool_manager |
| HOOK | per-agent | `hooks` + `hook_configs` | agent template | `template.py:239-286` per-agent hooks |
| LLM_PROVIDER | per-agent | `llm_provider` + `llm_provider_config` | agent template | `template.py:291-297` per-agent llm_config |
| SYSTEM_PROMPT_PROVIDER | per-agent | `system_prompt_provider` + `system_prompt_config` | agent template | `template.py:128-136` per-agent system_prompt |
| MEMORY_PROVIDER | per-agent | `memory_providers` + `memory_provider_configs` | agent template `memory.providers` | `template.py:195-207` per-agent memory |
| SKILL_SOURCE | per-agent | `skill_sources` + `skill_source_configs` | agent template `skills` | `template.py:228` per-agent skill_manager |
| MEMORY_SYSTEM_MODIFIER | per-agent | `memory_system_modifiers` + configs | agent template `memory.modifiers` | `memory_defaults.py` per-agent memory config |
| INTERCEPTOR | pool 级 | `interceptors` + `interceptor_configs` | pool.yml `interceptors` | `pipeline_wiring.py` 克隆共享链后按 pool 追加 |
| COMMAND_HANDLER | pool 级 | `commands` | pool.yml `commands` | `pipeline_wiring.py` 构造 per-pool processor |
| INPUT_STAGE | workspace 级 | InfraAssembleStage 从 workspace 配置解析 | workspace 配置 | `input_pipeline/` 跨 pool 共享 |
| EXECUTION_STRATEGY | pool 级 | `execution_strategy` | pool.yml | `specs.py:78` MainAgentSpec |
| DATA_NAMESPACE | 全局 | 不在 AssemblySpec | 不在 roster | ComponentRegistry 全局注册 |

**per-agent 槽位**（7 个）在 AssemblySpec 中携带，由 AgentAssembleStage 从 ComponentRegistry 解析。

**pool 级槽位**（3 个：INTERCEPTOR/COMMAND_HANDLER/EXECUTION_STRATEGY）由 SpecBuilder 从 PoolRoster 投影到 AssemblySpec。EXECUTION_STRATEGY 在 PoolAssembleStage 解析；INTERCEPTOR/COMMAND_HANDLER 在 main agent 注册后由 pipeline wiring 解析，以便复用既有共享链和命令处理器。未配置 interceptors 时保持共享链引用；配置后复制共享链再追加，避免污染其他 pool。未配置 commands 时保留既有默认 processor；配置后以 roster 指定的 handler 集构造 per-pool processor。

**workspace 级槽位**（1 个：INPUT_STAGE）由 InfraAssembleStage 从 workspace 配置解析。

**全局槽位**（1 个：DATA_NAMESPACE）在 ComponentRegistry 启动时注册，不在任何 roster 中。

#### 默认组件包

每个槽位在不写 roster 时的默认组件：

| 槽位 | 默认组件 | 默认 config 来源 | 理由 |
|---|---|---|---|
| TOOL | `fs, search, read, write, bash, lsp`（ToolPreset.FULL 展开） | 各组件 config_model 默认值 | 当前 ToolPreset.FULL 行为 |
| HOOK | `inbox_flush, todo_continuation, deliver_retry` | 各组件 config_model 默认值 | 当前 `register_tree_aware_hooks` 行为 |
| LLM_PROVIDER | `default` | `model.yml` | §5.7 |
| SYSTEM_PROMPT_PROVIDER | `file_prompt` | `{path: agents/<agent_name>.md}` | §5.6 |
| MEMORY_PROVIDER | （空——使用框架默认 MemorySystem） | — | 当前行为：不额外注册 memory provider |
| SKILL_SOURCE | （空——无 skill） | — | 当前行为 |
| MEMORY_SYSTEM_MODIFIER | （空——使用 MemoryConfig 默认） | — | 当前行为 |
| INTERCEPTOR | `tool_timeout` | — | `ToolTimeoutInterceptor` 永不降格（§4.6） |
| COMMAND_HANDLER | `/cd, /stop, /pool` | — | 当前命令处理器 |
| INPUT_STAGE | `im_input, webui_input` | — | 当前 input pipeline stages |
| EXECUTION_STRATEGY | `react` | — | `ExecutionStrategyKind.REACT` 默认值 |
| DATA_NAMESPACE | （空——无自定义类型） | — | 插件按需注册 |

用户写空 agent template（不指定任何字段）→ 得到上述全部默认值。当前 bot_project 的默认行为与这个清单一致——迁移后用户无感知。

#### subagent 的 execution_strategy

`SubagentSpec`（specs.py:102-132）有自己的 `execution_strategy: ExecutionStrategyKind = REACT` + `provider_kind: ProviderKind | None = None`——subagent **可以独立选择**，不跟随 main agent。SpecBuilder 从 SubagentSpec 读取这两个字段，和 main agent 一样派生 agent_type。

当前代码中 subagent 通常跟随 main agent（react main → react sub），但这是 roster 约定，不是框架强制。用户可以配 react main + external sub（如果 external sub 的环境已配好）。

subagent 与 main agent 的配置差异：

| 维度 | main agent | subagent | 差异来源 |
|---|---|---|---|
| memory 默认 | `main_agent_memory()`（session+pruned+governance，archive/core 可开） | `subagent_memory()`（session+pruned+governance only） | `memory_defaults.py` 不同函数 |
| experience | 有（`ExperienceReviewHook`，声明 `tool_supplements: [experience]`） | 无（机制上可选，未声明） | 声明驱动——main-only 位置默认已删除（绑定跟随编译后的最终工具名册） |
| approval | 有（`ApprovalConfig`） | 无 | `MainAgentSpec` 有 approval 字段，`SubagentSpec` 无 |
| max_steps | 100 | 80 | `specs.py:71,112` 不同默认值 |
| tool_preset 默认 | `FULL` | `READ_WRITE` | `specs.py:74,113` 不同默认值 |
| comm_kind | `NORMAL` | `SUBAGENT` | `template.py:124` |
| 装配时机 | pool 创建时 | 首次 turn 时 | §6.3 |
| stage 子集 | 1→2→3→4 | 4→5（native）/ 3（external） | §6.3 |

这些差异全部由 SpecBuilder 从 PoolRoster（main vs subagent 条目）+ AgentRoster（per-agent template）读取后填入 AssemblySpec。AssemblySpec 的字段结构对 main/sub 完全相同——差异在值，不在结构。

#### AgentType YAML 派生规则

`agent_type` 由 RosterLoader 从 YAML 结构派生：

| YAML 位置 | 条件 | agent_type |
|---|---|---|
| `pool.yml` 的 `main_agent_name` | `provider_kind` 未设置 | `native_main` |
| `pool.yml` 的 `main_agent_name` | `provider_kind` 已设置（pi/opencode） | `external_main` |
| `pool.yml` 的 `subagents[].name` | `provider_kind` 未设置 | `native_sub` |
| `pool.yml` 的 `subagents[].name` | `provider_kind` 已设置 | `external_sub` |

`execution_strategy` 字段引用 EXECUTION_STRATEGY 槽位组件名（如 `react`/`external`）。`provider_kind` 是 pool.yml 的可选字段，v1 硬编码 pi/opencode（§6.4）。

#### AssemblySpec 构建

`SpecBuilder` 从 roster 对象构建 AssemblySpec：

```python
class SpecBuilder:
    @staticmethod
    def from_roster(
        pool_roster: PoolRoster,
        agent_roster: AgentRoster,
        agent_name: str,
        workspace_ctx: WorkspaceContext,
    ) -> AssemblySpec:
        """从 roster 构建 AssemblySpec。展开 toolPreset，合并增删语法，展开 system_prompt 语法糖。"""
        ...
```

`SpecBuilder.from_roster()` 执行：
1. 从 `pool_roster` 确定 `execution_strategy` + `provider_kind` → 派生 `agent_type`
2. 从 `agent_roster` 展开 `toolPreset`（§5.3 G7 合并顺序）→ 合并 `tools` 增删 → 最终 `tools: list[str]`
3. 展开 `system_prompt` 语法糖为 `system_prompt_provider` + `system_prompt_config`（§5.6）
4. 提取 `tool_configs`/`hook_configs`/`llm_provider_config`（如果 roster 携带）
5. 构建 frozen `AssemblySpec`

调用者：pool 创建时（main agent）和首次 turn 时（subagent），由 `create_pool` / `AgentTemplate.materialize` 的替代代码调用。

### 6.7 配置策略

#### 默认配置 + 覆盖机制

每个 agent 在不写 roster 时获得默认配置。main agent 和 subagent 有不同默认值。roster 增删语法在默认基础上覆盖。

**main agent 默认配置**（native_main）：

| 配置项 | 默认值 | 来源 |
|---|---|---|
| tools | ToolPreset.FULL 展开（fs/search/read/write/bash/lsp） | `specs.py:74` MainAgentSpec.tool_preset=FULL |
| hooks | inbox_flush + todo_continuation + deliver_retry + native_env + experience_review + cleanup_metrics + todo_reorientation | `register_tree_aware_hooks` + `pipeline_wiring.py` + `template.py` |
| memory | session(0.85/0.3) + pruned(enabled,50,200) + governance(tool_chain_repair+budget) | `main_agent_memory()` |
| experience | ExperienceConfig(enabled=最终工具名册含 experience 名) + 编译器注入 ExperienceReviewHook | 声明 `tool_supplements: [experience]`（等价 `tools: [+experience]`；原 main-agent experience preset 已删除） |
| approval | None（可选开启） | `specs.py:76` MainAgentSpec.approval=None |
| max_steps | 100 | `specs.py:71` |
| llm_provider | "default"（从 model.yml） | §5.7 |
| system_prompt | `agents/<agent_name>.md`（file_prompt） | §5.6 |

**subagent 默认配置**（native_sub）：

| 配置项 | 默认值 | 与 main 差异 |
|---|---|---|
| tools | ToolPreset.READ_WRITE 展开（fs/search/read/write） | 更受限（无 bash/lsp） |
| hooks | inbox_flush + todo_continuation + deliver_retry + native_env + **subagent_auto_send** + cleanup_metrics + todo_reorientation | 多 SubagentAutoSendHook，无 ExperienceReviewHook |
| memory | session(0.85/0.3) + pruned(enabled,50,200) + governance(tool_chain_repair only) | 无 archive/core/dream，governance 无 budget |
| experience | 无 | main-only |
| approval | 无 | SubagentSpec 无 approval 字段 |
| max_steps | 80 | 更少 |
| comm_kind | SUBAGENT | main 是 NORMAL |

**external agent 默认配置**（external_main/external_sub）：
- 跳过 memory/tools/hooks/system_prompt（external CLI 管理这些）
- 只有 broker I/O + emitter + ExternalTurnRunner + pipeline（无 hooks/provider/tools）

#### Hook 的 agent_type 自动过滤

hook 工厂声明 `applies_to: ClassVar[set[AgentType] | None]`。AgentAssembleStage 根据 `spec.agent_type` 自动过滤——用户不需要在 roster 里显式管理 agent_type 专属 hook。

```python
class HookFactory(ComponentFactory):
    applies_to: ClassVar[set[AgentType] | None] = None  # None = all types

class SubagentAutoSendHookFactory(HookFactory):
    applies_to = {AgentType.native_sub, AgentType.external_sub}

class ExperienceReviewHookFactory(HookFactory):
    applies_to = {AgentType.native_main}

class CleanupMetricsHookFactory(HookFactory):
    applies_to = {AgentType.native_sub, AgentType.external_sub}
```

roster 里不写 subagent_auto_send——subagent 自动获得它。用户禁用时用 `hooks: [-subagent_auto_send]`。

Hook 归属表（源码级确认）：

| Hook | applies_to | 注册位置 |
|---|---|---|
| InboxFlushHook | 所有 native | AgentFactory auto-inject |
| TodoContinuationHook | 所有 native | `register_tree_aware_hooks` |
| DeliverRetryHook | 所有 native（subagent no-op） | `register_tree_aware_hooks` |
| NativeEnvInjectionHook | 所有 native | main: pipeline_wiring / sub: template.py:265 |
| SubagentAutoSendHook | native_sub + external_sub | template.py:240 |
| CleanupMetricsHook | native_sub | template.py:223 |
| TodoReorientationHook | native_sub | template.py:220 |
| ExperienceReviewHook | native_main | pipeline_wiring |
| RunLoggingHook | 所有 native | 多处 |

#### memory 配置 vs 数据 scope

**配置 per-agent，数据 per-session/per-user/per-global**——这两个维度正交：

| 维度 | 层级 | 机制 | 说明 |
|---|---|---|---|
| memory **配置** | per-agent | AssemblySpec.memory_overrides → MemoryConfig merge | "用什么 memory 层 + 参数" |
| memory **数据** | per-session | SessionScope（session_id 隔离） | 消息历史、todo、trace |
| memory **数据** | per-user | UserScope（user_id 隔离） | 用户偏好、长期记忆 |
| memory **数据** | per-global | GlobalScope | 全局共享数据 |

同一 pool 的两个 session 共享 memory **配置**（都用 session+pruned+governance），但 memory **数据**完全隔离（SessionScope 按 session_id 隔离）。pool 配置 ≠ pool 数据隔离——pool 内的 session 数据按 SessionScope 隔离。

#### experience 工具 vs ExperienceReviewHook

- **ExperienceReviewHook**：默认给 native main agent，触发后启动 ExperienceReviewAgent
- **Experience 工具**（Read/Write/Edit/List/Rename/Delete）：ExperienceReviewAgent inline 专有，不通过 ComponentRegistry
- **main agent 的 experience 读取**：通过 memory injection（experience 内容注入 system prompt），不是工具

#### 特殊 agent 触发默认配置

特殊 agent 触发 plugin 的默认配置（roster 不写时默认启用）：

| 特殊 agent | 默认 | 触发条件默认值 |
|---|---|---|
| experience_review | enabled（native main agent） | min_messages=20, cooldown_turns=10 |
| session_compactor | enabled（native main agent） | max_output_tokens 自动从 model.yml 上下文窗口推导 |
| core_memory_consolidator | disabled（需 archive_enabled=True） | 定时器/手动 |
| archive_summarizer | disabled（需 archive_enabled=True） | 归档计数阈值 |

用户在 roster 里显式禁用：`experience_review: { enabled: false }`。

### 6.8 失败处理

| 失败场景 | 行为 | 实现 |
|---|---|---|
| **装配 stage 异常** | `builder.cleanup()` 逆序销毁已构建资源（DB 连接/文件句柄/线程），异常重抛给调用者 | `AssemblyPipeline.run()` try/except（§6.1） |
| **workspace 物化异常** | `ResourceFactory.materialize()` 是 atomic-with-cleanup：子资源构建失败时，已构建的子资源 teardown 后重抛。`WorkspaceRegistry.materialize()` 捕获异常、清理、重抛 | `WorkspaceRegistry` 现有行为 + `ResourceFactory` 实现 |
| **插件加载失败** | fault-isolated per plugin：一个插件失败不阻塞其他插件。失败插件的组件不可用 | `ComponentRegistryLoader.load()`（§4.5） |
| **插件 register() 异常** | 该插件的 `PluginRegistrationContext` 收集被丢弃（`__exit__` 检测异常不 flush）。其他插件不受影响 | `PluginRegistrationContext.__exit__`（§4.5） |
| **组件名未找到（装配时）** | fatal：stage 抛 `ComponentNotFoundError(name, slot, roster_location)`。装配中止。不做默认替换——静默错误比大声失败更糟 | `AgentAssembleStage` 解析工厂时 |
| **roster 结构校验失败** | fatal：`RosterLoader` 抛异常，指明哪个文件哪个字段。进程退出（fail-fast） | `RosterLoader.load()`（§5.2） |
| **特殊 agent 构造失败** | trigger 状态重置为 `enabled`（trigger 消耗回退），错误记录。下次触发条件满足时重试 | 特殊 agent 触发 hook 实现 |
| **特殊 agent 运行中崩溃** | 特殊 agent 是 transient（O12），进度丢失。trigger 状态在 roster 中持久，重启后重新安装。下次触发条件满足时重新运行 | §9 + §11 假设 1（重启生效） |
| **workspace 驱逐时 in-flight turn** | `WorkspaceRegistry` 的 in-flight turn protection 防止驱逐正在使用的 workspace | `WorkspaceRegistry` 现有行为（§7） |
| **进程重启** | 所有 in-memory 状态丢失。roster 从 YAML 重新加载，插件重新注册，workspace 懒重新物化。`session_workspace_map`（SQLite）持久——映射标记为 stale，首次访问时懒重新物化 | §11 假设 1（重启生效） |

---

## 7. 框架骨架（不可替换）

以下组件是框架级硬耦合，插件**不能替换**：

| 骨架组件 | 为什么不可替换 |
|---|---|
| `WorkspaceRegistry[R]` | 生命周期管理（materialize/evict/LRU/in-flight turn protection） |
| `PoolRouter` | session→pool 分发 + agent_name 调解 |
| `WorkspacePaths` | 14 个路径常量 + 包含检查 |
| `session_workspace_map` | FK 约束 SQLite 表 |
| `AgentPool` | extends AgentRegistry，拥有 poller/tree |
| `SessionTreeManager` | session 树所有权 |
| `InboxPoller` / `AgentMessageBus` | 单飞 + 轮询驱动收敛 |
| `ExecutionStrategyRegistry` | 策略分发（process-scoped, write-once） |
| `PoolSpec` / `MainAgentSpec` / `SubagentSpec` | frozen Pydantic 配置 schema |
| `GraphOrchestrator` | 图编排（在 InfraAssembleStage 构建） |

插件通过 ABC 扩展点控制"构建什么"：

| 扩展点 ABC | 文件 | 作用 |
|---|---|---|
| `ResourceFactory[R]` | `workspace/factory.py:12` | workspace 资源构建 |
| `ExecutionStrategy` | `multi_agent/execution_strategy.py:94` | pool 形状 |
| `WorkspaceManager` | `workspace/resources.py:55` | workspace 解析 |
| `WorkspaceControlPort` | `workspace/port.py:17` | cd/exit/pwd |
| `InputContext` | `input_pipeline/context.py:8` | input pipeline 上下文 |

---

## 8. 图接缝

### 8.1 agent-as-node 同源

`BotAgentNodeFactory`（BIZ `agent_node_factory.py:38`）**已经**从 pool 取预构建 agent（`_resolve_pool()` → `workspace.pools.get(pool_name)` → `pool.pool.get(agent_name)`）。不构造 agent，不绕过 pool。

AssemblyPipeline 是 pool 级装配（在 `create_pool` 内调用）。图节点复用 pool 的预构建 agent——"同源"已满足。不引入第二条装配路径。

`BotAgentNode`/`BotAgentNodeFactory` 留 BIZ（绑定业务概念 `WorkspaceResolverCell`/`KnowledgeNodeConfig`）。框架提供 `AgentNode` ABC（`src/modex_agent/agents/agent_node.py:40`）。

当前无真实使用不阻碍设计——graph mode 的 agent-as-node 能力保留，未来启用时同源已满足。

### 8.2 GraphSpec state schema 声明

当前 `GraphSpec.state_class: str`（`spec.py:103`）只能引用预注册类。新增声明式 state schema：

- modex_graph 的 `GraphSpec` 新增 `state_schema: dict[str, FieldSpec] | None`（可选，与 `state_class` 互斥）
- `FieldSpec` 是 modex_graph 的 frozen Pydantic model（`name`/`type`/`item_type`/`initial`）——只描述字段形状
- `GraphSpecCompiler` 新增可选注入点 `state_schema_compiler: Callable[[dict[str, FieldSpec]], type[GraphState]] | None`
- modex_agent 侧注入 compiler，从 ComponentRegistry 的 DATA_NAMESPACE 槽位解析自定义类型

YAML 形态：
```yaml
name: research-workflow
state_schema:
  research_notes:
    type: string
    initial: ""
  tool_results:
    type: list
    item_type: string
    initial: []
  custom_data:
    type: my_plugin_data_type    # 从 DATA_NAMESPACE 解析
    initial: null
nodes: [...]
edges: [...]
```

关键约束：modex_graph 是独立包（架构守卫强制不 import modex_agent），编译逻辑不能放在 modex_graph 里。这是"框架定信封（FieldSpec 形状），业务定信件（编译成什么 model）"的模式。

### 8.3 类型登记共用（原则 4）

GraphSpec state schema 的自定义类型与 KVStore 插件数据共用同一套类型登记（ComponentRegistry 的 DATA_NAMESPACE 槽位）。一个插件可以声明一个 DATA_NAMESPACE 类型，既用于自己的持久数据（KVStore），也用于图 state schema。

### 8.4 变量投影到 WebUI

不进本图。WebUI 是业务层，变量投影是 UI 关注点。WebUI 已有 graph REST API（`bot/webui/routes/graph_routes.py`）可扩展。

### 8.5 图装配代码搬家

图装配（GraphOrchestrator + NodeRegistry + GraphSpecLoader）在 `InfraAssembleStage` 内完成（workspace 级基础设施）。代码归属：

| 代码 | 归属 | 理由 |
|---|---|---|
| `GraphSpecLoader` YAML 解析 + GraphSpec 构造 | FW | 通用 |
| `state_schema_compiler` 注入 + DATA_NAMESPACE 类型解析 | FW | 框架级类型登记消费 |
| NodeRegistry 注册（start/end/function/delay/human_input） | FW | 通用 node types |
| GraphOrchestrator 装配 | FW | 通用图编排（在 InfraAssembleStage 构建） |
| `BotAgentNodeFactory` 注册到 NodeRegistry | BIZ | 绑定业务 |
| `BotAgentNode` | BIZ | 绑定业务 |
| `WebUIGraphOutputAdapter` | BIZ | WebUI 是业务 |

---

## 9. 特殊 agent 例外

特殊 agent（experience reviewer / session compactor / core memory consolidator / archive summarizer）是**唯一例外**：

- **不走 AssemblyPipeline**——它们的 tool 是特殊配置的（只读不可写），inline 构造
- **统一的部分**：它们的**触发**（是否启用 + 触发条件）通过 plugin 配置控制，在 roster 里显式启用/禁用
- **理由**：tool 不可组件化（inline `ReadTool`/`WriteTool` 直接 new），强行收敛代价过大
- **影响**：这是 4 个内置 agent，数量固定，不扩展——例外是封闭的

触发机制 plugin 化示例：
- `experience_review` plugin 提供 `ExperienceReviewHook` + 配置（`enabled`/`min_messages`/`cooldown_turns`）
- `session_compactor` plugin 提供 compactor + 配置（`max_output_tokens`/`max_iterations`）
- `core_memory_consolidator` plugin 提供 consolidator + 配置
- `archive_summarizer` plugin 提供 summarizer + 配置

用户在 roster 里启用/禁用这些 plugin，不配置 agent template。当前"模型未配置就静默跳过"（`wiring.py:534` 的 `pipeline is None` guard）由触发 plugin 化消除。

特殊 agent 触发状态机：`disabled → enabled → triggered → running → done → enabled（cooldown 后重触发）`。`done` 不是终态——`cooldown_turns`（experience_review）/ `max_output_tokens` 阈值（compactor）/定时器（consolidator）再次满足时，从 `done` 回到 `enabled`，等待下一次触发。

这些 plugin 注册的是 `TriggerConfigFactory`——一个 `ComponentFactory` 子类，其 `create(config, ctx)` 返回 `TriggerConfig`（frozen Pydantic）。由 `InfraAssembleStage` 从 DATA_NAMESPACE 槽位解析工厂、调用 `create()` 得到 `TriggerConfig`、读取后安装触发 hook。这保持了 ComponentRegistry 的统一存储形态（所有条目都是 ComponentFactory），同时让 TriggerConfig 通过工厂接口流转。

---

## 10. 数据层（01 票决议）

### 10.1 KVStore + 类型命名空间层

插件持久数据的家 = 现有 KVStore（`MemoryStoreBundle` 成员，经 `MemoryStoreRegistry.resolve` 按 `RecordScope` 解析，FILE/SQLite 双后端现成）。

框架唯一新增：类型命名空间层——`命名空间 → Pydantic model` 注册表，写入校验，类型化读取。

```python
class TypedBundle(Generic[T]):
    """类型化 KVStore 访问器，绑定到特定命名空间的 Pydantic model。"""
    
    def get(self, key: str, scope: RecordScope) -> T | None: ...
    def set(self, key: str, value: T, scope: RecordScope) -> None: ...
    def list_keys(self, scope: RecordScope) -> list[str]: ...
    def delete(self, key: str, scope: RecordScope) -> None: ...

def resolve_bundle(namespace: str) -> TypedBundle:
    """从 ComponentRegistry 的 DATA_NAMESPACE 槽位解析命名空间，返回类型化 KVStore 访问器。
    
    插件在 register() 时注册命名空间 + Pydantic model：
        ctx.register_namespace("my_data", SimpleFactory(MyDataModel, MyDataConfig))
    
    运行时通过 resolve_bundle 获取访问器：
        bundle = resolve_bundle("my_data")
        bundle.set("session_key", MyDataModel(...), scope=SessionScope(...))
        data = bundle.get("session_key", scope=SessionScope(...))
    """
    ...
```

`resolve_bundle` 挂在 `ComponentRegistry` 上（`registry.resolve_bundle(namespace)`）。插件在 `register()` 时通过 `PluginRegistrationContext.register_namespace` 注册命名空间工厂；运行时通过 `registry.resolve_bundle(namespace)` 获取 `TypedBundle` 访问器。

### 10.2 作用域

沿用现有 RecordScope（session / user / global）；turn 级临时数据留在 `runtime.state` 不动；`TurnCustomKey` 保持框架内部闭集。

### 10.3 v1 范围

v1 只做变量面；transcript 是呈现层（已验证），事件面开放留雾区。

### 10.4 验证状态

01 决议不需要单独 prototype（08 票已关闭）——KVStore 是已有基础设施（生产中运行），类型命名空间层是薄新增（类型注册表 + 校验函数），实现时用 TDD 验证即可。

---

## 11. 假设与翻转条件

### 假设

1. **重启生效**（不做 HMR/热插拔/沙箱自挂载）——降低所有设计的复杂度
2. **组件工厂无状态**（`create()` 的输出是 per-pool 实例，但工厂本身不持有运行时状态）——全局 registry 共享安全
3. **特殊 agent 数量固定**（4 个）——例外封闭
4. **modex_graph 保持独立包**——编译逻辑在 modex_agent 侧注入

### 翻转条件

- 如果未来需要 per-workspace 私有插件 → 原则 1 需要分层 registry
- 如果未来需要运行时热插拔 → 整个设计需要重做
- 如果特殊 agent 需要用户自定义 → 例外不再封闭，需要重新评估收敛
- 如果 v1 `provider_kind` 硬编码暴露问题 → 提前落 EXTERNAL_PROVIDER 槽位

---

## 12. 冲突决议记录

| 冲突 | 涉及票 | 决议 | 理由 |
|---|---|---|---|
| 02 Q2 "保持 collect-then-inject" vs Q4 "全删旧 plugin 体系" | 02, 03 | **Q4 推翻 02 Q2**。新设计：ComponentRegistry + AssemblyPipeline 直接按名解析工厂 | 旧体系的 inject 路径从未实现，collect-then-inject 是空壳 |
| 02 Q3 "工厂形状 builtins 等 04" vs Q9 "AssemblyPipeline" | 02, 03, 04 | **一致**。管道支持 `factory.create(config, ctx)` | 工厂从 ctx 获取 per-pool 参数 |
| 05 票面 "BotAgentNodeFactory 直接消费统一装配器的产出" vs Q13 | 05, 03 | **Q13 确认已满足** | 图节点复用 pool 预构建 agent，不引入第二条路径 |

### 闭包检查修正（2026-08-18）

| Gap | 修正 |
|---|---|
| F1 AssembledAgent 从哪来 | 三层分离：Spec(frozen 输入) → Builder(可变累积) → Agent(输出)。Stage `process(spec, builder, ctx) -> None` |
| F2 SpecialAgentStage 矛盾 | 从 stage 列表删除。特殊 agent 完全在管道外 |
| F3 GraphAssembleStage 位置 | 从 agent 装配 stage 列表删除。图装配是 InfraAssembleStage 的职责 |
| F4 实例 vs 工厂 | 统一为 ComponentFactory。无依赖组件用 SimpleFactory 包装 |
| C1 memory.governance | v1 从 YAML 示例删除。governance 从 MemoryConfig 派生 |
| C2 SubagentAssemble 时机 | 管道是可复用函数，pool 创建时构建 main，首次 turn 时构建 sub |
| C3 EXTERNAL_PROVIDER | v1 保留 provider_kind 硬编码，v2 落槽位 |
| C4 08 票状态 | 已关闭，§10.4/§15 修正 |
| G1 main template 引用 | 隐式 `templates/<main_agent_name>.yml`，可选 `main_template` 字段 |
| G2 memory 字段映射 | MemoryOverrides（全字段可选）与默认 MemoryConfig merge |
| G3 system_prompt | 文件路径语法糖，展开为 `file_prompt` 组件 + path config |
| G4 spec 携带名还是实例 | 携带组件名（字符串），实例由 stage 运行时解析 |
| G5 Roster 类型 | frozen Pydantic，RosterLoader 启动时解析 |
| G6 per-pool 工厂参数 | 工厂从 AssemblyContext.pool_runtime 获取 |
| G7 增删合并顺序 | 展开 toolPreset → 解析增删 → 无前缀=替换 / 有+/-=增删 |
| G8 llm_provider config | v1 保留 model.yml，默认 "default" 组件从 model.yml 加载 |
| G9 workspace 驱逐 | AssemblyContext 分全局层+workspace 层，工厂全局共享、实例 per-pool |

### 第二轮闭包检查修正（2026-08-18，design-closure skill）

5 维度并行 trace（data-flow/lifecycle/convergence/interface/state-machine）发现 13 个 gap，全部修复：

| Gap | 严重度 | 修正 |
|---|---|---|
| GAP-1 插件加载机制未定义 | CRITICAL | 新增 `ComponentRegistryLoader`（§4.5）——发现/实例化/调用 Plugin.register()，fault-isolated per plugin。`PluginRegistrationContext` 改为 context manager，`__exit__` flush |
| GAP-2 AssemblySpec 构建+config 传输 | HIGH | 新增 `SpecBuilder.from_roster()`（§6.6）+ `tool_configs`/`hook_configs`/`llm_provider_config` 字段 + `pool_assembly_ctx` 适配现有策略签名（§6.5） |
| GAP-3 Builder 装配失败无清理 | MEDIUM | `AssemblyPipeline.run()` 添加 try/except + `builder.cleanup()` 逆序销毁（§6.1） |
| GAP-4 WorkspaceResources 持有者 | HIGH | `workspace_resources` 移到 AssemblyContext workspace 层（§6.5）。InfraAssembleStage 检测已存在则跳过——首个 pool 构建，后续 pool 共享 |
| GAP-5 DATA_NAMESPACE 语义过载 | MEDIUM | TriggerConfig 改为 `TriggerConfigFactory`（ComponentFactory 子类），`create()` 返回 TriggerConfig。统一存储形态（§9） |
| GAP-6 AssembledAgent 类型未定义 | HIGH | 新增 `class AssembledAgent` block + 字段 + 消费者映射（§6.1） |
| GAP-7 resolve_bundle 未定义 | MEDIUM | 新增 `TypedBundle` + `resolve_bundle(namespace)` 签名 + 调用路径（§10.1） |
| GAP-8 RosterLoader 未定义 | MEDIUM | 新增 `class RosterLoader` 签名 + `RosterBundle`。明确 GraphRoster（配置层）vs GraphSpec（引擎层）关系（§5.2） |
| GAP-9 ctx.is_subagent 不在 AssemblyContext | LOW | 改为 `spec.agent_type == AgentType.external_sub`（§6.4） |
| GAP-10 ResourceFactory.materialize 路径 | MEDIUM | 改为通过 `workspace_registry.materialize()`（不直接调 factory），保留缓存/LRU/单飞（§6.2） |
| GAP-11 崩溃处理系统性未指定 | MEDIUM | 新增 §6.7 失败处理节——10 个场景全部定义行为 |
| GAP-12 特殊 agent 重触发 | LOW | 添加 `done → enabled` 循环转换 + cooldown 语义（§9） |
| GAP-13 AgentType YAML 派生 | LOW | 新增派生规则表（§6.6）——provider_kind 是否设置决定 native/external |
| GAP-14 历史实现清理规划不完整 | MEDIUM | 新增 §13.1 文件级清理清单——27 个文件按阶段分组，每个标注处置（删除/迁移到 FW/修改/保留）+ 替代 |
| GAP-15 7 槽位配置传输路径缺失 | HIGH | AssemblySpec 补齐 7 个槽位字段（memory_providers/skill_sources/memory_system_modifiers + per-component configs）。INTERCEPTOR/COMMAND_HANDLER/INPUT_STAGE 不在 AssemblySpec——分别由 PoolAssembleStage/InfraAssembleStage 从 PoolRoster/workspace 配置解析（§6.6 槽位配置层级表） |
| GAP-16 默认组件包未定义 | HIGH | 新增默认组件包表（§6.6）——12 个槽位每个都有默认组件 + 默认 config 来源。空 agent template 得到当前 bot_project 默认行为 |
| GAP-17 subagent execution_strategy 填充 | LOW | SubagentSpec 有独立 execution_strategy + provider_kind（specs.py:120-121）。SpecBuilder 从 SubagentSpec 读取，和 main agent 一样派生 agent_type。subagent 可独立选择，不强制跟随 main（§6.6 subagent 差异表） |
| GAP-18 roster YAML 缺少 per-component config | MEDIUM | §5.1 YAML 示例补充 tool_configs/hook_configs/memory.providers/memory.modifiers/skills 字段 + pool.yml 补充 interceptors/commands |
| GAP-19 hook 缺少 agent_type 自动过滤 | MEDIUM | HookFactory 声明 `applies_to: ClassVar[set[AgentType] | None]`。AgentAssembleStage 根据 spec.agent_type 自动过滤——subagent_auto_send 不需 roster 显式配置（§6.7 Hook 归属表） |
| GAP-20 配置策略未定义 | HIGH | 新增 §6.7 配置策略节——main/sub 默认配置表 + hook applies_to 过滤 + memory 配置 vs 数据 scope 正交 + experience 工具 vs hook 区分 + 特殊 agent 触发默认值 |
| GAP-21 模块落地位置未定义 | HIGH | 新增 §18 模块落地设计——在 `src/modex_agent/plugins/` 内实现（替换旧空壳），22 个新文件分布在 `plugins/`（18）+ `config/`（3）+ `__init__.py`（1）。现有实现不动，defaults 包装现有 ABC。不新增顶层包 |

---

## 13. 分阶段实现路径

```
阶段 1: 基础设施                    阶段 2: 收敛 agent 装配
┌─────────────────────────┐       ┌─────────────────────────────┐
│ 删除旧 plugin 体系        │       │ assemble_agent 统一入口       │
│ ComponentRegistry        │  ──>  │ ExternalStrategy 收敛 main+sub│
│ ComponentFactory + ABC   │       │ AgentTemplate.materialize 收敛│
│ Plugin(ABC) + 12 槽位    │       │ 5 个默认 stage               │
│ AssemblyPipeline/Stage   │       │ AssemblySpec/Builder/Context  │
│ AssemblySpec/Builder     │       │                              │
└─────────────────────────┘       └─────────────────────────────┘
        │                                  │
        v                                  v
阶段 3: roster + 配置              阶段 4: 特殊 agent + 触发
┌─────────────────────────┐       ┌─────────────────────────────┐
│ RosterLoader             │       │ 触发机制 plugin 化           │
│ 两层 YAML (pool+template)│       │ 默认 plugin 包               │
│ toolPreset 合并          │  ──>  │ (experience/compactor/...)   │
│ 增删语法                 │       │                              │
│ 默认组件包               │       │                              │
└─────────────────────────┘       └─────────────────────────────┘
        │                                  │
        v                                  v
阶段 5: 图接缝                     阶段 6: 迁移 + 测试
┌─────────────────────────┐       ┌─────────────────────────────┐
│ GraphSpec state_schema   │       │ bot_project 迁移到新装配链    │
│ FieldSpec + compiler     │  ──>  │ 现有测试适配                 │
│ 图装配进 InfraAssemble   │       │ 新管道测试 + 集成验证         │
│ DATA_NAMESPACE 类型共用  │       │ 删除旧装配代码               │
└─────────────────────────┘       └─────────────────────────────┘
```

### 13.1 历史代码清理清单

文件级处置表，按阶段分组。每个文件标注：处置（删除/迁移到 FW/修改/保留）+ 替代。

#### 阶段 1：基础设施（旧 plugin 体系删除）

| 文件 | 位置 | 处置 | 替代 |
|---|---|---|---|
| `src/modex_agent/plugins/__init__.py` | FW | 删除 | `ComponentRegistry` |
| `src/modex_agent/plugins/context.py` | FW | 删除 | `PluginRegistrationContext` |
| `src/modex_agent/plugins/manager.py` | FW | 删除 | `ComponentRegistryLoader` |
| `src/modex_agent/plugins/loader.py` | FW | 删除 | `ComponentRegistryLoader._discover()` |
| `src/modex_agent/plugins/bundled/` | FW | 删除 | bundled 默认 plugin 包 |
| `bot/plugins/integration.py` | BIZ | 删除 | — |
| `bot/service/_runtime_builders.py` | BIZ | 删除（`_collect_run_hooks` 函数） | InfraAssembleStage |
| `bot/workspace/wiring/resources.py:226-233` | BIZ | 修改（删除 `plugin_integration` 引用 + `_collect_run_hooks` 调用） | InfraAssembleStage |
| `bot/service/core.py:366` | BIZ | 修改（删除 `PluginIntegration` 初始化） | `ComponentRegistryLoader.load()` |
| `bot/service/builders.py:188` | BIZ | 修改（删除 PluginIntegration 字段） | — |

#### 阶段 2：收敛 agent 装配（旧装配代码迁移/删除）

| 文件 | 位置 | 处置 | 替代 |
|---|---|---|---|
| `bot/service/pool/factory.py`（596行） | BIZ | 修改（`create_pool` 改为调用 `assemble_agent()`） | AssemblyPipeline |
| `bot/service/pool/pool_construction.py`（123行） | BIZ | 删除 | `PoolAssembleStage` |
| `bot/service/pool/pipeline_wiring.py`（224行） | BIZ | 删除 | `AgentAssembleStage`（hooks/interceptors/governance/approval） |
| `bot/service/pool/agent_factory.py`（146行） | BIZ | 删除 | `AgentAssembleStage` + `ComponentFactory.create()` |
| `bot/service/pool/assembly_context.py`（134行） | BIZ | 迁移到 FW（`PoolAssemblyContext` 成为框架类型，`AssemblyContext.pool_runtime.pool_assembly_ctx` 引用） | FW `PoolAssemblyContext` |
| `bot/service/pool/strategy_registry.py`（37行） | BIZ | 删除 | `EXECUTION_STRATEGY` 槽位 + `ExecutionStrategyRegistry` |
| `bot/service/pool/communication.py`（170行） | BIZ | 修改（保留通信构建逻辑，被 `PoolAssembleStage` 调用，不再独立编排） | `PoolAssembleStage` 内调用 |
| `bot/service/pool/memory_defaults.py`（70行） | BIZ | 迁移到 FW | 框架默认 `MEMORY_SYSTEM_MODIFIER` plugin |
| `bot/service/pool/tool_projection.py`（57行） | BIZ | 删除 | `toolPreset` + `SpecBuilder.from_roster()` |
| `bot/service/pool/external_subagent.py`（51行） | BIZ | 删除 | `ExternalExecutionStrategy`（Q6 收敛 main+sub） |
| `bot/service/pool/__init__.py` | BIZ | 修改（re-exports 更新） | — |
| `bot/workspace/wiring/stack.py`（161行） | BIZ | 修改（`build_workspace_stack` 调用新装配链） | AssemblyPipeline |
| `bot/workspace/wiring/resources.py`（563行） | BIZ | 修改（`_assemble_resources` 调用 stage 而非内联装配；图装配委托 `InfraAssembleStage`） | InfraAssembleStage |
| `bot/workspace/wiring/pool_wiring.py`（111行） | BIZ | 修改（`_wire_pool_to_resources` 简化，ExperienceReviewHook 由触发 plugin 安装） | 触发 plugin 化（§9） |
| `bot/service/builders.py` | BIZ | 修改（tool factories 注册到 ComponentRegistry，`resolve_system_prompt` 改为 SYSTEM_PROMPT_PROVIDER 组件） | ComponentRegistry |
| `bot/service/_assembly_helpers.py` | BIZ | 评估：有用逻辑迁移到 stage，剩余删除 | 按内容评估 |
| `bot/service/react_strategy.py` | BIZ | 修改（`ReactExecutionStrategy.assemble` 调用 `assemble_agent()`，内联装配逻辑删除） | AssemblyPipeline |
| `bot/service/core.py` | BIZ | 修改（启动序列调用 `ComponentRegistryLoader.load()` + `RosterLoader.load()`，删除旧 `PluginIntegration`） | ComponentRegistryLoader + RosterLoader |
| `src/modex_agent/multi_agent/template.py` | FW | 修改（`AgentTemplate.materialize` 改为调用 `assemble_agent(sub_spec, ctx)`，native/external 分支删除） | AssemblyPipeline |
| `src/modex_agent/agents/external/subagent_builder.py` | FW | 删除（`BotSubagentExternalBuilder` 逻辑吸收进 `ExternalExecutionStrategy.assemble`） | ExternalExecutionStrategy |
| `src/modex_agent/agents/external/builder.py` | FW | 评估：如果 `ExternalAgentBuilder` 被 `ExternalExecutionStrategy` 完全覆盖则删除，否则保留 | ExternalExecutionStrategy |

#### 阶段 3：roster + 配置

| 文件 | 位置 | 处置 | 替代 |
|---|---|---|---|
| `bot/config/memory_defaults.py` | BIZ | 迁移到 FW | 框架默认 MemoryConfig plugin |
| `bot/config/pools/*/pool.yml` | BIZ | 修改（适配新 roster 格式） | PoolRoster |
| `bot/templates/*.yml` | BIZ | 修改/新增（per-agent roster） | AgentRoster |

#### 阶段 5：图接缝

| 文件 | 位置 | 处置 | 替代 |
|---|---|---|---|
| `bot/graph/spec_loader.py` | BIZ | 迁移到 FW | FW `GraphSpecLoader` |
| `bot/graph/agent_node_factory.py` | BIZ | 保留（绑定业务 `WorkspaceResolverCell`） | — |
| `bot/graph/output_adapter.py` | BIZ | 保留（WebUI 是业务） | — |
| `bot/graph/__init__.py` | BIZ | 修改（re-exports 更新） | — |

#### 阶段 6：测试适配

| 范围 | 处置 |
|---|---|
| 依赖旧 `PluginManager`/`PluginContext` 的测试 | 删除或重写为 `ComponentRegistryLoader` 测试 |
| 依赖旧 `create_pool` 内联装配的测试 | 修改为调用 `assemble_agent()` |
| 依赖旧 `AgentTemplate.materialize` 分支的测试 | 修改为调用 `assemble_agent(sub_spec, ctx)` |
| 依赖旧 `BotSubagentExternalBuilder` 的测试 | 修改为 `ExternalExecutionStrategy.assemble` sub 分支 |
| 架构守卫测试（`test_modex_graph_isolation.py` 等） | 验证不破坏 |
| 新增 | AssemblyPipeline 单元测试 + 集成测试 + ComponentRegistryLoader 测试 + RosterLoader 测试 |

### 规模估算

| 变更类型 | 估算行数 | 内容 |
|---|---|---|
| 新增框架代码 | ~2800 | ComponentRegistry、ComponentFactory、Plugin ABC、12 槽位、AssemblyPipeline、AssemblyStage、AssemblySpec/Builder/Context/Result、5 个默认 stage、默认 plugins、RosterLoader |
| 修改框架代码 | ~800 | ExecutionStrategy 收敛、AgentTemplate.materialize 收敛、PluginContext 删除 |
| 修改业务代码 | ~1200 | create_pool 重构为 stage 调用、wiring 重构、roster 解析、builders 收敛 |
| 删除代码 | ~500 | 当前 plugin 体系 |
| 测试 | ~2500 | 新管道单元测试 + 集成测试 + 现有测试适配 |
| **总计** | **~7800** | |

---

## 14. 参考项目对照

| 维度 | 参考项目 | ModexAgent | 关系 |
|---|---|---|---|
| 装配机制 | 反应式依赖图（Fiber PENDING→LOADING→ACTIVE） | AssemblyPipeline（有序 stage 管道，spec→builder→agent） | **全新设计**，非 port |
| 槽位数量 | 56（29 core + 26 seam + 1 bundle） | 12（v1 闭集） | 压缩 |
| workspace | 数据注册表 | 运行时隔离容器 + 资源构建可插拔 | **不同概念** |
| 热插拔 | Fiber `restart()`/`update()` | **不做**（重启生效） | 明确放弃 |
| Python SDK | subprocess + JSON-RPC | 原生 Python | 无参考价值 |
| schemastery | Standard Schema V1 | frozen Pydantic v2 `extra="forbid"` | 映射表见 06 票研究产出 |

---

## 15. 票状态总表

| 票 | 状态 | 主题 | 与 SPEC 关系 |
|---|---|---|---|
| 01 | ✅ closed | KVStore + 命名空间层 | §10 数据层，无冲突 |
| 02 | ✅ closed (修正) | 12 槽位 + ComponentRegistry | §4，**Q2 collect-then-inject 被推翻**（§12） |
| 03 | ✅ closed | YAML + AssemblyPipeline | §5+§6 核心设计 |
| 04 | ✅ closed | 装配搬家 | §13 实现路径 |
| 05 | ✅ closed | 图接缝 | §8 图接缝 |
| 06 | ✅ closed | 参考项目取证 | §14 参考对照 |
| 07 | ✅ closed | Python 先例 | §4.5 Plugin(ABC) 契约 |
| 08 | ✅ closed | 数据层原型 | §10.4 不需要 prototype |

---

## 16. 闭包检查结果

### 第一轮（内部检查）

trace 了七个维度（配置流/控制流/类型流/生命周期/扩展点/一致性/遗漏），发现 4 条架构级断裂 + 4 条设计级矛盾 + 9 条遗漏，全部在 §12 修正表中解决。

### 第二轮（design-closure skill，5 维度并行 trace）

5 个 dimension tracer（data-flow/lifecycle/convergence/interface/state-machine）并行 trace 了 70 个结构项，发现 13 个 gap（1 CRITICAL + 3 HIGH + 6 MEDIUM + 3 LOW），全部在 §12 第二轮修正表中解决。

**收敛维度完全闭合**——10 个 concern 无双路径违规。**跨维度 seam** 6 条 trace，4 条 gap 已修复。

**设计已闭环**：每条路径端到端连接，无断裂；SPEC 内部无矛盾；所有遗漏有明确方案；13 个 gap 全部修复。可以进入实现阶段。

### 第三轮（聚焦审视，5 个用户关注领域）

聚焦验证数据流完整性 / 配置处理 / 差异化配置 / 自定义插件处理 / 历史实现清理规划。前 4 个领域全部闭合（13 gap 修复一致）。第 5 个领域发现 GAP-14（历史代码清理清单不完整）——27 个文件需处置，SPEC 只覆盖 7 个。已修复：新增 §13.1 文件级清理清单。

**设计已闭环**（第三轮确认）：14 个 gap 全部修复，5 个用户关注领域全部闭合。可以进入实现阶段。

### 第四轮（提问式打磨，4 个聚焦问题）

聚焦验证配置隔离 / 全槽位插件化 / main-sub 差异 / 自定义 tool+memory 路径。发现 GAP-15/16/17/18（7 槽位配置传输缺失 + 默认组件包未定义 + subagent execution_strategy + per-component config YAML），全部修复：

- AssemblySpec 补齐 7 个槽位字段 + 槽位配置层级表（per-agent 7 / pool 3 / workspace 1 / global 1）
- 默认组件包表（12 槽位全有默认值，空 template = 当前 bot_project 行为）
- subagent 差异表（memory/experience/approval/max_steps/tool_preset/comm_kind/装配时机/stage 子集）
- roster YAML 示例补全 per-component config + pool 级槽位字段

**设计已闭环**（第四轮确认）：18 个 gap 全部修复。12 槽位全插件化、全配置化、全默认值。可以进入实现阶段。

### 第五轮（配置策略打磨，6 个聚焦问题）

聚焦验证默认配置策略 / subagent hook 差异化 / memory 层默认 / experience 默认 / memory 配置 vs 数据 scope / pool/workspace 插件化。发现 GAP-19/20（hook agent_type 过滤 + 配置策略未定义），全部修复：

- 新增 §6.7 配置策略节：main/sub 默认配置表 + hook applies_to 自动过滤 + memory 配置 vs 数据 scope 正交 + experience 工具 vs hook 区分 + 特殊 agent 触发默认值
- HookFactory 声明 `applies_to` —— subagent-only/main-only hook 自动过滤，用户不需 roster 显式管理

**设计已闭环**（第五轮确认）：20 个 gap 全部修复。配置策略完整——默认值、差异化、过滤、scope 正交、特殊 agent 触发全部定义。可以进入实现阶段。

### 第七轮（模块落地 + 插件来源确认）

聚焦验证插件体系模块落地位置 + bot_project 插件化配置分布。发现 GAP-21（模块落地位置未定义），已修复：

- 新增 §18 模块落地设计：在 `src/modex_agent/plugins/` 内实现（替换旧空壳），22 个新文件
- `plugins/defaults/` 包装现有 ABC 实现（现有代码不动）
- `plugins/assembly/` 放 AssemblyPipeline + 5 stage
- `config/` 放 RosterLoader + roster types + SpecBuilder
- 用户插件放 `examples/bot_project/plugins/` + `~/.modex/plugins/` + entry_points
- 依赖方向：`plugins/` → 现有模块，不反向

**设计已闭环**（第七轮确认）：21 个 gap 全部修复。模块落地位置、文件清单、依赖方向、用户插件目录全部明确。可以进入实现阶段。

### 第六轮（架构选型边界确认，参考项目对比）

聚焦验证 workspace/pool 是否应像参考项目那样完全插件化。经参考项目源码级审计（`bg_53ab8414` 报告），确认：参考项目的 workspace/pool 概念与我们的**根本不同**——参考项目的 workspace 是数据记录（path + sessionIds CRUD），不是运行时容器。参考项目无 pool/router/bus/poller/tree 等等价物。详见 §17。

---

## 17. 架构选型边界

### 17.1 选型结论

**保持当前架构（共享总线 + 持久池 + LRU 容器），不采用参考项目的架构（per-agent Inbox + scope 事件 + 按需创建 + Fiber disposal）。**

参考项目的 workspace/pool 完全是插件——但参考项目的 workspace 是**数据记录**（path + sessionIds CRUD），不是**运行时容器**。参考项目无 pool/router/bus/poller/tree 等等价物。两种架构是不同的运行时模型，不是"同一模型的不同实现"。

### 17.2 我们架构的优势

| 优势 | 说明 | 代码依据 |
|---|---|---|
| **持久池零延迟** | agent 在 pool 创建时预构建，首次 turn 直接执行。参考项目每次按需创建，首次 turn 有装配延迟 | `pool/pool_construction.py` 预构建 main agent |
| **LRU eviction** | 多 workspace 场景自动管理内存，不活跃的 workspace 自动驱逐释放资源。参考项目无此能力，内存无限增长直到 owner 显式 dispose | `WorkspaceRegistry` materialize/evict/LRU |
| **In-flight turn protection** | 防止并发 turn 破坏 agent 状态（inflight dict 单飞）。参考项目只有 ACP 协议层有一层保护，workspace 级无 | `InboxPoller.inflight: dict[sid, Task]` |
| **跨 pool peer 通信** | 不同 pool 的 main agent 互相通信（`send_to_peer`）。参考项目无 peer 通信概念 | `PeerNormalStrategy` + `CommunicationTarget.tree_ref` |
| **显式 session 树** | SessionTreeManager 持有树结构，O(1) 查询父子关系。参考项目从 header 字段派生，需要遍历查询 | `SessionTreeManager` |
| **poll-driven 收敛** | 单 InboxPoller per pool 驱动所有 between-turn 调度，fold-in 处理 mid-turn 消息。参考项目每个 agent 有自己的 Inbox，无统一收敛点 | `InboxPoller` + `InboxFlushHook` |
| **已验证生产运行** | bot_project 端到端跑通，有测试覆盖。参考项目的架构在 Python 里无直接等价物（Fiber/scope/context proxy 是 TypeScript 特定） | 全仓库测试套件 |

### 17.3 我们架构的缺点（诚实承认）

| 缺点 | 说明 | 影响范围 |
|---|---|---|
| **8 个骨架组件框架级硬耦合** | WorkspaceRegistry/PoolRouter/AgentPool/SessionTreeManager/InboxPoller/AgentMessageBus/WorkspacePaths/session_workspace_map 不可独立替换 | 用户不能自定义这些组件的行为（只能通过 ABC 扩展点间接控制） |
| **组件交互复杂** | InboxPoller + AgentMessageBus + PoolRouter + SessionTreeManager 四者耦合，修改一个需要理解全部 | 维护成本高，新人理解曲线陡 |
| **插件化边界受骨架限制** | workspace/pool 的行为不能完全由插件定义。用户不能像参考项目那样"换一个 workspace 实现" | workspace/pool 的生命周期管理/路由/调度策略固定 |
| **无 Fiber 式自动 disposal** | Python 无 Fiber 的自动逆序 disposal。我们的 cleanup 是手写的（AssemblyBuilder.cleanup()） | 资源泄漏风险比参考项目高（但 §6.8 失败处理已覆盖装配场景） |
| **首次 pool 创建有成本** | 持久池预构建意味着 pool 创建时构建所有 main agent。参考项目按需创建，启动更快 | 多 pool 场景下启动时间更长（但首次 turn 零延迟补偿） |

### 17.4 参考项目架构对比（为什么不采用）

| 维度 | 参考项目 | 我们 | 不采用参考项目的理由 |
|---|---|---|---|
| workspace | 数据记录（path + sessionIds CRUD），`extends Service` 插件 | 运行时容器（broker/inbox/bus/pools/interceptor/graph） | 我们需要运行时容器——多 workspace 的 LRU/eviction/in-flight 保护是核心能力 |
| pool | 无——SubagentRuntime 是按需 provider 注册表 | AgentPool——持久星型拓扑 hub | 我们需要持久池——首次 turn 零延迟 + 跨 pool peer 通信 |
| 消息路由 | scope-filtered 事件分发 + 直接方法调用 | PoolRouter + AgentMessageBus + InboxPoller 共享总线 | 我们需要共享总线——poll-driven 收敛 + fold-in + 单飞保护 |
| session 树 | SessionHeader.parentSession 字段，按需派生 | SessionTreeManager 持有树 | 我们需要 O(1) 树查询——多级 subagent 场景 |
| LRU/eviction | 无——agent 活到 owner dispose | WorkspaceRegistry materialize/evict/LRU | 我们需要自动内存管理——多 workspace 场景 |
| 运行时隔离 | 参考项目的 scope 机制（scope 创建与父子绑定） | per-workspace AssemblyContext（resources evictable） | 两种机制等价目的，不同形态。我们的更重但更显式 |
| Fiber disposal | 自动逆序销毁 | 手写 cleanup（AssemblyBuilder.cleanup()） | Python 无 Fiber 等价物，手写是唯一选择 |
| TypeScript 特定 | 是——Context proxy/Reflect/Fiber 是 TS 特定 | N/A | 在 Python 里重新实现这套机制 = 重新发明轮子 |

**核心判断**：完整重构到参考项目架构 = 抛弃 ~5000 行骨架代码 + 重新实现 LRU/in-flight/peer/持久池（参考项目没有这些，要从零写）+ 迁移 ~5000 行测试。总计 ~15000 行 + 高风险。换来的只是"骨架可替换"——但用户不需要替换骨架，需要的是自定义 tool/hook/memory/prompt/strategy，这些我们已支持。

### 17.5 绝对不做清单（v1 + 可预见的未来）

以下内容**明确排除在设计范围之外**，不纳入实现规划：

#### 架构级（不做）

| # | 不做项 | 理由 |
|---|---|---|
| N1 | **不采用参考项目的 per-agent Inbox + scope 事件架构** | 需要完整重写骨架（~5000 行），丢失 LRU/in-flight/peer/持久池，重新实现这些（~3000 行），高风险无对等收益 |
| N2 | **不实现 Fiber 机制的 Python 等价物** | Fiber 是 TS 特定的插件生命周期状态机。Python 无 proxy-based context，重新实现成本高且无直接收益 |
| N3 | **不实现 Context proxy / ReflectService 的 Python 等价物** | 同 N2——Python 无 Proxy-based 依赖注入容器，ComponentRegistry 已是等价物 |
| N4 | **不做运行时热插拔 / HMR** | 重启生效降低所有设计复杂度。热插拔需要 Fiber 式 disposal + 状态迁移，成本极高 |
| N5 | **不做 per-session 配置隔离** | 配置隔离到 pool 级（不同 pool 不同 roster）。session 级只需状态隔离（SessionScope KVStore + runtime.state），不需配置隔离。参考项目的 per-session preset 机制在 Python 里需要 per-session AssemblyContext，与当前设计冲突 |
| N6 | **不做动态 agent 定义（运行时创建新 agent 类型）** | 所有 agent 在启动时从 YAML 加载。`assemble_agent(spec, ctx)` 技术上可复用，但 SPEC 不暴露动态注册接口。参考项目的动态 define/run/stop/undefine 模型自修改能力与我们的重启生效假设根本冲突 |
| N7 | **不做插件间依赖声明** | 固定 stage 顺序隐式覆盖大部分依赖。跨关注点依赖（如 tool 依赖特定 hook）由 roster 显式引用保证（用户同时引用两者） |

#### 槽位级（不做）

| # | 不做项 | 理由 |
|---|---|---|
| N8 | **不开放 ADAPTER + EMITTER 槽位（v1）** | per-instance 工厂构造，需要装配回迁才有框架落点。v1 业务层处理 |
| N9 | **不开放 EXTERNAL_PROVIDER 槽位（v1）** | v1 保留 `provider_kind` 硬编码。v2 落槽位时改为组件名引用 |
| N10 | **不开放 GOVERNANCE / APPROVAL_CLASSIFIER 槽位（v1）** | v1 governance 从 MemoryConfig 派生。v2 开槽位时 roster 可配置 |
| N11 | **不开放 SANDBOX 槽位** | 17 文件零外部引用。先以 TOOL_CALL interceptor 形态接线再议 |
| N12 | **不开放 ASSEMBLY_STAGE 槽位（插件自定义装配步骤）** | 固定 5 stage。参考项目通过 setup 钩子让插件注入装配步骤——我们的固定 stage + hooks/interceptors 覆盖大部分场景。若未来需要，AssemblyPipeline 可扩展支持插件注册的 stage |

#### 组件级（不做）

| # | 不做项 | 理由 |
|---|---|---|
| N13 | **不替换 WorkspaceRegistry[R] 的生命周期管理** | materialize/evict/LRU/in-flight protection 是框架级核心能力。策略可参数化（容量/驱逐策略可配置），但管理职责本身不可替换 |
| N14 | **不替换 PoolRouter 的 session→pool 分发** | PoolRoutingStore ABC 已允许后端可插拔，但分发逻辑本身不可替换 |
| N15 | **不替换 AgentPool 的 poller/tree/单飞** | InboxPoller 的轮询策略可参数化（间隔/event-driven），AgentPool 的单飞策略可配置（超时/重试），但组件本身不可替换 |
| N16 | **不替换 SessionTreeManager 的树所有权** | 树结构是 pool 级核心。SessionTreeManager 的实现可参数化，但树所有权不可移交 |
| N17 | **不替换 AgentMessageBus 的消息总线** | LocalAgentMessageBus 是默认实现。后端可插拔（本地 vs 远程），但总线职责本身不可替换 |
| N18 | **不将特殊 agent（experience/compactor/consolidator/summarizer）走 AssemblyPipeline** | tool 特殊配置不可组件化（inline 构造）。例外封闭（4 个内置 agent，数量固定） |

#### 功能级（不做）

| # | 不做项 | 理由 |
|---|---|---|
| N19 | **不做 per-step model selection（框架层）** | WebUI 已有 per-turn model selector（业务层）。per-step 切换通过 hook 实现（运行时覆盖），不需要框架层重做 |
| N20 | **不做 per-workspace 私有插件目录** | v1 用"workspace 专属插件注册到全局 ComponentRegistry + roster 只在该 workspace 引用"近似。若未来需要 per-workscope registry，是原则 1 的翻转条件 |
| N21 | **不做变量面板 / WebUI 呈现插件数据（v1）** | v1 只做变量面（KVStore + 类型命名空间）。WebUI 呈现是 UI 关注点，后期评估 |
| N22 | **不做事件面开放（transcript 插件可登记事件类型）** | v1 只做变量面。事件面开放留雾区，待变量面原型反馈后毕业 |
| N23 | **不做 eval / cassette 回放适配新数据层** | 后期 |
| N24 | **不做 A→B stage 重组（v1）** | AssemblySpec 已按关注点设计，A→B 迁移成本中等。v1 用选项 A（按当前装配链 7 步），待阶段 2 完成后评估是否需要 B（按关注点重组） |

### 17.6 可参数化的骨架策略（虽不可替换，但可配置）

以下骨架组件虽不可替换，但其策略可通过配置参数化：

| 组件 | 可参数化策略 | 配置方式 |
|---|---|---|
| WorkspaceRegistry | LRU 容量 / 驱逐策略（LRU/FIFO/手动） | workspace 配置 |
| InboxPoller | 轮询间隔 / event-driven 开关 | pool 配置 |
| AgentPool | 单飞超时 / 重试策略 / 最大错误退避 | pool 配置 |
| SessionTreeManager | 最大深度 / 最大子节点数 | pool 配置 |
| AgentMessageBus | 后端（本地/远程） | workspace 配置 |

这些参数化能力不在 v1 实现范围内，但设计上不阻止未来添加。骨架组件的接口应预留配置注入点。

### 17.7 3 个保守组件的可插件化路径（未来）

以下 3 个组件当前是"保守"硬编码，但可低成本外部化为插件：

| 组件 | 当前 | 可插件化路径 | 优先级 |
|---|---|---|---|
| WorkspacePaths | 14 个路径常量 | 外部化为 `WorkspacePathsProvider` 插件（参考项目只有一个路径规范化函数） | 低 |
| session_workspace_map | SQLite FK 表 | 插件拥有的 KV 表（参考项目用存储域 KV 表） | 低 |
| ExecutionStrategyRegistry / GraphOrchestrator | 已在可插拔路径上 | 已通过 EXECUTION_STRATEGY 槽位 + InfraAssembleStage | 已在 SPEC 覆盖 |

这些不在 v1 实现范围内，但 SPEC 设计不阻止未来外部化。

---

## 18. 模块落地设计

### 18.1 选型

**在 `src/modex_agent/plugins/` 内实现（替换旧空壳），不新增顶层包。**

旧 `src/modex_agent/plugins/`（5 文件空壳）全删（§4.4），新代码填充同一目录。框架自带默认组件的 ComponentFactory 放在 `plugins/defaults/` 子模块。AssemblyPipeline 基础设施放在 `plugins/assembly/` 子模块。

不选 `src/modex_plugins/` 顶层包的理由：
- 依赖方向——新代码要 import `modex_agent` 的 ABC（Tool/Hook/ExecutionStrategy），作为子模块无 import 方向问题；作为独立包则用户需装两个包
- `modex_graph` 已是独立包（ADR-0033），再加一个顶层包增加复杂度
- 框架自带默认组件天然属于框架

### 18.2 模块结构

```
src/modex_agent/plugins/
├── __init__.py                    # 公共 API 导出（ComponentRegistry, Plugin, ComponentFactory, AssemblyPipeline 等）
├── abc.py                         # Plugin(ABC) + ComponentFactory(ABC) + SimpleFactory + HookFactory(applies_to)
├── registry.py                    # ComponentRegistry + ComponentSlot(StrEnum) + resolve_bundle + TypedBundle
├── loader.py                      # ComponentRegistryLoader + PluginRegistrationContext
├── defaults/                      # bundled 默认插件（框架自带，包装现有实现）
│   ├── __init__.py                # DefaultPlugin(Plugin) — register() 调用下面所有 register_* 
│   ├── tools.py                   # 标准工具 ComponentFactory（fs/search/read/write/bash/lsp/aci/ast_grep/todo/task/send_to_peer/send_to_agent）
│   ├── hooks.py                   # 标准 hook ComponentFactory（9 个，带 applies_to 声明）
│   ├── strategies.py              # react + external ExecutionStrategy ComponentFactory
│   ├── llm.py                     # default LLM_PROVIDER（从 model.yml 读 config）
│   ├── prompt.py                  # file_prompt SYSTEM_PROMPT_PROVIDER
│   ├── memory.py                  # main_agent_memory + subagent_memory MEMORY_SYSTEM_MODIFIER
│   ├── interceptors.py            # tool_timeout INTERCEPTOR
│   ├── commands.py                # /cd /stop /pool /approve /deny /continue COMMAND_HANDLER
│   ├── input_stages.py            # 8 个标准 INPUT_STAGE ComponentFactory
│   └── triggers.py                # 4 个特殊 agent TriggerConfigFactory
└── assembly/                      # AssemblyPipeline 基础设施
    ├── __init__.py
    ├── pipeline.py                # AssemblyPipeline + AssemblyStage(ABC)
    ├── spec.py                    # AssemblySpec + AgentType + SpecBuilder + MemoryOverrides
    ├── builder.py                 # AssemblyBuilder + AssembledAgent
    ├── context.py                 # AssemblyContext + PoolRuntimeDeps
    └── stages/                    # 5 个默认 stage
        ├── __init__.py
        ├── workspace_materialize.py   # Stage 1: WorkspaceMaterializeStage
        ├── infra_assemble.py          # Stage 2: InfraAssembleStage
        ├── pool_assemble.py           # Stage 3: PoolAssembleStage
        ├── agent_assemble.py          # Stage 4: AgentAssembleStage
        └── subagent_assemble.py       # Stage 5: SubagentAssembleStage
```

### 18.3 与现有模块的关系

**现有实现不动**——`plugins/defaults/` 里的 ComponentFactory **包装**现有 ABC 实现，不移动现有代码。

```
modex_agent/
├── plugins/               # ← 新：插件基础设施 + bundled 默认 + AssemblyPipeline
│   ├── abc.py             #    Plugin(ABC) / ComponentFactory(ABC) / SimpleFactory / HookFactory
│   ├── registry.py        #    ComponentRegistry（唯一信任边界）
│   ├── loader.py          #    ComponentRegistryLoader（启动加载）
│   ├── defaults/          #    bundled 默认组件（包装现有实现）
│   └── assembly/          #    AssemblyPipeline + 5 stage
├── hook/builtin/          # ← 现有不动：InboxFlushHook / TodoContinuationHook 等
├── interceptor/builtin/   # ← 现有不动：ToolTimeoutInterceptor
├── tools/standard/        # ← 现有不动：fs/search/read/write/bash/lsp
├── tools/ast/             # ← 现有不动：aci/ast_grep
├── providers/             # ← 现有不动：LiteLLM/OpenAI provider
├── multi_agent/           # ← 现有不动：AgentPool/ExecutionStrategy/PoolRouter
├── memory/                # ← 现有不动：MemorySystem/KVStore/MemoryStoreBundle
├── workspace/             # ← 现有不动：WorkspaceRegistry/WorkspacePaths
├── commands/              # ← 现有不动：CommandHandler ABC
├── input_pipeline/        # ← 现有不动：InputStage ABC
└── ...                    # ← 其他 15 个模块不动
```

包装示例：

```python
# plugins/defaults/hooks.py
from modex_agent.hook.builtin.inbox_flush import InboxFlushHook
from modex_agent.hook.builtin.todo_continuation import TodoContinuationHook
from ..abc import ComponentFactory, SimpleFactory, HookFactory
from ..loader import PluginRegistrationContext

def register_default_hooks(ctx: PluginRegistrationContext) -> None:
    ctx.register_hook("inbox_flush", SimpleFactory(InboxFlushHook(), HookConfig))
    ctx.register_hook("todo_continuation", TodoContinuationFactory())  # 工厂形（需 per-pool tree）
    # ... 其余 7 个
```

```python
# plugins/defaults/strategies.py
from modex_agent.multi_agent.execution_strategy import ReactExecutionStrategy, ExternalExecutionStrategy
from ..abc import ComponentFactory

class ReactStrategyFactory(ComponentFactory):
    config_model = StrategyConfig
    def create(self, config, ctx): return ReactExecutionStrategy()  # 现有实现零改动
```

### 18.4 依赖方向

```
plugins/abc.py          → core/（Tool/Hook ABC）
plugins/registry.py     → plugins/abc.py
plugins/loader.py       → plugins/registry.py + plugins/abc.py
plugins/defaults/*      → plugins/abc.py + 现有实现（hook/builtin/、tools/standard/ 等）
plugins/assembly/*      → plugins/registry.py + plugins/abc.py + multi_agent/ + workspace/ + memory/
```

所有依赖方向是 `plugins/` → 现有模块，不反向。现有模块不知道 `plugins/` 的存在——它们只暴露 ABC 和实现。

### 18.5 用户插件目录

用户的 Plugin 类不放在 `src/modex_agent/plugins/` 里。它们放在四个源（§4.1 四源优先级）：

| 源 | 目录 | 谁放 | 优先级 |
|---|---|---|---|
| **bundled** | `src/modex_agent/plugins/defaults/` | 框架开发者 | 最高 |
| **project** | `examples/bot_project/plugins/` | 应用开发者 | 高 |
| **user** | `~/.modex/plugins/` | 终端用户 | 中 |
| **entry_points** | PyPI 包 `modex_agent.plugins` entry group | 第三方分发 | 最低 |

`ComponentRegistryLoader._discover(source)` 按优先级扫描对应目录，import Plugin 子类，调用 `register()`。

### 18.6 RosterLoader 落地

`RosterLoader`（§5.2）是装配链的入口，与 ComponentRegistry 平行。落地位置：

```
src/modex_agent/
├── plugins/               # ComponentRegistry + AssemblyPipeline（机制）
│   └── ...
├── config/                # RosterLoader + roster types（配置解析）
│   ├── roster.py          # PoolRoster / AgentRoster / GraphRoster（frozen Pydantic）
│   ├── loader.py          # RosterLoader + RosterBundle
│   └── spec_builder.py    # SpecBuilder.from_roster() → AssemblySpec
└── ...
```

`config/` 是新模块（框架级配置解析），与 `plugins/` 分离——`plugins/` 负责"组件注册与装配机制"，`config/` 负责"YAML 解析与 spec 构建"。

### 18.7 新增文件清单

| 文件 | 位置 | 阶段 | 内容 |
|---|---|---|---|
| `plugins/abc.py` | FW | 1 | Plugin + ComponentFactory + SimpleFactory + HookFactory |
| `plugins/registry.py` | FW | 1 | ComponentRegistry + ComponentSlot + resolve_bundle + TypedBundle |
| `plugins/loader.py` | FW | 1 | ComponentRegistryLoader + PluginRegistrationContext |
| `plugins/defaults/__init__.py` | FW | 1 | DefaultPlugin(Plugin) 入口 |
| `plugins/defaults/tools.py` | FW | 1-2 | 标准工具 ComponentFactory |
| `plugins/defaults/hooks.py` | FW | 1-2 | 标准 hook ComponentFactory（含 applies_to） |
| `plugins/defaults/strategies.py` | FW | 2 | react + external strategy ComponentFactory |
| `plugins/defaults/llm.py` | FW | 1 | default LLM_PROVIDER |
| `plugins/defaults/prompt.py` | FW | 1 | file_prompt SYSTEM_PROMPT_PROVIDER |
| `plugins/defaults/memory.py` | FW | 2 | main_agent_memory + subagent_memory |
| `plugins/defaults/interceptors.py` | FW | 1 | tool_timeout |
| `plugins/defaults/commands.py` | FW | 1 | 标准命令处理器 |
| `plugins/defaults/input_stages.py` | FW | 1 | 8 个标准 input stage |
| `plugins/defaults/triggers.py` | FW | 4 | 4 个特殊 agent TriggerConfigFactory |
| `plugins/assembly/pipeline.py` | FW | 1 | AssemblyPipeline + AssemblyStage(ABC) |
| `plugins/assembly/spec.py` | FW | 1-2 | AssemblySpec + AgentType + SpecBuilder + MemoryOverrides |
| `plugins/assembly/builder.py` | FW | 1 | AssemblyBuilder + AssembledAgent |
| `plugins/assembly/context.py` | FW | 1 | AssemblyContext + PoolRuntimeDeps |
| `plugins/assembly/stages/*.py` | FW | 2 | 5 个默认 stage |
| `config/roster.py` | FW | 3 | PoolRoster / AgentRoster / GraphRoster |
| `config/loader.py` | FW | 3 | RosterLoader + RosterBundle |
| `config/spec_builder.py` | FW | 3 | SpecBuilder.from_roster() |

总计 ~22 个新文件，分布在 `plugins/`（18 文件）+ `config/`（3 文件）+ `plugins/__init__.py`（1 文件）。

---

## 19. Errata (实现期修订)

本节记录 SPEC 原文在实现/Oracle 审查期间被修订的 4 处设计决定。每条标注：原文章节、原文主张、修订后主张、修订理由。

### Errata-1: Per-pool 模板（§5.1 全局模板 → per-pool）

**原文**（§5.1 两层结构）：原设计采用全局模板方案——`config/templates/*.yml` 是一个跨 pool 共享的模板目录，所有 pool 的 subagent 引用同一全局命名空间下的模板名。

**修订后**：模板是 **per-pool** 的——每个 pool 拥有自己的 `config/pools/<pool>/templates/*.yml` 目录。`RosterLoader` 通过 `PoolStore.read_pool(name)` 读取每个 pool 的 `pool.yml` + `templates/*.yml`，模板名在该 pool 内解析。不同 pool 可以有同名模板（互不冲突）。

**理由**：per-pool 模板与现有 `AgentTemplateRegistry`（`multi_agent/template_registry.py`）的行为一致——它扫描 `config/pools/<pool>/templates/*.yml`。全局模板方案会破坏现有隔离语义，且与 `PoolStore` 的 per-pool 读写契约冲突。实现时（task 8 RosterLoader）确认 per-pool 是唯一与现有代码兼容的方案。

**实现依据**：`src/modex_agent/config/loader.py` RosterLoader 通过 `pool_store.read_pool(name)` 取得 `PoolSpec`（含 `subagents: list[SubagentSpec]`），不从全局 `config/templates/` 目录读取。

### Errata-2: FieldSpec 无 name 字段（§8.2 name/type/item_type/initial → type/item_type/initial）

**原文**（§8.2 GraphSpec state schema 声明）：`FieldSpec` 是 frozen Pydantic model，字段为 `name` / `type` / `item_type` / `initial`。

**修订后**：`FieldSpec` **无 `name` 字段**。`state_schema` 是 `dict[str, FieldSpec]`——dict 的 key 就是字段名，`FieldSpec` 只携带 `type` / `item_type` / `initial`。这消除了 dict key 与 `name` 字段之间的冗余和不一致风险。

**理由**：dict key 已经唯一标识字段名；在 `FieldSpec` 内重复存储 `name` 会导致两个真相源（dict key vs `name` 字段），且 Pydantic `extra="forbid"` 无法防止 key 与 `name` 不一致的 bug。移除 `name` 字段使 `FieldSpec` 成为纯粹的"字段形状描述"——dict key 是身份，`FieldSpec` 是形状。

**YAML 形态不变**（§8.2 YAML 示例仍然正确）：
```yaml
state_schema:
  research_notes:          # ← dict key = 字段名（身份）
    type: string           # ← FieldSpec 字段（形状）
    initial: ""
```

### Errata-3: strategies / input_stages 不入 FW defaults（§6.6 默认组件包表）

**原文**（§6.6 默认组件包表）：12 个槽位全部列出默认组件，包括：
- EXECUTION_STRATEGY: 默认 `react`（默认 config 来源：—）
- INPUT_STAGE: 默认 `im_input, webui_input`（默认 config 来源：—）

暗示 FW（`plugins/defaults/`）自带这两个槽位的默认组件工厂。

**修订后**：EXECUTION_STRATEGY 和 INPUT_STAGE 的默认组件**不在 FW `plugins/defaults/` 中注册**。它们是 **bot plugin 领地**——由 bot 项目通过自己的插件注册：
- `BotStrategiesPlugin`（bot 侧）注册 `react` + `external` ExecutionStrategy 工厂到 EXECUTION_STRATEGY 槽位
- `IMInputStagesPlugin`（bot 侧）注册 IM input stage 工厂到 INPUT_STAGE 槽位

FW `plugins/defaults/` 只注册与框架 ABC 直接绑定的默认组件（tools/hooks/memory/interceptors/commands/llm/prompt）。EXECUTION_STRATEGY 和 INPUT_STAGE 的具体实现绑定业务层概念（bot 的 `ReactExecutionStrategy` / `ExternalExecutionStrategy` / IM pipeline stages），不适合放在 FW 默认包里。

**理由**：
1. `ReactExecutionStrategy` / `ExternalExecutionStrategy` 当前定义在 `bot/service/`（BIZ 层），不在 FW。FW 默认包不应 import BIZ 代码。
2. IM input stages（`SetChannel` / `ResolvePool` / `SkillParse` 等）绑定 bot 的 `BotInputContext`，是业务层概念。
3. ComponentRegistry 的四源优先级（bundled > project > user > entry_points）允许 bot 项目通过 project 源注册这些组件——bot 启动时加载自己的插件，注册 strategy + input stage 工厂，roster 引用组件名。

**影响**：空 agent template 仍然得到当前 bot_project 默认行为——因为 bot 启动时通过 `BotStrategiesPlugin` + `IMInputStagesPlugin` 注册了 `react` / `external` / `im_input` / `webui_input` 组件。roster 不写 `execution_strategy` 时，SpecBuilder 使用字符串默认值 `"react"`，装配时从 registry 解析——只要 bot 插件已注册 `react` 工厂，就能解析成功。

### Errata-4: async 管道（§6.1 sync → async）

**原文**（§6.1 三层分离）：Stage 签名和 Pipeline.run 都是同步：
```python
class AssemblyStage(ABC):
    @abstractmethod
    def process(self, spec: AssemblySpec, builder: AssemblyBuilder, ctx: AssemblyContext) -> None: ...

class AssemblyPipeline:
    def run(self, spec: AssemblySpec, ctx: AssemblyContext) -> AssembledAgent: ...
```

**修订后**：管道是 **async** 的。Stage.process 和 Pipeline.run 都是 `async def`，`AssemblyBuilder.build_agent()` 和 `cleanup()` 也是 `async def`：
```python
class AssemblyStage(ABC):
    @abstractmethod
    async def process(self, spec: AssemblySpec, builder: AssemblyBuilder, ctx: AssemblyContext) -> None: ...

class AssemblyPipeline:
    async def run(self, spec: AssemblySpec, ctx: AssemblyContext) -> AssembledAgent: ...

class AssemblyBuilder:
    async def build_agent(self) -> AssembledAgent: ...
    async def cleanup(self) -> None: ...
```

**理由**：`ExecutionStrategy.assemble`（`multi_agent/execution_strategy.py:139`）是 `async def assemble(self, ctx: PoolAssemblyContext) -> StrategyAssembly`。`PoolAssembleStage` 调用 `strategy.assemble(ctx)` 必须 `await`。如果管道是 sync 的，`PoolAssembleStage` 需要用 `asyncio.run()` 包装 strategy 调用——这会创建嵌套 event loop，在已有 event loop 的 bot 进程中崩溃。async 管道是唯一与现有 async strategy 签名兼容的方案。

**影响**：所有 stage 实现 + `assemble_agent()` 入口 + `AssemblyBuilder.build_agent()` / `cleanup()` 都是 `async def`。调用者（`create_pool` / `AgentTemplate.materialize`）在已有 event loop 中 `await assemble_agent(spec, ctx)`。

**与第二轮闭包检查 GAP-3 的关系**：GAP-3 添加了 `builder.cleanup()` 逆序销毁——本 errata 将 `cleanup()` 从 sync 改为 async，使 cleanup 回调（`pool.shutdown_all` / `bridge.stop` / `poller.stop` 等）可以 `await`。这与 task 10 notepad finding #1 一致（`Callable[[], Awaitable[None]]` 是 cleanup 回调的规范类型）。

### Errata-5: 管道收敛为 main-agent 编排器；subagent 直接构建（2026-08-19 实现期修订）

**原文**（§6.3 Stage 子集表）：`native_sub` 走管道 stages 4→5；`external_sub` 走管道 stage 3（strategy 内部处理 sub）。`AgentAssembleStage`（stage 4）"构建 agent"。

**修订后**：
1. **AssemblyPipeline 是 main-agent 编排器**——`native_main` 与 `external_main` 走 stages 1→2→3（3-stage 构造器）。Stage 4 对 native_main 本就产出被丢弃（代码评审 I4——真实 main agent 由 `strategy.assemble_main` 构建），从子集中移除。
2. **subagent 不走管道**——`AgentTemplate.materialize` 直接构建（native 路径恢复 3d85e7ee 的生产验证构建逻辑：session-only memory + preset tools + per-invocation hooks + `agent_factory.create_agent`）；external subagent 经 `deps.strategy_registry` 按 sub spec 自己的 `execution_strategy` 解析策略并调用 `assemble_sub(SubagentInvocationContext)`。
3. **`AgentAssembleStage` / `SubagentAssembleStage` 保留为独立 v2 组件**（registry 驱动的组件解析，被单测与集成测试直接行使），不是生产管道成员。收敛的前提是工厂契约获得 per-invocation context（`parent_session` / `invocation_id` / materialize deps 不适配 per-pool `AssemblyContext`）。
4. **`ExecutionStrategy.assemble` 拆分为 `assemble_main(PoolAssemblyContext) -> StrategyAssembly` + `assemble_sub(SubagentInvocationContext) -> SubagentAssembly`**——消灭 union 类型 + isinstance 派发（评审 I12）。`assemble_sub` 默认 raise（react sub 由 materialize 直接构建，不经策略）。
5. **stages 2/3 为供给契约（supply-only）**——`SupplyInfra` frozen dataclass（`pool_assembly_ctx` + `pool` + `state_schema_compiler`）取代 `dict[str, Any]` 载体（评审 C2）；缺失供给直接 raise。Stage 2 的自建分支（FW broker + trigger 解析）删除——生产不可达（I8 修复后供给恒预填），且其 14-key 字典形状与生产载体不符，正是评审谴责的"stub 掩蔽现实"模式。特殊 agent 触发解析随 v2 per-invocation 契约回归。

**理由**：生产启动路径端到端验证（`tests/integration/test_production_boot_e2e.py`）证明：供给模式下管道的实际职责是"复制供给 infra + 调用 `strategy.assemble_main`"，而 subagent 的 per-invocation 数据无法流经 per-pool ctx。本次修订让生产行为与架构声明一致，消灭缺少 `.pipeline` 的 placeholder 输出与双路径（I8：测试走 legacy 分支、生产走管道分支）。

**实现依据**：`src/modex_agent/plugins/assembly/pipeline.py`（3-stage + sub 类型 raise）、`multi_agent/template.py`（直接构建 + `assemble_sub` 派发）、`multi_agent/execution_strategy.py`（ABC 拆分 + `SubagentAssembly`）、`plugins/assembly/context.py`（`SupplyInfra`）、`bot/service/pool/factory.py`（supply 必传 + service 级 registry 单例线程化）。

### Errata-6: 统一消费做实 (2026-08-19 实现期修订, HEAD=4482cac4)

本 errata 记录统一消费波次(WE/WA/WB/WF/WD)对 Errata-5 的 5 处做实修订。Wave B 做实共享核心后,Errata-5 移除 Stage 4 的理由(产出被丢弃)不再成立,12 槽位消费矩阵从声明变为生产可达。

#### (a) Stage 4 做实 + native_main 恢复 1→2→3→4

**原文**(Errata-5 第 1 条 + §6.3 Stage 子集表):Stage 4 对 `native_main` 产出被丢弃(真实 main 由 `strategy.assemble_main` 构建),从 `native_main` 子集移除;`native_main` 走 stages 1→2→3。

**修订后**:`AgentAssembleStage.process` 调用 `assemble_native_agent`(见 (b))产出权威 `NativeAssemblyResult`。`create_pool` 消费 `result.instance` 作为 main 权威 agent,不再经 `_register_main_agent` 重复创建。`native_main` stage 子集恢复 **1→2→3→4**;`_register_main_agent` 与 `_AssembledAgentStub` 删除(代码评审 I4 永久关闭)。`external_main` 仍走 1→2→3(strategy 拥有 external 构建)。

**理由**:Errata-5 的移除是"产出去除"的临时收敛,保留了 `_register_main_agent` 双创建路径(I4)与 main/sub 双装配代码。Wave B 提取共享核心后,Stage 4 产出成为 main 权威,双创建路径消灭的前提是 Stage 4 做实。

#### (b) assemble_native_agent 签名 + NativeAssemblyInputs

**原文**(§6.6 SpecBuilder + Errata-5 第 2 条):subagent 由 `AgentTemplate.materialize` 直接构建(恢复 3d85e7ee 生产验证逻辑);main 由 `strategy.assemble_main` 构建。两路径各自内联装配。

**修订后**:两路径收敛到单一核心 `assemble_native_agent`(`src/modex_agent/plugins/assembly/native_core.py`):

```python
# 普通类 (非 @dataclass(frozen=True));字段经 __init__ 赋值,与 native_core.py 一致
class NativeAssemblyInputs:
    agent_factory: AgentFactory
    broker: MessageBroker
    llm_defaults: LlmDefaults            # frozen Pydantic BaseModel (extra="forbid", arbitrary_types_allowed=True); model/temp/max_tokens/effort/model_info
    pool: AgentPool | None = None
    context_manager: ContextManager | None = None
    memory_system: MemorySystem | None = None
    memory_config: MemoryConfig | None = None
    llm_provider: LLMProvider | None = None    # 预填则绕过槽位解析
    tool_manager: InMemoryToolManager | None = None
    skill_manager: SkillManager | None = None
    system_prompt: str | None = None
    system_prompt_provider: SystemPromptProvider | None = None
    output_adapter: OutputAdapter | None = None
    tree: SessionTreeManager | None = None
    resolver: WorkspacePathResolver | None = None
    root_provider: WorkspaceRootProvider | None = None
    todo_store: TodoStore | None = None
    mcp_registry: McpConnectionRegistry | None = None
    session_registry: SessionRegistry | None = None
    notification_service: AgentNotificationService | None = None
    safety: RuntimeSafetyPolicy | None = None
    memory_store_registry: MemoryStoreRegistry | None = None
    project_dir: Path | None = None
    data_dir: Path | None = None
    control_origin: str = ""
    emitter_factory: Callable[[str], ContentEmitter[AgentEvent]] | None = None
    on_subagent_created: Callable[[str, str], Awaitable[None]] | None = None
    extra_hooks: tuple[Hook, ...] = ()
    execution_strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT

# 普通类 (非 @dataclass(frozen=True));字段经 __init__ 赋值,与 native_core.py 一致
class NativeAssemblyResult:
    descriptor: AgentDescriptor
    instance: AgentInstance
    tool_manager: InMemoryToolManager
    llm_provider: LLMProvider
    system_prompt_provider: SystemPromptProvider
    system_prompt: str
    memory_providers: tuple[MemoryProvider, ...]
    skill_sources: tuple[SkillSource, ...]
    memory_config: MemoryConfig
    hook_runner: HookRunner

async def assemble_native_agent(
    spec: AssemblySpec,
    registry: ComponentRegistry,
    inputs: NativeAssemblyInputs,
    *,
    ctx: AssemblyContext,
    parent_session: str | None = None,
    invocation_id: str | None = None,
) -> NativeAssemblyResult:
```

核心职责 = 7 槽解析(TOOL/LLM_PROVIDER/SYSTEM_PROMPT_PROVIDER/MEMORY_PROVIDER/SKILL_SOURCE/MEMORY_SYSTEM_MODIFIER/HOOK)+ memory merge(单 modifier 限制)+ hook 派发(react/memory 双 runner,`applies_to` 过滤)+ `wrap_standard_tools`(root_provider)+ descriptor 构造 + `agent_factory.create_agent` + hook 落地 + `pool.register_resident` + FORK/AutoSend(`parent_session` 门控)。

> **Errata-8 (2026-08-20)**: 上述 `memory_providers`/`skill_sources` 字段与 7 槽解析收敛为 5 槽 (TOOL/LLM_PROVIDER/SYSTEM_PROMPT_PROVIDER/MEMORY_SYSTEM/HOOK), memory merge 单 fallback 路径 — 见 §19 Errata-8 (a)/(b)。

**两条进入时机**:
- **main-pipeline**:`AgentAssembleStage`(Stage 4)从 `PoolAssemblyContext` + `StrategyAssembly` 侧产物组装 `NativeAssemblyInputs`,调用核心;`result.instance` 成为 main 权威产出。
- **sub-materialize**:`AgentTemplate.materialize` 从 `AgentMaterializeDeps` 组装 inputs,调用核心(传入 `parent_session` + `invocation_id`);instance 注册到 pool。

**不进核心**(留在入口的 per-type 接线):main 的 `_wire_main_pipeline`(governance/approval/services/interceptor/command_processor)、experience hook wiring、cassette wrap、emitter 工厂包装。这些是 main/sub 差异本身。

**理由**:main 与 sub 的组件解析逻辑(7 槽 + memory merge + hook 派发)完全同构,差异只在 per-type 接线与 per-invocation 参数。提取核心消灭双装配代码,`parent_session`/`invocation_id` 经关键字参数传入,适配 sub 的 per-invocation 契约而不污染 per-pool `AssemblyContext`。

#### (c) 12 槽位消费矩阵做实

> **Errata-8 (2026-08-20)**: 本矩阵被 10 槽矩阵取代 (3 槽删除; LLM 解析单一机制) — 见 §19 Errata-8 (a)/(e) 与 `HANDOFF.md` 更新后的槽位矩阵。

**原文**(§4.3 12 槽位表 + §6.6 槽位配置层级表):12 槽位声明齐全,但 INTERCEPTOR/COMMAND_HANDLER/INPUT_STAGE 标注为"pool 级/workspace 级,由 stage 解析",生产消费路径未做实(MEMORY_PROVIDER/SKILL_SOURCE 标注"消费随 roster 引用")。

**修订后**:12 槽位全部生产可达,消费矩阵如下:

| 槽位 | 注册方 | 消费方 | 状态 |
|---|---|---|---|
| `EXECUTION_STRATEGY` | `BotStrategiesPlugin`(react/external,`SimpleFactory` 包装) | `PoolAssembleStage` 派生 `ExecutionStrategyRegistry`;gating 消费 | 生产消费 |
| `TOOL` | FW `defaults/tools.py` + `BotStrategiesPlugin`(bash)+ `BotHooksPlugin`(experience) | `assemble_native_agent`(`_resolve_multi`) | 生产消费 |
| `HOOK` | FW `defaults/hooks.py` + `BotHooksPlugin` | `assemble_native_agent`(`_dispatch_hooks`,`applies_to` 过滤 + react/memory 双 runner) | 生产消费 |
| `LLM_PROVIDER` | FW `defaults/llm.py`(default)+ `BotStrategiesPlugin`(bot_default) | `assemble_native_agent`(`_resolve_single`;`inputs.llm_provider` 预填可绕过) | 生产消费 |
| `SYSTEM_PROMPT_PROVIDER` | FW `defaults/prompt.py`(file_prompt) | `assemble_native_agent`(`_resolve_single`,project_dir 相对路径解析) | 生产消费 |
| `MEMORY_SYSTEM_MODIFIER` | FW `defaults/memory.py`(main_agent_memory/subagent_memory) | `assemble_native_agent`(`_merge_memory`,单 modifier 限制) | 生产消费 |
| `COMMAND_HANDLER` | FW `defaults/commands.py` + bot | `pipeline_wiring` 构造 per-pool `SlashCommandProcessor`(roster `commands` 缺省 = 服务默认集) | 生产消费 |
| `INPUT_STAGE` | `IMInputStagesPlugin`(im_input/webui_input 等聚合工厂) | `build_im_pipeline`/`build_webui_pipeline` 按槽名解析(骨架顺序代码定,见 (d)) | 生产消费 |
| `MEMORY_PROVIDER` | 插件按需注册 | `assemble_native_agent`(`_resolve_multi`);消费随 roster 引用 | 注册可用 |
| `SKILL_SOURCE` | 插件按需注册 | `assemble_native_agent`(`_resolve_multi`);消费随 roster 引用 | 注册可用 |
| `INTERCEPTOR` | FW `defaults/interceptors.py`(tool_timeout)+ bot | `pipeline_wiring` per-pool 链(见 (e)) | per-pool 链 |
| `DATA_NAMESPACE` | 插件按需注册 | `GraphSpecCompiler` state_schema 类型解析 + KVStore `TypedBundle` | graph compiler |

**理由**:SPEC §1 完成判据("工具/钩子/模型/提示词/拦截器/命令全部可注册,重启生效")要求 12 槽位生产可达。T0.1/T0.2/T0.3 红测试转绿(自定义 tool/hook/strategy 经 roster 引用到达生产装配产物)是矩阵做实的机器证明。

#### (d) INPUT_STAGE 骨架顺序不可配语义

**原文**(§6.6 槽位配置层级表 INPUT_STAGE 行):"workspace 级,`InfraAssembleStage` 从 workspace 配置解析"。

**修订后**:`build_im_pipeline`/`build_webui_pipeline` 的 stage 列表 = **骨架顺序(代码定,顺序不可配)** + 按 `INPUT_STAGE` 槽名解析。默认名列表 = 现列表(`im_input`/`webui_input` 聚合工厂已在 `IMInputStagesPlugin` 注册)。自定义 stage 名按代码定义的扩展点确定性排序插入,不能重排内置骨架。空 `INPUT_STAGE` registry 在 `set_channel` 处 raise `ComponentNotFoundError`,不回退直接构造。`BotService` 持有 `self._service_assembly_ctx`(registry + home workspace_ctx + stack registry)作为服务级最小装配 ctx,两个 pipeline 构建函数签名加 `registry: ComponentRegistry, ctx: AssemblyContext`。

**理由**:input pipeline 的 stage 顺序是控制流不变量(`/cd`/`/stop` 必须在 resolve_pool 之前,persist 必须在 enqueue 之前)。允许 roster 重排会破坏 claim/consume 语义。槽位化只开放"插入什么 stage",不开放"stage 在哪"。

#### (e) per-pool interceptor 链 + COMMAND_HANDLER 槽位化

**原文**(§5.1 pool.yml 示例 + §6.6 槽位配置层级表):`interceptors`/`commands` 为 pool 级字段,`pipeline_wiring` 解析。

**修订后**:
- **interceptors**:pool.yml `interceptors` 名单 → `SpecBuilder.from_roster` 投影到 `AssemblySpec.interceptors` → `pipeline_wiring` 解析。无名单 = 共享 `InterceptorChain` 引用(行为不变,跨池不污染);有名单 = 克隆共享链 + 追加 roster 指定的 interceptor(经 `INTERCEPTOR` 槽 `factory.create` 解析)。`InterceptorChain` 组合性核实通过(F0:克隆 + append 不破坏既有 interceptors)。
- **commands**:`CommandDispatchStage(handlers=...)` 的 handlers 改为从 `COMMAND_HANDLER` 槽按名解析。roster `commands` 缺省 = 服务默认 `SlashCommandProcessor`;配置后以 roster 指定的 handler 集构造 per-pool processor。`continue` handler 注册为 FW 工厂。IM 的 `/cd`/`/stop` input-stage 通道不动(双通道现状:input pipeline stage 与 slash command processor 各管各的)。

**理由**:interceptor 链是 pool 级隔离边界(不同 pool 不同拦截策略);per-pool 克隆保证配置不污染他池。COMMAND_HANDLER 槽位化消灭硬编码 handler 列表,roster 可点名自定义命令。两者从"死字段"(`MainAgentSpec` 携带但 `pipeline_wiring` 不消费)转为生产消费。

**实现依据**:`plugins/assembly/native_core.py`(核心)、`plugins/assembly/stages/agent_assemble.py`(Stage 4 调核心)、`multi_agent/template.py`(materialize 调核心)、`bot/service/pool/factory.py`(消费 Stage 4 产出 + 删 `_register_main_agent`)、`bot/service/pool/pipeline_wiring.py`(interceptor 链 + COMMAND_HANDLER 槽位化)、`bot/input_pipeline/assembly.py` + `examples/bot_project/plugins/im_input_stages.py`(INPUT_STAGE 槽位化)、`examples/bot_project/plugins/bot_strategies.py` + `bot_hooks.py`(槽位注册)。

**影响**:§6.3 Stage 子集表 `native_main` 恢复 1→2→3→4(覆盖 Errata-5 的 1→2→3);§6.6 槽位配置层级表 INTERCEPTOR/COMMAND_HANDLER/INPUT_STAGE 行从"未做实"改为"生产消费";§4.3 12 槽位全部生产可达。subagent 仍不经管道(Errata-5 第 2 条保留),但经同一 `assemble_native_agent` 核心,核心是统一点,管道是 main 编排器。

### Errata-7: MEMORY_SYSTEM 槽位 (2026-08-19 实现期修订, HEAD=448f0bb6)

> **Errata-8 (2026-08-20)**: `MEMORY_SYSTEM` 保留并补完 (见 §19 Errata-8 (c)); 本 errata 声明的 13 槽位集被 10 槽集取代, (a)/(d) 中与 `MEMORY_PROVIDER`/`MEMORY_SYSTEM_MODIFIER` 的粒度关系描述随之失效。

本 errata 记录 cut C 对 §4.3 槽位集的第 13 槽追加。`MEMORY_SYSTEM` 是装配系统最大的替换粒度——一个槽位产出 `ContextManager`,即 agent 运行时实际消费的上下文接口。槽位集从 12 扩展到 13。

#### (a) 槽位定义

`ComponentSlot.MEMORY_SYSTEM = "memory_system"` — 工厂产出 `ContextManager` 实例(`src/modex_agent/plugins/abc.py`)。这是槽位集中唯一产出 `ContextManager` 的槽位;`MEMORY_PROVIDER` 产出 `MemoryProvider`(被默认 `MemorySystemContextManager` 消费),`MEMORY_SYSTEM_MODIFIER` 产出修改 `MemoryConfig` 的 modifier——两者都在 `ContextManager` 之下操作。`MEMORY_SYSTEM` 直接替换 `ContextManager` 本身。

`AssemblySpec` 新增字段(`src/modex_agent/plugins/assembly/spec.py`):
```python
memory_system: str | None = None           # MEMORY_SYSTEM 槽位组件名(可选)
memory_system_config: dict[str, Any] = Field(default_factory=dict)
```

`ComponentSlot` 枚举从 12 成员扩展到 13 成员(`MEMORY_SYSTEM` 插入在 `MEMORY_SYSTEM_MODIFIER` 之后)。枚举权威性约束更新:不再禁止"add"——additions require a SPEC errata(`MEMORY_SYSTEM` 经 Errata-7 加入)。

#### (b) 消费路径

`native_core.assemble_native_agent()`(`src/modex_agent/plugins/assembly/native_core.py:268-279`)在 LLM provider 解析后、system prompt 解析前,检查 `spec.memory_system`:

```python
# Resolve MEMORY_SYSTEM slot if spec references it (SPEC Errata-7).
if spec.memory_system is not None:
    ctx_with_provider = _replace(ctx, llm_provider=provider)
    mem_factory = registry.resolve(ComponentSlot.MEMORY_SYSTEM, spec.memory_system)
    mem_config = mem_factory.config_model.model_validate(spec.memory_system_config)
    context_manager: ContextManager | None = await mem_factory.create(
        mem_config, ctx_with_provider
    )
else:
    context_manager = inputs.context_manager
```

- **`spec.memory_system` 已设**:resolve 槽位 → `factory.create(config, ctx_with_llm_provider)` → 结果作为 `context_manager`,替代 `inputs.context_manager`。
- **`spec.memory_system` 为 None**:现有 `inputs.context_manager` 路径不变(默认 `MemorySystemContextManager` 由调用方经 `NativeAssemblyInputs` 传入)。

main(Stage 4)与 sub(`AgentTemplate.materialize`)都经同一 `assemble_native_agent` 核心,因此 `MEMORY_SYSTEM` 对两者均生效——与 Errata-6 (b) 的统一核心契约一致。

#### (c) AssemblyContext.llm_provider 新字段

`AssemblyContext`(`src/modex_agent/plugins/assembly/context.py:121`)新增 `llm_provider: LLMProvider | None = None` 字段。`MEMORY_SYSTEM` 工厂经 `ctx_with_llm_provider`(用 `dataclasses.replace` 从 ctx 派生)访问已解析的 LLM provider——工厂需要 LLM provider 来构建自定义 `ContextManager`(例如自定义 prompt 组装可能需要模型信息)。其他槽位的工厂不受影响(`llm_provider` 为 None 时走原 ctx)。

`AssemblyContext` 是 frozen dataclass;`_replace` 派生新实例不违反不可变性。`llm_provider` 只在 `MEMORY_SYSTEM` 解析分支中被填充,其他路径保持 None。

#### (d) 设计理由:替换 ContextManager 而非 MemorySystem

一个槽位产出 `ContextManager`(agent 运行时的实际消费接口),而非 `MemorySystem`——因为 `ContextManager` 包装 `MemorySystem` + `injection_policy` + `experience` + prompt assembly。替换 `ContextManager` = 替换 agent 看到的一切(历史、治理、提示词组装、经验注入)。替换 `MemorySystem` 只换存储层,agent 仍经框架默认 `ContextManager` 组装 prompt——粒度不够。

`MEMORY_SYSTEM` 与 `MEMORY_PROVIDER`/`MEMORY_SYSTEM_MODIFIER` 的关系:
- `MEMORY_PROVIDER`:在默认 `ContextManager` 内注册额外 `MemoryProvider`(增量,不替换框架组装)
- `MEMORY_SYSTEM_MODIFIER`:修改 `MemoryConfig`(参数级,不替换 `ContextManager`)
- `MEMORY_SYSTEM`:整体替换 `ContextManager`(用户完全拥有 prompt 组装 + 历史 + 治理 + 所有 memory 行为)

三者是递进替换粒度:modifier(参数)→ provider(增量内容)→ system(整体接口)。用户按需选择粒度。

#### (e) 无内置工厂

`DefaultPlugin`(`src/modex_agent/plugins/defaults.py`)不在 `MEMORY_SYSTEM` 槽注册任何工厂——槽位为空。理由(原则 5:骨架固定,框架不硬编码 bot-specific memory wiring):框架默认 `MemorySystemContextManager` 已由 `NativeAssemblyInputs.context_manager` 路径提供,无需槽位化默认值。用户要替换 `ContextManager` 时注册自定义工厂并在 roster 引用:

```yaml
# pool.yml
memory_system: my_custom_context_manager
memory_system_config:
  custom_param: value
```

未注册工厂而 roster 引用 `memory_system` 名 → 装配时 `registry.resolve` raise `ComponentNotFoundError`(与所有其他槽位的 late-binding 失败行为一致,§6.8)。

#### (f) E2E 机器证明

`tests/integration/test_memory_system_slot.py` — `test_memory_system_plugin_replaces_main_and_subagent_context_manager`:
1. 注册 `_ProbeMemoryPlugin`(probe factory 产出 `_ProbeContextManager`,system_prompt = `PROBE_MEMORY_ACTIVE`)
2. roster 引用 `memory_system: "probe_memory"`(pool.yml + agent template)
3. boot pools → 断言 main agent `context_manager` is `_ProbeContextManager`(line 219)
4. materialize subagent → 断言 subagent `context_manager` is `_ProbeContextManager`(line 230)
5. 两者 `load()` 返回 `system_prompt == PROBE_MEMORY_ACTIVE`——证明 probe ContextManager 到达生产装配产物,非默认 `MemorySystemContextManager`

红绿轨迹:probe factory 注册前 roster 引用 `probe_memory` → `ComponentNotFoundError`(红);注册后 → main + sub 均使用 probe ContextManager(绿)。

**影响**:§4.3 槽位表从 12 扩展到 13(新增 `MEMORY_SYSTEM` 行);§4.5 `PluginRegistrationContext` 的 `register_*` 方法数从 12 到 13(新增 `register_memory_system`);`ComponentSlot` 枚举权威性约束更新(additions require errata);`AssemblySpec` 新增 `memory_system`/`memory_system_config` 字段;`AssemblyContext` 新增 `llm_provider` 字段。Errata-6 (c) 的消费矩阵新增 `MEMORY_SYSTEM` 行(production-consumed via native_core resolve)。

### Errata-8: 槽位理性化 — 槽位集 13→10 + 收敛收口 (2026-08-20 实现期修订, HEAD=d96497d9)

本 errata 记录槽位理性化波次 (W0-W6) 的落盘: 执行台账 `.omo/plans/slot-rationalization-steps.md`, 设计 `plan-slot-rationalization.md`, 波次 commit 链见 `HANDOFF.md` "Slot Rationalization Waves"。核心修订: 槽位集从 13 收敛到 10 (三槽删除), LLM provider 解析收敛为单一机制, SYSTEM_PROMPT_PROVIDER 与 MEMORY_SYSTEM 获得 roster 面。本 errata 取代 Errata-6 (c) 的 12 槽位消费矩阵、Errata-7 的 13 槽位扩展声明、以及 ADR-0041 的 D-A5 分层表述 (见 (e))。

#### (a) 槽位集 13→10: 权威清单 + 移除理由 + 迁移指引

**权威槽位清单 (10 成员, `src/modex_agent/plugins/abc.py` `ComponentSlot`)**:

`TOOL`, `HOOK`, `MEMORY_SYSTEM`, `LLM_PROVIDER`, `SYSTEM_PROMPT_PROVIDER`, `INTERCEPTOR`, `COMMAND_HANDLER`, `EXECUTION_STRATEGY`, `INPUT_STAGE`, `DATA_NAMESPACE`

**移除三项** (2026-08-19 三评审员审计: 三槽零生产者/零消费者/零 YAML 引用):

- `MEMORY_PROVIDER` — 细粒度需求由 `MEMORY_SYSTEM` 粒度 + memory 包自有扩展点覆盖 (见 (b))。`MemoryProvider` ABC 迁至 `modex_agent/memory/core/provider.py` (修复 memory→plugins 依赖倒置; `tests/architecture/test_memory_package_isolation.py` AST 级守卫)。recorder fan-out (`MemoryAppendRecorder(providers=...)`) 是独立能力, 原样保留。
- `SKILL_SOURCE` — 零表面; 磁盘 skill 体系 (`skills/<pool>/<agent>/` + WebUI Skills 面) 覆盖全部真实用法; SkillManager 组合 seam **决定不开** (无需求, 远程技能源出现时再议)。
- `MEMORY_SYSTEM_MODIFIER` — 生产恒走 `inputs.memory_config` fallback, 工厂注册无人点名 (工厂注册与函数直调双路径债); preset 迁至 `modex_agent/memory/presets.py` 为**纯函数** (无工厂间接层); 参数级需求由 `memory:` MemoryOverrides YAML 覆盖。

**迁移指引 (死配置有声化)**:

- `memory.providers` / `memory.modifiers` 子键 → `_build_memory_overrides` 发 **warning** (`config/spec_builder.py`; 死键从静默变有声)。
- 模板携带 `skills:` → `SubagentSpec` (`extra="forbid"`) **硬拒绝** (ValidationError); PoolStore 层该模板被跳过 (warning 日志); pool REST API `PUT /api/pools/<name>` 返回 **400** + `subagents.<n>.skills` 字段错误。技能分配走磁盘 `skills/<pool>/<agent>/` (WebUI Skills 标签页)。

#### (b) memory 两层定位 (终局)

- `memory:` (MemoryOverrides: 嵌套 `session.max_context_tokens` + 平铺 `archive_enabled` / `core_enabled`, 与 §5.1 示例同形) — **参数级**覆盖, 作用于 preset 产物之上 (override 以 `model_copy(update={...})` 应用, 保留子配置内部值)。
- `memory_system:` (Errata-7 槽位) — **接口级**整体替换 `ContextManager`。
- 程序化扩展走 memory 包自有 seam (`SystemPromptProvider` pipeline / `MemorySystem` 子类化), **不占槽位**。两层 YAML + 包内 seam 覆盖全部需求粒度 — 这是移除三个 memory 相关槽位的理论依据。

#### (c) MEMORY_SYSTEM 补完 (Errata-7 收尾)

- **config roster 面**: `MainAgentSpec`/`SubagentSpec` 新增 `memory_system_config: dict[str, Any]` (默认 `{}`); `SpecBuilder.from_roster` 真投影 (原硬编码 `{}`); PoolStore 以 preserved-key 形态 round-trip (与 `memory_system` 同语义); `config/AGENTS.md` roster 表有行。
- **孤儿 hook 报错**: `spec.memory_system is not None` 时, `_dispatch_hooks` 遇 memory-runner hook → `ValueError`, 文案**点名 hook 名**并指向补救 ("register the hook inside the custom ContextManager instead")。此前 memory hook 静默挂死在被孤儿化的默认 memory system 上 (审计 I2)。
- **生态损失清单 (如实声明)**: 替换 `ContextManager` 即放弃 — experience 注入 (`ExperienceProvider` 骑默认 CM 的 prompt pipeline)、BIZ cleanup hooks (挂 `pool_data.context_manager.memory_system`)、dream 触发。自定义 CM 的 owner 必须自担等价物。

#### (d) SYSTEM_PROMPT_PROVIDER 选择器语法

`system_prompt_provider` (+ `system_prompt_provider_config`) 在 `_expand_system_prompt` 的优先级链:

1. **显式 provider 名** → 名字 + verbatim config 投影, 语法糖不适用
2. **`system_prompt` 糖** → `file_prompt` + `{path}`
3. **`prompt_name` / agent 名约定** → `file_prompt` + `agents/<name>.md`

第三方注册自定义 prompt provider 后可被 YAML 点名; `file_prompt` 仍是缺省。此前槽机制活但出口恒为 `file_prompt` (无选择器, 审计发现)。

#### (e) LLM provider 解析收敛 (取代 ADR-0041 D-A5 分层表述)

- **单一机制**: `AgentFactory.create_agent(..., llm_provider: LLMProvider | None = None)` — per-agent override > factory default > LiteLLM。名字→实例解析**每 agent 恰好一次**, 发生在生产入口: main 在 `create_pool` (pipeline 之前), sub 在 deps 组装 (`factory.py` `_resolve_llm_slot`)。
- **删除**: `StrategyAssembly.provider` 字段 (原 main 路径 strategy 内部解析的产物) 与 `bot/service/builders.py` 的 `_build_llm_provider` fallback (W0.4 终验: 生产不可达的重复路径)。cassette 包装改为 build_native_inputs 侧以同一 recorder 执行 (策略只决定 enablement + 包装 tool_manager + 返回 recorder)。
- **FW `default` 工厂边界**: 只管 FW 单 provider schema; BIZ model.yml 多 provider 格式解析归 BIZ `bot_default` 工厂 (原 FW 桥 `_llm_config_from_multi_provider` 删除, 审计 I5)。
- **native_core `_resolve_single(LLM_PROVIDER)`**: 降格为文档化通用 fallback (`inputs.llm_provider=None` 的直调方), 不再是生产双路径的一支。
- **core.py `_build_default_provider`**: 独立合法路径 — bot 全局 provider (memory 摘要器 + experience review 共用), model.yml 缺失时刻意返回 None; **不属于本收敛** (W0.4 终验)。
- **W4 翻转决策: 未触发** — bot 套件全绿, subagent 保持 `bot_default` 默认 (获得 per-turn 模型切换), HANDOFF leftover #1 关闭。

#### (f) DATA_NAMESPACE 语义纯化

- **trigger 注册删除**: `plugins/defaults/triggers.py` 整文件删除 (4 个 TriggerFactory 的死注册 — 消费分支已随 Errata-5 删除); `TriggerOverrides` 类 + `PoolRoster.triggers` 字段一并删除 (零 loader 写入/零读取/零 YAML 面)。注意: `DefaultMemorySystem.core_memory_consolidator` 等**内存系统侧**同名组件是活能力, 不受影响。
- **槽语义**: DefaultPlugin 留空 — 插件按需注册 (与 MEMORY_SYSTEM 同语义)。DATA_NAMESPACE 只做类型登记 (graph state_schema 类型解析 + KVStore `TypedBundle`)。
- **接线**: `GraphOrchestrator` 接受可选注入的 `state_schema_compiler: Callable[[dict[str, FieldSpec]], type[GraphState]] | None` (与既有 seam 同类型); BIZ graph wiring 传 `build_state_schema_compiler(service._component_registry)` (`resources.py`, 服务级 registry 单例)。E2E 机器证明: `tests/integration/test_graph_data_namespace_e2e.py` (自定义 DATA_NAMESPACE 插件类型经 registry 解析编译 state_schema)。

#### (g) 如实声明 (已知限制, 记录而非隐藏)

- **INPUT_STAGE**: stage 顺序代码定义 (控制流不变量), 自定义 stage 全局自动插入 (确定性排序进代码定义的扩展点) — 无 per-pool YAML 选择 (维持 Errata-6 (d))。
- **EXECUTION_STRATEGY sub 限制**: sub 路径的自定义 strategy 名被 cast — 仅 `external` 特判 (`template.py` materialize 早分派); native sub 由 `AgentTemplate.materialize` 直接构建, 不经策略。
- **InfraAssembleStage 组装产物已删除（final-review 收敛）**: `builder.infra.state_schema_compiler`（stage 2 填充）零下游消费者 — BIZ 从服务级 registry 直建同一 callable 接线（W6）；final-review 已删除 stage 内填充与 `SupplyInfra.state_schema_compiler` 字段，`build_state_schema_compiler` 收敛为单一生产构建点（BIZ 接线，`plan-slot-rationalization.md` §5.1 INC-4 处置完成）。
- **`_create_state` 只处理 `state_class`**: `state_schema` spec 经编译 (create_instance/PUT 校验通过) 但 `run_instance` 的 state 构造路径不支持 — W6 E2E 驻留编译路径。已知 gap, 如实记录。

#### (h) 门禁机器与权威数字

`scripts/verify_slot_gates.py` — 16 个机械门禁 (移除台账 L1-L6 + 收敛台账 C1-C2 的 grep 固化), 永久回归锚; W7 收口后 16/16 绿。

| 项 | 权威值 |
|---|---|
| `ComponentSlot` 成员 | **10** |
| DefaultPlugin populated | **6** {TOOL, HOOK, LLM_PROVIDER, SYSTEM_PROMPT_PROVIDER, INTERCEPTOR, COMMAND_HANDLER} |
| DefaultPlugin empty | **4** {EXECUTION_STRATEGY, INPUT_STAGE, MEMORY_SYSTEM, DATA_NAMESPACE} |
| native_core 解析 per-agent 槽位 | **5** {TOOL, HOOK, LLM_PROVIDER, SYSTEM_PROMPT_PROVIDER, MEMORY_SYSTEM} |
| `PluginRegistrationContext` register_* 方法 | **10** |
| 同源重名注册 | **ValueError** (跨源 first-seen-wins + warning) |

红→绿锚点: T-P1 (prompt 选择器) / T-P2 (sub LLM 槽解析) / T-P3 (memory_system_config) / T-P4 (孤儿 hook ValueError) — `tests/integration/test_slot_rationalization_e2e.py`; 图 E2E — `test_graph_data_namespace_e2e.py`。

**实现依据**: `src/modex_agent/plugins/` (abc/loader/registry/defaults/assembly/native_core), `src/modex_agent/memory/presets.py` + `memory/core/provider.py`, `src/modex_agent/config/spec_builder.py`, `src/modex_agent/multi_agent/pool_config/` (specs/store/roster), `examples/bot_project/bot/service/pool/factory.py`, `src/modex_agent/orchestration/graph_orchestrator.py`, `scripts/verify_slot_gates.py`。

**影响**: §4.3 槽位表、§5.1 示例、§6.6 字段/层级表/默认组件包、Errata-6 (b)(c)、Errata-7 (a) 各加 "superseded by Errata-8" 指针 (原文保留, 活文档指针惯例); ADR-0041 追加 Slot Rationalization addendum; HANDOFF 追加波次记录与 10 槽矩阵。
