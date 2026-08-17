# 07 — 全局 state 快照层退役(原:并发快照一致性)

Status: open
Labels: wayfinder:deliberate
Assignee: GYT
Blocked-by: 01 (已关闭;实施排 05 之后)

## Question

建议文档 §六.3 声称(优先级最高的状态风险):ParallelScheduler 下并发运行 A、B 两节点,A 完成时 `complete_invocation` 做的**全量** state checkpoint 可能已包含 B 的中间写入(`node_scratch` 半完成值、共享 graph 字段),崩溃恢复选 A 快照后 B 重执行,可能基于自己的陈旧中间状态继续。

**用户立场:希望完成修复,但非常怀疑当前设计实际不会出现此问题。** 故本票为 prove-or-fix:

1. **证明或证伪**(以 01 票审计假设 3 的证据为起点,必要时写最小复现图验证):在当前不变式下 — 每节点串行门(同一 Node 对象永不并发)、`node_scratch` 键隔离、asyncio 单线程、store 同步调用、B 重执行时 `begin_invocation` 后从 IntegratedInput 消费 — B 的中间 scratch 值残留是否真的有害?关键子问题:B 重执行是否会**读**自己的 stale scratch(`node_scratch[self.node_id]` 不重置)并据此走错分支;共享 graph 字段(非 scratch)的并发中间写是否存在于真实图(bot 图、未来 ReAct deliver 化后)。
2. **若证伪**(当前设计不触发):把不变式论证写入 ADR(哪些前提必须保持:scratch 隔离契约、串行门、重执行幂等约定),并加一个守护测试断言前提(如静态检查/回归用例)。
3. **若证实**:最小修复设计 — 候选:checkpoint 时剥离非本节点 scratch(违反全量快照共享语义,牵动 rebuild_main_state)、`begin_invocation` 时清空本节点 scratch 键、或节点重执行前 scratch 重置约定。避免建议文档中的 write-set/delta commit 大改(与"收敛不添新机制"冲突),除非别无选择。

产出:结论 + (修复实现 或 不变式守护测试) + ADR 段落。

## 重定范围(2026-08-15,用户先验:全量快照内容应坚决收敛)

用户质疑 complete_invocation 的全量 state 快照(node.py:240 → state.checkpoint() 全量 dump → 新者胜出恢复)。精确解剖已完成:记录结构本就是 per-node 版本链,偏差仅在 state_json 内容 = 整个共享 blob(含全部节点 scratch)。事实:ReAct=Null(不适用);bot 图 state 近空;真实代价 = W7 陈旧残留(重执行不幂等的根源)+ 未来共享 state 图。

**候选方案**(07 票内定稿):
- (A) 保持全量 + 不变式文档 + 守护测试(原 prove-or-fix 形态)
- (B) 完整 per-node 所有权重构(全图字段拆归属)— 倾向过度设计:收敛后无人使用共享字段
- (C,推荐) **剥离式**:checkpoint/suspend 只存全图字段 + 本节点自身 scratch(剥离他人);依据 = AGENTS.md 已禁读他人 scratch,剥离即持久层强制契约;crash 重执行得干净 scratch(幂等友好,优于今日陈旧残留);suspend-resume 不变;零合并逻辑。W7 结构性根杀。

时序:03/05 落地后开工(state 面先收缩,再动快照模型)。

## 定稿(2026-08-15,方案 D 扩展 = 全局 state 持久化层整体退役)

用户决议:全局 state 无消费者(核实:ReAct=Null no-op 且审批走 agent 层 TurnSnapshot;bot 图不读;调度/恢复只读 status+deliver);不应由 node 刷新;真需要图级快照 = 实例 metadata + 各节点状态(即既有第一套持久层,GraphStateSnapshot 查询已存在)。比 D 更进一步:suspend 快照同样退役(零真实消费者,恢复=重执行+deliver 输入,不需要内存断点续传)。

**删除清单**:① complete_invocation 的 state 参数+COMPLETED 记录 state_json 写入;② suspend_invocation 三实现+suspended 标志+快照存储;③ Node.run resume 特殊分支(resume_snapshot/is_resume/仅-resume 的 CONSUMED_PENDING 过滤);④ rebuild_main_state 删除(bootstrap 第 1 步退化为构造默认 state 对象);⑤ bootstrap suspended-seed 分支(并入 deliver 触发路径);⑥ state_json/suspended 列(迁移删除,不留死列);⑦ HumanInputNode 改幂等模式(输入有答案→继续,无→interrupt)+ suspend/resume 测试重写 + ADR 快照段落更新(13 票)。

**保留**:GraphInterrupt 异常与上行(ReAct 审批/WebUI 暂停依赖,中断值经异常载给 orchestrator/agent 层);实例级 PAUSED(instance_store 纯状态机,无 state);ctx.state 运行时共享对象(内存语义,引擎不持久化它,ReAct turn 状态载体)。

**挂起新语义**:interrupt → 节点 CANCELED(微决策:不保留无行为差异的 suspended 态)+ 实例 PAUSED;恢复 = 答案以 deliver(PENDING)到达 → 既有准入(store 扫描/外部投递)→ 节点重新执行,CONSUMED_PENDING 输入按 at-least-once 重读(IntegratedPayload.status 供业务过滤)。

**终局恢复模型**:节点恢复(崩溃/挂起/外部触发)≡ 重新执行 + deliver 准入 + 版本链递增;W7 结构性消失(快照物理不存在);三触发收敛为一条再执行路径 — 三合一兑现。持久层终态 ≡ 实例 metadata + 节点生命周期链 + deliver 四态。

**实现期核验点**:interrupt_policy.py 与 GraphAsNode(子图嵌套)是否有依赖挂起快照的路径。API 变更接受(complete_invocation 签名简化/rebuild 删除),不留兼容 shim(仓库收敛规则)。

## 评审修订(2026-08-15,三子代理审核收口)

**C4 命名消歧**:删除清单②精确化 — 删除 **NodeStateStore.suspend_invocation**(节点级,三实现);**保留 GraphInstanceStore.suspend_invocation**(实例级 RUNNING→PAUSED CAS,graph_control pause/resume 依赖)。两 store 同名方法,实现时以此为准。

**C2 CANCELED 规范定义**(消除 07/08 双语义):节点 CANCELED = **尝试级取消**(该次尝试终止;bootstrap 种子推导跳过,但 PENDING 扫描可再触发该节点 — 恢复不排斥)。**可恢复性由实例状态决定**:PAUSED=挂起场景可恢复(答案 PENDING → 节点再执行);STOPPED=用户关闭,终态不恢复(其节点残留 STAGED 永不可见=正确)。"整图不恢复"归因于实例终态,非节点 CANCELED 本身。08 表述已按此修正。

**C3 bootstrap 模式权威表**(全入口):start_run 首次运行→FRESH;start_invoke(re-invoke)→FRESH;recover_crashed/孤儿拾取→RECOVERY;resume from PAUSED→RECOVERY;deliver_to_node→**不调 bootstrap**:RUNNING 实例 notify 引擎(_recheck_pending 消费),PAUSED/PENDING 实例仅持久化(分别由 resume/start_run 消费,FRESH 的零扫描不影响 — 节点执行时 collect 全量可见)。

**B1 消费者补录**:examples/bot_project/bot/webui/routes/graph_routes.py `handle_get_instance` 读 `NodeInvocationRecord.state_json`(节点结果+实例结果,graph_routes.py:407-474)→ 07 删除 state_json 后**迁移到实例 io_record/output 路径**;`examples/bot_project/tests/webui/test_graph_routes.py` 同步更新。

**B2 HITL 恢复触发器(2026-08-15 定稿:显式 resume only)**:PAUSED 实例收到答案 deliver **不自动恢复** — 人工/WebUI resume 按钮触发(答案先到只是 PENDING 落库,后到也行);deliver 路径不反向调用恢复逻辑(无新耦合)。无答案时显式 resume = RECOVERY 空种子→[entry] 重跑(该行为文档化;调用方自己权衡)。端到端测试:暂停→答案 deliver(PENDING 可见但不运行)→resume→节点再执行消费答案→继续。

**B3 GraphAsNode 删除(2026-08-15 用户裁定,替代此前"嵌套寻址修复"方案)**:核实 GraphAsNode/Factory/Config 全部使用面=自身+导出+tests(test_subgraph.py/test_graph_as_node.py),modex_agent/bot_project/任何 YAML 零使用 — 纯框架投机面(ADR-0033 D8)。**删除**:graph_as_node.py、nodes/__init__ 与包导出、node_factory 注册类型 `graph_as_node`、两个测试文件;ADR-0033 D8 移除(13 票)。**组合语义 = 节点实现自由**(与 BotAgentNode 内跑会话树、ReAct 内置记忆同构):任何 Node 可在 execute 内自建引擎跑内层图,内层图是节点私有实现细节,**不参与外层生命周期**。中断契约(文档化模式,非框架机制):内层 interrupt 由外层节点自行处理(如 ReAct 的 agent 层 TurnSnapshot 模式);若冒泡则按外层普通节点语义(外层 CANCELED+实例 PAUSED,答案 deliver 到外层节点 id,外层再执行时自行路由答案进内层)— 嵌套寻址难题随框架包装器删除而**消解**,原"嵌套中断恢复测试"改为删除后的组合模式示例测试(可选)。

**B4 HumanInputNode 契约补全**:IntegratedInput 存在 pending 载荷 → 取**投递序最后一条**为答案,deliver 其 content 下游;无 → interrupt。`_resumed` 标志相关测试改为真实答案投递测试。

**B7 验收清单**:①迁移后 state_json/suspended 全库无引用(grep 守护);②WebUI 结果路径(graph_routes)绿;③嵌套图中断恢复测试;④PAUSED HITL 端到端(依赖 B2);⑤恢复测试套件重写清单:test_node_state_store/test_persistence_coordinator/test_node_run_lifecycle/test_scheduler_recovery/test_persistence_types/test_persistence_impl/test_null_coordinator/test_linear_recovery_entry/test_sqlite_coordinator_wiring/test_external_control_e2e/test_distributed_persistence_e2e/test_graph_orchestration_schema/bot test_graph_routes。

## 决不允许回潮清单(2026-08-15 定稿,实施与后续设计的硬约束)

**最终方案 = 收敛三合一**:崩溃恢复 ≡ 挂起恢复 ≡ 外部触发 ≡ **重新执行 + deliver 准入 + 版本链递增**。持久层 ≡ 实例 metadata + 节点生命周期链 + deliver 四态。图引擎只持久化调度事实,不持久化业务数据。

**一定不能做/不能保留**:
1. ❌ 全局 state 快照(complete/suspend 的 state_json、checkpoint 持久化)— 零消费者,列一并删除
2. ❌ Node.run resume 特殊分支(resume_snapshot/is_resume/仅-resume 的过滤)— 恢复即重执行,无特殊分支
3. ❌ rebuild_main_state/任何"从快照恢复共享 state"路径
4. ❌ suspended 态(节点记录)— interrupt 后节点 = CANCELED,不引入无行为差异的新态
5. ❌ 新的持久化机制(框架级去重键、lease/heartbeat、per-node 所有权合并、delta commit)— 全部拒绝;幂等靠状态机+at-least-once,隔离靠"快照不存在"
6. ❌ 兼容 shim/旧路径并存 — 删干净,不留 dual-path
6-bis. ❌ scratch 跨 invocation 累积语义 — scratch=per-invocation 工作区(随快照退役,该语义自然消失,不得以其他形式复活,如"scratch 持久化")
7. ✅ 保留白名单(仅此三项):GraphInterrupt 异常上行、实例级 PAUSED 状态机、ctx.state 运行时对象(内存,不持久化)
