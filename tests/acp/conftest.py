"""ACP protocol tests require the optional SDK extra."""

import pytest

pytest.importorskip("acp", reason="Install ModexAgent[acp] to run ACP protocol tests")
