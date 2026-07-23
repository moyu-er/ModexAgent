from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.multi_agent.address import AgentAddress


class TestAgentAddress:
    def test_inherits_address(self) -> None:
        addr = AgentAddress(kind="agent", name="coder", role="developer", capabilities=["python", "code_review"])
        assert addr.kind == "agent"
        assert addr.name == "coder"
        assert addr.role == "developer"
        assert addr.capabilities == ["python", "code_review"]

    def test_default_values(self) -> None:
        addr = AgentAddress()
        assert addr.kind == "agent"
        assert addr.name == ""
        assert addr.role is None
        assert addr.capabilities == []

    def test_immutability(self) -> None:
        addr = AgentAddress(kind="agent", name="coder")
        with pytest.raises(ValidationError):
            addr.name = "reviewer"

    def test_str_with_role_and_capabilities(self) -> None:
        addr = AgentAddress(kind="agent", name="coder", role="developer", capabilities=["python"])
        assert str(addr) == "agent:coder@developer[python]"

    def test_str_without_extras(self) -> None:
        addr = AgentAddress(kind="agent", name="plain")
        assert str(addr) == "agent:plain"

    def test_eq_and_hash(self) -> None:
        a1 = AgentAddress(kind="agent", name="a", role="r", capabilities=["c1"])
        a2 = AgentAddress(kind="agent", name="a", role="r", capabilities=["c1"])
        a3 = AgentAddress(kind="agent", name="b", role="r", capabilities=["c1"])
        assert a1 == a2
        assert hash(a1) == hash(a2)
        assert a1 != a3
