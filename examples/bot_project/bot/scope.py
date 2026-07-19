"""Bot-project RecordScope subclass with the pool dimension.

The framework's :class:`modex_agent.core.scope.RecordScope` carries the 11
framework-managed isolation dimensions. The bot project adds a 12th business
dimension — ``pool`` — via this subclass, so framework-managed and
business-scoped records land in separate storage buckets by construction.

A :class:`BotRecordScope` with non-``None`` ``pool`` produces different
canonical JSON (and therefore a different ``scope_key``) than a base
:class:`RecordScope` — this is intentional (ADR-0028).
"""

from __future__ import annotations

from modex_agent.core.scope import RecordScope


class BotRecordScope(RecordScope):
    """Business-layer :class:`RecordScope` adding the bot's pool isolation.

    ``pool`` is the bot's per-pool routing dimension (e.g. ``"default"``,
    ``"coder"``). Framework code never reads or sets ``pool``; only the bot
    project constructs :class:`BotRecordScope` instances.
    """

    pool: str | None = None


__all__ = ["BotRecordScope"]
