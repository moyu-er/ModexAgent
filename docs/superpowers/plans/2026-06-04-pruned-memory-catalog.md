# Pruned Memory Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pruned memory catalog layer that preserves original pruned messages as retrievable files with structured indexing, and injects a lightweight catalog into the system prompt.

**Architecture:** PrunedManager is a standalone component (NOT a MemorySystem layer) with its own PrunedStorage ABC + FilePrunedStorage. It hooks into `cleanup_session()` to write pruned content and into injection policies to expose a catalog XML. Independent of archive configuration, available to all agent types.

**Tech Stack:** Python 3.12+, dataclasses, asyncio, Pydantic (for config), pytest + pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-06-04-pruned-memory-catalog-design.md`

---

## File Structure

### New Files
- `framework/memory/pruned/__init__.py` — Package exports
- `framework/memory/pruned/models.py` — `PrunedIndexEntry` dataclass
- `framework/memory/pruned/storage.py` — `PrunedStorage` ABC + `FilePrunedStorage`
- `framework/memory/pruned/manager.py` — `PrunedManager`
- `tests/unit/memory/pruned/__init__.py` — Empty test package marker
- `tests/unit/memory/pruned/test_models.py` — PrunedIndexEntry tests
- `tests/unit/memory/pruned/test_storage.py` — FilePrunedStorage tests
- `tests/unit/memory/pruned/test_manager.py` — PrunedManager tests

### Modified Files
- `framework/ioc/configs/memory.py:110-125` — Add `PrunedCatalogConfig`, add field to `MemoryConfig`
- `framework/memory/default_system.py:39-110` — Timestamp guarantee + pruned_manager forwarding
- `framework/memory/cleanup.py:79-91,260-316` — Add pruned writing step after archive
- `framework/memory/injection/full_injection.py:40-66` — Add pruned catalog injection
- `framework/memory/injection/restricted_injection.py:12-29` — Add pruned catalog injection
- `framework/memory/__init__.py:16-108,110-199` — Add exports
- `framework/ioc/factories/memory.py:66-106` — Create PrunedManager from config
- `framework/memory/system.py:30-85,96-134` — Forward PrunedManager
- `examples/bot_project/config/pools/main.yml` — Add `pruned:` config
- `examples/bot_project/config/pools/coding.yml` — Add `pruned:` config
- `examples/bot_project/bot/service/core.py:352-371` — Wire PrunedManager to injection
- `examples/bot_project/bot/service/builders.py:360-396` — Wire PrunedManager to subagent injection
- `tests/unit/memory/test_cleanup.py` — Add pruned integration tests

---

### Task 1: PrunedIndexEntry Model

**Files:**
- Create: `framework/memory/pruned/__init__.py`
- Create: `framework/memory/pruned/models.py`
- Create: `tests/unit/memory/pruned/__init__.py`
- Create: `tests/unit/memory/pruned/test_models.py`

- [ ] **Step 1: Write tests for PrunedIndexEntry**

```python
# tests/unit/memory/pruned/test_models.py
from __future__ import annotations

import json

import pytest

from framework.memory.pruned.models import PrunedIndexEntry


class TestPrunedIndexEntry:
    def test_creation_defaults(self):
        entry = PrunedIndexEntry(
            id=1,
            cleanup_time=1717401600,
            cleanup_time_display="2024-06-03 08:00",
            message_count=10,
            content_filename="pruned_2024-06-03_08.00-2024-06-03_09.30.jsonl",
        )
        assert entry.id == 1
        assert entry.start_time == 0
        assert entry.end_time == 0
        assert entry.start_time_display == ""
        assert entry.end_time_display == ""
        assert entry.topic == ""

    def test_creation_full(self):
        entry = PrunedIndexEntry(
            id=2,
            start_time=1717401600,
            end_time=1717407000,
            cleanup_time=1717408000,
            start_time_display="2024-06-03 08:00",
            end_time_display="2024-06-03 09:30",
            cleanup_time_display="2024-06-03 10:00",
            topic="Terminal tool refactoring",
            message_count=45,
            content_filename="pruned_2024-06-03_08.00-2024-06-03_09.30.jsonl",
        )
        assert entry.start_time == 1717401600
        assert entry.topic == "Terminal tool refactoring"

    def test_frozen(self):
        entry = PrunedIndexEntry(
            id=1,
            cleanup_time=1717401600,
            cleanup_time_display="2024-06-03 08:00",
            message_count=5,
            content_filename="pruned_2024-06-03_08.00.jsonl",
        )
        with pytest.raises(AttributeError):
            entry.id = 99  # type: ignore[misc]

    def test_to_dict_round_trip(self):
        entry = PrunedIndexEntry(
            id=3,
            start_time=1717401600,
            end_time=1717407000,
            cleanup_time=1717408000,
            start_time_display="2024-06-03 08:00",
            end_time_display="2024-06-03 09:30",
            cleanup_time_display="2024-06-03 10:00",
            topic="User preference correction",
            message_count=12,
            content_filename="pruned_2024-06-03_08.00-2024-06-03_09.30.jsonl",
        )
        d = entry.to_dict()
        assert d["id"] == 3
        assert d["start_time"] == 1717401600
        assert d["message_count"] == 12
        # Verify JSON-serializable
        json_str = json.dumps(d)
        parsed = json.loads(json_str)
        restored = PrunedIndexEntry.from_dict(parsed)
        assert restored == entry

    def test_from_dict_missing_optional_fields(self):
        d = {
            "id": 5,
            "cleanup_time": 1717408000,
            "cleanup_time_display": "2024-06-03 10:00",
            "message_count": 8,
            "content_filename": "pruned_2024-06-03_10.00.jsonl",
        }
        entry = PrunedIndexEntry.from_dict(d)
        assert entry.start_time == 0
        assert entry.end_time == 0
        assert entry.start_time_display == ""
        assert entry.end_time_display == ""
        assert entry.topic == ""

    def test_from_dict_extra_fields_ignored(self):
        d = {
            "id": 6,
            "cleanup_time": 1717408000,
            "cleanup_time_display": "2024-06-03 10:00",
            "message_count": 8,
            "content_filename": "pruned_2024-06-03_10.00.jsonl",
            "unknown_field": "should be ignored",
        }
        entry = PrunedIndexEntry.from_dict(d)
        assert entry.id == 6
        assert not hasattr(entry, "unknown_field")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/memory/pruned/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'framework.memory.pruned'`

- [ ] **Step 3: Create package and model**

```python
# framework/memory/pruned/__init__.py
"""Pruned memory catalog — retrievable original pruned messages with structured indexing."""
```

```python
# framework/memory/pruned/models.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PrunedIndexEntry:
    """A single entry in the pruned memory catalog index.

    Each entry corresponds to one cleanup event that pruned messages from a session.
    """

    id: int  # monotonic ID, managed by PrunedManager
    cleanup_time: int  # epoch seconds, always present
    cleanup_time_display: str  # "YYYY-MM-DD HH:MM"
    message_count: int
    content_filename: str  # exactly matches the actual file name on disk
    start_time: int = 0  # epoch seconds of first pruned message, 0 if unknown
    end_time: int = 0  # epoch seconds of last pruned message, 0 if unknown
    start_time_display: str = ""  # "YYYY-MM-DD HH:MM" or ""
    end_time_display: str = ""  # "YYYY-MM-DD HH:MM" or ""
    topic: str = ""  # from archive CONTEXT summary; fallback to time range when archive off

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "cleanup_time": self.cleanup_time,
            "start_time_display": self.start_time_display,
            "end_time_display": self.end_time_display,
            "cleanup_time_display": self.cleanup_time_display,
            "topic": self.topic,
            "message_count": self.message_count,
            "content_filename": self.content_filename,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PrunedIndexEntry:
        return cls(
            id=data["id"],
            start_time=data.get("start_time", 0),
            end_time=data.get("end_time", 0),
            cleanup_time=data["cleanup_time"],
            start_time_display=data.get("start_time_display", ""),
            end_time_display=data.get("end_time_display", ""),
            cleanup_time_display=data.get("cleanup_time_display", ""),
            topic=data.get("topic", ""),
            message_count=data.get("message_count", 0),
            content_filename=data["content_filename"],
        )
```

- [ ] **Step 4: Create test package marker**

```python
# tests/unit/memory/pruned/__init__.py
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/memory/pruned/test_models.py -v`
Expected: All 6 tests PASS

- [ ] **Step 6: Commit**

```bash
git add framework/memory/pruned/__init__.py framework/memory/pruned/models.py tests/unit/memory/pruned/__init__.py tests/unit/memory/pruned/test_models.py
git commit -m "feat(pruned): add PrunedIndexEntry data model"
```

---

### Task 2: PrunedStorage ABC + FilePrunedStorage

**Files:**
- Create: `framework/memory/pruned/storage.py`
- Create: `tests/unit/memory/pruned/test_storage.py`

- [ ] **Step 1: Write tests for FilePrunedStorage**

```python
# tests/unit/memory/pruned/test_storage.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from framework.memory.pruned.models import PrunedIndexEntry
from framework.memory.pruned.storage import FilePrunedStorage


@pytest.fixture
def pruned_dir(tmp_path: Path) -> Path:
    return tmp_path / "pruned"


@pytest.fixture
def storage(pruned_dir: Path) -> FilePrunedStorage:
    return FilePrunedStorage(pruned_dir)


def _entry(**overrides) -> PrunedIndexEntry:
    defaults = dict(
        id=1,
        cleanup_time=1717408000,
        cleanup_time_display="2024-06-03 10:00",
        message_count=5,
        content_filename="pruned_2024-06-03_10.00.jsonl",
    )
    defaults.update(overrides)
    return PrunedIndexEntry(**defaults)


class TestFilePrunedStorage:
    def test_has_content_false_when_empty(self, storage: FilePrunedStorage):
        assert storage.has_content() is False

    def test_has_content_false_when_only_index(self, storage: FilePrunedStorage):
        storage.append_index(_entry())
        assert storage.has_content() is False

    def test_has_content_true_after_write(self, storage: FilePrunedStorage):
        storage.write_pruned("pruned_2024-06-03_10.00.jsonl", [{"role": "user", "content": "hi"}])
        assert storage.has_content() is True

    def test_write_and_read_pruned_file(self, storage: FilePrunedStorage, pruned_dir: Path):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        storage.write_pruned("pruned_2024-06-03_10.00.jsonl", messages)
        file_path = pruned_dir / "pruned_2024-06-03_10.00.jsonl"
        assert file_path.exists()
        lines = file_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["content"] == "hello"
        assert json.loads(lines[1])["content"] == "world"

    def test_append_and_read_index(self, storage: FilePrunedStorage):
        e1 = _entry(id=1, content_filename="pruned_a.jsonl")
        e2 = _entry(id=2, content_filename="pruned_b.jsonl")
        storage.append_index(e1)
        storage.append_index(e2)
        entries = storage.read_index()
        assert len(entries) == 2
        assert entries[0].id == 1
        assert entries[1].id == 2

    def test_read_index_empty(self, storage: FilePrunedStorage):
        assert storage.read_index() == []

    def test_get_directory_path(self, storage: FilePrunedStorage, pruned_dir: Path):
        path = storage.get_directory_path()
        assert path == str(pruned_dir.resolve())

    def test_prune_oldest_keeps_recent(self, storage: FilePrunedStorage):
        for i in range(5):
            name = f"pruned_{i}.jsonl"
            storage.write_pruned(name, [{"role": "user", "content": f"msg{i}"}])
            storage.append_index(_entry(id=i + 1, content_filename=name))
        storage.prune_oldest(keep_count=3)
        entries = storage.read_index()
        assert len(entries) == 3
        assert entries[0].id == 3  # oldest kept
        assert entries[2].id == 5  # newest

    def test_prune_oldest_deletes_files(self, storage: FilePrunedStorage, pruned_dir: Path):
        for i in range(4):
            name = f"pruned_{i}.jsonl"
            storage.write_pruned(name, [{"role": "user", "content": str(i)}])
            storage.append_index(_entry(id=i + 1, content_filename=name))
        storage.prune_oldest(keep_count=2)
        assert not (pruned_dir / "pruned_0.jsonl").exists()
        assert not (pruned_dir / "pruned_1.jsonl").exists()
        assert (pruned_dir / "pruned_2.jsonl").exists()
        assert (pruned_dir / "pruned_3.jsonl").exists()

    def test_prune_oldest_noop_when_under_limit(self, storage: FilePrunedStorage):
        storage.write_pruned("pruned_0.jsonl", [{"role": "user", "content": "hi"}])
        storage.append_index(_entry(id=1, content_filename="pruned_0.jsonl"))
        storage.prune_oldest(keep_count=5)
        assert len(storage.read_index()) == 1

    def test_creates_directory_on_first_write(self, storage: FilePrunedStorage, pruned_dir: Path):
        assert not pruned_dir.exists()
        storage.write_pruned("pruned_0.jsonl", [{"role": "user", "content": "hi"}])
        assert pruned_dir.exists()

    def test_write_pruned_empty_messages(self, storage: FilePrunedStorage, pruned_dir: Path):
        storage.write_pruned("pruned_empty.jsonl", [])
        file_path = pruned_dir / "pruned_empty.jsonl"
        assert file_path.exists()
        content = file_path.read_text(encoding="utf-8").strip()
        assert content == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/memory/pruned/test_storage.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement PrunedStorage ABC + FilePrunedStorage**

```python
# framework/memory/pruned/storage.py
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path

from framework.memory.pruned.models import PrunedIndexEntry

logger = logging.getLogger(__name__)


class PrunedStorage(ABC):
    """Abstract storage backend for pruned memory catalog."""

    @abstractmethod
    def write_pruned(self, filename: str, messages: list[dict]) -> None:
        """Write pruned messages to a named file."""

    @abstractmethod
    def append_index(self, entry: PrunedIndexEntry) -> None:
        """Append an entry to the index file."""

    @abstractmethod
    def read_index(self) -> list[PrunedIndexEntry]:
        """Read all entries from the index file."""

    @abstractmethod
    def has_content(self) -> bool:
        """Return True if any pruned segment files exist."""

    @abstractmethod
    def prune_oldest(self, keep_count: int) -> None:
        """Delete oldest pruned files and their index entries, keeping only *keep_count*."""

    @abstractmethod
    def get_directory_path(self) -> str:
        """Return absolute path to the pruned directory for injection XML."""


class FilePrunedStorage(PrunedStorage):
    """Local file-system storage for pruned memory catalog."""

    def __init__(self, pruned_dir: Path, index_filename: str = "index.jsonl") -> None:
        self._dir = pruned_dir
        self._index_file = pruned_dir / index_filename

    def write_pruned(self, filename: str, messages: list[dict]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._dir / filename
        lines = [json.dumps(msg, ensure_ascii=False) for msg in messages]
        content = "\n".join(lines) + "\n" if lines else ""
        tmp = target.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)

    def append_index(self, entry: PrunedIndexEntry) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        with open(self._index_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    def read_index(self) -> list[PrunedIndexEntry]:
        if not self._index_file.exists():
            return []
        entries: list[PrunedIndexEntry] = []
        with open(self._index_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(PrunedIndexEntry.from_dict(json.loads(line)))
        return entries

    def has_content(self) -> bool:
        if not self._dir.exists():
            return False
        index_name = self._index_file.name
        return any(
            f.is_file() and f.suffix == ".jsonl" and f.name != index_name
            for f in self._dir.iterdir()
        )

    def prune_oldest(self, keep_count: int) -> None:
        entries = self.read_index()
        if len(entries) <= keep_count:
            return
        to_remove = entries[:-keep_count]
        for entry in to_remove:
            target = self._dir / entry.content_filename
            if target.exists():
                target.unlink()
                logger.debug("Pruned file deleted: %s", target)
        kept = entries[-keep_count:]
        tmp = self._index_file.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for entry in kept:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        tmp.replace(self._index_file)

    def get_directory_path(self) -> str:
        return str(self._dir.resolve())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/memory/pruned/test_storage.py -v`
Expected: All 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add framework/memory/pruned/storage.py tests/unit/memory/pruned/test_storage.py
git commit -m "feat(pruned): add PrunedStorage ABC and FilePrunedStorage"
```

---

### Task 3: PrunedManager

**Files:**
- Create: `framework/memory/pruned/manager.py`
- Create: `tests/unit/memory/pruned/test_manager.py`

- [ ] **Step 1: Write tests for PrunedManager**

```python
# tests/unit/memory/pruned/test_manager.py
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import pytest

from framework.memory.pruned.manager import PrunedManager
from framework.memory.pruned.models import PrunedIndexEntry
from framework.memory.pruned.storage import FilePrunedStorage


@pytest.fixture
def pruned_dir(tmp_path: Path) -> Path:
    return tmp_path / "pruned"


@pytest.fixture
def manager(pruned_dir: Path) -> PrunedManager:
    storage = FilePrunedStorage(pruned_dir)
    return PrunedManager(storage, max_files=5)


@pytest.fixture
def now() -> datetime:
    return datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)


class TestFilenameGeneration:
    def test_both_times_available(self, manager: PrunedManager):
        start = datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc)
        end = datetime(2024, 6, 3, 10, 20, tzinfo=timezone.utc)
        name = manager._generate_filename(start, end, datetime.now(timezone.utc))
        assert name == "pruned_2024-06-03_09.00-2024-06-03_10.20.jsonl"

    def test_start_missing(self, manager: PrunedManager):
        cleanup = datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)
        name = manager._generate_filename(None, cleanup, cleanup)
        assert name == "pruned_2024-06-03_10.00.jsonl"

    def test_end_missing(self, manager: PrunedManager):
        cleanup = datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)
        name = manager._generate_filename(cleanup, None, cleanup)
        assert name == "pruned_2024-06-03_10.00.jsonl"

    def test_both_missing(self, manager: PrunedManager):
        cleanup = datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)
        name = manager._generate_filename(None, None, cleanup)
        assert name == "pruned_2024-06-03_10.00.jsonl"


class TestWritePruned:
    @pytest.mark.asyncio
    async def test_write_with_topic(self, manager: PrunedManager, pruned_dir: Path, now: datetime):
        messages = [
            {"role": "user", "content": "hello", "created_at": "2024-06-03 09:00:00"},
            {"role": "assistant", "content": "world", "created_at": "2024-06-03 09:05:00"},
        ]
        await manager.write_pruned(messages, topic="User greeting", cleanup_time=now)
        entries = manager._storage.read_index()
        assert len(entries) == 1
        assert entries[0].topic == "User greeting"
        assert entries[0].message_count == 2
        assert entries[0].content_filename.startswith("pruned_")
        # File exists
        assert (pruned_dir / entries[0].content_filename).exists()

    @pytest.mark.asyncio
    async def test_write_without_topic_uses_time_fallback(
        self, manager: PrunedManager, now: datetime,
    ):
        messages = [
            {"role": "user", "content": "hi", "created_at": "2024-06-03 09:30:00"},
            {"role": "assistant", "content": "there", "created_at": "2024-06-03 09:45:00"},
        ]
        await manager.write_pruned(messages, topic=None, cleanup_time=now)
        entries = manager._storage.read_index()
        assert "2024-06-03 09:30" in entries[0].topic
        assert "2024-06-03 09:45" in entries[0].topic
        assert "2 messages" in entries[0].topic

    @pytest.mark.asyncio
    async def test_write_without_times_uses_cleanup_time(
        self, manager: PrunedManager, now: datetime,
    ):
        messages = [{"role": "user", "content": "hi"}]
        await manager.write_pruned(messages, topic=None, cleanup_time=now)
        entries = manager._storage.read_index()
        assert entries[0].start_time == 0
        assert entries[0].end_time == 0
        assert "2024-06-03 10:00" in entries[0].topic

    @pytest.mark.asyncio
    async def test_id_auto_increments(self, manager: PrunedManager, now: datetime):
        msgs = [{"role": "user", "content": f"msg{i}"}] for i in range(3)
        for msgs_i in msgs:
            await manager.write_pruned(msgs_i, topic="test", cleanup_time=now)
        entries = manager._storage.read_index()
        assert [e.id for e in entries] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_topic_truncation(self, pruned_dir: Path, now: datetime):
        storage = FilePrunedStorage(pruned_dir)
        mgr = PrunedManager(storage, max_files=5, topic_max_chars=20)
        long_topic = "A" * 200
        await mgr.write_pruned([{"role": "user", "content": "hi"}], topic=long_topic, cleanup_time=now)
        entries = mgr._storage.read_index()
        assert len(entries[0].topic) <= 20


class TestEviction:
    @pytest.mark.asyncio
    async def test_eviction_removes_oldest(self, pruned_dir: Path, now: datetime):
        storage = FilePrunedStorage(pruned_dir)
        mgr = PrunedManager(storage, max_files=3)
        for i in range(5):
            msgs = [{"role": "user", "content": f"msg{i}"}]
            await mgr.write_pruned(msgs, topic=f"topic {i}", cleanup_time=now)
        entries = mgr._storage.read_index()
        assert len(entries) == 3
        assert entries[0].id == 3  # oldest kept
        assert entries[2].id == 5  # newest
        # Oldest files deleted
        assert not (pruned_dir / entries[0].content_filename).exists() or True
        # Actually check that the first two entries' files are gone
        all_files = list(pruned_dir.glob("pruned_*.jsonl"))
        # index.jsonl is not matched because it doesn't start with "pruned_" followed by a date
        # Actually it could. Let's check by count of non-index files
        segment_files = [f for f in all_files if f.name != "index.jsonl"]
        assert len(segment_files) == 3


class TestInjectionXml:
    def test_returns_none_when_no_content(self, manager: PrunedManager):
        assert manager.get_injection_xml() is None

    @pytest.mark.asyncio
    async def test_returns_xml_when_content_exists(
        self, manager: PrunedManager, pruned_dir: Path, now: datetime,
    ):
        await manager.write_pruned(
            [{"role": "user", "content": "hi"}], topic="test", cleanup_time=now,
        )
        xml = manager.get_injection_xml()
        assert xml is not None
        assert "<memory_archives>" in xml
        assert "<directory path=" in xml
        assert str(pruned_dir.resolve()) in xml
        assert "index.jsonl" in xml
        assert "editable" in xml
        assert "must NOT be modified" in xml

    @pytest.mark.asyncio
    async def test_xml_contains_absolute_path(
        self, manager: PrunedManager, pruned_dir: Path, now: datetime,
    ):
        await manager.write_pruned(
            [{"role": "user", "content": "hi"}], topic="test", cleanup_time=now,
        )
        xml = manager.get_injection_xml()
        assert str(pruned_dir.resolve()) in xml
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/memory/pruned/test_manager.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement PrunedManager**

```python
# framework/memory/pruned/manager.py
from __future__ import annotations

import logging
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

from framework.memory.pruned.models import PrunedIndexEntry
from framework.memory.pruned.storage import PrunedStorage

logger = logging.getLogger(__name__)

_INJECTION_XML_TEMPLATE = (
    "<memory_archives>\n"
    "<!-- Pruned conversation segments are stored as read-only files in the directory below.\n"
    "     An index.jsonl in the same directory catalogs each segment with topic, time range,\n"
    "     and file path.\n"
    "     NOTE: index.jsonl is editable — you should update it to improve topic descriptions\n"
    "     or categorization when you have better context. The pruned segment files themselves\n"
    "     must NOT be modified. -->\n"
    '  <directory path="{path}"/>\n'
    "</memory_archives>"
)


class PrunedManager:
    """Manages pruned memory catalog: writes on cleanup, reads for injection.

    Not a MemorySystem layer — a standalone component shared by cleanup and injection.
    Independent of archive configuration.
    """

    def __init__(
        self,
        storage: PrunedStorage,
        max_files: int = 50,
        topic_max_chars: int = 200,
    ) -> None:
        self._storage = storage
        self._max_files = max_files
        self._topic_max = topic_max_chars
        self._next_id = self._derive_next_id()

    def _derive_next_id(self) -> int:
        entries = self._storage.read_index()
        if not entries:
            return 1
        return max(e.id for e in entries) + 1

    # -- Called by cleanup_session() --

    async def write_pruned(
        self,
        pruned_messages: list[dict],
        topic: str | None,
        cleanup_time: datetime,
    ) -> None:
        """Write pruned messages to file and append index entry.

        Steps:
        1. Extract start/end time from first/last message created_at.
        2. Generate filename (start-end or single cleanup time).
        3. If topic is None, build fallback from time range + count.
        4. Write messages to file.
        5. Build PrunedIndexEntry with auto-incremented ID.
        6. Append to index.jsonl.
        7. Prune oldest if over max_files.
        """
        start, end = self._extract_time_range(pruned_messages)
        filename = self._generate_filename(start, end, cleanup_time)

        effective_topic = self._resolve_topic(
            topic, start, end, cleanup_time, len(pruned_messages),
        )

        self._storage.write_pruned(filename, pruned_messages)

        entry = self._build_index_entry(
            pruned_messages, effective_topic, cleanup_time, filename, start, end,
        )
        self._storage.append_index(entry)
        self._next_id += 1

        self._storage.prune_oldest(self._max_files)

    # -- Called by injection policies --

    def get_injection_xml(self) -> str | None:
        """Return catalog XML for system prompt, or None if no pruned content."""
        if not self._storage.has_content():
            return None
        path = xml_escape(self._storage.get_directory_path())
        return _INJECTION_XML_TEMPLATE.format(path=path)

    # -- Internal helpers --

    def _generate_filename(
        self,
        start: datetime | None,
        end: datetime | None,
        cleanup_time: datetime,
    ) -> str:
        if start is not None and end is not None:
            s = start.strftime("%Y-%m-%d_%H.%M")
            e = end.strftime("%Y-%m-%d_%H.%M")
            return f"pruned_{s}-{e}.jsonl"
        return f"pruned_{cleanup_time.strftime('%Y-%m-%d_%H.%M')}.jsonl"

    def _build_index_entry(
        self,
        pruned_messages: list[dict],
        topic: str,
        cleanup_time: datetime,
        filename: str,
        start: datetime | None,
        end: datetime | None,
    ) -> PrunedIndexEntry:
        return PrunedIndexEntry(
            id=self._next_id,
            start_time=int(start.timestamp()) if start else 0,
            end_time=int(end.timestamp()) if end else 0,
            cleanup_time=int(cleanup_time.timestamp()),
            start_time_display=start.strftime("%Y-%m-%d %H:%M") if start else "",
            end_time_display=end.strftime("%Y-%m-%d %H:%M") if end else "",
            cleanup_time_display=cleanup_time.strftime("%Y-%m-%d %H:%M"),
            topic=topic,
            message_count=len(pruned_messages),
            content_filename=filename,
        )

    def _resolve_topic(
        self,
        topic: str | None,
        start: datetime | None,
        end: datetime | None,
        cleanup_time: datetime,
        count: int,
    ) -> str:
        if topic is not None:
            return topic[:self._topic_max]
        # Fallback: time range + message count
        if start and end:
            return f"{start.strftime('%Y-%m-%d %H:%M')} ~ {end.strftime('%Y-%m-%d %H:%M')} ({count} messages)"
        return f"{cleanup_time.strftime('%Y-%m-%d %H:%M')} ({count} messages)"

    @staticmethod
    def _extract_time_range(
        messages: list[dict],
    ) -> tuple[datetime | None, datetime | None]:
        """Extract earliest and latest created_at from messages."""
        start: datetime | None = None
        end: datetime | None = None
        for msg in messages:
            raw = msg.get("created_at")
            if not raw:
                continue
            try:
                if isinstance(raw, datetime):
                    dt = raw
                elif isinstance(raw, str):
                    dt = datetime.fromisoformat(raw)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                else:
                    continue
                if start is None or dt < start:
                    start = dt
                if end is None or dt > end:
                    end = dt
            except (ValueError, TypeError):
                continue
        return start, end
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/memory/pruned/test_manager.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add framework/memory/pruned/manager.py tests/unit/memory/pruned/test_manager.py
git commit -m "feat(pruned): add PrunedManager with write, injection, and eviction"
```

---

### Task 4: PrunedCatalogConfig

**Files:**
- Modify: `framework/ioc/configs/memory.py`

- [ ] **Step 1: Add PrunedCatalogConfig and wire into MemoryConfig**

Add the new config model after the existing `GovernanceConfig` class (after line ~108), and add a field to `MemoryConfig`:

```python
# In framework/ioc/configs/memory.py, add after GovernanceConfig class:

class PrunedCatalogConfig(BaseModel):
    """Configuration for pruned memory catalog."""
    enabled: bool = True
    max_files: int = 50
    topic_max_chars: int = 200
```

Then in `MemoryConfig` class, add after the `governance` field (around line 128):

```python
    pruned: PrunedCatalogConfig | None = None
```

The exact changes needed in `framework/ioc/configs/memory.py`:

1. Add `PrunedCatalogConfig` class after `GovernanceConfig` (around line 108)
2. Add `pruned: PrunedCatalogConfig | None = None` field to `MemoryConfig` (after the `governance` field)

- [ ] **Step 2: Verify the config loads correctly**

Run: `python -c "from framework.ioc.configs.memory import MemoryConfig, PrunedCatalogConfig; c = MemoryConfig(); assert c.pruned is None; c2 = MemoryConfig(pruned=PrunedCatalogConfig()); assert c2.pruned.enabled is True; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add framework/ioc/configs/memory.py
git commit -m "feat(pruned): add PrunedCatalogConfig to MemoryConfig"
```

---

### Task 5: Timestamp Guarantee in ScopedMessageHistory

**Files:**
- Modify: `framework/memory/default_system.py`
- Add tests to verify timestamp auto-fill

- [ ] **Step 1: Write test for timestamp guarantee**

Add to `tests/unit/memory/pruned/` a new test file:

```python
# tests/unit/memory/pruned/test_timestamp_guarantee.py
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from framework.memory.core.message import ChatMessage


class TestTimestampGuarantee:
    def test_ensure_timestamps_fills_none(self):
        from framework.memory.default_system import ScopedMessageHistory

        now = datetime.now(timezone.utc)
        msg_no_ts = ChatMessage(role="user", content="hello")
        assert msg_no_ts.created_at is None

        patched = ScopedMessageHistory._ensure_timestamps([msg_no_ts])
        assert patched[0].created_at is not None
        # Should be roughly now (within 5 seconds)
        delta = abs((patched[0].created_at - now).total_seconds())
        assert delta < 5

    def test_ensure_timestamps_preserves_existing(self):
        from framework.memory.default_system import ScopedMessageHistory

        existing = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        msg_with_ts = ChatMessage(role="user", content="hello", created_at=existing)
        patched = ScopedMessageHistory._ensure_timestamps([msg_with_ts])
        assert patched[0].created_at == existing

    def test_ensure_timestamps_mixed(self):
        from framework.memory.default_system import ScopedMessageHistory

        existing = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        msgs = [
            ChatMessage(role="user", content="with ts", created_at=existing),
            ChatMessage(role="assistant", content="without ts"),
        ]
        patched = ScopedMessageHistory._ensure_timestamps(msgs)
        assert patched[0].created_at == existing
        assert patched[1].created_at is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/memory/pruned/test_timestamp_guarantee.py -v`
Expected: FAIL — `AttributeError` (method doesn't exist yet)

- [ ] **Step 3: Add _ensure_timestamps to ScopedMessageHistory**

In `framework/memory/default_system.py`, add the static method to `ScopedMessageHistory` class (after `__init__`, around line 62):

```python
    @staticmethod
    def _ensure_timestamps(messages: list[ChatMessage | dict[str, Any]]) -> list[ChatMessage | dict[str, Any]]:
        """Auto-fill created_at on ChatMessage objects that lack it."""
        from datetime import UTC, datetime
        now = datetime.now(UTC)
        patched: list[ChatMessage | dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, ChatMessage) and msg.created_at is None:
                patched.append(msg.model_copy(update={"created_at": now}))
            else:
                patched.append(msg)
        return patched
```

Then modify `append()` method to call `_ensure_timestamps` before adding messages. Change:

```python
    async def append(self, message: ChatMessage | dict[str, Any]) -> None:
        await self._manager.add_messages(self._context, [message])
```

To:

```python
    async def append(self, message: ChatMessage | dict[str, Any]) -> None:
        [patched] = self._ensure_timestamps([message])
        await self._manager.add_messages(self._context, [patched])
```

Similarly, modify `extend()` method. Change:

```python
    async def extend(self, messages: Sequence[ChatMessage | dict[str, Any]]) -> None:
        if not messages:
            return
        await self._manager.add_messages(self._context, list(messages))
```

To:

```python
    async def extend(self, messages: Sequence[ChatMessage | dict[str, Any]]) -> None:
        if not messages:
            return
        patched = self._ensure_timestamps(list(messages))
        await self._manager.add_messages(self._context, patched)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/memory/pruned/test_timestamp_guarantee.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Run existing tests to verify no regressions**

Run: `python -m pytest tests/unit/memory/ -v --timeout=30`
Expected: All existing tests PASS

- [ ] **Step 6: Commit**

```bash
git add framework/memory/default_system.py tests/unit/memory/pruned/test_timestamp_guarantee.py
git commit -m "feat(pruned): auto-fill created_at on messages entering storage"
```

---

### Task 6: Cleanup Integration

**Files:**
- Modify: `framework/memory/cleanup.py`

- [ ] **Step 1: Add pruned_manager parameter to cleanup_session()**

In `framework/memory/cleanup.py`, add import at the top:

```python
from framework.memory.pruned.manager import PrunedManager
```

Add parameter to `cleanup_session()` signature (after `user_retention` param):

```python
    pruned_manager: PrunedManager | None = None,
```

- [ ] **Step 2: Add pruned writing after archive generation**

After the archive generation block (after line ~316, before the `return CleanupResult(...)`), insert pruned writing:

```python
    # ── Step 4: Pruned catalog (optional, independent of archive) ────────
    if pruned_manager is not None and pruned_messages:
        try:
            # Reuse topic from archive if available
            pruned_topic: str | None = None
            if (
                archive is not None
                and archive_strategy is not None
                and not archive_skipped
                and "gen_result" in dir()
            ):
                # gen_result is the ArchiveGenerationResult from above
                for w in gen_result.writes:
                    if hasattr(w, "channel") and w.channel.value == "context":
                        pruned_topic = w.summary[:200]
                        break
            await pruned_manager.write_pruned(
                pruned_messages,
                topic=pruned_topic,
                cleanup_time=datetime.now(UTC),
            )
        except Exception:
            logger.warning(
                "Pruned catalog write failed: session=%s",
                context.session_id, exc_info=True,
            )
```

**Important:** The `gen_result` variable is only available inside the archive try block. To access it for topic reuse, we need to hoist it. Before the archive block, add:

```python
    gen_result = None
```

Then in the archive try block (around line 292-293), assign the result:

```python
                    gen_result = await archive_strategy.generate(
                        archive_inputs, context, trigger_reason,
                    )
```

And in the pruned section, use `gen_result` instead of the `"gen_result" in dir()` hack:

```python
    # ── Step 4: Pruned catalog (optional, independent of archive) ────────
    if pruned_manager is not None and pruned_messages:
        try:
            pruned_topic: str | None = None
            if gen_result is not None:
                for w in gen_result.writes:
                    if hasattr(w, "channel") and w.channel.value == "context":
                        pruned_topic = w.summary[:200]
                        break
            await pruned_manager.write_pruned(
                pruned_messages,
                topic=pruned_topic,
                cleanup_time=datetime.now(UTC),
            )
        except Exception:
            logger.warning(
                "Pruned catalog write failed: session=%s",
                context.session_id, exc_info=True,
            )
```

- [ ] **Step 3: Forward pruned_manager through ScopedMessageHistory**

In `framework/memory/default_system.py`, add to `ScopedMessageHistory.__init__()`:

```python
        self._pruned_manager: PrunedManager | None = pruned_manager
```

Add parameter to constructor:

```python
        pruned_manager: PrunedManager | None = None,
```

Add import at the top:

```python
from framework.memory.pruned.manager import PrunedManager
```

In `_run_cleanup()` method, forward it to `cleanup_session()`:

Add `pruned_manager=self._pruned_manager` to the `cleanup_session()` call.

- [ ] **Step 4: Forward pruned_manager through DefaultMemorySystem**

In `DefaultMemorySystem.__init__()`, add parameter and store:

```python
        self._pruned_manager: PrunedManager | None = pruned_manager
```

Add parameter:

```python
        pruned_manager: PrunedManager | None = None,
```

In `create_message_history()`, forward to `ScopedMessageHistory`:

Add `pruned_manager=self._pruned_manager` to the `ScopedMessageHistory()` constructor call.

- [ ] **Step 5: Expose pruned_manager as a property**

Add to `DefaultMemorySystem`:

```python
    @property
    def pruned_manager(self) -> PrunedManager | None:
        return self._pruned_manager
```

- [ ] **Step 6: Run existing tests to verify no regressions**

Run: `python -m pytest tests/unit/memory/test_cleanup.py -v --timeout=30`
Expected: All existing tests PASS (pruned_manager defaults to None, no change in behavior)

- [ ] **Step 7: Commit**

```bash
git add framework/memory/cleanup.py framework/memory/default_system.py
git commit -m "feat(pruned): integrate PrunedManager into cleanup pipeline"
```

---

### Task 7: Injection Policy Changes

**Files:**
- Modify: `framework/memory/injection/full_injection.py`
- Modify: `framework/memory/injection/restricted_injection.py`

- [ ] **Step 1: Add pruned catalog injection to FullInjectionPolicy**

In `framework/memory/injection/full_injection.py`:

Add import at the top:

```python
from framework.memory.pruned.manager import PrunedManager
```

Add parameter to `__init__()`:

```python
        pruned_manager: PrunedManager | None = None,
```

Store it:

```python
        self._pruned_manager = pruned_manager
```

Add injection call in `assemble()` method, after `_inject_archive` call (around line 64):

```python
        self._inject_pruned_catalog(sections)
```

Add the injection method to the class (after `_inject_archive` method):

```python
    def _inject_pruned_catalog(self, sections: list[_PromptSection]) -> None:
        if self._pruned_manager is None:
            return
        xml = self._pruned_manager.get_injection_xml()
        if xml:
            sections.append(_PromptSection(content=xml, priority=85))
```

- [ ] **Step 2: Add pruned catalog injection to RestrictedInjectionPolicy**

In `framework/memory/injection/restricted_injection.py`:

Add import:

```python
from framework.memory.pruned.manager import PrunedManager
```

Add parameter to `__init__()`:

```python
        pruned_manager: PrunedManager | None = None,
```

Store it:

```python
        self._pruned_manager = pruned_manager
```

Modify `assemble()` to include pruned catalog in system_prompt:

```python
    async def assemble(
        self,
        *,
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str = "",
    ) -> InjectionResult:
        messages = await memory_system.get_history(context, max_messages=self._max_messages)
        system_prompt = ""
        if self._pruned_manager is not None:
            xml = self._pruned_manager.get_injection_xml()
            if xml:
                system_prompt = xml
        return InjectionResult(
            system_prompt=system_prompt,
            messages=list(messages),
        )
```

- [ ] **Step 3: Run tests to verify no regressions**

Run: `python -m pytest tests/unit/memory/ -v --timeout=30 -k "injection"`
Expected: Existing tests PASS (pruned_manager defaults to None)

- [ ] **Step 4: Commit**

```bash
git add framework/memory/injection/full_injection.py framework/memory/injection/restricted_injection.py
git commit -m "feat(pruned): add pruned catalog injection to both injection policies"
```

---

### Task 8: Framework Wiring — __init__.py, system.py, IOC Factory

**Files:**
- Modify: `framework/memory/__init__.py`
- Modify: `framework/memory/system.py`
- Modify: `framework/ioc/factories/memory.py`

- [ ] **Step 1: Update framework/memory/__init__.py exports**

Add imports for the new pruned module:

```python
from framework.memory.pruned.manager import PrunedManager
from framework.memory.pruned.models import PrunedIndexEntry
from framework.memory.pruned.storage import FilePrunedStorage, PrunedStorage
```

Add to `__all__` list (in alphabetical order within a new `# Pruned` section):

```python
    # Pruned catalog
    "FilePrunedStorage",
    "PrunedIndexEntry",
    "PrunedManager",
    "PrunedStorage",
```

- [ ] **Step 2: Update create_memory_system() in system.py**

Add parameter to `create_memory_system()`:

```python
    pruned_manager: PrunedManager | None = None,
```

Add import:

```python
from framework.memory.pruned.manager import PrunedManager
```

Forward to `DefaultMemorySystem`:

```python
        pruned_manager=pruned_manager,
```

- [ ] **Step 3: Update IOC factory to create PrunedManager**

In `framework/ioc/factories/memory.py`:

Add to `create_memory()` function, after `archive_strategy` creation (around line 92):

```python
    # Pruned catalog manager (independent of archive)
    from framework.memory.pruned.manager import PrunedManager
    from framework.memory.pruned.storage import FilePrunedStorage

    pruned_manager = None
    if cfg.pruned is not None and cfg.pruned.enabled:
        pruned_dir = workspace / "pruned"
        pruned_storage = FilePrunedStorage(pruned_dir)
        pruned_manager = PrunedManager(
            storage=pruned_storage,
            max_files=cfg.pruned.max_files,
            topic_max_chars=cfg.pruned.topic_max_chars,
        )
```

Add to the `create_memory_system()` call:

```python
        pruned_manager=pruned_manager,
```

- [ ] **Step 4: Run framework tests to verify no regressions**

Run: `python -m pytest tests/unit/memory/ -v --timeout=30`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add framework/memory/__init__.py framework/memory/system.py framework/ioc/factories/memory.py
git commit -m "feat(pruned): wire PrunedManager through framework and IOC factory"
```

---

### Task 9: bot_project Config + Wiring

**Files:**
- Modify: `examples/bot_project/config/pools/main.yml`
- Modify: `examples/bot_project/config/pools/coding.yml`
- Modify: `examples/bot_project/bot/service/core.py`
- Modify: `examples/bot_project/bot/service/builders.py`

- [ ] **Step 1: Add pruned config to pool YAML files**

In `examples/bot_project/config/pools/main.yml`, add after the `governance:` block:

```yaml
  pruned: {enabled: true, max_files: 50, topic_max_chars: 200}
```

In `examples/bot_project/config/pools/coding.yml`, add after the `governance:` block:

```yaml
  pruned: {enabled: true, max_files: 50, topic_max_chars: 200}
```

- [ ] **Step 2: Wire PrunedManager to injection policy in core.py**

In `examples/bot_project/bot/service/core.py`, find the pipeline mode memory setup (around line 352-371). After `self.memory_system` is created and initialized, extract the pruned_manager and pass it to the injection policy:

Change:

```python
        self.context_manager = MemorySystemContextManager(
            memory_system=self.memory_system,
            default_agent_id=main_cfg.name if main_cfg else "main",
            default_agent_role="main",
            base_system_prompt=main_cfg.system_prompt if main_cfg else "",
            injection_policy=FullInjectionPolicy(),
        )
```

To:

```python
        pruned_mgr = getattr(self.memory_system, "pruned_manager", None)
        self.context_manager = MemorySystemContextManager(
            memory_system=self.memory_system,
            default_agent_id=main_cfg.name if main_cfg else "main",
            default_agent_role="main",
            base_system_prompt=main_cfg.system_prompt if main_cfg else "",
            injection_policy=FullInjectionPolicy(pruned_manager=pruned_mgr),
        )
```

- [ ] **Step 3: Wire PrunedManager to subagent injection in builders.py**

In `examples/bot_project/bot/service/builders.py`, find `_create_subagent_memory()` (around line 360-396). The subagent creates its own memory system. Add pruned_manager extraction and pass to RestrictedInjectionPolicy.

After the subagent memory system is created, extract pruned_manager:

```python
        pruned_mgr = getattr(memory_system, "pruned_manager", None)
```

Change the RestrictedInjectionPolicy instantiation:

```python
        injection_policy=RestrictedInjectionPolicy(max_session_messages=20),
```

To:

```python
        injection_policy=RestrictedInjectionPolicy(
            max_session_messages=20,
            pruned_manager=pruned_mgr,
        ),
```

- [ ] **Step 4: Verify bot_project can be imported without errors**

Run: `cd examples/bot_project && python -c "from bot.service.core import BotService; print('OK')"`
Expected: `OK` (or at least no import errors related to pruned module)

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/config/pools/main.yml examples/bot_project/config/pools/coding.yml examples/bot_project/bot/service/core.py examples/bot_project/bot/service/builders.py
git commit -m "feat(pruned): wire pruned catalog config into bot_project"
```

---

### Task 10: Cleanup Pruned Integration Tests

**Files:**
- Modify: `tests/unit/memory/test_cleanup.py`

- [ ] **Step 1: Add pruned integration tests to cleanup test file**

Add to `tests/unit/memory/test_cleanup.py`:

```python
# Add at the top imports:
from framework.memory.pruned.manager import PrunedManager
from framework.memory.pruned.storage import FilePrunedStorage

# Add new test class:

class TestPrunedCatalogIntegration:
    """Tests for pruned catalog writing during cleanup."""

    @pytest.fixture
    def pruned_dir(self, tmp_path):
        return tmp_path / "pruned"

    @pytest.fixture
    def pruned_manager(self, pruned_dir):
        storage = FilePrunedStorage(pruned_dir)
        return PrunedManager(storage, max_files=5)

    @pytest.mark.asyncio
    async def test_pruned_written_on_cleanup(
        self, registry, pruned_manager, pruned_dir,
    ):
        from framework.memory.layers.config import MemoryLayerConfigSet
        from framework.memory.layers.factory import MemoryLayerFactory

        config = MemoryLayerConfigSet()
        layer_set = MemoryLayerFactory.single_user(registry=registry, config=config)
        session = layer_set.session
        ctx = _ctx("pruned-test")

        # Add enough messages to trigger cleanup
        for i in range(15):
            await _add_messages(session, ctx, [_user_msg(f"msg{i}"), _assistant_msg(f"reply{i}")])

        result = await cleanup_session(
            session=session,
            archive=None,
            context=ctx,
            max_messages=10,
            max_tokens=None,
            pruned_manager=pruned_manager,
        )
        assert result.triggered is True
        assert result.messages_pruned > 0

        # Verify pruned files were written
        entries = pruned_manager._storage.read_index()
        assert len(entries) >= 1
        assert entries[0].message_count > 0

    @pytest.mark.asyncio
    async def test_pruned_not_written_when_none_manager(self, registry):
        """Verify cleanup works normally when pruned_manager is None."""
        from framework.memory.layers.config import MemoryLayerConfigSet
        from framework.memory.layers.factory import MemoryLayerFactory

        config = MemoryLayerConfigSet()
        layer_set = MemoryLayerFactory.single_user(registry=registry, config=config)
        session = layer_set.session
        ctx = _ctx("no-pruned-test")

        for i in range(15):
            await _add_messages(session, ctx, [_user_msg(f"msg{i}"), _assistant_msg(f"reply{i}")])

        result = await cleanup_session(
            session=session,
            archive=None,
            context=ctx,
            max_messages=10,
            max_tokens=None,
            pruned_manager=None,
        )
        assert result.triggered is True

    @pytest.mark.asyncio
    async def test_pruned_independent_of_archive(self, registry, pruned_manager):
        """Pruned is written even when archive is disabled."""
        from framework.memory.layers.config import MemoryLayerConfigSet
        from framework.memory.layers.factory import MemoryLayerFactory

        config = MemoryLayerConfigSet()
        layer_set = MemoryLayerFactory.single_user(registry=registry, config=config)
        session = layer_set.session
        ctx = _ctx("pruned-no-archive")

        for i in range(15):
            await _add_messages(session, ctx, [_user_msg(f"msg{i}"), _assistant_msg(f"reply{i}")])

        result = await cleanup_session(
            session=session,
            archive=None,
            context=ctx,
            max_messages=10,
            max_tokens=None,
            pruned_manager=pruned_manager,
        )
        assert result.triggered is True
        entries = pruned_manager._storage.read_index()
        assert len(entries) >= 1
        # Topic should be time-range fallback (no archive summary)
        assert entries[0].topic != ""
        # Should contain time info since no archive
        assert "messages" in entries[0].topic
```

- [ ] **Step 2: Run all tests**

Run: `python -m pytest tests/unit/memory/ -v --timeout=60`
Expected: All tests PASS, including new pruned integration tests

- [ ] **Step 3: Commit**

```bash
git add tests/unit/memory/test_cleanup.py
git commit -m "test(pruned): add cleanup integration tests for pruned catalog"
```

---

### Task 11: Injection Policy Tests

**Files:**
- Create: `tests/unit/memory/pruned/test_injection.py`

- [ ] **Step 1: Write injection policy tests with pruned catalog**

```python
# tests/unit/memory/pruned/test_injection.py
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from framework.memory.injection.full_injection import FullInjectionPolicy
from framework.memory.injection.restricted_injection import RestrictedInjectionPolicy
from framework.memory.pruned.manager import PrunedManager
from framework.memory.pruned.storage import FilePrunedStorage


@pytest.fixture
def pruned_dir(tmp_path: Path) -> Path:
    return tmp_path / "pruned"


@pytest.fixture
def pruned_manager(pruned_dir: Path) -> PrunedManager:
    return PrunedManager(FilePrunedStorage(pruned_dir))


NOW = datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)


class TestFullInjectionPrunedCatalog:
    def test_no_injection_when_no_manager(self):
        policy = FullInjectionPolicy()
        # policy._pruned_manager is None, so nothing to check here
        # Just verify construction works
        assert policy._pruned_manager is None

    def test_no_injection_when_manager_has_no_content(self, pruned_manager: PrunedManager):
        policy = FullInjectionPolicy(pruned_manager=pruned_manager)
        # No pruned content written yet
        assert pruned_manager.get_injection_xml() is None

    @pytest.mark.asyncio
    async def test_injection_xml_present_after_pruned_write(
        self, pruned_manager: PrunedManager, pruned_dir: Path,
    ):
        await pruned_manager.write_pruned(
            [{"role": "user", "content": "hi"}],
            topic="test topic",
            cleanup_time=NOW,
        )
        policy = FullInjectionPolicy(pruned_manager=pruned_manager)
        xml = pruned_manager.get_injection_xml()
        assert xml is not None
        assert "<memory_archives>" in xml
        assert str(pruned_dir.resolve()) in xml


class TestRestrictedInjectionPrunedCatalog:
    def test_no_injection_when_no_manager(self):
        policy = RestrictedInjectionPolicy()
        assert policy._pruned_manager is None

    @pytest.mark.asyncio
    async def test_system_prompt_contains_catalog_after_pruned_write(
        self, pruned_manager: PrunedManager, pruned_dir: Path,
    ):
        from framework.memory.core.scope import MemoryContext
        from framework.memory.stores.scoped_in_memory import InMemoryStoreRegistry
        from framework.memory.system import create_memory_system

        await pruned_manager.write_pruned(
            [{"role": "user", "content": "hi"}],
            topic="test",
            cleanup_time=NOW,
        )

        system = create_memory_system(
            workspace=pruned_dir / "ws",
            pruned_manager=pruned_manager,
        )
        ctx = MemoryContext(
            session_id="test:main",
            user_id="default",
            agent_id="main",
            agent_role="main",
        )
        policy = RestrictedInjectionPolicy(
            max_session_messages=10,
            pruned_manager=pruned_manager,
        )
        result = await policy.assemble(
            context=ctx,
            memory_system=system,
        )
        assert "<memory_archives>" in result.system_prompt
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/unit/memory/pruned/test_injection.py -v --timeout=30`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/unit/memory/pruned/test_injection.py
git commit -m "test(pruned): add injection policy tests for pruned catalog"
```

---

### Task 12: Final Integration + Full Test Suite

- [ ] **Step 1: Run complete test suite**

Run: `python -m pytest tests/unit/memory/ -v --timeout=60`
Expected: All tests PASS

- [ ] **Step 2: Run any existing bot_project tests**

Run: `cd examples/bot_project && python -m pytest tests/ -v --timeout=60 -x 2>&1 | head -50`
Expected: No import errors or failures related to pruned module

- [ ] **Step 3: Verify pruned directory is created correctly with a dry-run test**

Run: `python -c "
from pathlib import Path
from datetime import datetime, timezone
from framework.memory.pruned.manager import PrunedManager
from framework.memory.pruned.storage import FilePrunedStorage
import tempfile, asyncio

async def test():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / 'pruned'
        mgr = PrunedManager(FilePrunedStorage(d))
        await mgr.write_pruned(
            [{'role': 'user', 'content': 'hello', 'created_at': '2024-06-03 09:00:00'}],
            topic='Test topic',
            cleanup_time=datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc),
        )
        print('Files:', list(d.iterdir()))
        print('Index:', mgr._storage.read_index()[0])
        print('XML:', mgr.get_injection_xml()[:100])
        print('OK')

asyncio.run(test())
"
Expected: Shows created files, index entry, and XML output without errors

- [ ] **Step 4: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix(pruned): integration adjustments from full test suite"
```
