"""Sanitized environment builder for subprocess execution.

Builds caller-selected environment dictionaries, preserving OS-critical
entries. This utility is not a default filter on every shell path; native
HOST/LOCAL execution can inherit credentials. PASSTHROUGH and explicit
inheritance/overrides may include secrets.

The Windows PATH enrichment (registry merge) is injected as a callable
so this module has no reverse dependency on ``tools.terminal.env``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

# Builds a complete env dict (os.environ superset with PATH enrichment).
# Windows STANDARD policy consumes it to pick up registry PATH entries
# missing from os.environ.
FullEnvProvider = Callable[[], dict[str, str]]


class EnvPolicy(StrEnum):
    """How aggressively to sanitize the environment."""

    MINIMAL = "minimal"  # Essential vars only
    STANDARD = "standard"  # Essential + development vars
    PASSTHROUGH = "passthrough"  # Full os.environ copy


@dataclass
class EnvBuilderConfig:
    """Configuration for EnvironmentBuilder."""

    policy: EnvPolicy = EnvPolicy.STANDARD
    extra_vars: dict[str, str] = field(default_factory=dict)
    inherit_vars: list[str] = field(default_factory=list)


class EnvironmentBuilder:
    """Build sanitized environment dicts for subprocess execution.

    Resolution order for the final dict:

    1. Base set selected by policy + platform
    2. Specific vars inherited from ``os.environ`` (``inherit_vars``)
    3. Static ``extra_vars`` from config
    4. Per-call ``overrides`` (highest priority)
    """

    _UNIX_MINIMAL: frozenset[str] = frozenset(
        {
            "HOME",
            "LANG",
            "TERM",
            "PYTHONUNBUFFERED",
        }
    )

    _UNIX_STANDARD: frozenset[str] = _UNIX_MINIMAL | frozenset(
        {
            "PATH",
            "USER",
            "LC_ALL",
            "PYTHONIOENCODING",
        }
    )

    _WINDOWS_CRITICAL: frozenset[str] = frozenset(
        {
            "SYSTEMROOT",
            "COMSPEC",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "PATHEXT",
            "TEMP",
            "TMP",
            "PROGRAMFILES",
            "HOMEDRIVE",
            "HOMEPATH",
            "PATH",
        }
    )

    def __init__(
        self,
        config: EnvBuilderConfig | None = None,
        *,
        full_env_provider: FullEnvProvider | None = None,
    ) -> None:
        self._config = config or EnvBuilderConfig()
        self._is_windows = sys.platform == "win32"
        self._full_env_provider = full_env_provider

    def build(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        """Build sanitized environment dict.

        On Windows, the STANDARD policy reads from the injected full-env
        provider when present (e.g. the assembly layer injects
        :func:`modex_agent.tools.terminal.env.build_full_env` to pick up
        registry PATH entries missing from ``os.environ``). Without a
        provider, ``os.environ`` is the source; this module has no
        dependency on ``tools.terminal``.

        Args:
            overrides: Highest-priority key-value pairs that override
                everything else.

        Returns:
            A clean ``dict[str, str]`` suitable for ``subprocess.run(env=...)``.
        """
        # 1. Base set from policy.
        if self._config.policy == EnvPolicy.PASSTHROUGH:
            env: dict[str, str] = dict(os.environ)
        else:
            base_keys = self._select_base_keys()
            if self._is_windows and self._config.policy == EnvPolicy.STANDARD:
                source_env = (
                    self._full_env_provider() if self._full_env_provider is not None else os.environ
                )
                env = {key: value for key in base_keys if (value := source_env.get(key)) is not None}
            else:
                env = {
                    key: value for key in base_keys if (value := os.environ.get(key)) is not None
                }

        # 2. Inherit specific extra vars from os.environ.
        for key in self._config.inherit_vars:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value

        # 3. Merge extra_vars from config.
        env.update(self._config.extra_vars)

        # 4. Merge overrides (highest priority).
        if overrides:
            env.update(overrides)

        return env

    def _select_base_keys(self) -> frozenset[str]:
        """Return the set of environment variable names for the current
        policy and platform."""
        policy = self._config.policy

        if policy == EnvPolicy.MINIMAL:
            if self._is_windows:
                return self._WINDOWS_CRITICAL
            return self._UNIX_MINIMAL

        # STANDARD (or any unrecognised value falls through here).
        if self._is_windows:
            return self._WINDOWS_CRITICAL
        return self._UNIX_STANDARD
