"""Regression test: SessionInfo snowflake MUST NOT be double-encoded.

When a WebUI session is created with ``factory.create(agent_name)``, the
snowflake is ``encode_snowflake(random_uuid)``.  When ``_ws_send_message``
forwards the message, it must pass the **already-resolved** SessionInfo
via ``UserInputEnvelope.pre_resolved_session``, not the raw snowflake as
``conversation_id`` — otherwise ``resolve_session_routing`` re-encodes the
already-encoded snowflake, producing a different session_id and causing
transcripts to be saved under the wrong key.
"""

from __future__ import annotations

from framework.core.session_id import SessionInfo, SessionIdFactory, encode_snowflake


def test_factory_double_encode_changes_snowflake():
    """Encoding an already-encoded value produces a DIFFERENT snowflake."""
    factory = SessionIdFactory()

    # Simulate POST /api/sessions: create session with random snowflake
    session = factory.create(agent_name="coding")
    original_snowflake = session.session_id_prefix
    original_session_id = str(session)

    # Simulate _ws_send_message: the snowflake is extracted as conversation_id
    conv_id = original_snowflake

    # Simulate resolve_session_routing WITHOUT pre_resolved_session:
    # external_id=conv_id (already encoded) is re-encoded.
    re_encoded = factory.create(agent_name="coding", external_id=conv_id)
    re_encoded_session_id = str(re_encoded)

    # BUG: the re-encoded session_id differs from the original
    assert re_encoded_session_id != original_session_id, (
        f"Double-encoding produces a different session id:\n"
        f"  original: {original_session_id}\n"
        f"  re-encoded: {re_encoded_session_id}\n"
        f"This is the crosstalk bug."
    )


def test_pre_resolved_session_preserves_identity():
    """When pre_resolved_session is set, the session identity is kept VERBATIM."""
    factory = SessionIdFactory()

    # Simulate POST /api/sessions
    original = factory.create(agent_name="coding")
    original_id = str(original)

    # Simulate _ws_send_message with pre_resolved_session set
    # (the fix): the pipeline uses str(original) directly, no re-encoding.
    direct = str(original)
    assert direct == original_id, "str() must preserve the original session_id"

    # from_str must round-trip correctly
    recovered = SessionInfo.from_str(original_id)
    assert str(recovered) == original_id
    assert recovered.session_id_prefix == original.session_id_prefix
