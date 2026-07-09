"""MCP transport configuration injector.

Provides an extension point for modifying MCP transport configuration
(environment variables and HTTP headers) at connection time. This is useful
when values are not known at static configuration time and must be supplied
at runtime from a separate source.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from modex_agent.utils.file_io import read_json_robust

_logger = logging.getLogger(__name__)

# Default path for the JSON-backed injector.
# Uses a dot-prefixed file inside the bot/runtime data directory so it stays
# out of version control by default.
DEFAULT_MCP_INJECTOR_PATH: str = ".modex/mcp_inject.json"


class _JsonLoadResult:
    """Container for safe JSON load outcome."""

    def __init__(self, data: dict[str, Any] | None, error: Exception | None) -> None:
        self.data = data
        self.error = error


class MCPTransportInjector(ABC):
    """Abstract runtime injector for MCP transport configuration.

    Implementations decide how to augment the static ``env`` and ``headers``
    values declared in MCP server configuration before a client connection is
    established.
    """

    @abstractmethod
    def apply(
        self,
        server_name: str,
        transport: str,
        env: dict[str, str],
        headers: dict[str, str],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Return possibly-modified env and headers for ``server_name``.

        Args:
            server_name: Logical name of the MCP server being connected.
            transport: Canonical transport string (e.g. ``stdio``, ``sse``).
            env: Static environment variables from MCP configuration.
            headers: Static HTTP headers from MCP configuration.

        Returns:
            A tuple ``(env, headers)`` to use for the connection. Callers must
            not mutate the input dictionaries.
        """
        ...


class NullMCPTransportInjector(MCPTransportInjector):
    """No-op injector: returns the configuration unchanged."""

    def apply(
        self,
        server_name: str,
        transport: str,
        env: dict[str, str],
        headers: dict[str, str],
    ) -> tuple[dict[str, str], dict[str, str]]:
        return env, headers


class JsonFileMCPTransportInjector(MCPTransportInjector):
    """Inject env/headers from a global JSON file.

    The file format is a single JSON object at the root. It supports two
    shapes that can also be mixed:

    1. Explicit ``env`` / ``headers`` sections — each section is merged only
       into the corresponding transport field::

        {
          "env": {"API_KEY": "secret"},
          "headers": {"Authorization": "Bearer secret"}
        }

    2. Flat key-value map — the same pairs are used as a **shared base**
       for both ``env`` and ``headers``::

        {
          "API_KEY": "secret",
          "REGION": "us-east"
        }

    When both are present, flat keys form the base, then ``env`` / ``headers``
    are merged on top for their respective targets. Section keys are matched
    case-insensitively and normalized to lower-case.

    The root-level shapes above are **global**: they apply to EVERY server. To
    scope a value to a single server (so one server's secret is not propagated
    to the others), use the top-level ``servers`` map, keyed by server name,
    whose sections accept the same flat / ``env`` / ``headers`` shapes and
    **override** the global set for that server only::

        {
          "env": {"COMMON": "shared"},                       // every server
          "servers": {
            "alpha": {"env": {"ALPHA_KEY": "a-only"}},       // alpha only
            "beta":  {"headers": {"Authorization": "Bearer b"}} // beta only
          }
        }

    Here ``alpha`` receives ``COMMON`` + ``ALPHA_KEY`` and ``beta`` receives
    ``COMMON`` + its header; neither sees the other's secret. An unknown server
    name falls back to the global set alone.

    Missing files are treated as empty: no injection occurs and the original
    configuration is used. Corrupt or unreadable files are logged at error level
    with full traceback, then ignored so the main MCP flow continues.
    """

    def __init__(self, path: str | Path = DEFAULT_MCP_INJECTOR_PATH) -> None:
        self._path = Path(path)
        self._cache: dict[str, Any] | None = None
        self._load_error: Exception | None = None

    def apply(
        self,
        server_name: str,
        transport: str,
        env: dict[str, str],
        headers: dict[str, str],
    ) -> tuple[dict[str, str], dict[str, str]]:
        load_result = self._load()
        if load_result.error is not None:
            _logger.error(
                "[MCP Injector] Failed to load %s; continuing without injection",
                self._path,
                exc_info=True,
            )
            return env, headers

        data = load_result.data or {}
        if not isinstance(data, dict):
            _logger.warning(
                "[MCP Injector] %s root is not a JSON object; ignoring",
                self._path,
            )
            return env, headers

        injected_env, injected_headers = self._extract_injected(data, server_name)

        merged_env = {**env, **injected_env}
        merged_headers = {**headers, **injected_headers}

        if injected_env or injected_headers:
            _logger.debug(
                "[MCP Injector] Applied runtime config for %s (env=%d, headers=%d)",
                server_name,
                len(injected_env),
                len(injected_headers),
            )

        return merged_env, merged_headers

    @staticmethod
    def _section_pairs(
        section: dict[str, Any],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Extract ``(env, headers)`` from one flat + env/headers section.

        Flat keys form the base for both targets; explicit ``env`` / ``headers``
        sections merge on top for their respective target. The ``servers`` key is
        never treated as a flat pair — it is the per-server map, handled by
        :meth:`_extract_injected`. Section keys are matched case-insensitively.
        """
        unified: dict[str, str] = {}
        env_section: dict[str, str] = {}
        headers_section: dict[str, str] = {}

        for raw_key, value in section.items():
            key = str(raw_key).lower()
            if key == "env":
                env_section = JsonFileMCPTransportInjector._as_str_map(value)
            elif key == "headers":
                headers_section = JsonFileMCPTransportInjector._as_str_map(value)
            elif key == "servers":
                continue
            else:
                try:
                    unified[str(raw_key)] = str(value) if value is not None else ""
                except Exception:
                    continue

        return {**unified, **env_section}, {**unified, **headers_section}

    @staticmethod
    def _extract_injected(
        data: dict[str, Any], server_name: str
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Return ``(env, headers)`` to inject for ``server_name``.

        Root-level flat / ``env`` / ``headers`` pairs are **global** (apply to
        every server — backward compatible). A top-level ``servers`` map scopes
        pairs to a single server and **overrides** the global set for that
        server only, so a secret intended for one server does not propagate to
        the others. An unknown server name falls back to the global set.
        """
        global_env, global_headers = JsonFileMCPTransportInjector._section_pairs(data)

        servers_map: dict[str, Any] | None = None
        for raw_key, value in data.items():
            if str(raw_key).lower() == "servers" and isinstance(value, dict):
                servers_map = value
                break

        if servers_map is not None:
            section = servers_map.get(server_name)
            if isinstance(section, dict):
                per_env, per_headers = JsonFileMCPTransportInjector._section_pairs(section)
                return {**global_env, **per_env}, {**global_headers, **per_headers}

        return global_env, global_headers

    def _load(self) -> _JsonLoadResult:
        if self._cache is not None:
            return _JsonLoadResult(self._cache, None)
        if self._load_error is not None:
            return _JsonLoadResult(None, self._load_error)

        try:
            data = read_json_robust(self._path)
        except Exception as exc:
            self._load_error = exc
            return _JsonLoadResult(None, exc)

        if data is None:
            self._cache = {}
            return _JsonLoadResult(self._cache, None)
        if not isinstance(data, dict):
            _logger.warning(
                "[MCP Injector] %s root is not a JSON object; ignoring",
                self._path,
            )
            self._cache = {}
            return _JsonLoadResult(self._cache, None)

        self._cache = data
        return _JsonLoadResult(self._cache, None)

    @staticmethod
    def _as_str_map(value: object) -> dict[str, str]:
        """Normalize an injected value to a ``dict[str, str]``.

        Non-dict values are ignored. Non-string values are coerced to string.
        """
        if not isinstance(value, dict):
            return {}
        result: dict[str, str] = {}
        for key, val in value.items():
            try:
                result[str(key)] = str(val) if val is not None else ""
            except Exception:
                continue
        return result
