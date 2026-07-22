from modex_agent.ioc.configs.memory import MemoryConfig


def test_subagent_memory_is_minimal_and_matches_session_only():
    from bot.config.memory_defaults import subagent_memory

    m: MemoryConfig = subagent_memory()
    assert m.session is not None
    assert m.archive is None or m.archive.enabled is False
    assert m.core is None or m.core.enabled is False
    assert m.pruned is not None and m.pruned.enabled is True
    assert m.governance is not None and m.governance.tool_chain_repair is True


def test_main_memory_rich_has_long_term_layers():
    from bot.config.memory_defaults import main_agent_memory

    m: MemoryConfig = main_agent_memory()
    assert m.archive is not None and m.archive.enabled is True
    assert m.core is not None and m.core.enabled is True
    assert m.pruned is not None and m.pruned.enabled is True


def test_main_and_subagent_memory_both_build_layer_config():
    """Regression: the baked presets must pass the memory layer builder.

    Startup crashed with ``AttributeError: 'NoneType' has no attribute
    'user_retention'`` at ``_build_memory_layer_config(cfg)`` because a pool
    with no ``memory:`` block fed None into it. The baked presets (used by
    every main agent and subagent) must each (a) carry an enabled
    UserRetentionBuffer and (b) build a layer-config without crashing — that
    is the default URB config both agent kinds must always have.
    """
    from bot.config.memory_defaults import main_agent_memory, subagent_memory
    from modex_agent.ioc.factories.memory import _build_memory_layer_config

    for preset in (main_agent_memory(), subagent_memory()):
        assert preset.user_retention.enabled is True  # URB on by default
        layer_cfg = _build_memory_layer_config(preset)  # must not crash
        assert layer_cfg.user_retention.enabled is True
