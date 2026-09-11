"""Ordered lifecycle ownership for resources assembled with a native agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from modex_agent.core.tool_group import ToolGroupResource

if TYPE_CHECKING:
    from modex_agent.multi_agent.descriptor import AgentInstance


class AssemblyResourceOwner:
    """Own resources in dependency order until an AgentInstance adopts them.

    Guards are adopted before the shell groups that depend on them. Rollback
    and instance teardown therefore close from the tail, retaining the failed
    dependent and every prerequisite for a later retry.
    """

    def __init__(self) -> None:
        self._resources: list[ToolGroupResource] = []
        self._instance: AgentInstance | None = None

    @property
    def has_resources(self) -> bool:
        return bool(self._resources)

    def adopt(self, resource: ToolGroupResource) -> None:
        if all(existing is not resource for existing in self._resources):
            self._resources.append(resource)

    def transfer(self, instance: AgentInstance) -> None:
        instance.adopt_resources(tuple(self._resources))
        self._resources.clear()
        self._instance = instance

    async def cleanup(self) -> None:
        instance = self._instance
        if instance is not None:
            if not await instance.stop():
                raise RuntimeError(
                    "assembly cleanup retained resources because agent turns did not drain"
                )
            if self._instance is instance:
                self._instance = None
            return
        while self._resources:
            await self._resources[-1].aclose()
            self._resources.pop()

    async def rollback(self, failure: BaseException | None = None) -> None:
        """Attempt pending cleanup without replacing the assembly failure."""
        try:
            await self.cleanup()
        except BaseException as cleanup_error:
            if failure is None:
                raise
            if cleanup_error is not failure:
                failure.add_note(
                    f"Assembly resource cleanup also failed: {cleanup_error!r}"
                )


__all__ = ["AssemblyResourceOwner"]
