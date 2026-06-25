"""The inbox MQ package must not depend on or re-export application-layer hooks."""

import modex_agent.multi_agent.inbox as inbox_pkg


def test_inbox_package_does_not_reexport_inbox_flush_hook() -> None:
    assert not hasattr(inbox_pkg, "InboxFlushHook")
    assert "InboxFlushHook" not in getattr(inbox_pkg, "__all__", [])
