# Subagent Invocation UUID — Incremental Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `dispatch_task` tool and `invocation_id` UUID for multi-invocation context isolation — each task dispatch creates an isolated session, and follow-up messages route to the correct session via optional `invocation_id`.

**Architecture:** Extend `framework/multi_agent/tools.py` with `DispatchTaskTool` (creates invocation_id, builds isolated session_id) and add optional `invocation_id` parameter to `SendMessageAsyncTool.execute()`. UUID serves triple role: context isolation (appended to session_id), request-result correlation (stored as correlation_id), and follow-up routing key (passed as invocation_id param). Bot registers `DispatchTaskTool` for main agent only; subagents retain only `SendMessageAsyncTool`. No changes to AgentPool, SubagentService, or memory layer.

**Tech Stack:** Python 3.12+, asyncio

**Relationship to base plan:** This plan is an **incremental extension** of `docs/superpowers/plans/2026-05-21-subagent-refactoring-plan.md`. It adds tasks 5a–5f *after* the existing Phase 5 tasks (5.1–5.3). Dependency: Base Plan Phases 0–4 must be complete before executing these tasks. Tasks 5a and 5b (framework) must complete before 5c (bot registration). 5d (system prompts) can run in parallel with 5e–5f (tests).

---

## Phase 5a: Incremental Additions — Invocation UUID

### Task 5a.1: Add `DispatchTaskTool` to `framework/multi_agent/tools.py`

**Files:**
- Modify: `framework/multi_agent/tools.py` (append new class)

- [ ] **Step 1: Read the current tools.py imports and top-level structure**

Open `framework/multi_agent/tools.py`. Note the existing imports: `Tool`, `MessageBroker`, `AgentAddress`, `AgentMessageBus`, `AgentRegistry`, `SessionIdStrategy`, `DefaultSessionIdStrategy`, `asyncio`, `uuid`.

- [ ] **Step 2: Add the `DispatchTaskTool` class after `SendMessageAsyncTool`**

At the end of `framework/multi_agent/tools.py`, after the `SendMessageAsyncTool` class, append:

```python
class DispatchTaskTool(Tool):
    """Dispatch a new task to a subagent.

    Creates an isolated invocation session and returns an invocation_id for
    follow-up communication via send_message_async.

    Use this when:
    - Starting a new, independent task for a subagent
    - You need clean context (no history leakage from other tasks)
    - You want to run multiple tasks on the same subagent in parallel

    The returned invocation_id can be passed to send_message_async for
    follow-up messages targeting this specific task session.
    """

    def __init__(
        self,
        broker: MessageBroker,
        self_address: AgentAddress,
        allowed_callers: list[str] | None = None,
        allowed_targets: list[str] | None = None,
        agent_bus: AgentMessageBus | None = None,
        registry: AgentRegistry | None = None,
        session_strategy: SessionIdStrategy | None = None,
    ):
        import uuid as _uuid

        self._broker = broker
        self._self_address = self_address
        self._allowed_callers = set(allowed_callers) if allowed_callers else None
        self._allowed_targets = set(allowed_targets) if allowed_targets else None
        self._agent_bus = agent_bus
        self._registry = registry
        self._session_strategy = session_strategy or DefaultSessionIdStrategy()
        self._uuid = _uuid
        super().__init__(
            name="dispatch_task",
            description=(
                "Dispatch a new task to a subagent. Creates an isolated session "
                "and returns an invocation_id for follow-up communication.\n\n"
                "Use this when starting a new, independent task that should not "
                "share context with other tasks. The returned invocation_id can "
                "be passed to send_message_async for follow-up messages.\n\n"
                "IMPORTANT: This sends the task asynchronously. The subagent will "
                "process it and send results to your inbox."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target_agent": {
                        "type": "string",
                        "description": (
                            "Name of the subagent to dispatch the task to. "
                            "Must be a known subagent."
                        ),
                    },
                    "task_prompt": {
                        "type": "string",
                        "description": (
                            "The task description. Be thorough and precise. "
                            "Include all information the subagent needs."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "Optional additional context or background information "
                            "to include with the task."
                        ),
                    },
                },
                "required": ["target_agent", "task_prompt"],
            },
        )

    def _is_allowed(self, caller_context: dict | None = None) -> bool:
        if self._allowed_callers is None:
            return True
        if not caller_context:
            return True
        agent_name = caller_context.get("agent_name")
        return agent_name is None or agent_name in self._allowed_callers

    def _is_target_allowed(self, target_agent: str) -> bool:
        if self._allowed_targets is None:
            return True
        return target_agent in self._allowed_targets

    def _build_session_id(
        self, conversation_id: str, target_agent: str, invocation_id: str | None = None
    ) -> str:
        base = f"{conversation_id}:{target_agent}"
        if invocation_id:
            return f"{base}:{invocation_id}"
        return base

    async def execute(
        self, target_agent: str, task_prompt: str, context: str | None = None
    ) -> str:
        caller_context = None  # resolved by caller if available

        if not self._is_allowed(caller_context):
            return "Error: dispatch_task is not allowed for this caller."

        if not self._is_target_allowed(target_agent):
            return f"Error: dispatch_task to {target_agent} is not allowed by policy."

        # Registry existence check
        if self._registry is not None:
            available = [p.name for p in self._registry.list_profiles()]
            if target_agent not in available:
                return (
                    f"Error: Target agent '{target_agent}' not found. "
                    f"Available agents: {', '.join(available)}"
                )

        # Generate invocation_id
        inv_id = f"inv_{self._uuid.uuid4().hex[:12]}"

        # Resolve conversation_id from context
        from framework.multi_agent.context import current_conversation_id

        conv_id = current_conversation_id.get() or ""
        if not conv_id:
            conv_id = "default"

        # Build isolated session_id
        session_id = self._build_session_id(conv_id, target_agent, inv_id)

        # Build content with optional context
        content = task_prompt
        if context:
            content = f"{task_prompt}\n\n[Additional Context]\n{context}"

        from .address import AgentAddress
        from .envelope import AgentMessageEnvelope

        envelope = AgentMessageEnvelope(
            payload={
                "content": content,
                "task_prompt": task_prompt,
                "message_type": "task_request",
                "invocation_id": inv_id,
            },
            source=AgentAddress(kind=self._self_address.kind, name=self._self_address.name),
            target=AgentAddress(kind="agent", name=target_agent),
            message_type="task_request",
            conversation_id=conv_id,
            agent_session_id=session_id,
            correlation_id=inv_id,
        )

        if envelope.target is not None:
            broker_msg = envelope.to_broker_message()
            logger.info(
                "DispatchTaskTool: dispatching to %s inv_id=%s session=%s",
                target_agent,
                inv_id,
                session_id,
            )
            await self._broker.send_to(envelope.target, broker_msg)
            return f"Task dispatched to {target_agent}. invocation_id: {inv_id}"
        return "Error: target agent not specified."
```

- [ ] **Step 3: Run import verification**

```bash
python -c "from framework.multi_agent.tools import DispatchTaskTool; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add framework/multi_agent/tools.py
git commit -m "feat: add DispatchTaskTool with invocation_id session isolation"
```

---

### Task 5a.2: Add `invocation_id` parameter to `SendMessageAsyncTool.execute()`

**Files:**
- Modify: `framework/multi_agent/tools.py:240-338` (SendMessageAsyncTool class)

- [ ] **Step 1: Add `invocation_id` to the tool description**

In `SendMessageAsyncTool.__init__`, update the `description` parameter of `super().__init__()` to include the new parameter. Change:

```python
description=(
    "Send a message to another agent's inbox asynchronously. ..."
),
```

to:

```python
description=(
    "Send a message to another agent's inbox asynchronously. "
    "The target agent will pull it during its next turn.\n\n"
    "If invocation_id is provided, the message is routed to that specific "
    "invocation session. Omit invocation_id to send to the default session."
),
```

- [ ] **Step 2: Add `invocation_id` to the parameters schema**

In the `parameters` dict of `super().__init__()`, add after the `content` property block:

```python
                    "invocation_id": {
                        "type": "string",
                        "description": (
                            "Optional. The invocation_id returned by dispatch_task. "
                            "If provided, routes this message to that specific task session. "
                            "Omit to send to the default session."
                        ),
                    },
```

- [ ] **Step 3: Update `execute()` signature to accept `invocation_id`**

Change the `execute` method signature from:

```python
    async def execute(self, target_agent, content, *,
                      conversation_id="", message_type="agent_message"):
```

to:

```python
    async def execute(self, target_agent, content, *,
                      conversation_id="", message_type="agent_message",
                      invocation_id=None):
```

- [ ] **Step 4: Update session_id and payload construction in `execute()` body**

Replace the session_id construction section. Find this block (~line 325-330):

```python
        envelope = AgentMessageEnvelope(
            payload={"content": content, "message_type": message_type},
            source=AgentAddress(kind=self._self_address.kind, name=self._self_address.name),
            target=AgentAddress(kind="agent", name=target_agent),
            message_type=message_type,
            conversation_id=conversation_id,
            agent_session_id=agent_session_id
            or self._session_strategy.target_session(conversation_id, target_agent, self._self_address.name),
        )
```

Replace with:

```python
        # Build session_id with optional invocation suffix
        base_session = agent_session_id or self._session_strategy.target_session(
            conversation_id, target_agent, self._self_address.name,
        )
        if invocation_id:
            base_session = f"{base_session}:{invocation_id}"

        payload = {"content": content, "message_type": message_type}
        if invocation_id:
            payload["invocation_id"] = invocation_id

        envelope = AgentMessageEnvelope(
            payload=payload,
            source=AgentAddress(kind=self._self_address.kind, name=self._self_address.name),
            target=AgentAddress(kind="agent", name=target_agent),
            message_type=message_type,
            conversation_id=conversation_id,
            agent_session_id=base_session,
        )
```

- [ ] **Step 5: Run import and basic smoke test**

```bash
python -c "from framework.multi_agent.tools import SendMessageAsyncTool; print('OK')"
```
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add framework/multi_agent/tools.py
git commit -m "feat: add optional invocation_id parameter to SendMessageAsyncTool"
```

---

### Task 5a.3: Register `DispatchTaskTool` in bot `builders.py` (main agent only)

**Files:**
- Modify: `examples/bot_project/bot/service/builders.py` (in `_register_multi_agent_tools`)

- [ ] **Step 1: Find where `SendMessageAsyncTool` is registered for main agent**

```bash
grep -n "SendMessageAsyncTool\|send_message_async" examples/bot_project/bot/service/builders.py
```

- [ ] **Step 2: Add `DispatchTaskTool` import**

At the top of `builders.py`, add to the existing multi_agent imports:

```python
from framework.multi_agent.tools import DispatchTaskTool
```

- [ ] **Step 3: Register `DispatchTaskTool` for main agent**

In `_register_multi_agent_tools()`, after `SendMessageAsyncTool` registration block, add:

```python
        # 2. Task dispatch — creates invocation session (main only)
        tool_manager.register(DispatchTaskTool(
            broker=self.broker, self_address=parent_address,
            allowed_targets=peer_names or None, agent_bus=self.agent_bus,
            registry=self.agent_pool, session_strategy=strategy,
        ))
        print("   [OK] dispatch_task registered")
```

- [ ] **Step 4: Verify import**

```bash
cd examples/bot_project && python -c "import sys; sys.path.insert(0, '.'); from bot.service.builders import AgentBuilderMixin; print('OK')"
```
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/bot/service/builders.py
git commit -m "feat: register DispatchTaskTool for main agent in bot builders"
```

---

### Task 5a.4: Update bot system prompts for `invocation_id` usage

**Files:**
- Modify: `examples/bot_project/config/bot_config.yml` (main agent and subagent system prompts)

- [ ] **Step 1: Find the main agent system prompt**

```bash
grep -n "system_prompt\|## 核心规则" examples/bot_project/config/bot_config.yml | head -20
```

- [ ] **Step 2: Add task dispatch guidance to main agent system prompt**

In the main agent's `system_prompt` section, add:

```yaml
      ## 调用 Subagent

      当需要让 subagent 执行独立任务时，使用 dispatch_task：
      - dispatch_task 会创建一个隔离的会话，返回 invocation_id
      - 同一个 subagent 可以同时处理多个独立任务（每个有自己的 invocation_id）
      - 后续跟进消息时，用 send_message_async 并传入 invocation_id
      - 不要把不同任务的内容混在一个会话里

      示例：
      1. dispatch_task(target_agent="office-expert", task_prompt="review file A")
         → returns: "Task dispatched to office-expert. invocation_id: inv_a1b2c3d4e5f6"
      2. send_message_async(target_agent="office-expert", content="also check tests",
                            invocation_id="inv_a1b2c3d4e5f6")
```

- [ ] **Step 3: Update subagent system prompts**

In each subagent's `system_prompt` section, add or update:

```yaml
      ## 回复父 Agent

      当你收到带有 invocation_id 的任务时：
      - 你的会话是隔离的，看不到其他任务的历史
      - 用 send_message_async(invocation_id="{收到的UUID}", content="...") 汇报结果
      - 不要遗漏 invocation_id，否则会发到默认会话

      当你收到没有 invocation_id 的普通消息时：
      - 这是对默认会话的跟进
      - 用 send_message_async(content="...") 回复即可
```

- [ ] **Step 4: Commit**

```bash
git add examples/bot_project/config/bot_config.yml
git commit -m "docs: add invocation_id usage guidance to bot system prompts"
```

---

### Task 5a.5: Add unit tests for `DispatchTaskTool`

**Files:**
- Modify: `tests/unit/multi_agent/test_tools_enhanced_validation.py` (append new test class)

- [ ] **Step 1: Add `DispatchTaskTool` import to test file**

```python
from framework.multi_agent.tools import DispatchTaskTool
```

- [ ] **Step 2: Add test class at end of test file**

```python
class TestDispatchTaskTool:
    """dispatch_task must create invocation_id and isolated session."""

    async def test_dispatch_returns_invocation_id(self):
        """dispatch_task should return invocation_id in the response string."""
        broker = InMemoryMessageBroker()
        await broker.start()
        target_addr = AgentAddress(kind="agent", name="worker")
        await broker.register_consumer(target_addr)

        tool = DispatchTaskTool(
            broker=broker,
            self_address=AgentAddress(name="main"),
            allowed_targets=["worker"],
        )

        result = await tool.execute(
            target_agent="worker",
            task_prompt="review file A",
        )

        assert "invocation_id:" in result, (
            f"Response should contain invocation_id, got: {result!r}"
        )
        inv_id = result.split("invocation_id: ")[-1].strip()
        assert inv_id.startswith("inv_"), (
            f"invocation_id should start with 'inv_', got: {inv_id!r}"
        )
        await broker.stop()

    async def test_dispatch_creates_isolated_session_ids(self):
        """Two dispatch_task calls to the same target should produce
        different invocation_ids (and therefore different session_ids)."""
        broker = InMemoryMessageBroker()
        await broker.start()
        target_addr = AgentAddress(kind="agent", name="worker")
        await broker.register_consumer(target_addr)

        tool = DispatchTaskTool(
            broker=broker,
            self_address=AgentAddress(name="main"),
            allowed_targets=["worker"],
        )

        r1 = await tool.execute(target_agent="worker", task_prompt="task A")
        r2 = await tool.execute(target_agent="worker", task_prompt="task B")

        inv1 = r1.split("invocation_id: ")[-1].strip()
        inv2 = r2.split("invocation_id: ")[-1].strip()
        assert inv1 != inv2, (
            f"Each dispatch must get unique invocation_id, got {inv1!r} twice"
        )
        await broker.stop()

    async def test_dispatch_allows_registered_target(self):
        """Target in registry should pass ACL and existence check."""
        broker = InMemoryMessageBroker()
        await broker.start()
        target_addr = AgentAddress(kind="agent", name="worker")
        await broker.register_consumer(target_addr)

        registry = FakeRegistry([FakeProfile("main"), FakeProfile("worker")])
        tool = DispatchTaskTool(
            broker=broker,
            self_address=AgentAddress(name="main"),
            registry=registry,
            allowed_targets=["worker"],
        )

        result = await tool.execute(target_agent="worker", task_prompt="task")
        assert "invocation_id:" in result
        await broker.stop()

    async def test_dispatch_blocks_disallowed_target(self):
        """ACL should block dispatch to non-allowed targets."""
        tool = DispatchTaskTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            allowed_targets=["allowed_agent"],
        )

        result = await tool.execute(
            target_agent="blocked_agent",
            task_prompt="task",
        )

        assert "not allowed" in result
        assert "invocation_id:" not in result

    async def test_dispatch_blocks_nonexistent_target(self):
        """Registry should reject unknown agents."""
        registry = FakeRegistry([FakeProfile("main"), FakeProfile("known")])
        tool = DispatchTaskTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            registry=registry,
        )

        result = await tool.execute(target_agent="unknown", task_prompt="task")
        assert "not found" in result
        assert "invocation_id:" not in result
```

- [ ] **Step 2: Run the new tests**

```bash
pytest tests/unit/multi_agent/test_tools_enhanced_validation.py::TestDispatchTaskTool -v
```
Expected: 5 passed

- [ ] **Step 3: Commit**

```bash
git add tests/unit/multi_agent/test_tools_enhanced_validation.py
git commit -m "test: add DispatchTaskTool unit tests"
```

---

### Task 5a.6: Add unit tests for `invocation_id` session routing

**Files:**
- Modify: `tests/unit/multi_agent/test_tools_enhanced_validation.py` (append new test class)

- [ ] **Step 1: Add test class for `SendMessageAsyncTool` invocation_id routing**

Append to `test_tools_enhanced_validation.py`:

```python
class TestSendMessageAsyncInvocationRouting:
    """send_message_async must route to correct session based on invocation_id."""

    async def test_with_invocation_id_appends_to_session(self):
        """When invocation_id is provided, session_id must include it."""
        agent_bus = _make_bus()
        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            allowed_targets=["worker"],
            agent_bus=agent_bus,
        )

        await tool.execute(
            target_agent="worker",
            content="followup message",
            conversation_id="conv_001",
            message_type="agent_message",
            invocation_id="inv_test123",
        )

        inbox_key = "conv_001:worker"
        envelopes = await agent_bus.poll(inbox_key, limit=1)
        assert len(envelopes) == 1, "Expected 1 envelope"
        envelope = envelopes[0]
        assert envelope.agent_session_id.endswith(":inv_test123"), (
            f"Session ID should end with :inv_test123, got {envelope.agent_session_id!r}"
        )
        assert envelope.payload.get("invocation_id") == "inv_test123", (
            "Payload should contain invocation_id"
        )

    async def test_without_invocation_id_uses_default_session(self):
        """Without invocation_id, session_id should NOT include invocation suffix."""
        agent_bus = _make_bus()
        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            allowed_targets=["worker"],
            agent_bus=agent_bus,
        )

        await tool.execute(
            target_agent="worker",
            content="hello",
            conversation_id="conv_001",
            message_type="agent_message",
        )

        inbox_key = "conv_001:worker"
        envelopes = await agent_bus.poll(inbox_key, limit=1)
        assert len(envelopes) == 1
        envelope = envelopes[0]
        assert ":inv_" not in envelope.agent_session_id, (
            f"Session ID should not contain invocation suffix, got {envelope.agent_session_id!r}"
        )
        assert "invocation_id" not in envelope.payload, (
            "Payload should not contain invocation_id when not provided"
        )

    async def test_different_invocations_go_to_different_sessions(self):
        """Two messages with different invocation_ids should go to different sessions."""
        agent_bus = _make_bus()
        tool = SendMessageAsyncTool(
            broker=InMemoryMessageBroker(),
            self_address=AgentAddress(name="main"),
            allowed_targets=["worker"],
            agent_bus=agent_bus,
        )

        await tool.execute(
            target_agent="worker", content="msg A",
            conversation_id="conv_001", invocation_id="inv_aaa",
        )
        await tool.execute(
            target_agent="worker", content="msg B",
            conversation_id="conv_001", invocation_id="inv_bbb",
        )

        inbox_key = "conv_001:worker"
        envelopes = await agent_bus.poll(inbox_key, limit=2)
        assert len(envelopes) == 2
        sessions = {e.agent_session_id for e in envelopes}
        assert len(sessions) == 2, (
            f"Two invocations should produce 2 different sessions, got {sessions}"
        )
```

- [ ] **Step 2: Run the new tests**

```bash
pytest tests/unit/multi_agent/test_tools_enhanced_validation.py::TestSendMessageAsyncInvocationRouting -v
```
Expected: 3 passed

- [ ] **Step 3: Run full multi_agent test suite**

```bash
pytest tests/unit/multi_agent/test_tools_enhanced_validation.py tests/unit/multi_agent/test_pool.py -v
```
Expected: all pass (15+ tests now)

- [ ] **Step 4: Commit**

```bash
git add tests/unit/multi_agent/test_tools_enhanced_validation.py
git commit -m "test: add invocation_id session routing tests for SendMessageAsyncTool"
```

---

## Verification

After all tasks complete:

- [ ] `dispatch_task` creates isolated session (two calls → two different session_ids)
- [ ] `dispatch_task` returns `invocation_id` in tool response string
- [ ] `invocation_id` present in envelope payload and as `correlation_id`
- [ ] `send_message_async(invocation_id=uuid)` routes to correct session (session_id ends with invocation suffix)
- [ ] `send_message_async()` without invocation_id routes to default session (no suffix, no payload field)
- [ ] Multiple concurrent invocations of same subagent produce different sessions
- [ ] Subagent cannot use `dispatch_task` (tool not registered)
- [ ] Framework tests pass: `pytest tests/unit/multi_agent/test_tools_enhanced_validation.py tests/unit/multi_agent/test_pool.py -v`
- [ ] Type check: `mypy framework/multi_agent/tools.py`
