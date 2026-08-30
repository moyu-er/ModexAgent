# 确定性组件指标(无 judge,CI 可跑)

Status: closed
Labels: wayfinder:grilling
Blocked-by: 记忆遥测插桩 (09)
Resolved: 2026-08-20 — 指标集/归因边界/CI gating 落定(独立决策,用户授权)

## Question

哪些记忆指标可以零 LLM 成本、确定性计算,并进 CI 作为回归切片?

调研输入(research/memory-eval-landscape.md):

- **组件分解教训**:检索方式差 20pp、写策略只差 3-8pp,检索精度与最终正确率 r=0.98 —— 端到端分数掩盖失败层;证据召回 Recall@k 是无 judge、零噪声的确定性指标
- **写路径成本必须一等**:某商业系统 2/6 题型追平朴素 RAG 但写路径 50× 成本;换 embedding 一项 +6.2pp —— 不报写成本的对比不可信
- **压缩三轴**(通用模式,事件流规约、录制即可算):峰值有界性(peak/预算)、密度(保留分数)、开销(转录膨胀)+ 策略归因
- **利用增量**:同题带记忆 vs 无记忆两臂,判 Beneficial/Harmful/Ignored/Neutral —— 定位检索失败还是利用失败
- **CI 现实**:业界无人在 CI 跑 LLM-judged 记忆基准(主流实现均手动);CI 形态 = 确定性切片(脚本化运行 + 比值断言,如压缩组峰值比 <1.2 vs 基线 >1.7);逐层切片必要(聚合 -2~-6pp 时责任层已 -25~-91pp)

候选指标(待 deliberation 裁剪):
1. 压缩三轴 + cleanup 触发敏感性(从 09 的 memory.cleanup span 规约)
2. 写路径成本:每条入库记忆的 LLM 调用数/token/时延;读时延(检索→注入)
3. 利用增量 harness(两臂运行 + 判定)
4. 注入留存率:检索全量 vs 实际注入 vs 被裁剪(从 memory.read span diff)

## Comments

**Resolution (2026-08-20,授权独立决策)** — 全部指标从票 09 span 规约(单一事实源);阈值锚定**我们自己的配置不变量**而非外部绝对数。

**指标集**:
1. **压缩三轴**(memory.cleanup span 规约):峰值有界性 `peak/(max_token_ratio×window) ≤ 1.0` + 每次 cleanup 严格降总 token + 占位符单调性(同消息同替换、窗口只扩不缩,前缀稳定不变量)—— 三者**硬断言**;密度(保留分数分布)与开销(catalog+占位符 token 占比)v1 记录不设门
2. **写路径成本**:逐项 LLM 调用数/token/时延(compact 摘要、Dream 巩固、core 巩固);**归因边界:per-turn `cost_usd` 不含后台记忆成本** —— 这些成本挂自身 span,汇总为 run 级 `memory_write_cost_usd` score;评测中 harness 驱动的 Dream 归 probe run experiment;生产后台 Dream(未来启用)= 日聚合,永不进 per-turn
3. **读路径**:load() 时延 + 注入 token 数;注入留存率 = 实际注入/检索可用(section 级,自 memory.read span)
4. **利用增量**:票 11 双臂数据四分类规约(Beneficial/Harmful/Ignored/Neutral);beneficial/harmful 率为采样指标(需 LLM,不进 CI);v1 记录不设门,首轮基线后再定

**CI 确定性切片**(零 LLM、零网络、本地计算不经 Langfuse):探针快照重放(证据召回 + 隔离污染标志)+ 压缩不变量脚本化断言(脚本化假消息流即可,无需真模型);**gating = 回归预算模式:冻结基线之上"新增失败即红"**;绝对阈值待首轮基线从数据定,不预造魔法数。

**metrics.py 数据源切换**:bot/eval/metrics.py 离线聚合改读 Langfuse(LangfuseTraceQuery);与 CleanupMetricsHook 退役同批落地(09 绑定);eval 运行报告与 CI 切片两条消费路径分立。

**实现切片**:①span→指标规约器 ②CI 回放切片 + gating 配置 ③metrics.py 改造(Langfuse 源)④`memory_write_cost_usd` 聚合与注入 ⑤首轮基线跑 + 阈值标定(后续)。

**Erratum(2026-08-20 design-closure 实测复核)**:指标 2(写路径成本)的数据源钉死为票 09 Erratum 的扩展 payload(compact/consolidate 返回 usage)——否则 `memory_write_cost_usd` 无源恒空。指标 3(读路径)的 load() 时延同样来自 CONTEXT_ASSEMBLED payload 的耗时字段。
