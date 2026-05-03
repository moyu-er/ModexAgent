## 1. 文档方向是否对齐？

**对齐，而且对齐度很高。**

这个设计文档开头就把当前问题定位为：

* `agent.py` 臃肿；
* Hook / Interceptor / Approval 与 ReAct 循环紧耦合；
* 审批恢复链路过长；
* 没有通用图抽象；
* `AgentContext` 被 ReAct 专属字段污染；
* 审批状态无抽象。([GitHub][1])

这些问题和我们讨论的“runtime 中心化、边界收口、删除多套 hook / approval / interceptor 路径”是同一个方向。

设计目标里也明确写了：

* 通用图引擎；
* `AgentContext` 瘦身；
* 审批状态持久化抽象；
* `SuspendStrategy` 可插拔；
* 整批审批、中断恢复；
* `Clean / Full` 双模式；
* 旧实现直接移除，不做兼容层。([GitHub][1])

所以从设计意图看，它不是在继续往 Pipeline 里堆逻辑，而是在把 ReAct 执行拆成：

```text
Graph Engine
→ ReAct Nodes
→ Runtime Services
→ Approval / Hook / Interceptor / Control
```

这和我们前面建议的方向一致。

---

## 2. 它是否也认同“approval 不应该藏在 interceptor 里”？

是的。

`react-hook-interceptor-control-integration-design.md` 明确指出当前 approval 有两个执行模型：

```text
ToolNode -> classify tier -> SuspendResumeStrategy -> GraphInterrupt -> Pipeline -> UI -> resume
```

以及：

```text
TieredToolApprovalInterceptor.around_tool_call()
```

文档判断后者是第二套模型，保留两套 active flow 会让行为难以推理。它还明确说：approval classification 不应该隐藏在 generic interceptor 里，而应该是 `ApprovalRuntime`，由 ToolNode 查询。([GitHub][2])

这和我建议完全一致：

> **ReAct full mode 里，approval 应该走 `ApprovalRuntime + classifier + suspend/resume strategy`，而不是走 approval interceptor。**

---

## 3. 它是否也认同 hook / interceptor / control 的边界？

是的，而且写得很直接。

设计文档把边界分得很清楚：

| 机制          | 文档中的定位                                               |
| ----------- | ---------------------------------------------------- |
| Hook        | 生命周期观察、轻量上下文变换、最终内容变换                                |
| Interceptor | 包裹 turn / iteration / LLM / tool 执行边界                |
| Control     | runtime command plane，不是普通 hook                      |
| Approval    | 显式 runtime service，不是 interceptor 副作用                |
| Pipeline    | 组装 runtime services，不嵌入 approval/control/recovery 细节 |

文档明确说 hooks 不应该做 cancellation、approval、hard policy enforcement；interceptors 适合 timeout、result truncation、monitoring、低层 wrapper；control 是一等 side channel，需要 command transport、persistence、routing 和未来 active-operation handles。([GitHub][2])

所以这个设计文档的核心方向就是：

```text
Pipeline assembles runtime
ReAct owns execution boundaries
Hooks observe/transform
Interceptors wrap calls
Control handles commands
Approval handles suspend/resume policy
```

---

## 4. 最近代码是否也在朝这个方向演进？

**是的，提交记录非常明显。**

5 月 2 日到 5 月 3 日的提交基本就是按这个路线推进：

* `feat: add ReActRuntime with clean/full mode normalization`
* `feat: normalize ReActRuntime at ReActAgent.run() entry`
* `refactor: simplify ReActAgent to use only runtime for hooks/interceptors/checkpoints/injections`
* `feat: wrap turn in around_turn() interceptor boundary`
* `feat: wrap iteration in around_iteration() boundary, remove node feature flags`
* `feat: add ControlRuntime, ControlPhase, ControlStore, InMemoryControlStore`
* `feat: drain control commands at 5 safe boundaries`
* `refactor: ControlDrainInterceptor delegates to ControlRuntime.drain()`
* `refactor: delete ToolPolicyGuardHook (replaced by ApprovalRuntime.classifier)`
* `feat: add ApprovalClassifier, TieredToolApprovalClassifier, ApprovalRuntime`
* `refactor: ToolNode uses ApprovalRuntime.classifier, delete classify_tier()`
* `feat: sync bot_project to use ReActRuntime + ApprovalRuntime`。([GitHub][3])

这组提交说明最近代码不是零散修 bug，而是在系统性地做三件事：

```text
1. ReActRuntime 中心化
2. hook/interceptor/control/approval 边界明确化
3. 删除旧的重复 policy / approval / extension 路径
```

---

## 5. 当前代码落地到什么程度？

从 `framework/agents/react/runtime.py` 看，`ReActRuntime` 已经存在，并且已经承担 clean/full mode 归一化：clean mode 会清掉 hooks、interceptor、checkpoint、suspend strategy、injection queue 等 extension；full mode 会从 `AgentContext.extensions` 中消费 hook runner、interceptor chain、checkpoint store、suspend strategy、injection queue、governance、safety 等服务。([GitHub][4])

`ReActAgent.run()` 也已经改成：优先使用 `context.runtime`，否则从 context 构造 `ReActRuntime`，然后通过 runtime 调用 control、hooks、interceptors，并把 turn boundary 包进 `around_turn()`。([GitHub][5])

`current-runtime.md` 也把当前目标层次写清楚了：Pipeline 组装 runtime services 和处理 I/O；ReAct 拥有 turn、iteration、LLM、tool、approval、resume 边界；hooks 观察/变换；interceptors 包裹执行 scope；control 负责存储、接收、drain，并未来支持 live operation targeting。([GitHub][6])

所以代码确实已经从“设计文档阶段”进入“实现收口阶段”。

---

## 6. 但还没有完全完成

我会把当前状态判断为：

> **方向正确，近期代码强烈对齐；但还没完全收口完。**

原因有几个：

第一，`ReActRuntime.from_context()` 目前 full mode 里已经消费了 hooks、interceptors、checkpoint、suspend、injection、governance、safety，但从当前 raw 文件看，`approval` 和 `control` 字段虽然在 dataclass 上存在，`from_context()` 返回值里未明显填入它们。([GitHub][4])
不过提交记录又显示 bot_project 已同步到 `ReActRuntime + ApprovalRuntime`，说明可能是通过 `prebuilt_runtime` / `context.runtime` 直接注入，而不是全部经 `from_context()` 构造。([GitHub][3])

第二，`current-runtime.md` 仍然说 bot_project 默认 interceptor chain 包含 `ControlDrainInterceptor` 和 `ToolResultLimitInterceptor`，而 approval wiring 应来自 runtime construction 和 explicit policy objects。([GitHub][6]) 这说明“approval 不走 interceptor”已经是目标，但 control drain 仍保留 interceptor adapter 形态。这是合理过渡，但后续要避免它和 `runtime.control.drain()` 双重触发。

第三，`Pipeline should assemble runtime services, not embed approval/control/recovery behavior inline` 是设计目标，文档也指出 Pipeline 目前仍处理 input、context recovery、approval store setup、approval command parsing、GraphInterrupt、resume、output finalization 等大量逻辑。([GitHub][2]) 最近提交有 `refactor: extract 6 private methods from _process_message_locked`，这说明正在减肥，但还不是完全拆成稳定组件。([GitHub][3])

---

## 7. 我对当前演进方向的判断

我的判断是：

```text
superpower/spec 文档方向：正确
hook/interceptor/control 设计方向：正确
recent commits 演进方向：正确
当前完成度：约 60%~70%
主要剩余风险：runtime 注入路径、Pipeline glue、legacy fallback、control/approval adapter 重叠
```

换句话说：

> **是的，文档和近期代码都在朝“ReActRuntime 中心化 + clean/full 边界 + approval 显式 runtime service + control 一等 command plane + hook/interceptor 职责收口”的方向走。**

短期最应该继续做的是：

```text
1. 确认 bot_project 只通过 ReActRuntime 注入 approval/control
2. 删除 ToolPolicyGuardHook / classify_tier fallback / approval interceptor 主路径
3. 确认 hook 只剩 HookRunner 一套 dispatch
4. 确认 turn / iteration / tool / stream interceptor scope 都有测试
5. 把 Pipeline approval/resume 逻辑继续抽到 ApprovalCoordinator / TurnRunner
```
