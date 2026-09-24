# PRD:per-model 上下文预算与统一会话压缩

- 状态:设计定稿,待实现(实现拆解见同目录 `tickets.md`)
- 日期:2026-09-23
- 范围:框架(`src/modex_agent/`)+ bot 示例(`examples/bot_project/`);外部代理(agents/external)不在范围内——其 owns its model config

## 1. 问题与已确认决策

三个问题:

1. 模型上下文上限是全局的(`BotModelConfig.max_context_tokens`),不随"当轮选了哪个模型"变化;会话中途切到小上下文模型时无任何预算处理。
2. 子代理无法独立选择模型;experienceReviewer 等后台代理的模型是"偶然继承当轮选择",不是稳定契约。
3. 压缩摘要调用是单遍全量,小模型放不下即失败降级。

已确认决策(讨论定稿,实现不得偏离):

| # | 决策 |
|---|---|
| D-1 | **切模型事件零处理**。校验只存在于两个既有触发面:LLM 调用前 + memory append。前台仅切换不运行时不 compact(与 opencode/kimi-code/pi 三家一致) |
| D-2 | **pre-LLM 检查不是暴力机械治理**,与 append 路径收敛到 memory 的统一 compact 接口(新增 `compact_session` 抽象方法,默认 no-op) |
| D-3 | 新治理放在原暴力治理(`ContextBudgetGovernance`)的链路位置之前,**基本替代 bot 中的使用** |
| D-4 | 分段压缩采用**滑动窗口分段摘要 + 最终统一整合**(map + reduce),非链式 refine |
| D-5 | 子代理**默认运行全局默认模型**,可通过声明显式选择(pin);采样参数不复制,由模型条目独占。(重定基 2026-09-24:原设计"默认继承调用方"基于错误基线——子代理 turn 由 InboxPoller 后台任务派生,turn 级 ContextVar 从不跨任务边界,"继承调用方"在运行时从未成立;经用户确认改为默认模型,并显式 pin 化) |
| D-6 | 压缩用**当轮所选模型**(显式契约化现有 ContextVar 继承);后台维护代理(reviewer/title)用默认模型 pin |

## 2. 现状事实基线(实现前必读,均已在源码核实)

### 2.1 模型配置与选择链路

- bot 实际生效的是 `BotModelConfig`(`bot/service/model_config.py:94`):`default_provider/default_model/max_context_tokens=200000/providers[]`;每个 `ModelCfg`(model_config.py:22-33)已有 `temperature/top_p/max_output_tokens(默认50000)/reasoning_effort(默认none)/capabilities`——**采样集已 per-model,唯缺 context_limit**。
- `synthesize_llm_config`(model_config.py:163-179)把 ResolvedModel 合成为框架 `LLMConfig`(含 reasoning_effort)。
- 每 turn 模型选择:WebUI 消息级选择器 → `ModelChoiceStage`(`bot/input_pipeline/stages/model_choice.py:44-65`)→ `ModelChoiceRegistry`(session→ResolvedModel LRU,`bot/service/model_choice.py:37-63`)→ `ModelChoiceBindHook`(BeforeGraphHook,model_choice.py:66-94)把选择快照进 ContextVar 并覆写 `runtime.services.model_info` → 池级单例 `BotModelProvider.stream()` 读 ContextVar 决定真实模型(`bot/plugins/model_provider.py:69-79`,按 (provider.key, model) 缓存真实 provider)。
- `ModelInfo`(`core/capabilities.py:50`)仅 `model_name + capabilities`;经 `runtime_info[RuntimeInfoKey.MODEL_INFO]` 流入 `load()`(system.py:259),工具经 `ctx.runtime.model_info` 读取(runtime/services.py:112-113)。

### 2.2 会话写入与压缩

- 消息**实时落库**:`MemorySystemContextManager.save()` 为空操作("All messages written in real-time through ScopedMessageHistory",system.py:374-381)。turn 中每条 assistant/工具消息都走 `ScopedMessageHistory.append` → `_run_cleanup_if_triggered`(history.py:176-194)——append 检查天然逐消息、含 turn 中途。
- 持久压缩 `cleanup_session`(memory/cleanup.py:483):5 阶段(prepare→compact 生成→commit `[compact_summary]+[tail]`→pruned catalog→archive);阈值 `check_cleanup_trigger`(cleanup.py:745-766):`effective = max_context_tokens − max_output_tokens`,`pressure > effective × 0.85`;唯一调用方是 history.py:109(即"只在添加记忆时检查")。
- 摘要器 `SessionCompactorAgent`(agents/summarizer/session_compactor.py:44):**单遍全量**——整个被剪 transcript 塞进一条 user 消息;prompt 已支持 `previous_summary` 链式更新(session_compactor.py:234-260);失败降级为 tail-only 无摘要。
- 压缩用池 provider(`BotModelProvider`),turn 任务内触发时经 ContextVar 继承当轮模型——**是偶然行为,非契约**。
- `load()` 里的 `ensure_within_budget`(system.py:213 调用,default_system.py:305 实现)是框架预留的 "Budget enforcement before every LLM request" 钩子,**当前为 no-op 存根**。

### 2.3 治理与应急

- `ContextBudgetGovernance`(memory/context_governance.py:140):pre-LLM 机械治理,把保护窗(protect_tokens=40k)外旧工具结果替换为占位符,仅作用请求副本;阈值 0.60 × 池装配时的**静态** max_context_tokens。
- 接线:ioc 工厂 `create_governance`(ioc/factories/governance.py:13)链 = `ContextBudgetGovernance → ToolChainRepairGovernance`;子代理 `create_subagent_governance`(governance.py:66)仅 ToolChainRepair。
- 治理挂载点:`LLMNode._build_messages`(agents/react/nodes/llm.py:199-228)——构建顺序为 `[system?] + ctx.to_messages()` → `apply_governance` → `inject_multimodal`。**治理收到的就是 [system]+history,之后再无其他拼装**(压缩后重建消息列表的契约依据)。
- 应急压缩 `EmergencyCompactionGovernance`(agents/react/error_recovery.py:41):`ReactLlmClient` 捕获 provider 上下文溢出错误后砍尾(留 10 → 留 5),无摘要(llm_client.py:75-105)。

### 2.4 子代理与后台代理

- 所有原生子代理共享池级 `BotModelProvider`(`AgentSpec` 只有 `llm_provider` 组件名覆盖,scope/spec.py:204,bot 只注册一个组件 → 无 per-subagent 模型选择)。**基线勘误(2026-09-24)**:原稿称"子代理实际模型 = 当轮 ContextVar 选择"——错;子代理 turn 由 `InboxPoller` 的后台循环 `create_task` 派生(inbox_poller.py:186-203),拷贝的是 poller 启动上下文,turn 级 `current_model_choice` ContextVar 从不达子代理 turn,未配置子代理实际一直运行**默认模型**。D-5 据此重定基并显式 pin 化(§4.5)。
- `AgentMaterializeDeps` 携带池级默认(multi_agent/materialize_deps.py:61-66;bot 侧取 default_resolved,factory.py:737-752);descriptor 层 `AgentLLMConfig` 的 TODO(descriptor.py:32-36)已指明收敛方向:采样参数由 provider/模型条目独占。
- experienceReviewer:`ExperienceReviewAgent` 用 `SupplyInfra.default_llm_provider`(= BotModelProvider,capability.py:161);后台任务由 turn 任务 `create_task` 派生(supply.py:122-125)→ **继承触发轮的模型,属意外耦合**。
- session_title:刻意直连默认模型(session_title_task.py:103-152)。

## 3. 参考实现要点(.references)

| | opencode | kimi-code | pi | 本设计 |
|---|---|---|---|---|
| limit 来源 | models.dev 目录 | config.toml 必填 `max_context_size` + 413 观测学习 `min(config, 0.85×失败请求)` | models.dev + 用户覆盖 | model.yml per-model 声明 |
| 检查点 | 步间 + 流中 + 报错后 | 每 step 前(阻塞式) + 报错后 | 轮间 + 新 prompt 前 + 报错后 | **pre-LLM 治理 + append 实时**,两面对一个接口 |
| 阈值 | 减法式 usable + reserved | 双条件:ratio 0.85 **或** used+reserve≥max(reserve 50k) | 绝对:`tokens > window − reserve(16k)` | 双条件(吸收 kimi),reserve=`min(20k, limit×10%)` |
| 摘要放不下 | 直接失败终止 | 预裁剪丢最老 + 溢出按 0.7/0.5/0.35 收缩 + wire 日志兜底 | 失败即失败 | **滑窗 map + 统一 reduce,深度 2** |
| tail 保留 | `clamp(usable×0.25, 2k..15k)` | 用户消息头/尾逐字保留(预算 20k/2k) | `keepRecentTokens=20k`,切点永不在 toolResult | 预算绝对值化 `clamp(usable×0.25, 2k..15k)` |
| 切模型 | 零处理 | 零处理(检查时重解析当前模型) | 零处理 + sameModel 防误触 | 零处理;吸收 sameModel 防误触 |

**采纳**:双条件阈值(kimi)、reserve 概念(三家)、tail 预算绝对值化(opencode/pi)、序列化时工具结果截断 2000 字符(opencode/pi)、sameModel 防误触(pi)、压缩用当前会话模型(三家一致)。
**不采纳/超越**:摘要放不下即失败(opencode/pi)或丢最老不摘要(kimi)——本设计做滑窗分段;models.dev 外部目录——改配置声明;kimi 观测上限学习列为 P2 可选。

## 4. 设计

### 4.1 总体结构:一个接口、两个触发面、一条升级链

```
                 ┌── memory ABC: compact_session(context, *, max_context_tokens, source) ──┐
                 │    默认 no-op(返回 triggered=False);DefaultMemorySystem 独家实现      │
                 └──────────────┬──────────────────────────────┬──────────────────────────┘
   触发面 ① pre-LLM             │                              │        触发面 ② append(实时写)
   MemoryCompactionGovernance   │                              │   ScopedMessageHistory._run_cleanup_if_triggered
   (新,治理链首,每 LLM 迭代)  │                              │   (每条消息落库,含 turn 中途)
        廉价预检 ──超限──→ compact_session ──┘
```

升级链(信息保全从高到低):**统一压缩(0.85,LLM 摘要,持久)→ 机械占位(0.60,仅副本,bot 默认链路退场,保留给无 memory 部署)→ 应急砍尾(provider 报错后,不动)**。

### 4.2 D1 per-model 模型档案

- `ModelCfg` 增加 `context_limit: int | None = None`(None=继承全局 `max_context_tokens`,完全向后兼容);`ResolvedModel` 自然携带。
- 框架 `ModelInfo`(core/capabilities.py:50)增加 `context_limit: int | None = None` 与 `max_output_tokens: int | None = None`——ModelInfo 是"当轮激活模型"的值对象,预算字段随既有 per-turn 管道(`ModelChoiceBindHook` 覆写 `services.model_info` → `runtime_info[MODEL_INFO]` → `load()`/`ctx.runtime.model_info`)到达所有消费点,**零新增穿线**。
- 装配期校验:`synthesize_llm_config` clamp/assert `max_output_tokens ≤ context_limit − 预留`。
- **优先级链(唯一定义处)**:`当轮 model_info.context_limit → 池 memory_overrides.max_context_tokens → 全局 max_context_tokens`,由**框架侧**单一 resolver 函数实现(bot 的全局值经装配已流入框架 `MemoryConfig`,链条实为两级),治理与 history 覆盖共用。

### 4.3 D2 统一压缩接口 + MemoryCompactionGovernance

#### 4.3.1 memory 的 `compact_session`(默认 pass)

```python
class ContextManagedMemorySystem(ABC):
    async def compact_session(
        self, context: MemoryContext, *,
        budget: ContextBudget | None = None,
        source: CompactionSource,
    ) -> CleanupResult:
        """统一会话压缩入口。默认 no-op。"""
        return CleanupResult(triggered=False)
```

- 签名收敛说明(实现定稿):预算参数为 T1 的 `ContextBudget` 整体(含当轮 limit 与 max_output),而非散 int——T4 的分段压缩需要两者算预算;`None` = 回退系统静态配置。

- 默认 no-op 而非 abstract(先例:`ContextManager.flush` no-op 钩子)——`InMemoryContextManager` 类部署与外部代理零改动。
- `DefaultMemorySystem.compact_session` 收编现散在 history.py:109 的 10+ kwargs `cleanup_session(...)` 自由函数调用(系统本就持有 session/archive/pruned/compactor/hook_runner/estimator 全部依赖);history 侧只留"触发判断 → 调接口 → 刷缓存"。新增两个职责:
  - **在飞去重**:per-MemoryContext in-flight 标志,防止治理与 append 相邻触发跑两遍摘要;落败方复用现有 revision-conflict 语义。
  - **source 标注**:CleanupResult/hook 载荷区分"写侧/读侧触发"。
- 两级检查:调用方廉价预检(逐消息 `token_count` 已缓存);接口内 `_prepare_cleanup_phase` 权威复核(现状语义)。
- `ensure_within_budget` **维持 no-op 存根**:load 时刻被新治理覆盖(turn 首次 `_build_messages` 紧跟 load),该钩子按其文档注释继续留给未来 read/check 策略。

#### 4.3.2 MemoryCompactionGovernance(链首,非暴力)

```python
class MemoryCompactionGovernance(ContextGovernance):
    def __init__(self, memory_system, *, memory_context_factory,
                 fallback_budget, token_estimator, ratio=0.85, reserve=None): ...

    async def apply(self, messages, ctx) -> list[dict]:
        # 1. 当轮 limit:ctx.runtime.model_info(D-1 后带预算字段)→ 构造回退值
        # 2. 零改写快速路径:压力 ≤ 阈值 → 原样返回(成本 = 对已缓存 token_count 求和)
        # 3. 超限 → memory_system.compact_session(context, budget=当轮预算, source=PRE_LLM)
        # 4. 只要发起过 compact 调用就重建(无论 CleanupResult 如何):
        #      await ctx.history.refresh()   # MessageHistory ABC 新增可选 no-op 方法
        #      return [system?] + await ctx.to_messages()   # 形状契约:llm.py:207-211
        #    (实现将原稿步骤 4/5 合并为"发起即重建":幂等,且覆盖并发写侧刚
        #     压缩完、本侧视图陈旧的情形)
```

- **重建契约依据**(已核实):`_build_messages` = `[system?] + ctx.to_messages()` → `apply_governance` → `inject_multimodal`(llm.py:199-228);治理返回 `[system]+刷新后 history` 形状精确,多模态注入在治理之后不受影响。
- **MessageHistory.refresh()**:core ABC 新增可选 no-op 默认方法(先例:`ContextManager.flush`);`ScopedMessageHistory` 实现为置空缓存、下次 `to_list` 懒加载。
- **接线**:`create_governance` / `create_subagent_governance`(ioc/factories/governance.py)链首插入,两工厂签名增加 memory_system + context factory 注入——**主代理与子代理同链**(收敛规则 1);bot 装配处停用 `governance.budget` 配置,机械治理退场。治理的 MemoryContext 必须与 `load()` 为本 session 构建的**同一实例**(memory store 按 MemoryContext 键控,异键即压缩错会话)。
- **治理独有价值**:它在每次迭代以当轮预算检查装配后的会话历史副本(与 append 面同一阈值函数、同一 resolver),是切模型后首个 LLM 调用前的读侧防线。注:压力求和按 `check_cleanup_trigger` 惯例**排除 system 消息**——system prompt 膨胀不在其判定内(压缩也缩不了 system prompt),该场景仍由 provider 溢出后的应急层兜底。
- **UX 已就位**:`CLEANUP_TRIGGERED` 在慢速摘要调用前派发(cleanup.py:592-604),WebUI 可先告知;压缩用量经 `UsageCollectingProvider` 隔离记账。
- **触发公式(共享函数,双条件)**:

```
usable = limit − max_output_tokens − reserve       # reserve = min(20_000, limit×10%),可配
触发:  pressure > usable × ratio(0.85)  或  pressure + reserve ≥ limit − max_output_tokens
```

数学注记(实现评审确认):默认 ratio 0.85 与自动 reserve(≈0.1×usable)下,绝对条件的触发点恒晚于 ratio 条件——默认配置下绝对条件**惰性**(零行为);它为放宽 ratio(≥0.9)或显式大 reserve(kimi 式固定 50k)的部署而存在,测试以显式参数锁定。

#### 4.3.3 append 路径收敛 + 每 turn limit 下沉

- `_run_cleanup_if_triggered` 改调 `compact_session`(行为等价重构)。
- `create_message_history`(default_system.py:101)增加可选 per-history 预算覆盖:`load()` 时从 `runtime_info[MODEL_INFO]` 取当轮预算传入;新 history 实例的 append 检查优先用它,静态池配置为回退——切小模型后 turn 中途增长由此覆盖。
- 应急路径吸收 pi 的 sameModel 防误触:溢出错误若来自与当前不同的模型(切换后旧模型延迟报错),不触发应急砍尾,按当前模型重新判定。

### 4.4 D3 分段压缩:滑动窗口 map + 统一整合 reduce

入口为 `compact_session` → `_compact_generation_phase`,层级递进:

1. **预算**:`B = limit − S − prompt开销(~2k)`;段摘要输出预算 `S = min(max_output_tokens, clamp(limit×5%, 1k..4k))`。
2. **序列化瘦身**(先做,常可免分段):`_serialize_messages` 工具结果截断 2000 字符。
3. **单遍优先**:transcript ≤ B → 现状单遍(带 previous_summary,prompt 零改动)。
4. **滑窗 map**:按消息边界切段(**不拆 assistant tool_call / tool result 对**,复用现有 sanitizer/boundary 逻辑),每段 ≤ B×0.7;每段独立调现有 compact prompt(段间可并发,单段失败独立重试);首段携带旧 compact_summary。
5. **统一 reduce**:一次调用,输入 = N 个段摘要拼接,复用 compact 模板整合语义,输出最终 `compact_summary` → 现有 commit/catalog/archive 语义不变(topic 仍从最终摘要提取)。
6. **递归防护**:段摘要总量仍 > B → 再分批套一层 map,深度上限 2;仍失败维持 tail-only 降级 + hook 通知;总尝试数上限(kimi 用 5)。
7. **tail 预算绝对值化**:`keep_ratio` 语义改为 token 预算 `clamp(usable×0.25, 2k..15k)`。
8. 压缩模型 = 当轮模型(两触发面均在 turn 上下文内,ContextVar 已绑定;后台/IM 回落默认)。可选配置 `memory.compaction.model: current | default | <provider/model>` 后补。

### 4.5 D4 子代理模型:默认运行默认模型,显式 pin

- **声明面**:`AgentSpec` 增加 `model: ModelRef | None`(scope/spec.py,紧邻 `llm_provider`);`ModelRef = {provider: str, name: str}` 引用 model.yml 条目。**不复制采样字段**(顺 descriptor.py:32-36 TODO 收敛方向)。
- **物化**:池装配/scope 编译时解析为 `ResolvedModel`;`AgentTemplate.materialize` 显式声明优先:构造 `PinnedModelProvider`(`synthesize_llm_config(resolved) → create_llm_provider`,按 (provider,model) 缓存共享,忽略 ContextVar),`model_info` 用该模型档案;`AgentMaterializeDeps` 的池级默认被 pinned 值替换后继续下传——**pinned 子代理的子代理以 pinned 值为子树默认("最近显式声明生效")**。
- **缺省 = 默认模型(重定基,2026-09-24)**:`AgentMaterializeDeps.llm_provider` = `PinnedModelProvider(default_resolved)`——未配置子代理显式钉在默认模型上,与池级 `LlmDefaults`(本就取 default_resolved)一致,消除"档案写默认模型、实际跟随 ContextVar"的分叉。背景:子代理 turn 由 InboxPoller 后台任务派生,turn 级 ContextVar 从不跨任务边界,"继承调用方"从未在运行时成立(§2.4 勘误)。
- **子代理会话压缩补全**:provider 解析上移到内存构建之前,`build_session_only_memory` 接收子代理的**有效 provider**(pin 或默认 pin)——`cfg.compact` 启用时子代理内存系统获得真实 compactor(经与主路径同一 `build_session_compactor` 构造),子代理清理不再是无摘要的 tail-only 降级。
- **根 agent 与外部策略 agent 不接受 model 声明(装配期 fail-fast)**(实现定稿补充):根模型即会话可选模型,与 per-turn `ModelChoiceBindHook` 覆写语义冲突;外部代理 owns its model config。错误信息说明"pin 声明在子代理上"。
- 外部代理不动。

### 4.6 D5 后台维护代理模型策略

- compaction = 当轮模型(4.4.8)。
- experienceReviewer、session_title = 默认模型 pin:`SupplyInfra.default_llm_provider` 从 `BotModelProvider` 换为 `PinnedModelProvider(default_resolved)`,切断 create_task 继承 ContextVar 的意外耦合——后台任务稳定用默认模型,不再"顺手"用触发轮的贵模型。

### 4.7 D6 前端

- **Settings→Models**(ModelEditor.tsx):每模型 `context_limit` 输入;全局 `max_context_tokens` 降为"未设置时的默认"提示;显示有效预算 = limit − max_output;`PUT /api/config/model` 校验(max_output ≤ limit − 预留)。
- **ModelSelector.tsx**:选项带上下文限额徽标;切换到更小模型时**纯信息性提示**("上限 X,当前会话约 Y,超出时下轮自动压缩"),无任何动作(D-1)。
- **AgentForm.tsx / PoolsConfigView.tsx**:每个 agent 增加模型选择:默认项"默认模型"(重定基后语义:未配置 = 全局默认模型)+ model.yml 条目;scope 编译透传 `AgentSpec.model`。
- **压缩可视化**:复用 `CleanupTriggeredHook` → WebUI 通道渲染"上下文已压缩"卡片;可选会话 token 用量/上限指示(数据源:逐消息缓存 token_count;真实 provider usage 回填为后续增强)。

### 4.8 场景走查(行为矩阵)

| 场景 | 行为 |
|---|---|
| 前台切模型,不运行 | 零处理;前端仅信息提示 |
| 切小模型后首条消息 | turn 首次 `_build_messages` → 治理检查(当轮 limit)→ 超限 → compact_session(当轮模型摘要)→ 重建消息 → 正常调用 |
| 长 turn 中途增长 | 工具结果 append 实时触发检查(per-history 当轮预算);下一次迭代治理再兜底 |
| 子代理 pin 小模型 | fork 会话 load 后首次迭代同主代理路径;materialize 即用 PinnedModelProvider |
| 未配置子代理 | 默认模型 pin(D-5 重定基);其会话压缩摘要也由该 pin 生成(子代理内存系统补 compactor) |
| system prompt 膨胀 | 不在压缩判定内(压力求和排除 system 消息)→ provider 溢出后应急层兜底 |
| 压缩请求自身放不下 | 序列化截断 → 单遍 → 滑窗 map+reduce → 深度 2 → tail-only 降级 + hook 通知 |
| provider 报上下文溢出 | 应急砍尾(不变);sameModel 防误触 |

## 5. 备选方案与取舍

- **链式 refine vs 滑窗 map+reduce**:refine 顺序叙事好但 N 次串行、running summary 膨胀挤占段预算;map+reduce 可并发、失败域小、段摘要限长。按 D-4 选 map+reduce。
- **机械治理去留**:bot 链路退场后,0.60~0.85 区间旧工具输出不再被占位符抹掉,改由真压缩保全——信息更好、每次压缩多一次同步 LLM 调用(与 kimi 阻塞式压缩一致)。类保留在框架,服务无 memory 部署与显式 opt-in。
- **load 时检查(填 ensure_within_budget)vs 治理面**:治理面覆盖 load 后紧邻的首个 `_build_messages`,且看到装配后全量;不填钩子,少一个机制。
- **ModelInfo 携带预算字段 vs 新建 turn 级配置对象**:复用既有 per-turn 管道零新增穿线;ModelInfo 语义扩为"当轮激活模型档案"。

## 6. 收敛性自检(设计层)

1. **一关注一 owner**:会话压缩唯一编排者 = `DefaultMemorySystem.compact_session`(history 与治理均为薄调用方);阈值判定唯一函数 = 升级版 `check_cleanup_trigger`;当轮 limit 唯一来源 = `ModelInfo`(优先级链在单一 resolver 定义);采样集唯一来源 = model.yml `ModelCfg`(`AgentSpec.model` 只引用不复制)。
2. **零并行路径、零兼容 shim**:pre-LLM 持久压缩只有新治理一条路;history 直调 `cleanup_session` 的旧路径**删除**(收编进接口,不留双轨);bot 的机械治理接线**同批次移除**;`ensure_within_budget` 不填不废(保留原注释语义);应急压缩唯一 reactive 层不动。
3. **深模块**:`compact_session(context, max_context_tokens, source)` 三参数,内部吸收原 10+ kwargs 编排;`MemoryCompactionGovernance.apply` 对调用方只暴露"进消息、出消息"。
4. **删除测试**:删 `MemoryCompactionGovernance` → pre-LLM 持久压缩不存在(单 owner,成立);删 `compact_session` → 两触发面同时失能(单 owner,成立);删 `PinnedModelProvider` → 显式 pin 不存在,缺省继承不受影响(正交,成立)。
5. **契约化而非新机制**:压缩用当轮模型、子代理默认继承,均为"把现有事实行为升为显式契约",不引入第二套模型解析。
6. **无新增完成追踪/超时/重试机制**(收敛规则 3/4):分段压缩的重试属于压缩编排内部(owning layer),不在调用方包裹。

## 7. 开放问题(实现前需拍板)

1. map 段间是否留 overlap(如 512 token)保跨段语境——参考实现均不留,建议第一版不留。
2. reserve 默认 `min(20k, limit×10%)`、ratio 0.85 维持现状,是否接受。
3. kimi 式观测上限学习(413 反推 `min(config, observed)`)是否进第一版——建议 P2(需额外持久化状态)。
