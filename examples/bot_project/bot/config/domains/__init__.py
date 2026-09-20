"""Per-domain schema registration.

IM/model keep their existing import-time registration. Personal-assistant
preferences are assembly-owned and injected into ConfigController directly;
they do not register a process-global config path.
"""

from __future__ import annotations
