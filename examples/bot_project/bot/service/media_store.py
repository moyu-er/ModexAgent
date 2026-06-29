"""Workspace- and pool-partitioned media store (ctxvar-routed writes).

Mirrors :mod:`bot.service.workspace_store` (the transcript store) exactly in
shape: a service-singleton with unified workspace+pool routing, NOT a parallel
mechanism. It resolves the pool's media directory from the bound workspace root
(the ``bind_workspace_root`` ctxvar) for in-turn writers, and accepts an
explicit ``media_dir`` override for HTTP readers (the WebUI download/upload
endpoints, which run outside any turn).

The framework :class:`LocalFileMediaStore` receives the resolved directory and
has NO workspace/pool coupling — it owns only the
``uploads/<session_id>/<attachment_id>`` subtree. This is the seam split
ADR-0013 §6 / design.md D8 require: framework byte store, business resolver.

Physical layout (ADR-0013 §3)::

    <root>/<data_dir>/media/<pool>/uploads/<session_id>/<attachment_id>

``<root>/<data_dir>/media/<pool>`` is what :meth:`WorkspacePaths.media_dir`
returns; this resolver builds ``WorkspacePaths(root=<ws_root> / <data_dir>)``
then calls ``.media_dir(pool)`` — the same plumbing the transcript store uses
for ``sessions_dir``.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path

from modex_agent.media.store import LocalFileMediaStore, MediaStore
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.runtime import is_workspace_root_bound, resolve_workspace_root

logger = logging.getLogger(__name__)


class WorkspaceScopedMediaStore:
    """Per-(workspace,pool) resolver handing each (ws,pool) a cached store.

    - One cached :class:`LocalFileMediaStore` per (media_dir, pool), lazily
      created under the resolved ``<root>/<data_dir>/media/<pool>/``.
    - Writers (``store_for`` with no override) resolve the owning media dir
      from the bound workspace root ctxvar; an unbound root lands under
      ``Path.cwd()/.modex`` and is surfaced with a loud warning.
    - Readers pass an explicit ``media_dir`` so HTTP handlers do not depend on
      the ctxvar.
    """

    def __init__(self, data_dir_name: str) -> None:
        self._data_dir_name: str = data_dir_name

    # ------------------------------------------------------------------
    # Directory resolution
    # ------------------------------------------------------------------

    def _ctxvar_media_dir(self, pool: str) -> Path:
        """Resolve the pool's media dir from the bound workspace root (ctxvar).

        Mirrors ``WorkspaceScopedTranscriptStore._ctxvar_sessions_dir``:
        ``WorkspacePaths(root=<ws_root> / <data_dir>).media_dir(pool)``.
        """
        root = resolve_workspace_root()
        return WorkspacePaths(root=root / self._data_dir_name).media_dir(pool)

    def _resolve_dir(self, media_dir: Path | None, pool: str) -> Path:
        """Return the explicit dir when given, else the ctxvar-resolved dir."""
        if media_dir is not None:
            return media_dir
        return self._ctxvar_media_dir(pool)

    def media_dir_for_pool(
        self, pool: str, *, media_dir: Path | None = None
    ) -> Path:
        """Return the resolved media directory for *pool*.

        Uses the ctxvar root when ``media_dir`` is omitted (in-turn callers).
        HTTP handlers should pass an explicit ``media_dir`` instead.
        """
        return self._resolve_dir(media_dir, pool)

    # ------------------------------------------------------------------
    # Physical stores
    # ------------------------------------------------------------------

    @staticmethod
    @functools.lru_cache(maxsize=64)
    def _store_for(media_dir: Path) -> LocalFileMediaStore:
        """Return a :class:`LocalFileMediaStore` for *media_dir*.

        ``media_dir`` already encodes the pool (``.../media/<pool>``), so it is
        the complete identity of a (ws,pool) partition — no separate pool key
        is needed (unlike the transcript store, where ``sessions_dir`` is the
        pool-agnostic parent and the store is built as
        ``JSONLTranscriptStore(sessions_dir / pool_key)``). The framework store
        is a stateless wrapper over the directory; the LRU cache is safe
        (evicted entries recreate with no data loss). The returned store has no
        ws/pool knowledge beyond the directory it owns.
        """
        return LocalFileMediaStore(media_dir)

    def store_for(
        self, pool: str, *, media_dir: Path | None = None
    ) -> MediaStore:
        """Return the framework store for the resolved (ws,pool) media dir.

        With no ``media_dir``: routes by ctxvar root (in-turn writers). With
        ``media_dir``: trusts the explicit dir (HTTP readers). An unbound root
        in the ctxvar path is surfaced with a loud warning, matching the
        transcript store.
        """
        if media_dir is not None:
            resolved = media_dir
        else:
            if not is_workspace_root_bound():
                logger.warning(
                    "[ws-partition] media store resolve for pool=%s with NO "
                    "bound workspace root — resolving under %s (cwd/home). "
                    "This is expected only outside a turn; a turn-time writer "
                    "must run inside bind_workspace_root() or pass an explicit "
                    "media_dir.",
                    pool,
                    resolve_workspace_root(),
                )
            resolved = self._ctxvar_media_dir(pool)
        return self._store_for(resolved)
