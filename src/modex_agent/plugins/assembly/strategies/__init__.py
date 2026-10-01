"""Bundled pool-shape strategies for the assembly pipeline (W4a).

Promoted from ``examples/bot_project/bot/service/{react_strategy,external_strategy}.py``
(plan SD-7 W4a) so the framework ships a runnable pool shape. The planned
homes ``agents/react/strategy.py`` / ``agents/external/strategy.py`` were
rejected by the layering tree (``tests/architecture/test_dependency_tree.py``):
the strategies compose pipeline/multi_agent/plugins-assembly objects at
runtime (``assemble_external_pipeline``, the runtime-constructor seam in
``native_core``, roster-hook dispatch), and W3b placed ``agents`` (L4)
strictly below ``pipeline`` (L5) and ``multi_agent`` (L6) —
``multi_agent/factory.py`` documents that judgment for the very same
composition. ``plugins/assembly`` (L7) is the lowest layer from which every
strategy dependency points strictly downward.

W5: the strategies own their runtimes. ``external`` builds its
``ExternalAgent`` + ``ExternalTurnRunner`` directly in ``assemble_main``
(the ``ExternalAwareFactory`` stub-subclass hack is deleted); ``react``
returns its react products and lets Stage 4 construct the react runtime
through the native core's default runtime constructor.
"""

from modex_agent.plugins.assembly.strategies.external import (
    ExternalExecutionStrategy,
    ProviderUnavailableError,
    build_external_env_spec,
)
from modex_agent.plugins.assembly.strategies.react import ReactExecutionStrategy

__all__ = [
    "ExternalExecutionStrategy",
    "ProviderUnavailableError",
    "ReactExecutionStrategy",
    "build_external_env_spec",
]
