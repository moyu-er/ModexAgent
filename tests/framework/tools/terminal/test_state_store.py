"""Tests for TerminalStateStore."""

import json
import tempfile
from pathlib import Path

import pytest

from framework.tools.terminal.state_store import JsonTerminalStateStore


class TestJsonTerminalStateStore:
    def test_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = JsonTerminalStateStore(Path(td))
            state = {
                "version": 1,
                "default_terminal": "tab-1",
                "sessions": [
                    {
                        "name": "tab-1",
                        "shell_type": "bash",
                        "shell_path": "/bin/bash",
                        "cwd": "/home/user",
                        "env": {"KEY": "value"},
                        "created_at": 1234567890.0,
                        "last_active": 1234567900.0,
                        "history": [
                            {
                                "command": "ls",
                                "output": "file.txt",
                                "exit_code": 0,
                                "timestamp": 1234567895.0,
                            }
                        ],
                        "needs_restart": True,
                    }
                ],
            }
            store.save(state)
            loaded = store.load()
            assert loaded["default_terminal"] == "tab-1"
            assert len(loaded["sessions"]) == 1
            assert loaded["sessions"][0]["name"] == "tab-1"

    def test_load_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = JsonTerminalStateStore(Path(td) / "nonexistent")
            result = store.load()
            assert result == {}

    def test_load_corrupted_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = JsonTerminalStateStore(Path(td))
            store._file_path.write_text("not json")
            result = store.load()
            assert result == {}
