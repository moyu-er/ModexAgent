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

        injected_env, injected_headers = self._extract_injected(data)

        merged_env = {**env, **injected_env}
        merged_headers = {**headers, **injected_headers}

        if injected_env or injected_headers:
            _logger.debug(
                "[MCP Injector] Applied runtime config (env=%d, headers=%d)",
                len(injected_env),
                len(injected_headers),
            )

        return merged_env, merged_headers

    @staticmethod
    def _extract_injected(data: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
        """Return (env, headers) to inject from the global config object."""
        unified: dict[str, str] = {}
        env_section: dict[str, str] = {}
        headers_section: dict[str, str] = {}

        for raw_key, value in data.items():
            key = str(raw_key).lower()
            if key == "env":
                env_section = JsonFileMCPTransportInjector._as_str_map(value)
            elif key == "headers":
                headers_section = JsonFileMCPTransportInjector._as_str_map(value)
            else:
                try:
                    unified[str(raw_key)] = str(value) if value is not None else ""
                except Exception:
                    continue

        injected_env = {**unified, **env_section}
        injected_headers = {**unified, **headers_section}
        return injected_env, injected_headers

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
