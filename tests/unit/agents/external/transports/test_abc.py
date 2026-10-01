"""Unit tests for the ``ExternalTransport`` ABC (the access-form seam).

Structural contract:

- Abstract ``execute`` — the ABC cannot be instantiated directly; a
  subclass omitting ``execute`` is still abstract.
- Default ``close`` is a no-op concrete method.
- ``StaleSessionError`` is the seam's retry vocabulary (raised by
  transports, caught by the harness).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from modex_agent.agents.external.transports import (
    ExternalTransport,
    StaleSessionError,
)
from modex_agent.agents.external.types import BackendResult, ExecOptions


class TestExternalTransportABC:
    def test_cannot_instantiate_abc_directly(self) -> None:
        with pytest.raises(TypeError):
            ExternalTransport()  # type: ignore[abstract]

    def test_subclass_without_execute_is_abstract(self) -> None:
        class Half(ExternalTransport):
            pass

        with pytest.raises(TypeError):
            Half()  # type: ignore[abstract]

    async def test_concrete_subclass_executes(self, tmp_path: Path) -> None:
        driven: list[ExecOptions] = []

        class Concrete(ExternalTransport):
            async def execute(
                self,
                opts: ExecOptions,
                env: Mapping[str, str],
                on_event,
                on_child_event=None,
            ) -> BackendResult:
                driven.append(opts)
                return BackendResult(status="completed", session_id="s1")

        result = await Concrete().execute(ExecOptions(prompt="x", workdir=tmp_path), {}, None)
        assert result.status == "completed"
        assert len(driven) == 1

    async def test_default_close_is_noop(self) -> None:
        class Concrete(ExternalTransport):
            async def execute(
                self,
                opts: ExecOptions,
                env: Mapping[str, str],
                on_event,
                on_child_event=None,
            ) -> BackendResult:
                return BackendResult(status="completed")

        await Concrete().close()  # must not raise


class TestStaleSessionError:
    def test_is_plain_exception_with_message(self) -> None:
        exc = StaleSessionError("provider session expired")
        assert isinstance(exc, Exception)
        assert "expired" in str(exc)
