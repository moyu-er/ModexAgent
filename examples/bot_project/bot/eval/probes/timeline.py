"""Deterministic timestamped conversation materialization."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from bot.eval.probes.schema import Fact, Persona, Session, SessionTurn, Speaker


class TimelineParameters(BaseModel):
    """Session count, token range, and temporal span for one sampled world."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sessions_per_persona: int = Field(ge=1)
    min_tokens_per_persona: int = Field(ge=1)
    max_tokens_per_persona: int = Field(ge=1)
    weeks: int = Field(ge=1)


class TimelineSampler:
    """Own one seeded RNG while constructing persona conversation timelines."""

    def __init__(
        self,
        rng: random.Random,
        parameters: TimelineParameters,
        start: datetime,
    ) -> None:
        self._rng = rng
        self._parameters = parameters
        self._start = start

    def sample(self, personas: list[Persona], facts: list[Fact]) -> list[Session]:
        """Materialize all timestamped sessions and deterministic neutral filler."""
        sessions: list[Session] = []
        total_days = self._parameters.weeks * 7
        for persona_index, persona in enumerate(personas):
            target = self._rng.randint(
                self._parameters.min_tokens_per_persona,
                self._parameters.max_tokens_per_persona,
            )
            base_tokens, remainder = divmod(target, self._parameters.sessions_per_persona)
            persona_facts = [fact for fact in facts if fact.persona_id == persona.persona_id]
            for session_index in range(self._parameters.sessions_per_persona):
                session_target = base_tokens + (1 if session_index < remainder else 0)
                day = (session_index * (total_days - 1)) // max(
                    1, self._parameters.sessions_per_persona - 1
                )
                timestamp = self._start + timedelta(days=day, minutes=persona_index)
                assigned = [
                    fact
                    for fact in persona_facts
                    if fact.valid_from <= timestamp
                    and (
                        session_index == self._parameters.sessions_per_persona - 1
                        or fact.valid_from
                        > timestamp
                        - timedelta(
                            days=max(1, total_days // self._parameters.sessions_per_persona)
                        )
                    )
                ]
                turns = [
                    SessionTurn(
                        speaker=Speaker.USER,
                        text=fact.surface_refs[0],
                        fact_ids=[fact.fact_id],
                    )
                    for fact in sorted(assigned, key=lambda item: (item.valid_from, item.fact_id))
                ]
                turns.append(
                    SessionTurn(
                        speaker=Speaker.ASSISTANT,
                        text=" neutral" * session_target,
                        fact_ids=[],
                    )
                )
                sessions.append(
                    Session(
                        session_id=(f"{persona.persona_id}-session-{session_index + 1:02d}"),
                        persona_id=persona.persona_id,
                        timestamp=timestamp,
                        target_tokens=session_target,
                        turns=turns,
                    )
                )
        return sorted(sessions, key=lambda session: (session.timestamp, session.session_id))
