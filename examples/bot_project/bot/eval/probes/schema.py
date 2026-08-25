"""Frozen schemas and load-time orthogonality checks for memory probes."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self, assert_never

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProbeSchemaError(ValueError):
    """A cross-record probe invariant is invalid."""


class ProbeType(StrEnum):
    """The five orthogonal memory capabilities measured by the suite."""

    EXTRACTION = "extraction"
    TEMPORAL = "temporal"
    KNOWLEDGE_UPDATE = "knowledge_update"
    REFUSAL = "refusal"
    CROSS_USER_ISOLATION = "cross_user_isolation"


class Speaker(StrEnum):
    """Allowed sides of a rendered memory conversation."""

    USER = "user"
    ASSISTANT = "assistant"


class Persona(BaseModel):
    """A stable synthetic user identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    persona_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    traits: list[str] = Field(min_length=1)


class Fact(BaseModel):
    """Programmatically sampled truth with dependency and update edges."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str = Field(min_length=1)
    persona_id: str = Field(min_length=1)
    attribute: str = Field(min_length=1)
    value: str = Field(min_length=1)
    valid_from: datetime
    superseded_by: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    surface_refs: list[str] = Field(min_length=1)


class SessionTurn(BaseModel):
    """One rendered sentence carrying explicit fact provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    speaker: Speaker
    text: str = Field(min_length=1)
    fact_ids: list[str] = Field(default_factory=list)


class Session(BaseModel):
    """One timestamped conversation in a persona timeline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    persona_id: str = Field(min_length=1)
    timestamp: datetime
    target_tokens: int = Field(default=0, ge=0)
    turns: list[SessionTurn] = Field(min_length=1)


class Probe(BaseModel):
    """One question whose answer remains wholly programmatic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_id: str = Field(min_length=1)
    probe_type: ProbeType
    persona_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    expected_answers: list[str]
    fact_ids: list[str]
    forbidden_fact_ids: list[str] = Field(default_factory=list)
    dual_arm: bool = False


class WorldSpec(BaseModel):
    """A complete frozen world; construction performs all cross-record checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    suite_version: str = Field(min_length=1)
    seed: int
    personas: list[Persona] = Field(min_length=2)
    facts: list[Fact]
    sessions: list[Session] = Field(min_length=1)
    probes: list[Probe] = Field(min_length=1)
    experiment_ref: str | None = None

    @model_validator(mode="after")
    def _validate_world(self) -> Self:
        personas = {persona.persona_id: persona for persona in self.personas}
        facts = {fact.fact_id: fact for fact in self.facts}
        if len(personas) != len(self.personas):
            raise ProbeSchemaError("persona ids must be unique")
        if len(facts) != len(self.facts):
            raise ProbeSchemaError("fact ids must be unique")
        if len({session.session_id for session in self.sessions}) != len(self.sessions):
            raise ProbeSchemaError("session ids must be unique")
        if len({probe.probe_id for probe in self.probes}) != len(self.probes):
            raise ProbeSchemaError("probe ids must be unique")
        self._validate_fact_graph(personas, facts)
        self._validate_sessions(personas, facts)
        for probe in self.probes:
            self._validate_probe(probe, personas, facts)
        return self

    @staticmethod
    def _validate_fact_graph(
        personas: dict[str, Persona],
        facts: dict[str, Fact],
    ) -> None:
        graph: dict[str, list[str]] = {}
        for fact in facts.values():
            if fact.persona_id not in personas:
                raise ProbeSchemaError(f"fact {fact.fact_id} references unknown persona")
            edges = list(fact.depends_on)
            if fact.superseded_by is not None and fact.superseded_by not in facts:
                raise ProbeSchemaError(
                    f"fact {fact.fact_id} references missing fact {fact.superseded_by}"
                )
            for edge in edges:
                if edge not in facts:
                    raise ProbeSchemaError(f"fact {fact.fact_id} references missing fact {edge}")
            graph[fact.fact_id] = edges
        pending = {node: len(edges) for node, edges in graph.items()}
        resolved = {node for node, count in pending.items() if count == 0}
        while resolved:
            current = resolved.pop()
            pending.pop(current, None)
            for node, edges in graph.items():
                if current in edges and node in pending:
                    pending[node] -= 1
                    if pending[node] == 0:
                        resolved.add(node)
        if pending:
            raise ProbeSchemaError(f"fact dependency graph contains a cycle: {sorted(pending)}")

    def _validate_sessions(
        self,
        personas: dict[str, Persona],
        facts: dict[str, Fact],
    ) -> None:
        for session in self.sessions:
            if session.persona_id not in personas:
                raise ProbeSchemaError(f"session {session.session_id} references unknown persona")
            for turn in session.turns:
                for fact_id in turn.fact_ids:
                    fact = facts.get(fact_id)
                    if fact is None:
                        raise ProbeSchemaError(f"session turn references missing fact {fact_id}")
                    if fact.persona_id != session.persona_id:
                        raise ProbeSchemaError(f"session turn crosses persona for fact {fact_id}")

    def _validate_probe(
        self,
        probe: Probe,
        personas: dict[str, Persona],
        facts: dict[str, Fact],
    ) -> None:
        if probe.persona_id not in personas:
            raise ProbeSchemaError(f"probe {probe.probe_id} references unknown persona")
        referenced = probe.fact_ids + probe.forbidden_fact_ids
        missing = [fact_id for fact_id in referenced if fact_id not in facts]
        if missing:
            raise ProbeSchemaError(f"probe {probe.probe_id} references missing facts {missing}")
        expected = [facts[fact_id].value for fact_id in probe.fact_ids]
        if probe.expected_answers != expected:
            raise ProbeSchemaError(
                f"probe {probe.probe_id} truth must equal referenced fact values"
            )
        match probe.probe_type:
            case ProbeType.EXTRACTION:
                if len(probe.fact_ids) != 1 or probe.forbidden_fact_ids:
                    raise ProbeSchemaError("extraction requires one fact and no forbidden facts")
            case ProbeType.TEMPORAL:
                selected = [facts[fact_id] for fact_id in probe.fact_ids]
                attributes = {(fact.persona_id, fact.attribute) for fact in selected}
                timestamps = [fact.valid_from for fact in selected]
                if len(selected) < 2 or len(attributes) != 1 or timestamps != sorted(timestamps):
                    raise ProbeSchemaError(
                        "temporal requires ordered facts for one persona attribute"
                    )
            case ProbeType.KNOWLEDGE_UPDATE:
                current = [facts[fact_id] for fact_id in probe.fact_ids]
                old = [facts[fact_id] for fact_id in probe.forbidden_fact_ids]
                if (
                    len(current) != 1
                    or not old
                    or any(fact.superseded_by != current[0].fact_id for fact in old)
                ):
                    raise ProbeSchemaError(
                        "knowledge_update requires a supersession edge to current truth"
                    )
            case ProbeType.REFUSAL:
                if probe.fact_ids or probe.forbidden_fact_ids or probe.expected_answers:
                    raise ProbeSchemaError("refusal must have no stored truth")
            case ProbeType.CROSS_USER_ISOLATION:
                own = [facts[fact_id] for fact_id in probe.fact_ids]
                forbidden = [facts[fact_id] for fact_id in probe.forbidden_fact_ids]
                if (
                    not own
                    or not forbidden
                    or any(fact.persona_id == probe.persona_id for fact in forbidden)
                    or any(fact.persona_id != probe.persona_id for fact in own)
                    or not (
                        {fact.attribute for fact in own} & {fact.attribute for fact in forbidden}
                    )
                ):
                    raise ProbeSchemaError(
                        "cross-user isolation requires colliding evidence from another user"
                    )
            case unreachable:
                assert_never(unreachable)

    def fact_by_id(self, fact_id: str) -> Fact:
        """Return a validated fact by id."""
        return next(fact for fact in self.facts if fact.fact_id == fact_id)
