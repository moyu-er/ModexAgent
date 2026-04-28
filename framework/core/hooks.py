"""此模块已废弃。

AgentRunHook、RuntimeContextHook、CompositeRunHook、RunLoggingHook 已迁入新包：
  - framework.hook.Hook          → Hook 协议
  - framework.hook.builtin.RunLoggingHook        → RunLoggingHook
  - framework.hook.builtin.RuntimeContextHook    → RuntimeContextHook
  - framework.hook.HookRunner    → 替代 CompositeRunHook

请从新路径导入，本文件不再作为对外公开 API。"""
