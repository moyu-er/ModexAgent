"""MCP server registry — single source of truth for all MCP server definitions.

Agent configs reference servers by NAME (``mcp: ["playwright", "fetch"]``);
this module resolves those names against ``config/mcp/registry.json``
(a Claude-style ``{"mcpServers": {...}}`` file) and returns the raw server
configs (with ``${ENV}`` interpolation applied by the caller).

Per-agent JSON files (``config/mcp/<agent>.json``) are gone — every server
lives in the registry, and agents select by name.

Phase 2A adds write operations (``write_registry``, ``upsert_server``,
``delete_server``) for the MCP CRUD API. ``${ENV}`` interpolation stays OUT
of the store — the registry persists raw values with ``${ENV}``
placeholders, and interpolation happens at load in the existing loader
(preserved unchanged). Agents reference servers by name from the scope
declaration (ticket 11 — the pool.yml reference scan is gone; deletion is
unconditional and stale declaration references surface at boot's MCP-set
validation).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from modex_agent.ioc.configs.mcp import MCPServerEntry

REGISTRY_PATH = Path("config/mcp/registry.json")

_logger = logging.getLogger(__name__)


class UnknownMcpServer(KeyError):  # noqa: N818 — stable pre-existing name
    """Raised when an agent selects an MCP server not present in the registry."""


def read_registry(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Read and return the ``mcpServers`` mapping from the registry file.

    Returns ``{}`` when the file is absent. Accepts either ``mcpServers``
    or ``servers`` as the top-level key (Claude-style + compact alias).
    """
    p = path or REGISTRY_PATH
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("mcpServers") or data.get("servers") or {}


def read_shared_registry_flag(path: Path | None = None) -> bool:
    """Read the top-level ``sharedRegistry`` boolean (ADR-0017 Task 5a).

    Returns ``True`` (opt INTO the shared MCP connection registry) unless the
    file explicitly sets ``sharedRegistry: false``. The registry is an
    optimization, so this FAILS OPEN on every degenerate input — missing file,
    absent key, non-dict root, malformed JSON — so a corrupted/absent config
    can never break MCP; the bot simply falls back to today's per-pool
    ``MCPClientManager`` path.

    The flag's policy is local to MCP config (architecture rule 7, locality):
    it lives next to ``mcpServers`` rather than in the app-level config.
    """
    p = path or REGISTRY_PATH
    if not p.exists():
        return True
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _logger.warning(
            "sharedRegistry: failed to parse %s (%s); defaulting to True", p, exc
        )
        return True
    if not isinstance(data, dict):
        return True
    return bool(data.get("sharedRegistry", True))


def resolve_agent_mcp_servers(
    selection: list[str],
    path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve an agent's MCP server selection against the registry.

    Raises :class:`UnknownMcpServer` listing any names absent from the
    registry. Returns a name→config mapping preserving the selection order.
    """
    registry = read_registry(path)
    missing = [s for s in selection if s not in registry]
    if missing:
        raise UnknownMcpServer(f"MCP servers not in registry: {missing}")
    return {name: registry[name] for name in selection}


# ─── Phase 2A: write operations ──────────────────────────────────────────────


def _coerce_to_entry(entry: MCPServerEntry | dict[str, Any]) -> MCPServerEntry:
    """Accept either a typed ``MCPServerEntry`` or a raw dict at the boundary."""
    if isinstance(entry, MCPServerEntry):
        return entry
    return MCPServerEntry.model_validate(dict(entry))


def _entry_to_registry_dict(entry: MCPServerEntry) -> dict[str, Any]:
    """Serialize an entry for the registry file.

    Uses ``by_alias=True`` so the transport discriminator is written as the
    on-disk ``type`` key (matching registry.json); ``env`` is written as
    ``env`` (the framework model accepts ``environment`` on input via its
    ``_normalize_input`` validator but serializes the field name). Drops
    ``None`` and empty-default values to keep the file compact and
    idempotent across round-trips.
    """
    dumped = entry.model_dump(by_alias=True, exclude_none=True)
    # Drop empty containers that are just defaults — keeps the file compact
    # and matches the existing registry.json style (entries omit empty lists).
    return {k: v for k, v in dumped.items() if v not in ([], {})}


def write_registry(
    servers: dict[str, MCPServerEntry | dict[str, Any]],
    path: Path | None = None,
) -> None:
    """Atomically write the full ``{"mcpServers": {...}}`` registry.

    Coerces each value to :class:`MCPServerEntry` for validation, then
    serializes via ``model_dump(by_alias=True, exclude_none=True)``. Writes
    a ``.tmp`` file then ``os.replace`` so a crash mid-write cannot corrupt
    the registry.
    """
    p = path or REGISTRY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, dict[str, Any]] = {}
    for name, entry in servers.items():
        coerced = _coerce_to_entry(entry)
        payload[name] = _entry_to_registry_dict(coerced)
    text = json.dumps({"mcpServers": payload}, indent=2, ensure_ascii=False)
    _atomic_write(p, text)


def upsert_server(
    name: str,
    entry: MCPServerEntry | dict[str, Any],
    path: Path | None = None,
) -> None:
    """Insert or update a single server in the registry (atomic).

    Preserves all other entries and key ordering (Python dicts preserve
    insertion order; an update keeps the existing position, an insert
    appends).
    """
    registry = read_registry(path)
    registry[name] = _entry_to_registry_dict(_coerce_to_entry(entry))
    p = path or REGISTRY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"mcpServers": registry}, indent=2, ensure_ascii=False)
    _atomic_write(p, text)


def delete_server(name: str, path: Path | None = None) -> bool:
    """Remove a server from the registry. Returns ``True`` if it was present.

    Does NOT refuse deletion when referenced by a declaration — a stale
    reference surfaces loudly at the next boot's workspace MCP-set
    validation. Atomic (``.tmp`` + replace).
    """
    registry = read_registry(path)
    if name not in registry:
        return False
    del registry[name]
    p = path or REGISTRY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"mcpServers": registry}, indent=2, ensure_ascii=False)
    _atomic_write(p, text)
    return True



def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (``.tmp`` + ``os.replace``)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

