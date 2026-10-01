"""W6 — the INPUT_STAGE declarative order face (``resolve_stage_names``).

``order=None`` must be byte-identical to the code-defined skeletons + the
custom-stage insertion rule (zero behavior change when unconfigured); a
declared order reorders / repositions stages and must cover exactly the
resolved set. The end-to-end red anchor (a declaration through the
production bot builder) lives in
``examples/bot_project/tests/input_pipeline/test_stage_order.py``.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from modex_agent.pipeline.input.skeleton import (
    ACP_STAGE_SKELETON,
    IM_STAGE_SKELETON,
    WEBUI_STAGE_SKELETON,
    InputStageName,
    resolve_stage_names,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.scope.component_registry import ComponentRegistry
from modex_agent.scope.components import SimpleFactory


class _StageConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _registry_with_custom_stages(*custom_names: str) -> ComponentRegistry:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as ctx:
        for name in custom_names:
            ctx.register_input_stage(name, SimpleFactory(name, _StageConfig))
    return registry


def _names(skeleton: tuple[InputStageName, ...]) -> list[str]:
    return [stage.value for stage in skeleton]


class TestDefaultOrderUnchanged:
    def test_order_none_is_byte_identical_for_all_three_skeletons(self) -> None:
        """No custom stages, no order → the skeletons verbatim."""
        registry = ComponentRegistry()
        assert resolve_stage_names(registry, IM_STAGE_SKELETON) == tuple(
            _names(IM_STAGE_SKELETON)
        )
        assert resolve_stage_names(registry, WEBUI_STAGE_SKELETON) == tuple(
            _names(WEBUI_STAGE_SKELETON)
        )
        assert resolve_stage_names(registry, ACP_STAGE_SKELETON) == tuple(
            _names(ACP_STAGE_SKELETON)
        )

    def test_custom_stages_insert_before_unsupported_guard_by_default(self) -> None:
        registry = _registry_with_custom_stages("zebra_cmd", "alpha_cmd")
        resolved = resolve_stage_names(registry, WEBUI_STAGE_SKELETON)
        # Deterministic (registry-sorted) insertion immediately before the
        # terminal guard — the pre-W6 behavior.
        insertion = resolved.index(InputStageName.UNSUPPORTED_COMMAND)
        assert list(resolved[insertion - 2 : insertion]) == ["alpha_cmd", "zebra_cmd"]


class TestDeclaredOrder:
    def test_order_reorders_builtin_stages(self) -> None:
        registry = ComponentRegistry()
        default = list(_names(WEBUI_STAGE_SKELETON))
        reordered = [default[-1], *default[:-1]]  # persist first
        assert resolve_stage_names(
            registry, WEBUI_STAGE_SKELETON, order=reordered
        ) == tuple(reordered)

    def test_order_repositions_custom_stages_anywhere(self) -> None:
        registry = _registry_with_custom_stages("vip_cmd")
        default = list(_names(IM_STAGE_SKELETON))
        guard = default.index(InputStageName.UNSUPPORTED_COMMAND)
        declared = [
            "vip_cmd",  # the custom stage FIRST, not before the guard
            *default[:guard],
            *default[guard:],
        ]
        assert resolve_stage_names(registry, IM_STAGE_SKELETON, order=declared)[0] == (
            "vip_cmd"
        )

    def test_unknown_name_fails_loudly(self) -> None:
        registry = ComponentRegistry()
        default = list(_names(WEBUI_STAGE_SKELETON))
        with pytest.raises(ValueError, match="unknown=\\['no_such_stage'\\]"):
            resolve_stage_names(
                registry, WEBUI_STAGE_SKELETON, order=[*default, "no_such_stage"]
            )

    def test_missing_stage_fails_loudly(self) -> None:
        registry = ComponentRegistry()
        default = list(_names(WEBUI_STAGE_SKELETON))
        dropped = default[:-1]  # persist_user_message omitted
        with pytest.raises(ValueError, match="missing=\\['persist_user_message'\\]"):
            resolve_stage_names(registry, WEBUI_STAGE_SKELETON, order=dropped)

    def test_order_ignores_builtin_stage_registered_as_custom(self) -> None:
        """A plugin re-registering a builtin name does not duplicate it —
        builtin names never enter the custom insertion set."""
        registry = ComponentRegistry()
        with PluginRegistrationContext(registry) as ctx:
            ctx.register_input_stage(
                str(InputStageName.APPROVAL), SimpleFactory("dup", _StageConfig)
            )
        assert resolve_stage_names(registry, WEBUI_STAGE_SKELETON).count(
            str(InputStageName.APPROVAL)
        ) == 1
