"""NoopUserInterface — 无用户界面（cron/headless）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from framework.control.ui.abc import ControlUserInterface


class NoopUserInterface(ControlUserInterface):
    """无用户界面。所有消息丢弃，所有问题返回 None。"""

    async def render_message(
        self,
        session_id: str,
        content: str,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        return ""

    async def render_question(
        self,
        session_id: str,
        question: str,
        options: Sequence[str],
        timeout: float,
        metadata: Mapping[str, object] | None = None,
    ) -> str | None:
        return None

    async def update_message(
        self,
        session_id: str,
        message_id: str,
        content: str,
    ) -> None:
        pass
