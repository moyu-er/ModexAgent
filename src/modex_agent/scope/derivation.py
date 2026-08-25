"""Shared spec-derivation core (SPEC §5.3/§5.6/§6.6).

The pure functions every AssemblySpec derivation shares: preset expansion,
tools/hooks ``+/-`` merging, the system-prompt sugar chain, agent-type
derivation, and the default component names. Historically the private
helpers of the deleted legacy spec-builder road; the ScopeCompiler
consumed them as-is (converge, don't duplicate) and they moved here when
the legacy roster road was deleted (ticket 11) — the compiler is their
single consumer now.
"""

from __future__ import annotations

from typing import Any

from modex_agent.core.constants import ProviderKind
from modex_agent.plugins.abc import AgentType
from modex_agent.tools.presets import ToolPreset, get_preset_tools

# Default LLM provider component name (SPEC §5.7 — v1 framework default).
_DEFAULT_LLM_PROVIDER: str = "default"

# Default system prompt provider component name (SPEC §5.6).
_FILE_PROMPT_PROVIDER: str = "file_prompt"

_BASH_TOOL_NAME = "bash"

# Presets that include the bash tool — mirrors ``get_preset_tools``' bash
# gating (``subprocess_tool_factory`` branch in presets.py).
_BASH_PRESETS = frozenset(
    {ToolPreset.FULL, ToolPreset.READ_ONLY, ToolPreset.READ_WRITE}
)


def _derive_agent_type(
    is_main: bool,
    provider_kind: ProviderKind | None,
) -> AgentType:
    """Derive AgentType from main/sub × provider_kind (SPEC §6.6).

    provider_kind None → native; pi/opencode → external.
    is_main → main variant; else sub variant.
    """
    external = provider_kind is not None
    if is_main:
        return AgentType.external_main if external else AgentType.native_main
    return AgentType.external_sub if external else AgentType.native_sub


def _expand_preset_tool_names(preset: ToolPreset) -> list[str]:
    """Expand a ToolPreset to its component tool names.

    ``get_preset_tools`` is called without ``subprocess_tool_factory`` (it
    returns Tool instances; only names are needed here), so the bash name is
    appended explicitly per the same gating presets.py applies: FULL,
    READ_ONLY, and READ_WRITE include ``bash``; NONE and WEB do not. The
    default registry registers a ``bash`` factory, keeping the name
    resolvable.
    """
    tools = get_preset_tools(preset)
    names = [t.name for t in tools]
    if preset in _BASH_PRESETS:
        names.append(_BASH_TOOL_NAME)
    return names


def _merge_tools(
    preset_names: list[str],
    tools_override: list[str] | None,
) -> list[str]:
    """Merge tools +/- syntax against the preset set (SPEC §5.3 G7).

    - ``tools_override`` is None → use preset set verbatim
    - All entries without +/- prefix → REPLACE preset set
    - Any entry with +/- → ADD/REMOVE from preset set; unprefixed entries
      in a mixed list are ignored (baseline annotations, not operational)
    """
    if tools_override is None:
        return list(preset_names)

    has_prefix = any(t.startswith(("+", "-")) for t in tools_override)
    if not has_prefix:
        # All unprefixed → replace preset set entirely
        return list(tools_override)

    # Mixed +/- list: only process +/- entries, ignore unprefixed
    result: list[str] = list(preset_names)
    for entry in tools_override:
        if entry.startswith("+"):
            name = entry[1:]
            if name and name not in result:
                result.append(name)
        elif entry.startswith("-"):
            name = entry[1:]
            if name in result:
                result.remove(name)
        # Unprefixed entry in mixed list → ignore (baseline annotation)
    return result


def _merge_hooks(
    hooks_override: list[str] | None,
) -> list[str]:
    """Merge hooks +/- increments into the final hook list (SPEC §5.3, C2).

    The roster ``hooks`` list is an INCREMENT over the code-wired default
    hook set (wired by ``_wire_main_pipeline`` / ``materialize`` — the
    defaults never enter the roster), so unlike ``_merge_tools`` there is
    no preset base to replace:

    - ``hooks_override`` is None → no roster hooks
    - ``+name`` → add (prefix stripped)
    - ``-name`` → exclude (no-op when absent); cannot remove code-wired
      defaults (D-A8)
    - bare ``name`` → add
    """
    if hooks_override is None:
        return []

    result: list[str] = []
    for entry in hooks_override:
        if entry.startswith("+"):
            name = entry.removeprefix("+")
            if name and name not in result:
                result.append(name)
        elif entry.startswith("-"):
            name = entry.removeprefix("-")
            if name in result:
                result.remove(name)
        elif entry and entry not in result:
            result.append(entry)
    return result


def _expand_system_prompt(
    system_prompt: str | None,
    system_prompt_provider: str | None,
    system_prompt_provider_config: dict[str, Any],
    prompt_name: str | None,
    agent_name: str,
) -> tuple[str, dict[str, Any]]:
    """Expand the system-prompt roster surface (SPEC §5.6).

    Priority chain:
    1. Explicit ``system_prompt_provider`` factory name — wins over the
       ``system_prompt`` sugar and the naming convention; the provider's
       roster config is projected verbatim (the sugar is NOT applied).
    2. ``system_prompt`` sugar →
        ``system_prompt_provider: "file_prompt"``,
        ``system_prompt_config: {path: "agents/foo.md"}``
    3. ``prompt_name`` / agent-name convention → ``file_prompt`` with
       ``agents/<name>.md``.
    """
    if system_prompt_provider is not None:
        return system_prompt_provider, dict(system_prompt_provider_config)

    if system_prompt is not None:
        return _FILE_PROMPT_PROVIDER, {"path": system_prompt}

    # Fall back to prompt_name or agent-name convention
    name = prompt_name or agent_name
    return _FILE_PROMPT_PROVIDER, {"path": f"agents/{name}.md"}
