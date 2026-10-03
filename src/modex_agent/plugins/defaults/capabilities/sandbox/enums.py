from enum import StrEnum


class SandboxType(StrEnum):
    SUBPROCESS = "subprocess"
    LANDLOCK = "landlock"
    DOCKER = "docker"
    E2B = "e2b"

    @property
    def display_name(self) -> str:
        return {
            SandboxType.SUBPROCESS: "Subprocess (Local)",
            SandboxType.LANDLOCK: "Landlock (Linux Filesystem)",
            SandboxType.DOCKER: "Docker (Container)",
            SandboxType.E2B: "E2B (Cloud)",
        }[self]

    @property
    def description(self) -> str:
        return {
            SandboxType.SUBPROCESS: "Basic process execution (low isolation)",
            SandboxType.LANDLOCK: "Linux Landlock filesystem sandbox (high isolation)",
            SandboxType.DOCKER: "Docker container sandbox (high isolation)",
            SandboxType.E2B: "E2B cloud sandbox (highest isolation)",
        }[self]

    @property
    def requires(self) -> str:
        return {
            SandboxType.SUBPROCESS: "None",
            SandboxType.LANDLOCK: "Linux 5.13+ with landlock package",
            SandboxType.DOCKER: "Docker daemon running",
            SandboxType.E2B: "E2B_API_KEY environment variable",
        }[self]


__all__ = ["SandboxType"]
