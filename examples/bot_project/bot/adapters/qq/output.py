"""QQ Bot 输出适配器 - V2 架构.

Split from ``bot/adapters/qq.py``. Logic unchanged; only the module boundary
moved.
"""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bot.adapters.qq._ws_state import QQ_FILE_TYPE_IMAGE, _qq_file_type
from bot.adapters.qq.input import QQInputAdapter
from modex_agent.adapters.platform import StreamingMode
from modex_agent.pipeline.adapters import OutputAdapter, OutputMessage
from modex_agent.pipeline.filters import ChainedContentFilter, WhitespaceFilter


class QQOutputAdapter(OutputAdapter):
    """QQ Bot 输出适配器 - V2 架构

    将 Agent 的输出发送到 QQ。
    支持 send_delta() 实现伪流式（缓冲后一次性发送）。
    支持发送附件（图片、文件等）。
    """

    def __init__(self, qq_input_adapter: QQInputAdapter) -> None:
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
            # 1) 先发送附件。优先用结构化 Attachment 记录（name/is_image 来自 ingest
            # 时的魔数分类，ADR-0013 §8），否则回退到旧路径列表。持久化路径的
            # basename 可能是不透明 id（无扩展名），若据此取名/分类，图片会被当成
            # 无名文件发出、后缀丢失。文件名与图片判定都取自记录，不在 adapter 里
            # 维护扩展名表。
            if message.attachment_records:
                media_items: list[tuple[str, str | None, bool | None]] = [
                    (r.path, r.name, r.is_image) for r in message.attachment_records
                ]
            else:
                media_items = [(p, None, None) for p in message.attachments]
            for media_ref, display_name, is_image in media_items:
                ok = await self._send_media(
                    chat_id=chat_id,
                    media_ref=media_ref,
                    msg_id=msg_id,
                    is_group=is_group,
                    display_name=display_name,
                    is_image=is_image,
                )
                if not ok:
                    filename = (
                        display_name
                        or os.path.basename(urlparse(media_ref).path)
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
        *,
        display_name: str | None = None,
        is_image: bool | None = None,
    ) -> bool:
        """读取文件 -> base64 编码 -> 上传 -> msg_type=7 发送。

        *display_name* / *is_image*（来自 Attachment 记录）覆盖路径 basename
        推断：持久化路径的 basename 可能是不透明 id（无扩展名），据此取名会让
        图片被当作无名文件、丢失后缀。
        """
        if not self._qq_input._client:
            return False

        data, read_name = await self._read_media_bytes(media_ref)
        if not data:
            return False
        filename = display_name or read_name
        if not filename:
            return False

        try:
            file_type = _qq_file_type(filename, is_image=is_image)
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

    async def send_stream(self, content_iterator: Any, session_id: str) -> None:
        """发送流式输出到 QQ（兼容性方法）

        QQ 不支持真正的流式，所以我们收集所有内容后一次性发送。
        """
        async for chunk in content_iterator:
            await self.send_delta(chunk, session_id)
        await self.flush_deltas(session_id)
