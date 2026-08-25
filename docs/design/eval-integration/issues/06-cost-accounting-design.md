# 成本核算设计

Status: closed
Labels: wayfinder:grilling
Resolved: 2026-08-18 — 四项决议全按推荐落定(见 Comments)

## Question

定成本核算的落地方案(依赖调研结论):

- 本地价格表(首选兜底已定):git 版本化、四桶语义(input/output/cache_read/cache_write,cache 折价系数)、体积 <10MB、从 chat span 已有的 token 属性(含 cache 桶)计算;模型 id 前缀容忍匹配;未定价模型诚实标记而非记 $0
- 计算位置与注入:按 experiment/run 聚合 cost/task,以 `cost_usd` score 注入 Langfuse,使 compare 表直接出现成本列;聚合走 LangfuseTraceQuery 读回还是 turn 内 stash,二选一并说明理由
- 与 Langfuse 原生定价(若调研支持)的分工:原生算它认识的模型,本地表兜底,还是全走本地
- 采样预算对齐:$15/轮 约束下,成本报表的触发粒度(手动)

Blocked-by: Langfuse 成本能力调研

## Comments

**Resolution (2026-08-18,用户确认"按推荐来")** — 四项决议:

**❶ 架构分工 = (B) 双源,分两期实现**(最终形态):本地价格表为单一真源(计算 + `cost_usd` score 注入进实验对比),同时把价格表同步推 Langfuse Models API 让原生 UI 显示成本。同源计算保证两边数字一致。若出现"两边对不上"的 bug 倾向,降级为 (A) 全本地。

**❷ 聚合粒度 = 逐轨迹(turn 级)注入 + CLI 聚合展示**:`cost_usd` 与 12 指标同批注入(同一 `L2ScoreInjector`,同一 fire-and-forget 容错)。实验级成本 = compare 对该实验全部 `cost_usd` 求和/均值 —— score 即存储,无需 LangfuseTraceQuery 读回、无需 turn stash。**语义钉死:turn 级 `cost_usd` 是该轮增量**,非会话总成本;会话级汇总由 UI/CLI 聚合呈现(与 12 指标同语义)。翻转条件:若需会话总成本语义,改会话末单次注入(失去逐轮可比)。

**❸ 价格表 = (c) 两层**:框架内置 `src/modex_agent/trace/prices.json`(常用模型小表,<50KB,远低于 10MB 上限)+ bot_project `config/model_prices.yml` 覆盖/扩展(自定义 provider 在业务侧,框架不认识)。匹配语义照调研结论:正则(`(?i)`)+ 前缀容忍;未定价模型 → score metadata 带 `unpriced: true` 诚实标记,不记 $0。

**❹ 分期 = 先 A' 最小核**:一期 = 两层表 + 逐轮 `cost_usd` score + compare 成本列;二期(视使用频率再定)= Models API 同步脚本 + `gen_ai.usage.cost` trace 属性(注意:该属性一旦携带 Langfuse 不再自算,二期引入时与同步脚本二选一或明确优先级)。

**实现切片(交实现会话)**:
1. 框架 `trace/pricing.py`:PriceBook 加载(两层合并)、`(?i)` 正则匹配、`usage_cost(chat span 四桶属性) -> float | None(unpriced)`
2. bot_project `config/model_prices.yml` + 内置 prices.json 初始表
3. `RootSpanHook` 或 score 注入路径:第 13 个 score `cost_usd`(溯源按契约 08 走 score comment JSON-in-string:`{unpriced, price_source, model_id}` —— v4 实测 metadata 字段被静默丢弃,见票 08 Erratum)—— 与 12 指标同批
4. eval CLI `compare` 加成本列(求和/均值)

依赖链:实现切片 3 涉及 score 命名,须与工单 08(Langfuse 判定契约)的 score 命名规范对齐 —— **08 先于实现**。

