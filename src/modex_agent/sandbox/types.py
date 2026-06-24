from dataclasses import dataclass, field


@dataclass
class SandboxArtifact:
    path: str
    size: int
    mime_type: str


@dataclass
class SandboxResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    artifacts: list[SandboxArtifact] = field(default_factory=list)
    execution_time_ms: float = 0.0
    error: str | None = None
