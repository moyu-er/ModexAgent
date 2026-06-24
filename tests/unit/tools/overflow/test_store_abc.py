from __future__ import annotations

import pytest

from modex_agent.tools.overflow.store import ToolOverflowStore


class TestToolOverflowStoreABC:
    def test_direct_instantiation_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            ToolOverflowStore()
