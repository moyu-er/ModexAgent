"""External env builder — the single convergence point for ``MODEX_*`` vars.

`ExternalEnvBuilder.build(spec, base_env)` is the only place in the
codebase that constructs the 9 ``MODEX_*`` environment variables and the
PATH-prepend for ``modexctl``. Per ADR-0022 D6, no other site is
permitted to construct them.
"""

from __future__ import annotations

import os

from modex_agent.core.agent import AgentCommKind

from .types import ExternalEnvSpec


def _format_pool_map(pool_map: dict[str, str]) -> str:
    """Serialise an ``agent_name`` → ``pool_name`` map as ``name=pool;...``.

    Stable ordering by name keeps the wire-format reproducible so
    fixtures and trace dumps are deterministic.
    """
    return ";".join(f"{name}={pool}" for name, pool in sorted(pool_map.items()))


def _format_targets(targets: list[tuple[str, str]]) -> str:
    """Serialise target list as ``name=description;...``.

    Order is preserved as-given (the caller is the
    ``CommunicationTargetStore``, which already produces a stable
    order). ``=`` in a description is intentionally kept verbatim so
    callouts like ``"answer queries (status=open)"`` survive.
    """
    return ";".join(f"{name}={description}" for name, description in targets)


class ExternalEnvBuilder:
    """Static builder for the per-spawn ``MODEX_*`` env dict.

    Carries no state — the sole method is a pure function over its
    arguments plus the path-handling convention in this module.
    """

    @staticmethod
    def build_modex_vars(spec: ExternalEnvSpec) -> dict[str, str]:
        """Build only the ``MODEX_*`` env vars from an ``ExternalEnvSpec``.

        This is the single extraction point for MODEX_ var construction
        (ADR-0022 D6). Both :meth:`build` (external coding spawn) and
        :class:`modex_agent.hook.builtin.env_injection.NativeEnvInjectionHook`
        (native agent contextvar injection) call this method so the two
        paths never diverge.

        Returns a new ``dict[str, str]`` containing only ``MODEX_*`` keys.
        No ``PATH`` reconstruction or base-env merge happens here — callers
        that need a full spawn env use :meth:`build`.
        """
        modex: dict[str, str] = {
            "MODEX_WORKSPACE_ROOT": str(spec.workspace_root),
            "MODEX_INBOX_ROOT": str(spec.inbox_root),
            "MODEX_WORKDIR": str(spec.workdir),
            "MODEX_SESSION_ID": spec.session_id,
            "MODEX_AGENT_NAME": spec.agent_name,
            "MODEX_PROVIDER_SESSION_ID": spec.provider_session_id,
            "MODEX_AGENT_POOL_MAP": _format_pool_map(spec.agent_pool_map),
            "MODEX_TARGETS": _format_targets(spec.targets),
            "MODEX_COMM_KIND": spec.comm_kind.value,
        }
        if spec.comm_kind == AgentCommKind.SUBAGENT and spec.parent_session_id is not None:
            modex["MODEX_PARENT_SESSION_ID"] = spec.parent_session_id
        if spec.workflow_id is not None:
            modex["MODEX_WORKFLOW_ID"] = str(spec.workflow_id)
        if spec.task_id is not None:
            modex["MODEX_TASK_ID"] = str(spec.task_id)
        if spec.node_id is not None:
            modex["MODEX_NODE_ID"] = str(spec.node_id)
        return modex

    @staticmethod
    def build(spec: ExternalEnvSpec, base_env: dict[str, str]) -> dict[str, str]:
        """Build the spawn env from an ``ExternalEnvSpec`` and a base env.

        The returned dict is a **new** ``dict[str, str]`` — the input
        ``base_env`` is not mutated. ``PATH`` is recreated by
        ``modexctl_bin_dir + os.pathsep + base_env["PATH"]``; missing
        ``PATH`` on POSIX is treated as empty (Windows shells always
        provide one).

        Args:
            spec: Source values for the ``MODEX_*`` fields.
            base_env: Base environment to merge with (typically
                ``os.environ``). Only ``PATH`` is read from it; the
                result is a fresh dict the caller can mutate freely.

        Returns:
            New ``dict[str, str]`` containing the ``MODEX_*`` string
            vars plus a recreated ``PATH`` with the modexctl directory
            prepended.
        """
        modex = ExternalEnvBuilder.build_modex_vars(spec)

        base_path = base_env.get("PATH", "")
        new_path = (
            str(spec.modexctl_bin_dir) + os.pathsep + base_path
            if base_path
            else str(spec.modexctl_bin_dir)
        )

        merged: dict[str, str] = dict(base_env)
        merged.update(modex)
        merged["PATH"] = new_path
        return merged


__all__ = ["ExternalEnvBuilder"]
