from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from modex_agent.sandbox.env_builder import EnvBuilderConfig, EnvironmentBuilder, EnvPolicy


class TestEnvironmentBuilderUnixMinimal:
    """MINIMAL policy on Unix: only 4 essential vars."""

    @pytest.fixture
    def builder(self) -> EnvironmentBuilder:
        config = EnvBuilderConfig(policy=EnvPolicy.MINIMAL)
        return EnvironmentBuilder(config)

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-only test")
    def test_minimal_contains_home(self, builder: EnvironmentBuilder) -> None:
        env = builder.build()
        assert "HOME" in env

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-only test")
    def test_minimal_contains_lang(self, builder: EnvironmentBuilder) -> None:
        env = builder.build()
        assert "LANG" in env

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-only test")
    def test_minimal_excludes_path(self, builder: EnvironmentBuilder) -> None:
        env = builder.build()
        assert "PATH" not in env


class TestEnvironmentBuilderUnixStandard:
    """STANDARD policy on Unix: minimal + development vars."""

    @pytest.fixture
    def builder(self) -> EnvironmentBuilder:
        return EnvironmentBuilder()  # default is STANDARD

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-only test")
    def test_standard_contains_path(self, builder: EnvironmentBuilder) -> None:
        env = builder.build()
        assert "PATH" in env

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-only test")
    def test_standard_contains_home(self, builder: EnvironmentBuilder) -> None:
        env = builder.build()
        assert "HOME" in env


class TestEnvironmentBuilderWindows:
    """Windows: must include critical system vars."""

    @pytest.fixture
    def builder(self) -> EnvironmentBuilder:
        return EnvironmentBuilder()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_windows_contains_systemroot(self, builder: EnvironmentBuilder) -> None:
        env = builder.build()
        assert "SYSTEMROOT" in env

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_windows_contains_comspec(self, builder: EnvironmentBuilder) -> None:
        env = builder.build()
        assert "COMSPEC" in env

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_windows_contains_path(self, builder: EnvironmentBuilder) -> None:
        env = builder.build()
        assert "PATH" in env


class TestEnvironmentBuilderPassthrough:
    """PASSTHROUGH: full os.environ."""

    def test_passthrough_has_all_env(self) -> None:
        config = EnvBuilderConfig(policy=EnvPolicy.PASSTHROUGH)
        builder = EnvironmentBuilder(config)
        env = builder.build()
        # Should contain at least the keys from os.environ
        for key in os.environ:
            assert key in env


class TestEnvironmentBuilderOverrides:
    """Override layers work correctly."""

    def test_overrides_take_priority(self) -> None:
        builder = EnvironmentBuilder()
        env = builder.build(overrides={"CUSTOM_VAR": "custom_value"})
        assert env["CUSTOM_VAR"] == "custom_value"

    def test_extra_vars_merged(self) -> None:
        config = EnvBuilderConfig(extra_vars={"MY_TOOL": "1.0"})
        builder = EnvironmentBuilder(config)
        env = builder.build()
        assert env["MY_TOOL"] == "1.0"

    def test_inherit_vars(self) -> None:
        # Inherit a var from os.environ that's not in the default set
        os_key = list(os.environ.keys())[0]  # pick any existing key
        config = EnvBuilderConfig(
            policy=EnvPolicy.MINIMAL,
            inherit_vars=[os_key],
        )
        builder = EnvironmentBuilder(config)
        env = builder.build()
        assert os_key in env

    def test_override_overrides_existing(self) -> None:
        builder = EnvironmentBuilder()
        env = builder.build(overrides={"HOME": "/custom/home"})
        assert env["HOME"] == "/custom/home"


win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behavior")


class TestFullEnvProviderInjection:
    """Windows STANDARD PATH enrichment is constructor-injected — no
    reverse dependency on tools.terminal.env (sandbox ticket 01)."""

    @win_only
    def test_injected_provider_used_on_windows_standard(self) -> None:
        calls: list[int] = []

        def provider() -> dict[str, str]:
            calls.append(1)
            return {"PATH": "C:\\injected", "SYSTEMROOT": "C:\\Windows"}

        builder = EnvironmentBuilder(
            EnvBuilderConfig(policy=EnvPolicy.STANDARD), full_env_provider=provider
        )
        env = builder.build()
        assert calls == [1]
        assert env["PATH"] == "C:\\injected"
        assert env["SYSTEMROOT"] == "C:\\Windows"

    @win_only
    def test_default_uses_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "C:\\from-os-environ")
        builder = EnvironmentBuilder(EnvBuilderConfig(policy=EnvPolicy.STANDARD))
        env = builder.build()
        assert env["PATH"] == "C:\\from-os-environ"

    @win_only
    def test_provider_not_used_for_minimal_policy(self) -> None:
        calls: list[int] = []

        def provider() -> dict[str, str]:
            calls.append(1)
            return {"PATH": "C:\\injected", "TEMP": "C:\\t"}

        builder = EnvironmentBuilder(
            EnvBuilderConfig(policy=EnvPolicy.MINIMAL), full_env_provider=provider
        )
        builder.build()
        assert calls == []

    @win_only
    def test_provider_not_used_for_passthrough(self) -> None:
        calls: list[int] = []

        def provider() -> dict[str, str]:
            calls.append(1)
            return {"PATH": "C:\\injected"}

        builder = EnvironmentBuilder(
            EnvBuilderConfig(policy=EnvPolicy.PASSTHROUGH), full_env_provider=provider
        )
        builder.build()
        assert calls == []


class TestModuleHasNoTerminalDependency:
    def test_env_builder_does_not_import_terminal(self) -> None:
        """No runtime import of tools.terminal from the sandbox package
        (the docstring may name it, code must not import it)."""
        import ast

        import modex_agent.sandbox.env_builder as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "tools.terminal" in node.module:
                raise AssertionError(f"reverse dependency on {node.module}")
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "modex_agent.tools.terminal.env":
                raise AssertionError("reverse dependency on tools.terminal.env")
