from modex_agent.hook.config import HookConfig, HooksConfig


class TestHooksConfig:
    def test_defaults(self) -> None:
        cfg = HooksConfig()
        names = [h.name for h in cfg.items]
        assert "logging" in names
        assert "runtime_context" in names

    def test_explicit_items(self) -> None:
        cfg = HooksConfig(items=[HookConfig(name="my_hook")])
        assert len(cfg.items) == 1
        assert cfg.items[0].name == "my_hook"
