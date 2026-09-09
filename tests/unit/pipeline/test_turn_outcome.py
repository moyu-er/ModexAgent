from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.core.emitter import AgentResult
from modex_agent.pipeline.turn_outcome import (
    TurnOutcome,
    TurnOutcomeKind,
    TurnSuspension,
)


@pytest.mark.parametrize(
    "kind",
    [TurnOutcomeKind.FINISHED, TurnOutcomeKind.SUSPENDED],
)
def test_turn_outcome_rejects_payload_from_another_variant(
    kind: TurnOutcomeKind,
) -> None:
    """FINISHED and SUSPENDED are exclusive variants, even though suspension
    details themselves remain optional."""
    result = AgentResult(content="done")
    suspension = TurnSuspension(turn_uuid=None, requests=[])

    with pytest.raises(ValidationError):
        TurnOutcome(kind=kind, result=result, suspension=suspension)
