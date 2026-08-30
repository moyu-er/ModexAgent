# E2E 记忆哨兵与消融臂

Status: closed
Labels: wayfinder:grilling
Blocked-by: Harbor 可行性 spike (01), ModexHarborAgent 适配器设计 (02), 长期记忆层启用裁定 (13)
Resolved: 2026-08-20 — 设计落定(独立决策,用户授权);执行依赖 01/02 落地

## Question

e2e 层(Terminal-Bench 采样)如何兜底召回型探针测不出的记忆失败?

调研输入(research/memory-eval-landscape.md):

- **相互依赖任务教训**(业界 agentic 记忆基准):会话召回基准上近饱和的模型,在"后半任务依赖前半蒸馏入记忆"的相互依赖 agentic 任务上掉到 40-60% —— 召回探针(票 11)测不出程序性/执行性记忆失败,e2e 层必须兜底
- **消融臂模式**(通用模式,直接复用):每实例全新 agent 上下文,仅持久记忆跨实例边界;臂编码进 experiment 名(`{benchmark}.{run-id}.nomemory` / `.memory`)→ 官方 harness 打任务分,Langfuse compare 直接对比 —— 因果隔离干净
- 哨兵任务设计:结构性依赖跨会话记忆的 Terminal-Bench 任务(例:多阶段任务,后期阶段需要早期阶段写入记忆的事实/偏好/结论;或同 chain 多任务共享 memory namespace)

待 deliberation 的子问题:
- 哨兵任务数量(1 个起步?)与归属(自定义任务集 vs 改造现有任务)
- 消融臂的采样预算(nomemory/memory 双臂 = 双倍采样成本,与 $15/轮预算的关系)
- 与票 01/02(harbor 线)的执行顺序:本票设计可先行,执行依赖适配器落地

## Comments

**Resolution (2026-08-20,授权独立决策)** — 设计关闭;执行排期依赖 01(spike 结果)与 02(适配器落地)。

**哨兵形态**:自定义 mini-chain `memory-chain-v1` —— 3 个任务,`evals/` 自维护、冻结+版本化(纪律同票 11 探针库):任务 1 在完成正常工作的过程中自然建立用户事实/偏好;任务 2/3 **全新会话**、结构性依赖任务 1 的事实(无记忆则极难完成)—— 相互依赖任务教训的落地:召回型探针(11)测不出"记忆服务于行动"的失败,此链兜底。

**消融臂**:每实例全新 agent 上下文,**仅持久记忆跨实例边界**(链内共享 namespace);臂编码进 experiment 名:`{benchmark}.{run-id}.memory` / `.nomemory`;官方 harness 打任务分 + 契约 08 `verdict_<benchmark>` 注入;两臂差 = 记忆对任务成功的净贡献。

**预算**:2 臂 × 3 任务 ≈ $3-5/轮,与主采样轮同跑;首轮先单链,v1 不扩。

**对票 02 的设计输入(硬约束,须记入 02 票面)**:适配器必须支持 (a) 链内跨任务实例的**共享记忆 namespace 持久化**;(b) `.nomemory` 臂的按实例隔离。此需求影响容器内记忆工作区映射设计,02 设计时一并解决。

**验收**:memory 臂成功率显著高于 nomemory 臂(方向性即可,v1 不设统计显著性门槛——3 任务样本太小,看的是信号不是结论)。

**Erratum(2026-08-20 design-closure 实测复核)**:臂的 experiment 联动复用票 02 Erratum 机制(容器内根 span 携带 `langfuse.experiment.*` 属性,臂名进 experiment 名;events_only 下 POST dataset-run-items 为空操作桩,不可作联动路径)。
