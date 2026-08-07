"""Long-term memory defaults initializer.

Extracted from ``pool_builder.py`` (ADR-0025 ticket 6 split). Initializes
default long-term memory files if core memory is enabled.
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.core.scope import MemoryContext
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.memory.default_system import DefaultMemorySystem


async def ensure_long_term_defaults(
    project_dir: Path,
    memory_cfg: MemoryConfig | None,
    memory_system: DefaultMemorySystem,
) -> None:
    """Initialize default long-term memory files if core memory is enabled.

    Supports both old ``long_term`` config (deprecated) and new ``core``
    config. Template paths in config are relative to the project directory.
    Resolves them to absolute paths before calling ``ensure_defaults`` so
    the core memory layer finds templates regardless of CWD (critical after
    ``/cd`` switches the conversation to a different workspace).
    """
    if memory_cfg is None:
        return

    core_enabled = False
    if memory_cfg.long_term is not None and memory_cfg.long_term.enabled:
        core_enabled = True
    if memory_cfg.core is not None and memory_cfg.core.enabled:
        core_enabled = True
    if not core_enabled:
        return

    lt_mgr = memory_system.core_memory_manager
    if lt_mgr is None:
        return

    raw_template_dir: str | None = None
    if memory_cfg.core is not None:
        raw_template_dir = memory_cfg.core.default_templates_dir
    if not raw_template_dir and memory_cfg.long_term is not None:
        raw_template_dir = memory_cfg.long_term.default_templates_dir
    if raw_template_dir:
        abs_template_dir = str((project_dir / raw_template_dir).resolve())
        lt_mgr._config = lt_mgr._config.model_copy(
            update={"default_templates_dir": abs_template_dir}
        )

    defaults: dict[str, str] = {
        "soul": (
            "## 沟通风格\n"
            "- 使用中文回复，风格自然、简洁\n"
            "- 优先给出直接答案，再补充解释\n"
            "- 不确定的事情如实说明，不编造\n"
        ),
        "user": (
            "## 用户画像\n- 首次使用，暂无特定偏好记录\n- 后续对话中会逐渐积累用户习惯和偏好\n"
        ),
        "memory": ("## 相关知识\n- 暂无特定领域知识记录\n- 长期对话中会自动整理和更新\n"),
    }

    ctx = MemoryContext(session_id="default", user_id="default")
    await lt_mgr.ensure_defaults(ctx, defaults)
    print("   [OK] Long-term memory defaults ensured")
