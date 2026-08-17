# 03 — ReAct 数据流 deliver 化:实现与回归验证

Status: open
Labels: wayfinder:task
Assignee:
Blocked-by: 07 (顺序定案 05→07→03:07 先收缩 Node.run/state 模型,03 对终态机制写一遍不返工;02 已关闭)

## Mechanism note (2026-08-15)

02 决议的载荷语义(payload=None/错误描述符,读 history)不变;实现基座是 05 的 STAGED 机制 — Node.deliver 已改为即刻经 coordinator 路由(ReAct 装 Null,deliver 即可见,Linear 下行为与今日一致)。本票只改 ReAct 六节点的读写行为,不动机制。

07 联动(2026-08-15):ReAct 审批挂起本就全走 agent 层 TurnSnapshot(TurnStateStore),图层 suspend 快照退役不触及;本票第 3 项"approval 挂起/恢复路径"按 07 定稿理解 = 无图层改动,仅确认现有 agent 层管线在 05/07 落地后回归绿。

## Question

按 02 票定稿(以 Mechanism note 为准 — 下方旧条目已按终态语义改写,2026-08-15 评审修订):

1. 改写 `StartNode`/`LLMNode`/`ToolNode`/`AfterTurnNode`/`EndNode`:载荷语义按 02 定稿 — LLM→TOOL / LLM→AFTER(终答)/ TOOL→LLM / AFTER→BEFORE/END 均 payload=None,接收方读 history 末条(ToolNode 就地 canonicalize call_id);LLM→AFTER(错误)`phase=FAILED` + `deliver({"error": text})`,AfterTurnNode ERROR 分支并入 FAILED 分支。**不**从 IntegratedInput 读业务数据。
2. 缩减 `ReActTurnState`(删除 `llm_response`),同步收敛 `ReActSnapshotPolicy` / TurnStateStore 持久化面。
3. approval:零图层改动 — agent 层 TurnSnapshot 管线在 05/07 落地后的回归验证(07 已删图层 suspend/resume 机制,ReAct 审批本就不经它)。
4. (删除 — 02 已定无框架 codec;未来第三强类型 payload 消费者出现按 02 flip 条件重估)
5. 验证:4 级 hook 时序逐点核对(02 的核对清单)、approval suspend/resume 回归、`tests/unit/agents/react/` 全绿、ReAct 全量回归绿。

关闭标准:代码落地 + 测试全绿 + `react/AGENTS.md` 与 ADR 相应段落合并更新。
