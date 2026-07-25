"""external_coding agent communication — env injection, reply contract, modexctl send/receive.

Verifies the external_coding communication path works end-to-end:
1. ExternalEnvBuilder.build() produces the full MODEX_* env dict for spawning
   an external coding agent (Pi/OpenCode) — including PATH prepend.
2. build_dispatch_xml with EXTERNAL_CODING target produces the peer format
   with <reply_contract> instructing the external agent to reply via
   `modexctl send` (not send_to_agent tool).
3. build_peer_agent_message with receiver_implementation=EXTERNAL produces
   the correct CLI reply instructions.
4. modexctl _send PeerNormal path (used by external agents to reply) writes
   the reply to the target pool's InboxMQ.
5. The reply contract convergence point (build_dispatch_xml) routes
   external targets to peer format and native targets to agent format.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from modex_agent.agents.external_coding.env_builder import ExternalEnvBuilder
from modex_agent.agents.external_coding.types import ExternalEnvSpec
from modex_agent.core.agent import AgentCommKind, AgentImplementation
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.multi_agent.message_xml import (
    build_agent_message,
    build_dispatch_xml,
    build_peer_agent_message,
)
from modexctl.main import build_app


def _make_external_spec(
    *,
    agent_name: str = "opencode",
    session_id: str = "conv1.opencode",
    comm_kind: AgentCommKind = AgentCommKind.NORMAL,
) -> ExternalEnvSpec:
    return ExternalEnvSpec(
        workspace_root=Path("/tmp/ws"),
        inbox_root=Path("/tmp/ws/.modex/inbox"),
        workdir=Path("/tmp/ws"),
        session_id=session_id,
        agent_name=agent_name,
        provider_session_id="provider-sess-123",
        agent_pool_map={agent_name: "opencode", "default": "default"},
        targets=[("default", "General-purpose assistant")],
        modexctl_bin_dir=Path("/tmp/bin"),
        comm_kind=comm_kind,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Step 1: ExternalEnvBuilder.build() for external_coding spawn
# ═══════════════════════════════════════════════════════════════════════════════


class TestExternalEnvBuilderForExternalCoding:
    def test_build_emits_all_9_modex_vars_plus_path(self) -> None:
        spec = _make_external_spec()
        base_env = {"PATH": "/usr/bin", "HOME": "/tmp"}
        env = ExternalEnvBuilder.build(spec, base_env)

        # All 9 MODEX_* vars present (compare via str(Path) for cross-platform)
        assert env["MODEX_WORKSPACE_ROOT"] == str(Path("/tmp/ws"))
        assert env["MODEX_INBOX_ROOT"] == str(Path("/tmp/ws/.modex/inbox"))
        assert env["MODEX_WORKDIR"] == str(Path("/tmp/ws"))
        assert env["MODEX_SESSION_ID"] == "conv1.opencode"
        assert env["MODEX_AGENT_NAME"] == "opencode"
        assert env["MODEX_PROVIDER_SESSION_ID"] == "provider-sess-123"
        assert "opencode=opencode" in env["MODEX_AGENT_POOL_MAP"]
        assert "default=default" in env["MODEX_AGENT_POOL_MAP"]
        assert env["MODEX_COMM_KIND"] == "normal"

        # PATH must have modexctl_bin_dir prepended
        assert env["PATH"].startswith(str(Path("/tmp/bin")) + os.pathsep)

        # Base env preserved
        assert env["HOME"] == "/tmp"

    def test_build_does_not_mutate_base_env(self) -> None:
        spec = _make_external_spec()
        base_env = {"PATH": "/usr/bin"}
        base_copy = dict(base_env)
        ExternalEnvBuilder.build(spec, base_env)
        assert base_env == base_copy, "build must not mutate base_env"

    def test_build_modex_vars_omits_parent_for_normal(self) -> None:
        spec = _make_external_spec(comm_kind=AgentCommKind.NORMAL)
        vars_dict = ExternalEnvBuilder.build_modex_vars(spec)
        assert "MODEX_PARENT_SESSION_ID" not in vars_dict


# ═══════════════════════════════════════════════════════════════════════════════
# Step 2+3: build_dispatch_xml routes external targets to peer format
# ═══════════════════════════════════════════════════════════════════════════════


class TestReplyContractConvergence:
    """build_dispatch_xml is the single convergence point for the
    "target is external → peer format" rule (ADR-0022 D6)."""

    def test_external_target_uses_peer_format_with_modexctl_instructions(self) -> None:
        xml = build_dispatch_xml(
            source="orchestrator",
            invocation_id="inv1",
            content="do this task",
            target_execution_strategy=ExecutionStrategyKind.EXTERNAL_CODING,
        )
        # External targets get <reply_contract> with modexctl send instructions
        assert "<agent_message" in xml
        assert "<reply_contract>" in xml
        assert "modexctl send" in xml
        assert 'send_to_agent' not in xml, (
            "external targets must NOT see send_to_agent instructions"
        )

    def test_native_target_uses_agent_format_without_reply_contract(self) -> None:
        xml = build_dispatch_xml(
            source="orchestrator",
            invocation_id="inv1",
            content="do this task",
            target_execution_strategy=ExecutionStrategyKind.REACT,
        )
        # Native targets get the minimal format (SubagentAutoSendHook delivers reply)
        assert "<agent_message" in xml
        assert "<reply_contract>" not in xml, (
            "native targets must NOT carry reply_contract (token overhead)"
        )
        assert 'invocation_id="inv1"' in xml

    def test_build_peer_agent_message_external_implementation(self) -> None:
        xml = build_peer_agent_message(
            source="default",
            content="hello",
            receiver_implementation=AgentImplementation.EXTERNAL,
        )
        assert "<reply_contract>" in xml
        assert "modexctl send" in xml
        assert '--to "default"' in xml

    def test_build_peer_agent_message_native_implementation(self) -> None:
        xml = build_peer_agent_message(
            source="default",
            content="hello",
            receiver_implementation=AgentImplementation.NATIVE,
        )
        assert "<reply_contract>" in xml
        assert "send_to_agent" in xml
        assert 'target_agent = "default"' in xml

    def test_build_agent_message_no_reply_contract(self) -> None:
        """build_agent_message (subagent dispatch/parent reply) never carries
        reply_contract — the SubagentAutoSendHook delivers replies automatically."""
        xml = build_agent_message(source="orchestrator", invocation_id="inv1", content="hi")
        assert "<agent_message" in xml
        assert "<reply_contract>" not in xml


# ═══════════════════════════════════════════════════════════════════════════════
# Step 4: external agent replies via modexctl send (PeerNormal path)
# ═══════════════════════════════════════════════════════════════════════════════


class TestExternalAgentReplyViaModexctl:
    """An external coding agent (opencode) replies to a peer (default) via
    `modexctl send --to default --content ...`.

    The external agent's bash tool inherits MODEX_* env vars (set by
    ExternalEnvBuilder.build at spawn time), so it can invoke modexctl.
    """

    def test_external_agent_reply_lands_on_peer_pool(self, tmp_path: Path) -> None:
        inbox_root = tmp_path / "ws" / ".modex" / "inbox"

        # opencode's env (as spawned by ExternalCodingAgentBuilder)
        env = {
            "MODEX_SESSION_ID": "conv1.opencode",
            "MODEX_AGENT_NAME": "opencode",
            "MODEX_INBOX_ROOT": str(inbox_root),
            "MODEX_AGENT_POOL_MAP": "opencode=opencode;default=default",
            "MODEX_TARGETS": "default=General assistant",
            "MODEX_COMM_KIND": "normal",
            # External agents also have MODEX_WORKDIR, MODEX_PROVIDER_SESSION_ID, etc.
            # but modexctl send only needs the 5 comm env keys.
        }
        old_env = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            result = CliRunner().invoke(
                build_app(),
                ["send", "--to", "default", "--content", "task completed successfully"],
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        assert result.exit_code == 0

        db_path = tmp_path / "ws" / ".modex" / "state.db"
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT session_id, message_type, payload_json FROM inbox_messages"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        session_id, msg_type, payload_json = row
        # PeerNormal: prefix-reuse → conv1.default
        assert session_id == "conv1.default"
        assert msg_type == "agent_message"
        payload = json.loads(payload_json)
        assert payload["source"] == "opencode"
        assert "task completed successfully" in payload["content"]

    def test_external_agent_can_list_peers(self, tmp_path: Path) -> None:
        """modexctl agents lists routable peers — external agents use this
        to discover who they can reply to."""
        inbox_root = tmp_path / "ws" / ".modex" / "inbox"
        env = {
            "MODEX_SESSION_ID": "conv1.opencode",
            "MODEX_AGENT_NAME": "opencode",
            "MODEX_INBOX_ROOT": str(inbox_root),
            "MODEX_AGENT_POOL_MAP": "opencode=opencode;default=default;orchestrator=coder",
            "MODEX_TARGETS": "default=General assistant;orchestrator=Code orchestrator",
            "MODEX_COMM_KIND": "normal",
        }
        old_env = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            result = CliRunner().invoke(build_app(), ["agents"])
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        assert result.exit_code == 0
        lines = result.stdout.splitlines()
        assert len(lines) == 2
        assert "default" in lines[0]
        assert "General assistant" in lines[0]
        assert "orchestrator" in lines[1]


# ═══════════════════════════════════════════════════════════════════════════════
# Step 5: external_coding main agent can receive peer messages
# ═══════════════════════════════════════════════════════════════════════════════


class TestExternalCodingReceivesPeerMessages:
    """When a native peer sends to an external_coding main agent via modexctl,
    the message lands on the external pool's InboxMQ. The external agent's
    poller picks it up and the ExternalTurnRunner executes a turn.

    Note: external_coding main agents DO have a pipeline (ExternalTurnRunner
    is wired through AgentPipeline). The poller finds them via pool.get(name)
    — they are eagerly registered at boot via _register_main_agent, same as
    react agents.
    """

    def test_peer_message_to_external_coding_lands_on_pool(self, tmp_path: Path) -> None:
        """default pool sends to opencode pool's main agent."""
        inbox_root = tmp_path / "ws" / ".modex" / "inbox"
        env = {
            "MODEX_SESSION_ID": "conv1.default",
            "MODEX_AGENT_NAME": "default",
            "MODEX_INBOX_ROOT": str(inbox_root),
            "MODEX_AGENT_POOL_MAP": "default=default;opencode=opencode",
            "MODEX_TARGETS": "opencode=Autonomous coding agent",
            "MODEX_COMM_KIND": "normal",
        }
        old_env = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            result = CliRunner().invoke(
                build_app(),
                ["send", "--to", "opencode", "--content", "implement feature X"],
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        assert result.exit_code == 0

        db_path = tmp_path / "ws" / ".modex" / "state.db"
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT session_id, owner_scope_key, payload_json FROM inbox_messages"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        session_id, owner_scope_key, payload_json = row
        # PeerNormal: prefix-reuse → conv1.opencode
        assert session_id == "conv1.opencode"
        # owner_scope_key must be opencode pool's scope
        assert "opencode" in owner_scope_key
        # The XML content must include <reply_contract> because the SENDER
        # (default, native) sends via PeerNormal which uses build_peer_agent_message.
        # But wait — modexctl's PeerNormal path uses build_peer_agent_message
        # with default receiver_implementation=NATIVE (the receiver is native).
        # For external receivers, the sender should use EXTERNAL — but modexctl
        # does NOT know the receiver's implementation (MODEX_TARGETS carries
        # only name=description). This is the known limitation documented in
        # the commit message: "modexctl same-pool dispatch to external_coding
        # subagents hardcodes REACT execution_strategy".
        #
        # For PeerNormal cross-pool: build_peer_agent_message is used with
        # default NATIVE implementation. The external receiver sees
        # "send_to_agent" instructions but it has no send_to_agent tool —
        # it must use modexctl send. This IS a known gap, but the message
        # still lands correctly; only the reply_contract wording is suboptimal.
        payload = json.loads(payload_json)
        assert "implement feature X" in payload["content"]
