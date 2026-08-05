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
    assert m.archive is None  # default off
    assert m.core is None      # default off
    assert m.compact is not None and m.compact.enabled is True
    assert m.pruned is not None and m.pruned.enabled is True



