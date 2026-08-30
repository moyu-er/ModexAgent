# Langfuse OSS v4 自定义模型定价能力 — 调研结论

> 来源:官方文档(langfuse.com/docs)、langfuse/langfuse 仓库 **v4.11.0 tag 源码**(commit `84663514`)、官方 changelog、GitHub issues。调研日期:2026-08-18。

## Q1: 自托管 OSS 能否定义自定义模型单价? — 能,完全支持,project 级

- **无 EE 门槛**:self-hosted Open Source 计划的功能表里 "Token and cost tracking" = Yes;models 相关代码(features/models、public API)零 license 检查;166 条默认价格表本身就放在 OSS 仓库里(`worker/src/constants/default-model-prices.json`)
- **两条配置路径**:UI(Project Settings → Models,页面在 `project/[projectId]/settings/models/`)+ API(`GET/POST /api/public/models`、`GET/DELETE /api/public/models/{id}`,changelog 2024-07-03)
- **作用域:仅 project 级**(API key 即 project 级)。实例级/全局配置是开放 feature request(#14265),当前不存在
- **数量无上限**(仅 regex 合法性校验 + 同 project 内 modelName 唯一 + DB 唯一约束 `[projectId, modelName, startDate, unit]`)

## Q2: OTLP 摄入的 trace 能否吃到自定义价格? — 能,同一条计价路径

- OTLP 路由 → worker 的 OTel 处理器转成 generation 事件 → `IngestionService` 统一计价:正则匹配模型定义 → 匹配 pricing tier → `calculateUsageCosts`(逐 usageType 单价 × 数量)
- **模型名匹配是原始字符串的 Postgres 正则(`~`),无前缀剥离、无大小写归一** —— 大小写不敏感需 pattern 自带 `(?i)`;默认表条目形如 `(?i)^(openai/)?(gpt-4o)$`(自带可选 provider 前缀)。自定义 id(如 `openai/step-3.7-flash`)需自写 pattern(注意转义点号)
- **优先级**:project 自定义 > Langfuse 托管默认(`ORDER BY project_id ASC` 使非 NULL 的 project 行先命中);同优先级取 `start_date DESC`
- **⚠️ 计价只发生在摄入时**:价格变更只影响新 trace,历史数据不重算(文档明示;模型增删会清 Redis 缓存 + 10s 本地 TTL)

## Q3: 未知模型的行为? — cost 为 NULL,非 $0,无标记

`findModel` 无命中 → `modelPrices=[]` → `cost_details={}`、`total_cost=undefined`(存 NULL)。代码库中不存在 "unpriced" 标记概念(grep 零命中);文档排障指引是"添加自定义模型定义"。

## Q4: OSS vs Cloud/EE 差距? — 模型定价无差距

唯一相关缺口:无实例级配置(见 Q1)。Cloud pricing 页的 "Custom usage pricing" 行是 Langfuse 自身计费,与模型成本定义无关。

## Q5: cache 桶定价? — 支持,按 usageType 字符串精确匹配

- 默认表已含 cache 价格键:`input_cached_tokens`、`input_cache_read`、`cache_read_input_tokens`、`input_cache_creation`、`cache_creation_input_tokens`(Claude 另有 `_5m`/`_1h` 变体)
- **OTLP 归一化**:`gen_ai.usage.cache_read.input_tokens` → `input_cached_tokens`;`cache_creation` → `input_cache_creation`;且 input 按**含 cache 的包容数转独占数**(input = input_tokens − cacheRead − cacheCreation)—— 与我们 chat span 的语义一致,需注意别双计
- 自定义模型的价格键名必须与 usage_details 键**逐字相同**才命中
- 已知开放 issue #12635(OTel generation 的 cache token 历史缺口;v4.11.0 源码已含归一化处理,issue 疑似未关)

## 另一关键事实:自带 cost 优先

若 span 携带 `gen_ai.usage.cost`,Langfuse **完全不重算**——provided cost 赢,`cost_details.total` 即它。这给"本地算钱注入"留了正门,但注入的是 trace 属性而非 score。

## 对成本核算设计(ticket 06)的直接含义

1. **Langfuse 原生定价可用且能覆盖自定义 provider** —— step-3.7-flash 配 project 级模型定义 + `(?i)` pattern 即可,含 cache 桶
2. **但有两个结构性短板**:(a) 摄入时计价 → 改价不回溯;(b) 价格表在 Langfuse DB 里,不在 git —— 静默漂移风险正是本 map 要消灭的东西
3. **合理分工浮现**:本地价格表(git 版本化)为 source of truth → 同步脚本推 Langfuse Models API(让 UI/trace 自带成本)+ `cost_usd` score 注入(让 compare 表有成本列,且不受"摄入时计价"限制,可对历史 run 事后聚合)
