# 02 — ReAct 数据流 deliver 化:设计决策

Status: closed
Labels: wayfinder:deliberate
Assignee: GYT
Blocked-by: (无 — frontier)

## Question

ReAct 六节点图(START→BEFORE→LLM↔TOOL→AFTER→END)目前节点间数据通过共享 `ReActTurnState` 字段传递(`llm_response`、`tool_batches`、`approval` 等),这是 ADR-0034 D7 记录的已知债务。用户已决策:**数据流全部 deliver 化,不再依赖 state 传递**。需要定稿的设计问题:

1. **字段划分**:哪些字段改为 deliver 载荷(LLM→TOOL 的 LLMResponse、TOOL→LLM 的执行结果、TOOL/AFTER 的路由依据),哪些保留为 turn 级共享状态(`turn_attempt`、`iteration`、`result`、`resume_target`、messages)?划分标准是什么(跨节点业务数据=deliver;turn 级元数据=state)?
2. **approval 挂起/恢复路径**:ApprovalTransaction 目前经共享 state + suspend snapshot 恢复;deliver 化后 suspend→resume 的输入流如何走(RESUME payload? AgentNode 的 `_integrate_upstream` 过滤 CONSUMED_PENDING 模式是否直接适用)?
3. **载荷类型往返**:DeliverStore(SQLite)将 content JSON 化,Pydantic 模型恢复后是 dict — LLMResponse/ToolBatchState 需要 re-validate 约定;是否引入统一内容 codec(见地图 Not yet specified)。
4. **快照/codec 影响**:deliver 化后 `ReActTurnState` 字段缩减,`ReActSnapshotPolicy` 与 TurnStateStore 持久化字段如何随之收敛;turn 中断恢复(TurnSnapshot 路径)与图级 suspend 的关系是否更清晰了。
5. **hook 时序不变式**:LLMNode/ToolNode 现在从 state 读上游数据;deliver 化后从 IntegratedInput 读 — 4 级 hook 层级的触发时机必须逐点核对不变。
6. **调度器影响**:LinearScheduler 下 deliver 已是唯一路由路径,数据 deliver 化后 Linear 的共享准入逻辑还剩什么独立部分(衔接地图 fog 中的调度收敛项)。

产出:定稿设计(可直接进入 03 实现),含字段划分表、approval 恢复时序图、hook 时序核对清单。

## Comments

**决议(2026-08-15,与 GYT deliberate 定稿):**

**数据通道三分原则** — history 是唯一持久上下文源(所有该被 LLM 后续看到的消息由产生节点 append);deliver 是路由信号 + 瞬态单跳数据(默认 payload=None,仅错误描述符等本跳控制数据带最小 payload);state 只存 turn 生命周期(phase/iteration/turn_attempt/approval/tool_batches/resume_target/result)。节点输入策略(读 history/记忆/integrated_input,甚至无输入纯靠记忆)是各 node 自己的设计自由 — ReAct 已有记忆持久化。与 bot 图 AgentNode 模式(输入注入会话记忆、deliver 一次性消费)同构,即三合一收敛。

**Q1-Q6 定案**:Q1 经 integrated_input 图机制收敛,ReAct 用 NullDeliverStore 内存语义(不引 SQL);Q2/Q3 承认分层,payload 不承载会话数据,各 node append history;Q4 approval/挂起恢复机制不动;Q5 载荷契约 = JSON-serializable Any,无框架 codec;Q6 错误等非记忆消息走 deliver(`{"error": text}`),AfterTurnNode 的 ERROR 分支并入 FAILED 分支。

**Hand-off 表**:LLM→TOOL / TOOL→LLM / LLM→AFTER(终答)payload=None,接收方读 history 末条(ToolNode 就地 canonicalize call_id);LLM→AFTER(max-iterations)None 兜底分支;LLM→AFTER(错误)`phase=FAILED` + `deliver({"error": text})`;TOOL→AFTER(FAILED/CANCELLED/dedup-stop)None;AFTER→BEFORE/END payload 改 None(END 读 state.result 不变)。

**State 处置**:删除 `llm_response`(唯一跳间 hand-off 字段);保留 phase/iteration/turn_attempt/approval/tool_batches/resume_target/result/message_delta/current_node。附带收益:W7/D3 并发快照陈旧共享字段对 ReAct 暴露面归零。

**残余 flip 条件**:第三个强类型 payload 消费者出现 → 重估 codec(地图 fog)。

**错误分类学(2026-08-15 补充,修正已删除的伪 flip 条件)**:错误分两类 — (1) 基础设施错误(LLM API 调用失败、框架异常):不属于会话内容,**永不进 history**,对 agent 可见无意义(重试在 client 层);去向 = deliver payload 单跳 → AgentResult(error) → 调用方 + 日志。(2) 会话级事实(工具执行失败结果):本就是会话内容,走 ToolNode 正常路径 append history,属于数据流而非错误处理。"错误需对下轮 LLM 可见 → 改走 history"不构成 flip 条件 — 基础设施错误对 agent 可见无正当场景。
