"""Wire-event classes stay in their wire homes (record-vocabulary guard).

The transcript record cutover replaced the persisted ServerEvent
generation with the typed ``TranscriptRecord`` union. The regression
this guard exists for: a non-wire module imported a retired wire class
and matched it over loaded records — silently reading nothing for
every newly written session (the session-title reader). Wire classes
are projection-only; record consumers use the record vocabulary.

Rule: production bot code may import from ``bot.webui.events`` only
the envelope/meta names (``SessionMeta``, ``DeltaEnvelope``,
``WebUIEventType``, ``WebSocketAction``), unless the file is one of the
wire homes (the WS projection emitter, the legacy read adapter, the
inbound echo route, or the module itself).
"""

from __future__ import annotations

import ast
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parents[2] / "examples" / "bot_project" / "bot"

_EVENTS_MODULE_PARTS = ("bot", "webui", "events")

# Envelope/meta names importable from anywhere (not record-shaped).
_ALWAYS_ALLOWED = frozenset(
    {"SessionMeta", "DeltaEnvelope", "WebUIEventType", "WebSocketAction"}
)

# Files allowed to import ANY name from bot.webui.events.
_WIRE_HOMES = frozenset(
    {
        "webui/emitter/web_bot.py",  # WS wire projection
        "webui/transcript_store.py",  # legacy read adapter (ADR-recorded)
        "webui/routes/websocket/messaging.py",  # inbound WS echo
        "webui/events.py",  # the module itself
    }
)


def _iter_bot_files() -> list[Path]:
    return sorted(p for p in _BOT_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _resolved_module(node: ast.ImportFrom, file: Path) -> tuple[str, ...] | None:
    """Resolve an ImportFrom target to dotted parts, or None if unrelated."""
    repo_relative = file.relative_to(_BOT_DIR.parent).with_suffix("")
    package_parts = repo_relative.parts[:-1]  # bot/…/x.py -> its package
    if node.level == 0:
        if not node.module:
            return None
        return tuple(node.module.split("."))
    base = package_parts[: len(package_parts) - (node.level - 1)]
    if node.module:
        return (*base, *node.module.split("."))
    return tuple(base)


def _wire_class_imports() -> list[tuple[str, str]]:
    """(file, name) pairs importing non-envelope names from bot.webui.events."""
    offenders: list[tuple[str, str]] = []
    for file in _iter_bot_files():
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        rel = file.relative_to(_BOT_DIR).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if _resolved_module(node, file) != _EVENTS_MODULE_PARTS:
                continue
            if rel in _WIRE_HOMES:
                continue
            for alias in node.names:
                if alias.name not in _ALWAYS_ALLOWED and alias.name != "*":
                    offenders.append((rel, alias.name))
    return offenders


def test_wire_classes_are_imported_only_by_wire_homes() -> None:
    offenders = _wire_class_imports()
    assert not offenders, (
        "Wire-event classes are projection-only. Record consumers must use "
        "the TranscriptRecord vocabulary (bot.webui.transcript_store — "
        "earliest_user_content / materialize_records / adapt_legacy_records), "
        f"not wire classes. Offending imports: {offenders}"
    )
