# 未投递检测与唤醒机制

Status: triage:closed
Assignee: sisyphus
Started: 2026-08-02
Resolved: 2026-08-02

## Question

用户描述的核心创新:"agent 可能自主结束,但是 hook 提示它还没投递任务到后续节点,又唤醒继续"。

grilling 对齐:这是 **Bug 防护**场景——节点 execute 返回后没有有效下游 dispatch(transition/command/goto 都为空或指向不存在节点)。

需要决策:

1. **"未投递"的精确定义** — 什么情况算"未投递"?
   - `NodeResult.transition is None` 且 `NodeResult.command is None`?
   - `transition` 不为 None 但没有匹配的静态边?
   - 有匹配的边但指向 END(算"正常结束"还是"未投递")?
   - 与现有的 default edge 机制(D6 priority 4)如何关系?有 default edge 算"已投递"吗?

2. **检测机制** — 在什么层面检测?
   - `GraphRuntime.after_node` hook?(engine-auto-invoked,D5)
   - 调度器内部(scheduler 的 routing 逻辑中)?
   - 如果检测到未投递,是 raise error 还是触发唤醒?

3. **唤醒机制** — 如何"唤醒"?
   - **方案 A:新 instance**(ADR-0034 multi-instance model):当前 instance COMPLETED,创建新 instance 重跑同一节点。符合现有模型,但 agent 不知道为什么被重跑。
   - **方案 B:GraphInterrupt + resume**:节点 execute 内部检测到未投递,raise GraphInterrupt,恢复后重跑。但"未投递"是 execute 返回后才知道的,不是 execute 内部能检测的。
   - **方案 C:框架注入提示后重跑**:检测到未投递 → 创建新 instance → 在 state 中注入"你还没投递,请调用 dispatch/transition"提示 → 重跑。agent 看到提示后修正行为。
   - **方案 D:框架自动补 dispatch**:不唤醒,框架根据默认边自动补一个 dispatch。但这失去了"让 agent 知道自己忘了"的学习意义。

4. **防无限循环** — 如果 agent 每次都忘了投递,唤醒会无限循环。如何限制?
   - max_retry per node(如 3 次)?
   - 超过限制后 raise RoutingError(回到现有行为)?

5. **与 RoutingError 的关系** — 当前未匹配路由会 raise RoutingError(D6 priority 5)。唤醒机制是替代 RoutingError?还是在 RoutingError 之前插入一层?

## Context

- grilling 对齐:唤醒场景 = Bug 防护(忘了投递)
- ticket 02 决议:"自主结束" = execute 返回 NodeResult;AgentNode 的 execute 内部复用 TurnRunner/agent.run
- 01 research 关键发现:**after_node 时序不足** — `after_node` 在路由编译之前调用(linear.py:91-100, parallel.py:385-399),hook 此时只能看见 NodeResult 和合并后的 state,**看不见实际产生的 dispatch**。检测"未投递"需要路由后的新事件(如 `after_dispatch` / `delivery_outcome`)
- ADR-0033 D6:四层 routing 优先级(Command.goto > transition > conditional > default edge > RoutingError)
- ADR-0033 D5:GraphRuntime.after_node 是 engine-auto-invoked 钩子
- ADR-0034 D7:multi-instance model,loops produce NEW instances(`body#0`, `body#1`, `body#2`)
- ADR-0034 D2:ParallelScheduler 的 continuous scheduling,instance 完成后触发 _schedule() 重新检查

## Resolution criteria

明确以下决策:
- "未投递"的精确定义(与 default edge / END 的关系)
- 检测层面(after_node hook vs scheduler 内部)
- 唤醒机制方案(A/B/C/D 选一,或组合)
- 防无限循环策略
- 与 RoutingError 的关系(替代 / 前置 / 共存)

## Resolution

### "未投递"的精确定义

基于 routing 逻辑(parallel.py:403-473 `_compile_routing`)的 8 种情况分析:

| 情况 | NodeResult | _compile_routing 行为 | 算"未投递" |
|------|------------|----------------------|-----------|
| A | Command.goto=str | dispatch 到 1 个 target | 否 |
| B | Command.goto=list[Task] | dispatch 到多个 target | 否 |
| C | transition 匹配静态边 | dispatch 到匹配 target | 否 |
| D | transition 不匹配,有 default edge | dispatch 到 default | 否(default 保证投递) |
| **E** | **transition 不匹配,无 default edge** | **当前 raise RoutingError → 改为错误反馈+重跑** | **是** |
| F | 无 transition/Command,无 manual dispatch,有 default edge | dispatch 到 default | 否(default 保证投递) |
| G | 无 transition/Command,无 manual dispatch,无 default edge | silent skip(保持) | 否(可能旁挂逻辑/END 收尾) |
| H | 无 transition/Command,有 manual dispatch | silent skip(节点自己投递了) | 否(manual 保证投递) |

**"未投递"= 情况 E**:节点设了 transition,但 transition 不匹配任何静态边,且没有 default edge 兜底。

**情况 G 保持 silent skip**:可能是旁挂逻辑(不影响主流程)或 END 节点收尾,不改。

### 检测与处理:调度器层面

检测和处理都在 scheduler 的 `_compile_routing` 中:

1. `_compile_routing` 发现 transition 不匹配且无 default edge(情况 E)
2. **不 raise RoutingError**
3. 创建新 instance(同节点,新 seq,multi-instance model)
4. 在 state 中注入错误信息(如 `state.__routing_error__ = "transition 'xxx' matched no static edge and no default edge exists"`)
5. `_mark_ready` 新 instance
6. scheduler 正常调度循环执行新 instance
7. agent 在 execute 中看到 state 里的错误信息(对 agent 而言类似 tool 错误反馈),自己理解并修正 transition

**错误信息注入**:通过 `NodeResult.state_update` 或直接写入 state 的特殊字段(如 `__routing_error__`)。agent 在下次 execute 时从 state 读取错误信息。

### 防无限循环

- max_retry per node(默认 3 次)
- 超过限制后 raise RoutingError(回到当前行为,作为最终安全网)
- retry 计数挂在 instance 或 node 上(内存中,不需要持久化——crash 后重新计算)

### 与 RoutingError 的关系

- **共存**:RoutingError 仍是最终安全网(max_retry 超限后 raise)
- **前置**:错误反馈+重跑在 RoutingError 之前插入,给 agent 修正机会
- 情况 E 从"立即报错"变为"先重试 N 次,仍失败再报错"

### hook 主动检测(待办,不实现)

`after_node` / `after_dispatch` / `delivery_outcome` 钩子主动检测"节点忘了投递"(情况 G 的主动提示)——**不是必备能力,留作待办**。

未来实现可以提高 agent 能力(主动提示"你还没投递")。当前 01 research 发现 `after_node` 时序不足(在路由编译之前调用),检测"未投递"需要路由后的新事件——这也需要待办项解决。

### 与其他 ticket 的关系

- **ticket 04**:错误反馈+重跑是调度器(scheduler)层面的行为,不是节点内部逻辑。与 InterruptPolicy(图层面 interrupt 处理)是不同层——InterruptPolicy 管图层面收到 GraphInterrupt 后的行为,本 ticket 管 scheduler 编译路由时发现无效路由后的行为。
- **ticket 10**:retry 计数不需要持久化(crash 后重新计算),不增加 CheckpointData 字段。

### deliver/submit 修正(来自 ticket 07)

- ticket 03 原定义的"情况 E(transition 不匹配且无 default edge)"不再存在——transition 被 deliver/next_node 替代
- "未投递"重新定义:execute 返回但无任何 deliver 累积 → 可能是 silent skip(旁挂/END 收尾)或需要错误反馈重跑
- 检测机制不变:scheduler 层面,在 _submit 时检查是否有累积的 deliver。无累积 + 无默认下游 → 错误反馈重跑(max_retry per node)
- 防无限循环不变:max_retry per node(默认 3 次),超限 raise RoutingError
