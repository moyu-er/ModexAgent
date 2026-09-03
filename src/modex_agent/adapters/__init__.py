"""Adapters — platform I/O contracts and emitter bridge.

Facade (ADR-0005): ``StreamingMode`` / ``PlatformAdapter`` / ``AdapterRegistry``
(platform), ``OutputAdapter`` family (output), ``StreamingAwareEmitter``
(emitter), and the ``ContentFilter`` family (filters).
"""

from modex_agent.adapters.emitter import StreamingAwareEmitter
from modex_agent.adapters.filters import (
    ChainedContentFilter,
    ContentFilter,
    ReasoningContentFilter,
    WhitespaceFilter,
)
from modex_agent.adapters.output import (
    CLIOutputAdapter,
    HTTPOutputAdapter,
    NullOutputAdapter,
    OutputAdapter,
)
from modex_agent.adapters.platform import (
    AdapterRegistry,
    PlatformAdapter,
    StreamingMode,
)

__all__ = [
    # platform
    "AdapterRegistry",
    "PlatformAdapter",
    "StreamingMode",
    # output
    "CLIOutputAdapter",
    "HTTPOutputAdapter",
    "NullOutputAdapter",
    "OutputAdapter",
    # emitter
    "StreamingAwareEmitter",
    # filters
    "ChainedContentFilter",
    "ContentFilter",
    "ReasoningContentFilter",
    "WhitespaceFilter",
]
