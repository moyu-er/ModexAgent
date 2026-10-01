from modex_agent.approval.config import ApprovalConfig, ToolApprovalEntry


class TestApprovalConfig:
    def test_defaults(self) -> None:
        cfg = ApprovalConfig()
        assert cfg.enabled is False
        assert cfg.tools == {}

    def test_with_tools(self) -> None:
        cfg = ApprovalConfig(
            tools={
                "bash": ToolApprovalEntry(allowed_paths=["*"]),
                "write": ToolApprovalEntry(allowed_paths=["./*"]),
            }
        )
        assert cfg.tools["bash"].allowed_paths == ["*"]
        assert cfg.tools["write"].allowed_paths == ["./*"]
