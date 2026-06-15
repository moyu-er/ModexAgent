"""QQ Bot V2 Adapters - 使用新架构 InputAdapter/OutputAdapter

基于 Agent Framework V2 架构的 QQ Bot 适配器实现。
使用 AgentPipeline 替代 MessageBus 架构。

新增功能：
- 支持接收 QQ 附件（图片、文件等），下载到本地 media 目录
- 支持发送文件到 QQ（msg_type=7 + base64 上传）
- 支持 markdown 消息格式
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import mimetypes
import os
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bot.utils.media_utils import download_file
from framework.adapters.platform import StreamingMode
from framework.agents.react import ReActEvent
from framework.core.emitter import EmitterConfig, StreamingAwareEmitter
from framework.pipeline.adapters import (
    InputAdapter,
    InputMessage,
    OutputAdapter,
    OutputMessage,
)
from framework.pipeline.filters import ChainedContentFilter, WhitespaceFilter

# QQ rich media file_type: 1=image, 4=file
QQ_FILE_TYPE_IMAGE = 1
QQ_FILE_TYPE_FILE = 4

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ico", ".svg"}


def _guess_send_file_type(filename: str) -> int:
    """判断发送文件的类型：图片 -> 1，其他 -> 4。"""
    ext = Path(filename).suffix.lower()
    mime, _ = mimetypes.guess_type(filename)
    if ext in _IMAGE_EXTS or (mime and mime.startswith("image/")):
        return QQ_FILE_TYPE_IMAGE
    return QQ_FILE_TYPE_FILE


class QQInputAdapter(InputAdapter):
    """QQ Bot 输入适配器 - V2 架构

    使用腾讯官方 botpy SDK 接收 QQ 消息。
    支持接收附件（图片、文件等）。
    """

    def __init__(
        self,
        app_id: str,
        secret: str,
        sandbox: bool = False,
        allow_from: list | None = None,
        media_dir: str | None = None,
    ):
        super().__init__()
        self.app_id = app_id
        self.secret = secret
        self.sandbox = sandbox
        self.allow_from = allow_from or []

        self._client = None
        self._running = False
        self._bot_task: asyncio.Task | None = None
        self._processed_ids: deque = deque(maxlen=1000)
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._botpy = None
        self.last_input_metadata: dict[str, Any] = {}

        # 媒体文件保存目录
        self._media_dir = (
            Path(media_dir)
            if media_dir
            else Path(__file__).resolve().parent.parent.parent / "data" / "media" / "qq"
        )
        self._media_dir.mkdir(parents=True, exist_ok=True)

        self._input_pipeline = None
        self._input_ctx = None
        self._output_adapter = None

    @property
    def name(self) -> str:
        return "qq"

    def _get_botpy(self):
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
            def __init__(inner_self):  # noqa: N805
                super().__init__(intents=intents)

            async def on_ready(inner_self):  # noqa: N805
                print(f"[QQInputAdapter] Ready: {inner_self.robot.name}")

            async def on_c2c_message_create(inner_self, message):  # noqa: N805
                await self._on_message(message, is_group=False)

            async def on_group_at_message_create(inner_self, message):  # noqa: N805
                await self._on_message(message, is_group=True)

            async def on_direct_message_create(inner_self, message):  # noqa: N805
                await self._on_message(message, is_group=False)

        self._client = _Bot()

        # Start bot in background task
        self._bot_task = asyncio.create_task(self._run_bot())
        print(f"[QQInputAdapter] Started, media_dir={self._media_dir}")

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

        print("[QQInputAdapter] Stopped")

    def put_input_message(self, msg: InputMessage) -> None:
        """Push a fully-built InputMessage onto the receive queue.
        S8 EnqueueStage calls this via ctx.enqueue_message."""
        self._message_queue.put_nowait(msg)

    async def receive(self):
        """接收输入消息（异步迭代器）"""
        while self._running:
            try:
                # 等待消息，带超时以便检查运行状态
                message = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
                yield message
            except TimeoutError:
                continue

    async def _on_message(self, data, is_group: bool = False) -> None:
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

            # 下载附件
            attachments: list[str] = []
            recv_lines: list[str] = []
            att_meta: list[dict[str, Any]] = []
            raw_attachments = getattr(data, "attachments", None) or []

            if raw_attachments:
                for att in raw_attachments:
                    url = getattr(att, "url", None) or ""
                    filename = getattr(att, "filename", None) or ""
                    if url:
                        print(f"[QQInputAdapter] Downloading attachment: {filename or url}")
                        local_path = await download_file(
                            url=url,
                            dest_dir=self._media_dir,
                            filename_hint=filename,
                        )
                        if local_path:
                            attachments.append(local_path)
                            shown_name = filename or os.path.basename(local_path)
                            recv_lines.append(f"- {shown_name}\n  saved: {local_path}")
                            print(f"[QQInputAdapter] Saved attachment: {local_path}")
                        else:
                            shown_name = filename or url
                            recv_lines.append(f"- {shown_name}\n  saved: [download failed]")
                        att_meta.append(
                            {
                                "url": url,
                                "filename": filename,
                                "saved_path": local_path,
                            }
                        )

            # 将附件信息附加到 content
            if recv_lines:
                tag = (
                    "[Image]"
                    if any(Path(p).suffix.lower() in _IMAGE_EXTS for p in attachments)
                    else "[File]"
                )
                file_block = "Received files:\n" + "\n".join(recv_lines)
                content = f"{content}\n\n{file_block}" if content else f"{tag}\n{file_block}"

            if not content and not attachments:
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
                "conversation_id": user_id,
                "is_group": is_group,
                "chat_id": chat_id,
            }
            if channel_id:
                metadata["channel"] = str(channel_id)
            if att_meta:
                metadata["attachments"] = att_meta

            # 记录最近一次输入 metadata，供 OutputAdapter 区分 C2C / 群聊
            self.last_input_metadata = metadata

            # ── S0: produce the seed envelope (adapter normalization done above) ──
            from framework.input_pipeline.envelope import UserInputEnvelope, AttachmentRef

            seed = UserInputEnvelope(
                conversation_id=user_id,
                content=content,
                channel=self.name,            # "qq"
                explicit_pool=None,
                metadata={
                    "message_id": data.id,
                    "is_group": is_group,
                    "chat_id": chat_id,
                    "conversation_id": user_id,
                },
                attachments=[AttachmentRef(local_path=p) for p in attachments],
            )
            result = await self._input_pipeline.handle(seed, self._input_ctx)

            # ── Surface Terminate responses (pool switch, invalid skill, etc.) ──
            if not result.should_continue():
                response = getattr(result, "response", None)
                if response and isinstance(response, dict):
                    msg_text = str(response.get("message", ""))
                    if msg_text:
                        out = self._output_adapter or self._ctrl_output_adapter
                        if out is not None:
                            from framework.core.types import OutputMessage
                            await out.send(OutputMessage(content=msg_text), user_id)
                        else:
                            print(f"[QQInputAdapter] Cannot send Terminate notice (no output adapter): {msg_text}")
            # continue path: S8 already enqueued via ctx.enqueue_message (= put_input_message)

        except Exception as e:
            print(f"[QQInputAdapter] Error handling message: {e}")
            import traceback

            traceback.print_exc()


class QQOutputAdapter(OutputAdapter):
    """QQ Bot 输出适配器 - V2 架构

    将 Agent 的输出发送到 QQ。
    支持 send_delta() 实现伪流式（缓冲后一次性发送）。
    支持发送附件（图片、文件等）。
    """

    def __init__(self, qq_input_adapter: QQInputAdapter):
        self._qq_input = qq_input_adapter
        self._delta_buffers: dict[str, list[str]] = {}  # 用于流式输出的缓冲
        self.content_filter = ChainedContentFilter(
            [
                WhitespaceFilter(),
            ]
        )
        self._msg_seq: int = 1  # 用于避免 QQ API 去重

    @property
    def name(self) -> str:
        return "qq"

    @property
    def streaming_mode(self) -> StreamingMode:
        """QQ 不支持真流式，只支持缓冲后一次性发送"""
        return StreamingMode.PSEUDO

    async def send(self, message: OutputMessage, session_id: str) -> None:
        """发送完整消息到 QQ（支持 C2C 私聊和群聊自动路由，支持附件）"""
        if not self._qq_input._client:
            print("[QQOutputAdapter] Client not initialized")
            return

        message = await self._apply_filter(message)
        content = message.content or ""

        # 获取原始消息和聊天类型
        raw_msg = self._qq_input.last_input_metadata.get("raw_message")
        is_group = self._qq_input.last_input_metadata.get("is_group", False)
        chat_id = self._qq_input.last_input_metadata.get("chat_id", session_id)
        msg_id = self._qq_input.last_input_metadata.get("message_id")

        # 获取 group_openid（群聊消息使用 group_openid 作为 chat_id）
        group_openid = getattr(raw_msg, "group_openid", None) if raw_msg else None
        if group_openid:
            is_group = True
            chat_id = group_openid

        try:
            # 1) 先发送附件
            for media_ref in message.attachments:
                ok = await self._send_media(
                    chat_id=chat_id,
                    media_ref=media_ref,
                    msg_id=msg_id,
                    is_group=is_group,
                )
                if not ok:
                    filename = (
                        os.path.basename(urlparse(media_ref).path)
                        or os.path.basename(media_ref)
                        or "file"
                    )
                    await self._send_text_only(
                        chat_id=chat_id,
                        is_group=is_group,
                        msg_id=msg_id,
                        content=f"[附件发送失败: {filename}]",
                    )

            # 2) 发送文本内容
            if content and content.strip() and content != "（无回复内容）":
                use_markdown = message.message_type == "markdown"
                await self._send_text_only(
                    chat_id=chat_id,
                    is_group=is_group,
                    msg_id=msg_id,
                    content=content.strip(),
                    use_markdown=use_markdown,
                )
        except Exception as e:
            print(f"[QQOutputAdapter] Error sending message: {e}")
            import traceback

            traceback.print_exc()

    async def _send_text_only(
        self,
        chat_id: str,
        is_group: bool,
        msg_id: str | None,
        content: str,
        use_markdown: bool = False,
    ) -> None:
        """发送纯文本消息。"""
        if not self._qq_input._client:
            return

        self._msg_seq += 1
        payload: dict[str, Any] = {
            "msg_type": 2 if use_markdown else 0,
            "msg_id": msg_id,
            "msg_seq": self._msg_seq,
        }
        if use_markdown:
            payload["markdown"] = {"content": content}
        else:
            payload["content"] = content

        if is_group:
            await self._qq_input._client.api.post_group_message(
                group_openid=chat_id,
                **payload,
            )
            print(f"[QQOutputAdapter] Sent to group {chat_id}: {content[:50]}...")
        else:
            await self._qq_input._client.api.post_c2c_message(
                openid=chat_id,
                **payload,
            )
            print(f"[QQOutputAdapter] Sent to {chat_id}: {content[:50]}...")

    async def _send_media(
        self,
        chat_id: str,
        media_ref: str,
        msg_id: str | None,
        is_group: bool,
    ) -> bool:
        """读取文件 -> base64 编码 -> 上传 -> msg_type=7 发送。"""
        if not self._qq_input._client:
            return False

        data, filename = await self._read_media_bytes(media_ref)
        if not data or not filename:
            return False

        try:
            file_type = _guess_send_file_type(filename)
            file_data_b64 = base64.b64encode(data).decode()

            media_obj = await self._post_base64file(
                chat_id=chat_id,
                is_group=is_group,
                file_type=file_type,
                file_data=file_data_b64,
                file_name=filename,
                srv_send_msg=False,
            )
            if not media_obj:
                print("[QQOutputAdapter] Media upload failed: empty response")
                return False

            self._msg_seq += 1
            payload: dict[str, Any] = {
                "msg_type": 7,
                "msg_id": msg_id,
                "msg_seq": self._msg_seq,
                "media": media_obj,
            }
            if is_group:
                await self._qq_input._client.api.post_group_message(
                    group_openid=chat_id,
                    **payload,
                )
            else:
                await self._qq_input._client.api.post_c2c_message(
                    openid=chat_id,
                    **payload,
                )

            print(f"[QQOutputAdapter] Media sent: {filename}")
            return True
        except Exception as e:
            print(f"[QQOutputAdapter] Send media failed: {filename} err={e}")
            return False

    async def _read_media_bytes(self, media_ref: str) -> tuple[bytes | None, str | None]:
        """从本地路径或 URL 读取文件字节。"""
        media_ref = (media_ref or "").strip()
        if not media_ref:
            return None, None

        # 本地文件（排除 file:// 协议，避免 Path 处理不确定行为）
        if (
            not media_ref.startswith("http://")
            and not media_ref.startswith("https://")
            and not media_ref.startswith("file://")
        ):
            try:
                local_path = Path(os.path.expanduser(media_ref))
                if not local_path.is_file():
                    print(f"[QQOutputAdapter] Media file not found: {local_path}")
                    return None, None
                data = await asyncio.to_thread(local_path.read_bytes)
                return data, local_path.name
            except Exception as e:
                print(f"[QQOutputAdapter] Media read error: {media_ref} err={e}")
                return None, None

        # 远程 URL
        try:
            from urllib.request import urlopen

            def _fetch() -> tuple[bytes, str]:
                with urlopen(media_ref, timeout=60) as resp:  # noqa: S310
                    return resp.read(), os.path.basename(urlparse(media_ref).path) or "file.bin"

            return await asyncio.to_thread(_fetch)
        except Exception as e:
            print(f"[QQOutputAdapter] Media download error: {media_ref} err={e}")
            return None, None

    async def _post_base64file(
        self,
        chat_id: str,
        is_group: bool,
        file_type: int,
        file_data: str,
        file_name: str | None = None,
        srv_send_msg: bool = False,
    ) -> dict[str, Any] | None:
        """上传 base64 编码的文件并返回 Media 对象。"""
        if not self._qq_input._client:
            return None

        try:
            from botpy.http import Route
        except ImportError:
            print("[QQOutputAdapter] botpy not available")
            return None

        if is_group:
            endpoint = "/v2/groups/{group_openid}/files"
            id_key = "group_openid"
        else:
            endpoint = "/v2/users/{openid}/files"
            id_key = "openid"

        payload: dict[str, Any] = {
            id_key: chat_id,
            "file_type": file_type,
            "file_data": file_data,
            "srv_send_msg": srv_send_msg,
        }
        # 非图片类型才传 file_name，否则 QQ 客户端会将图片渲染为附件而非内联显示
        if file_type != QQ_FILE_TYPE_IMAGE and file_name:
            payload["file_name"] = file_name

        try:
            route = Route("POST", endpoint, **{id_key: chat_id})
            result = await self._qq_input._client.api._http.request(route, json=payload)

            if isinstance(result, dict) and "file_info" in result:
                return {"file_info": result["file_info"]}
            return result if isinstance(result, dict) else None
        except Exception as e:
            print(f"[QQOutputAdapter] Base64 upload failed: {e}")
            return None

    async def send_delta(
        self, delta: str, session_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """发送流式增量（QQ 伪流式：缓冲到内存，不立即发送）

        QQ 不支持真正的流式传输，所以我们缓冲所有增量，
        直到 flush_deltas() 被调用时才一次性发送。

        Args:
            delta: 内容片段
            session_id: 会话ID
            metadata: 可选元数据（如 reasoning 标记等，QQ 暂不支持）
        """
        if not delta:
            return

        if session_id not in self._delta_buffers:
            self._delta_buffers[session_id] = []
        self._delta_buffers[session_id].append(delta)

    async def flush_deltas(self, session_id: str) -> None:
        """刷新缓冲区，发送收集的内容

        将缓冲的所有增量合并后一次性发送到 QQ。
        """
        if session_id not in self._delta_buffers:
            return

        content = "".join(self._delta_buffers[session_id])
        if content:
            await self.send(OutputMessage(content=content), session_id)

        # 清理缓冲区
        del self._delta_buffers[session_id]

    async def send_stream(self, content_iterator, session_id: str) -> None:
        """发送流式输出到 QQ（兼容性方法）

        QQ 不支持真正的流式，所以我们收集所有内容后一次性发送。
        """
        async for chunk in content_iterator:
            await self.send_delta(chunk, session_id)
        await self.flush_deltas(session_id)


class QQEmitterConfig:
    """QQ Bot 的 Emitter 配置工厂"""

    @staticmethod
    def minimal() -> EmitterConfig:
        """最小配置 - 接收模型内容、工具调用日志和最终结果"""
        return EmitterConfig(
            enabled_events={
                "model_output",
                "tool_call_start",
                "tool_call_end",
                "final_output",
                "error",
            }
        )

    @staticmethod
    def with_tools() -> EmitterConfig:
        """带工具调用配置"""
        return EmitterConfig(
            enabled_events={
                "model_output",
                "tool_call_start",
                "tool_call_end",
                "final_output",
                "error",
            }
        )

    @staticmethod
    def debug() -> EmitterConfig:
        """调试配置 - 接收所有事件"""
        return EmitterConfig()  # 默认启用所有

    @staticmethod
    def custom(enabled: set | None = None, disabled: set | None = None) -> EmitterConfig:
        """自定义配置"""
        return EmitterConfig(
            enabled_events=enabled,
            disabled_events=disabled or set(),
        )


class QQBotEmitter(StreamingAwareEmitter[ReActEvent]):
    """QQ Bot 事件处理器

    业务逻辑：
    - 模型内容：通过 emit_delta 缓冲/发送给用户
    - 思维链：只记日志，不发用户
    - 工具调用：记录到日志，不发给用户
    """

    async def _on_event(self, event: ReActEvent, data: Any = None) -> None:
        """处理业务事件。

        MODEL_OUTPUT 的内容传输由 emit_delta/emit_content 负责，
        此处不重复处理。日志记录后交由基类完成缓冲、flush 和错误发送。
        """
        event_name = event.value if isinstance(event, Enum) else str(event)

        if event_name == "model_reasoning":
            logging.getLogger("bot.reasoning").info(f"[Reasoning] {data}")
        elif event_name == "tool_call_start":
            logging.getLogger("bot.tools").info(f"[Tool Call] {data}")
        elif event_name == "tool_call_end":
            logging.getLogger("bot.tools").info(f"[Tool Result] {data}")

        # 基类负责 reasoning 缓存、final_output flush、error 发送等通用逻辑
        await super()._on_event(event, data)
