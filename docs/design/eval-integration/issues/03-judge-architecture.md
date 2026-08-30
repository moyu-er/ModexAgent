# Judge 架构设计

Status: closed
Labels: wayfinder:grilling
Blocks: Judge 校准门槛 (04)
Resolved: 2026-08-18 — 完整设计落定(见 Comments),含联网调研实证

## Question

设计 LLM-as-judge 的实现形态,基准线为已验证的 rubric-judge 模式,但用我们的优势落地:

- 形态:单次阻塞 rubric 评审(每条 rubric 二值判定 → 加权 → score),judge 以**被 trace 的 agent run** 实现 → judge 的完整 prompt/response 自动经 OTel 进 Langfuse(天然可审计)
- 确定性:provider 层 + 请求层双层 temperature=0;`JUDGE_*` 独立环境变量(回退主模型配置)
- 评分对象:候选的 trajectory + 产物(输入脱敏:gold rubric 对被评 agent 不可见)
- 判决落库:以 score 注入候选 trace(复用 L2ScoreInjector 模式),score metadata 携带 judge prompt 版本串与 rubric 权重 —— prompt 版本化是超出基础 rubric-judge 形态的增强
- 解析韧性:判决 JSON 解析失败逐条置 NA,永不崩;judge 未产出 → 明确的 fallback 语义
- 与 world_assertions 的关系:judge 补主观维度(清晰度/遵循度/完整性),不替代程序化断言

产出:设计决定 + 实现切片清单(交 /implement 类会话执行)。

## Comments

**Resolution (2026-08-18,用户确认)** — 设计 v1。证据基础:rubric-judge 参考形态(单次评审/二值判定/温度钉死/env 分离/解析韧性/全量审计)+ 联网实证(公开 LLM 评审偏差研究、大规模多评审验证协议、公开基准官方评审实践、业界评测指南)。

**A1 形态**:单次阻塞 rubric 评审(一次调用评全部 rubric),外面包最小 trace 包裹(一个 chat span,独立 trace_id)→ judge 的完整 prompt/response 经 OTel 进 Langfuse,天然可审计。升级路径(校准暴露问题时启用):逐维度隔离调用(防维度污染)/ agentic judge(带工具验证产物)。

**A2 评审上下文**(按证据强度排序,且 prompt 中明示证据可信层级——"看工具原始输出,不看 agent 自述"):① 任务原文 ② world_assertions 判定结果(最高可信)③ 完整 trajectory ④ 产物摘录(每件 ≤8KB,截断显式标记)。rubric 对 judge 可见,对候选 agent 不可见(脱敏在任务定义层)。

**A3 Rubric 库**:集中库 `evals/judge/rubrics/` 按类别分集,任务引用 `rubric_set`;版本 = 库内容 SHA 前 8 位进溯源 comment。每条 rubric **二值判定(MET/UNMET)+ justification**,加权聚合;维度必须正交可独立判定(校验:非重叠、权重和=1);写法要求"陌生人能照着打分"。显式出口 `CANNOT_ASSESS` 防幻觉;解析失败逐条 NA 永不崩;NA 条目按 0 计入总分并在 comment 标 `na_count`(诚实降分不静默剔除)。初始集 `general-agent` 五维:task_completion(高权重)/ empirical_verification(是否实际验证过产出)/ instruction_following / grounded_reporting / efficiency(轻)。

**A4 判决落库**(契约 08 执行):逐 instance 注入 `judge_rubric_overall` + 逐维 `judge_<dim>`(0/1);comment 溯源 `{scorer:"judge", version:"<rubric-hash>+<prompt-hash>", report_source:"llm_judge", run_ref}`;逐条 justification 全文 → 本地 run 目录 JSON,run_ref 指向。

**A5 编排**:独立 judge pass,新 CLI `python -m bot.eval.cli judge --experiment <name> [--rubric-set ...]`;从 LangfuseTraceQuery 读候选轨迹(跨进程读契约);重评不重跑;校准重复评审只是参数。

**实现切片**:① `bot/eval/judge.py`(上下文组装 + provider 构建 JUDGE_* + trace 包裹)② rubric 库 + 加载/校验/版本化 ③ 判决解析(宽容 JSON/NA/CANNOT_ASSESS)④ score 注入(契约 08 comment 格式)⑤ CLI 命令 ⑥ 本地 justification 归档。

**升级路径备忘**(不阻塞一期):逐维隔离调用 / agentic judge / pilot→自适应校准扩充(见票 04)。
