# Intra-package imports are package-absolute, not relative

The framework mixes two intra-package import styles: ~777 relative imports
(`from .core import X`, `from ..core import Y`) across 215 files and ~276
package-absolute imports (`from modex_agent.core import X`) across 78 files. The
mix is the real "non-compliance," not the relative style itself. With the
package name now stable as `modex_agent` (ADR-0003), we pick one style for all
intra-package imports.

## Considered Options

1. **Package-absolute everywhere (chosen).** All intra-package imports use
   `from modex_agent.<module> import ...`. Matches PEP 8's default
   recommendation, is fully readable from the package root, and is what the
   user's "relative imports look non-compliant" intuition points at. Renames
   are handled by PyCharm refactor.

2. **Relative everywhere.** Convert the 276 absolute imports to relative.
   Less work (relative already dominates), refactor-friendly, and PEP
   8-acceptable. But `from ....` across deep directories is hard to read, and
   relative's only real advantage — surviving a package rename — is moot once
   the name is stable (ADR-0003).

3. **Keep the mix, normalize later.** Rejected: a mixed style is exactly the
   problem being fixed.

## Consequences

- The 777 relative imports convert to `from modex_agent.<module> import ...`.
  This is the single largest mechanical task in the refactor. It is one-shot,
  scriptable (see the migration plan), and reviewable as a diff. No behavior
  change.
- No intra-package relative imports (`from .` / `from ..`) remain after
  migration; external imports stay absolute.
- External/public examples and tests use the published path
  (`from modex_agent import ...`), consistent with how third parties will
  consume the package.
