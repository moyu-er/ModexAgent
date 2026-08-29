# SPEC：Capability 能力包体系（插件内推导 + 动态启用）

**状态**：核心决议定稿（2026-08-27 审议收敛，ADR-0047）；实施未开始
**前置体系**：ADR-0041 / `docs/design/scope-converge/SPEC.md`（10 槽 ComponentRegistry + 装配管道）；ADR-0042/0043 / `docs/design/scope-assembly/SPEC.md`（scope 声明树 + 统一装配，已实施）
**日期**：2026-08-27（v2——整合启用解析决议与收敛纪律，取代 v1 草案）

---

## 0. 一句话

把"一组绑在一起用的组件"（工具 + hook + 提示段 + 供给）从编译器/装配代码里的硬编码特例，升格为一等插件单元 **Capability（能力包）**：启用与否由能力包自己的谓词扫描声明动态判定 + 声明显式覆盖，生效形态由能力包在编译期读声明树推导，装配期经既有槽位解析接线——**框架不认识任何具体能力，也不硬编码任何默认能力集**。

---

## 1. 背景与动机

### 1.1 现状：能力事实上存在，机制上有五种并存的绑定模式

scope-assembly 落地后，"agent 怎么组装"已声明化，但"一组组件绑在一起生效"仍是编译器/装配代码里的特例。代码级盘点（2026-08-27）：

| # | 绑定模式 | 代表 | 绑定时机 | 拆除手段 |
|---|---|---|---|---|
| A | 结构推导 | `task` / `send_to_agent` / `send_to_peer`（`compiler.py _derived_entries`） | 编译期（树） | 改树（重启生效） |
| B | 名册驱动绑定 | experience 全家（工具名存活 ⇒ 注入 hook + 启用 manager/curator，`compiler.py` 特例） | 编译期（名册） | `tools: [-experience]` 整包 / `hooks: [-experience_review]` 拆件 |
| C | Supplement 枚举 | todo / aci（O3 同名替换）/ ast_grep（`ToolSupplement` FW 闭集） | 编译期（名册合并） | 不声明即无 |
| D | 无条件注入 + 运行时 gate | `TodoContinuationHook`、`TodoAwareSystemPromptProvider`、`AgentCommunicationSystemPromptProvider`、tree-aware hooks | 装配期 | **大多无声明面排除手段** |
| E | 位置推导默认 | toolset / memory family / approval 资格 | 编译期 | 显式声明覆盖 |

同一个问题——"这个能力归不归这个 agent"——有三种答案（树结构 / 名册存活 / 运行时查工具注册）。心智模型不统一，第三方无法复用：想发布"工具 + hook + 提示段"成套能力的插件作者，只能在多个槽位分别注册组件、让用户逐个点名，包的内在绑定关系无法由插件自己表达（现有最远点是 `reference_collector`：单 hook + 内联 config）。

### 1.2 已有的正确雏形

experience 是体系内唯一"单开关绑多件"的完整先例：一个声明绑定工具 + review hook + manager + prompt 注入 + curator 五件套，绑定信号是编译后最终工具名册。本设计把这个特例**泛化为通用机制**，并把 todo / 通信工具两个历史形态收敛进来。

### 1.3 用户诉求（本设计的直接输入，2026-08-27 审议确认）

1. **插件化启用**：能力包由插件提供，声明引用启用——可用性（registry 注册）与启用（生效）分离。
2. **推导全部在插件内部**：包括 `task` 在内，能力"是否启用、启用后得到什么"的推导逻辑都是能力包自己的实现，不是编译器硬编码。编译器只提供协议，不认识任何具体能力。
3. **动态启用**：能力包不一定是"声明了才启用"的静态开关——它可以扫描读取声明（树位置、agent 自身字段）来动态判定自己是否适用。至少通信类能力（task 等）如此。
4. **收敛纪律**：现存胶水代码中硬编码配置/注入的内容全部不可接受——一切组件进入 agent 的路径必须声明化/配置化。

### 1.4 独立设计依据（为什么是这个形状）

- **可用性 ≠ 启用**：安装一个插件 ≠ 任何 agent 获得它的能力。信任边界在安装期（插件 Python 已在进程内运行），组合边界在声明期（谁获得什么）——两者分离后，"装了但没人用"是合法且常见的状态。
- **启用是谓词，不是开关**：把"是否适用"表达为能力包内的纯谓词（读声明视图），天然覆盖两种形态——静态包（恒返回 False，声明才启用）与动态包（读树/字段自判）。框架无需区分两种包。
- **声明树是编译期资产**：通信拓扑（谁可派谁）在编译期已知，树推导可以进入编译产物而非运行时判断——这是本框架的既有结构优势，能力包协议直接继承。

---

## 2. 设计原则

- **P1 编译纯函数不变**：能力包的启用判定（C0）与推导（C1/C2）在 `compile_scope` 内执行，必须是确定性纯函数——无 IO、无时钟、无 registry 状态读取。spec-hash 字节稳定性契约扩展覆盖能力解析与推导。
- **P2 名字解析单路径**：能力包贡献的工具名/hook 名流入**既有** `tools`/`hooks` 列表，经**既有** TOOL/HOOK 槽位解析。能力包不构成第二条组件解析路径（收敛规则 1）。
- **P3 编译期优先**：绑定判定收敛到编译期。模式 D（装配期无条件注入 + 运行时 gate）整体消灭——编译期已知生效能力集与树位置，运行时查工具注册属于历史形态。
- **P4 能力包自包含**：包内绑定关系（启用谓词、锚校验、降级模式、树推导）由能力包自己声明与实现；框架只提供协议与账单。框架不硬编码任何具体能力包名——**包括默认集**（默认能力面也是能力包自己声明的 auto-apply，见 C0）。
- **P5 供给随需求**：池级供给（store/服务）由池内 agent 的生效能力集合驱动聚合——无人启用则不构建（对齐 SupplyInfra 语义，消灭"永远在场但无人用"的暗供给）。
- **P6 逢二再抽象**：提示段只在固定锚点内排序（不开放任意位置）；池级供给配置面 v1 不开（今日无一真实需求）；profile 携带 capabilities 待第二需求。
- **P7 无声明面注入死亡（收敛纪律）**：任何组件进入 agent 的唯一路径是编译产物——本地声明 / profile / 位置默认 / 能力包贡献（含 auto-apply）。装配代码无条件注入组件是缺陷，W6 收敛清单波逐项消灭，机械门禁固化。

---

## 3. 核心模型

### 3.1 Capability：跨槽位的五相单元

```
注册表: CAPABILITY 槽 (全部可用能力包)
                │
     C0 启用解析 (编译期, per agent)
     ┌─────────┴──────────┐
     │ auto:   cap.applies(声明视图) → bool      ← 插件自己的判断 (默认 False)
     │ override: 声明块 capabilities: {…}        ← 显式覆盖, 双向永远赢
     └─────────┬──────────┘
                ▼ 生效集 = auto ∆ override
     C1 贡献 (编译期, 名册合并前): cap.contribute(tree, config) → 贡献
     合并: tools:/hooks: ± 语义作用于合并基 (既有机制)
     C2 绑定 (编译期, 名册合并后): cap.bind(tree, config, final) → 绑定
                ▼
     S 供给 (pool 装配期): cap.supply(pool_view) → 池级供给
     A 接线 (agent 装配期): cap.assemble(binding, ctx) → 提示段 + per-agent 接线
```

能力包是**第 11 个 ComponentSlot**（`CAPABILITY`）——槽位集 10→11，ADR-0047 即 scope-converge Errata-8 约定的"additions require a SPEC errata"之 errata。理由：既有 10 槽全部是"装配期单一组件工厂"，Capability 是"编译期启用判定 + 树推导 + 跨槽贡献"单元，任何既有槽都不适配。

与两份前置 SPEC 的关系：scope-converge 解决**槽位维度**（组件可注册），scope-assembly 解决**组合维度**（agent 可声明），本设计补**能力维度**（成套组件可打包、启用可动态、推导归插件）。

### 3.2 C0：启用解析（本设计的核心相）

```python
def applies(self, view: AgentDeclarationView) -> bool:
    """启用谓词——能力包扫描声明，判定自己是否适用于这个 agent。
    默认 False（纯 opt-in 包）。必须是纯函数。"""
```

- **生效集 = 自动面 ∆ 覆盖面**：
  - 自动面 = 所有 `applies(view) is True` 的能力包；
  - 覆盖面 = 声明块 `capabilities:` 的键集，值为 `false`（强制关闭，压掉 auto）或配置映射（强制启用 + 携带配置，压掉 auto 的默认配置）；
  - **显式覆盖双向永远赢**：`{subagents: false}` 关掉自动启用；`{todo: {max: 5}}` 启用一个不自动的包并带配置。
- **谓词输入 = AgentDeclarationView**（frozen 只读）：树位置（`is_root` / `parent` / `children` / `peers`）+ 该 agent **自身声明的字段**（toolset / tools / memory / mcp / use_terminal / execution_strategy 等的声明值）。**只读声明态**——永不读最终名册、永不读其它能力包的贡献（否则"启用←贡献←启用"循环，确定性破坏）。
- **external agent 在 C0 之前结构排除**：谓词不为 external agent 运行（external 策略不吃 native 组件面，ADR-0042 §4 第 2 轴不变）——因此 subagents 的 auto-apply 不会波及 external agent；V12 收窄为"仅显式声明 external + capabilities → boot error"。
- **框架默认能力集 = ∅**：v1 草案曾让框架硬编码"默认 `[subagents]`"——违反 P4（框架认识具体能力名），已废弃。零配置行为不变由 subagents 的谓词保证（见 §8.4），知识归位到能力包。
- **"这个 agent 有什么能力"的唯一权威答案是账单**：覆盖映射语义下不存在"声明列表即全集"的读法——生效集 = auto ∆ override，三态条目（auto / declared / vetoed）+ 来源全部经账单可查（§9）。

`subagents` 能力包的完整实现就是两段普通 Python：

```python
def applies(self, view) -> bool:
    # 该 agent 参与通信拓扑才启用：有子、或非根、或有 peer。
    # 无子无 peer 的根 = 今日行为（通信三件套一样不得）——零配置等价的锚点。
    return bool(view.children) or not view.is_root or bool(view.peers)

def contribute(self, tree, config):
    c = CapabilityContribution()
    if tree.children:                       # 有子 ⇒ task + 委派指南段
        c = c.with_tool("task").with_section("subagents.delegation", order=40)
    if not tree.is_root:                    # 非根 ⇒ 咨询工具 + 回流 hook + 咨询段
        c = c.with_tool("send_to_agent").with_hook("subagent_auto_send") \
             .with_section("subagents.consultation", order=41)
    if tree.is_root and tree.peers:         # 根且有 links ⇒ peer 工具 + peer 段
        c = c.with_tool("send_to_peer").with_section("subagents.peer", order=42)
    return c
```

"扫描读取配置来动态启用"的完整表达力：谓词可读任意自身声明字段——例如一个假想的 terminal-safety 包可以写 `return view.declared.use_terminal is True`。

### 3.3 其余四相（v1 已定，简述）

| 阶段 | 签名 | 时机 | 纯度 | 输出 |
|---|---|---|---|---|
| C1 贡献 | `contribute(tree, config) → CapabilityContribution` | 编译期，名册合并**前** | 纯 | 工具名 / O3 替换记账 / hook 名 / 提示段规格 |
| C2 绑定 | `bind(tree, config, final) → CapabilityBinding` | 编译期，名册合并**后** | 纯 | 存活的 section 集合 + 私有接线载荷；不一致 → `CapabilityError` |
| S 供给 | `supply(pool_view) → CapabilitySupply \| None` | pool 装配期 | 可读 workspace paths | 池级供给对象（store / 服务） |
| A 接线 | `assemble(binding, ctx) → CapabilityWiring` | agent 装配期 | 可读上下文链 | 有序 prompt providers + per-agent 接线对象 |

C1/C2 的拆分复刻 experience 的既有语义并泛化：贡献进合并基（所以 `tools: [-x]` 能 veto），绑定在合并后（所以门控与锚校验能看到最终名册——锚存活才带非工具件，`hooks: [-y]` 显式拆件可辨）。

### 3.4 两个高度的否决通道（显式区分）

| 通道 | 作用面 | 时机 | 语义 |
|---|---|---|---|
| `capabilities: {cap: false}` | **能力级**——整包关闭 | C0 | 包不参与编译，零贡献 |
| `tools: [-task]` / `hooks: [-y]` | **组件级**——外科拆件 | 名册合并 | 包仍启用，指定组件被 veto（C2 锚校验可能因此炸响） |

两通道正交：先能力级开关，后组件级拆件。`±` 前缀语法**仅属于组件级**——capabilities 是映射不是列表，不引入 `[-cap]` 列表语法（避免与 `tools:` 的替换/合并语义混淆）。

### 3.5 端到端样例（subagents，树推导 + 动态启用旗舰）

```yaml
# 声明（bot.yml 摘录）
coder:
  agents:
    orchestrator:
      capabilities: {todo: {}}              # subagents 未声明——但 auto-apply 已生效
      agents:
        explore:      {capabilities: {todo: {}}}     # 非根 ⇒ subagents 自动
        general:      {capabilities: {todo: {}, aci: {}}}
        lone_worker:  {capabilities: {subagents: false, todo: {}}}  # 显式关闭通信包
```

1. **C0**：`orchestrator` 有 children ⇒ subagents auto ✓ + todo declared ✓；`explore` 非根 ⇒ subagents auto ✓ + todo ✓；`lone_worker` 非根但显式 `{subagents: false}` ⇒ 覆盖压掉 auto。
2. **C1**：`orchestrator` → base 名册 + `task` + section `subagents.delegation`；`explore` → + `send_to_agent` + hook `subagent_auto_send` + section `subagents.consultation`；`general` 另有 aci 贡献替换记账 `edit ← aci_edit`。
3. **合并**：`tools: [-task]` 若出现则按既有 ± 语义从名册移除 `task`。
4. **C2**：`orchestrator` 有 children 但 `task` 不在最终名册 → `CapabilityError`（V6 语义，错误带 pool/agent/能力包名与修复指引）；锚存活则 binding 记录存活 sections。
5. **池装配 S**：池内生效能力包集合 = {subagents, todo, aci} → `subagents.supply()` 建 `AgentCommunicationService`（+ peer tree refs），`todo.supply()` 建 `TodoStore`（路径来自 WorkspaceContext.paths——工具配置零路径字段原则不变）。
6. **agent 装配 A**：`subagents.assemble()` 建 per-agent `CommunicationTargetStore`（main 从 children、subagent 从 parent——现有逻辑搬家）+ 三个 section provider；`todo.assemble()` 建 TaskDiscipline provider。
7. **运行**：`task` 工具经 TOOL 槽既有工厂解析（读 capability supply + per-agent store）——机制不变，所有权从编译器硬编码移到能力包。

---

## 4. Capability 协议（类型定义）

```python
class ComponentSlot(StrEnum):
    ...
    CAPABILITY = "capability"        # 第 11 槽（ADR-0047）


class Capability(ABC):
    """能力包——编译期启用判定 + 树推导 + 装配期接线的跨槽位单元。

    C0/C1/C2 必须是确定性纯函数（P1）：输入只有 AgentDeclarationView /
    config / 最终名册视图，输出 frozen。违反确定性会破坏 spec-hash
    字节稳定性（守卫测试覆盖）。
    """

    name: str                                   # 注册名 = 声明键
    config_model: ClassVar[type[BaseModel]]     # frozen, extra="forbid"（unknown key 拒绝）

    # ── 编译期 ──
    def applies(self, view: AgentDeclarationView) -> bool:
        """C0：启用谓词。默认 False（纯 opt-in 包）。纯函数，只读声明态。"""

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        """C1：名册合并前的贡献。默认空。"""

    def bind(self, tree: TreePositionView, config: BaseModel,
             final: FinalRosterView) -> CapabilityBinding:
        """C2：名册合并后的锚校验与 section 门控。
        默认实现：无锚（贡献即绑定）。不一致 raise CapabilityError（boot fail）。"""

    # ── 装配期 ──
    def supply(self, view: PoolSupplyView) -> CapabilitySupply | None:
        """S：池级供给。view 列出池内启用本包的 (agent, config)。
        无池级需求的能力包不覆写（返回 None）。缺供给可解析物 raise（loudly）。"""

    async def assemble(self, binding: CapabilityBinding,
                       ctx: AgentContext) -> CapabilityWiring:
        """A：per-agent 接线。产出有序 prompt providers + 接线对象。
        依赖池级供给时从 ctx.pool_runtime.capability_supply 取，缺失即 raise。"""


@dataclass(frozen=True)
class AgentDeclarationView:
    """C0 谓词的只读输入：树位置 + 该 agent 自身的声明字段（合并前的声明态）。
    永不暴露最终名册或其它能力包的贡献（防启用循环）。"""
    pool_name: str
    agent_name: str
    is_root: bool
    parent: str | None
    children: tuple[ChildSummary, ...]      # (name, description) —— 仅直接子节点
    peers: tuple[str, ...]
    declared: AgentDeclaredFields            # 自身声明字段的 frozen 投影（tools/toolset/memory/mcp/...）


@dataclass(frozen=True)
class CapabilityContribution:
    tools: tuple[str, ...] = ()                       # 进入名册合并基
    tool_replacements: tuple[ToolReplacement, ...] = ()  # O3 编译期替换记账（aci 模式泛化）
    hooks: tuple[str, ...] = ()                       # 进入 merged_hooks
    sections: tuple[PromptSectionSpec, ...] = ()      # (section_id, order, config)


@dataclass(frozen=True)
class PromptSectionSpec:
    section_id: str        # 命名空间约定 "<cap>.<section>"（todo.discipline）
    order: int             # 块内排序（升序）
    config: Mapping[str, Any] = MappingProxyType({})   # 喂给 assemble 的 section 配置


@dataclass(frozen=True)
class CapabilityBinding:
    active_sections: tuple[PromptSectionSpec, ...]    # C2 门控后的存活集合
    payload: Mapping[str, Any] = MappingProxyType({}) # 能力包私有编译产物（线程到 assemble）


class CapabilitySupply(ABC):
    """池级供给基类。具体形状由能力包定义；工厂侧消费时校验具体类型（与
    DATA_NAMESPACE 的 SimpleFactory 检查同款"真实扩展边界"豁免）。"""


@dataclass(frozen=True)
class CapabilityWiring:
    prompt_providers: tuple[SystemPromptProvider, ...]  # 按 section order 排序
    artifacts: Mapping[str, Any] = MappingProxyType({}) # per-agent 接线对象（如 target store）
```

插件注册面（`Plugin.register`）：

```python
class TodoPlugin(Plugin):
    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_capability("todo", TodoCapability())
        # 包内组件照常注册进既有槽位——单路径解析（P2）
        ctx.register_tool("todo_write", TodoToolFactory())
        ctx.register_tool("todo_read", TodoToolFactory())
        ctx.register_hook("todo_continuation", TodoContinuationHookFactory())      # react runner
        ctx.register_hook("todo_reorientation", TodoReorientationHookFactory())    # memory runner
        ctx.register_hook("todo_planning_nudge", TodoPlanningNudgeHookFactory())   # react runner
```

贡献名与槽位注册名的错配（贡献了未注册工厂的名字）沿用既有 late-binding 纪律：装配期 `ComponentNotFoundError`，响亮失败。**例外**：`CAPABILITY` 槽本身在**编译期**解析——能力包参与编译，引用未注册能力包必须在 boot 即炸（比槽位 late-binding 早一个周期，与"改树重启生效"的失败时序一致）。

---

## 5. 声明面（YAML）

### 5.1 字段与形态

```yaml
agents:
  orchestrator:
    capabilities:                  # 映射是唯一形态；值为 false 或配置映射
      subagents: false             # 强制关闭（压掉 auto）
      todo: {}                     # 强制启用，默认配置
      experience:
        min_messages: 20           # 强制启用 + 配置（能力包 config_model 校验）
```

- **仅映射形态**：v1 的 list 糖（`capabilities: [todo]`）废除——覆盖语义下列表天然歧义（"启用这些"还是"替换全集"？），一种语义一种语法。
- 值经能力包 `config_model` 校验（frozen + extra="forbid"，unknown key 拒绝）——配置错误在 boot 响亮。
- 空块 `capabilities: {}` = 无覆盖（自动面原样生效）；不写字段同义。
- **组合内联，资源引用**原则（scope-assembly §3.7）不变：capabilities 块只携带小参数，大块资源各归其文件。

### 5.2 解析语义（覆盖映射，非替换）

- 生效集 = 自动面 ∆ 覆盖面（§3.2）——**capabilities 字段永远不是"全集声明"**。
- profile v1 不携带 capabilities（profile 保持纯 toolset 预设，逢二再抽象）；local 是唯一覆盖层。
- 位置推导默认表（scope-assembly §3.2）新增一行：`capabilities` —— 框架默认 ∅ / 覆盖 `capabilities:` 块；**默认通信能力由 `subagents` 的 auto-apply 谓词提供**（零配置行为不变的机制所在）。

### 5.3 与既有字段的关系

- `tools:` / `hooks:` 的 `+/-` 合并语义**不变**，作用于（含能力包贡献的）合并基——它们是能力包的**组件级拆件 veto 通道**（§3.4）：`tools: [-experience]` 拆整包、`hooks: [-experience_review]` 拆 reviewer 保工具（minus-wins 语义原样保留，由 C2 的 final 视图承载）。
- `tool_supplements` 字段**死亡**（W4 删除）：`ToolSupplement` 枚举（AST_GREP/TODO/ACI/EXPERIENCE）全部升格为 FW bundled 能力包。`tools: [+experience] ≡ 完整包` 的编译器特例等价关系**死亡**：`tools:` 恢复"引用单个工具"的本义（裸引用 experience 工具 = 拆包后的裸工具——工具工厂自身的供给需求照旧响亮校验，但无 hook/提示段/后台件，降级模式文档化于 W5）。

---

## 6. 编译管线变更

`compile_scope` 内（per agent）插入三相：

```
_compile_agent:
  1. toolset/preset 展开（现状不动）
  2. [C0] 生效能力集解析:
       auto = {cap | cap.applies(declaration_view) is True}      # 谓词纯函数
       effective = (auto ∆ declared_overrides)                   # false 剔除 / config 覆盖
       （external agent：跳过 C0，effective = 显式声明集；非空 → V12 boot error）
  3. [C1] for cap_name in effective（确定序：注册表枚举序）:
       cap = registry.resolve(CAPABILITY, cap_name)      # 未注册 → ComponentNotFoundError（boot fail, V13）
       config = cap.config_model 校验（拒绝 unknown key）
       contrib = cap.contribute(tree_view, config)        # 纯函数
       base_tools += contrib.tools；应用 tool_replacements（O3 记账）
       pending_hooks += contrib.hooks
       pending_sections += contrib.sections
  4. tools: ± 合并（现状不动——能力包贡献名进入合并基）
  5. [C2] for cap in effective:
       binding = cap.bind(tree_view, config, FinalRosterView(final_tools, merged_hooks))
         # 默认无锚直通；锚缺失 → CapabilityError（boot fail，带 pool/agent/capability 上下文）
       capabilities_block[name] = (config, binding)
  6. AssemblySpec 增字段：capabilities: tuple[CompiledCapability, ...]
       # CompiledCapability = (name, config, binding) —— frozen、JSON 可序列化（spec-hash 面）
```

要点：

- **编译器能力包无关**：`_derived_entries`、EXPERIENCE hook 注入、`_apply_supplements` 的 ACI 分支全部迁出（§8），编译器只剩协议。
- **字节稳定性**：`CompiledCapability` 只携带 name/config/binding（frozen 模型，无运行时对象）；能力包对象本身不进编译产物。确定性契约（P1）由守卫测试锚定：同声明 + 同注册表 → 两次编译 hash 相等（auto-apply 谓词纳入守卫——谓词读的声明视图同样是编译输入）。
- **签名变更**：`compile_scope(...)` 新增 `registry: ComponentRegistry` 参数（进程级单例，不影响稳定性）。
- **V6 处置**：`ScopeTreeValidator` 的 V6（有子 ⇒ 生效名册含 task）**保留**——树派发能力与骨架（broker/session tree/invocation）深耦合，task 名是骨架完整性事实，不随第三方能力包泛化（N6）。`SubagentsCapability.bind` 双检同规则（错误上下文更丰富：点名能力包与拆除路径）。两处同错不同层：validator 兜结构性，bind 给修复指引。

---

## 7. 装配管线变更

### 7.1 池级供给面（Stage 3 侧）

`PoolRuntimeDeps` 新增通用供给面：

```python
@dataclass(frozen=True)
class PoolRuntimeDeps:
    ...
    capability_supply: Mapping[str, CapabilitySupply] = MappingProxyType({})
    # W4 收敛后删除 todo_store / communication 等类型化字段——
    # 全部供给走这一条面（TodoToolFactory 改读 capability_supply["todo"]）
```

- pool 装配聚合本池编译产物中出现的**生效能力包集合**，逐个调 `supply()`；返回 None 跳过。
- 供给对象按能力包注册名索引；消费侧（TOOL/HOOK 槽工厂、assemble）取用时校验具体类型，缺失/类型不符响亮 raise（与 `ExperienceToolFactory` 现有 loudly 模式一致）。
- **W4 收敛台账**：`todo_store`、`communication` 类型化字段迁入 `capability_supply` 后删除——供给单机制（收敛规则 1），迁移期两波各自 split-brain。

### 7.2 agent 装配面（native_core 内）

`assemble_native_agent` 在 hook 派发后新增能力包派发：

```
for compiled_cap in spec.capabilities:
    cap = registry.resolve(CAPABILITY, compiled_cap.name)
    wiring = await cap.assemble(compiled_cap.binding, agent_ctx)
    capability_wirings[compiled_cap.name] = wiring
```

- main（Stage 4）与 subagent（`AgentTemplate.materialize`）同经 native_core——能力包接线对两者自动一致（与 hooks 的收敛点同构）。
- per-agent 接线对象（如 per-agent `CommunicationTargetStore`）经 wiring.artifacts 到达消费方；工具工厂从上下文链读取（机制同今日 `CommunicationFacilities` 的 per-agent 供给路径，所有权搬家）。
- external agent：编译期已结构排除（§6 C0 步骤）；显式声明 + external → V12 boot error（防静默无效声明）。

### 7.3 提示段面（新表面）

`MemorySystemContextManager.load()` 的硬编码 provider 列表开放**一个**插入点：

```
[1 runtime/model-info] [2 base] [2a fork] [2b 能力包段块(按 order 升序)] [3 core memory] [4 archive]
[5 pruned] [6 provider blocks] [7 prefetch] [8 experience(→ 能力包段之一)] [9 skills] [10 roles] [11 graph]
```

- 能力包段块取代今日 2b（TodoAware）与 2c（AgentComm）两个硬编码位，锚点固定于 fork 之后、core memory 之前——**块内** order 排序，**块位**不可配置（N4：提示段顺序是 KV-cache 前缀稳定性事实；INPUT_STAGE"骨架顺序不可配、开放插什么"同款语义）。
- provider 实例由 `assemble()` 产出，经 `NativeAssemblyInputs` 新参数 `capability_sections: tuple[SystemPromptProvider, ...]` 注入 context manager（与今日 tool_manager 注入同通道）。
- 段内容的 session 内稳定性由能力包自负（沿用 SystemPromptPipeline 的 version 缓存契约：version 稳定 ⇒ 前缀缓存命中）。
- 现有 `TodoAwareSystemPromptProvider` / `AgentCommunicationSystemPromptProvider` 的**运行时工具注册检测逻辑死亡**（P3）：编译期已知 section 归属，provider 退化为静态内容（version 恒定）或 manager 驱动内容（experience 段 version = 内容 hash，既有实现搬家）。

---

## 8. 迁移映射（五个 FW bundled 能力包）

迁移纪律：每包一波、自带删除台账、split-brain 判据（迁移前后 effective 产物逐项一致：生效能力集 / 工具名册 / hook 名册 / 提示段内容 / 供给对象）。每包标注 `applies` 谓词——**谓词语义本身就是 split-brain 的一部分**（谓词输出 ≡ 迁移前"是否获得该能力"的判定）。

### 8.1 `ast_grep` + `aci`（最简先行——纯工具面）

| 项 | 迁移前 | 迁移后 |
|---|---|---|
| `applies` | — | `False`（纯 opt-in，等价今日"不声明即无"） |
| 启用 | `tool_supplements: [ast_grep]` / `[aci]` | `capabilities: {ast_grep: {}}` / `{aci: {}}` |
| 工具 | `ToolSupplement` 枚举展开 | contribute: tools / tool_replacements=[(edit, aci_edit)] |
| 替换记账 | `_apply_supplements` ACI 分支 | O3 记账泛化（ToolReplacement 进 contribution） |
| 删除 | `ToolSupplement` 枚举成员、`tool_supplements` 字段、`_apply_supplements` | — |

### 8.2 `todo`（全要素范本：工具 + 双 runner hook + 提示段 + 池供给）

| 组件 | 迁移前 | 迁移后 |
|---|---|---|
| `applies` | — | `False`（opt-in；今日 supplement 不声明即无，语义等价） |
| 工具 | supplement 展开 → `TodoToolFactory`（读 `pool_runtime.todo_store`） | contribute: `[todo_write, todo_read]`；工厂改读 `capability_supply["todo"]` |
| TodoContinuationHook | `register_tree_aware_hooks` 无条件注入 + 运行时 gate（模式 D） | contribute: hooks `[todo_continuation]`；只有生效该包的 agent 获得（gate 死亡） |
| TodoReorientationHook | memory runner 无条件注册 | contribute: hooks `[todo_reorientation]`（memory runner 面） |
| 提示段 | `TodoAwareSystemPromptProvider` 硬编码 + 运行时 gate | section `todo.discipline`（order=30），assemble 产出静态 provider |
| 供给 | `build_pool_todo_store` 恒构建 | `supply()` 按需构建（池内无人启用则无 store） |
| 拆件 | 不可（无声明面） | `tools: [-todo_write]` 拆件 → C2 锚校验炸响（两工具必须同进退——比现状更严且有声） |
| 删除 | `TodoAwareSystemPromptProvider`、`register_tree_aware_hooks` 的 todo 分支（deliver_retry/length_guard 保留至 W6）、`TodoPlanningNudgeHook` 工厂残留、`pool_runtime.todo_store` 字段 | — |

### 8.3 `experience`（供给最重：manager + curator + 注入）

| 组件 | 迁移前 | 迁移后 |
|---|---|---|
| `applies` | — | `False`（opt-in，等价今日 name-merge 特例的"声明才启用"） |
| 启用 | `tool_supplements: [experience]`（name-merge 特例） | `capabilities: {experience: {…}}` |
| 工具名 | 编译器 EXPERIENCE name-merge 特例 | contribute: tools `[experience]`（进合并基，± 语义原样） |
| review hook | 编译器 `EXPERIENCE_REVIEW_HOOK_NAME` 注入特例 | C2 bind：锚存活且无 `-experience_review` veto ⇒ binding 携带 hook 名（minus-wins 原样） |
| manager/curator | `declared_assembly_deps` 读名册派生 enabled | `supply()`：池内生效 ⇒ manager + dir + curator 后台 runner |
| 注入段 | `ExperienceProvider`（context manager 特判 `_experience_manager`） | section `experience.injection`（order=50），assemble 从 supply 取 manager 构 provider |
| 删除 | 编译器两处特例、`declared_assembly_deps` 的 experience 派生分支、`ExperienceConfig.enabled` 的名册跟随逻辑（编译产物 capabilities 块即真相源） | — |

### 8.4 `subagents`（树推导 + 动态启用旗舰：通信三工具 + facilities）

| 组件 | 迁移前 | 迁移后 |
|---|---|---|
| `applies` | —（编译器硬编码推导） | `bool(children) or not is_root or bool(peers)`——参与通信拓扑即启用；无子无 peer 的根不得（≡ 今日 derived-entries 输出为空） |
| 三工具 | `compiler._derived_entries` 硬编码（模式 A） | `SubagentsCapability.contribute` 读树（§3.2） |
| 提示段 | `AgentCommunicationSystemPromptProvider` 硬编码三 sub-provider + 运行时 gate | sections `subagents.delegation/consultation/peer`（order 40/41/42），assemble 产出 |
| SubagentAutoSendHook | template 物化期默认注入 | contribute: hooks（非根才贡献） |
| facilities | BIZ supply infra（SupplyInfra.communication）+ `pool_runtime.communication` | `supply()` 建 service（peer tree refs 解析随迁）；per-agent target store 移入 assemble |
| V6 | validator 单检 | validator 保留 + bind 双检 |
| 删除 | `_derived_entries`、`AgentCommunicationSystemPromptProvider`、`pool_runtime.communication` 字段、`_wire_main_pipeline` 通信特例 | — |

### 8.5 `reference_collector`（BIZ 第三方样例）

现有最完整的第三方形态（单 hook + `hook_configs.max_sources`）。迁移演示：**无需升级**——单 hook 组件继续走 HOOK 槽是合法形态（applies=False、无贡献、直接槽位引用）；若作者想成包（加提示段/工具/池状态），改写为 Capability 即可获得动态启用与账单归属。此对照进 W5 文档（插件作者指南）。

---

## 9. Provenance 与账单

- `ToolOrigin` 新增 `CAPABILITY_DERIVED`（携带能力包名）；`AgentProvenance` 新增 capabilities 维度：
  - 每 agent 的生效能力集，条目三态：`auto`（谓词命中）/ `declared`（显式启用）/ `vetoed`（auto 命中但被 `false` 压掉）；
  - 每包贡献条目（工具/hook/section）与 C2 门控结果（存活/组件级 veto/锚拒绝）；
  - auto 条目携带 `registration_source`（O2 既有审计面）——第三方包静默生效于此可查。
- `GET /api/scope/bill` 按请求重算（无 boot 缓存，scope-assembly 规则 3 不变）——能力解析与推导是纯函数，重算廉价性保持。**覆盖映射语义下账单是"agent 有什么能力"的唯一权威答案**（§3.2）。

## 10. 校验规则变更

| # | 规则 | 阶段 | 状态 |
|---|---|---|---|
| V1-V5, V7, V9-V11 | （现状） | 一 | 不变 |
| V6 | 有子 ⇒ 生效名册含 `task` | 二 | 保留（骨架树完整性）；bind 双检 |
| **V12（新）** | external agent **显式声明** `capabilities` 非空 → boot error | 一 | 新增（auto 面不运行为 external，故仅显式声明可达此规则） |
| **V13（新）** | `capabilities` 引用未注册名 → boot error（编译期 CAPABILITY 槽解析） | 一 | 新增 |
| （能力包内规则） | 锚校验、树一致性、拆件合法性 | 编译 C2 | 能力包自治（CapabilityError），框架不枚举 |

## 11. 错误处理矩阵

| 失败场景 | 行为 | 时机 |
|---|---|---|
| 声明引用未注册能力包 | `ComponentNotFoundError`（V13）——boot fail，早于任何装配 | 编译期 |
| 能力包 config unknown key / 类型错 | `config_model` 校验拒绝——boot fail | 编译期 |
| C2 锚校验失败（如 children 无 task） | `CapabilityError`（点名能力包/agent/拆除路径）——boot fail | 编译期 |
| 谓词/推导非纯（不确定性） | spec-hash 稳定性守卫测试抓（CI 红）；运行期不设防线（纯度是契约非运行时检查） | 测试期 |
| 贡献名无槽位工厂（贡献/注册错配） | 装配期 `ComponentNotFoundError`（既有 late-binding 纪律） | 装配期 |
| `assemble` 需求的供给缺失（supply 未建/类型不符） | 响亮 `ValueError`，带能力包名与补救指引 | 装配期 |
| `supply()` 自身构建失败 | pool 装配中止（供给原子性随 pool 装配既有失败语义） | 装配期 |

## 12. 明确不做的事（N 清单）

| # | 不做 | 理由 | 翻转条件 |
|---|---|---|---|
| N1 | 运行时启用/禁用能力包（热插拔） | ADR-0041 N4 / ADR-0042 N2 同源；重启生效是全局假设 | 同 ADR-0042 N2 翻转条件（换装机制预留缝已存在） |
| N2 | per-session 能力组合 | 配置隔离到 agent 声明级；会话差异 = 数据差异（SessionScope） | scope-converge N5 翻转时一并评估 |
| N3 | 能力包间依赖声明 | 显式共同引用即组合（scope-converge N7 先例）；bind 可检测互斥，不做前置依赖图 | 真实第三方生态出现循环依赖痛点 |
| N4 | 提示段任意位置插入 | 段序是 KV-cache 前缀稳定性事实；固定锚点+块内 order（INPUT_STAGE 骨架顺序先例） | 出现必须插在 core memory 之后的真实能力包 |
| N5 | 池级能力包配置块 | 今日零真实需求（experience curator 等保持 preset-baked）；供给面自带聚合语义 | 某能力包供给需要池级参数且有第二实例 |
| N6 | 第三方 dispatch 工具的 V6 泛化 | task 名与骨架（broker/session tree/invocation）深耦合；星型拓扑是骨架事实 | 出现第二个真实 dispatch 机制（连骨架一起评估） |
| N7 | 特殊 agent（reviewer/compactor）进装配管道 | ADR-0041 N18 维持；能力包只携带触发/接线面（experience 的 reviewer 保持 inline） | ADR-0041 N18 翻转条件 |
| N8 | 第二套 veto 语法 | 组件级拆件复用 `tools:/hooks:` ± 既有语义；能力级关闭是映射 `false` 单通道——两种高度两套语法，各自唯一 | ± 语义被证明无法表达某拆件需求 |
| N9 | profile 携带 capabilities | profile 现为纯 toolset 预设；auto 面 + local 覆盖已覆盖需求（逢二再抽象） | 多 pool 复用同一能力覆盖成为高频操作 |
| N10 | auto-apply 源门控（bundled/project 可 auto、user/entry_points 仅显式） | 安装即信任（插件 Python 已在进程内）；静默生效风险由账单三态 + registration_source 审计承载 | 真实第三方生态出现"装了就全局生效"的投诉 |

## 13. Wave 计划

| Wave | 内容 | 删除台账（对冲） | 交付判据 |
|---|---|---|---|
| **W1 类型基座** | `Capability` ABC（含 `applies`）+ `CAPABILITY` 槽 + `AgentDeclarationView`/`TreePositionView` + Contribution/Binding/Supply/Wiring frozen 类型 + 编译协议插入（C0→C1→合并→C2）+ 声明面（覆盖映射、仅 map 形态、V12/V13）+ 注册面 `register_capability` | 无（纯加法波——下一波点名对冲） | T-CAP1 红绿锚：dummy 能力包（谓词 + 贡献 + 绑定）端到端进编译产物；零能力包声明 = 迁移前 spec 逐字节等价（除新增空 capabilities 块）；谓词确定性守卫（同声明双编译 hash 相等） |
| **W2 提示段面** | pipeline 锚点（fork 后/core 前）+ `NativeAssemblyInputs.capability_sections` 通道 + context manager 参数 + 块内 order + external 排除接线 | `MemorySystemContextManager.load()` 的 2b/2c 硬编码位改为锚点占位 | section 内容/顺序守卫测试；KV-cache version 契约测试 |
| **W3 能力包迁移**（3a→3d 递进，各一波） | 3a `ast_grep`+`aci` → 3b `todo` → 3c `experience` → 3d `subagents`（§8 各表，含各自 `applies`） | 每包自带删除台账（§8）；W3d 后 `capability_supply` 上线 | 每包 split-brain：迁移前后**生效能力集**（谓词 ≡ 旧判定）/ 工具名册 / hook 名册 / 提示段内容 / 供给对象逐项一致 |
| **W4 供给收敛** | `capability_supply` 全面接管：`pool_runtime.todo_store`/`communication` 字段删除、工厂改读供给面、`tool_supplements` 字段+`ToolSupplement` 枚举死亡、`_derived_entries`/EXPERIENCE 编译特例死亡、nudge 工厂残留删除 | （本波即删除波） | slot gates 扩展（`verify_slot_gates.py` 增能力包机械门禁）；1900+ bot 测试全绿 |
| **W5 账单+文档** | ToolOrigin/账单 capabilities 三态维度、bill 端点扩展、bot.yml 全量迁移到 `capabilities:` 面、AGENTS.md 群（root/scope/plugins/配置示例）、插件作者指南（reference_collector 对照 + 裸工具降级模式文档化） | 文档中 `tool_supplements` 语义全部退役 | bill 可审计第三方能力包来源（含 auto 条目）；文档 doc-sync 绿 |
| **W6 收敛清单**（P7 兑现波） | 逐项审计消灭残余无声明面注入：`register_tree_aware_hooks` 残余（deliver_retry / length_guard）、`model_choice_bind`、`native_env` 及一切装配代码无条件注入点——每项要么升格能力包、要么进框架位置默认（编译产物可见、可覆盖、可 veto、账单可查），`_wire_main_pipeline` 残余特例同类处置 | 全部无条件注入代码路径；`register_tree_aware_hooks` 本体 | 机械门禁：装配路径无条件组件注入 grep 守卫进 `verify_slot_gates.py`；账单零"无来源"组件 |

规模初估：新增 ~1400（协议+五包+守卫）/ 修改 ~900 / 删除 ~800（特例+枚举+硬编码 provider+残余注入）。

## 14. 验证判据（完成定义）

1. **零胶水能力包**：第三方"工具+hook+提示段+池供给"四要素能力包，纯 plugin 注册 + YAML 一键引用到达生产装配产物，FW 零代码（T-CAP2 集成测试，T0.x 同款机器证明）。
2. **动态启用可证**：谓词读声明字段切换启用的能力包（如假想 terminal-safety 包读 `use_terminal`）有红绿测试——同声明改一个字段，生效集变。
3. **split-brain 全绿**：五包迁移前后 effective 产物逐项一致（W3 各波判据，含谓词语义等价）。
4. **树推导等价**：三层嵌套树上 `SubagentsCapability` 的 C0+C1 输出 ≡ 迁移前"是否获得 + derived 条目"（表驱动对照测试，含无子无 peer 根的空输出）。
5. **确定性**：同声明+同注册表双编译 spec-hash 相等（auto-apply 谓词纳入字节稳定性守卫）。
6. **失败有声**：未注册引用（V13）/ external 显式+capabilities（V12）/ 锚缺失（V6 双检）三条 boot-fail 路径各有测试。
7. **零配置行为不变**：空声明（无 capabilities 块）编译产物 ≡ 迁移前（subagents 谓词兜底通信推导）。
8. **无声明面注入清零**：W6 机械门禁绿；账单上每个组件可溯源（声明/profile/位置默认/能力包贡献之一）。
9. 现有全量测试 + slot gates 全绿。

## 15. 开放问题（实现前需拍板，不阻塞 W1/W2）

- **OQ1 supply 聚合语义**：池内多 agent 生效同一包且覆盖配置不同时，`supply()` 拿到全量 `(agent, config)` 视图自行裁决——todo/experience/subagents 均无真实分歧需求，v1 如此；若出现"配置必须一致"的包，其 supply 自行 raise（能力包自治）。
- **OQ2 subagents 供给的 FW 化边界**：`AgentCommunicationService` 构建今日在 BIZ supply infra；W3d 迁移时是"能力包 FW 化（构建逻辑随包搬家）"还是"FW 包声明 demand、BIZ 供给注册"——倾向前者（能力包自包含，P4），但 peer tree refs 解析涉及 workspace 资源束，实现时定夺。
- **OQ3 sections 的动态内容**：v1 section 配置静态；`{{model}}/{{cwd}}` 插值类需求由 provider 动态版本承担（既有 pipeline 能力），不进声明面。
- **OQ4 `hooks:` 名单与能力包 hook 贡献的 dedup**：与今日 roster-hook dedup（`roster_hook_names`）同规则——能力包贡献名进入 merged_hooks 后即 roster 名单成员，code-wired 默认侧同名跳过；W3 各波迁移时逐包核对，W6 清余后此问题随注入点死亡而消失。
- **OQ5 编译期解析 CAPABILITY 槽的守卫**：capability 是唯一编译期解析的槽位（其余 late-binding）——`scripts/verify_slot_gates.py` 需要新机械门禁锚定这个不对称（防止未来误把其他槽提前到编译期）。

---

## 附录 A：术语（canonical 定义见根 `CONTEXT.md`，此处仅索引）

- **Capability（能力包）** / **Auto-Apply（自动启用）** / **启用谓词** / **Enablement Resolution（启用解析）** / **生效能力集** / **声明覆盖** / **锚（anchor）** / **段（section）** / **供给（supply）**——以 CONTEXT.md 为准。
