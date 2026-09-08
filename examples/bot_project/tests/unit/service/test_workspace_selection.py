"""Workspace resource-selection helpers (ticket 14) — unit tests.

Covers the declaration-boot helpers in ``bot.service.pool.declaration``:
the config-view override resolution (继承父层 + 声明差异), the single
stack-shape mechanism (N15), the loud agent-level MCP-set validation, and
``declared_pool_root`` over both root forms.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bot.config.mcp_registry import UnknownMcpServer
from bot.service.pool.declaration import (
    apply_workspace_resource_selection,
    declared_pool_root,
    load_scope_declaration_opt,
    validate_agent_mcp_sets,
    workspace_layer_present,
)

from modex_agent.ioc.configs.app import AppConfig
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.scope.spec import ScopeKind


def _component_registry() -> ComponentRegistry:
    """DefaultPlugin registry — the tree derivation is capability-contributed
    (the subagents migration); the boot compile resolves it against this."""
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    return registry


def _write_declaration(tmp_path: Path, body: str) -> Path:
    scopes = tmp_path / "config" / "scopes"
    scopes.mkdir(parents=True)
    path = scopes / "bot.yml"
    path.write_text(body, encoding="utf-8")
    return path


class TestApplyWorkspaceResourceSelection:
    def test_declared_backend_overrides_service_config(self, tmp_path: Path) -> None:
        path = _write_declaration(
            tmp_path,
            "workspace:\n"
            "  name: w\n"
            "  persistence:\n"
            "    backend: file\n"
            "  pools: {}\n",
        )
        spec = load_scope_declaration_opt(path)
        app_config = AppConfig.model_validate(
            {"persistence": {"backend": "sqlite"}}
        )

        resolved = apply_workspace_resource_selection(app_config, spec)

        assert resolved is not app_config
        assert resolved.persistence.backend is PersistenceBackend.FILE
        # The service-level view is untouched (no hidden mutation).
        assert app_config.persistence.backend is PersistenceBackend.SQLITE

    def test_declared_paths_override_service_config(self, tmp_path: Path) -> None:
        path = _write_declaration(
            tmp_path,
            "workspace:\n  name: w\n  paths:\n    data_dir_name: .custom\n  pools: {}\n",
        )
        spec = load_scope_declaration_opt(path)
        app_config = AppConfig.model_validate({})

        resolved = apply_workspace_resource_selection(app_config, spec)

        assert resolved.paths.data_dir_name == ".custom"
        assert app_config.paths.data_dir_name == ".modex"

    def test_absent_fields_inherit_service_config(self, tmp_path: Path) -> None:
        path = _write_declaration(
            tmp_path, "workspace:\n  name: w\n  pools: {}\n"
        )
        spec = load_scope_declaration_opt(path)
        app_config = AppConfig.model_validate(
            {"persistence": {"backend": "file"}, "paths": {"data_dir_name": ".keep"}}
        )

        resolved = apply_workspace_resource_selection(app_config, spec)

        assert resolved is app_config
        assert resolved.persistence.backend is PersistenceBackend.FILE
        assert resolved.paths.data_dir_name == ".keep"

    def test_pool_as_root_and_absent_declarations_change_nothing(
        self, tmp_path: Path
    ) -> None:
        pool_root = load_scope_declaration(
            _write_declaration(
                tmp_path, "pool:\n  name: solo\n  agents:\n    solo: {}\n"
            )
        )
        app_config = AppConfig.model_validate({})
        assert apply_workspace_resource_selection(app_config, pool_root) is app_config
        assert apply_workspace_resource_selection(app_config, None) is app_config

    def test_matching_declaration_returns_same_view(self, tmp_path: Path) -> None:
        path = _write_declaration(
            tmp_path,
            "workspace:\n"
            "  name: w\n"
            "  persistence:\n"
            "    backend: sqlite\n"
            "  paths:\n"
            "    data_dir_name: .modex\n"
            "  pools: {}\n",
        )
        spec = load_scope_declaration_opt(path)
        app_config = AppConfig.model_validate({})
        assert apply_workspace_resource_selection(app_config, spec) is app_config


class TestStackShapeMechanism:
    def test_workspace_layer_selects_multi_live(self, tmp_path: Path) -> None:
        path = _write_declaration(
            tmp_path, "workspace:\n  name: w\n  pools: {}\n"
        )
        assert workspace_layer_present(load_scope_declaration_opt(path)) is True

    def test_pool_as_root_boots_single_home(self, tmp_path: Path) -> None:
        path = _write_declaration(
            tmp_path, "pool:\n  name: solo\n  agents:\n    solo: {}\n"
        )
        assert workspace_layer_present(load_scope_declaration_opt(path)) is False

    def test_absent_declaration_boots_single_home(self, tmp_path: Path) -> None:
        assert workspace_layer_present(None) is False
        assert load_scope_declaration_opt(tmp_path / "config" / "scopes" / "bot.yml") is None


class TestAgentMcpSetValidation:
    def test_unknown_name_fails_loud(self, tmp_path: Path) -> None:
        path = _write_declaration(
            tmp_path,
            "workspace:\n"
            "  name: w\n"
            "  pools:\n"
            "    main:\n"
            "      agents:\n"
            "        main:\n"
            "          mcp: [playwright, typo-server]\n",
        )
        spec = load_scope_declaration_opt(path)

        with pytest.raises(UnknownMcpServer, match="typo-server"):
            validate_agent_mcp_sets(spec, {"playwright"})

    def test_known_names_pass(self, tmp_path: Path) -> None:
        path = _write_declaration(
            tmp_path,
            "workspace:\n"
            "  name: w\n"
            "  pools:\n"
            "    main:\n"
            "      agents:\n"
            "        main:\n"
            "          mcp: [playwright]\n",
        )
        spec = load_scope_declaration_opt(path)
        validate_agent_mcp_sets(spec, {"playwright", "other"})

    def test_no_agent_selections_skips_validation(self, tmp_path: Path) -> None:
        path = _write_declaration(
            tmp_path, "workspace:\n  name: w\n  pools: {}\n"
        )
        spec = load_scope_declaration_opt(path)
        validate_agent_mcp_sets(spec, {})

    def test_pool_as_root_form_validates(self, tmp_path: Path) -> None:
        path = _write_declaration(
            tmp_path,
            "pool:\n"
            "  name: solo\n"
            "  agents:\n"
            "    solo:\n"
            "      mcp: [typo-server]\n",
        )
        spec = load_scope_declaration_opt(path)

        with pytest.raises(UnknownMcpServer, match="typo-server"):
            validate_agent_mcp_sets(spec, {"playwright"})

    def test_empty_registry_is_degenerate_not_a_typo(self, tmp_path: Path) -> None:
        path = _write_declaration(
            tmp_path,
            "workspace:\n"
            "  name: w\n"
            "  pools:\n"
            "    main:\n"
            "      agents:\n"
            "        main:\n"
            "          mcp: [playwright]\n",
        )
        spec = load_scope_declaration_opt(path)
        # No MCP registry configured at all — warning, not boot failure.
        validate_agent_mcp_sets(spec, {})

    def test_absent_declaration_skips_validation(self) -> None:
        validate_agent_mcp_sets(None, {"playwright"})


class TestDeclaredPoolRoot:
    def test_workspace_form_root_resolves(self, tmp_path: Path) -> None:
        from bot.service.pool.declaration import boot_scope_declaration

        path = _write_declaration(
            tmp_path,
            "workspace:\n"
            "  name: w\n"
            "  pools:\n"
            "    main:\n"
            "      agents:\n"
            "        main:\n"
            "          description: root\n"
            "          agents:\n"
            "            helper:\n"
            "              description: child\n",
        )
        spec = load_scope_declaration(path)
        boot = boot_scope_declaration(
            declaration_path=path,
            project_dir=tmp_path,
            data_dir=tmp_path / ".modex",
            graphs_dirs=(),
            default_llm_provider="bot_default",
            registry=_component_registry(),
        )

        root = declared_pool_root(boot, "main")
        assert root is not None
        assert root.provenance.agent == "main"
        assert declared_pool_root(boot, "undeclared") is None
        assert workspace_layer_present(spec) is True
        assert spec.kind is ScopeKind.WORKSPACE

    def test_pool_as_root_form_root_resolves(self, tmp_path: Path) -> None:
        from bot.service.pool.declaration import boot_scope_declaration

        path = _write_declaration(
            tmp_path,
            "pool:\n"
            "  name: solo\n"
            "  agents:\n"
            "    solo:\n"
            "      description: standalone root\n"
            "      agents:\n"
            "        helper:\n"
            "          description: child\n",
        )
        boot = boot_scope_declaration(
            declaration_path=path,
            project_dir=tmp_path,
            data_dir=tmp_path / ".modex",
            graphs_dirs=(),
            default_llm_provider="bot_default",
            registry=_component_registry(),
        )

        root = declared_pool_root(boot, "solo")
        assert root is not None
        assert root.provenance.agent == "solo"
        assert declared_pool_root(boot, "other") is None
