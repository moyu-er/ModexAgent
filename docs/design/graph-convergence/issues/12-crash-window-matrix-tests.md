# 12 — 崩溃窗口矩阵测试落地

Status: open
Labels: wayfinder:task
Assignee:
Blocked-by: 01, 05, 07, 03, 09 (实施链全覆盖;06/08/10/11 已关闭)

## Question

以 01 票审计产出的**完整崩溃窗口清单**为矩阵行,以 04/06/07 票定稿的语义为期望,落地故障注入测试套件:

1. **注入缝**:每个窗口两写之间的可控崩溃点(测试专用 store 包装/注入 hook,不污染生产代码路径 — 缝的设计本身是一个小决策)。
2. **断言不变式**:每窗口断言 (I1) 可恢复(bootstrap 能继续)、(I2) 输入不丢失、(I3) 重复有界且符合定稿语义(at-least-once 哪些是 by-design、幂等键关闭哪些)、(I4) 生命周期转换幂等(CAS/孤儿清理/finalize)。
3. **覆盖对象**:`Node.run` 生命周期窗口、bootstrap 恢复分支、route_deliver/deliver 消费状态机、orchestrator 实例层窗口、re-invocation 窗口(08 票结论并入)、pending deliver 恢复窗口(05 票新增)。
4. **位置**:`tests/unit/modex_graph/`(引擎层)与必要的 `tests/integration/graph_orchestration/`(编排层)。

关闭标准:矩阵清单中每个窗口都有对应测试且绿;窗口清单与测试一一可追溯(audit 文档交叉引用)。

## 语义更新(2026-08-15,源自 04/05)

审计 D1/D4 的期望语义已由 04/05 定稿改写:输出重复 = at-least-once by design(绝不作废 + 按来源全量提升,重试 N 次 N 份可见);D4 = 输出不丢(STAGED 持久可观测)。矩阵断言按此更新,不再把"重复"当失败。验收含 05 票的"同一状态机测试套件跑三实现"(Null 无状态契约除外)。

## 矩阵行补全(2026-08-15 评审修订)

在 W1-W14 基础上补以下行(语义输入:08 模式/03 ReAct/09 清扫):
- **FRESH vs RECOVERY**:re-invoke 后 v1 残留(PENDING/CONSUMED_PENDING/STAGED)不主动触发、数据全量可见;RECOVERY 残留照常种子化。
- **END 种子/reached_end**:上游全 COMPLETED+END 有 PENDING 崩溃恢复 → END 执行且 reached_end=True(非 FAILED/从头重跑)。
- **ReAct Null 路径**:Null 四态契约差异(STAGED 无操作/mark=删除)回归 — 02/03 落地后 ReAct 全链路。
- **09 清扫**:executor∉alive 的 RUNNING → CRASHED;executor∈alive 不动;NULL 视为脏;终态 attrs 保留。
- **D6 处置=接受+回归**:stop 协作语义(节点体在 STOPPED 后完成、实例 CAS 静默幂等)断言为文档化行为。
- **D7 处置=接受+回归**:complete 后 finalize/IORecord 窗口崩溃 → 实例状态权威(COMPLETED),io null 被结果路径容忍。
- **D8 处置=接受+文档**:Linear 调度器不支持外部投递准入(设计内:Linear=ReAct 内部流,外部投递属 Parallel/bot 图场景)— 断言其显式拒绝或文档化,不修。

**输出基数规范口径**(全票统一,R5):可见行数 = 成功 deliver() 声明数;崩溃尝试的声明行存活,由后续完成的源 invocation 一并提升。
