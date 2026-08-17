# 13 — 终局:ADR 合并、文档收敛与地图收口

Status: open
Labels: wayfinder:task
Assignee:
Blocked-by: 03, 05, 06, 07, 08, 09, 10, 11, 12

## Question

所有实现票关闭后的收口工作(ADR 活文档治理:细化合并入原 ADR,不开平行版本):

1. **ADR 合并**:0033/0034(D7 ReAct 共享 state 债务段落 — deliver 化后改写)、0040(re-invocation 状态语义、deliver 账本作用域、GC 判断)、pending-deliver-store 与 deliver 幂等键若构成"真正新决策"则按规则评估新 ADR,否则并入 0033 持久化契约段落。
2. **AGENTS.md 族更新**:`src/modex_graph/AGENTS.md`(调度收敛、store 家族)、`src/modex_agent/agents/react/AGENTS.md`(数据流描述)、`docs/design/graph-orchestration/distributed-persistence.md` 权威描述同步。
3. **地图收口**:核对 Decisions so far 完整;fog 区逐项判定"已解决/明确 defer 并记录去向";全量测试套件(框架 + bot_project)绿灯确认。
4. **建议文档定位**:在 `docs/handoff/suggestion.md` 头部或地图中标注各 P0/P1 项的处置结论(采纳/证伪/defer),闭环参考。
5. **外部投递 at-least-once 文档化**(06 关闭并入):deliver_to_node 调用方重试可产生重复 PENDING 行,框架接受 at-least-once,不提供去重键 — 写入 ADR-0033/控制面文档。
6. **文档义务补全**(2026-08-15 评审修订,此前遗漏):
   - `src/modex_graph/interrupt_policy.py`(CrashPolicy 仍描述 crash+suspended 重入 — 随 07 退役改写)与 `src/modex_graph/README.md`。
   - `src/modex_graph/AGENTS.md` **State Ownership 段**:保留运行时隔离契约(跨节点只走 deliver、scratch 键隔离、串行门),删除 checkpoint/恢复依据;明确 scratch=per-invocation 工作区、永不持久化。
   - 09 契约:attrs 扩展位(类型例外已文档化)/ProcessIdentity/Registry/清扫语义。
   - D5:ADR-0038 文档-代码偏差修正。
   - D6/D7/D8 处置结论标注(接受+文档化,见 12 票矩阵行);D8 明确"Linear 不支持外部投递准入"写入调度器文档。
   - `research/crash-window-audit.md`:保留为收敛前历史基线(已加 banner),不重写证据。
   - ADR-0033 **D8 移除**(GraphAsNode 删除);`src/modex_graph/AGENTS.md` 增"子图组合=节点实现自由"模式说明(execute 内自建引擎,内层不参与外层生命周期,中断契约见 07 票)。
   - 12 票的"窗口↔测试一一可追溯"交叉引用核对。

关闭标准:文档收敛 + 全绿 + 本地图所有票关闭(fog 清空或显式 defer)。
