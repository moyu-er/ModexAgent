"""Direct tests for ``native_core._merge_memory`` (C5 single fallback path).

``_merge_memory`` branch matrix (post-W1 shape — two fallbacks only):
- fallback set (``inputs.memory_config``) → used verbatim with no overrides
- fallback None → ``subagent_memory()`` preset
- each override applied via ``model_copy`` (M1: enabling a layer preserves
  the preset's internals; fresh construction only when the base layer is
  None)

Assembly negative paths (unknown names, missing memory system) live in
``test_stage_agent.py`` next to the assembly harness.
"""

from __future__ import annotations

from modex_agent.ioc.configs.memory import (
    ArchiveConfig,
    CoreMemoryConfig,
    MemoryConfig,
    SessionConfig,
)
from modex_agent.memory.presets import subagent_memory
from modex_agent.plugins.assembly.native_core import _merge_memory
from modex_agent.plugins.assembly.spec import MemoryOverrides


class TestMergeMemoryFallback:
    def test_fallback_set_no_overrides_used_verbatim(self) -> None:
        fallback = MemoryConfig(session=SessionConfig(max_context_tokens=1234))
        result = _merge_memory(fallback, MemoryOverrides())
        assert result == fallback
        assert result.session.max_context_tokens == 1234

    def test_fallback_none_uses_subagent_preset(self) -> None:
        result = _merge_memory(None, MemoryOverrides())
        assert result == subagent_memory()
        assert result.archive is None
        assert result.core is None


class TestMergeMemoryOverrides:
    def test_max_context_tokens_override_applied(self) -> None:
        fallback = MemoryConfig(session=SessionConfig(max_context_tokens=200000))
        result = _merge_memory(
            fallback, MemoryOverrides(max_context_tokens=32000)
        )
        assert result.session.max_context_tokens == 32000
        # Session internals beyond the overridden field are preserved.
        assert result.session.max_token_ratio == fallback.session.max_token_ratio

    def test_archive_enable_on_base_without_archive_constructs_fresh(self) -> None:
        result = _merge_memory(
            MemoryConfig(archive=None), MemoryOverrides(archive_enabled=True)
        )
        assert result.archive is not None
        assert result.archive.enabled is True
        assert result.archive == ArchiveConfig(enabled=True)

    def test_archive_enable_preserves_base_internals(self) -> None:
        """M1: enabling archive on a base that carries a (disabled) archive
        layer toggles ``enabled`` only — the preset's internals survive."""
        fallback = MemoryConfig(
            archive=ArchiveConfig(
                enabled=False,
                max_entries=42,
                max_archive_inject=1,
                scope=["global"],
            )
        )
        result = _merge_memory(fallback, MemoryOverrides(archive_enabled=True))
        assert result.archive is not None
        assert result.archive.enabled is True
        assert result.archive.max_entries == 42
        assert result.archive.max_archive_inject == 1
        assert result.archive.scope == ["global"]

    def test_archive_disable_on_enabled_base_yields_none(self) -> None:
        fallback = MemoryConfig(archive=ArchiveConfig(enabled=True))
        result = _merge_memory(fallback, MemoryOverrides(archive_enabled=False))
        assert result.archive is None

    def test_core_enable_on_base_without_core_constructs_fresh(self) -> None:
        result = _merge_memory(
            MemoryConfig(core=None), MemoryOverrides(core_enabled=True)
        )
        assert result.core is not None
        assert result.core.enabled is True
        assert result.core == CoreMemoryConfig(enabled=True)

    def test_core_enable_preserves_base_internals(self) -> None:
        fallback = MemoryConfig(
            core=CoreMemoryConfig(
                enabled=False, default_templates_dir="custom/", scope=["global"]
            )
        )
        result = _merge_memory(fallback, MemoryOverrides(core_enabled=True))
        assert result.core is not None
        assert result.core.enabled is True
        assert result.core.default_templates_dir == "custom/"
        assert result.core.scope == ["global"]

    def test_core_disable_on_enabled_base_yields_none(self) -> None:
        fallback = MemoryConfig(core=CoreMemoryConfig(enabled=True))
        result = _merge_memory(fallback, MemoryOverrides(core_enabled=False))
        assert result.core is None

    def test_all_three_overrides_apply_together(self) -> None:
        result = _merge_memory(
            MemoryConfig(),
            MemoryOverrides(
                max_context_tokens=8000,
                archive_enabled=True,
                core_enabled=True,
            ),
        )
        assert result.session.max_context_tokens == 8000
        assert result.archive is not None and result.archive.enabled
        assert result.core is not None and result.core.enabled


class TestMergeMemoryUntouchedLayers:
    def test_override_does_not_mutate_fallback(self) -> None:
        fallback = MemoryConfig(
            archive=ArchiveConfig(enabled=False, max_entries=42)
        )
        _merge_memory(fallback, MemoryOverrides(archive_enabled=True))
        assert fallback.archive is not None
        assert fallback.archive.enabled is False
        assert fallback.archive.max_entries == 42
