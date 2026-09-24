# 实现规划:per-model 上下文预算与统一会话压缩

- 设计依据:同目录 `PRD.md`(2026-09-23 定稿)
- 拆解原则:每个批次独立可验收、可提交;**行为等价重构与新行为分批**;每张 ticket 带"删除的旧路径"与"迁移的调用方"清单(收敛规则:被替换路径不得残留生产调用方)
- 测试纪律:预存失败是 bug 不是噪音,与改动同批修复提交;测试必须打真实调用面;UT 硬超时、有界验证,不反复跑慢套件

## 批次总览(依赖顺序)

```
T1 数据面(ModelCfg/ModelInfo 预算字段)
T2 compact_session 接口收编(行为零变化重构)          ← T1 后
T3 MemoryCompactionGovernance + 接线替换 + 双条件阈值  ← T2 后
T4 分段压缩(滑窗 map + 统一 reduce)                  ← T3 后
T5 子代理模型(默认模型[重定基] + 显式 pin)              ← T1 后,与 T2-T4 并行
T6 后台代理策略 + 前端                                ← T1/T5 后
```

---

## T1 per-model 预算字段(数据面)

**改动面**
- `examples/bot_project/bot/service/model_config.py`:`ModelCfg` 加 `context_limit: int | None = None`(ge 校验);`ResolvedModel` 透出;`synthesize_llm_config` 增加 clamp:`max_output_tokens = min(max_output_tokens, context_limit − reserve)`(limit 为 None 时跳过)。
- `src/modex_agent/core/capabilities.py`:`ModelInfo` 加 `context_limit: int | None = None`、`max_output_tokens: int | None = None`(frozen/extra=forbid 不变,默认 None 不破坏现有构造点)。
- `examples/bot_project/bot/service/model_choice.py`:`ModelChoiceBindHook` 覆写 `services.model_info` 时携带两个预算字段(数据源:本 turn `ResolvedModel`)。
- 池装配处(factory.py:577-580 / 748-751 / pool_construction.py:158-161):`LlmDefaults.model_info` 同步带预算字段。

**验收**
- 新增单测:context_limit 解析/继承全局;clamp 生效(limit < max_output 时);ModelInfo 新字段 round-trip。
- `pytest tests/unit/ioc/test_model_config.py examples/bot_project/tests/unit/service/ -k "model" --timeout=120`。
- **收敛检查点**:优先级链 resolver 在本 ticket 落成**唯一函数**,放**框架侧**(如 `src/modex_agent/memory/` 预算小模块,或治理文件内):`resolve_effective_budget(model_info, fallback) -> (limit, max_output)`。理由:消费方(治理、history)是框架代码,不能 import bot;bot 的"全局 max_context_tokens"本就经装配流入框架 `MemoryConfig`,故链条实为 `当轮 model_info → 池 MemoryConfig`,无需 bot 侧第三层。此后所有消费点只准调它,不得各自 if-else。

**旧路径清理**:无新增即无清理;确认没有任何地方并行新增第二种 limit 表达(检查 `/api/models` 是否需要透出 context_limit → 归 T6)。

---

## T2 `compact_session` 接口收编(行为零变化)

**改动面**
- `src/modex_agent/memory/core/system.py`:`ContextManagedMemorySystem` 加非抽象方法 `compact_session(context, *, max_context_tokens=None, source) -> CleanupResult`,默认实现返回 `CleanupResult(triggered=False)`(文档注明默认 no-op 语义与 source 枚举)。
- `src/modex_agent/memory/default_system.py`:`DefaultMemorySystem.compact_session` 落地——把 `history.py:109` 的 `cleanup_session(...)` 自由函数调用(10+ kwargs)整体迁入;新增 per-MemoryContext 在飞去重(asyncio.Lock 或 in-flight 标志);`CleanupResult`/hook 载荷带 `source`。
- `src/modex_agent/memory/history.py`:`_run_cleanup_if_triggered` 改为三步——触发判断(仍用 `check_cleanup_trigger`)→ `memory_system.compact_session(...)` → 刷新缓存。**删除对 `cleanup_session` 的直接 import 与调用**。
- 注意:`CleanupResult`(memory/cleanup.py:66)按需加 `source: str = ""` 字段。

**验收(等价重构线)**
- 现有清理套件**全绿即通过**:`pytest tests/unit/memory/test_cleanup.py tests/unit/memory/test_cleanup_hooks.py --timeout=120`。
- 新增单测:默认 no-op(基类行为);在飞去重(并发两次触发只跑一次摘要,落败方 triggered/pruned 语义符合 revision-conflict 现状);source 透传到 hook。
- **收敛检查点**:`grep -rn "from modex_agent.memory.cleanup import cleanup_session" src/ examples/` 生产代码命中数 = 0(仅 default_system.py 内部实现引用)。

**旧路径清理**:history.py 直调 cleanup_session 的路径**本批删除**,不留兼容分支(收敛规则 2)。

---

## T3 MemoryCompactionGovernance + 接线替换 + 双条件阈值

**改动面**
- `src/modex_agent/core`(MessageHistory ABC 所在):加可选 no-op 方法 `async def refresh(self) -> None`;`ListMessageHistory` 用默认;`ScopedMessageHistory`(memory/history.py:67)覆写为置空缓存。
- 新文件 `src/modex_agent/memory/compaction_governance.py`:`MemoryCompactionGovernance(ContextGovernance)`,按 PRD 4.3.2 伪码实现;回退预算用构造注入;当轮预算取 `ctx.runtime.model_info`(None 安全)→ resolver。
- `src/modex_agent/memory/cleanup.py`:`check_cleanup_trigger` 升级双条件(加 `reserve` 参数,默认 `min(20_000, limit×10%)`;向后兼容:reserve=0 退化为现公式)。
- `src/modex_agent/ioc/factories/governance.py`:`create_governance` / `create_subagent_governance` 签名增加 `memory_system=None, memory_context_factory=None`;链首插入 `MemoryCompactionGovernance`(memory_system 为 None 时不插,等价旧行为);`ContextBudgetGovernance` 保留在配置存在时(顺序:memory → budget → tool_chain_repair)。
- **正确性硬约束**:治理拿到的 MemoryContext 必须与 `load()` 为本 session 构建/缓存的**同一实例**(经 `MemorySystemContextManager` 的 session 级 context 解析注入,不要另建)——memory store 按 MemoryContext 键控,拿到异键 context 会压缩到错误会话的存储。
- `examples/bot_project/bot/workspace/wiring/stack.py` + 相关装配:注入 memory_system/context factory;**停用 bot 的 `governance.budget` 默认接线**(机械治理从 bot 链路退场,PRD D-3)。
- `src/modex_agent/memory/default_system.py` + `system.py`:`create_message_history` / `load()` 增加可选 per-history 当轮预算覆盖(runtime_info[MODEL_INFO] → history 实例)。
- `src/modex_agent/agents/react/error_recovery.py`:sameModel 防误触——溢出错误可归因到与当前不同的模型时不砍尾(需要错误负载带模型名或按请求快照比较;若 provider 错误不带模型,则退化为仅按当前 limit 重判,记录取舍)。

**验收**
- 新增单测(tests/unit/memory/):
  - 超限 → compact_session 被调一次(source="pre_llm"),返回列表 = [system]+刷新后 history;
  - 未超限 → 零改写(原 list 原样返回,token 求和成本路径);
  - triggered 但 pruned==0 → 原样返回;
  - 双条件公式:小窗口下绝对保留条件先于 ratio 触发;
  - per-history 预算覆盖:append 检查用当轮 limit,静态配置回退。
- 集成面:`examples/bot_project/tests/` 下挑一个现有 bot 装配测试验证链路组装(memory 治理在链首)。
- **收敛检查点**:
  - `grep -rn "ContextBudgetGovernance" examples/bot_project/bot/ | grep -v test` 命中数 = 0(bot 不再接线);
  - `create_governance` 与 `create_subagent_governance` 的**全部生产调用方**同批更新签名(收敛规则 1:主/子代理同链);
  - `ensure_within_budget` 保持 no-op(git diff 确认未动)。

**旧路径清理**:bot 的 governance.budget 接线移除;不留"memory 治理失败回退机械治理"的双轨分支(应急压缩已是兜底)。

---

## T4 分段压缩(滑窗 map + 统一 reduce)

**改动面**
- `src/modex_agent/agents/summarizer/session_compactor.py`:
  - `_serialize_messages` 工具结果截断 2000 字符(占位标注截断量);
  - `compact()` 分层:预算计算(`B`、`S` 按 PRD 4.4)→ 单遍(≤B,现状路径)→ 滑窗分段(消息边界,不拆 tool_call/result 对)→ map 段摘要(可并发,首段携带 previous_summary,单段重试)→ reduce 统一整合 → 深度 2 递归 → 抛出/返回降级信号;
  - 段摘要/最终摘要的 max_output 用 `S`。
- `src/modex_agent/memory/cleanup.py`:`_compact_generation_phase` 接住降级信号,维持 tail-only 现有降级;`keep_ratio` → token 预算语义(`clamp(usable×0.25, 2k..15k)`),`CleanupResult` 语义字段同步。
- 压缩调用总尝试上限(默认 5,kimi 同款)落在 compactor 编排内部。

**验收**
- 单测(用假 provider 计数):大 transcript → 分段数/调用数符合预算推导;段边界不拆 tool 对(用 sanitizer 既有测试模式);深度 2 触发条件;最终失败降级 tail-only 且 hook 通知;单段失败仅重试该段。
- 既有 compact 测试(serialize/usage/topic)回归全绿。
- **收敛检查点**:分段逻辑只存在于 SessionCompactorAgent(owning layer),`cleanup.py` 与治理不感知分段(接口不变:`compact_session` 签名零改动)。

---

## T5 子代理模型(默认运行默认模型[2026-09-24 重定基] + 显式 pin)

> **评审重定基(2026-09-24)**:代码评审证实原"缺省=继承调用方"基线认定有误——子代理 turn 由 InboxPoller 后台任务派生(inbox_poller.py:186-203),turn 级 ContextVar 从不跨任务边界,继承从未在运行时成立。经用户确认,缺省语义重定基为**全局默认模型**并显式 pin 化:`AgentMaterializeDeps.llm_provider = PinnedModelProvider(default_resolved)`;同批补全子代理会话压缩(provider 解析上移,`build_session_only_memory` 接收有效 provider 构造 compactor,与主路径共享 `build_session_compactor`)。PRD §2.4/§4.5、ADR-0050 §5、AgentForm 文案同步更新。

**改动面**
- `src/modex_agent/scope/spec.py`:`AgentSpec` 加 `model: ModelRef | None = None`(紧邻 `llm_provider`,spec.py:204);`ModelRef` frozen pydantic `{provider: str, name: str}`;scope 校验:引用必须在 model.yml 中可解析(编译期 fail-fast)。
- `examples/bot_project/bot/plugins/`(或 model_config.py 旁):`PinnedModelProvider(LLMProvider)` —— 构造期 `synthesize_llm_config(resolved) → create_llm_provider`,忽略 ContextVar;按 (provider.key, model) 缓存共享实例。
- `examples/bot_project/bot/service/pool/factory.py`:AgentMaterializeDeps 组装处(737-752)——agent 显式 `model` 时:deps 换用 pinned provider + 该模型档案(model_info/预算);**pinned 值替换池默认继续下传**(子树默认)。
- `src/modex_agent/multi_agent/template.py`:materialize 用 deps 传入值,无 agent 级 if-else(收敛点:解析只在池装配做一次,template 不感知 bot 模型表)。
- WebUI 后端:`/api/models` 已有;scope 编译入口接受并透传 `model` 字段(与 T6 前端衔接)。

**验收**
- 单测:缺省(无 model 声明)→ 共享 provider(继承调用方,现状不变,pin 行为零变化);显式 pin → materialize 产物用 PinnedModelProvider 且 model_info 为该模型;pin 嵌套 → 子树默认 = 最近显式声明;引用不存在 → 编译期报错。
- **收敛检查点**:`AgentLLMConfig` 的采样字段(descriptor.py:26-48)不新增不扩展——pin 只引用不复制(TODO 收敛方向);`grep -rn "PinnedModelProvider" examples/bot_project/bot/ | grep -v test` 调用点集中在池装配一处。

**旧路径清理**:无(纯新增能力 + 契约化现状)。

---

## T6 后台代理策略 + 前端

**后端**
- `examples/bot_project/bot/plugins/`(experience capability 装配):`SupplyInfra.default_llm_provider` → `PinnedModelProvider(default_resolved)`;确认 reviewer 不再经 ContextVar 继承当轮模型(补一条测试:turn 选贵模型后触发 review,review 请求模型 = 默认模型)。
- `/api/models` 返回加 `context_limit`;`PUT /api/config/model` 校验(max_output ≤ limit − 预留;全局 max_context_tokens 降级语义提示)。

**前端**(webui/src/)
- `settings/ModelEditor.tsx`:每模型 `context_limit` 输入 + 有效预算展示 + 保存校验联动。
- `components/ModelSelector.tsx`:选项限额徽标;切换到更小模型且当前会话超限 → **纯信息性提示**(无动作,PRD D-1)。
- `settings/pools/AgentForm.tsx`:agent(含子代理)模型选择,默认项文案"跟随会话(继承调用方)";提交走既有 `PUT /api/scope/model` 通道。
- 压缩卡片:复用既有 CleanupTriggeredHook → WS 事件通道渲染"上下文已压缩"(若 UI 侧缺事件消费则最小补一个消息卡片组件)。

**验收**:后端单测 + 前端构建通过(`npm run build`);信息性提示不触发任何后端调用(网络面板核验)。

---

## 总体验收清单

1. `pytest tests/unit/memory/ tests/unit/agents/ -k "compact or governance or cleanup" --timeout=180` 全绿。
2. `pytest examples/bot_project/tests/unit -k "model or pool" --timeout=180` 全绿。
3. 行为矩阵(PRD 4.8)逐行有对应测试或人工核验记录。
4. 收敛 grep 面(见各 ticket)全部归零或收敛到唯一调用点。

## 收敛自检清单(实现层,提交前逐项过)

- [ ] 压缩编排唯一入口 `DefaultMemorySystem.compact_session`;history 与治理均为薄调用方,无第三处直调 `cleanup_session`。
- [ ] 阈值判定唯一函数;双条件公式只在 `check_cleanup_trigger` 定义一次。
- [ ] 当轮预算解析唯一 resolver;治理/history/装配三处共用,无一各自展开优先级链。
- [ ] `ensure_within_budget` 未被填充(保留原注释与 no-op);pre-LLM 持久压缩仅 `MemoryCompactionGovernance` 一条路径。
- [ ] bot 链路不再接线 `ContextBudgetGovernance`;框架类保留但无 bot 生产调用。
- [ ] `MessageHistory.refresh()` 为可选 no-op 默认;无调用方对不存在的能力做 hasattr/getattr 探测。
- [ ] 分段逻辑封闭在 SessionCompactorAgent;`compact_session` 签名与治理契约在 T4 前后零变化。
- [ ] 子代理 pin 只引用模型条目;`AgentLLMConfig` 采样字段未扩展;缺省路径行为与改动前逐字节等价(现有测试作证)。
- [ ] 后台代理(reviewer/title/compaction)模型策略各只有一处定义;无 ContextVar 偶然继承残留(reviewer 补测锁定)。
- [ ] 无新增超时/重试/watchdog 包裹(收敛规则 3/4);分段重试与在飞去重均在 owning layer 内部。
- [ ] 每张 ticket 的"旧路径清理"项执行完毕,git grep 复核。

## ADR 计划

随 T2/T3 提交一篇 ADR《统一会话压缩接口与 per-model 上下文预算》:决策(一接口两面一链、机械治理退场、切模型零处理)、后果(压缩成为同步成本、0.60~0.85 区间行为变化)、与 opencode/kimi/pi 的对照引用本 PRD。按 ADR 治理规则:并入相关既有 ADR 或新立,不并行版本。
