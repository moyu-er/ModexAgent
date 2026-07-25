"""`WriteConflictDetector` ABC + `GenerationWriteTracker` default implementation.

Per ADR-0034 D18: conflict detection for `LastValue` fields under
continuous scheduling. Replaces the batch-barrier model where
`apply_concurrent_updates` sees all writes at once.

A **generation** = all instances that forked the same main_state snapshot.
When multiple instances become READY simultaneously (before any merge
occurs), they share the same ``fork_version`` and belong to one
generation. Two instances in the same generation writing the same
``LastValue`` field = conflict (``InvalidUpdateError``).

**Cross-generation concurrency** (continuous scheduling): a new instance
can fork while an older-generation instance is still RUNNING. These two
instances are truly concurrent (the new instance did NOT see the older
one's merged result). To catch conflicts between them, each generation
records a ``concurrent_versions`` set — the set of other generation
versions that had running instances at fork time. The relationship is
**bidirectional**: when a new generation registers, it adds itself to
every in-flight generation's ``concurrent_versions`` and vice versa.
``commit`` checks ``written_fields`` of the committing generation AND all
its ``concurrent_versions``.

Generations are NOT deleted when ``pending_count`` drops to zero — their
``written_fields`` must remain available for later commits from
concurrent instances that registered them before they completed. All
generations are cleared at ``reset()`` (start of each ``run_async``).

Concurrency safety: all methods are synchronous (no ``await``). The
caller (``ParallelScheduler``) must invoke ``commit`` +
``apply_state_update`` + ``advance`` + ``complete`` as one atomic
synchronous segment — no ``await`` between them. asyncio's single-thread
model guarantees no interleaving.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection


class _Generation:
    """Write-tracking state for one fork-version group.

    Regular class with ``__slots__`` — runtime state, not serialized
    (per rule 12: runtime objects with mutable state are regular classes).

    ``concurrent_versions`` records the set of other generation versions
    that had running instances at this generation's registration time
    (bidirectional — those generations also record this one). ``commit``
    checks ``written_fields`` of self AND all ``concurrent_versions`` to
    catch cross-generation concurrent writes.
    """

    __slots__ = ("version", "written_fields", "pending_count", "concurrent_versions")

    def __init__(self, version: int) -> None:
        self.version: int = version
        self.written_fields: set[str] = set()
        self.pending_count: int = 0
        self.concurrent_versions: set[int] = set()


class WriteConflictDetector(ABC):
    """Detects concurrent write conflicts for ``LastValue`` fields.

    The ABC abstracts the conflict-detection strategy so future
    implementations (optimistic retry, custom resolution, distributed
    detection) can replace the default generation-based tracker without
    modifying the scheduler.

    Lifecycle (called by ``ParallelScheduler``):

    1. ``register(fork_version)`` — when an instance forks main_state.
    2. ``commit(fork_version, fields)`` — before merge, raises
       ``InvalidUpdateError`` on same-generation collision.
    3. ``advance()`` — after merge, increments main_state version.
    4. ``complete(fork_version)`` — after merge, decrements writer count.
    5. ``reset()`` — at the start of each ``run_async`` call.
    """

    @abstractmethod
    def register(self, fork_version: int) -> None:
        """Called when an instance forks. Tracks a new concurrent writer
        in the given generation."""
        ...

    @abstractmethod
    def commit(self, fork_version: int, fields: Collection[str]) -> None:
        """Called before merge. Raises ``InvalidUpdateError`` if a
        same-generation instance already wrote any of ``fields``."""
        ...

    @abstractmethod
    def complete(self, fork_version: int) -> None:
        """Called after merge. Decrements the generation's writer count.
        Cleans up when all writers in a generation are done."""
        ...

    @abstractmethod
    def advance(self) -> int:
        """Called after merge. Increments and returns the new
        main_state version. Subsequent forks enter a new generation."""
        ...

    @abstractmethod
    def reset(self) -> None:
        """Called at the start of each ``run_async``. Clears all state."""
        ...


class GenerationWriteTracker(WriteConflictDetector):
    """Default ``WriteConflictDetector`` — generation-based conflict detection.

    Tracks a ``dict[int, _Generation]`` where each ``_Generation`` holds
    the set of ``LastValue`` fields already written by concurrent
    instances in that generation, plus a count of outstanding writers and
    a set of cross-generation concurrent versions.

    Generations are NOT deleted when ``pending_count`` drops to zero.
    Their ``written_fields`` must remain available for ``commit`` calls
    from instances in concurrent generations that registered them before
    they completed. All state is cleared at ``reset()``.
    """

    __slots__ = ("_current_version", "_generations")

    def __init__(self) -> None:
        self._current_version: int = 0
        self._generations: dict[int, _Generation] = {}

    @property
    def current_version(self) -> int:
        """The current main_state version. Instances fork this version."""
        return self._current_version

    def register(self, fork_version: int) -> None:
        """Register a new concurrent writer in generation ``fork_version``.

        Records all other generations with ``pending_count > 0`` as
        concurrent (bidirectional): this generation's ``concurrent_versions``
        gets every in-flight generation, and each in-flight generation's
        ``concurrent_versions`` gets this one. This ensures that a commit
        from either side detects a conflict if both wrote the same
        ``LastValue`` field — even if one completes before the other commits.
        """
        gen = self._generations.setdefault(fork_version, _Generation(fork_version))
        gen.pending_count += 1
        for other_ver, other_gen in self._generations.items():
            if other_ver == fork_version:
                continue
            if other_gen.pending_count > 0:
                gen.concurrent_versions.add(other_ver)
                other_gen.concurrent_versions.add(fork_version)

    def commit(self, fork_version: int, fields: Collection[str]) -> None:
        """Check ``fields`` against own generation AND all concurrent generations.

        Raises ``InvalidUpdateError`` if any concurrent generation (including
        self) already wrote any of ``fields``. After the check, fields are
        added to this generation's ``written_fields`` for future commits.
        """
        from .exceptions import InvalidUpdateError

        gen = self._generations.get(fork_version)
        if gen is None:
            return
        check_fields = gen.written_fields.copy()
        for cv in gen.concurrent_versions:
            other = self._generations.get(cv)
            if other is not None:
                check_fields |= other.written_fields
        for field in fields:
            if field in check_fields:
                raise InvalidUpdateError(
                    f"LastValue field {field!r} written by multiple concurrent "
                    f"instances (generation {fork_version} or its concurrent "
                    f"generations {gen.concurrent_versions}). Use ReducerChannel "
                    f"for fan-in, or ensure only one instance writes this field "
                    f"per generation."
                )
        gen.written_fields.update(fields)

    def complete(self, fork_version: int) -> None:
        """Decrement the generation's writer count.

        Does NOT delete the generation — its ``written_fields`` must remain
        available for ``commit`` calls from instances in concurrent
        generations. Cleanup happens at ``reset()``.
        """
        gen = self._generations.get(fork_version)
        if gen is None:
            return
        gen.pending_count -= 1

    def advance(self) -> int:
        self._current_version += 1
        return self._current_version

    def reset(self) -> None:
        self._current_version = 0
        self._generations.clear()


__all__ = [
    "WriteConflictDetector",
    "GenerationWriteTracker",
]
