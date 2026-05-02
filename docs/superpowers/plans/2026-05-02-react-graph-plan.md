# ReAct Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the monolithic `ReActAgent.run()` with a clean abstract graph engine (`framework/core/graph/`), node-based ReAct loop, pluggable suspend strategy, and proper approval state persistence.

**Architecture:** Three-layer design — (1) `framework/core/graph/` generic Node/Edge/Graph/Engine with interrupt/resume primitives; (2) `framework/agents/react/` ReAct-specific nodes, strategies, and graph wiring; (3) `framework/pipeline/` updated to inject strategy and handle approval isolation.

**Tech Stack:** Python 3.11+, asyncio, dataclasses, pytest, ruff, mypy

**Spec:** `docs/superpowers/specs/2026-05-02-react-graph-redesign.md`

---

## File Structure Map

```
framework/core/graph/           [NEW] Generic graph abstractions
├── constants.py                GraphNode, GraphMetaKey
├── node.py                     Node ABC, NodeTransition
├── graph.py                    Edge, Graph
├── engine.py                   GraphEngine
├── interrupt.py                GraphInterrupt, interrupt(), Command, _current_resume
└── __init__.py                 Re-exports

framework/core/
├── context_extensions.py       [NEW] ExtensionKey
└── agent.py                    [MODIFY] Thin AgentContext, ctx_ext(), to_messages()

framework/approval/             [NEW] Approval state persistence
├── constants.py                ApprovalDecision, ApprovalTier, ApprovalStatus
├── state.py                    ApprovalRequest, ApprovalState
├── store.py                    ApprovalStateStore ABC + LocalFile + InMemory
└── __init__.py

framework/agents/react/
├── constants.py                [NEW] ReActNode, ReActReason, ReActMetaKey
├── state.py                    [NEW] TurnResumeState + TurnResumeStateStore
├── strategy.py                 [NEW] SuspendStrategy ABC + InlineWait + SuspendResume
├── graph.py                    [NEW] ReActGraph (clean/full)
├── agent.py                    [MODIFY] ReActAgent thin shell (~150 lines)
├── builder.py                  [MODIFY] Small adapt if needed
└── nodes/                      [NEW]
    ├── __init__.py
    ├── start.py                StartNode
    ├── llm.py                  LLMNode
    ├── tool.py                 ToolNode
    └── end.py                  EndNode

framework/pipeline/
└── pipeline.py                 [MODIFY] Strategy injection, approval consumption, buffer

tests/unit/core/
└── test_agent_context.py       [MODIFY] Update for new to_messages() behavior

tests/unit/core/graph/          [NEW]
├── test_node.py
├── test_graph.py
├── test_engine.py
└── test_interrupt.py

tests/unit/agents/react/        [NEW]
├── test_constants.py
├── test_nodes.py
├── test_graph.py
├── test_strategy.py
└── test_agent.py

tests/unit/approval/            [NEW]
├── test_state.py
└── test_store.py
```

---

### Task 1: Graph constants + context extension keys

**Files:**
- Create: `framework/core/graph/__init__.py` (empty)
- Create: `framework/core/graph/constants.py`
- Create: `framework/core/context_extensions.py`

- [ ] **Step 1: Create graph constants**

Write `framework/core/graph/constants.py`:

```python
"""Core graph constants."""
from enum import StrEnum


class GraphNode(StrEnum):
    """Engine-recognized sentinel node names."""
    END = "__end__"


class GraphMetaKey:
    """Keys used in ctx.metadata by the graph engine."""
    GRAPH_RESULT = "_graph_result"
```

- [ ] **Step 2: Create extension keys**

Write `framework/core/context_extensions.py`:

```python
"""ExtensionKey constants for AgentContext.extensions dict."""


class ExtensionKey:
    HOOK_RUNNER = "hook_runner"
    HOOKS = "hooks"
    INTERCEPTOR_CHAIN = "interceptor_chain"
    CHECKPOINT_STORE = "checkpoint_store"
    RUNTIME_CTX_MGR = "runtime_context_manager"
    RUNTIME_CTX = "runtime_context"
    GOVERNANCE = "governance"
    SAFETY = "safety"
    INJECTION_QUEUE = "injection_queue"
    MAX_TOOLS_PER_TURN = "max_tools_per_turn"
    ON_CHECKPOINT = "on_checkpoint"
    SUSPEND_STRATEGY = "suspend_strategy"
```

- [ ] **Step 3: Verify**

```bash
python -c "from framework.core.graph.constants import GraphNode, GraphMetaKey; print('OK')"
python -c "from framework.core.context_extensions import ExtensionKey; print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add framework/core/graph/ framework/core/context_extensions.py
git commit -m "feat: add core graph constants and extension keys"
```

---

### Task 2: ReAct + Approval constants

**Files:**
- Create: `framework/agents/react/constants.py`
- Create: `framework/approval/__init__.py` (empty)
- Create: `framework/approval/constants.py`

- [ ] **Step 1: Create ReAct constants**

Write `framework/agents/react/constants.py`:

```python
"""ReAct graph constants — node names, transition reasons, metadata keys."""
from enum import StrEnum


class ReActNode(StrEnum):
    START = "start"
    LLM = "llm"
    TOOL = "tool"
    END = "end"


class ReActReason(StrEnum):
    # start → ...
    NORMAL_START = "normal_start"
    RESUME_TOOLS = "resume_tools"
    # llm → ...
    HAS_TOOLS = "has_tools"
    NO_TOOLS = "no_tools"
    MAX_ITERATIONS = "max_iterations"
    LLM_ERROR = "llm_error"
    # tool → ...
    TOOLS_DONE = "tools_done"
    TURN_CANCELLED = "turn_cancelled"
    # end → GraphNode.END
    DONE = "done"


class ReActMetaKey:
    ITERATION = "_react_iteration"
    LLM_RESPONSE = "_llm_response"
    ITERATION_MSGS = "_iteration_messages"
    RESUME_STATE = "_turn_resume_state"
    TOOL_DECISIONS = "_tool_decisions"
    DENY_AS_CANCEL = "_deny_as_cancel"
    APPROVAL_DENIAL = "_approval_denial"
    INJECTION_CYCLE = "_injection_cycle_count"
```

- [ ] **Step 2: Create Approval constants**

Write `framework/approval/constants.py`:

```python
"""Approval system constants."""
from enum import StrEnum


class ApprovalDecision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    PENDING = "pending"
    PREEMPTED = "preempted"


class ApprovalTier(StrEnum):
    NORMAL = "normal"
    DANGEROUS = "dangerous"
    SENSITIVE = "sensitive"
    HARDLINE = "hardline"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    PARTIAL = "partial"
```

- [ ] **Step 3: Write tests**

Write `tests/unit/agents/react/test_constants.py`:

```python
"""Tests for ReAct constants."""
from framework.agents.react.constants import ReActNode, ReActReason, ReActMetaKey
from framework.core.graph.constants import GraphNode


class TestReActNode:
    def test_all_values_are_strings(self):
        for node in ReActNode:
            assert isinstance(node.value, str)

    def test_sentinels_match(self):
        assert GraphNode.END == "__end__"
        assert ReActNode.START == "start"


class TestReActReason:
    def test_all_reasons_defined(self):
        expected = {"normal_start", "resume_tools", "has_tools", "no_tools",
                    "max_iterations", "llm_error", "tools_done", "turn_cancelled", "done"}
        actual = {r.value for r in ReActReason}
        assert actual == expected


class TestReActMetaKey:
    def test_keys_are_strings(self):
        for attr in dir(ReActMetaKey):
            if not attr.startswith("_"):
                assert isinstance(getattr(ReActMetaKey, attr), str)
```

Write `tests/unit/approval/test_state.py` (partial — constants validation):

```python
"""Tests for approval state and constants."""
from framework.approval.constants import ApprovalDecision, ApprovalTier, ApprovalStatus


class TestApprovalConstants:
    def test_decisions(self):
        assert set(ApprovalDecision) == {"allowed", "denied", "pending", "preempted"}

    def test_tiers(self):
        assert set(ApprovalTier) == {"normal", "dangerous", "sensitive", "hardline"}

    def test_statuses(self):
        assert set(ApprovalStatus) == {"pending", "approved", "denied", "partial"}
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/agents/react/test_constants.py tests/unit/approval/test_state.py -v
```

- [ ] **Step 5: Commit**

```bash
git add framework/agents/react/constants.py framework/approval/constants.py framework/approval/__init__.py tests/unit/agents/react/test_constants.py tests/unit/approval/test_state.py
git commit -m "feat: add ReAct and Approval enum constants"
```

---

### Task 3: Redesign AgentContext

**Files:**
- Modify: `framework/core/agent.py`

**Changes:**
1. Remove ReAct-specific fields from `AgentContext`: `hooks`, `hook_runner`, `interceptor_chain`, `checkpoint_store`, `runtime_context_manager`, `runtime_context`, `governance`, `safety`, `injection_queue`, `max_tools_per_turn`, `on_checkpoint`
2. Add `extensions: dict[str, Any]` and `emitter: ContentEmitter | None`
3. `to_messages()` no longer prepends system prompt — it filters system messages from history and returns only role-messages
4. Add `ctx_ext()` module-level helper

- [ ] **Step 1: Write upgrade test**

Write (append to existing `tests/unit/core/test_agent_context.py`):

```python
from framework.core.agent import ctx_ext
from framework.core.context_extensions import ExtensionKey

class TestAgentContextExtensions:
    def test_extensions_defaults_to_empty_dict(self):
        from framework.core.agent import AgentContext
        from framework.core.tool_manager import InMemoryToolManager
        from framework.memory.history import ListMessageHistory

        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
        )
        assert ctx.extensions == {}

    def test_extensions_accessible_via_ctx_ext(self):
        from framework.core.agent import AgentContext
        from framework.core.tool_manager import InMemoryToolManager
        from framework.memory.history import ListMessageHistory

        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            extensions={ExtensionKey.MAX_TOOLS_PER_TURN: 5},
        )
        assert ctx_ext(ctx, ExtensionKey.MAX_TOOLS_PER_TURN) == 5
        assert ctx_ext(ctx, "nonexistent", default=99) == 99

class TestAgentContextToMessagesNoSystemPrompt:
    """to_messages() no longer includes system_prompt in returned messages."""

    @pytest.mark.asyncio
    async def test_system_prompt_not_in_messages(self):
        from framework.core.agent import AgentContext
        from framework.core.tool_manager import InMemoryToolManager
        from framework.memory.history import ListMessageHistory

        ctx = AgentContext(
            system_prompt="You are a bot",
            history=ListMessageHistory([
                {"role": "user", "content": "hello"},
            ]),
            tool_manager=InMemoryToolManager(),
        )
        msgs = await ctx.to_messages()
        roles = [m["role"] for m in msgs]
        assert "system" not in roles
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "hello"
```

- [ ] **Step 2: Run upgrade test — verify it fails on old behavior**

```bash
pytest tests/unit/core/test_agent_context.py::TestAgentContextToMessagesNoSystemPrompt -v
```
Expected: FAIL — the old `to_messages()` returns system prompt in messages[0].

- [ ] **Step 3: Implement new AgentContext**

Replace `framework/core/agent.py` `AgentContext` dataclass:

```python
@dataclass
class AgentContext:
    """Agent execution context — core fields only. Extensions for agent-type-specific services."""

    # ── Core (required) ──
    system_prompt: str
    history: MessageHistory
    tool_manager: ToolManager

    # ── Common config ──
    session_id: str = ""
    max_iterations: int = 10
    temperature: float | None = None
    max_tokens: int | None = None
    attachments: list[str] = field(default_factory=list)

    # ── Extensible services (injected by upper layers) ──
    extensions: dict[str, Any] = field(default_factory=dict)

    # ── Inter-node communication (read/written during graph execution) ──
    metadata: dict[str, Any] = field(default_factory=dict)

    # ── Set by Agent.run() during graph execution ──
    emitter: ContentEmitter | None = None

    def add_attachment(self, path: str) -> None:
        self.attachments.append(path)

    async def to_messages(self) -> list[dict[str, Any]]:
        """Return history messages WITHOUT system prompt.
        LLMNode explicitly prepends system prompt when assembling context.
        """
        history_list = await self.history.to_list()
        history_list, _has_agent_msgs = normalize_agent_messages_for_llm(history_list)
        non_system = [msg for msg in history_list if msg.get("role") != "system"]

        def _strip_none(d: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in d.items() if v is not None}

        return [_strip_none(msg) for msg in non_system]

    def get_tool_descriptions(self) -> list[dict[str, Any]]:
        return self.tool_manager.get_tool_descriptions()
```

Add `ctx_ext()` helper at module level:

```python
def ctx_ext(ctx: AgentContext, key: str, default: Any = None) -> Any:
    """Safe accessor for AgentContext.extensions."""
    return ctx.extensions.get(key, default)
```

Update imports: add `from .context_extensions import ExtensionKey` is NOT needed in agent.py (that would create a circular concern). The `ctx_ext` helper takes `str` keys, so no import needed.

- [ ] **Step 4: Update ReActAgent references to moved fields**

In `framework/agents/react/agent.py`, all current references like `context.hook_runner` / `context.interceptor_chain` / etc. need to become `ctx_ext(context, ExtensionKey.HOOK_RUNNER)`. Do this now to keep the file consistent:

Search for and replace in `framework/agents/react/agent.py`:

```python
# Old → New
context.hook_runner → ctx_ext(context, ExtensionKey.HOOK_RUNNER)
context.interceptor_chain → ctx_ext(context, ExtensionKey.INTERCEPTOR_CHAIN)
context.checkpoint_store → ctx_ext(context, ExtensionKey.CHECKPOINT_STORE)
context.safety → ctx_ext(context, ExtensionKey.SAFETY)
context.hooks → ctx_ext(context, ExtensionKey.HOOKS, [])
context.injection_queue → ctx_ext(context, ExtensionKey.INJECTION_QUEUE)
context.governance → ctx_ext(context, ExtensionKey.GOVERNANCE)
context.runtime_context_manager → ctx_ext(context, ExtensionKey.RUNTIME_CTX_MGR)
context.runtime_context → ctx_ext(context, ExtensionKey.RUNTIME_CTX)
context.on_checkpoint → ctx_ext(context, ExtensionKey.ON_CHECKPOINT)
```

Add these imports at top of `framework/agents/react/agent.py`:
```python
from framework.core.agent import ctx_ext
from framework.core.context_extensions import ExtensionKey
```

- [ ] **Step 5: Update Pipeline AgentContext construction**

In `framework/pipeline/pipeline.py` `_process_message_locked()`, update the `AgentContext(...)` constructor call. All fields that moved to extensions now go through `extensions={}`:

```python
agent_context = AgentContext(
    system_prompt=context_state.system_prompt,
    history=context_state.history,
    tool_manager=self.tool_manager,
    session_id=session_id,
    max_iterations=self.max_iterations,
    extensions={
        ExtensionKey.HOOKS: self.hooks,
        ExtensionKey.HOOK_RUNNER: self.hook_runner,
        ExtensionKey.INTERCEPTOR_CHAIN: self.interceptor_chain,
        ExtensionKey.CHECKPOINT_STORE: self.checkpoint_store,
        ExtensionKey.RUNTIME_CTX_MGR: self.runtime_context_manager,
        ExtensionKey.GOVERNANCE: self.governance,
        ExtensionKey.SAFETY: self.safety,
        ExtensionKey.INJECTION_QUEUE: injection_queue,
        ExtensionKey.ON_CHECKPOINT: on_checkpoint,
    },
)
```

Add import at top of `framework/pipeline/pipeline.py`:
```python
from framework.core.context_extensions import ExtensionKey
```

- [ ] **Step 6: Run existing tests**

```bash
pytest tests/unit/core/test_agent_context.py -v
pytest tests/unit/ -q --tb=short --ignore=tests/unit/plugins 2>&1 | tail -5
```

Fix any failures caused by field renames before proceeding.

- [ ] **Step 7: Commit**

```bash
git add framework/core/agent.py framework/agents/react/agent.py framework/pipeline/pipeline.py tests/unit/core/test_agent_context.py
git commit -m "refactor: thin AgentContext with extensions dict, remove system prompt from to_messages()"
```

---

### Task 4: Core graph — Node & NodeTransition

**Files:**
- Create: `framework/core/graph/node.py`
- Create: `tests/unit/core/graph/__init__.py` (empty)
- Create: `tests/unit/core/graph/test_node.py`

- [ ] **Step 1: Write failing test**

Write `tests/unit/core/graph/test_node.py`:

```python
"""Tests for Node ABC and NodeTransition."""
import pytest
from framework.core.graph.node import Node, NodeTransition


class _TestNode(Node):
    def __init__(self, name: str, next_target: str = "__end__", next_reason: str = "done"):
        super().__init__(name)
        self._next = NodeTransition(next_target, next_reason)

    async def execute(self, ctx):
        return self._next


class TestNodeTransition:
    def test_create_transition(self):
        t = NodeTransition("target", "my_reason")
        assert t.target == "target"
        assert t.reason == "my_reason"

    def test_frozen(self):
        t = NodeTransition("a", "b")
        with pytest.raises(Exception):
            t.target = "c"


class TestNode:
    @pytest.mark.asyncio
    async def test_execute_returns_transition(self):
        node = _TestNode("test_node", "next_node", "some_reason")
        t = await node.execute({})
        assert t.target == "next_node"
        assert t.reason == "some_reason"

    def test_name_attribute(self):
        node = _TestNode("my_node")
        assert node.name == "my_node"
```

- [ ] **Step 2: Run test — verify failure**

```bash
pytest tests/unit/core/graph/test_node.py -v
```
Expected: FAIL — module not found.

- [ ] **Step 3: Implement Node and NodeTransition**

Write `framework/core/graph/node.py`:

```python
"""Node ABC and NodeTransition."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.core.agent import AgentContext


@dataclass(frozen=True)
class NodeTransition:
    """A node's routing instruction: which node to go to next, and why."""
    target: str
    reason: str


class Node(ABC):
    """Abstract graph node. Executes logic and returns a routing transition."""

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    async def execute(self, ctx: AgentContext) -> NodeTransition:
        """Execute node logic and return the next node transition."""
        ...
```

- [ ] **Step 4: Run test — verify pass**

```bash
pytest tests/unit/core/graph/test_node.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add framework/core/graph/node.py tests/unit/core/graph/
git commit -m "feat: add Node ABC and NodeTransition"
```

---

### Task 5: Core graph — Edge & Graph

**Files:**
- Create: `framework/core/graph/graph.py`
- Create: `tests/unit/core/graph/test_graph.py`

- [ ] **Step 1: Write failing test**

Write `tests/unit/core/graph/test_graph.py`:

```python
"""Tests for Edge and Graph."""
import pytest
from framework.core.graph.graph import Edge, Graph
from framework.core.graph.node import Node, NodeTransition
from framework.core.graph.constants import GraphNode


class _NoOpNode(Node):
    def __init__(self, name: str):
        super().__init__(name)

    async def execute(self, ctx):
        return NodeTransition(GraphNode.END, "done")


class TestEdge:
    def test_unconditional_edge(self):
        e = Edge("a", "b")
        assert e.source == "a"
        assert e.target == "b"
        assert e.reason is None

    def test_conditional_edge(self):
        e = Edge("a", "b", reason="has_tools")
        assert e.reason == "has_tools"

    def test_frozen(self):
        e = Edge("a", "b")
        with pytest.raises(Exception):
            e.target = "c"


class TestGraph:
    def test_add_node(self):
        g = Graph()
        g.add_node(_NoOpNode("n1"))
        assert "n1" in g._nodes

    def test_add_edge(self):
        g = Graph()
        g.add_edge("a", "b", reason="r1")
        assert len(g._edges["a"]) == 1
        assert g._edges["a"][0].target == "b"

    def test_next_node_exact_match(self):
        g = Graph()
        g.add_edge("a", "b", reason="r1")
        g.add_edge("a", "c", reason="r2")
        assert g.next_node("a", "r1") == "b"
        assert g.next_node("a", "r2") == "c"

    def test_next_node_fallback_to_unconditional(self):
        g = Graph()
        g.add_edge("a", "b", reason="r1")
        g.add_edge("a", "c", reason=None)
        assert g.next_node("a", "unknown_reason") == "c"

    def test_next_node_no_match_raises(self):
        g = Graph()
        g.add_edge("a", "b", reason="r1")
        with pytest.raises(KeyError):
            g.next_node("a", "no_such_reason")

    def test_entry_node_default(self):
        g = Graph()
        assert g.entry_node == "start"
```

- [ ] **Step 2: Run test — verify failure**

```bash
pytest tests/unit/core/graph/test_graph.py -v
```
Expected: FAIL.

- [ ] **Step 3: Implement Edge and Graph**

Write `framework/core/graph/graph.py`:

```python
"""Edge and Graph."""
from __future__ import annotations

from dataclasses import dataclass

from .node import Node


@dataclass(frozen=True)
class Edge:
    """Directed edge between nodes. reason=None means unconditional fallback."""
    source: str
    target: str
    reason: str | None = None


class Graph:
    """A directed graph of named nodes and edges."""

    def __init__(self, name: str = "graph") -> None:
        self.name = name
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, list[Edge]] = {}
        self.entry_node: str = "start"

    def add_node(self, node: Node) -> None:
        self._nodes[node.name] = node

    def add_edge(self, source: str, target: str, reason: str | None = None) -> None:
        self._edges.setdefault(source, []).append(Edge(source, target, reason))

    def next_node(self, source: str, reason: str) -> str:
        """Match edge: exact reason first, then unconditional fallback."""
        candidates = self._edges.get(source, [])
        for edge in candidates:
            if edge.reason == reason:
                return edge.target
        for edge in candidates:
            if edge.reason is None:
                return edge.target
        raise KeyError(f"No edge from {source!r} for reason {reason!r}")
```

- [ ] **Step 4: Run test — verify pass**

```bash
pytest tests/unit/core/graph/test_graph.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add framework/core/graph/graph.py tests/unit/core/graph/test_graph.py
git commit -m "feat: add Edge and Graph"
```

---

### Task 6: Core graph — GraphEngine

**Files:**
- Create: `framework/core/graph/engine.py`
- Create: `tests/unit/core/graph/test_engine.py`

- [ ] **Step 1: Write failing test**

Write `tests/unit/core/graph/test_engine.py`:

```python
"""Tests for GraphEngine."""
import pytest
from framework.core.graph.engine import GraphEngine
from framework.core.graph.graph import Graph
from framework.core.graph.node import Node, NodeTransition
from framework.core.graph.constants import GraphNode, GraphMetaKey


class _TrackedNode(Node):
    """A test node that records its execution."""
    def __init__(self, name: str, next_target: str, next_reason: str,
                 side_effect: callable = None):
        super().__init__(name)
        self.next_transition = NodeTransition(next_target, next_reason)
        self.side_effect = side_effect
        self.call_count = 0

    async def execute(self, ctx):
        self.call_count += 1
        if self.side_effect:
            self.side_effect(ctx)
        return self.next_transition


class TestGraphEngine:
    @pytest.mark.asyncio
    async def test_single_node_to_end(self):
        g = Graph()
        node = _TrackedNode("start", GraphNode.END, "done")
        g.add_node(node)

        ctx = type("Ctx", (), {"metadata": {}})()
        result = await GraphEngine(g).run(ctx)
        assert node.call_count == 1

    @pytest.mark.asyncio
    async def test_two_node_chain(self):
        g = Graph()
        n1 = _TrackedNode("start", "n2", "go")
        n2 = _TrackedNode("n2", GraphNode.END, "done")
        g.add_node(n1)
        g.add_node(n2)
        g.add_edge("start", "n2", reason="go")

        ctx = type("Ctx", (), {"metadata": {}})()
        result = await GraphEngine(g).run(ctx)
        assert n1.call_count == 1
        assert n2.call_count == 1

    @pytest.mark.asyncio
    async def test_build_result_reads_metadata(self):
        g = Graph()
        node = _TrackedNode("start", GraphNode.END, "done",
                            side_effect=lambda ctx: ctx.metadata.update(
                                {GraphMetaKey.GRAPH_RESULT: 42}))
        g.add_node(node)

        ctx = type("Ctx", (), {"metadata": {}})()
        result = await GraphEngine(g).run(ctx)
        assert result == 42

    @pytest.mark.asyncio
    async def test_build_result_overridable(self):
        class MyEngine(GraphEngine):
            def build_result(self, ctx):
                return "custom"

        g = Graph()
        g.add_node(_TrackedNode("start", GraphNode.END, "done"))
        ctx = type("Ctx", (), {"metadata": {}})()
        result = await MyEngine(g).run(ctx)
        assert result == "custom"

    @pytest.mark.asyncio
    async def test_graph_interrupt_propagates(self):
        from framework.core.graph.interrupt import GraphInterrupt

        class _RaiseNode(Node):
            def __init__(self):
                super().__init__("start")
            async def execute(self, ctx):
                raise GraphInterrupt(value="paused", node_name="start", iteration=1)

        g = Graph()
        g.add_node(_RaiseNode())
        ctx = type("Ctx", (), {"metadata": {}})()

        with pytest.raises(GraphInterrupt) as exc_info:
            await GraphEngine(g).run(ctx)
        assert exc_info.value.value == "paused"
        assert exc_info.value.node_name == "start"
```

- [ ] **Step 2: Run test — verify failure**

```bash
pytest tests/unit/core/graph/test_engine.py -v
```
Expected: FAIL.

- [ ] **Step 3: Implement GraphEngine**

Write `framework/core/graph/engine.py`:

```python
"""GraphEngine — drives node execution and edge routing."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .constants import GraphNode, GraphMetaKey
from .graph import Graph
from .interrupt import GraphInterrupt

if TYPE_CHECKING:
    from framework.core.agent import AgentContext


class GraphEngine:
    """Executes a Graph by iterating nodes and following edges.
    Agnostic to ReAct / Hook / Interceptor / Approval.
    """

    def __init__(self, graph: Graph) -> None:
        self.graph = graph

    async def run(self, ctx: AgentContext) -> Any:
        """Single entry point. Runs from entry_node until GraphNode.END."""
        current: str = self.graph.entry_node
        while current != GraphNode.END:
            node = self.graph._nodes[current]
            try:
                transition = await node.execute(ctx)
            except GraphInterrupt:
                raise
            current = self.graph.next_node(current, transition.reason)
        return self.build_result(ctx)

    def build_result(self, ctx: AgentContext) -> Any:
        """Extract final result from ctx. Override for typed returns."""
        return ctx.metadata.get(GraphMetaKey.GRAPH_RESULT)
```

- [ ] **Step 4: Run test — verify pass**

```bash
pytest tests/unit/core/graph/test_engine.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add framework/core/graph/engine.py tests/unit/core/graph/test_engine.py
git commit -m "feat: add GraphEngine"
```

---

### Task 7: Core graph — interrupt() & GraphInterrupt

**Files:**
- Create: `framework/core/graph/interrupt.py`
- Create: `tests/unit/core/graph/test_interrupt.py`
- Modify: `framework/core/graph/__init__.py` (add re-exports)

- [ ] **Step 1: Write failing test**

Write `tests/unit/core/graph/test_interrupt.py`:

```python
"""Tests for interrupt(), GraphInterrupt, Command, _current_resume."""
import pytest
from framework.core.graph.interrupt import (
    GraphInterrupt, interrupt, Command, _current_resume,
)


class TestGraphInterrupt:
    def test_create(self):
        exc = GraphInterrupt(value=["req1"], node_name="tool", iteration=3)
        assert exc.value == ["req1"]
        assert exc.node_name == "tool"
        assert exc.iteration == 3

    def test_is_exception(self):
        exc = GraphInterrupt(value=None, node_name="x", iteration=0)
        assert isinstance(exc, Exception)


class TestInterruptFunction:
    def test_raises_when_no_resume(self):
        token = _current_resume.set(None)
        try:
            with pytest.raises(GraphInterrupt) as exc_info:
                interrupt(["req_a", "req_b"])
            assert exc_info.value.value == ["req_a", "req_b"]
        finally:
            _current_resume.reset(token)

    def test_returns_resume_value_when_set(self):
        token = _current_resume.set(["ALLOWED", "DENIED"])
        try:
            result = interrupt(["req_a", "req_b"])
            assert result == ["ALLOWED", "DENIED"]
        finally:
            _current_resume.reset(token)


class TestCommand:
    def test_create_with_resume(self):
        cmd = Command(resume=["ALLOWED", "ALLOWED"])
        assert cmd.resume == ["ALLOWED", "ALLOWED"]
        assert cmd.update is None

    def test_create_with_update(self):
        cmd = Command(update={"key": "value"})
        assert cmd.update == {"key": "value"}

    def test_frozen(self):
        cmd = Command(resume=["x"])
        with pytest.raises(Exception):
            cmd.resume = ["y"]
```

- [ ] **Step 2: Run test — verify failure**

```bash
pytest tests/unit/core/graph/test_interrupt.py -v
```
Expected: FAIL.

- [ ] **Step 3: Implement interrupt module**

Write `framework/core/graph/interrupt.py`:

```python
"""Graph interrupt/resume primitives (LangGraph-inspired)."""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any


_current_resume: contextvars.ContextVar[Any] = contextvars.ContextVar("_gr_resume")


class GraphInterrupt(Exception):
    """Raised by interrupt() on first call. Caught by GraphEngine, surfaced to Pipeline."""

    def __init__(self, *, value: Any, node_name: str, iteration: int) -> None:
        self.value = value
        self.node_name = node_name
        self.iteration = iteration
        super().__init__(value)


def interrupt(value: Any) -> Any:
    """Called inside a node. First call raises GraphInterrupt; on resume returns injected value."""
    resume = _current_resume.get(None)
    if resume is not None:
        return resume
    raise GraphInterrupt(value=value, node_name="", iteration=0)


@dataclass(frozen=True)
class Command:
    """Resume instruction constructed by Pipeline after approval completes."""
    resume: Any = None
    update: dict | None = None
```

- [ ] **Step 4: Run test — verify pass**

```bash
pytest tests/unit/core/graph/test_interrupt.py -v
```
Expected: PASS.

- [ ] **Step 5: Update core/graph/__init__.py**

Write `framework/core/graph/__init__.py`:

```python
"""Core graph abstractions."""
from .constants import GraphNode, GraphMetaKey
from .engine import GraphEngine
from .graph import Edge, Graph
from .interrupt import Command, GraphInterrupt, interrupt, _current_resume
from .node import Node, NodeTransition

__all__ = [
    "Command",
    "Edge",
    "Graph",
    "GraphEngine",
    "GraphInterrupt",
    "GraphMetaKey",
    "GraphNode",
    "Node",
    "NodeTransition",
    "_current_resume",
    "interrupt",
]
```

- [ ] **Step 6: Commit**

```bash
git add framework/core/graph/interrupt.py framework/core/graph/__init__.py tests/unit/core/graph/test_interrupt.py
git commit -m "feat: add interrupt(), GraphInterrupt, and Command"
```

---

### Task 8: Approval state data model

**Files:**
- Create: `framework/approval/state.py`
- Extend: `tests/unit/approval/test_state.py`

- [ ] **Step 1: Extend test**

Append to `tests/unit/approval/test_state.py`:

```python
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.approval.constants import ApprovalDecision, ApprovalStatus


class TestApprovalRequest:
    def test_create(self):
        req = ApprovalRequest(
            tool_name="delete_file",
            tool_call_id="call_001",
            arguments={"path": "/tmp/x"},
            tier="dangerous",
            iteration=2,
        )
        assert req.tool_name == "delete_file"
        assert req.tool_call_id == "call_001"
        assert req.tier == "dangerous"
        assert req.iteration == 2


class TestApprovalState:
    def test_new_state_all_pending(self):
        reqs = [
            ApprovalRequest("t1", "c1", {}, "dangerous", 1),
            ApprovalRequest("t2", "c2", {}, "sensitive", 1),
        ]
        state = ApprovalState(session_id="s1", requests=reqs)
        assert state.status == ApprovalStatus.PENDING
        assert state.every_tool_decided is False
        assert state.unresolved_count == 2

    def test_apply_allowed(self):
        reqs = [ApprovalRequest("t1", "c1", {}, "dangerous", 1)]
        state = ApprovalState(session_id="s1", requests=reqs)
        state.apply("c1", ApprovalDecision.ALLOWED)
        assert state.every_tool_decided is True
        assert state.final_decisions() == [ApprovalDecision.ALLOWED]

    def test_apply_denied_cascades(self):
        reqs = [
            ApprovalRequest("t1", "c1", {}, "dangerous", 1),
            ApprovalRequest("t2", "c2", {}, "sensitive", 1),
        ]
        state = ApprovalState(session_id="s1", requests=reqs)
        state.apply("c1", ApprovalDecision.DENIED)
        # c2 should be PREEMPTED
        assert state.final_decisions() == [ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED]
        assert state.status == ApprovalStatus.DENIED

    def test_partial_approval(self):
        reqs = [
            ApprovalRequest("t1", "c1", {}, "dangerous", 1),
            ApprovalRequest("t2", "c2", {}, "dangerous", 1),
        ]
        state = ApprovalState(session_id="s1", requests=reqs)
        state.apply("c1", ApprovalDecision.ALLOWED)
        assert state.every_tool_decided is False
        assert state.unresolved_count == 1
        assert state.status == ApprovalStatus.PARTIAL
```

- [ ] **Step 2: Run test — verify failure**

```bash
pytest tests/unit/approval/test_state.py::TestApprovalState -v
```
Expected: FAIL — module not found.

- [ ] **Step 3: Implement ApprovalState**

Write `framework/approval/state.py`:

```python
"""Approval state data models."""
from dataclasses import dataclass, field

from .constants import ApprovalDecision, ApprovalStatus


@dataclass
class ApprovalRequest:
    """A single tool's approval request."""
    tool_name: str
    tool_call_id: str
    arguments: dict
    tier: str
    iteration: int


@dataclass
class ApprovalState:
    """Approval state for one ReAct turn. May contain multiple tools to approve."""

    session_id: str
    requests: list[ApprovalRequest]
    decisions: dict[str, str] = field(default_factory=dict)
    status: str = ApprovalStatus.PENDING

    @property
    def every_tool_decided(self) -> bool:
        return all(
            tc_id in self.decisions
            and self.decisions[tc_id] != ApprovalDecision.PENDING
            for tc_id in (r.tool_call_id for r in self.requests)
        )

    @property
    def unresolved_count(self) -> int:
        return sum(
            1 for r in self.requests
            if r.tool_call_id not in self.decisions
            or self.decisions[r.tool_call_id] == ApprovalDecision.PENDING
        )

    def apply(self, tool_call_id: str, decision: str) -> None:
        """Apply a decision. DENIED cascades to mark remaining PENDING as PREEMPTED."""
        self.decisions[tool_call_id] = decision
        if decision == ApprovalDecision.DENIED:
            for r in self.requests:
                if r.tool_call_id not in self.decisions:
                    self.decisions[r.tool_call_id] = ApprovalDecision.PREEMPTED
                elif self.decisions[r.tool_call_id] == ApprovalDecision.PENDING:
                    self.decisions[r.tool_call_id] = ApprovalDecision.PREEMPTED
            self.status = ApprovalStatus.DENIED
        elif self.every_tool_decided:
            self.status = ApprovalStatus.APPROVED
        else:
            self.status = ApprovalStatus.PARTIAL

    def final_decisions(self) -> list[str]:
        """Return decisions in request order. Unresolved → PREEMPTED."""
        return [
            self.decisions.get(r.tool_call_id, ApprovalDecision.PREEMPTED)
            for r in self.requests
        ]
```

- [ ] **Step 4: Run test — verify pass**

```bash
pytest tests/unit/approval/test_state.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add framework/approval/state.py tests/unit/approval/test_state.py
git commit -m "feat: add ApprovalRequest and ApprovalState data models"
```

---

### Task 9: ApprovalStateStore ABC + implementations

**Files:**
- Create: `framework/approval/store.py`
- Create: `tests/unit/approval/test_store.py`

- [ ] **Step 1: Write failing test**

Write `tests/unit/approval/test_store.py`:

```python
"""Tests for ApprovalStateStore implementations."""
import tempfile
from pathlib import Path
import pytest
from framework.approval.store import (
    ApprovalStateStore, LocalFileApprovalStateStore, InMemoryApprovalStateStore,
)
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.approval.constants import ApprovalDecision


class TestInMemoryApprovalStateStore:
    @pytest.mark.asyncio
    async def test_save_and_load(self):
        store = InMemoryApprovalStateStore()
        reqs = [ApprovalRequest("t1", "c1", {}, "dangerous", 1)]
        state = ApprovalState(session_id="s1", requests=reqs)
        await store.save(state)

        loaded = await store.load("s1")
        assert loaded is not None
        assert loaded.session_id == "s1"
        assert len(loaded.requests) == 1

    @pytest.mark.asyncio
    async def test_load_nonexistent(self):
        store = InMemoryApprovalStateStore()
        loaded = await store.load("no_such_session")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_delete(self):
        store = InMemoryApprovalStateStore()
        reqs = [ApprovalRequest("t1", "c1", {}, "dangerous", 1)]
        state = ApprovalState(session_id="s1", requests=reqs)
        await store.save(state)
        await store.delete("s1")
        assert await store.load("s1") is None


class TestLocalFileApprovalStateStore:
    @pytest.mark.asyncio
    async def test_save_load_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileApprovalStateStore(Path(tmp))
            reqs = [ApprovalRequest("t1", "c1", {"p": "/x"}, "dangerous", 1)]
            state = ApprovalState(session_id="s2", requests=reqs)
            state.apply("c1", ApprovalDecision.ALLOWED)

            await store.save(state)
            loaded = await store.load("s2")
            assert loaded is not None
            assert loaded.session_id == "s2"
            assert loaded.every_tool_decided is True

            await store.delete("s2")
            assert await store.load("s2") is None
```

- [ ] **Step 2: Run test — verify failure**

```bash
pytest tests/unit/approval/test_store.py -v
```
Expected: FAIL.

- [ ] **Step 3: Implement stores**

Write `framework/approval/store.py`:

```python
"""ApprovalStateStore ABC and default implementations."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

from .state import ApprovalRequest, ApprovalState


class ApprovalStateStore(ABC):
    """Approval state persistence abstraction. Swap for Redis, DB, etc."""

    @abstractmethod
    async def save(self, state: ApprovalState) -> None: ...

    @abstractmethod
    async def load(self, session_id: str) -> ApprovalState | None: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...


class InMemoryApprovalStateStore(ApprovalStateStore):
    """In-memory store for testing and InlineWait strategy."""

    def __init__(self) -> None:
        self._store: dict[str, ApprovalState] = {}

    async def save(self, state: ApprovalState) -> None:
        self._store[state.session_id] = state

    async def load(self, session_id: str) -> ApprovalState | None:
        return self._store.get(session_id)

    async def delete(self, session_id: str) -> None:
        self._store.pop(session_id, None)


class LocalFileApprovalStateStore(ApprovalStateStore):
    """Default: JSON file persistence. key: {workspace}/{session_id}_approval.json"""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
        return self._workspace / f"{safe}_approval.json"

    async def save(self, state: ApprovalState) -> None:
        data = {
            "session_id": state.session_id,
            "requests": [
                {"tool_name": r.tool_name, "tool_call_id": r.tool_call_id,
                 "arguments": r.arguments, "tier": r.tier, "iteration": r.iteration}
                for r in state.requests
            ],
            "decisions": state.decisions,
            "status": state.status,
        }
        self._path(state.session_id).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    async def load(self, session_id: str) -> ApprovalState | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        reqs = [ApprovalRequest(**r) for r in data["requests"]]
        state = ApprovalState(
            session_id=data["session_id"], requests=reqs,
            decisions=data.get("decisions", {}), status=data.get("status", "pending"),
        )
        return state

    async def delete(self, session_id: str) -> None:
        self._path(session_id).unlink(missing_ok=True)
```

- [ ] **Step 4: Run test — verify pass**

```bash
pytest tests/unit/approval/test_store.py -v
```
Expected: PASS.

- [ ] **Step 5: Update approval __init__.py**

Write `framework/approval/__init__.py`:

```python
"""Approval system."""
from .state import ApprovalRequest, ApprovalState
from .store import (
    ApprovalStateStore,
    InMemoryApprovalStateStore,
    LocalFileApprovalStateStore,
)
```

- [ ] **Step 6: Commit**

```bash
git add framework/approval/store.py framework/approval/__init__.py tests/unit/approval/test_store.py
git commit -m "feat: add ApprovalStateStore ABC, LocalFile and InMemory implementations"
```

---

### Task 10: TurnResumeState + Store

**Files:**
- Create: `framework/agents/react/state.py`
- Create: `tests/unit/agents/react/test_state.py`

- [ ] **Step 1: Write failing test**

Write `tests/unit/agents/react/test_state.py`:

```python
"""Tests for TurnResumeState and TurnResumeStateStore."""
import pytest
from framework.agents.react.state import (
    TurnResumeState, TurnResumeStateStore,
    InMemoryTurnResumeStateStore,
)


class TestTurnResumeState:
    def test_create(self):
        trs = TurnResumeState(
            iteration=3,
            tool_calls=[{"id": "c1", "type": "function", "function": {}}],
            tool_decisions=["pending", "pending"],
            all_new_messages=[{"role": "assistant", "content": "Let me check..."}],
        )
        assert trs.iteration == 3
        assert len(trs.tool_calls) == 1
        assert trs.tool_decisions == ["pending", "pending"]


class TestInMemoryTurnResumeStateStore:
    @pytest.mark.asyncio
    async def test_save_load_delete(self):
        store = InMemoryTurnResumeStateStore()
        trs = TurnResumeState(iteration=2, tool_calls=[], tool_decisions=[],
                              all_new_messages=[])
        await store.save("sid", trs)

        loaded = await store.load("sid")
        assert loaded is not None
        assert loaded.iteration == 2

        await store.delete("sid")
        assert await store.load("sid") is None

    @pytest.mark.asyncio
    async def test_load_missing_returns_none(self):
        store = InMemoryTurnResumeStateStore()
        assert await store.load("no_session") is None
```

- [ ] **Step 2: Run test — verify failure**

```bash
pytest tests/unit/agents/react/test_state.py -v
```
Expected: FAIL.

- [ ] **Step 3: Implement**

Write `framework/agents/react/state.py`:

```python
"""TurnResumeState — execution snapshot for SuspendResume recovery."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.control.checkpoint import CheckpointStore


@dataclass
class TurnResumeState:
    """Snapshot captured when ToolNode suspends for approval. Restored on resume."""
    iteration: int
    tool_calls: list[dict[str, Any]]
    tool_decisions: list[str]
    all_new_messages: list[dict[str, Any]]


class TurnResumeStateStore(ABC):
    """Persistence abstraction for TurnResumeState."""

    @abstractmethod
    async def save(self, session_id: str, state: TurnResumeState) -> None: ...

    @abstractmethod
    async def load(self, session_id: str) -> TurnResumeState | None: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...


class InMemoryTurnResumeStateStore(TurnResumeStateStore):
    """In-memory store for testing and InlineWait strategy."""

    def __init__(self) -> None:
        self._store: dict[str, TurnResumeState] = {}

    async def save(self, session_id: str, state: TurnResumeState) -> None:
        self._store[session_id] = state

    async def load(self, session_id: str) -> TurnResumeState | None:
        return self._store.get(session_id)

    async def delete(self, session_id: str) -> None:
        self._store.pop(session_id, None)


class StateStoreTurnResumeStateStore(TurnResumeStateStore):
    """Wraps existing CheckpointStore for durable persistence. SuspendResume uses this."""

    def __init__(self, checkpoint_store: "CheckpointStore") -> None:
        self._store = checkpoint_store

    def _key(self, session_id: str) -> str:
        return f"{session_id}:turn_resume"

    async def save(self, session_id: str, state: TurnResumeState) -> None:
        data = {
            "iteration": state.iteration,
            "tool_calls": state.tool_calls,
            "tool_decisions": state.tool_decisions,
            "all_new_messages": state.all_new_messages,
        }
        await self._store.save(self._key(session_id), data)

    async def load(self, session_id: str) -> TurnResumeState | None:
        data = await self._store.load(self._key(session_id))
        if data is None:
            return None
        return TurnResumeState(
            iteration=data["iteration"],
            tool_calls=data["tool_calls"],
            tool_decisions=data["tool_decisions"],
            all_new_messages=data["all_new_messages"],
        )

    async def delete(self, session_id: str) -> None:
        await self._store.clear(self._key(session_id))
```

- [ ] **Step 4: Run test — verify pass**

```bash
pytest tests/unit/agents/react/test_state.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add framework/agents/react/state.py tests/unit/agents/react/test_state.py
git commit -m "feat: add TurnResumeState and TurnResumeStateStore"
```

---

### Task 11: SuspendStrategy ABC + implementations

**Files:**
- Create: `framework/agents/react/strategy.py`
- Create: `tests/unit/agents/react/test_strategy.py`

- [ ] **Step 1: Write failing test**

Write `tests/unit/agents/react/test_strategy.py`:

```python
"""Tests for SuspendStrategy implementations."""
import pytest
from framework.agents.react.strategy import (
    SuspendStrategy, InlineWaitStrategy, SuspendResumeStrategy,
)
from framework.approval.state import ApprovalRequest
from framework.approval.constants import ApprovalDecision
from framework.approval.store import InMemoryApprovalStateStore
from framework.agents.react.state import InMemoryTurnResumeStateStore
from framework.core.graph.interrupt import GraphInterrupt, _current_resume
from framework.core.context_extensions import ExtensionKey
from framework.agents.react.constants import ReActMetaKey


class _MockChannel:
    """Mock approval channel for InlineWait testing."""
    def __init__(self, responses: list[str]):
        self._responses = responses
        self._idx = 0

    async def wait_for_decision(self, tool_call_id: str) -> str:
        resp = self._responses[self._idx]
        self._idx += 1
        return resp


class TestInlineWaitStrategy:
    @pytest.mark.asyncio
    async def test_all_allowed(self):
        strategy = InlineWaitStrategy(_MockChannel(["allowed", "allowed"]))
        reqs = [
            ApprovalRequest("t1", "c1", {}, "dangerous", 1),
            ApprovalRequest("t2", "c2", {}, "dangerous", 1),
        ]
        ctx = type("Ctx", (), {"session_id": "s1", "emitter": type("E", (), {
            "emit": lambda *a, **kw: None
        })()})()

        decisions = await strategy.solicit_approval(reqs, ctx)
        assert decisions == [ApprovalDecision.ALLOWED, ApprovalDecision.ALLOWED]

    @pytest.mark.asyncio
    async def test_denied_cascades(self):
        strategy = InlineWaitStrategy(_MockChannel(["denied"]))
        reqs = [
            ApprovalRequest("t1", "c1", {}, "dangerous", 1),
            ApprovalRequest("t2", "c2", {}, "dangerous", 1),
        ]
        ctx = type("Ctx", (), {"session_id": "s1", "emitter": type("E", (), {
            "emit": lambda *a, **kw: None
        })()})()

        decisions = await strategy.solicit_approval(reqs, ctx)
        assert decisions == [ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED]


class TestSuspendResumeStrategy:
    @pytest.mark.asyncio
    async def test_first_call_raises(self):
        strategy = SuspendResumeStrategy(
            InMemoryApprovalStateStore(), InMemoryTurnResumeStateStore(),
        )
        reqs = [
            ApprovalRequest("t1", "c1", {}, "dangerous", 1),
            ApprovalRequest("t2", "c2", {}, "dangerous", 1),
        ]
        ctx = type("Ctx", (), {
            "session_id": "s1",
            "metadata": {ReActMetaKey.ITERATION: 1, ReActMetaKey.ITERATION_MSGS: []},
        })()

        with pytest.raises(GraphInterrupt) as exc_info:
            await strategy.solicit_approval(reqs, ctx)
        assert exc_info.value.value == reqs

    @pytest.mark.asyncio
    async def test_second_call_returns_resume(self):
        store = InMemoryApprovalStateStore()
        resume_store = InMemoryTurnResumeStateStore()
        strategy = SuspendResumeStrategy(store, resume_store)
        reqs = [ApprovalRequest("t1", "c1", {}, "dangerous", 1)]
        ctx = type("Ctx", (), {
            "session_id": "s1",
            "metadata": {ReActMetaKey.ITERATION: 1, ReActMetaKey.ITERATION_MSGS: []},
        })()

        # First call raises
        with pytest.raises(GraphInterrupt):
            await strategy.solicit_approval(reqs, ctx)

        # Approval state should be persisted
        saved = await store.load("s1")
        assert saved is not None
        assert len(saved.requests) == 1

        # Second call with resume contextvar set
        token = _current_resume.set([ApprovalDecision.ALLOWED])
        try:
            decisions = await strategy.solicit_approval(reqs, ctx)
            assert decisions == [ApprovalDecision.ALLOWED]
        finally:
            _current_resume.reset(token)
```

- [ ] **Step 2: Run test — verify failure**

```bash
pytest tests/unit/agents/react/test_strategy.py -v
```
Expected: FAIL.

- [ ] **Step 3: Implement strategies**

Write `framework/agents/react/strategy.py`:

```python
"""SuspendStrategy ABC — pluggable approval behavior for ToolNode."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from framework.approval.constants import ApprovalDecision
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.core.graph.interrupt import interrupt

from .constants import ReActMetaKey
from .state import TurnResumeState

if TYPE_CHECKING:
    from framework.core.agent import AgentContext


class SuspendStrategy(ABC):
    """Pluggable approval strategy. ToolNode calls solicit_approval() without knowing
    whether it's InlineWait (blocks, no persist) or SuspendResume (raises, persists)."""

    @abstractmethod
    async def solicit_approval(
        self, requests: list[ApprovalRequest], ctx: AgentContext
    ) -> list[str]:
        """Request approval and return final decisions."""
        ...


class InlineWaitStrategy(SuspendStrategy):
    """Blocking approval — polls a channel per-tool, no persistence."""

    def __init__(self, channel: Any) -> None:
        self._channel = channel

    async def solicit_approval(
        self, requests: list[ApprovalRequest], ctx: AgentContext
    ) -> list[str]:
        state = ApprovalState(session_id=ctx.session_id, requests=list(requests))

        for req in requests:
            await ctx.emitter.emit("approval_required", req)
            decision = await self._channel.wait_for_decision(req.tool_call_id)
            state.apply(req.tool_call_id, decision)
            if decision == ApprovalDecision.DENIED:
                break

        return state.final_decisions()


class SuspendResumeStrategy(SuspendStrategy):
    """Interrupt-resume approval — persists state, raises GraphInterrupt, resumes on re-entry."""

    def __init__(self, approval_store: Any, resume_store: Any) -> None:
        self._approval_store = approval_store
        self._resume_store = resume_store

    async def solicit_approval(
        self, requests: list[ApprovalRequest], ctx: AgentContext
    ) -> list[str]:
        # Persist approval state (for Pipeline to consume)
        approval_state = ApprovalState(session_id=ctx.session_id, requests=list(requests))
        await self._approval_store.save(approval_state)

        # Persist execution snapshot (for StartNode to route to ToolNode on resume)
        resume_state = TurnResumeState(
            iteration=ctx.metadata[ReActMetaKey.ITERATION],
            tool_calls=[self._tc_to_dict(r) for r in requests],
            tool_decisions=[ApprovalDecision.PENDING] * len(requests),
            all_new_messages=list(ctx.metadata.get(ReActMetaKey.ITERATION_MSGS, [])),
        )
        await self._resume_store.save(ctx.session_id, resume_state)

        # interrupt() → first call raises, resume returns injected decisions
        return interrupt(requests)

    @staticmethod
    def _tc_to_dict(r: ApprovalRequest) -> dict[str, Any]:
        return {
            "id": r.tool_call_id,
            "type": "function",
            "function": {"name": r.tool_name, "arguments": r.arguments},
        }
```

- [ ] **Step 4: Run test — verify pass**

```bash
pytest tests/unit/agents/react/test_strategy.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add framework/agents/react/strategy.py tests/unit/agents/react/test_strategy.py
git commit -m "feat: add SuspendStrategy ABC, InlineWait and SuspendResume implementations"
```

---

### Task 12: ReAct Nodes — StartNode + EndNode

**Files:**
- Create: `framework/agents/react/nodes/__init__.py` (empty)
- Create: `framework/agents/react/nodes/start.py`
- Create: `framework/agents/react/nodes/end.py`
- Create: `tests/unit/agents/react/test_nodes.py`

- [ ] **Step 1: Write failing test for StartNode and EndNode**

Write `tests/unit/agents/react/test_nodes.py`:

```python
"""Tests for ReAct nodes."""
import pytest
from framework.agents.react.nodes.start import StartNode
from framework.agents.react.nodes.end import EndNode
from framework.agents.react.constants import ReActNode, ReActReason, ReActMetaKey
from framework.agents.react.state import TurnResumeState
from framework.core.graph.constants import GraphNode, GraphMetaKey
from framework.core.graph.node import NodeTransition


class _MockEmitter:
    def __init__(self):
        self.events = []

    async def emit(self, event, data=None):
        self.events.append((event, data))

    async def emit_complete(self, result):
        self.events.append(("complete", result))


class _MockHistory:
    async def to_list(self):
        return []


class TestStartNode:
    @pytest.mark.asyncio
    async def test_normal_start_routes_to_llm(self):
        node = StartNode()
        ctx = type("Ctx", (), {
            "metadata": {},
            "emitter": _MockEmitter(),
        })()

        t = await node.execute(ctx)
        assert t.target == ReActNode.LLM
        assert t.reason == ReActReason.NORMAL_START
        assert ctx.metadata[ReActMetaKey.ITERATION] == 0

    @pytest.mark.asyncio
    async def test_resume_routes_to_tool(self):
        node = StartNode()
        resume = TurnResumeState(
            iteration=3, tool_calls=[],
            tool_decisions=["allowed"], all_new_messages=[],
        )
        ctx = type("Ctx", (), {
            "metadata": {ReActMetaKey.RESUME_STATE: resume},
            "emitter": _MockEmitter(),
        })()

        t = await node.execute(ctx)
        assert t.target == ReActNode.TOOL
        assert t.reason == ReActReason.RESUME_TOOLS
        assert ctx.metadata[ReActMetaKey.ITERATION] == 3
        assert ctx.metadata[ReActMetaKey.TOOL_DECISIONS] == ["allowed"]


class TestEndNode:
    @pytest.mark.asyncio
    async def test_writes_result_to_metadata(self):
        agent = type("A", (), {"_clear_checkpoint": lambda self, ctx: None})()
        node = EndNode(agent)
        llm_response = type("R", (), {"content": "Done!", "reasoning_content": None, "tool_calls": None})()
        ctx = type("Ctx", (), {
            "metadata": {ReActMetaKey.LLM_RESPONSE: llm_response, ReActMetaKey.ITERATION_MSGS: []},
            "emitter": _MockEmitter(),
            "attachments": [],
        })()

        t = await node.execute(ctx)
        assert t.target == GraphNode.END
        result = ctx.metadata[GraphMetaKey.GRAPH_RESULT]
        assert result.content == "Done!"
```

- [ ] **Step 2: Run test — verify failure**

```bash
pytest tests/unit/agents/react/test_nodes.py -v
```
Expected: FAIL.

- [ ] **Step 3: Implement StartNode**

Write `framework/agents/react/nodes/start.py`:

```python
"""StartNode — entry point for ReAct graph."""
from framework.core.graph.node import Node, NodeTransition
from framework.core.agent import AgentContext
from framework.agents.react.constants import ReActNode, ReActReason, ReActMetaKey
from framework.agents.react.agent import ReActEvent


class StartNode(Node):
    """Routes to LLM normally, or to ToolNode when resuming from approval."""

    def __init__(self) -> None:
        super().__init__(ReActNode.START)

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        resume_state = ctx.metadata.get(ReActMetaKey.RESUME_STATE)

        if resume_state is not None:
            ctx.metadata[ReActMetaKey.ITERATION] = resume_state.iteration
            ctx.metadata[ReActMetaKey.TOOL_DECISIONS] = resume_state.tool_decisions
            ctx.metadata[ReActMetaKey.ITERATION_MSGS] = list(resume_state.all_new_messages)
            return NodeTransition(ReActNode.TOOL, ReActReason.RESUME_TOOLS)

        ctx.metadata[ReActMetaKey.ITERATION] = 0
        await ctx.emitter.emit(ReActEvent.START)
        return NodeTransition(ReActNode.LLM, ReActReason.NORMAL_START)
```

- [ ] **Step 4: Implement EndNode**

Write `framework/agents/react/nodes/end.py`:

```python
"""EndNode — builds AgentResult and marks completion."""
from framework.core.graph.node import Node, NodeTransition
from framework.core.graph.constants import GraphNode, GraphMetaKey
from framework.core.agent import AgentContext
from framework.core.emitter import AgentResult
from framework.agents.react.constants import ReActNode, ReActReason, ReActMetaKey
from framework.agents.react.agent import ReActEvent


class EndNode(Node):
    """Constructs final result and emits completion events."""

    def __init__(self, agent: "ReActAgent") -> None:
        super().__init__(ReActNode.END)
        self._agent = agent

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        response = ctx.metadata.pop(ReActMetaKey.LLM_RESPONSE, None)
        messages = ctx.metadata.pop(ReActMetaKey.ITERATION_MSGS, [])

        if response and not getattr(response, "tool_calls", None):
            result = AgentResult(
                content=getattr(response, "content", "") or "",
                reasoning=getattr(response, "reasoning_content", None),
                messages=messages,
                attachments=ctx.attachments,
            )
            await ctx.emitter.emit(ReActEvent.FINAL_OUTPUT, result)
        else:
            result = AgentResult(
                content="达到最大迭代次数",
                stop_reason="max_iterations",
                messages=messages,
                attachments=ctx.attachments,
            )

        await self._agent._clear_checkpoint(ctx)
        await ctx.emitter.emit_complete(result)
        ctx.metadata[GraphMetaKey.GRAPH_RESULT] = result
        return NodeTransition(GraphNode.END, ReActReason.DONE)
```

- [ ] **Step 5: Run test — verify pass**

```bash
pytest tests/unit/agents/react/test_nodes.py -v
```
Expected: PASS.

- [ ] **Step 6: Write nodes/__init__.py**

Write `framework/agents/react/nodes/__init__.py`:

```python
from .start import StartNode
from .llm import LLMNode
from .tool import ToolNode
from .end import EndNode

__all__ = ["StartNode", "LLMNode", "ToolNode", "EndNode"]
```

- [ ] **Step 6: Commit**

```bash
git add framework/agents/react/nodes/ framework/agents/react/nodes/start.py framework/agents/react/nodes/end.py tests/unit/agents/react/test_nodes.py
git commit -m "feat: add StartNode and EndNode"
```

---

### Task 13: ReAct Nodes — LLMNode

**Files:**
- Create: `framework/agents/react/nodes/llm.py`

- [ ] **Step 1: Extend test_nodes.py with LLMNode tests**

Append to `tests/unit/agents/react/test_nodes.py`:

```python
from framework.agents.react.nodes.llm import LLMNode
from framework.core.llm_error import RuntimeSafetyPolicy


class TestLLMNode:
    @pytest.mark.asyncio
    async def test_routes_to_end_on_no_tool_calls(self):
        async def _mock_llm(messages, ctx):
            return type("R", (), {
                "content": "Hello!", "reasoning_content": None,
                "tool_calls": None, "finish_reason": "stop",
            })()

        agent = type("A", (), {
            "_build_assistant_message": lambda self, resp: {"role": "assistant", "content": "Hello!"},
            "_call_hooks": lambda self, *a, **kw: None,
            "_drain_injections": lambda self, ctx: None,
        })()
        node = LLMNode(agent, enable_hooks=False)
        node._call_llm = _mock_llm

        class _History:
            async def append(self, msg): pass
            async def to_list(self): return []

        ctx = type("Ctx", (), {
            "metadata": {ReActMetaKey.ITERATION: 0},
            "emitter": _MockEmitter(),
            "max_iterations": 10,
            "system_prompt": "You are helpful.",
            "history": _History(),
            "extensions": {},
            "to_messages": lambda self: [],
        })()

        t = await node.execute(ctx)
        assert t.target == ReActNode.END
        assert t.reason == ReActReason.NO_TOOLS

    @pytest.mark.asyncio
    async def test_routes_to_tool_on_has_tool_calls(self):
        async def _mock_llm(messages, ctx):
            return type("R", (), {
                "content": None, "reasoning_content": None,
                "tool_calls": [type("TC", (), {"tool_name": "search"})()],
                "finish_reason": "tool_calls",
            })()

        agent = type("A", (), {
            "_build_assistant_message": lambda self, resp: {"role": "assistant", "tool_calls": []},
            "_call_hooks": lambda self, *a, **kw: None,
            "_drain_injections": lambda self, ctx: None,
        })()
        node = LLMNode(agent, enable_hooks=False)
        node._call_llm = _mock_llm

        class _History:
            async def append(self, msg): pass
            async def to_list(self): return []

        ctx = type("Ctx", (), {
            "metadata": {ReActMetaKey.ITERATION: 0},
            "emitter": _MockEmitter(),
            "max_iterations": 10,
            "system_prompt": "You are helpful.",
            "history": _History(),
            "extensions": {},
            "to_messages": lambda self: [],
        })()

        t = await node.execute(ctx)
        assert t.target == ReActNode.TOOL
        assert t.reason == ReActReason.HAS_TOOLS

    @pytest.mark.asyncio
    async def test_routes_to_end_on_max_iterations(self):
        agent = type("A", (), {
            "_call_hooks": lambda self, *a, **kw: None,
        })()
        node = LLMNode(agent, enable_hooks=False)

        ctx = type("Ctx", (), {
            "metadata": {ReActMetaKey.ITERATION: 10},
            "emitter": _MockEmitter(),
            "max_iterations": 10,
            "extensions": {},
        })()

        t = await node.execute(ctx)
        assert t.target == ReActNode.END
        assert t.reason == ReActReason.MAX_ITERATIONS
```

- [ ] **Step 2: Run test — verify failure**

```bash
pytest tests/unit/agents/react/test_nodes.py::TestLLMNode -v
```
Expected: FAIL.

- [ ] **Step 3: Implement LLMNode**

Write `framework/agents/react/nodes/llm.py`:

```python
"""LLMNode — assembles messages, calls LLM, writes assistant message."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.core.agent import AgentContext, ctx_ext
from framework.core.constants import FinishReason
from framework.core.emitter import LLMResponse
from framework.core.context_extensions import ExtensionKey
from framework.core.graph.node import Node, NodeTransition
from framework.core.provider import StreamingLLMProvider
from framework.agents.react.agent import ReActEvent
from framework.agents.react.constants import ReActNode, ReActReason, ReActMetaKey
from framework.hook import HookPoint

if TYPE_CHECKING:
    from framework.agents.react.agent import ReActAgent


class LLMNode(Node):
    """Calls LLM, writes assistant message, routes to ToolNode or EndNode."""

    def __init__(self, agent: ReActAgent, *, enable_hooks: bool = True) -> None:
        super().__init__(ReActNode.LLM)
        self._agent = agent
        self._enable_hooks = enable_hooks

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        iteration = ctx.metadata[ReActMetaKey.ITERATION] + 1
        ctx.metadata[ReActMetaKey.ITERATION] = iteration

        if iteration > ctx.max_iterations:
            await ctx.emitter.emit(ReActEvent.MAX_ITERATIONS)
            return NodeTransition(ReActNode.END, ReActReason.MAX_ITERATIONS)

        await ctx.emitter.emit(ReActEvent.ITERATION_START, {"iteration": iteration})

        if self._enable_hooks:
            await self._agent._call_hooks(HookPoint.BEFORE_ITERATION, ctx)
            await self._agent._drain_injections(ctx)

        messages = await self._build_messages(ctx)
        response = await self._call_llm(messages, ctx)

        if self._enable_hooks:
            await self._agent._call_hooks(HookPoint.AFTER_LLM_RESPONSE, ctx, response)

        if response.finish_reason == FinishReason.ERROR.value:
            return NodeTransition(ReActNode.END, ReActReason.LLM_ERROR)

        assistant_msg = self._agent._build_assistant_message(response)
        await ctx.history.append(assistant_msg)
        ctx.metadata[ReActMetaKey.LLM_RESPONSE] = response
        ctx.metadata[ReActMetaKey.ITERATION_MSGS] = [assistant_msg]

        if response.tool_calls:
            return NodeTransition(ReActNode.TOOL, ReActReason.HAS_TOOLS)
        return NodeTransition(ReActNode.END, ReActReason.NO_TOOLS)

    async def _build_messages(self, ctx: AgentContext) -> list[dict[str, Any]]:
        """Assemble system prompt + history. System prompt is fixed throughout the loop."""
        messages: list[dict[str, Any]] = []
        if ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})
        messages.extend(await ctx.to_messages())

        governance = ctx_ext(ctx, ExtensionKey.GOVERNANCE)
        if governance is not None:
            messages = await governance.apply(messages)
        return messages

    async def _call_llm(self, messages: list[dict[str, Any]], ctx: AgentContext) -> LLMResponse:
        """Dispatch to streaming or non-streaming LLM call."""
        emitter = ctx.emitter
        if emitter and emitter.wants_streaming() and isinstance(self._agent.provider, StreamingLLMProvider):
            interceptor_chain = ctx_ext(ctx, ExtensionKey.INTERCEPTOR_CHAIN)
            if interceptor_chain and interceptor_chain.has_scope("llm_stream"):
                return await self._agent._stream_with_control(messages, ctx)
            return await self._stream_plain(messages, ctx)
        return await self._call_non_streaming(messages, ctx)

    async def _stream_plain(self, messages, ctx) -> LLMResponse:
        async def _on_content(delta: str) -> None:
            if delta:
                await ctx.emitter.emit_delta(delta)
                await ctx.emitter.emit(ReActEvent.MODEL_OUTPUT, delta)

        async def _on_reasoning(delta: str) -> None:
            if delta:
                await ctx.emitter.emit(ReActEvent.MODEL_REASONING, delta)

        response = await self._agent.provider.chat_stream(
            messages=messages,
            tools=ctx.get_tool_descriptions() if ctx.tool_manager else None,
            temperature=ctx.temperature or 0.7,
            max_tokens=ctx.max_tokens,
            on_content_delta=_on_content,
            on_reasoning_delta=_on_reasoning,
        )
        await ctx.emitter.emit_stream_end(resuming=bool(response.tool_calls))
        return response

    async def _call_non_streaming(self, messages, ctx) -> LLMResponse:
        response = await self._agent.provider.chat(
            messages=messages,
            tools=ctx.get_tool_descriptions() if ctx.tool_manager else None,
            temperature=ctx.temperature or 0.7,
            max_tokens=ctx.max_tokens,
        )
        if response.content:
            await ctx.emitter.emit_content(response.content)
            await ctx.emitter.emit(ReActEvent.MODEL_OUTPUT, response.content)
        await ctx.emitter.emit_stream_end(resuming=bool(response.tool_calls))
        return response
```

- [ ] **Step 4: Run test — verify pass**

```bash
pytest tests/unit/agents/react/test_nodes.py::TestLLMNode -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add framework/agents/react/nodes/llm.py tests/unit/agents/react/test_nodes.py
git commit -m "feat: add LLMNode"
```

---

### Task 14: ReAct Nodes — ToolNode

**Files:**
- Create: `framework/agents/react/nodes/tool.py`

- [ ] **Step 1: Extend test_nodes.py with ToolNode tests**

Append to `tests/unit/agents/react/test_nodes.py`:

```python
from framework.agents.react.nodes.tool import ToolNode
from framework.approval.constants import ApprovalDecision
from framework.approval.state import ApprovalRequest


class TestToolNode:
    @pytest.mark.asyncio
    async def test_execute_batch_all_allowed(self):
        results = []

        class _MockAgent:
            async def _execute_tool(self, tc, ctx):
                class R:
                    tool_name = tc.tool_name
                    result = f"result_{tc.tool_name}"
                    error = None
                return R()
            def _build_tool_message(self, result, call_id):
                return {"role": "tool", "content": f"r_{result.tool_name}"}
            async def _drain_injections(self, ctx):
                pass

        agent = _MockAgent()
        node = ToolNode(agent, enable_approval=False, enable_hooks=False)

        class _History:
            def __init__(self):
                self.msgs = []
            async def append(self, msg):
                self.msgs.append(msg)

        history = _History()
        tc1 = type("TC", (), {"tool_name": "search", "call_id": "c1", "arguments": {}})()
        tc2 = type("TC", (), {"tool_name": "read", "call_id": "c2", "arguments": {}})()
        response = type("R", (), {"tool_calls": [tc1, tc2]})()

        ctx = type("Ctx", (), {
            "metadata": {
                ReActMetaKey.LLM_RESPONSE: response,
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
            },
            "emitter": _MockEmitter(),
            "history": history,
            "extensions": {},
        })()

        t = await node.execute(ctx)
        assert t.target == ReActNode.LLM
        assert t.reason == ReActReason.TOOLS_DONE
        assert len(history.msgs) == 2  # one tool result per tool

    @pytest.mark.asyncio
    async def test_denied_tool_writes_error_result(self):
        class _MockAgent:
            async def _execute_tool(self, tc, ctx):
                class R:
                    tool_name = tc.tool_name
                    result = f"ok_{tc.tool_name}"
                    error = None
                return R()
            def _build_tool_message(self, result, call_id):
                return {"role": "tool", "content": str(result.result) if result.result else str(result.error)}
            async def _drain_injections(self, ctx):
                pass
            async def _save_denial_checkpoint(self, ctx):
                pass

        agent = _MockAgent()
        node = ToolNode(agent, enable_approval=False, enable_hooks=False)

        class _History:
            def __init__(self):
                self.msgs = []
            async def append(self, msg):
                self.msgs.append(msg)

        history = _History()
        tc1 = type("TC", (), {"tool_name": "t1", "call_id": "c1", "arguments": {}})()

        # Inject pre-made decisions via metadata (simulated resume)
        response = type("R", (), {"tool_calls": [tc1]})()

        ctx = type("Ctx", (), {
            "metadata": {
                ReActMetaKey.LLM_RESPONSE: response,
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
                ReActMetaKey.DENY_AS_CANCEL: True,
            },
            "emitter": _MockEmitter(),
            "history": history,
            "extensions": {},
        })()

        # Override _execute_batch to test denied path
        result = await node._execute_batch(
            [tc1], [ApprovalDecision.DENIED], ctx,
        )
        assert result.target == ReActNode.END
        assert result.reason == ReActReason.TURN_CANCELLED
        # Tool message should contain error text
        assert "Error" in str(history.msgs[0].get("content", ""))
```

- [ ] **Step 2: Run test — verify failure**

```bash
pytest tests/unit/agents/react/test_nodes.py::TestToolNode -v
```
Expected: FAIL.

- [ ] **Step 3: Implement ToolNode**

Write `framework/agents/react/nodes/tool.py`:

```python
"""ToolNode — classify tools → strategy → batch execute."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.core.agent import AgentContext, ctx_ext
from framework.core.context_extensions import ExtensionKey
from framework.core.emitter import ToolCall, ToolResult
from framework.core.graph.node import Node, NodeTransition
from framework.approval.constants import ApprovalDecision
from framework.approval.state import ApprovalRequest
from framework.agents.react.agent import ReActEvent
from framework.agents.react.constants import ReActNode, ReActReason, ReActMetaKey
from framework.hook import HookPoint

if TYPE_CHECKING:
    from framework.agents.react.strategy import SuspendStrategy
    from framework.agents.react.agent import ReActAgent


class ToolNode(Node):
    """Two-phase tool node: classify all → strategy solicit → batch execute."""

    def __init__(self, agent: ReActAgent, *,
                 enable_approval: bool = True,
                 enable_hooks: bool = True) -> None:
        super().__init__(ReActNode.TOOL)
        self._agent = agent
        self._enable_approval = enable_approval
        self._enable_hooks = enable_hooks

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        response = ctx.metadata.pop(ReActMetaKey.LLM_RESPONSE)
        tool_calls: list[ToolCall] = response.tool_calls

        # Phase 1: classify all tools
        decisions = self._classify_all(tool_calls, ctx)

        # Phase 2: if any need approval, delegate to strategy
        if self._enable_approval and ApprovalDecision.PENDING in decisions:
            requests = [
                ApprovalRequest(
                    tool_name=tc.tool_name,
                    tool_call_id=tc.call_id or "",
                    arguments=tc.arguments or {},
                    tier="dangerous",
                    iteration=ctx.metadata[ReActMetaKey.ITERATION],
                )
                for tc, d in zip(tool_calls, decisions) if d == ApprovalDecision.PENDING
            ]
            strategy = ctx_ext(ctx, ExtensionKey.SUSPEND_STRATEGY)
            # Build full decision list: preserved + resolved
            resolved = await strategy.solicit_approval(requests, ctx)
            decisions = self._merge(decisions, resolved)

        # Phase 3: batch execute
        return await self._execute_batch(tool_calls, decisions, ctx)

    def _classify_all(self, tool_calls: list[ToolCall], ctx: AgentContext) -> list[str]:
        """Classify via interceptor._classify_tier(). NORMAL→ALLOWED, HARDLINE→DENIED, others→PENDING."""
        interceptor_chain = ctx_ext(ctx, ExtensionKey.INTERCEPTOR_CHAIN)
        decisions: list[str] = []
        for tc in tool_calls:
            if interceptor_chain:
                tier = interceptor_chain.classify_tier(tc)
                if tier == "normal":
                    decisions.append(ApprovalDecision.ALLOWED)
                elif tier == "hardline":
                    decisions.append(ApprovalDecision.DENIED)
                else:
                    decisions.append(ApprovalDecision.PENDING)
            else:
                decisions.append(ApprovalDecision.ALLOWED)
        return decisions

    def _merge(self, original: list[str], resolved: list[str]) -> list[str]:
        """Merge resolved decisions into original list."""
        result = list(original)
        ri = 0
        for i in range(len(result)):
            if result[i] == ApprovalDecision.PENDING:
                if ri < len(resolved):
                    result[i] = resolved[ri]
                    ri += 1
        return result

    async def _execute_batch(
        self, tool_calls: list[ToolCall], decisions: list[str], ctx: AgentContext,
    ) -> NodeTransition:
        denied_encountered = False
        for tc, dec in zip(tool_calls, decisions):
            if denied_encountered:
                dec = ApprovalDecision.PREEMPTED

            await ctx.emitter.emit(ReActEvent.TOOL_CALL_START, tc)

            if dec == ApprovalDecision.ALLOWED:
                if self._enable_hooks:
                    await self._agent._call_hooks(HookPoint.BEFORE_TOOL_EXECUTION, ctx, [tc])
                result = await self._agent._execute_tool(tc, ctx)
                if self._enable_hooks:
                    await self._agent._call_hooks(HookPoint.AFTER_TOOL_EXECUTION, ctx, [result])
            else:
                result = ToolResult(
                    tool_name=tc.tool_name, result=None,
                    error=f"Error: {dec}",
                )

            await ctx.emitter.emit(ReActEvent.TOOL_CALL_END, (tc, result))

            tool_msg = self._agent._build_tool_message(result, tc.call_id)
            await ctx.history.append(tool_msg)
            ctx.metadata[ReActMetaKey.ITERATION_MSGS].append(tool_msg)

            if dec in (ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED):
                denied_encountered = True

        if self._enable_hooks:
            await self._agent._drain_injections(ctx)

        if denied_encountered and ctx.metadata.get(ReActMetaKey.DENY_AS_CANCEL):
            await self._agent._save_denial_checkpoint(ctx)
            return NodeTransition(ReActNode.END, ReActReason.TURN_CANCELLED)

        return NodeTransition(ReActNode.LLM, ReActReason.TOOLS_DONE)
```

- [ ] **Step 4: Run test — verify pass**

```bash
pytest tests/unit/agents/react/test_nodes.py::TestToolNode -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add framework/agents/react/nodes/tool.py tests/unit/agents/react/test_nodes.py
git commit -m "feat: add ToolNode with two-phase classify/execute"
```

---

### Task 15: ReActGraph

**Files:**
- Create: `framework/agents/react/graph.py`
- Create: `tests/unit/agents/react/test_graph.py`

- [ ] **Step 1: Write failing test**

Write `tests/unit/agents/react/test_graph.py`:

```python
"""Tests for ReActGraph."""
from framework.agents.react.graph import ReActGraph
from framework.agents.react.constants import ReActNode, ReActReason


class TestReActGraph:
    def test_full_mode_has_all_nodes(self):
        g = ReActGraph(None, mode="full")
        assert ReActNode.START in g._nodes
        assert ReActNode.LLM in g._nodes
        assert ReActNode.TOOL in g._nodes
        assert ReActNode.END in g._nodes

    def test_clean_mode_has_all_nodes(self):
        g = ReActGraph(None, mode="clean")
        assert ReActNode.START in g._nodes
        assert ReActNode.LLM in g._nodes
        assert ReActNode.TOOL in g._nodes
        assert ReActNode.END in g._nodes

    def test_full_mode_all_edges_defined(self):
        g = ReActGraph(None, mode="full")
        assert g.next_node(ReActNode.START, ReActReason.NORMAL_START) == ReActNode.LLM
        assert g.next_node(ReActNode.START, ReActReason.RESUME_TOOLS) == ReActNode.TOOL
        assert g.next_node(ReActNode.LLM, ReActReason.HAS_TOOLS) == ReActNode.TOOL
        assert g.next_node(ReActNode.LLM, ReActReason.NO_TOOLS) == ReActNode.END
        assert g.next_node(ReActNode.TOOL, ReActReason.TOOLS_DONE) == ReActNode.LLM
        assert g.next_node(ReActNode.TOOL, ReActReason.TURN_CANCELLED) == ReActNode.END

    def test_clean_mode_same_topology(self):
        g = ReActGraph(None, mode="clean")
        # Clean should have same topology
        assert g.next_node(ReActNode.LLM, ReActReason.HAS_TOOLS) == ReActNode.TOOL
        assert g.next_node(ReActNode.LLM, ReActReason.NO_TOOLS) == ReActNode.END
        assert g.next_node(ReActNode.TOOL, ReActReason.TOOLS_DONE) == ReActNode.LLM
```

- [ ] **Step 2: Run test — verify failure**

```bash
pytest tests/unit/agents/react/test_graph.py -v
```
Expected: FAIL.

- [ ] **Step 3: Implement ReActGraph**

Write `framework/agents/react/graph.py`:

```python
"""ReActGraph — clean or full ReAct loop as a Graph."""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from framework.core.graph.graph import Graph
from framework.agents.react.constants import ReActNode, ReActReason
from framework.agents.react.nodes.start import StartNode
from framework.agents.react.nodes.llm import LLMNode
from framework.agents.react.nodes.tool import ToolNode
from framework.agents.react.nodes.end import EndNode

if TYPE_CHECKING:
    from framework.agents.react.agent import ReActAgent


class ReActGraph(Graph):

    def __init__(self, agent: ReActAgent | None, *,
                 mode: Literal["clean", "full"] = "full") -> None:
        super().__init__(name=f"react_{mode}")
        enable = mode == "full"

        self.add_node(StartNode())
        self.add_node(LLMNode(agent, enable_hooks=enable))
        self.add_node(ToolNode(agent, enable_approval=enable, enable_hooks=enable))
        self.add_node(EndNode(agent))

        # start edges
        self.add_edge(ReActNode.START, ReActNode.LLM, reason=ReActReason.NORMAL_START)
        self.add_edge(ReActNode.START, ReActNode.TOOL, reason=ReActReason.RESUME_TOOLS)

        # llm edges
        self.add_edge(ReActNode.LLM, ReActNode.TOOL, reason=ReActReason.HAS_TOOLS)
        self.add_edge(ReActNode.LLM, ReActNode.END, reason=ReActReason.NO_TOOLS)
        self.add_edge(ReActNode.LLM, ReActNode.END, reason=ReActReason.MAX_ITERATIONS)
        self.add_edge(ReActNode.LLM, ReActNode.END, reason=ReActReason.LLM_ERROR)

        # tool edges
        self.add_edge(ReActNode.TOOL, ReActNode.LLM, reason=ReActReason.TOOLS_DONE)
        self.add_edge(ReActNode.TOOL, ReActNode.END, reason=ReActReason.TURN_CANCELLED)
```

- [ ] **Step 4: Run test — verify pass**

```bash
pytest tests/unit/agents/react/test_graph.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add framework/agents/react/graph.py tests/unit/agents/react/test_graph.py
git commit -m "feat: add ReActGraph with clean/full modes"
```

---

### Task 16: ReActAgent thin shell

**Files:**
- Modify: `framework/agents/react/agent.py`
- Create: `tests/unit/agents/react/test_agent.py`

- [ ] **Step 1: Write failing test**

Write `tests/unit/agents/react/test_agent.py`:

```python
"""Tests for ReActAgent thin shell."""
import pytest
from framework.agents.react.agent import ReActAgent, ReActEvent
from framework.core.agent import AgentContext
from framework.core.tool_manager import InMemoryToolManager
from framework.memory.history import ListMessageHistory
from framework.core.context_extensions import ExtensionKey
from framework.core.graph.graph import GraphEngine
from framework.agents.react.graph import ReActGraph
from framework.agents.react.strategy import InlineWaitStrategy


class _MockProvider:
    pass


class TestReActAgentConstruction:
    def test_creates_graph_and_engine(self):
        agent = ReActAgent(_MockProvider(), mode="clean")
        assert isinstance(agent.graph, ReActGraph)
        assert isinstance(agent.engine, GraphEngine)

    def test_name(self):
        agent = ReActAgent(_MockProvider())
        assert agent.name == "ReActAgent"


class TestReActAgentRun:
    @pytest.mark.asyncio
    async def test_contextvar_set_and_cleared(self):
        """Verify ctx.emitter is set before run and cleared after."""
        agent = ReActAgent(_MockProvider(), mode="clean")

        class _Emitter:
            def wants_streaming(self): return False

        class _History:
            async def to_list(self): return []

        ctx = AgentContext(
            system_prompt="Hi",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
        )

        # The clean graph will run to completion (no tool calls → LLM → End)
        # But without a real provider, it will error. That's OK for this test.
        emitter = _Emitter()
        try:
            result = await agent.run(ctx, emitter)
        except Exception:
            pass  # Expected without real provider

        # contextvar should be reset
        from framework.core.agent import current_agent_context
        try:
            val = current_agent_context.get()
            # Should not have our ctx anymore
            assert val is not ctx or ctx.emitter is None
        except LookupError:
            pass  # Also OK — contextvar may have been fully reset
```

- [ ] **Step 2: Run test — verify current agent.py still works**

```bash
pytest tests/unit/agents/react/test_agent.py -v
```
Expected: May FAIL depending on test setup. If it passes, proceed. If it fails because of test setup issues, fix the test, then proceed.

- [ ] **Step 3: Rewrite ReActAgent as thin shell**

Replace `framework/agents/react/agent.py`. The new file keeps:
- `ReActEvent` enum (unchanged)
- All helper methods that nodes call: `_execute_tool`, `_execute_tool_raw`, `_build_assistant_message`, `_build_tool_message`, `_call_hooks`, `_save_checkpoint`, `_save_denial_checkpoint`, `_clear_checkpoint`, `_drain_injections`, `_stream_with_control`, `_resolve_hook_timeout`, `_resolve_tool_timeout`
- NEW `run()` method — thin, delegates to engine
- NEW `__init__` — creates ReActGraph + GraphEngine

The key change is `run()`:

```python
async def run(
    self,
    context: AgentContext,
    emitter: ContentEmitter[ReActEvent],
) -> AgentResult:
    context.emitter = emitter
    ctx_token = current_agent_context.set(context)

    try:
        await self._call_hooks(HookPoint.BEFORE_TURN, context)
        result = await self.engine.run(context)
        await self._call_hooks(HookPoint.AFTER_TURN, context, result)
        return result
    except GraphInterrupt:
        raise
    except AgentControlError:
        logger.warning("ReActAgent control exit: ...")
        try:
            all_new = context.metadata.get(ReActMetaKey.ITERATION_MSGS, [])
            await asyncio.shield(self._save_checkpoint(all_new, context))
        except Exception:
            pass
        raise
    except asyncio.CancelledError:
        logger.warning("ReActAgent cancelled")
        try:
            all_new = context.metadata.get(ReActMetaKey.ITERATION_MSGS, [])
            await asyncio.shield(self._save_checkpoint(all_new, context))
        except Exception:
            pass
        raise
    except Exception as e:
        logger.exception("Agent execution error")
        await emitter.emit(ReActEvent.ERROR, str(e))
        all_new = context.metadata.get(ReActMetaKey.ITERATION_MSGS, [])
        await self._save_checkpoint(all_new, context)
        result = AgentResult(error=str(e), stop_reason="error", messages=all_new, attachments=context.attachments)
        await emitter.emit_complete(result)
        return result
    finally:
        context.metadata.pop(ReActMetaKey.DENY_AS_CANCEL, None)
        context.metadata.pop(ReActMetaKey.APPROVAL_DENIAL, None)
        context.metadata.pop(ReActMetaKey.INJECTION_CYCLE, None)
        context.emitter = None
        current_agent_context.reset(ctx_token)
```

The `__init__` method:

```python
def __init__(
    self,
    provider: LLMProvider,
    hook_timeout: float = _HOOK_TIMEOUT,
    tool_timeout: float = _TOOL_TIMEOUT,
    *,
    mode: Literal["clean", "full"] = "full",
):
    self.provider = provider
    self._hook_timeout = hook_timeout
    self._tool_timeout = tool_timeout
    self.graph = ReActGraph(self, mode=mode)
    self.engine = GraphEngine(self.graph)
```

Remove the old `run()` method body (lines 123-355). Keep all helper methods and the `ReActEvent` enum.

- [ ] **Step 4: Run all react tests**

```bash
pytest tests/unit/agents/react/ -v
```
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add framework/agents/react/agent.py tests/unit/agents/react/test_agent.py
git commit -m "refactor: ReActAgent thin shell delegating to GraphEngine"
```

---

### Task 17: Pipeline integration

**Files:**
- Modify: `framework/pipeline/pipeline.py`

**Changes:**
1. Inject `SuspendStrategy` into `AgentContext.extensions`
2. Add approval consumption in `_process_message_locked` (step [3])
3. Add multi-agent message buffer (`_approval_pending`)
4. Import new modules

- [ ] **Step 1: Add strategy injection**

In `_process_message_locked`, after constructing `agent_context`, add:

```python
# Inject suspend strategy
from framework.agents.react.strategy import SuspendResumeStrategy
from framework.agents.react.state import StateStoreTurnResumeStateStore
from framework.approval.store import LocalFileApprovalStateStore

if not agent_context.extensions.get(ExtensionKey.SUSPEND_STRATEGY):
    # Default: SuspendResume with LocalFile stores
    from pathlib import Path
    workspace = Path(".modex_approval")
    approval_store = LocalFileApprovalStateStore(workspace)
    resume_store = StateStoreTurnResumeStateStore(self.checkpoint_store)
    strategy = SuspendResumeStrategy(approval_store, resume_store)
    agent_context.extensions[ExtensionKey.SUSPEND_STRATEGY] = strategy
```

- [ ] **Step 2: Add approval consumption**

Insert between step [2] (command_interceptor) and step [4] (save user msg):

```python
# [3] Approval consumption
from framework.core.graph.interrupt import _current_resume
strategy = agent_context.extensions.get(ExtensionKey.SUSPEND_STRATEGY)
if isinstance(strategy, SuspendResumeStrategy):
    approval_state = await strategy._approval_store.load(session_id)
    if approval_state is not None and _looks_like_approval(input_msg):
        tool_call_id = _extract_tool_call_id(input_msg)
        decision = _extract_decision(input_msg)
        if tool_call_id and decision:
            approval_state.apply(tool_call_id, decision)
            if approval_state.every_tool_decided:
                resume_state = await strategy._resume_store.load(session_id)
                ctx = ...  # reconstruct AgentContext with resume state
                ctx.metadata[ReActMetaKey.RESUME_STATE] = resume_state
                _current_resume.set(approval_state.final_decisions())
                try:
                    await self.agent.run(ctx, ctx.emitter)
                finally:
                    _current_resume.set(None)
                await strategy._approval_store.delete(session_id)
                await strategy._resume_store.delete(session_id)
                await self._drain_approval_buffer(session_id)
            else:
                await strategy._approval_store.save(approval_state)
        return None
```

- [ ] **Step 3: Add multi-agent buffer**

Add to `AgentPipeline.__init__`:

```python
self._approval_pending: dict[str, list] = {}
```

In `_process_message()`:

```python
if session_id in self._approval_pending:
    self._approval_pending[session_id].append(input_msg)
    return None
```

In the `except GraphInterrupt` block after `agent.run()`:

```python
except GraphInterrupt:
    self._approval_pending.setdefault(session_id, [])
    raise
```

Add `_drain_approval_buffer` method:

```python
async def _drain_approval_buffer(self, session_id: str) -> None:
    pending = self._approval_pending.pop(session_id, [])
    for msg in pending:
        await self._process_message(msg)
```

- [ ] **Step 4: Add imports to pipeline.py**

```python
from framework.core.context_extensions import ExtensionKey
from framework.core.graph.interrupt import GraphInterrupt, _current_resume
from framework.agents.react.constants import ReActMetaKey
from framework.agents.react.strategy import SuspendResumeStrategy
from framework.approval.constants import ApprovalDecision
```

- [ ] **Step 5: Run pipeline tests**

```bash
pytest tests/unit/pipeline/ -v
```
Fix any failures.

- [ ] **Step 6: Commit**

```bash
git add framework/pipeline/pipeline.py
git commit -m "feat: add strategy injection, approval consumption, and multi-agent buffer to Pipeline"
```

---

### Task 18: Clean up stale code

- [ ] **Step 1: Remove stale __pycache__ and empty dirs**

```bash
find framework -type d -name "__pycache__" -path "*/checkpoint/*" -o -type d -name "__pycache__" -path "*/state_store/*" -o -type d -name "__pycache__" -path "*/ui/*" | xargs rm -rf
find framework/agents/react/nodes -name "*.py" -not -name "__init__.py" -not -name "start.py" -not -name "llm.py" -not -name "tool.py" -not -name "end.py" -delete
```

Also clean any stale .pyc files:

```bash
find framework -name "*.pyc" -delete
```

- [ ] **Step 2: Commit**

```bash
git add -u
git commit -m "chore: remove stale __pycache__ and leftover files"
```

---

### Task 19: Run full test suite

- [ ] **Step 1: Run all unit tests**

```bash
pytest tests/unit/ -q --tb=short --ignore=tests/unit/plugins 2>&1 | tail -10
```
Expected: All 1141+ tests pass. Fix any regressions.

- [ ] **Step 2: Run lint**

```bash
ruff check framework/
```
Fix any issues.

- [ ] **Step 3: Run type check**

```bash
mypy framework/ --ignore-missing-imports 2>&1 | tail -20
```
Fix type errors.

- [ ] **Step 4: Commit**

```bash
git add -u
git commit -m "chore: fix regressions from graph refactor"
```

---

### Task 20: bot_project adaptation

**Files:**
- Modify: `examples/bot_project/bot/service/core.py`

- [ ] **Step 1: Wire SuspendResumeStrategy in bot initialize()**

In `bot/service/core.py:initialize()`, after building `self.interceptor_chain`:

```python
# Build suspend strategy with persistent stores
from framework.agents.react.strategy import SuspendResumeStrategy
from framework.agents.react.state import StateStoreTurnResumeStateStore
from framework.approval.store import LocalFileApprovalStateStore
from pathlib import Path

approval_store = LocalFileApprovalStateStore(
    Path(self._config.get("approval_workspace", ".modex_approval"))
)
resume_store = StateStoreTurnResumeStateStore(self._state_store)
self._suspend_strategy = SuspendResumeStrategy(approval_store, resume_store)
```

- [ ] **Step 2: Inject strategy in _initialize_pipeline**

In `_initialize_pipeline`, when constructing `AgentPipeline`, add strategy to extensions:

```python
agent_context_extensions = {
    ExtensionKey.INTERCEPTOR_CHAIN: self.interceptor_chain,
    ExtensionKey.SUSPEND_STRATEGY: self._suspend_strategy,
    # ... other extensions
}
```

Alternatively, inject via Pipeline's agent context construction. Add `suspend_strategy` parameter to AgentPipeline if needed.

- [ ] **Step 3: Ensure ReActAgent uses full mode**

```python
self.agent = ReActAgentBuilder(provider=provider)\
    .with_mode("full")\
    .build()
```

- [ ] **Step 4: Test bot_project startup**

```bash
cd examples/bot_project && python -c "from bot.service.core import BotService; print('Import OK')"
```

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/
git commit -m "feat: wire SuspendResumeStrategy in bot_project"
```
