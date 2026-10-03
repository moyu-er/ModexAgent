"""The FW-bundled ``approval`` capability package (W1-B2, ADR-0047).

The complete approval vertical slice: config, classifier runtime,
argument matcher, guard composite, response parsing, UI, views, resumer,
renderer, input stage, slash commands, and the gate factory — one
package, one owner.

Import-light facade contract: importing this package does NOT eagerly
import the bundle's sandbox-coupled implementation modules
(``security``, ``resumer``, ``factory``). Only the registration entry is
importable from here; assembly wiring sites import the implementation
modules inside function bodies at their use sites. An import smoke test
pins this property via ``sys.modules``.
"""

from __future__ import annotations

from modex_agent.plugins.defaults.capabilities.approval.registration import (
    register_approval_feature,
)

__all__ = [
    "register_approval_feature",
]
