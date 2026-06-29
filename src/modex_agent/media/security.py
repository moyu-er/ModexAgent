"""Security policy for the perception gate — dangerous-executable magic bytes.

Disguise-rejection (matching the magic-byte signatures below) is the perception
gate's security mechanism (ADR-0013 §8): a file whose leading bytes reveal a
known executable family is hard-rejected as a ``dangerous_disguise`` regardless
of its declared extension, so a PE disguised as ``.png`` never reaches the type
allow-list. The gate's type allow-list independently rejects
executable-typed files — a real executable (``.exe`` with PE magic, or any
binary) classifies as ``kind=OTHER`` and is rejected with ``type_not_allowed``.

Extensions are deliberately NOT gated: script extensions (``.sh``/``.ps1``/
``.js``/...) that ``mimetypes`` maps to ``text/*`` classify as
``EXTRACTABLE_DOCUMENT`` and are legitimately readable text the agent may need
to analyze. Rejecting them by extension would be wrong; the magic-disguise
check is the real defense against binary executables masquerading under a
harmless name.
"""

from __future__ import annotations

import types
from enum import StrEnum


class DangerousExecutable(StrEnum):
    """Dangerous executable a disguise-rejection must catch by magic bytes.

    Identifies the file families the perception gate hard-rejects regardless of
    declared extension (ADR-0013 §8 disguise-rejection). The enum value is the
    human family name; the magic signatures live in ``_DANGEROUS_MAGIC``.
    """

    PE = "pe"  # Windows PE (EXE/DLL)
    ELF = "elf"  # Linux ELF
    MACH_O = "mach_o"  # macOS Mach-O


# Magic-byte signatures keyed by family. Mach-O has both 32- and 64-bit magics
# in each endianness: little-endian (feedface/feedfacf) and big-endian
# (cefaedfe/cffaedfe).
_DANGEROUS_MAGIC: dict[DangerousExecutable, tuple[bytes, ...]] = {
    DangerousExecutable.PE: (b"MZ",),
    DangerousExecutable.ELF: (b"\x7fELF",),
    DangerousExecutable.MACH_O: (
        b"\xfe\xed\xfa\xce",  # LE 32-bit
        b"\xfe\xed\xfa\xcf",  # LE 64-bit
        b"\xce\xfa\xed\xfe",  # BE 32-bit
        b"\xcf\xfa\xed\xfe",  # BE 64-bit
    ),
}

# Public read-only handle to the fixed magic table. MappingProxyType makes it
# immutable so a caller cannot neuter disguise-rejection by mutation. The
# perception gate reads this; callers cannot override it.
DANGEROUS_MAGIC: types.MappingProxyType[DangerousExecutable, tuple[bytes, ...]] = (
    types.MappingProxyType(_DANGEROUS_MAGIC)
)
