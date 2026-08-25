# 记忆遥测插桩(memory spans + 指标 hook)

Status: closed
Labels: wayfinder:grilling
Blocks: 确定性组件指标 (10)
Resolved: 2026-08-20 — 用户认可形态/职责切分,并纠正数据真源(OTel collector 唯一,本地 JSONL 属遗留)

## Question

框架记忆系统需要哪些遥测才能支撑记忆评测?哪些插桩点、哪些 span、哪些计数器,以及判定契约(v1)如何扩出 `memory_*` 类?

背景事实(2026-08-20 二次探查,scope-converge 插件化架构下重新钉死):

- **hook 启用双通道**(ioc/configs/hooks.py 的 HookConfig/HooksConfig 已是遗留死代码,零消费者):
  ① roster 增量通道 —— 模板/pool YAML `hooks: [name|+name|-name]` + `hook_configs:`,省略键 → `spec.hooks=[]` = **默认关**;名字须已注册,未注册装配时 fail-loud
  ② 代码接线通道 —— 生产默认在此(`_wire_main_pipeline`、`DefaultAgentFactory` 等),各带 roster 名去重守卫(roster 引用优先)
- **trace 完全绕过 HOOK 插槽**:`DefaultAgentFactory.create_agent`(multi_agent/factory.py:447-456)从 `bot_config.yml observability:` 块(经 `${ENV}` 插值)直连 `build_trace_hooks`;`trace_backend=off` 或 store=None → 空表;无 TRACE 插槽(10 插槽闭集,加需新 errata)
- **CleanupMetricsHook 预置双路径**:代码接线默认开(factory.py:764 每 pool + template.py:260 每 native 子代理,持续写 cleanup.jsonl)+ 插件工厂 opt-in(仅 native_sub);无出厂配置引用工厂路径 —— 记忆遥测设计不得引入第三条路径
- MemoryHookPoint 仅 2 值(CLEANUP_TRIGGERED/FINISHED),派发点仅 cleanup.py:464/585;read/write/consolidate 无 hook 点
- MEMORY_SYSTEM 插槽 = 整系统替换,默认空;与 roster memory hook 互斥(native_core.py:219-226 ValueError)
- 五个插桩 seam 原样存活:FullInjectionPolicy.assemble(injection/full_injection.py:45)、CoreMemorySearchStrategy(core_memory_search.py:21)、ScopedCoreMemoryManager.apply_update(layers/core.py:136)、_maybe_consolidate(layers/core.py:198)、DreamEngine(consolidation/dream_engine.py:26)
- eval harness 绕过装配直建 AgentRuntimeServices(sanctioned,确定性);新组件要触达 pool agent 必须走插件/装配路径
- 契约 08 扩展:`memory_*` score 类随本票决议落定(命名进契约,溯源 comment 同格式)

## Comments

**Resolution (2026-08-20,用户认可 ❶❸ + 纠正 ❷)**

**形态(新建,不改造旧物)**:新建 `MemoryTraceHookFactory(MemoryHookFactory)`,注册名 `memory_trace`,进 `register_default_hooks`(官方扩展配方);hook 持有 trace store,`trace_backend=off` 或 store=None → no-op。不占 MEMORY_SYSTEM 插槽(整系统替换语义,与 roster memory hook 互斥);不在框架记忆代码直发 span(保持记忆/观测分层);**不复用、不改造 CleanupMetricsHook** —— 新功能新 hook。

**hook point 扩展**:框架新增 3 个 MemoryHookPoint,派发点落在已存活 seam:
- `CONTEXT_ASSEMBLED`(read)@ FullInjectionPolicy.assemble —— payload 带 section 级 provenance(检索全量/实际注入/被裁剪,逐 section token+优先级;注入结果需携带 provenance,现只产出 prompt 串)
- `CORE_MEMORY_UPDATED`(write)@ ScopedCoreMemoryManager.apply_update —— MemoryUpdate(模式/目标/内容)+ 幂等结果 + 来源标签
- `CONSOLIDATION_FINISHED`(consolidate)@ DreamEngine / _maybe_consolidate —— 消费条数、前后 token、压缩比
- CLEANUP_TRIGGERED/FINISHED 已有,payload 已足(CleanupResult + reason + managers),span 直接吃其字段

**启用面(默认关)**:roster 增量通道 —— 模板/pool YAML `hooks: [+memory_trace]`(可选 `hook_configs`);省略键 = 不实例化 = 零负担。eval harness 显式装配。

**数据真源(❷ 纠正落定)**:OTel collector → Langfuse 是唯一观测方案;本地 JSONL(cleanup.jsonl、file-backend spans.jsonl)属遗留。`memory.cleanup` span 承接 CleanupMetricsHook 的全部数据职责;**CleanupMetricsHook + 两处代码接线默认注册(bot/service/pool/factory.py:764、multi_agent/template.py:260)按遗留退役删除**(收敛纪律:不留兼容垫片、不保留并行 JSONL 真源);消费链 `bot/eval/metrics.py` 改读 span 的改造归票 10 规约器,**退役与改造同批落地**(09⑤+10 绑定执行)。

**职责切分(❸)**:09 交付 = span 事件 + 会话级计数器(MetricCounters 模式,与 12 指标同批注入);`memory_*` 分数(证据召回/利用增量/压缩三轴/探针分)由票 10 规约器与票 11 harness 注入 —— span 是事实源,分数是派生物,注入义务在 10/11。

**实现切片**:① 3 个 hook point + payload(MemoryHookContext 族扩展)② MemoryTraceHook(span 发射,BaseTraceHook 模式,HookErrorPolicy.LOG)③ 工厂注册 + no-op 门 ④ cleanup span 字段对齐(完整覆盖 CleanupMetricRecord)⑤ CleanupMetricsHook 退役(与票 10 的 metrics.py 数据源改造同批)⑥ 契约 08 v1.1 增补 `memory_*` 事件与计数器命名。

**Erratum(2026-08-20 design-closure 实测复核)**:

- **payload 增携耗时与 LLM usage**:票 10 写路径成本(逐项 LLM 调用数/token/时延)的数据源在原 payload 规约中缺失——现状:compact/巩固的 LLM 调用走 `ScopedFileAgent._run_agent`(无 runtime services、无 trace hook,usage 在 provider 响应处丢弃),`compact()→str` / `consolidate()→bool` 均不回传 usage,CleanupResult 也只含内容级 token。**最小框架改动**:`SessionCompactorAgent.compact` 与 `CoreMemoryConsolidator.consolidate` 返回值扩展携带 usage(调用数/输入输出 token/时延),CLEANUP_FINISHED 与 CONSOLIDATION_FINISHED payload 相应增字段,CONTEXT_ASSEMBLED payload 增 assemble 耗时——记忆层出事实、hook 发 span,分层不变。
- **span 拓扑决议(消歧)**:memory span = 独立 root span(独立 trace),携带 `gen_ai.conversation.id` session 属性(session 链路经 Langfuse extractSessionId 已实测成立);评测场景叠加 `langfuse.experiment.*` 属性归属 experiment(与票 02 Erratum 同机制)。
