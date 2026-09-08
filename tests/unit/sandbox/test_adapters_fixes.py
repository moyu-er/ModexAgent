"""Regression tests for dormant-adapter bug fixes (sandbox ticket 01).

Each test pins one fix: subprocess exception type, docker argv command,
e2b os.environ handling. Docker/E2B SDKs are absent in CI — the tests
exercise the seams without the real services.
"""

from __future__ import annotations

import pytest

from modex_agent.sandbox.adapters.docker import DockerSandbox
from modex_agent.sandbox.adapters.e2b import E2BSandbox
from modex_agent.sandbox.adapters.subprocess import SubprocessSandbox
from modex_agent.sandbox.config import SandboxConfig
from modex_agent.sandbox.types import EnforcementLevel, SandboxResult
from modex_agent.sandbox.workspace_policy import WorkspacePolicyConfig


class TestSubprocessWorkspaceBoundary:
    """execute_command catches WorkspaceBoundaryError (the exception
    WorkspacePolicy.require_within actually raises), not its sibling."""

    async def test_outside_workspace_cwd_returns_error_result(self, tmp_path) -> None:
        inside = tmp_path / "ws"
        inside.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        config = SandboxConfig(
            workspace=WorkspacePolicyConfig(root=str(inside)),
            workspace_dir=str(inside),
        )
        sandbox = SubprocessSandbox(config)
        result = await sandbox.execute_command("echo hi", cwd=str(outside))
        assert result.success is False
        assert "outside" in (result.error or "") or "workspace" in (result.error or "")

    async def test_inside_workspace_cwd_executes(self, tmp_path) -> None:
        inside = tmp_path / "ws"
        inside.mkdir()

        config = SandboxConfig(
            workspace=WorkspacePolicyConfig(root=str(inside)),
            workspace_dir=str(inside),
        )
        sandbox = SubprocessSandbox(config)
        result = await sandbox.execute_command("echo hi", cwd=str(inside))
        assert result.success is True
        assert "hi" in result.stdout


class TestDockerArgvCommand:
    """docker execute_command passes argv, never an sh -c quoted string."""

    def test_language_images_dead_config_removed(self) -> None:
        assert not hasattr(DockerSandbox, "LANGUAGE_IMAGES")

    def test_run_kwargs_command_is_argv(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Capture containers.run kwargs and assert the command shape."""
        captured: dict = {}

        class FakeContainer:
            def wait(self, timeout=None):
                return {"StatusCode": 0}

            def logs(self, stdout=True, stderr=False):
                return b""

            def remove(self, force=False):
                pass

        class FakeContainers:
            def run(self, **kwargs):
                captured.update(kwargs)
                return FakeContainer()

        class FakeClient:
            containers = FakeContainers()

        import modex_agent.sandbox.adapters.docker as docker_module

        monkeypatch.setattr(docker_module, "_check_docker_available", lambda: True)
        monkeypatch.setattr(docker_module, "DOCKER_AVAILABLE", True)

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.config = SandboxConfig(workspace_dir=str(tmp_path))
        sandbox._command_guard = None
        sandbox._client = FakeClient()  # type: ignore[assignment]

        import asyncio

        result = asyncio.run(sandbox.execute_command("echo \"it's a 'quoted' arg\""))
        assert result.success is True

        command = captured["command"]
        assert isinstance(command, list), f"command must be argv, got {type(command)}"
        assert command[:2] == ["sh", "-c"]
        assert command[2] == "echo \"it's a 'quoted' arg\""


class TestE2bApiKeyHandling:
    """E2B passes the API key as an SDK parameter — never writes the
    process-global os.environ (concurrency-unsafe).

    The SDK is optional (absent in CI). ``raising=False`` installs the
    fake at the module attribute whether or not the real SDK defined it,
    so the REAL ``_create_sandbox`` runs against the fake.
    """

    def test_create_sandbox_passes_key_as_parameter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict] = []

        class FakeSdk:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                return object()

        import modex_agent.sandbox.adapters.e2b as e2b_module

        monkeypatch.setattr(e2b_module, "Sandbox", FakeSdk, raising=False)
        monkeypatch.delenv("E2B_API_KEY", raising=False)

        adapter = E2BSandbox(api_key="test-key-123")
        adapter._create_sandbox()
        assert calls == [{"api_key": "test-key-123"}]

    def test_create_without_key_uses_default_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict] = []

        class FakeSdk:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                return object()

        import modex_agent.sandbox.adapters.e2b as e2b_module

        monkeypatch.setattr(e2b_module, "Sandbox", FakeSdk, raising=False)
        monkeypatch.delenv("E2B_API_KEY", raising=False)

        adapter = E2BSandbox(api_key=None)
        adapter._create_sandbox()
        assert calls == [{}]

    def test_source_never_writes_e2b_key_to_environ(self) -> None:
        """Static contract: the adapter has no os.environ write for the key."""
        import inspect

        import modex_agent.sandbox.adapters.e2b as e2b_module

        source = inspect.getsource(e2b_module)
        assert "os.environ[\"E2B_API_KEY\"]" not in source
        assert "os.environ['E2B_API_KEY']" not in source
        assert "_set_api_key_env" not in source

    def test_execute_does_not_touch_global_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end through execute(): os.environ must be unchanged."""

        class FakeFiles:
            @staticmethod
            def make_dir(path):
                return None

            @staticmethod
            def list(path):
                return []

        class FakeResult:
            logs = type("L", (), {"stdout": [], "stderr": []})()
            error = None

        class FakeSdkSandbox:
            files = FakeFiles()

            def run_code(self, code, language=None):
                return FakeResult()

            def kill(self):
                pass

        import modex_agent.sandbox.adapters.e2b as e2b_module

        monkeypatch.setattr(e2b_module, "_check_e2b_available", lambda key=None: True)
        monkeypatch.delenv("E2B_API_KEY", raising=False)
        monkeypatch.setattr(E2BSandbox, "_create_sandbox", lambda self: FakeSdkSandbox())

        import os

        env_before = dict(os.environ)

        adapter = E2BSandbox(api_key="k-1")
        import asyncio

        result = asyncio.run(adapter.execute("print(1)"))
        assert result.success is True

        assert dict(os.environ) == env_before


class TestSandboxResultEnforcement:
    """SandboxResult carries the enforcement level (honest reporting)."""

    def test_default_enforcement_none(self) -> None:
        assert SandboxResult(success=True).enforcement is EnforcementLevel.NONE

    def test_exit_code_none_when_no_process(self) -> None:
        assert SandboxResult(success=False, error="blocked").exit_code is None
