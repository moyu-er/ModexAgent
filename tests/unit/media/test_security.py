"""Tests for media.security — dangerous-executable magic deny-list (ADR-0013 §8).

The deny-list is a fixed security policy: disguise-rejection by magic bytes is
the perception gate's defense against executable-masquerading-as-image. The
public handle must be immutable so a caller cannot neuter disguise-rejection
by mutation.
"""

from __future__ import annotations

import pytest

from modex_agent.media.security import DANGEROUS_MAGIC, DangerousExecutable


class TestDangerousExecutableDenyList:
    def test_pe_magic(self) -> None:
        assert b"MZ" in DANGEROUS_MAGIC[DangerousExecutable.PE]

    def test_elf_magic(self) -> None:
        assert b"\x7fELF" in DANGEROUS_MAGIC[DangerousExecutable.ELF]

    def test_mach_o_magic_all_four_variants(self) -> None:
        sigs = DANGEROUS_MAGIC[DangerousExecutable.MACH_O]
        assert b"\xfe\xed\xfa\xce" in sigs  # LE 32-bit
        assert b"\xfe\xed\xfa\xcf" in sigs  # LE 64-bit
        assert b"\xce\xfa\xed\xfe" in sigs  # BE 32-bit
        assert b"\xcf\xfa\xed\xfe" in sigs  # BE 64-bit

    def test_magic_deny_list_is_immutable(self) -> None:
        # The public handle is a read-only mapping; a caller cannot mutate it to
        # neuter disguise-rejection.
        with pytest.raises(TypeError):
            DANGEROUS_MAGIC[DangerousExecutable.PE] = ()  # type: ignore[index]
        with pytest.raises(TypeError):
            del DANGEROUS_MAGIC[DangerousExecutable.ELF]  # type: ignore[misc]
