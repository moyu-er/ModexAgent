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
