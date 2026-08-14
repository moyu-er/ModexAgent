"""Tests for the bot project's YAML GraphSpec loader."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_BOT_PROJECT = Path(__file__).resolve().parents[1]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.graph.spec_loader import GraphSpecLoader  # noqa: E402

from modex_graph.spec_store import InMemoryGraphSpecStore  # noqa: E402


def _write_spec(path: Path, *, name: str) -> None:
    path.write_text(
        "\n".join(
            (
                f"name: {name}",
                'version: "1.0"',
                "state_class: default",
                "nodes: []",
                "edges:",
                "  - source: __start__",
                "    target: __end__",
            )
        ),
        encoding="utf-8",
    )


def test_load_from_dir_saves_valid_yml_specs(tmp_path: Path) -> None:
    _write_spec(tmp_path / "first.yml", name="first")
    _write_spec(tmp_path / "second.yml", name="second")
    (tmp_path / "ignored.yaml").write_text("not: scanned", encoding="utf-8")
    store = InMemoryGraphSpecStore()

    loaded = GraphSpecLoader(store).load_from_dir(tmp_path)

    assert [spec.name for spec in loaded] == ["first", "second"]
    assert {spec.name for spec in store.list_all()} == {"first", "second"}


def test_load_from_dir_idempotent_on_reload(tmp_path: Path) -> None:
    """save_if_changed: reloading unchanged YAML does not create new specs."""
    _write_spec(tmp_path / "first.yml", name="first")
    _write_spec(tmp_path / "second.yml", name="second")
    store = InMemoryGraphSpecStore()

    GraphSpecLoader(store).load_from_dir(tmp_path)
    first_ids = {r.name: r.spec_id for r in store.list_records()}
    assert len(first_ids) == 2

    GraphSpecLoader(store).load_from_dir(tmp_path)
    second_ids = {r.name: r.spec_id for r in store.list_records()}

    assert second_ids == first_ids
    assert len(store.list_all()) == 2


def test_load_from_dir_creates_new_spec_on_yaml_change(tmp_path: Path) -> None:
    """save_if_changed: changed YAML creates a new spec_id; old one preserved."""
    _write_spec(tmp_path / "g.yml", name="g")
    store = InMemoryGraphSpecStore()

    GraphSpecLoader(store).load_from_dir(tmp_path)
    first_ids = {r.name: r.spec_id for r in store.list_records()}
    assert len(store.list_all()) == 1

    (tmp_path / "g.yml").write_text(
        "\n".join(
            (
                "name: g",
                'version: "2.0"',
                "state_class: default",
                "nodes: []",
                "edges:",
                "  - source: __start__",
                "    target: __end__",
            )
        ),
        encoding="utf-8",
    )
    GraphSpecLoader(store).load_from_dir(tmp_path)

    assert len(store.list_all()) == 2
    records = store.list_records()
    assert len(records) == 1
    assert records[0].spec_id != first_ids["g"]
    assert records[0].version == "2.0"
    old_spec = store.load_by_id(first_ids["g"])
    assert old_spec is not None
    assert old_spec.version == "1.0"


def test_load_from_dir_warns_and_skips_invalid_files(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / "invalid.yml").write_text("nodes: [", encoding="utf-8")
    store = InMemoryGraphSpecStore()

    with caplog.at_level(logging.WARNING):
        loaded = GraphSpecLoader(store).load_from_dir(tmp_path)

    assert loaded == []
    assert store.list_all() == []
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
