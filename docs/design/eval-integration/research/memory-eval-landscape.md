# 记忆评测景观:基准、实现与可借鉴模式

> 票 09-14 的调研基础(2026-08-20)。来源:公开基准论文/仓库、主流 OSS 记忆实现、框架内部探查。
> 纪律:本文不引用任何临时参照项目;借鉴模式以通用名描述。

## 1. 基准景观(任务分类法与规模)

| 基准 | 测什么 | 形态 | 指标 | 规模 | 来源 |
|---|---|---|---|---|---|
| LongMemEval (ICLR'25) | 五能力:抽取/多会话/时序/知识更新/弃答 | 多会话历史增量摄入后提问;S≈115k tokens,M≈1.5M | LLM-judge 正确率(按题型)+ 证据会话召回 | 500 人工审校题 | arxiv 2410.10813 |
| LoCoMo (ACL'24) | 长程对话记忆:单跳/多跳/时序/开放域/对抗 QA | 双 LLM 人格按人格+事件图对话,人工编辑 | F1 + 检索 Recall@k(证据 dia_id 标注) | 10 组对话,~600 轮/16k tokens/≤32 会话,~1.5k-2k 题 | aclanthology 2024.acl-long.747 |
| MemBench (ACL'25 Findings) | 事实+反思记忆 × 参与/旁观场景 | 模拟时序会话+噪声;选择题 | 正确率 / 证据 Recall@10 / 容量曲线(10k→100k tokens)/ 读写时延 | 全量 ~51k 会话/39k 题,采样集 ~1k | aclanthology 2025.findings-acl.989 |
| MemoryAgentBench (ICLR'26) | 四胜任力:精确检索/测试时学习/长程理解/选择性遗忘 | 长文重切为时间序块增量喂入("一次注入多次查询");FactConsolidation 用反事实编辑 —— 新事实必须胜过旧事实 | 子串/精确匹配、Recall@5、LLM-judge | 2071 实例,103k-1.44M tokens | arxiv 2507.05257 |
| BEAM (ICLR'26) | 10 能力(LongMemEval 五 + 矛盾解决/事件排序/指令遵循/偏好遵循/摘要) | 自动生成连贯单用户对话+人工验证探针;128K-10M 四档 | LLM-judge | 100 对话 2000 题 | arxiv 2510.27246 |
| MemSim/MemDaily (NeurIPS'25) | 事实记忆可靠性;6 题型(简单/条件/比较/聚合/后处理/噪声) | 贝叶斯关系网采样事实→LLM 仅改写;噪声比例调难度 | 正确率 / Recall@5 / 响应时延 / 写入时延 | 11 实体/73 属性,可扩展 | arxiv 2409.20163 |
| MemoryArena (2026) | 多会话**相互依赖的 agentic 任务**(后半任务依赖前半蒸馏入记忆) | 人工链式任务 | 任务成功率 | 766 任务 4 域 | arxiv 2602.16313 |

关键教训:
- **LLM 直造真值不可靠**:复杂题型(聚合/比较)<40% 正确 → 事实必须程序化采样,LLM 只做表面渲染(MemSim)
- **饱和风险**:128k+ 长上下文模型可无记忆解掉大量"记忆基准" → 任务量/时间深度/实体多样性必须超出单 prompt(arxiv 2602.19320);agentic 相互依赖任务上,近饱和模型掉到 40-60%(MemoryArena)
- **组件分解是 2025-26 最硬结论**:3×3 写×检索因子实验 —— 检索方式差 20pp,写策略只差 3-8pp;检索精度与最终正确率 r=0.98;原始分块 ≥ 昂贵抽取;token F1 对 20pp 差距不敏感,必须 LLM-judge(arxiv 2603.02473)。换 embedding 一项 +6.2pp;某商业记忆系统在 2/6 题型追平朴素 RAG 但写路径 50× 成本(arxiv 2606.29914)

## 2. Judge 记忆特化风险

- **参数知识覆盖**:参考答案与 judge 内部常识矛盾时,judge 无视参考按自身信念打分,CoT 反而加剧 —— 知识更新探针必然含这种矛盾,prompt 必须显式"检索到的记忆是唯一事实源"(arxiv 2601.07506)
- **证据锚定**:rubric 编译为锁定清单 + 必须逐字引用证据,无引用封顶判负(RULERS, arxiv 2601.08654)
- **失败模式电池**:GroUSE 7 类失败 144 单测;judge prompt/模型变更必须重跑(COLING 2025)
- 二值 pointwise 最可靠(翻转率/偏差均最优)—— 与本 map 票 03/04 的选择一致(arxiv 2606.13685)

## 3. 主流实现工程模式

### Mem0(mem0ai/memory-benchmarks;主仓库 `evaluation/` 为 submodule 指向它)
- 目录:`benchmarks/{locomo,longmemeval,beam}/run.py + prompts.py` + `benchmarks/common/{llm_client,mem0_client,metrics,schema,utils}.py`;结果 JSON 落 `results/`
- 流水线:Ingest → Search → Evaluate;**1 轮 1 次 add() 带 session 时间戳**;摄入断点续传(per-chunk checkpoint)
- 逐题:search top-k=200 → cutoffs(10/20/50/200)逐档 → answerer 生成 → judge 结构化输出 `{label: CORRECT/WRONG, reasoning}` → 1/0;judge 可选用证据(LoCoMo evidence dia_ids)
- **--predict-only / --evaluate-only / --resume**:检索阶段与评审阶段分离 —— 成本控制 + judge 迭代不重跑检索(直接复用的模式)
- 统一结果 JSON:metadata(模型/cutoffs/时间)+ 逐题记录;按题型 × cutoff 聚合

### Graphiti(getzep/graphiti)
- `tests/evals/eval_e2e_graph_building.py`:LongMemEval oracle 数据 → 会话作为 episode 增量摄入(reference_time 取 haystack_dates)→ **基线 vs 候选**记忆构建对比,judge 判 `candidate_is_worse`(布尔 + reasoning);基线缓存 `baseline_graph_results.json`
- **组件回归评测形态**:评的是记忆构建管线本身(改 prompt/模型后是否变差),不是端到端答题 —— 票 09/10 的直接参照
- judge prompt 在 `graphiti_core/prompts/eval.py`(EvalResponse: is_correct + reasoning);evals **不进 CI**,手动跑

### Zep(getzep/zep-papers)
- DMR 评测 notebook:dmr_grader = gpt-4o-mini, temp=0, 结构化 `Grade{is_correct}`;基线(全文/摘要)+ 检索环(边+节点、cross_encoder/rrf 重排、limit 20)

### Letta(letta-ai/letta)
- 主仓库**无标准化记忆基准代码**(issue #3115 确认);`integration_test_sleeptime_agent.py` 测 sleep-time agent;sleep-time 实验在独立仓库(AIME/GSM stateful,JSONL 进出)
- 博客路线:把对话历史放文件,agent 用 search_files/grep 工具检索作答(74.0%,GPT-4o-mini);"Memory Benchmark" 声称**动态上下文中即时生成记忆交互**(on-the-fly)—— 与探针生成器同思路

### LangMem(langchain-ai/langmem)
- 仓库内**无评测 harness**;评测故事 = LangSmith datasets;仓库测试用 **VCR cassette**(录制 HTTP 交互回放,确定性单测)—— 记忆抽取逻辑的 CI 测试形态

### OpenAI(ChatGPT "Dreaming" 记忆,2026-06)
- 评测三目标:**事实召回**(用上用户相关上下文)/ **偏好遵循** / **时效性**(时间敏感信息保持最新)
- 内部数字:事实召回 41.5%(2024)→ 67.9%(2025)→ 82.8%(2026);偏好遵循 71.3%;时效 75.1%
- 构造法:"要求模型回应一个需要召回用户事实的 prompt,正确使用相关上下文即奖励" —— 与探针形态一致;详细方法未公开
- 来源:openai.com/index/chatgpt-memory-dreaming(镜像 thenextinput.com)

### CI 形态总览
- **没有任何主流仓库在 CI 跑 LLM-judged 记忆基准**(Mem0/Graphiti/Letta/LangMem 均手动)
- CI 可行模式:(a) 确定性组件切片(脚本化运行 + 事件流规约 + 比值断言);(b) cassette 回放;(c) 曾有第三方 harness(owletto,仓库现已 404)以 JSONL-over-stdin 适配器协议(reset/setup/ingestScenario/retrieve/dispose)+ GitHub Actions 跑基准并上传 JSON/Markdown 工件 —— 适配器协议模式值得记录
- 逐层回归切片的必要性:聚合分数掩盖单层崩塌(聚合 -2~-6pp 时责任层已 -25~-91pp)(arxiv 2606.11686)

## 4. 通用借鉴模式(不具名)

1. **压缩三轴从事件流规约**:峰值有界性(peak/预算)、密度(保留分数)、开销(转录膨胀)+ 策略归因 —— 录制即可算,零新插桩
2. **append-only + recall 工具 = 可恢复性可机械验证**:原件不删,被压缩内容可按摘要引用的索引取回 → "压缩丢没丢信息"成为可测断言
3. **效果即测试**:脚本化确定性 agent,断言压缩组峰值比 <1.2 vs 基线 >1.7、每次 fold 严格变小、任务消息永不被 fold
4. **消融臂**:每实例全新 agent 上下文,仅持久记忆跨界;臂编码进 experiment 名 → 外部 harness 打任务分,因果隔离干净
5. **防毒/防回声**:蒸馏时剥离 Outcome/Evaluation/Score 段(评测标签不得入记忆污染后续跑);回放跳过记忆自身注入的消息
6. **no-op 优先的蒸馏** + 结构化改写守卫(超长/截断/抹除拒绝,写 durable 拒绝标记)
7. **生产五类 CI 探针模式**:召回探针 / 陈旧性(新事实必须胜)/ 跨用户污染 / 精度召回@k / 负例弃答(channel.tel 博客)

## 5. 对票面的映射

| 票 | 主要输入 |
|---|---|
| 09 记忆遥测插桩 | Graphiti 组件回归形态;框架 13 个已定位插桩点;7-span/MetricCounters/L2ScoreInjector 既有模式 |
| 10 确定性组件指标 | 压缩三轴/写路径成本/利用增量;逐层切片教训;CI = 确定性切片(业界无人 CI 跑 judge 基准) |
| 11 记忆探针套件 | 程序化真值生成;Mem0 harness 流水线/断点/predict-evaluate 分离/统一结果 JSON;OpenAI 三目标 → 题型;五类 CI 探针模式 |
| 12 judge 记忆特化 | 参数覆盖防护;证据锚定;GroUSE 电池;复用票 04 校准 |
| 14 e2e 哨兵 | MemoryArena 相互依赖任务教训;消融臂模式 |
