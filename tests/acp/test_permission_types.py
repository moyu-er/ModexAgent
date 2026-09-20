"""Permission value-object contracts (migrated from ``test_approval_bridge``).

``PendingApprovalBatch`` / ``ResolvedPermissionDecision`` / the always-kind
option members were deleted with ``approval_bridge`` — the ACP layer no longer
models approval batches (ADR-0011 batch atomicity lives in the framework's
``ApprovalTransaction``; the production driver applies decisions through the
pool's resume loop). What remains ACP-owned is the once-only permission
surface: the default option pair, the prompt's offered options, and the
cancel-means-no-option choice. Behavioral round-trips (allow → executed,
reject → denied) live in ``test_scripted.py``; server-side option validation
lives in ``test_server.py``.
"""

import pytest
from pydantic import ValidationError

from modex_agent.acp.types import (
    DEFAULT_PERMISSION_OPTIONS,
    AcpPermissionOption,
    PermissionChoice,
    PermissionPrompt,
)


def test_default_options_advertise_only_the_accepted_once_kinds() -> None:
    """Conservative surface (DESIGN.md §6.3): allow_once / reject_once only."""
    assert DEFAULT_PERMISSION_OPTIONS == (
        AcpPermissionOption.ALLOW_ONCE,
        AcpPermissionOption.REJECT_ONCE,
    )


def test_permission_option_members_are_once_only() -> None:
    assert {option.value for option in AcpPermissionOption} == {"allow_once", "reject_once"}
    with pytest.raises(ValueError):
        AcpPermissionOption("allow_always")


def test_prompt_defaults_to_the_once_only_option_pair() -> None:
    prompt = PermissionPrompt(
        approval_id="approval-1", tool_call_id="c1", tool_name="Bash", title="Bash (NORMAL)"
    )
    assert prompt.options == DEFAULT_PERMISSION_OPTIONS
    assert prompt.approval_id == "approval-1"
    assert prompt.tool_call_id == "c1"
    assert prompt.tool_name == "Bash"
    assert prompt.title == "Bash (NORMAL)"


def test_prompt_rejects_options_outside_the_once_only_kind_set() -> None:
    with pytest.raises(ValidationError):
        PermissionPrompt(
            approval_id="approval-1",
            tool_call_id="c1",
            tool_name="Bash",
            title="Bash",
            options=(AcpPermissionOption.ALLOW_ONCE, "allow_always"),  # type: ignore[arg-type]
        )


def test_cancelled_choice_is_no_option() -> None:
    """A client cancel arrives as ``option=None`` — never an implicit allow."""
    assert PermissionChoice(option=None).option is None
    assert PermissionChoice(option=AcpPermissionOption.ALLOW_ONCE).option is not None
