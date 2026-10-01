from __future__ import annotations

from modex_agent.approval.config import ApprovalConfig


def test_enabled_defaults_to_false():
    """Default-off: merely adding an `approval:` block never silently enables it."""
    cfg = ApprovalConfig()
    assert cfg.enabled is False
