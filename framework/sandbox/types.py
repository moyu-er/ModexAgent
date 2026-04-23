from dataclasses import dataclass, field
from typing import Dict, List, Optional


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
    artifacts: List[SandboxArtifact] = field(default_factory=list)
    execution_time_ms: float = 0.0
    error: Optional[str] = None
