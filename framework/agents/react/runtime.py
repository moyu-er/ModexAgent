"""ReActRuntime -- typed runtime service object for ReActAgent."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from framework.core.context_extensions import ExtensionKey

if TYPE_CHECKING:
    from framework.agents.react.approval import ApprovalRuntime
    from framework.control.checkpoint import RuntimeStateStore
    from framework.control.runtime import ControlRuntime
    from framework.core.llm_error import RuntimeSafetyPolicy
    from framework.hook import HookRunner
    from framework.interceptor.chain import InterceptorChain
    from framework.memory.context_governance import ContextGovernance

logger = logging.getLogger(__name__)

_CLEAN_EXTENSION_KEYS = (
    "hook_runner",
    "hooks",
    "interceptor_chain",
    "checkpoint_store",
    "injection_queue",
)


def sanitize_clean_runtime(ctx: Any) -> list[str]:
    """Clear full-mode extension keys from context. Returns list of disabled keys."""
    disabled: list[str] = []
    for key in _CLEAN_EXTENSION_KEYS:
        if key in ctx.extensions:
            ctx.extensions.pop(key, None)
            disabled.append(key)
    return disabled


@dataclass
class ReActRuntime:
    """Typed runtime service object for ReActAgent.

    mode="clean": all runtime services disabled (hooks, interceptors,
    approval, control, checkpoints, injection).
    mode="full": services consumed from AgentContext.extensions.
    """

    mode: Literal["clean", "full"]

    # Runtime services -- populated in full mode from AgentContext.extensions.
    hooks: HookRunner | None = None
    interceptors: InterceptorChain | None = None
    approval: ApprovalRuntime | None = None
    control: ControlRuntime | None = None
    checkpoint_store: RuntimeStateStore | None = None
    injection_queue: asyncio.Queue[str] | None = None
    governance: ContextGovernance | None = None
    safety: RuntimeSafetyPolicy | None = None

    @classmethod
    def clean(cls) -> ReActRuntime:
        """Factory for clean mode -- all services None."""
        return cls(mode="clean")

    @classmethod
    def from_context(cls, ctx: Any, *, mode: str) -> ReActRuntime:
        """Build runtime from AgentContext.extensions.

        clean mode: sanitise extension keys, return clean runtime.
        full mode: pop extension keys into typed fields.
        """
        if mode == "clean":
            disabled = sanitize_clean_runtime(ctx)
            if disabled:
                logger.info(
                    "ReActAgent clean mode: disabled runtime extensions: %s",
                    ", ".join(disabled),
                )
            return cls.clean()

        # Full mode: consume extensions into runtime fields
        from framework.hook import HookErrorPolicy, HookRunner, HookSpec

        hook_runner = ctx.extensions.pop("hook_runner", None)
        hooks = ctx.extensions.pop("hooks", None)
        if hook_runner is None and hooks:
            hook_runner = HookRunner([
                HookSpec(hook=h, on_error=HookErrorPolicy.LOG) for h in hooks
            ])

        return cls(
            mode="full",
            hooks=hook_runner,
            interceptors=ctx.extensions.pop("interceptor_chain", None),
            checkpoint_store=ctx.extensions.pop("checkpoint_store", None),
            injection_queue=ctx.extensions.pop("injection_queue", None),
            governance=ctx.extensions.pop(ExtensionKey.GOVERNANCE, None),
            safety=ctx.extensions.pop(ExtensionKey.SAFETY, None),
        )

    def validate(self) -> None:
        """Raise PolicyViolation if full-mode combination is invalid."""
        if self.mode == "clean":
            return
        if self.interceptors is not None:
            from framework.control.exceptions import PolicyViolation
            from framework.interceptor.builtin import ControlDrainInterceptor
            from framework.interceptor.builtin.tool_approval import (
                TieredToolApprovalInterceptor,
            )

            for interceptor in self.interceptors.interceptors:
                if isinstance(interceptor, ControlDrainInterceptor) and self.control is None:
                    raise PolicyViolation(
                        "ControlDrainInterceptor configured but no ControlRuntime present"
                    )
                if (
                    isinstance(interceptor, TieredToolApprovalInterceptor)
                    and self.approval is None
                ):
                    logger.warning(
                        "TieredToolApprovalInterceptor configured but no ApprovalRuntime "
                        "present; around_tool_call() still works, but classification "
                        "will be unavailable"
                    )
