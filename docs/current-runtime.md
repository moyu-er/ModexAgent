# Current Runtime Architecture

Date: 2026-05-02

This document records the current runtime shape after the ReAct graph, hook,
interceptor, control, approval, and runtime state updates. It is the source of
truth for project Markdown files that mention runtime behavior.

## ReAct Runtime

`framework.agents.react` is graph-based. `ReActAgent.run()` prepares the
`AgentContext`, then delegates turn execution to `ReActGraph` and graph nodes:

- `StartNode` initializes or resumes turn state.
- `LLMNode` calls the model, emits streaming events, and records tool calls or
  final output.
- `ToolNode` executes tool calls, applies approval/suspend behavior in full
  mode, and stores cancel/end metadata for cancellation paths.
- `EndNode` builds the final `AgentResult`, including `turn_cancelled` when a
  control or cancellation path ends the turn early.

`TurnResumeState` stores the resume target explicitly through `resume_node` and
`resume_reason`. Approval resume currently targets the tool node, but the stored
shape leaves room for other suspend/resume points later.

## Runtime Modes

`clean` mode is intended to be a plain ReAct graph run. When clean mode is used,
extra runtime services should be removed at turn entry: hooks, approval,
interceptor/control services, suspend/resume strategy, runtime state store, and
injection queues. The runtime should log one concise line when it sanitizes
these services. Clean mode should avoid scattered feature checks inside nodes.

`full` mode wires the extension systems explicitly:

- hooks observe or transform lifecycle payloads;
- interceptors wrap execution boundaries;
- control drains runtime commands at safe boundaries;
- approval can suspend and resume tool execution;
- runtime state can persist suspend/resume data.

## Hook, Interceptor, And Control Boundaries

Hooks live in `framework.hook`. They are lifecycle extension points. Hooks may
observe and modify context payloads, but they should not wrap execution. Per-turn
state belongs in `ctx.metadata`, not hook instance attributes, so shared hooks
remain safe in pool mode.

Interceptors live in `framework.interceptor`. They are onion-chain wrappers for
execution boundaries such as turn, iteration, LLM stream, and tool call. They are
the right place for timeout, approval policy enforcement, result transformation,
and control drains that must wrap a callable.

Control lives in `framework.control`. It is the runtime command plane for
messages or instructions sent by users or systems while an agent is running. The
current implementation supports command/event/checkpoint primitives and safe
boundary handling. Future live intervention should add an active-operation
registry so control can target an LLM stream or a running tool by operation ID.

Control should not be modeled only as normal graph nodes. Graph nodes are useful
for durable macro steps such as approval gates, async tool joins, or explicit
control checkpoints. Low-latency intervention still needs runtime services that
can drain commands and target active operations between graph nodes.

## Runtime State Store

The control checkpoint module now exposes runtime-state naming aliases:

- `RuntimeStateStore`
- `JsonFileRuntimeStateStore`
- `NoOpRuntimeStateStore`

These aliases point to the existing checkpoint store implementations and should
be preferred by ReAct/runtime integration code. The older `CheckpointStore`
names remain compatible for generic control checkpoint use.

## bot_project Defaults

`examples.bot_project` is the main end-to-end reference. Its default interceptor
chain currently includes:

- `ControlDrainInterceptor`
- `ToolResultLimitInterceptor`

`TurnTimeoutInterceptor` and `ToolTimeoutInterceptor` are not part of the
default bot runtime chain. Tool timeout belongs to the ReAct tool execution path
and configured runtime safety policy; turn timeout should be added only when the
turn/iteration interceptor scopes are fully owned by the ReAct runtime.

The example configuration no longer carries redundant default `approval:`
settings. Approval wiring should come from runtime construction and explicit
policy objects rather than duplicated config defaults.

## Design Direction

The target layering is:

1. Pipeline assembles runtime services and handles platform I/O.
2. ReAct owns turn, iteration, LLM, tool, approval, and resume boundaries.
3. Hooks observe/transform payloads.
4. Interceptors wrap execution scopes.
5. Control stores, receives, drains, and eventually targets live operations.

See `docs/architecture-graph-approval.md` for the detailed graph, hook,
interceptor, control, and approval design.
