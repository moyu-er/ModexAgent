"""Framework WorkspaceResources type contract (type-safety rule #8).

``resolve_workspace()`` returns ``WorkspaceResources`` so the pipeline and the
communication service can read ``ws.pool_data`` with a checked type instead of
``object`` + ``# type: ignore``. The business ``R`` satisfies the contract.
"""

from __future__ import annotations

from bot.workspace.handle import PoolWorkspaceResources

from modex_agent.workspace.resources import WorkspaceResources


def test_pool_workspace_resources_satisfies_framework_contract() -> None:
    assert issubclass(PoolWorkspaceResources, WorkspaceResources)
