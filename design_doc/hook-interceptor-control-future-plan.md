# Hook / Interceptor / Control 后续阶段规划

本文说明第一阶段不实现、但后续需要实现的能力。它参考 `intercept-design(todo).md` 的未来场景，并按当前目标设计的单数包名重写：`framework.hook`、`framework.interceptor`、`framework.control`。

## 一、前提

第一阶段必须已经具备：

- `framework.hook`：HookPoint、HookRunner、HookErrorPolicy、现有 hook 改造。
- `framework.interceptor`：InterceptorScope、InterceptorChain、tool/turn/iteration 三个边界。
- `framework.control`：ControlCommand、ControlScope、ControlChannel、ControlEventBus、ControlDrain、CheckpointStore。
- 代码装配式 `AgentRuntimeConfig`。
- tool approval 两种拒绝模式。
- 最小 checkpoint 和 termination metadata。

这些基础如果缺失，后续能力会变成硬编码补丁，扩展成本会明显上升。

## 二、阶段 2：Control 状态机

目标：实现外部运行时控制，不只支持 cancel。

能力：

- `PAUSE_RUN`
- `RESUME_RUN`
- `INJECT_USER_MESSAGE`
- 外部 `/stop`、`/pause`、`/resume`、人工补充消息。
- command priority、TTL、idempotency、auth_context。
- `ControlCommandHandler` 注册机制。

设计要求：

- pause 不是简单 return，需要明确 run 状态：`running`、`pausing`、`paused`、`resuming`、`cancelled`、`completed`。
- pause 时必须保存 checkpoint 或稳定恢复点。
- resume 只能从 checkpoint 恢复，不能复用已关闭的 coroutine。
- watcher 只能 `peek`，不能抢 `drain`。

当前设计是否支持：支持。主设计已补 `PAUSE_RUN`、`RESUME_RUN`、handler registry、CheckpointStore。

## 三、阶段 3：Streaming Interceptor

目标：让 interceptor 能作用于 LLM streaming，而不只包裹一次完整 LLM call。

能力：

- `InterceptorScope.LLM_STREAM`
- stream chunk 观察、过滤、审计、脱敏。
- stream idle timeout。
- streaming 中外部 cancel。
- partial output finalize。

建议接口：

```python
class StreamInterceptor(Protocol):
    async def around_llm_stream(
        self,
        ctx: AgentContext,
        call: LLMStreamContext,
        next_stream: LLMStreamNext,
    ) -> AsyncIterator[LLMStreamChunk]:
        ...
```

设计要求：

- chunk interceptor 不能破坏最终消息聚合。
- idle timeout owner 必须明确，不能和 provider timeout 重复。
- cancel 时要记录 partial output 是否保留。

当前设计是否支持：基本支持。已预留 `llm_stream` scope；后续需要补 `LLMStreamContext`、`LLMStreamChunk`、`LLMStreamNext`。

## 四、阶段 4：后台/异步工具

目标：支持长耗时工具后台执行、进度上报和结果回收。

能力：

- `BACKGROUND_TOOL_STARTED`
- `BACKGROUND_TOOL_PROGRESS`
- `BACKGROUND_TOOL_COMPLETED`
- `BACKGROUND_TOOL_RESULT`
- `BACKGROUND_TOOL_PROGRESS`
- start/poll 风格工具能力。

关键修正：不要采用“写入占位 tool result 后再替换历史”的方案。模型可能已经基于占位结果继续推理，替换历史会造成不可解释状态。

推荐模式：

- `start_background_tool` 返回 task id。
- `poll_background_tool` 查询结果。
- 或后台结果作为新的 control command / user-visible event 注入下一轮。
- 结果回收必须通过 call_id / task_id / correlation_id 关联。

当前设计是否支持：支持。主设计已预留 background command/event 类型，但后续需要新增 BackgroundTaskRegistry 和结果注入策略。

## 五、阶段 5：恢复与 Checkpoint 完整协议

目标：从“最小保存”升级到可恢复运行。

能力：

- checkpoint schema 版本化。
- pending tool calls 记录。
- termination metadata 标准化。
- restore_to_messages。
- approval denied cancel 后恢复。
- crash recovery。

设计要求：

- checkpoint 不能保存未脱敏 secret。
- pending tool calls 恢复时必须补齐合法 tool result，或回滚到 tool_call 前稳定点。
- checkpoint id、turn id、session id、agent id 必须关联。

当前设计是否支持：部分支持。第一阶段的 CheckpointStore 接口足够，但需要补 `AgentCheckpoint` 结构、版本字段、pending_tool_calls。

## 六、阶段 6：更广 AOP 接入

目标：把 interceptor 从 ReActAgent 扩展到框架其他调用边界。

范围：

- `agent_run`
- `pipeline_step`
- `pool_task`
- `memory_operation`
- `llm_call`

示例：

- Pipeline step timeout / audit。
- AgentPool task cancellation / routing audit。
- Memory operation compact / token budget / policy guard。
- LLM fallback / response validation。

设计要求：

- 每个 scope 都要有结构化 context 和 next-call protocol。
- 不允许用一个万能 `dict` context 承载所有 scope。
- 每个 scope 的异常策略必须独立定义。

当前设计是否支持：支持。主设计已预留 InterceptorScope 和 context 契约；后续要逐 scope 接入。

## 七、阶段 7：治理与预算策略

目标：把 token budget、microcompact、tool-chain repair 等治理能力放到合适边界。

建议归属：

| 能力 | 归属 |
|------|------|
| token budget 超限取消 | `PresetControlRule` -> `ControlCommand` |
| wall clock budget | `PresetControlRule` 或 timeout interceptor |
| tool chain repair | `framework.interceptor` 的 tool/iteration scope |
| microcompact | memory 层或 `memory_operation` interceptor |
| sensitive data redaction | approval interceptor / event bus serializer |

当前设计是否支持：支持。已有 PresetControlRule、memory_operation scope、tool/iteration scope。

## 八、阶段 8：插件化扩展

目标：让外部插件以代码方式贡献 hook、interceptor、control handler。

能力：

- `PluginContext.register_hook(...)`
- `PluginContext.register_interceptor(...)`
- `PluginContext.register_control_handler(...)`
- `PluginContext.register_preset_rule(...)`

设计要求：

- 插件注册的是强类型对象，不是 dict。
- 插件不能直接拿到 secret。
- 插件提供的 approval/event 输出要经过脱敏。

当前设计是否支持：部分支持。代码装配 API 支持对象注入；后续需要定义 PluginContext 扩展接口。

## 九、阶段 9：动态代码配置

目标：支持运行期替换策略，但仍以代码配置对象为核心。

能力：

- runtime config snapshot。
- hook/interceptor/control handler 增删。
- safe reload。
- per-agent / per-session runtime policy。

设计要求：

- 不在核心层引入 YAML。
- 如未来支持 YAML，只作为薄解析层，生成 `AgentRuntimeConfig`。
- 热更新必须保证正在执行的 turn 使用稳定 snapshot。

当前设计是否支持：部分支持。`AgentRuntimeConfig` 和对象装配支持静态组合；后续需要 RuntimeConfigRegistry / snapshot。

## 十、阶段 10：完整审批工作流

目标：从 tool 前审批扩展到多类审批。

能力：

- tool approval。
- LLM 高风险输出审批。
- memory write approval。
- external action approval。
- approval timeout escalation。
- batch tool approval。

设计要求：

- approval request 必须脱敏。
- approval response 必须通过 correlation_id 匹配。
- timeout 行为可选：tool_error、cancel_turn、cancel_run。
- batch tool call 中每个 tool_call 都必须有合法结果或 checkpoint 恢复策略。

当前设计是否支持：支持。ToolApprovalInterceptor 和 ControlEventBus 是基础；后续扩展 approval scope。

## 十一、需要回改主设计的检查

已检查当前主设计，后续扩展基本有承载点。为保证后续能力可落地，已同步补充：

- `InterceptorScope.LLM_STREAM`
- `ControlCommandType.BACKGROUND_TOOL_RESULT`
- `ControlCommandType.BACKGROUND_TOOL_PROGRESS`
- `ControlCommandType.PAUSE_RUN`
- `ControlCommandType.RESUME_RUN`
- background / pause / resume 相关 `ControlEventType`
- `ControlCommandHandler` 注册机制

因此，当前主设计不需要大改；后续实现时应坚持以下原则：

- 新能力通过新增 handler、scope、context 接入。
- 不改核心 channel/event bus/drain 接口。
- 不回到硬编码字符串和裸 dict。
- 不让 interceptor 绑定 ReAct turn。
