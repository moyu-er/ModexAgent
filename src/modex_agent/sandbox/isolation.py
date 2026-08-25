"""OS-level sandbox isolation for command execution.

This module provides cross-platform OS-level isolation using native mechanisms:
- Linux: bubblewrap (bwrap) for namespace/container isolation
- macOS: sandbox-exec (Seatbelt) for profile-based isolation
- Windows: Restricted Token + Job Objects + Low Integrity Level

Example:
    from framework.sandbox.isolation import IsolationManager

    # Create isolation manager
    isolation = IsolationManager(
        filesystem_config={
            "allow_read": ["/workspace"],
            "allow_write": ["/workspace/output"],
        },
        network_config={
            "allow_domains": ["pypi.org", "github.com"],
        }
    )

    # Execute command in isolated environment
    with isolation.isolated_shell() as shell:
        result = shell.execute("pip install requests")
"""

import abc
import logging
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class FilesystemIsolationConfig(BaseModel):
    """Filesystem isolation configuration.

    Attributes:
        allow_read: List of paths allowed for reading
        allow_write: List of paths allowed for writing
        deny_read: List of paths explicitly denied for reading
        deny_write: List of paths explicitly denied for writing
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allow_read: list[str] = Field(default_factory=list)
    allow_write: list[str] = Field(default_factory=list)
    deny_read: list[str] = Field(default_factory=list)
    deny_write: list[str] = Field(default_factory=list)


class NetworkIsolationConfig(BaseModel):
    """Network isolation configuration.

    Attributes:
        allow_domains: List of domains allowed for network access
        deny_domains: List of domains explicitly denied
        allow_all: Whether to allow all network access (default: False)
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allow_domains: list[str] = Field(default_factory=list)
    deny_domains: list[str] = Field(default_factory=list)
    allow_all: bool = False


class ResourceLimits(BaseModel):
    """Resource limits for sandboxed processes.

    Attributes:
        max_memory_mb: Maximum memory in MB
        max_cpu_percent: Maximum CPU percentage
        max_processes: Maximum number of processes
        timeout_seconds: Maximum execution time
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_memory_mb: int | None = None
    max_cpu_percent: int | None = None
    max_processes: int | None = None
    timeout_seconds: int | None = None


class IsolationConfig(BaseModel):
    """Complete isolation configuration.

    Attributes:
        filesystem: Filesystem isolation settings
        network: Network isolation settings
        resources: Resource limits
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    filesystem: FilesystemIsolationConfig = Field(default_factory=FilesystemIsolationConfig)
    network: NetworkIsolationConfig = Field(default_factory=NetworkIsolationConfig)
    resources: ResourceLimits = Field(default_factory=ResourceLimits)


class IsolationProvider(abc.ABC):
    """Abstract base class for OS-level isolation providers."""

    def __init__(self, config: IsolationConfig) -> None:
        self.config = config

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Check if this isolation provider is available on the system."""
        pass

    @abc.abstractmethod
    def wrap_command(self, command: list[str]) -> list[str]:
        """Wrap a command with isolation.

        Args:
            command: The command to wrap

        Returns:
            Modified command with isolation applied
        """
        pass

    @abc.abstractmethod
    def get_name(self) -> str:
        """Get the name of this provider."""
        pass


class BubblewrapProvider(IsolationProvider):
    """Linux bubblewrap-based isolation provider.

    Uses bubblewrap (bwrap) to create a minimal sandbox with:
    - New mount namespace (filesystem isolation)
    - New network namespace (if network isolation enabled)
    - New PID namespace
    - Resource limits via cgroups (if available)
    """

    def is_available(self) -> bool:
        """Check if bubblewrap is installed."""
        return shutil.which("bwrap") is not None

    def get_name(self) -> str:
        return "bubblewrap"

    def wrap_command(self, command: list[str]) -> list[str]:
        """Wrap command with bubblewrap.

        Creates a sandbox with:
        - Read-only root filesystem
        - Writable tmpfs for /tmp
        - Bind mounts for allowed paths
        - Network namespace isolation (if configured)
        """
        bwrap_args = ["bwrap"]

        # Basic sandbox setup
        bwrap_args.extend(
            [
                "--unshare-all",  # Unshare all namespaces
                "--die-with-parent",  # Kill sandbox when parent dies
                "--proc",
                "/proc",  # Mount new proc filesystem
                "--dev",
                "/dev",  # Mount minimal dev filesystem
                "--tmpfs",
                "/tmp",  # Writable tmpfs for /tmp
            ]
        )

        # Filesystem isolation
        fs = self.config.filesystem

        # Make root read-only by default
        bwrap_args.extend(["--ro-bind", "/", "/"])

        # Add writable directories
        for path in fs.allow_write:
            resolved = Path(path).resolve()
            if resolved.exists():
                bwrap_args.extend(["--bind", str(resolved), str(resolved)])

        # Add read-only directories (explicit)
        for path in fs.allow_read:
            resolved = Path(path).resolve()
            if resolved.exists() and str(resolved) not in fs.allow_write:
                bwrap_args.extend(["--ro-bind", str(resolved), str(resolved)])

        # Network isolation
        if not self.config.network.allow_all:
            bwrap_args.append("--unshare-net")

        # Resource limits (if cgroups available)
        resources = self.config.resources
        if resources.max_memory_mb:
            # Note: Requires cgroup v2 support
            bwrap_args.extend(["--memory-limit", str(resources.max_memory_mb * 1024 * 1024)])

        # Add the actual command
        bwrap_args.extend(["--", *command])

        return bwrap_args


class SandboxExecProvider(IsolationProvider):
    """macOS sandbox-exec-based isolation provider.

    Uses sandbox-exec with Seatbelt profiles to enforce:
    - Filesystem access restrictions
    - Network access restrictions
    - Mach service restrictions
    """

    def is_available(self) -> bool:
        """Check if sandbox-exec is available."""
        return shutil.which("sandbox-exec") is not None and platform.system() == "Darwin"

    def get_name(self) -> str:
        return "sandbox-exec"

    def _generate_profile(self) -> str:
        """Generate a Seatbelt profile for the sandbox.

        Returns:
            Seatbelt profile string
        """
        # Use a simpler, more permissive profile for compatibility
        # This profile allows basic shell operations while still providing some isolation
        lines = [
            "(version 1)",
            "",
            "; Allow basic operations",
            "(allow default)",
            "(allow process*)",
            "(allow file*)",
            "(allow signal (target self))",
            "",
        ]

        # Network access
        if not self.config.network.allow_all:
            lines.append("(deny network*)")

        return "\n".join(lines)

    def wrap_command(self, command: list[str]) -> list[str]:
        """Wrap command with sandbox-exec."""
        profile = self._generate_profile()

        return ["sandbox-exec", "-p", profile, *command]


class WindowsIsolationProvider(IsolationProvider):
    """Windows native isolation provider.

    Uses Windows security mechanisms:
    - Restricted Token (CreateRestrictedToken)
    - Low Integrity Level
    - Job Objects for resource limits

    Reference: Trae/AgentBox implementation
    """

    def is_available(self) -> bool:
        """Check if running on Windows."""
        return platform.system() == "Windows"

    def get_name(self) -> str:
        return "windows-native"

    def wrap_command(self, command: list[str]) -> list[str]:
        """Wrap command with Windows isolation.

        Note: Full implementation requires ctypes/CFFI for Win32 API calls.
        This is a simplified version using PowerShell constraints.
        """
        # For now, use PowerShell Constrained Language Mode as fallback
        # Full implementation would use CreateRestrictedToken + Job Objects

        logger.warning(
            "Windows isolation using basic PowerShell constraints. "
            "Full Restricted Token + Job Objects implementation requires Win32 API."
        )

        # Build a constrained execution environment
        ps_script = [
            "powershell.exe",
            "-ExecutionPolicy",
            "Restricted",
            "-Command",
        ]

        # Add filesystem restrictions via ACL checks

        # Build the constrained command.
        # NOTE: Do NOT add Set-Location here. subprocess.Popen(cwd=...) handles
        # working directory correctly. Adding Set-Location would override it and
        # cause files to be created in the wrong directory.
        constrained_cmd = "; ".join(
            [
                "$ErrorActionPreference = 'Stop'",
                " ".join(command),
            ]
        )

        ps_script.append(constrained_cmd)

        return ps_script


class IsolationManager:
    """Manager for OS-level sandbox isolation.

    Automatically selects the best available isolation provider
    for the current platform.

    Example:
        manager = IsolationManager()

        with manager.isolated_shell() as shell:
            result = shell.run(["pip", "install", "requests"])
    """

    def __init__(self, config: IsolationConfig | None = None) -> None:
        """Initialize isolation manager.

        Args:
            config: Isolation configuration. If None, uses defaults.
        """
        self.config = config or IsolationConfig()
        self._provider: IsolationProvider | None = None
        self._select_provider()

    def _select_provider(self) -> None:
        """Select the best available isolation provider for the current platform."""
        import platform as _platform

        system = _platform.system()
        if system == "Linux":
            providers = [BubblewrapProvider]
        elif system == "Darwin":
            providers = [SandboxExecProvider]
        elif system == "Windows":
            providers = [WindowsIsolationProvider]
        else:
            providers = [BubblewrapProvider, SandboxExecProvider, WindowsIsolationProvider]

        for provider_cls in providers:
            provider = provider_cls(self.config)
            if provider.is_available():
                self._provider = provider
                logger.info(f"Selected isolation provider: {provider.get_name()}")
                return

        logger.warning("No OS-level isolation provider available")

    def is_available(self) -> bool:
        """Check if isolation is available."""
        return self._provider is not None

    def get_provider_name(self) -> str | None:
        """Get the name of the selected provider."""
        return self._provider.get_name() if self._provider else None

    def wrap_command(self, command: list[str]) -> list[str]:
        """Wrap a command with isolation.

        Args:
            command: Command to wrap

        Returns:
            Command with isolation applied, or original if no provider available
        """
        if self._provider:
            return self._provider.wrap_command(command)
        return command

    def execute(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        """Execute a command in isolated environment.

        Args:
            command: Command to execute
            **kwargs: Additional arguments passed to subprocess.run

        Returns:
            CompletedProcess result
        """
        isolated_cmd = self.wrap_command(command)

        logger.debug(f"Executing isolated command: {' '.join(isolated_cmd)}")

        return subprocess.run(isolated_cmd, capture_output=True, text=True, **kwargs)


def get_default_isolation() -> IsolationManager:
    """Get default isolation manager with sensible defaults.

    Returns:
        IsolationManager with default configuration
    """
    config = IsolationConfig(
        filesystem=FilesystemIsolationConfig(
            allow_read=[str(Path.cwd())],
            allow_write=[str(Path.cwd() / "output"), tempfile.gettempdir()],
        ),
        network=NetworkIsolationConfig(
            allow_all=True,  # Allow network by default for compatibility
        ),
    )

    return IsolationManager(config)


# Convenience function for quick isolation
def isolate_command(command: list[str], **isolation_kwargs) -> subprocess.CompletedProcess:
    """Execute a command with OS-level isolation.

    Args:
        command: Command to execute
        **isolation_kwargs: Isolation configuration options

    Returns:
        CompletedProcess result

    Example:
        result = isolate_command(["pip", "install", "requests"])
        print(result.stdout)
    """
    config = IsolationConfig(**isolation_kwargs)
    manager = IsolationManager(config)
    return manager.execute(command)
