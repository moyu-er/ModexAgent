"""Unit tests for the provider ABCs (`ProviderBackend`, `ProviderEventParser`)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import override

import pytest

from modex_agent.agents.external import (
    BackendResult,
    Emission,
    ExecOptions,
    ExternalEvent,
    ProviderBackend,
    ProviderEventParser,
)


class _SimpleBackend(ProviderBackend):
    """Concrete subclass used to verify the ABC contract."""

    @override
    async def execute(self, opts: ExecOptions) -> BackendResult:
        return BackendResult(status="completed", session_id=opts.resume_session_id)


class _SimpleParser(ProviderEventParser):
    """Returns one text-delta per non-empty line; ignores garbage."""

    @override
    def parse_line(self, line: str) -> Iterator[Emission]:
        if not line.strip():
            return iter(())
        # Consume at least once so callers get a fresh iterator.
        if "no-emit" in line:
            return iter(())
        return iter([Emission(event=ExternalEvent.TEXT_DELTA, text=line)])


class TestProviderBackendABC:
    """ABC structural + instantiation contract."""

    def test_cannot_instantiate_abc_directly(self) -> None:
        with pytest.raises(TypeError):
            ProviderBackend()  # type: ignore[abstract]

    def test_concrete_subclass_can_be_instantiated(self) -> None:
        b = _SimpleBackend()
        assert isinstance(b, ProviderBackend)

    @pytest.mark.asyncio
    async def test_concrete_execute_returns_backend_result(self, tmp_path: Path) -> None:
        b = _SimpleBackend()
        opts = ExecOptions(prompt="hi", workdir=tmp_path, resume_session_id="ps1")
        result = await b.execute(opts)
        assert result.status == "completed"
        assert result.session_id == "ps1"


class TestProviderEventParserABC:
    """ABC structural + parse behaviour contract."""

    def test_cannot_instantiate_abc_directly(self) -> None:
        with pytest.raises(TypeError):
            ProviderEventParser()  # type: ignore[abstract]

    def test_concrete_subclass_can_be_instantiated(self) -> None:
        p = _SimpleParser()
        assert isinstance(p, ProviderEventParser)

    def test_parse_line_yields_text_delta(self) -> None:
        p = _SimpleParser()
        out = list(p.parse_line("hello"))
        assert len(out) == 1
        assert out[0].event is ExternalEvent.TEXT_DELTA
        assert out[0].text == "hello"

    def test_parse_line_empty_string_yields_nothing(self) -> None:
        p = _SimpleParser()
        assert list(p.parse_line("")) == []
        assert list(p.parse_line("   ")) == []

    def test_parse_line_sentinel_yields_empty_iterator(self) -> None:
        # The ``no-emit`` substring pattern is the parser's "I have
        # nothing to report" signal — important so callers can
        # distinguish "no emissions" from "garbage".
        p = _SimpleParser()
        assert list(p.parse_line("no-emit-for-you")) == []
