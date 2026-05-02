"""ExtensionKey constants for AgentContext.extensions dict."""


class ExtensionKey:
    RUNTIME_CTX_MGR = "runtime_context_manager"
    RUNTIME_CTX = "runtime_context"
    GOVERNANCE = "governance"
    SAFETY = "safety"
    MAX_TOOLS_PER_TURN = "max_tools_per_turn"
    ON_CHECKPOINT = "on_checkpoint"
