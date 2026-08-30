# Judge 校准门槛

Status: closed
Labels: wayfinder:grilling
Resolved: 2026-08-18 — 门槛表落定;pilot 先行(10 实例)裁定

## Question

judge 投产前的校准标准是什么?(常见 rubric-judge 实现通常不带校准机制 —— 这是我们的增强,标准需从零定):

- 一致性门槛:同 trajectory 重复评分(temp=0)的一致率阈值;达不到怎么办(改 prompt?加 panel 投票?)
- 与程序化断言的相关性:在带 world_assertions 的任务上,judge 分与断言 pass 的相关度要多高才可信
- 版本化纪律:judge prompt 每次变更后必须重跑的校准集是什么、多大
- 投产边界:校准达标的 judge 允许评什么(基准任务?生产 trace 采样?),未达标时允许什么

Blocked-by: Judge 架构设计

## Comments

**Resolution (2026-08-18,用户确认"砍小 + 确认")** — 采用最小可行验证协议(业界大规模多评审研究提炼,出处见 research)裁剪到 pointwise 场景:

**B1 校准集与真值**:**pilot 先行** —— 先 10 实例 × 逐 rubric 人工 MET/UNMET 标定(用户一次性投入 ~30-60 分钟),κ 崩则修 prompt 再说,达标再扩到 20-30 实例定稿。实例取自基准任务,含好/坏/边界混合。

**B2 度量与门槛**(逐 rubric 维度):
| 度量 | 门槛 |
|---|---|
| Cohen's κ(judge vs 人工) | ≥0.6 维度级 / ≥0.67 整体 |
| 混淆矩阵 + 严苛/宽松方向 | 必报;FP≠FN 偏斜 >2× → 修 prompt 重校 |
| test-retest(t=0,3 次重复) | 同判率 ≥95% |
| NA/CANNOT_ASSESS 率 | <5%;退化维度(全 MET/全 UNMET)标 NA 不给结论 |
| 偏差审计(长短回答判定分布差) | <10pp |

**B3 重校准纪律与投产边界**:触发 = judge prompt/模型/rubric 集任一变更(只重校该集),无豁免。投产分层:κ 达标 → compare 正式对比;未达标 → 分数照落库但 comment 标 `calibrated: false`,compare 标灰 —— **永不静默用未校准 judge 分做决策**。生产 trace 采样评审需基准达标后单独再校准(分布不同),留雾区不进本轮。校准成本:pilot ~30 次调用 ≈ $0.5;全量 60-90 次 ≈ $1-3。

**实现切片**:校准 runner(judge 命令的重复模式)+ κ/混淆矩阵/test-retest 计算脚本 + `calibrated` 标记写入 —— 并入票 03 切片 ⑤ 的 CLI。

