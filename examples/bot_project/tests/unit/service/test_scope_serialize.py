"""Canonical scope-declaration serializer — unit tests.

Covers ``bot.service.scope_serialize.serialize_scope_declaration``: the
strip-on-default rules (spec field defaults + position-derived defaults),
the pool-root terminal-face exception, roster prefix preservation, both
root forms, and load→serialize→load round-trip idempotence (including
against the shipped declaration — a property check, not a content pin).
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from bot.service.scope_serialize import serialize_scope_declaration

from modex_agent.scope.loader import load_scope_declaration

sys.path.insert(0, str(Path(__file__).parents[3]))

BOT_BASE = Path(__file__).resolve().parents[3]


def _round_trip(tmp_path: Path, yaml_text: str) -> tuple[str, str]:
    """Load → serialize → load → serialize; returns (first, second).

    Idempotence (``first == second``) is asserted by the caller; strict
    spec equality only holds when the input carries no absence-equivalent
    empty blocks (``capabilities: {}`` drops to ``None`` by design — the
    spec declares the two equivalent).
    """
    path = tmp_path / "bot.yml"
    path.write_text(yaml_text, encoding="utf-8")
    spec = load_scope_declaration(path)
    first = serialize_scope_declaration(spec)
    path.write_text(first, encoding="utf-8")
    return first, serialize_scope_declaration(load_scope_declaration(path))


def test_shipped_declaration_round_trip(tmp_path: Path) -> None:
    shipped = (BOT_BASE / "config" / "scopes" / "bot.yml").read_text(encoding="utf-8")
    first, second = _round_trip(tmp_path, shipped)
    assert first == second

    # The shipped declaration also satisfies strict spec equality (it
    # carries no absence-equivalent empty blocks).
    path = tmp_path / "bot.yml"
    assert load_scope_declaration(path) == load_scope_declaration(
        BOT_BASE / "config" / "scopes" / "bot.yml"
    )


def test_spec_defaults_are_stripped(tmp_path: Path) -> None:
    first, _ = _round_trip(
        tmp_path,
        "pool:\n"
        "  name: solo\n"
        "  agents:\n"
        "    root:\n"
        "      max_steps: 100\n"
        "      context_mode: fresh\n"
        "      agents:\n"
        "        sub:\n"
        "          max_steps: 100\n"
        "          context_mode: fresh\n",
    )
    assert "max_steps" not in first
    assert "context_mode" not in first


def test_deviations_are_kept(tmp_path: Path) -> None:
    first, _ = _round_trip(
        tmp_path,
        "pool:\n"
        "  name: solo\n"
        "  agents:\n"
        "    root:\n"
        "      max_steps: 50\n"
        "      agents:\n"
        "        sub:\n"
        "          context_mode: fork\n"
        "          fork_max_messages: 40\n"
        "          toolset: read_only\n",
    )
    assert "max_steps: 50" in first
    assert "context_mode: fork" in first
    assert "fork_max_messages: 40" in first
    assert "toolset: read_only" in first


def test_root_terminal_face_always_emitted(tmp_path: Path) -> None:
    first, _ = _round_trip(
        tmp_path,
        "pool:\n"
        "  name: solo\n"
        "  agents:\n"
        "    root:\n"
        "      agents:\n"
        "        sub:\n"
        "          description: child\n",
    )
    data = yaml.safe_load(first)
    root = data["pool"]["agents"]["root"]
    assert root["use_terminal"] is False
    assert root["terminal_visibility"] is False
    sub = root["agents"]["sub"]
    assert "use_terminal" not in sub
    assert "terminal_visibility" not in sub


def test_roster_prefixes_and_capability_vetoes_preserved(tmp_path: Path) -> None:
    first, _ = _round_trip(
        tmp_path,
        "pool:\n"
        "  name: solo\n"
        "  agents:\n"
        "    root:\n"
        "      hooks: [+extra_hook, -deliver_retry]\n"
        "      capabilities:\n"
        "        aci: {}\n"
        "        todo: false\n",
    )
    data = yaml.safe_load(first)
    root = data["pool"]["agents"]["root"]
    assert root["hooks"] == ["+extra_hook", "-deliver_retry"]
    assert root["capabilities"] == {"aci": {}, "todo": False}


def test_canonical_field_order(tmp_path: Path) -> None:
    first, _ = _round_trip(
        tmp_path,
        "pool:\n"
        "  name: solo\n"
        "  agents:\n"
        "    root:\n"
        "      approval:\n"
        "        enabled: true\n"
        "      mcp: [playwright]\n"
        "      description: out of order input\n"
        "      max_steps: 50\n"
        "      capabilities:\n"
        "        aci: {}\n",
    )
    data = yaml.safe_load(first)
    keys = list(data["pool"]["agents"]["root"])
    assert keys == [
        "description",
        "max_steps",
        "use_terminal",
        "terminal_visibility",
        "capabilities",
        "approval",
        "mcp",
    ]


def test_empty_capability_block_is_dropped(tmp_path: Path) -> None:
    first, _ = _round_trip(
        tmp_path,
        "pool:\n"
        "  name: solo\n"
        "  agents:\n"
        "    root:\n"
        "      capabilities: {}\n",
    )
    assert "capabilities" not in first


def test_workspace_form_with_resource_overrides(tmp_path: Path) -> None:
    first, _ = _round_trip(
        tmp_path,
        "workspace:\n"
        "  name: w\n"
        "  persistence:\n"
        "    backend: file\n"
        "  pools:\n"
        "    main:\n"
        "      peers: [helper]\n"
        "      agents:\n"
        "        main:\n"
        "          description: root\n"
        "    helper:\n"
        "      peers: [main]\n"
        "      agents:\n"
        "        helper:\n"
        "          description: peer root\n",
    )
    data = yaml.safe_load(first)
    assert data["workspace"]["persistence"] == {"backend": "file"}
    assert "paths" not in data["workspace"]
    assert data["workspace"]["pools"]["main"]["peers"] == ["helper"]


def test_workspace_default_paths_block_dropped(tmp_path: Path) -> None:
    """A paths block restating the service default is not re-emitted."""
    first, _ = _round_trip(
        tmp_path,
        "workspace:\n"
        "  name: w\n"
        "  paths:\n"
        "    data_dir_name: .modex\n"
        "  pools: {}\n",
    )
    data = yaml.safe_load(first)
    assert "paths" not in data["workspace"]
