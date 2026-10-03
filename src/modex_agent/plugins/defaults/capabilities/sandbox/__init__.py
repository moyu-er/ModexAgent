"""The FW-bundled ``sandbox`` capability package (W1-B3, ADR-0047).

The complete sandbox vertical slice: settings, decision service, guard
family, substrate runtimes (bwrap/Seatbelt/OCI), container executor,
shell plan, delegation snapshots, the guard interceptor, the legacy
adapter facade, and the opt-in guard factory — one package, one owner.

Import-light facade contract: importing this package does NOT eagerly
import the bundle's implementation modules (``decision``, ``guard_*``,
``interceptor``, ``runtime``, ``adapters``, …). Only the registration
entry is importable from here; assembly wiring sites import the
implementation modules inside function bodies at their use sites. An
import smoke test pins this property via ``sys.modules``.
"""

from __future__ import annotations

from modex_agent.plugins.defaults.capabilities.sandbox.registration import (
    register_sandbox_feature,
)

__all__ = [
    "register_sandbox_feature",
]
