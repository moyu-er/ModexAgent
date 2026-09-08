"""QQ Bot 输入适配器 - V2 架构.

Split from ``bot/adapters/qq.py``. Logic unchanged; ``download_file`` is
resolved through the ``bot.adapters.qq`` package namespace at call time so
that ``monkeypatch.setattr(bot.adapters.qq, "download_file", ...)`` continues
to affect ``_on_message`` (the test in
``tests/input_pipeline/test_qq_inbound_attachment.py`` relies on this).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import os
import tempfile
from collections import deque
from pathlib import Path
from typing import Any

import bot.adapters.qq as _qq_pkg
from bot.adapters.qq._ws_state import (
    _CHANNEL_WS_FILE,
    _REGISTRY_DIR,
    _read_channel_ws,
    _write_channel_ws,
)
from modex_agent.input_pipeline.envelope import AttachmentRef, UserInputEnvelope
from modex_agent.pipeline.adapters import (
    InputAdapter,
    InputMessage,
)


class QQInputAdapter(InputAdapter):
    """QQ Bot 输入适配器 - V2 架构

    使用腾讯官方 botpy SDK 接收 QQ 消息。
    支持接收附件（图片、文件等）。
    """

    def __init__(
        self,
        app_id: str,
        secret: str,
        allow_from: list | None = None,
        project_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self.app_id = app_id
        self.secret = secret
        self.allow_from = allow_from or []

        self._client = None
        self._running = False
        self._bot_task: asyncio.Task | None = None
        self._processed_ids: deque = deque(maxlen=1000)
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._botpy = None
        self.last_input_metadata: dict[str, Any] = {}

        # 入站附件的临时下载目录：ingest stage 读取字节后这些临时文件可丢弃。
        # 生命周期与适配器一致（stop() 清理）。
        self._inbound_tmp = tempfile.TemporaryDirectory(prefix="qq_inbound_")

        self._input_pipeline = None
        self._input_ctx = None
        self._output_adapter = None

        # Per-channel workspace persistence
        self._project_dir: Path = project_dir or Path.cwd()
        self._channel_ws_path: Path = (
            self._project_dir / ".modex" / _REGISTRY_DIR / _CHANNEL_WS_FILE
        )
        self.home = self._project_dir
        self.current_ws: Path = _read_channel_ws(
            self._channel_ws_path, self.name, self._project_dir
        )

    @property
    def name(self) -> str:
        return "qq"

    def save_current_ws(self) -> None:
        """Persist current_ws to channel_ws.json atomically."""
        data: dict[str, str] = {}
        if self._channel_ws_path.is_file():
            try:
                with open(self._channel_ws_path, encoding="utf-8") as f:
                    raw = json.load(f)
                try:
                    data = raw
                    _ = raw.get("__probe")
                except AttributeError:
                    data = {}
            except (json.JSONDecodeError, OSError):
                pass
        data[self.name] = str(self.current_ws)
        _write_channel_ws(self._channel_ws_path, data)

    def _get_botpy(self) -> Any:
        """Lazy import botpy"""
        if self._botpy is not None:
            return self._botpy

        try:
            import botpy

            self._botpy = botpy
            return botpy
        except ImportError as err:
            raise ImportError(
                "QQInputAdapter requires qq-botpy. Install with: pip install qq-botpy"
            ) from err

    def is_allowed(self, sender_id: str) -> bool:
        """检查发送者是否被允许"""
        if not self.allow_from or "*" in self.allow_from:
            return True
        return sender_id in self.allow_from

    async def start(self) -> None:
        """启动 QQ Bot 连接"""
        botpy = self._get_botpy()

        if not self.app_id or not self.secret:
            raise ValueError("QQ app_id and secret must be configured")

        self._running = True

        # Create bot client
        intents = botpy.Intents(public_messages=True, direct_message=True)

        class _Bot(botpy.Client):
            def __init__(inner_self) -> None:  # noqa: N805
                super().__init__(intents=intents)

            async def on_ready(inner_self) -> None:  # noqa: N805
                print(f"[QQInputAdapter] Ready: {inner_self.robot.name}")

            async def on_c2c_message_create(inner_self, message: Any) -> None:  # noqa: N805
                await self._on_message(message, is_group=False)

            async def on_group_at_message_create(inner_self, message: Any) -> None:  # noqa: N805
                await self._on_message(message, is_group=True)

            async def on_direct_message_create(inner_self, message: Any) -> None:  # noqa: N805
                await self._on_message(message, is_group=False)

        self._client = _Bot()

        # Start bot in background task
        self._bot_task = asyncio.create_task(self._run_bot())
        print("[QQInputAdapter] Started")

    async def _run_bot(self) -> None:
        """Run bot with auto-reconnect.

        Catches BaseException (SystemExit, GeneratorExit, etc.) from the
        botpy SDK to prevent SDK internal errors from killing the event
        loop.  Only CancelledError (graceful shutdown) and KeyboardInterrupt
        are allowed to propagate.
        """
        while self._running:
            try:
                await self._client.start(
                    appid=self.app_id,
                    secret=self.secret,
                )
            except asyncio.CancelledError:
                raise
            except KeyboardInterrupt:
                raise
            except BaseException as e:
                # botpy SDK may throw SystemExit / GeneratorExit on fatal
                # internal errors — reconnect instead of letting it kill
                # the process.
                print(f"[QQInputAdapter] SDK error ({type(e).__name__}): {e}")

            if self._running:
                print("[QQInputAdapter] Reconnecting in 5 seconds...")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """停止 QQ Bot 连接"""
        self._running = False

        if self._bot_task:
            self._bot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bot_task

        # 入站附件临时下载目录清理（ingest stage 已持久化接受的字节）
        with contextlib.suppress(Exception):
            self._inbound_tmp.cleanup()

        print("[QQInputAdapter] Stopped")

    def put_input_message(self, msg: InputMessage) -> None:
        """Push a fully-built InputMessage onto the receive queue.
        S8 EnqueueStage calls this via ctx.enqueue_message."""
        self._message_queue.put_nowait(msg)

    async def receive(self) -> Any:
        """接收输入消息（异步迭代器）"""
        while self._running:
            try:
                # 等待消息，带超时以便检查运行状态
                message = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
                yield message
            except TimeoutError:
                continue

    async def _on_message(self, data: Any, is_group: bool = False) -> None:
        """处理来自 QQ 的消息"""
        try:
            # 去重
            if data.id in self._processed_ids:
                return
            self._processed_ids.append(data.id)

            # 获取用户信息
            author = data.author
            user_id = str(getattr(author, "id", None) or getattr(author, "user_openid", "unknown"))

            # 检查是否允许
            if not self.is_allowed(user_id):
                return

            # 获取内容
            content = (data.content or "").strip()

            # 附件只下载到临时位置并产出 AttachmentRef —— 与 webui 完全一致
            # (ADR-0013 §12)。适配器不做任何字符串拼接进 content：gate / persist /
            # record 由共享 AttachmentIngestStage 负责，agent 感知是 preprocess 的
            # 瞬态 path-reference 注入（只进记忆，不进会话历史/transcript）。拼接
            # 会把临时路径写进 transcript+memory，且该临时文件在 stop() 后即被删除，
            # 留下悬挂引用。
            attachment_refs: list[AttachmentRef] = []
            raw_attachments = getattr(data, "attachments", None) or []

            inbound_tmp_dir = Path(self._inbound_tmp.name)

            if raw_attachments:
                for att in raw_attachments:
                    url = getattr(att, "url", None) or ""
                    filename = getattr(att, "filename", None) or ""
                    if url:
                        print(f"[QQInputAdapter] Downloading attachment: {filename or url}")
                        local_path = await _qq_pkg.download_file(
                            url=url,
                            dest_dir=inbound_tmp_dir,
                            filename_hint=filename,
                        )
                        if local_path:
                            shown_name = filename or os.path.basename(local_path)
                            sniffed_mime, _ = mimetypes.guess_type(shown_name)
                            attachment_refs.append(
                                AttachmentRef(
                                    local_path=local_path,
                                    filename=shown_name,
                                    mime_type=sniffed_mime,
                                )
                            )
                            print(f"[QQInputAdapter] Saved attachment: {local_path}")
                        else:
                            print(f"[QQInputAdapter] Download failed: {filename or url}")

            if not content and not attachment_refs:
                return

            print(f"[QQInputAdapter] Received from {user_id}: {content[:80]}...")

            # 确定 chat_id
            if is_group:
                chat_id = str(getattr(data, "group_openid", "unknown"))
                channel_id = chat_id
            else:
                chat_id = user_id
                channel_id = getattr(data, "channel_id", None) or chat_id

            # 提取运行时上下文（IM Bot 通用字段）
            metadata: dict[str, Any] = {
                "message_id": data.id,
                "raw_message": data,
                "user_id": user_id,
                "session_id": user_id,
                "is_group": is_group,
                "chat_id": chat_id,
            }
            if channel_id:
                metadata["channel"] = str(channel_id)

            # 记录最近一次输入 metadata，供 OutputAdapter 区分 C2C / 群聊
            self.last_input_metadata = metadata

            # ── S0: produce the seed envelope (adapter normalization done above) ──
            seed = UserInputEnvelope(
                external_id=user_id,
                content=content,
                channel=self.name,            # "qq"
                explicit_pool=None,
                metadata={
                    "message_id": data.id,
                    "is_group": is_group,
                    "chat_id": chat_id,
                    "session_id": user_id,
                },
                attachments=attachment_refs,
            )
            result = await self._input_pipeline.handle(seed, self._input_ctx)

            # ── Surface Terminate responses (pool switch, invalid skill, etc.) ──
            if not result.should_continue():
                response = result.response
                msg_text = ""
                if response is not None:
                    try:
                        msg_text = str(response.get("message", ""))
                    except AttributeError:
                        msg_text = ""
                if msg_text:
                    out = self._output_adapter or self._ctrl_output_adapter
                    if out is not None:
                        from modex_agent.messaging.models import OutputMessage
                        await out.send(OutputMessage(content=msg_text), user_id)
                    else:
                        print(f"[QQInputAdapter] Cannot send Terminate notice (no output adapter): {msg_text}")
            # continue path: S8 already enqueued via ctx.enqueue_message (= put_input_message)

        except Exception as e:
            print(f"[QQInputAdapter] Error handling message: {e}")
            import traceback

            traceback.print_exc()
