# Langfuse 判定契约

Status: closed
Labels: wayfinder:grilling
Resolved: 2026-08-18 — 四项决议按推荐落定;溯源落 comment(实测推翻 metadata 假设)

## Question

三类评分来源(基准官方 verifier、judge、成本)在 Langfuse 里的统一记录契约,使 compare 一张表看全:

- score 命名与命名空间:官方基准 verdict、judge 分(含 rubric 明细)、cost_usd 各自的 score name 规范
- 溯源字段:scorer 版本串(如 `terminalbench.official.v1`、`judge.rubric.<prompt-hash>`)、report_source(judge/verifier/cost)、run 关联 —— score metadata 的固定 key 集
- 结构映射:基准 run → Langfuse dataset/experiment 的组织方式(一个基准采样 = 一个 experiment;instance = dataset item),官方 result.json 留本地、路径引用进 metadata
- 现有 12 指标 score 与新契约的共存:命名不冲突、compare 可同表

与 judge/适配器两张 ticket 互相引用但独立成文 —— 本 ticket 定契约,它们遵循。

## Comments

**Resolution (2026-08-18,用户确认"按推荐来")** — 契约 v1:

**❶ 命名规范(b:新增带前缀,12 指标裸名不迁移)**:
- 现有 12 个 trajectory 指标保持裸名(`tool_success_rate` 等)—— 已上线、compare 依赖,迁移即历史断裂
- 新增三类:`cost_usd`(成本,票 06 已定)、`judge_<dim>`(judge 分,如 `judge_rubric_overall`;rubric 明细进溯源)、`verdict_<benchmark>`(官方 verdict,如 `verdict_terminalbench`)
- **版本串不进 score name,进溯源 comment**(name 变 = 历史断裂;版本必须活在可变载体)

**❷ 溯源载体 = score `comment` 字段,JSON-in-string(实测裁定)**:
- 实测证据(2026-08-18,自托管 v4.11.0):`POST /api/public/scores` 携带 `metadata` 对象被**静默丢弃**(写成功、读回 null);ingestion score-create 同 schema;唯一可自由存储的字段是 `comment` 字符串。ClickHouse `scores.comment` 列确认持久化
- 固定 key(单行紧凑 JSON):`{"scorer": "trajectory|judge|verifier|pricing", "version": "<语义版本串>", "report_source": "counters|llm_judge|official_harness|local_pricebook", "run_ref": "<本地 run 目录相对路径>"}`
- 解析方(实现于 compare/CLI)对非 JSON comment 宽容忽略 —— 与既有 12 指标(无 comment)共存

**❸ 结构映射**:
- 一个基准采样 = 一个 experiment,命名 `{benchmark}.{run-id}`(如 `terminalbench.2026-08-19-a`)—— experiment 名即版本锚,无独立 run registry
- 基准任务集 curate 为 dataset(如 `terminalbench-2.1-sampled-20`),instance = dataset item
- 官方 result.json 等重证据留本地 gitignored run 目录,`run_ref` 指向(Trace: comment 中的 run_ref;证据:本地文件)

**❹ verdict 注入义务**:官方 verifier 结果由适配器(票 02)在 run 结束后读取 result.json 并以 `verdict_<benchmark>` score 注入对应 trace —— "判定/度量归 Langfuse"边界的直接推论;契约只定注入什么与什么名,注入机制属票 02。

**对其他票的约束输出**:票 06 实现切片 3(cost_usd score)遵循本契约 comment 格式;票 03 judge 分数命名 `judge_*` + 溯源 comment;票 02 适配器实现 verdict 注入。

**Erratum(2026-08-20 design-closure 实测复核,本机 4.11.0 events_only)**:

- **读回配方**:`GET /api/public/v3/scores?fields=core,details,subject` —— `fields` 取**字段组名**(core/details/subject/annotation),默认投影不含 comment 与 subject。实测 comment(ClickHouse 持久化)与结构化溯源 `subject:{kind,id}` 均可完整读回 → 契约读写双向闭合;**compare/CLI 等消费方必须显式带 fields 参数**(票 04 的 calibrated 标灰依赖此)。
- **experimentId 过滤语义**:v3/scores 的 `experimentId` 匹配 score 自身 `dataset_run_id` 列(仅覆盖直接挂 run 的 score);挂 trace 的 score 经 UI compare 的 trace-join 归集;CLI 时间窗聚合不受影响。
- **events_only 摄入边界**:`POST /api/public/ingestion` 仅接受 score/log 事件(trace-create 400 拒绝);trace 只能经 OTLP 创建;score 先于 trace 到达可乱序,链接自动补齐。

## v1.1 (2026-08-20)

记忆遥测命名注册表（仅增加命名，不改变 v1 行为）：

- root span 事件：`memory.cleanup.triggered`、`memory.cleanup.finished`、`memory.context.assembled`、`memory.core.updated`、`memory.consolidation.finished`。
- 会话计数器：`memory_cleanup_total`、`memory_consolidation_total`、`memory_context_assembled_total`、`memory_core_updated_total`。
- 票 10 规约器保留 score 名：`memory_compression_ratio`、`memory_write_cost_usd`、`memory_read_latency_ms`、`memory_injection_retention`；利用增量四分类使用 `memory_utilization_delta_beneficial`、`memory_utilization_delta_harmful`、`memory_utilization_delta_ignored`、`memory_utilization_delta_neutral`。

