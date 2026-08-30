"""Tests for the FW ``GraphSpecLoader`` (migrated from BIZ).

Verifies that the FW loader handles:
- YAML parsing + ``GraphSpec`` construction from ``*.yml`` files.
- Content-deduplicated persistence (``save_if_changed``).
- Invalid file skipping (YAML/ValidationError/TopologyError).
- Stale spec cleanup (specs in store but no YAML on disk).
- Loading real graph YAML files from ``examples/bot_project/config/graphs/``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from modex_agent.graph.spec_loader import GraphSpecLoader
from modex_graph import InMemoryGraphSpecStore

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BIZ_GRAPHS_DIR = _REPO_ROOT / "examples" / "bot_project" / "config" / "graphs"


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


class TestGraphSpecLoaderLoadFromDir:
    def test_loads_valid_yml_specs(self, tmp_path: Path) -> None:
        _write_spec(tmp_path / "first.yml", name="first")
        _write_spec(tmp_path / "second.yml", name="second")
        (tmp_path / "ignored.yaml").write_text("not: scanned", encoding="utf-8")
        store = InMemoryGraphSpecStore()

        loaded = GraphSpecLoader(store).load_from_dir(tmp_path)

        assert [spec.name for spec in loaded] == ["first", "second"]
        assert {spec.name for spec in store.list_all()} == {"first", "second"}

    def test_idempotent_on_reload(self, tmp_path: Path) -> None:
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

    def test_creates_new_spec_on_yaml_change(self, tmp_path: Path) -> None:
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

    def test_warns_and_skips_invalid_files(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "invalid.yml").write_text("nodes: [", encoding="utf-8")
        store = InMemoryGraphSpecStore()

        with caplog.at_level(logging.WARNING):
            loaded = GraphSpecLoader(store).load_from_dir(tmp_path)

        assert loaded == []
        assert store.list_all() == []
        assert [record.levelno for record in caplog.records] == [logging.WARNING]

    def test_removes_stale_specs_not_on_disk(self, tmp_path: Path) -> None:
        _write_spec(tmp_path / "keep.yml", name="keep")
        _write_spec(tmp_path / "remove.yml", name="remove")
        store = InMemoryGraphSpecStore()

        GraphSpecLoader(store).load_from_dir(tmp_path)
        assert {r.name for r in store.list_records()} == {"keep", "remove"}

        (tmp_path / "remove.yml").unlink()
        GraphSpecLoader(store).load_from_dir(tmp_path)

        assert {r.name for r in store.list_records()} == {"keep"}


class TestGraphSpecLoaderRealYAML:
    def test_loads_biz_graph_yamls(self) -> None:
        if not _BIZ_GRAPHS_DIR.is_dir():
            pytest.skip(f"BIZ graphs dir not found: {_BIZ_GRAPHS_DIR}")
        yml_files = list(_BIZ_GRAPHS_DIR.glob("*.yml"))
        if not yml_files:
            pytest.skip("No .yml files in BIZ graphs dir")

        store = InMemoryGraphSpecStore()
        loaded = GraphSpecLoader(store).load_from_dir(_BIZ_GRAPHS_DIR)

        assert len(loaded) == len(yml_files)
        assert {spec.name for spec in loaded} == {
            f.stem for f in yml_files
        }
        assert len(store.list_all()) == len(yml_files)
