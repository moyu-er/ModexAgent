"""Load, validate, and version centralized judge rubric sets.

The general-agent weights prioritize task completion (0.35), give equal
importance to verification and instruction following (0.20 each), reserve
0.15 for grounded reporting, and keep efficiency intentionally light (0.10).
"""

from __future__ import annotations

import json
import math
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

_DEFAULT_LIBRARY_DIR: Final = Path(__file__).resolve().parents[3] / "evals" / "judge" / "rubrics"
_WEIGHT_TOLERANCE: Final = 1e-9
_VERSION_LENGTH: Final = 8


class Rubric(BaseModel):
    """One independently judged binary criterion and its aggregate weight."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion: str
    description: str
    weight: float


class RubricSet(BaseModel):
    """A named collection of independently judgeable rubrics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    rubrics: list[Rubric]


class RubricValidationError(ValueError):
    """A load-time orthogonality violation in a rubric set."""

    def __init__(self, *, criteria: tuple[str, ...], reason: str) -> None:
        self.criteria = criteria
        self.reason = reason
        names = ", ".join(criteria) or "<no criteria>"
        super().__init__(f"rubric orthogonality violation for [{names}]: {reason}")


def load_rubric_set(name: str, *, library_dir: Path | None = None) -> RubricSet:
    """Load and validate one named rubric set from the centralized library."""
    path = (library_dir or _DEFAULT_LIBRARY_DIR) / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"rubric set not found: {path}")
    rubric_set = RubricSet.model_validate_json(path.read_text(encoding="utf-8"))
    criteria = tuple(rubric.criterion for rubric in rubric_set.rubrics)
    duplicates = tuple(sorted(name for name, count in Counter(criteria).items() if count > 1))
    if duplicates:
        raise RubricValidationError(
            criteria=duplicates,
            reason="criterion names must be distinct",
        )

    total_weight = math.fsum(rubric.weight for rubric in rubric_set.rubrics)
    if not math.isclose(total_weight, 1.0, rel_tol=0.0, abs_tol=_WEIGHT_TOLERANCE):
        raise RubricValidationError(
            criteria=criteria,
            reason=f"weights sum to {total_weight:g}; expected 1.0 within {_WEIGHT_TOLERANCE:g}",
        )
    return rubric_set


def rubric_version(rubric_set: RubricSet) -> str:
    """Return the first eight hex characters of the set's canonical SHA-256."""
    canonical = json.dumps(
        rubric_set.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()[:_VERSION_LENGTH]
