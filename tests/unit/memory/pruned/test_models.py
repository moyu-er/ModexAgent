import json

import pytest

from framework.memory.pruned.models import PrunedIndexEntry


class TestPrunedIndexEntry:

    def test_creation_defaults(self) -> None:
        entry = PrunedIndexEntry(
            id=1,
            cleanup_time=1717500000,
            cleanup_time_display="2024-06-04 12:00",
            message_count=5,
            content_filename="pruned_001.json",
        )
        assert entry.id == 1
        assert entry.cleanup_time == 1717500000
        assert entry.cleanup_time_display == "2024-06-04 12:00"
        assert entry.message_count == 5
        assert entry.content_filename == "pruned_001.json"
        assert entry.start_time == 0
        assert entry.end_time == 0
        assert entry.start_time_display == ""
        assert entry.end_time_display == ""
        assert entry.topic == ""

    def test_creation_full(self) -> None:
        entry = PrunedIndexEntry(
            id=2,
            cleanup_time=1717500000,
            cleanup_time_display="2024-06-04 12:00",
            message_count=10,
            content_filename="pruned_002.json",
            start_time=1717490000,
            end_time=1717499000,
            start_time_display="2024-06-04 09:13",
            end_time_display="2024-06-04 11:43",
            topic="API integration debugging",
        )
        assert entry.start_time == 1717490000
        assert entry.end_time == 1717499000
        assert entry.start_time_display == "2024-06-04 09:13"
        assert entry.end_time_display == "2024-06-04 11:43"
        assert entry.topic == "API integration debugging"

    def test_frozen(self) -> None:
        entry = PrunedIndexEntry(
            id=1,
            cleanup_time=0,
            cleanup_time_display="",
            message_count=0,
            content_filename="x.json",
        )
        with pytest.raises(AttributeError):
            entry.id = 99  # type: ignore[misc]

    def test_to_dict_round_trip(self) -> None:
        original = PrunedIndexEntry(
            id=3,
            cleanup_time=1717500000,
            cleanup_time_display="2024-06-04 12:00",
            message_count=7,
            content_filename="pruned_003.json",
            start_time=1717490000,
            end_time=1717499000,
            start_time_display="2024-06-04 09:13",
            end_time_display="2024-06-04 11:43",
            topic="database migration",
        )
        serialized = json.dumps(original.to_dict())
        restored = PrunedIndexEntry.from_dict(json.loads(serialized))
        assert restored == original

    def test_from_dict_missing_optional_fields(self) -> None:
        data = {
            "id": 4,
            "cleanup_time": 1717500000,
            "cleanup_time_display": "2024-06-04 12:00",
            "message_count": 3,
            "content_filename": "pruned_004.json",
        }
        entry = PrunedIndexEntry.from_dict(data)
        assert entry.id == 4
        assert entry.start_time == 0
        assert entry.end_time == 0
        assert entry.start_time_display == ""
        assert entry.end_time_display == ""
        assert entry.topic == ""

    def test_from_dict_extra_fields_ignored(self) -> None:
        data = {
            "id": 5,
            "cleanup_time": 1717500000,
            "cleanup_time_display": "2024-06-04 12:00",
            "message_count": 1,
            "content_filename": "pruned_005.json",
            "unknown_field": "should be ignored",
            "another_extra": 42,
        }
        entry = PrunedIndexEntry.from_dict(data)
        assert entry.id == 5
        assert entry.message_count == 1
