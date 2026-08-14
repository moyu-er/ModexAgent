"""Storage lock abstraction layer."""

from __future__ import annotations

import asyncio
import threading
from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import TracebackType
from typing import Any, Final
from weakref import WeakValueDictionary


class StorageLock(ABC):
    """Abstract base class for storage-level locks.

    Provides async context managers for read and write access.
    """

    @abstractmethod
    def read(self, timeout: float | None = None) -> AbstractAsyncContextManager[StorageLockContext]:
        """Return an async context manager for read access."""
        pass

    @abstractmethod
    def write(
        self, timeout: float | None = None
    ) -> AbstractAsyncContextManager[StorageLockContext]:
        """Return an async context manager for write access."""
        pass


class StorageLockContext(AbstractAsyncContextManager["StorageLockContext"]):
    """Async context manager returned by StorageLock.read() / write()."""

    async def __aenter__(self) -> StorageLockContext:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.release()

    @abstractmethod
    async def acquire(self) -> None:
        pass

    @abstractmethod
    async def release(self) -> None:
        pass


class _AioRWLockReadContext(StorageLockContext):
    def __init__(self, lock: AioRWLock, timeout: float | None = None) -> None:
        self._lock = lock
        self._timeout = timeout

    async def acquire(self) -> None:
        if self._timeout is None:
            await self._lock.acquire_read()
            return
        await asyncio.wait_for(self._lock.acquire_read(), timeout=self._timeout)

    async def release(self) -> None:
        await self._lock.release_read()


class _AioRWLockWriteContext(StorageLockContext):
    def __init__(self, lock: AioRWLock, timeout: float | None = None) -> None:
        self._lock = lock
        self._timeout = timeout

    async def acquire(self) -> None:
        if self._timeout is None:
            await self._lock.acquire_write()
            return
        await asyncio.wait_for(self._lock.acquire_write(), timeout=self._timeout)

    async def release(self) -> None:
        await self._lock.release_write()


class AioRWLock(StorageLock):
    """Writer-reentrant asyncio RWLock.

    Tracks the writer task using asyncio.current_task() to allow the same
    task to re-enter the write lock. If the current task already holds the
    write lock, acquiring the read lock is a no-op.
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._writer_task: asyncio.Task[Any] | None = None
        self._writer_depth: int = 0
        self._reader_depths: dict[asyncio.Task[Any], int] = {}
        self._active_readers = 0
        self._waiting_writers = 0

    async def acquire_read(self) -> None:
        current = asyncio.current_task()
        if self._writer_task is not None and self._writer_task == current:
            return
        if current is None:
            raise RuntimeError("Cannot acquire a storage read lock outside an asyncio task")
        if current in self._reader_depths:
            self._reader_depths[current] += 1
            self._active_readers += 1
            return
        async with self._condition:
            await self._condition.wait_for(
                lambda: self._writer_task is None and self._waiting_writers == 0
            )
            self._reader_depths[current] = 1
            self._active_readers += 1

    async def release_read(self) -> None:
        current = asyncio.current_task()
        if self._writer_task is not None and self._writer_task == current:
            return
        if current is not None:
            depth = self._reader_depths.get(current)
            if depth is not None:
                async with self._condition:
                    if depth <= 1:
                        self._reader_depths.pop(current, None)
                    else:
                        self._reader_depths[current] = depth - 1
                    self._active_readers -= 1
                    if self._active_readers == 0:
                        self._condition.notify_all()
                return
        raise RuntimeError("Cannot release a read lock not held by the current task")

    async def acquire_write(self) -> None:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("Cannot acquire a storage write lock outside an asyncio task")
        if self._writer_task is not None and self._writer_task == current:
            self._writer_depth += 1
            return
        async with self._condition:
            self._waiting_writers += 1
            try:
                await self._condition.wait_for(
                    lambda: self._writer_task is None and self._active_readers == 0
                )
                self._writer_task = current
                self._writer_depth = 1
            finally:
                self._waiting_writers -= 1
                if self._waiting_writers == 0 and self._writer_task is None:
                    self._condition.notify_all()

    async def release_write(self) -> None:
        current = asyncio.current_task()
        if self._writer_task is not None and self._writer_task == current:
            self._writer_depth -= 1
            if self._writer_depth == 0:
                async with self._condition:
                    self._writer_task = None
                    self._condition.notify_all()
            return
        # Prevent illegal release of a lock not held by the current task
        raise RuntimeError("Cannot release a write lock not held by the current task")

    def read(self, timeout: float | None = None) -> StorageLockContext:
        return _AioRWLockReadContext(self, timeout=timeout)

    def write(self, timeout: float | None = None) -> StorageLockContext:
        return _AioRWLockWriteContext(self, timeout=timeout)


_FILE_PROCESS_LOCKS: WeakValueDictionary[str, AioRWLock] = WeakValueDictionary()
_FILE_PROCESS_LOCKS_GUARD: Final = threading.Lock()


class NoOpStorageLock(StorageLock):
    """Zero-overhead null implementation for single-threaded use."""

    class _NoOpContext(StorageLockContext):
        async def acquire(self) -> None:
            pass

        async def release(self) -> None:
            pass

    def read(self, timeout: float | None = None) -> StorageLockContext:
        _ = timeout
        return self._NoOpContext()

    def write(self, timeout: float | None = None) -> StorageLockContext:
        _ = timeout
        return self._NoOpContext()


class FileStorageLock(StorageLock):
    """Cross-process file-based lock using filelock.

    Uses an exclusive file lock for both read and write operations.
    Writer-reentrant within the same asyncio task to prevent deadlocks
    when storage methods call each other while holding the lock.
    filelock is imported lazily so it remains an optional dependency.
    """

    def __init__(self, lock_file: Path | str) -> None:
        try:
            import filelock
        except ImportError as exc:
            raise ImportError(
                "filelock is required for FileStorageLock. Install it with: pip install filelock"
            ) from exc
        self._lock_file = Path(lock_file).resolve()
        lock_key = str(self._lock_file)
        with _FILE_PROCESS_LOCKS_GUARD:
            # Share a single FileLock + AioRWLock pair per resolved path so
            # that multiple FileStorageLock instances on the same path are
            # reentrant within the process (filelock.FileLock without
            # is_singleton=True deadlocks on cross-instance reentry).
            entry = _FILE_PROCESS_LOCKS.get(lock_key)
            if entry is None:
                entry = filelock.FileLock(lock_key, thread_local=False)
                entry._modex_process_lock = AioRWLock()
                _FILE_PROCESS_LOCKS[lock_key] = entry
            self._lock = entry
            self._process_lock = entry._modex_process_lock

    class _FileLockContext(StorageLockContext):
        def __init__(
            self,
            owner: FileStorageLock,
            timeout: float | None = None,
        ) -> None:
            self._owner = owner
            self._timeout = timeout
            self._process_context = owner._process_lock.write(timeout=timeout)
            self._acquired = False

        async def acquire(self) -> None:
            await self._process_context.acquire()
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._owner._lock.acquire, self._timeout)
                self._acquired = True
            finally:
                if not self._acquired:
                    await self._process_context.release()

        async def release(self) -> None:
            if self._acquired:
                loop = asyncio.get_running_loop()
                try:
                    await loop.run_in_executor(None, self._owner._lock.release)
                finally:
                    self._acquired = False
                    await self._process_context.release()

    def read(self, timeout: float | None = None) -> StorageLockContext:
        return self._FileLockContext(self, timeout=timeout)

    def write(self, timeout: float | None = None) -> StorageLockContext:
        return self._FileLockContext(self, timeout=timeout)
