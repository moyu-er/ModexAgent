"""User-configured timezone — read once from TIMEZONE env var, cached.

When ``TIMEZONE`` is not set the server's local timezone is used.
This is cross-platform (Windows / Linux / macOS).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone as _dt_timezone, timedelta, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


def _local_timezone() -> tzinfo:
    """Return the server's local timezone — cross-platform safe."""
    tz = datetime.now().astimezone().tzinfo
    return tz if tz is not None else _dt_timezone.utc


@lru_cache(maxsize=1)
def _resolve_timezone() -> tzinfo:
    """Resolve the configured timezone, cached on first call."""
    name = os.environ.get("TIMEZONE", "").strip()
    if not name:
        return _local_timezone()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError):
        pass
    # Try offset format: UTC+8, +08:00, +0800, etc.
    if name.upper().startswith("UTC"):
        offset_str = name[3:].lstrip("+")
    elif name.startswith("+"):
        offset_str = name[1:]
    else:
        offset_str = None
    if offset_str is not None:
        try:
            hours_str, _, minutes_str = offset_str.partition(":")
            if not minutes_str:
                minutes_str = "00"
            offset = timedelta(hours=int(hours_str), minutes=int(minutes_str))
            return _dt_timezone(offset, name)
        except (ValueError, TypeError):
            pass
    # Last resort: local timezone
    logger.warning("Unrecognised TIMEZONE=%r, falling back to system local timezone", name)
    return _local_timezone()


def get_user_timezone() -> tzinfo:
    """Return the user-configured timezone.

    Reads ``TIMEZONE`` from environment.  Falls back to the server's local
    timezone when the variable is absent or unrecognised.

    The result is cached after the first call.

    Supported formats:
    - IANA zone name: ``Asia/Shanghai``, ``America/New_York``, ``UTC``
    - UTC offset: ``UTC+8``, ``+08:00``
    - Empty / missing: server local timezone (cross-platform)
    """
    return _resolve_timezone()
