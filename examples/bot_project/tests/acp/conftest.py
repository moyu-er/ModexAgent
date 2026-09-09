"""Editor integration tests require the optional ACP SDK."""

import pytest

pytest.importorskip("acp", reason="Install ModexAgent[acp] to run editor integration tests")
