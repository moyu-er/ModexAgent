"""Exact public-surface guards for facades affected by the E1 ownership split."""

from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType

import modex_agent
import modex_agent.agents.react as react
import modex_agent.approval as approval
import modex_agent.memory as memory
import modex_agent.messaging as messaging
import modex_agent.persistence as persistence
import modex_agent.persistence.adapters as persistence_adapters
import modex_agent.pipeline as pipeline
import modex_agent.runtime as runtime

REPO_ROOT = Path(__file__).resolve().parents[2]
TOP_LEVEL_INIT = REPO_ROOT / "src" / "modex_agent" / "__init__.py"

EXPECTED_MEMORY_ALL = [
    "MemorySystem",
    "DefaultMemorySystem",
    "create_memory_system",
    "MemorySystemContextManager",
    "ContextManager",
    "ContextState",
    "InMemoryContextManager",
    "MemoryContext",
    "SessionScope",
    "UserScope",
    "TenantScope",
    "AgentScope",
    "ChannelScope",
    "ChatScope",
    "CompositeScope",
    "GlobalScope",
    "MemoryInjectionPolicy",
    "FullInjectionPolicy",
    "RestrictedInjectionPolicy",
    "ListMessageHistory",
    "ScopedMessageHistory",
    "MemoryLayerConfigSet",
    "MemoryLayerFactory",
    "SessionMemoryConfig",
    "ArchiveMemoryConfig",
    "CoreMemoryConfig",
    "PrunedManager",
]

EXPECTED_APPROVAL_ALL = [
    "AgentApprovalConfig",
    "ApprovalDecision",
    "ApprovalStatus",
    "ApprovalTier",
    "ApprovalUserInterface",
    "IMUserInterface",
    "ToolApprovalConfig",
]

EXPECTED_REACT_ALL = [
    "ReActAgent",
    "ReActEvent",
    "ReActAgentBuilder",
    "ReactGraphRuntime",
    "ToolCallEndPayload",
    "next_call_id",
]

EXPECTED_PERSISTENCE_ALL = [
    "ConnectionManager",
    "ConnectionNotOpenError",
    "DatabaseKind",
    "InvalidMigrationNameError",
    "InMemorySessionRegistry",
    "MigrationRunner",
    "NestedTransactionError",
    "SessionRegistry",
    "SessionStore",
    "Transaction",
    "TransactionControlStatementError",
    "safe_filename",
]

EXPECTED_PERSISTENCE_ADAPTERS_ALL = [
    "ApprovalAuditEntry",
    "ApprovalAuditStore",
    "LocalFileSessionStore",
    "PoolRoutingCorruptionError",
    "SqliteApprovalAuditStore",
    "SqliteCursorStore",
    "SqliteExternalSessionMapStore",
    "SqliteInboxMQ",
    "SqliteKVStore",
    "SqliteMessageStore",
    "SqlitePoolRoutingStore",
    "SqliteSessionStore",
    "SqliteTodoStore",
    "SqliteTurnStateStore",
    "SqliteScopeRegistryStore",
]

EXPECTED_MESSAGING_ALL = [
    "Address",
    "AddressKind",
    "BrokerMessage",
    "BrokerBridgeService",
    "BrokerInputAdapter",
    "BrokerInputPayload",
    "BrokerOutputAdapter",
    "BrokerOutputPayload",
    "DeliveryError",
    "MessageBroker",
    "InMemoryMessageBroker",
    "ApprovalAction",
    "ApprovalDecisionInput",
    "InputMessage",
    "MessageType",
    "OutputMessage",
    "OutputMessageType",
    "OutputRoute",
    "ReminderKind",
]

EXPECTED_PIPELINE_ALL = [
    "InputAdapter",
    "AgentPipeline",
]

EXPECTED_RUNTIME_ALL = [
    "AgentKind",
    "AgentRuntime",
    "AgentRuntimeServices",
    "EXECUTOR_PROCESS_ID_KEY",
    "InMemoryRuntimeContext",
    "JsonFileTodoStore",
    "ProcessIdentity",
    "ProcessRegistry",
    "RuntimeContext",
    "RuntimeContextManager",
    "SingletonProcessRegistry",
    "SnapshotPolicy",
    "ToolCallRecord",
    "TodoItem",
    "TodoStatus",
    "TodoStore",
    "TurnCustomKey",
    "TurnPhase",
    "require_runtime_state",
]

EXPECTED_TOP_LEVEL_ALL = [
    "__version__",
    "MessageType",
    "ToolCall",
    "ToolResult",
    "Tool",
    "LLMProvider",
    "CallbackStreamProvider",
    "AgentEvent",
    "EmitterConfig",
    "TurnEvent",
    "TurnReasoningEvent",
    "TurnTextEvent",
    "TurnToolCallEvent",
    "TurnToolResultEvent",
    "AgentResult",
    "ContentEmitter",
    "StreamingAwareEmitter",
    "ToolManager",
    "InMemoryToolManager",
    "ToolConfig",
    "Agent",
    "AgentContext",
    "ContextManager",
    "ContextState",
    "ReActAgent",
    "ReActEvent",
    "AgentPipeline",
    "InputAdapter",
    "OutputAdapter",
    "InputMessage",
    "OutputMessage",
]

FACADE_CONTRACTS: tuple[tuple[str, ModuleType, list[str]], ...] = (
    ("memory", memory, EXPECTED_MEMORY_ALL),
    ("approval", approval, EXPECTED_APPROVAL_ALL),
    ("agents.react", react, EXPECTED_REACT_ALL),
    ("persistence", persistence, EXPECTED_PERSISTENCE_ALL),
    ("persistence.adapters", persistence_adapters, EXPECTED_PERSISTENCE_ADAPTERS_ALL),
    ("messaging", messaging, EXPECTED_MESSAGING_ALL),
    ("pipeline", pipeline, EXPECTED_PIPELINE_ALL),
    ("runtime", runtime, EXPECTED_RUNTIME_ALL),
    ("modex_agent", modex_agent, EXPECTED_TOP_LEVEL_ALL),
)

TOP_LEVEL_OWNER_BY_NAME = {
    "MessageType": "messaging",
    "ToolCall": "core",
    "ToolResult": "core",
    "Tool": "core",
    "LLMProvider": "core",
    "CallbackStreamProvider": "core",
    "AgentEvent": "core",
    "EmitterConfig": "core",
    "TurnEvent": "core",
    "TurnReasoningEvent": "core",
    "TurnTextEvent": "core",
    "TurnToolCallEvent": "core",
    "TurnToolResultEvent": "core",
    "AgentResult": "core",
    "ContentEmitter": "core",
    "StreamingAwareEmitter": "adapters",
    "ToolManager": "core",
    "InMemoryToolManager": "tools",
    "ToolConfig": "core",
    "Agent": "core",
    "AgentContext": "core",
    "ContextManager": "memory",
    "ContextState": "memory",
    "ReActAgent": "agents.react",
    "ReActEvent": "agents.react",
    "AgentPipeline": "pipeline",
    "InputAdapter": "pipeline",
    "OutputAdapter": "adapters",
    "InputMessage": "messaging",
    "OutputMessage": "messaging",
}


def test_e1_facades_have_exact_public_surfaces() -> None:
    for name, facade, expected in FACADE_CONTRACTS:
        actual = getattr(facade, "__all__", None)
        assert actual == expected, f"{name} facade __all__ mismatch: {actual}"


def test_e1_facade_exports_are_importable() -> None:
    for name, facade, expected in FACADE_CONTRACTS:
        missing = [export for export in expected if not hasattr(facade, export)]
        assert not missing, f"{name} facade exports are not importable: {missing}"


def test_compatibility_exports_are_absent_from_old_facades() -> None:
    assert not hasattr(memory, "MemorySystemABC")
    assert not hasattr(approval, "ApprovalAction")
    assert not hasattr(persistence, "LocalFileSessionStore")
    assert not hasattr(persistence_adapters, "SessionRegistry")
    assert not hasattr(persistence_adapters, "SessionStore")
    assert not hasattr(pipeline, "InputMessage")


def test_top_level_imports_convenience_names_from_final_owner_facades() -> None:
    tree = ast.parse(TOP_LEVEL_INIT.read_text(encoding="utf-8"))
    imported_from: dict[str, str] = {}
    facade_line: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is not None:
            facade_line.setdefault(node.module, node.lineno)
            for imported in node.names:
                imported_from[imported.asname or imported.name] = node.module

    actual = {name: imported_from.get(name) for name in TOP_LEVEL_OWNER_BY_NAME}
    assert actual == TOP_LEVEL_OWNER_BY_NAME
    assert facade_line["messaging"] < facade_line["adapters"]
    assert not any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "__getattr__"
        for node in tree.body
    )
