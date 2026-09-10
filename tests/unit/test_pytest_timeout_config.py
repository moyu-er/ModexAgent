"""The configured watchdog must terminate even a blocked test thread."""

import subprocess
import sys
from pathlib import Path


def test_timeout_watchdog_terminates_blocked_test(tmp_path: Path) -> None:
    test_file = tmp_path / "test_blocked.py"
    test_file.write_text(
        "import threading\n"
        "def test_blocked():\n"
        "    threading.Event().wait()\n",
        encoding="utf-8",
    )
    config = Path(__file__).resolve().parents[2] / "pyproject.toml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-c", str(config),
         "--timeout=0.2", "-q"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "Timeout" in result.stdout + result.stderr
