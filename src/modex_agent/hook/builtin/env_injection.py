"""NativeEnvInjectionHook — populate ``_modex_env`` / ``_current_session_id``.

Sets the two per-task ContextVars that native agent bash/terminal subprocess
tools (``SubprocessExecutor``, ``CommandTool``) read to inject ``MODEX_*``
env vars into spawned processes.

External coding agents get their env via ``ExternalEnvBuilder.build()`` at
spawn time; native agents (ReAct) have no spawn boundary, so this hook sets
the contextvar at ``BEFORE_TURN`` — the same point the agent's tools will
read it.

Per ADR-0022 D6, no other site constructs ``MODEX_*`` vars — this hook calls
``ExternalEnvBuilder.build_modex_vars(spec)``, the single extraction point.
"""

from __future__ import annotations

from modex_agent.agents.external.env_builder import ExternalEnvBuilder, join_modexctl_path
from modex_agent.agents.external.types import ExternalEnvSpec
from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.hook.abc import BeforeTurnHook
from modex_agent.runtime.env_context import _current_session_id, _modex_env
from modex_agent.tools.terminal.env import build_full_env


class NativeEnvInjectionHook(BeforeTurnHook):
    """Populate ``_modex_env`` and ``_current_session_id`` at turn start.

    Native agent bash/terminal subprocess tools (``SubprocessExecutor``,
    ``CommandTool``) read ``_modex_env.get()`` for env overrides. Without
    this hook that contextvar is always ``None``, so native agents get no
    ``MODEX_*`` env vars passed to their subprocess spawns.

    The hook accepts an ``ExternalEnvSpec`` template carrying pool-static
    fields (``workspace_root``, ``inbox_root``, ``workdir``,
    ``agent_pool_map``, ``targets``, ``modexctl_bin_dir``, ``comm_kind``).
    Per-turn fields are sourced from ``ctx.session`` / ``ctx.comm_kind`` and
    merged via ``model_copy(update={...})`` (the spec is frozen Pydantic):

    - ``session_id`` / ``agent_name`` — always overridden from ``ctx.session``
      (the template carries placeholders like ``__pending__.main``).
    - ``comm_kind`` — overridden from ``ctx.comm_kind`` when not ``None``;
      otherwise the template's value is preserved.
    - ``parent_session_id`` — overridden from ``ctx.session.parent_session_id``
      when not ``None``; otherwise the template's value is preserved.
    - ``workflow_id`` / ``task_id`` / ``node_id`` — left as-is from the
      template (``None`` for now — Phase 2 NodeTaskStore).

    Both the main-agent pipeline (``pool_builder._wire_main_pipeline``)
    and subagent templates (``AgentTemplate.materialize``) register this
    hook. Subagent templates carry a minimal pool_map (self + parent) and
    targets (parent only), matching star-topology constraints.
    """

    def __init__(self, env_spec_template: ExternalEnvSpec) -> None:
        self._template: ExternalEnvSpec = env_spec_template

    @property
    def name(self) -> str:
        return "native_env_injection"

    async def before_turn(self, ctx: AgentContext) -> None:
        overrides: dict[str, str | AgentCommKind | None] = {
            "session_id": ctx.session.session_id,
            "agent_name": ctx.session.agent_name,
        }
        # comm_kind: ctx overrides template when explicitly set on the turn.
        if ctx.comm_kind is not None:
            overrides["comm_kind"] = ctx.comm_kind
        # parent_session_id: ctx overrides template when the session has one.
        if ctx.session.parent_session_id is not None:
            overrides["parent_session_id"] = ctx.session.parent_session_id

        spec = self._template.model_copy(update=overrides)
        modex_vars = ExternalEnvBuilder.build_modex_vars(spec)

        # Prepend modexctl_bin_dir to PATH so native agent subprocesses find
        # modexctl. build_full_env(overrides) does env.update(overrides), so
        # PATH in overrides replaces the base PATH — we construct the full
        # PATH here (modexctl_bin_dir + build_full_env's PATH with bundled_bin
        # + registry merge) so nothing is lost.
        base_path = build_full_env().get("PATH", "")
        modex_vars["PATH"] = join_modexctl_path(spec.modexctl_bin_dir, base_path)

        _modex_env.set(modex_vars)
        _current_session_id.set(ctx.session.session_id)


__all__ = ["NativeEnvInjectionHook"]
