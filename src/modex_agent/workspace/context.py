"""WorkspaceContext — lightweight workspace identity (zero business deps).

Carries only the working dir (``target``), the data-root layout
(:class:`framework.workspace.paths.WorkspacePaths`), and the ``is_home`` flag. It
deliberately holds NO pool config: pool is a business concept and stays in the
business factory (this package is pool-agnostic). Cheap to hold many of these
concurrently — an idle workspace is just a context.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from modex_agent.workspace.paths import WorkspacePaths


@dataclass(frozen=True)
class WorkspaceContext:
    """Identity of one workspace. Immutable; keyed by ``target`` absolute path."""

    target: Path
    paths: WorkspacePaths
    is_home: bool

    @classmethod
    def from_target(
        cls, target: Path, *, data_dir_name: str, home: Path
    ) -> WorkspaceContext:
        resolved = Path(target).resolve()
        return cls(
            target=resolved,
            paths=WorkspacePaths(root=resolved / data_dir_name),
            is_home=resolved == Path(home).resolve(),
        )
