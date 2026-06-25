from __future__ import annotations

import pytest

from modex_agent.sandbox.enums import SandboxType
from modex_agent.sandbox.factory import PlatformFallbackChain, get_local_sandbox
from modex_agent.sandbox.platform import Platform


class TestPlatformFallbackChain:
    """PlatformFallbackChain returns correct order per platform."""

    @pytest.fixture
    def chain(self) -> PlatformFallbackChain:
        return PlatformFallbackChain()

    def test_linux_order(self, chain: PlatformFallbackChain) -> None:
        result = chain.for_platform(Platform.LINUX)
        assert result[0] == SandboxType.LANDLOCK
        assert result[1] == SandboxType.DOCKER
        assert result[2] == SandboxType.SUBPROCESS

    def test_macos_order(self, chain: PlatformFallbackChain) -> None:
        result = chain.for_platform(Platform.MACOS)
        assert result[0] == SandboxType.DOCKER
        assert result[1] == SandboxType.SUBPROCESS

    def test_windows_order(self, chain: PlatformFallbackChain) -> None:
        result = chain.for_platform(Platform.WINDOWS)
        assert result[0] == SandboxType.DOCKER
        assert result[1] == SandboxType.SUBPROCESS

    def test_unknown_order(self, chain: PlatformFallbackChain) -> None:
        result = chain.for_platform(Platform.UNKNOWN)
        assert result == (SandboxType.SUBPROCESS,)

    def test_custom_chain(self) -> None:
        custom = PlatformFallbackChain(
            linux=(SandboxType.SUBPROCESS,),
        )
        assert custom.for_platform(Platform.LINUX) == (SandboxType.SUBPROCESS,)


class TestGetLocalSandbox:
    """get_local_sandbox returns a usable adapter."""

    def test_returns_adapter(self) -> None:
        sandbox = get_local_sandbox()
        assert sandbox is not None
        assert sandbox.is_available

    def test_subprocess_always_available(self) -> None:
        # With a chain that only has SUBPROCESS
        chain = PlatformFallbackChain(
            linux=(SandboxType.SUBPROCESS,),
            macos=(SandboxType.SUBPROCESS,),
            windows=(SandboxType.SUBPROCESS,),
            unknown=(SandboxType.SUBPROCESS,),
        )
        sandbox = get_local_sandbox(chain=chain)
        assert sandbox.name == "subprocess"
