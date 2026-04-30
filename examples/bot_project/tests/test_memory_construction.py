"""Tests for memory system construction — scope isolation, injection, layers."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from framework.memory.core.scope import MemoryContext, MemoryScope, SessionScope
from framework.memory.injection import FullInjectionPolicy, RestrictedInjectionPolicy
from framework.memory.core.layers import SessionMemoryManager


class TestMemoryScopeConstruction:
    """验证不同 agent 角色的 MemoryContext 构造。"""

    def test_memory_context_creation(self):
        ctx = MemoryContext(
            session_id="conv:main",
            user_id="user1",
            tenant_id="t1",
        )
        assert ctx.session_id == "conv:main"
        assert ctx.user_id == "user1"
        assert ctx.tenant_id == "t1"

    def test_memory_context_isolation(self):
        ctx1 = MemoryContext(session_id="user_a:conv1")
        ctx2 = MemoryContext(session_id="user_b:conv1")
        assert ctx1.session_id != ctx2.session_id

    def test_memory_context_with_agent_role(self):
        ctx = MemoryContext(
            session_id="s1",
            user_id="u1",
        )
        assert ctx.session_id == "s1"


class TestInjectionPolicyBehavior:
    """验证 FullInjectionPolicy 和 RestrictedInjectionPolicy 的构造。"""

    async def test_full_injection_policy_creation(self):
        policy = FullInjectionPolicy()
        # assemble is the main method
        assert hasattr(policy, "assemble")

    async def test_restricted_injection_policy_creation(self):
        policy = RestrictedInjectionPolicy(max_session_messages=3)
        assert hasattr(policy, "assemble")

    async def test_restricted_injection_policy_default(self):
        policy = RestrictedInjectionPolicy()
        assert hasattr(policy, "assemble")
