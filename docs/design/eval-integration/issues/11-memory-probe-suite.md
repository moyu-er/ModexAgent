# 记忆探针套件(人格生成器 + 五型探针)

Status: closed
Labels: wayfinder:grilling
Blocks: Judge 记忆特化与校准 (12)
Blocked-by: 长期记忆层启用裁定 (13)
Resolved: 2026-08-20 — 完整设计 v1 用户认可(含价值论证 + 运转方式说明)

## Question

记忆探针数据怎么生成、怎么注入真实管线、怎么评分?题型集合是什么?

调研输入(research/memory-eval-landscape.md):

- **真值可靠性**:LLM 直造 ground truth 复杂题型 <40% 正确 → 事实必须程序化采样(人格/属性/事件图),LLM 只做表面渲染(业界模拟器共识模式);难度可用噪声比例调
- **业界产品记忆评测三目标 → 题型映射**:事实召回 / 偏好遵循 / 时效性 —— 与公开五能力分类合并成我们的题型集
- **候选五型探针**(合并公开能力分类 + 生产五类 CI 探针模式):
  1. 抽取(单跳/多会话)
  2. 时序(时间敏感信息)
  3. 知识更新(旧事实必须输给新事实 —— 反事实编辑模式)
  4. 弃答(没存过的事实必须承认不知道,防幻觉)
  5. 跨用户隔离(A 用户事实不得漏给 B —— 多用户 QQ/Telegram 现实直接相关)
- **harness 形态**(主流记忆评测 harness 流水线模式,直接复用):Ingest → Query → Evaluate;会话逐轮注入带时间戳;摄入断点续传;**predict/evaluate 分离 + resume**(judge 迭代不重跑检索,成本控制);统一结果 JSON(metadata + 逐题记录,按题型聚合)
- **确定性评分成分**:每题标注依据事实 ID → 证据召回 Recall@k(无 judge,证据标注模式)+ judge 判答案(进票 12);证据可见性依赖票 09 的 memory.read span(软依赖)
- **饱和防线**:任务量/时间深度/实体多样性必须超出单 prompt(128k+ 长上下文可无记忆解掉浅基准)

待 deliberation 的子问题:
- 生成器规模(实体数/属性数/会话数/探针数;业界参照:冻结校准集 ~100-200 探针)
- 注入路径:走真实 MemorySystemContextManager.load 逐会话摄入(触发真实压缩/巩固),不是离线直写存储 —— 确认
- 探针库冻结与版本化纪律(何时允许再生成/扩容)
- 成本边界:生成(一次性)+ 摄入(真实管线,$0 LLM?压缩/巩固是否触发 LLM 调用取决于配置)+ 逐题查询

## Comments

**Resolution (2026-08-20,用户认可完整设计)** — 对标业界、轻量化的记忆考卷;价值定位 = 记忆线的"打分"出处(09 测机器在转、10 测转得省、11 测记得对不对)。

**世界模型与题型**:程序化世界采样(WorldSpec:Fact{fact_id, attribute, value, valid_from, superseded_by, surface_refs}/Session/Probe 冻结 Pydantic);**每句渲染文本带 fact_ids 标注**(LLM 只做双侧表面渲染 —— 用户侧+助手侧,无权发明事实)。五型:抽取/时序/知识更新(判官区分新旧值,旧值=陈旧失败)/弃答/跨用户隔离(4 对人格共享属性碰撞域;**双信号**:①确定性污染检查 —— B 的注入上下文含 A 证据表面文本即红,从 memory.read span 读出,零判官 ②答案层泄漏判官)。

**流水线**:Generate(钉死 RNG 种子,一次生成冻结)→ Ingest(逐会话真实写路径 ScopedMessageHistory.append + 时间戳;评测 preset `max_context_tokens=32k` 收缩窗口使 50-80k token 历史/人格自然触发 2-3 轮 cleanup;**harness 显式 DreamEngine.run() 至 cursor 耗尽**;然后快照工作区,query/judge 重跑不重摄入)→ Query(**MemorySystemContextManager.load(query=question) 纯组装零 LLM + 单次裸 LLM 作答 —— agent/ReAct 不参与**,归因只反映记忆管线;利用增量 30 题双臂)。predict/evaluate 分离 + 断点续传 + resume。

**评分**:确定性优先 —— `memory_retrieval_recall`(证据表面文本归一化匹配于最终注入上下文,替代业界 top-k 检索面的适配)+ 隔离污染标志(均无判官);`memory_probe_<type>`(判官,票 12);利用增量四分类(Beneficial/Harmful/Ignored/Neutral)采集在本 harness、指标规约归票 10。落契约 08 v1.1:experiment=`memory-probes.{run-id}`,instance=probe,comment 溯源含 suite_version+harness-hash;**分数注入义务在本票 harness**。

**规模与成本**:8 人格 × ~35 会话 × 50-80k token × 跨 8 周;125 题(25/型)+ 30 双臂;每轮 ≤$5(生成 $1-2 一次性/摄入 $1-3/查询 $0.5-1/双臂 $0.3)。

**冻结纪律**:v1 冻结进 git(真值可审计、回归可比);再生成=新版本号,v2 并行短存保回归连续;判官校准集(12)从冻结库抽样;扩容(25→50 每型)待 12 校准稳定后。

**运转方式**:CLI 三命令(generate 一次性 / run 每轮+resume / judge 独立重跑);消费在 Langfuse(experiment compare:r1 vs r2 看杠杆调整效果);人工仅票 12 校准 pilot 标 ~10-30 题。CI 零成本切片(快照重放查召回+污染,无 LLM)归票 10。agent 不参与答题 —— "agent 能否用好记忆"归票 14 e2e 哨兵。

**实现切片**:①schema+generator(sampler/renderer)②harness(ingest/snapshot/query/resume)③确定性评分 ④judge 对接接口(12 实现)⑤CLI ⑥冻结库生成与 manifest ⑦score 注入(契约 08 v1.1)。

**Erratum(2026-08-20 design-closure 实测复核,本机 4.11.0 events_only)**:

- **experiment 联动机制(切片⑦补)**:predict/evaluate 分离 + resume 与 SDK `run_experiment` 的内联 evaluator 语义不兼容 → 自建 runner。联动配方:SDK `create_dataset`/`create_dataset_item` 建冻结探针数据集(4.11.0 实测可用)→ 每轮 `POST /api/public/dataset-run-items {runName, datasetItemId, traceId}` 取稳定 experiment id(**注意:events_only 下该端点是空操作桩,仅用于取 id,不写数据**)→ 每题的作答 span(裸 LLM 调用,harness 自发 OTLP)携带 `langfuse.experiment.{id,name,dataset.id,item.id}` 属性 → OTLP 摄入合成 run item → experiment 在 compare 出现;`memory_*` score 注入挂该 span 的 traceId。
- **读回**:score 溯源 comment 的读回按票 08 Erratum 配方(`v3/scores?fields=core,details,subject`)。

**v1 明确不做**:容量曲线/多 cutoff 检索曲线/百万 token 规模/>2 跳推理/判官 panel/生产流量采样 —— 雾区或二期。
