# Repository Guidelines

## Project Layout

`framework/` is the reusable agent framework. Key areas:

- `framework/agents/react/`: graph-based ReAct runtime.
- `framework/memory/`: session/archive/knowledge memory, compression, retention, and governance.
- `framework/multi_agent/`: peer/subagent coordination.
- `framework/tools/`, `framework/plugins/`, `framework/pipeline/`, `framework/hook/`, `framework/interceptor/`, `framework/control/`: tools, extensions, orchestration, lifecycle hooks, AOP wrappers, and runtime control.

`examples/bot_project/` is the primary end-to-end reference. Keep framework-generic behavior in `framework/`; keep QQ bot/business wiring in `examples/`.

## Commands

- `uv pip install -e ".[dev]"`: install development dependencies.
- `pytest tests/unit`: run fast unit tests.
- `PYTHONPATH=. python -m pytest examples/bot_project/tests/ -v`: run bot project tests.
- `ruff check framework tests`: lint.
- `ruff format framework tests`: format.
- `mypy framework`: type-check framework code.

## Coding Rules

- Python 3.12+, `from __future__ import annotations` in framework modules.
- Prefer enums/constants over raw strings for categories, roles, states, and protocol values.
- Prefer typed structures over loose dicts, using existing models such as `ChatMessage`, `ToolCall`, `LLMResponse`, `InputMessage`, and `OutputMessage`.
- Public functions need typed parameters and return values; avoid bare `Any`, `list`, `dict`, `object`, and `list[Any]` in framework-facing APIs.
- Use Protocols/ABCs for extension points; avoid depending on concrete implementations where a pluggable contract exists.
- Keep reusable framework behavior in `framework/` and example/business wiring in `examples/`; do not hard-code example-specific configuration into the framework.
- Avoid dynamic access such as `getattr`, `hasattr`, `*attr` unless it is needed for a real extension boundary or compatibility layer.
- `MessageRole` lives in `framework.core.types.MessageRole`; do not introduce
  another role enum in constants or feature modules.
- Hook per-turn state belongs in `ctx.metadata`, not shared instance attributes, unless keyed by `session_id`.

## Memory Rules

- Compression mutates persisted session/archive memory. It is checked after session message append via lifecycle.
- Governance only mutates the LLM input copy immediately before model calls. Governance output must not be written back to session.
- `keep_ratio_for_messages` and `keep_ratio_for_token` are hard caps for persistent compression.
- Prefer preserving human `user` inputs over `agent` inputs; `agent` inputs outrank assistant/tool process messages.
- `archive=None` is the standard session-only mode for peer/subagent memory. Do not add separate peer/subagent truncation or commit policies.
- ReAct tool processes must stay structurally legal: do not split `assistant.tool_calls` from matching `tool` results. Compression should skip open tool-call states and governance should repair model-visible copies.
- Subagent session memory is temporary and should be cleared after the subagent finishes.

See `examples/bot_project/docs/memory-system.md` for details.

## Testing

Add focused regression tests under `tests/unit/` for framework changes, especially memory, governance, tools, multi-agent routing, and sandbox behavior. For behavior changes, write or update tests before production code when practical.

## Approval Architecture Rules (CRITICAL)

These rules were distilled from design docs and bug fixes. Violating any of them
will re-introduce known bugs or create a second, conflicting approval path.

1. **One approval path only.** Approval must be implemented through the pipeline
   layer: `ToolNode` → `ApprovalTransaction` → `TurnSnapshot` → `ApprovalRenderer`.
   Do NOT add approval logic to interceptors, hooks, or control channel consumers.

2. **`ControlDrainInterceptor` must not drain `APPROVAL_RESPONSE`.**
   The drain set is for cancel / inject / config commands only.

3. **`ApprovalRuntime` is a policy service, not a state owner.** It owns
   classifier + deny_policy. State lives in `ApprovalTransaction` inside
   `ReActTurnState`.

4. **Deny policy defaults to `TOOL_RESULT_ONLY`.** Rejection (both `/deny` and
   unrelated input) returns tool errors with `deny_reason`, then continues the
   ReAct loop. Turn cancellation (`CANCEL_TURN`) is a configurable override.

5. **EXTENSION POINT comments required.** Any behaviour that is currently
   hard-coded but intended to be configurable in the future must be marked with
   an `EXTENSION POINT` comment explaining what, why, and how to configure it.

6. **`deny_reason` lives on `ApprovalTransaction.deny_reason`.** Do not read it
   from `ctx.metadata`, `ReActMetaKey`, or any other location.

7. **State migration must preserve existing logic.** When migrating from old
   state keys (e.g. `ReActMetaKey`) to typed state (`TurnCustomKey`), verify
   that every old key's behaviour has a corresponding new path. The
   `PRE_APPROVED_TOOL_IDS` loss during migration is the canonical example.
