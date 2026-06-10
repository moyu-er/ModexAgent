"""Sanitized environment builder for subprocess execution.

Builds minimal environment dicts that exclude secrets and irrelevant
variables while preserving OS-critical entries needed for correct
subprocess behaviour.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from enum import Enum


class EnvPolicy(str, Enum):
    """How aggressively to sanitize the environment."""

    MINIMAL = "minimal"          # Essential vars only
    STANDARD = "standard"        # Essential + development vars
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

    _UNIX_MINIMAL: frozenset[str] = frozenset({
        "HOME", "LANG", "TERM", "PYTHONUNBUFFERED",
    })

    _UNIX_STANDARD: frozenset[str] = _UNIX_MINIMAL | frozenset({
        "PATH", "USER", "LC_ALL", "PYTHONIOENCODING",
    })

    _WINDOWS_CRITICAL: frozenset[str] = frozenset({
        "SYSTEMROOT", "COMSPEC", "USERPROFILE", "APPDATA",
        "LOCALAPPDATA", "PATHEXT", "TEMP", "TMP",
        "PROGRAMFILES", "HOMEDRIVE", "HOMEPATH", "PATH",
    })

    def __init__(self, config: EnvBuilderConfig | None = None) -> None:
        self._config = config or EnvBuilderConfig()
        self._is_windows = sys.platform == "win32"

    def build(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        """Build sanitized environment dict.

        On Windows, the STANDARD policy uses ``build_full_env()`` from the
        terminal module so that the ``PATH`` includes registry entries that
        may be missing from ``os.environ``.

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
                # On Windows, build_full_env() merges registry PATH entries
                # that may be missing from os.environ (e.g. when launched
                # from an IDE).
                from framework.tools.terminal.env import build_full_env
                full_env = build_full_env()
                env = {
                    key: value
                    for key in base_keys
                    if (value := full_env.get(key)) is not None
                }
            else:
                env = {
                    key: value
                    for key in base_keys
                    if (value := os.environ.get(key)) is not None
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
