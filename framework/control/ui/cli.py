"""CLIUserInterface — 终端命令行交互。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from framework.control.ui.abc import ControlUserInterface


class CLIUserInterface(ControlUserInterface):
    """终端交互。render_question 用 print + input 同步阻塞。"""

    async def render_message(
        self,
        session_id: str,
        content: str,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        print(content)
        return ""

    async def render_question(
        self,
        session_id: str,
        question: str,
        options: Sequence[str],
        timeout: float,
        metadata: Mapping[str, object] | None = None,
    ) -> str | None:
        # NOTE: input() is synchronous blocking — the timeout parameter is
        # NOT enforced here. Callers must implement their own timeout
        # (e.g. via asyncio.wait_for or ControlWaitStrategy).
        print(question)
        prompt = f"[{'/'.join(options)}]: "
        try:
            answer = input(prompt).strip().lower()
            if answer in options:
                return answer
            return None
        except (EOFError, KeyboardInterrupt):
            return None

    async def update_message(
        self,
        session_id: str,
        message_id: str,
        content: str,
    ) -> None:
        print(content)  # CLI: print update inline
