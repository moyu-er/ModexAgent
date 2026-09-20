"""Seam contracts for ``modex_agent.acp.backend`` / the open-request models.

The backend seam (DESIGN.md §8) is the SDK-free boundary the bot implements:
``open`` covers new + load with the protocol ``cwd``, the handle drives
prompt/cancel/history/close, and rejections are typed ``AcpBackendError``.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.acp.backend import AcpInteraction, AcpSessionBackend, AcpSessionHandle
from modex_agent.acp.types import (
    AcpBackendError,
    AcpBackendErrorCode,
    AcpOpenKind,
    AcpOpenRequest,
    AcpPromptInput,
)


def test_open_request_new_carries_cwd_as_path() -> None:
    request = AcpOpenRequest(kind=AcpOpenKind.NEW, cwd="D:/projects/my-app")
    assert request.cwd == Path("D:/projects/my-app")
    assert request.session_id is None


def test_open_request_load_requires_session_id() -> None:
    request = AcpOpenRequest(kind=AcpOpenKind.LOAD, cwd="D:/projects/my-app", session_id="s1")
    assert request.session_id == "s1"
    with pytest.raises(ValidationError):
        AcpOpenRequest(kind=AcpOpenKind.LOAD, cwd="D:/projects/my-app")


def test_open_request_new_forbids_session_id() -> None:
    with pytest.raises(ValidationError):
        AcpOpenRequest(kind=AcpOpenKind.NEW, cwd="D:/x", session_id="s1")


def test_prompt_input_is_typed_text() -> None:
    assert AcpPromptInput(text="hello world").text == "hello world"
    with pytest.raises(ValidationError):
        AcpPromptInput(text=123)  # type: ignore[arg-type]


def test_backend_error_carries_typed_code_and_message() -> None:
    error = AcpBackendError(AcpBackendErrorCode.PROJECT_MISMATCH, "bound to D:/other")
    assert error.code is AcpBackendErrorCode.PROJECT_MISMATCH
    assert error.message == "bound to D:/other"
    assert "project_mismatch" in str(error)


def test_backend_error_code_members_match_design_table() -> None:
    assert {code.value for code in AcpBackendErrorCode} == {
        "invalid_cwd",
        "project_mismatch",
        "boot_failed",
        "not_found",
        "unsupported",
        "busy",
    }


def test_seam_is_sdk_free_abc_surface() -> None:
    """The three seam types are ABCs the bot subclasses without the SDK."""
    import inspect

    assert inspect.isabstract(AcpSessionBackend)
    assert inspect.isabstract(AcpSessionHandle)
    assert inspect.isabstract(AcpInteraction)
