"""DreamEngine 并发执行锁。

按 scope 键（session_id:user_id:tenant_id）隔离并发运行，
确保同一 scope 的 DreamEngine 不会同时执行。Pipeline 在触发
后台 DreamEngine 整理长期记忆时加锁，避免同一 session 被
多个并发 turn 同时处理。
"""

import asyncio

_dream_locks: dict[str, asyncio.Lock] = {}
