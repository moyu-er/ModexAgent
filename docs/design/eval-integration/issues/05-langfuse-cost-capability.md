# Langfuse 成本能力调研

Status: closed
Labels: wayfinder:research
Blocks: 成本核算设计 (06)
Resolved: 2026-08-18 — 结论见 research/langfuse-cost-capability.md

## Question

Langfuse OSS v4(自托管,4.11.0)的自定义模型定价能力事实:

1. 自托管 OSS 能否为任意 provider/model id 定义单价(Models & Prices API / UI)?是 admin/org 级还是 project 级?
2. 经 OTLP 摄入的 trace(`gen_ai.usage.*` 属性)能否吃到这些自定义价格并算出 cost?匹配规则(model id 归一化、前缀容忍)如何?
3. 自定义/未知模型 id 的兜底行为是什么(0 成本?未定价标记?)
4. 与"本地价格表 + cost_usd score 注入"方案的能力对比:哪条路能覆盖 step-3.7-flash 这类自定义 provider

产出:事实清单(带文档/源码出处),直接喂给"成本核算设计"ticket。结论写入 `docs/design/eval-integration/research/` 下。

## Comments

**Resolution (2026-08-18)**: 调研完成,结论存 `research/langfuse-cost-capability.md`(对照 v4.11.0 源码 tag `84663514` + 官方文档 + changelog + issues)。要点:

1. **自托管 OSS 完全支持自定义模型定价**,project 级,UI + API 双路径,无 EE 门槛、无数量上限
2. **OTLP 摄入走同一条计价路径**,但匹配是原始字符串 Postgres 正则(无前缀剥离;大小写需 pattern 自带 `(?i)`);project 定义 > 内置默认
3. **未知模型 → cost NULL**(非 $0,无标记)
4. **cache 桶定价支持**:价格键与 usage_details 键逐字匹配;OTLP 侧 cache_read→`input_cached_tokens` 且 input 由包容数转独占数
5. **两个结构性短板**:摄入时计价(改价不回溯历史)+ 价格表不在 git(漂移风险)
6. **附带发现**:span 携带 `gen_ai.usage.cost` 时 Langfuse 完全不重算 —— 本地算钱有"正门"

对 ticket 06 的指向:本地价格表(git)为 source of truth → 同步推 Langfuse Models API(UI/trace 有成本)+ `cost_usd` score 注入(compare 有成本列、可事后聚合)。

