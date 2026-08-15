"""Tests for process identity and liveness registry runtime primitives."""

from __future__ import annotations

import logging
import os
import socket

import pytest

from modex_agent.runtime.constants import EXECUTOR_PROCESS_ID_KEY
from modex_agent.runtime.process_identity import ProcessIdentity
from modex_agent.runtime.process_registry import SingletonProcessRegistry


def test_process_identity_is_lazy_stable_and_logs_generation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="modex_agent.runtime.process_identity"):
        identity = ProcessIdentity()
        assert caplog.records == []

        first_process_id = identity.process_id
        second_process_id = identity.process_id

    assert type(first_process_id) is int
    assert first_process_id > 0
    assert second_process_id == first_process_id
    assert [record.getMessage() for record in caplog.records] == [
        (
            "ProcessIdentity generated: "
            f"id={first_process_id}, host={socket.gethostname()}, pid={os.getpid()}"
        )
    ]
    assert caplog.records[0].levelno == logging.INFO


def test_singleton_process_registry_reports_own_process() -> None:
    identity = ProcessIdentity()
    own_process_id = identity.process_id
    registry = SingletonProcessRegistry(identity)

    assert registry.alive_process_ids() == {own_process_id}


def test_executor_process_id_key_is_canonical_attrs_key() -> None:
    assert EXECUTOR_PROCESS_ID_KEY == "executor_process_id"
