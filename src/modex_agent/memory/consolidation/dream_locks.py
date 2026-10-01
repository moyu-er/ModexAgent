"""DreamEngine concurrency-execution locks.

Concurrent runs are isolated per scope key
(session_id:user_id:tenant_id), ensuring the DreamEngine for the same
scope never executes concurrently. The pipeline takes the lock when
triggering the background DreamEngine long-term-memory consolidation,
so one session is never processed by several concurrent turns at once.
"""

import asyncio

_dream_locks: dict[str, asyncio.Lock] = {}
