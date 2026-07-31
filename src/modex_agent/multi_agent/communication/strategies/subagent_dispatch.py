"""Strategy for NORMAL → SUBAGENT task dispatch."""

from __future__ import annotations

import uuid as _uuid_mod

from modex_agent.core.session_id import SessionInfo
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.communication.result import AgentSendResult
from modex_agent.multi_agent.communication.strategies.base import SendRequest, SendStrategy
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.message_xml import build_dispatch_xml

_TASK_ID_BYTES = 8


class SubagentDispatchStrategy(SendStrategy):
    """NORMAL → SUBAGENT task dispatch.

    Mints a fresh invocation_id when none is provided, creates a
    task-scoped subagent session, builds a TASK_REQUEST envelope, and
    delivers via the local agent bus. Orchestration is inherited from
    :meth:`SendStrategy.execute`.
    """

    def normalize_invocation_id(self, req: SendRequest) -> str | None:
        """Mint a new invocation id when none is provided."""
        invocation_id = req.invocation_id
        if invocation_id is None or invocation_id.strip() == "":
            return _uuid_mod.uuid4().hex[:_TASK_ID_BYTES]
        return invocation_id

    def build_session(self, req: SendRequest, invocation_id: str) -> SessionInfo:
        """Create a task-scoped subagent session keyed by invocation_id."""
        return self._deps.session_factory.create_with_prefix(
            agent_name=req.target.name,
            prefix=invocation_id,
            parent_session_id=req.context.session,
        )

    def build_envelope(
        self, req: SendRequest, session: SessionInfo, invocation_id: str
    ) -> AgentMessageEnvelope:
        """Build a TASK_REQUEST envelope."""
        effective_source = self._resolve_source(req)
        parent_sid = req.context.session
        xml_content = build_dispatch_xml(
            source=effective_source.name,
            invocation_id=invocation_id,
            content=req.content,
            target_execution_strategy=req.target.execution_strategy,
        )
        return AgentMessageEnvelope(
            payload={"content": xml_content, "message_type": AgentMessageType.TASK_REQUEST},
            source=effective_source,
            target=AgentAddress(name=req.target.name),
            message_type=AgentMessageType.TASK_REQUEST,
            session_id=str(parent_sid),
            agent_session_id=str(session),
            parent_session_id=str(parent_sid),
            invocation_id=invocation_id,
        )

    def should_register_session(self) -> bool:
        """Subagent sessions are owned by the sender's pool."""
        return True

    def build_result(
        self, req: SendRequest, session: SessionInfo, invocation_id: str
    ) -> AgentSendResult:
        """Add trace/output paths for subagent ack."""
        from modex_agent.core.constants import ExecutionStrategyKind

        if req.target.execution_strategy == ExecutionStrategyKind.EXTERNAL:
            return self._build_external_result(req, session, invocation_id)
        return self._build_native_result(req, session, invocation_id)

    def _build_native_result(
        self, req: SendRequest, session: SessionInfo, invocation_id: str
    ) -> AgentSendResult:
        created_new_task = req.invocation_id is None or req.invocation_id.strip() == ""
        return AgentSendResult(
            target_agent=req.target.name,
            target_kind=req.target.kind,
            session_id=str(session),
            invocation_id=invocation_id,
            created_new_task=created_new_task,
            output_path=self._subagent_output_path(req.target.kind, str(session)),
            trace_dir=self._subagent_trace_dir(req.target.kind, str(session)),
        )

    def _build_external_result(
        self, req: SendRequest, session: SessionInfo, invocation_id: str
    ) -> AgentSendResult:
        created_new_task = req.invocation_id is None or req.invocation_id.strip() == ""
        return AgentSendResult(
            target_agent=req.target.name,
            target_kind=req.target.kind,
            session_id=str(session),
            invocation_id=invocation_id,
            created_new_task=created_new_task,
        )
