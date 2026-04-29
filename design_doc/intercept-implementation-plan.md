# Hook / Interceptor / Control 实施计划

## 执行规则

- 本计划面向当前实际代码改造。`intercept-design(todo).md` 是未落地设计草案，只作为参考材料，不作为被迁移对象
- 本计划不做向后兼容；当前代码中不符合目标边界的实现和导入应改造或删除，不做 re-export
- 每完成一个步骤，必须刷新本文件中的状态
- 每个步骤完成后，在"进展记录"写一句说明：做了什么、涉及哪些文件、是否有设计偏差
- 如果实现中发生设计变更，必须记录到"变更记录"，并同步刷新设计文档
- 每步都要补充或更新必要测试；不能验证时要记录原因
- 第一阶段的可配置指代码装配 API，不要求 YAML 或配置文件解析；不得为了配置化引入 `dict` 驱动的弱类型核心 API
- 第一阶段实现不得破坏 `hook-interceptor-control-future-plan.md` 中列出的后续扩展点
- 关键步骤完成后做好git提交
- 设计文档见`hook-interceptor-control-design-plan.md`

## 状态表

| 步骤 | 状态 | 目标 |
|------|------|------|
| 1. Hook 基础契约 | 已完成 | 建立 `framework.hook`，定义 `HookPoint`、`HookPayload`、`HookResult`、`HookErrorPolicy`、`HookSpec` |
| 2. HookRunner 与内置 Hook | 已完成 | 改造现有 hook 能力，迁入 `framework.hook.builtin`，确保当前方法名可命中 |
| 3. 统一控制异常 | 已完成 | 定义 `AgentControlError`、`TerminationReason`，让 hook/interceptor/control 共享终止语义 |
| 4. Interceptor 基础契约 | 已完成 | 建立 `framework.interceptor`，定义 `InterceptorScope`、各 scope context、next-call 协议 |
| 5. InterceptorChain | 已完成 | 实现通用 AOP 链，第一阶段接入 tool、turn、iteration；tool 边界兜底合法 ToolResult |
| 6. Tool 审批 | 已完成 | 实现 `ToolApprovalInterceptor`，支持 `deny_as_tool_error` 与 `deny_as_cancel`，保证 session/history 一致 |
| 7. Timeout 策略 | 已完成 | 实现 tool/turn timeout，从 `ctx.safety` 读取默认值并明确 owner，避免与 RuntimeSafetyPolicy 重复处理 |
| 8. Control 平面 | 已完成 | 建立 `framework.control`，支持 `ControlCommand`、`ControlScope`、channel、event bus、TTL、idempotency、drain/peek |
| 9. Preset 与 checkpoint | 已完成 | 实现 `PresetControlRule` 基础能力（TokenBudgetControlRule）；JsonFileCheckpointStore + NoOpCheckpointStore |
| 10. ReActAgent 接入 | 已完成 | 接入 HookRunner/InterceptorChain/AgentControlError；_call_hooks/_execute_tool 双路径兼容 |
| 11. bot_project 适配 | 已完成 | 示例项目通过代码 builder 装配 runtime，验证 pool/pipeline 基础运行路径 |
| 12. 清理与验证 | 已完成 | 删除旧入口，补齐测试矩阵，运行验证 |

状态取值：`待开始`、`进行中`、`已完成`、`阻塞`。

## 进展记录

- 2026-04-28：根据设计复核，明确第一阶段不做 YAML 配置，改为代码装配式配置。
- 2026-04-28：根据定位复核，明确原始设计文档是未落地草案，不是当前实现；实施计划改为面向当前代码改造，并采用单数包名 `framework.hook`、`framework.interceptor`、`framework.control`。
- 2026-04-28：合并二次 review 有效项，明确 HookPoint 必须匹配当前 hook 方法名，Interceptor 是通用 AOP scope，不只绑定 ReAct turn。
- 2026-04-28：对照原未落地草案补充覆盖性设计，实施计划从 9 步扩展到 12 步，增加目录契约、PresetControlRule、Channel/EventBus 语义、checkpoint owner 和测试矩阵。
- 2026-04-28：新增后续阶段规划文档，确认 pause/resume、stream、后台工具、完整 checkpoint、广域 AOP、治理、插件和动态代码配置的扩展路径。
- **2026-04-28 Steps 1-3**：创建 `framework/hook/` 包（abc.py、runner.py、builtin/）。Hook/HookPoint/HookPayload/HookResult/HookErrorPolicy/HookSpec 核心类型。5 个内置 Hook 从旧位置迁入 builtin/。统一的 AgentControlError 异常层级移入 `framework/control/exceptions.py`。更新了 AgentContext、ReActAgent、Pipeline、Session、Plugin 系统等所有调用方的导入路径。更新了 6 个测试文件中的 AgentRunHook 引用。928/928 测试通过。
- **2026-04-28 Steps 4-5**：创建 `framework/interceptor/` 包（abc.py、chain.py）。InterceptorScope 枚举（9 个 scope），Interceptor 协议，4 个 scope context（ToolCallContext/TurnContext/IterationContext/LLMCallContext），InterceptorChain 洋葱链执行器（tool/turn/iteration 三边界兜底）。
- **2026-04-28 Steps 6-9**：创建 5 个内置 Interceptor（ControlDrainInterceptor、ToolApprovalInterceptor、ToolTimeoutInterceptor、TurnTimeoutInterceptor、ToolResultLimitInterceptor）。Control 平面类型（ControlCommand/ControlScope/ControlEvent + 枚举）、InMemoryControlChannel、CallbackControlEventBus、JsonFileCheckpointStore。PresetControlRule 协议 + TokenBudgetControlRule 实现。928/928 测试通过。
- **2026-04-28 Step 10**：ReActAgent._call_hooks 改为优先使用 HookRunner（保留旧路径回退）；_execute_tool 优先使用 InterceptorChain 包裹（保留旧路径回退）；新增 AgentControlError 捕获处理。928/928 测试通过。
- **2026-04-29 Step 11**：bot_project 适配完成。DefaultAgentFactory session 模式传入 hook_runner/interceptor_chain/checkpoint_store；BotService peer agent 的 PeerAutoSendHook 注入改为优先使用 HookRunner；builders.py 更新。254/254 相关测试通过。
- **2026-04-29 Step 12**：清理与验证完成。删除 framework/core/hooks.py；删除 framework/multi_agent/hooks.py 中的 TaskInterventionHook（功能由 ControlDrainInterceptor 替代）；更新 framework/multi_agent/__init__.py 移除 TaskInterventionHook 导出；更新 test_core_runtime.py 中对应测试为 ControlDrainInterceptor 测试。新增 5 个测试文件（37 个测试用例）补齐测试矩阵：test_interceptor_chain.py、test_tool_approval_interceptor.py、test_control_channel.py、test_control_drain_interceptor.py、test_hook_error_policy.py。修复 ControlDrainInterceptor 支持 CANCEL_RUN 命令。254/254 相关测试通过。

## 变更记录

- 2026-04-28：实施计划增加"代码配置优先"约束；bot_project 适配目标从读取配置文件改为 builder 代码装配。
- 2026-04-28：步骤命名从"迁移"调整为"改造"，目标包名从复数调整为单数。
- 2026-04-28：步骤 1、3、5、6、7 增加 HookPayload/HookResult、通用 Interceptor scope、RuntimeSafetyPolicy 共存、ControlScope、checkpoint_store 接入要求。
- 2026-04-28：拆分 HookRunner、InterceptorChain、Control 平面、Preset/checkpoint、ReActAgent 接入步骤，避免单步覆盖过宽。
- 2026-04-28：主设计补充 `llm_stream` scope、后台工具/进度/pause/resume 的 command/event 预留，以及 `ControlCommandHandler` 注册机制。
- 2026-04-28：ReActAgent 接入采用双路径兼容模式（HookRunner 优先，旧 _call_hooks 回退；InterceptorChain 优先，旧 _execute_tool 回退），确保不破坏现有测试和调用方。
- 2026-04-29：DefaultAgentFactory session 模式补齐 hook_runner/interceptor_chain/checkpoint_store 传递；BotService peer agent 的 PeerAutoSendHook 注入改为优先 HookRunner。
- 2026-04-29：删除 framework/core/hooks.py（已废弃）；删除 TaskInterventionHook（功能由 ControlDrainInterceptor 替代）；ControlDrainInterceptor 支持 CANCEL_RUN + CANCEL_TURN 两种取消命令。
- 2026-04-29：新增 5 个测试文件补齐测试矩阵（37 个测试用例），覆盖 InterceptorChain 异常兜底、ToolApproval 审批行为、ControlChannel TTL、ControlDrain 取消、HookErrorPolicy 策略。
