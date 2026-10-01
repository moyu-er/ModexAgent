"""Turn-execution vocabulary contract root (W1 layering surgery).

The pure enums and value objects every package level shares for one agent
turn: state/operation enums, turn models, the dispatch deadline, approval
decision/audit contracts, env ContextVars, and the todo values. Consumers
import module-qualified (``modex_agent.core.turn.enums`` etc.) — no facade
re-exports.
"""
