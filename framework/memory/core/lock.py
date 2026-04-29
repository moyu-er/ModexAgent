"""Storage lock abstraction layer."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import TracebackType
from typing import Any


class StorageLock(ABC):
    """Abstract base class for storage-level locks.

    Provides async context managers for read and write access.
    """

    @abstractmethod
    def read(self, timeout: float | None = None) -> AbstractAsyncContextManager[StorageLockContext]:
        """Return an async context manager for read access."""
        pass

    @abstractmethod
    def write(self, timeout: float | None = None) -> AbstractAsyncContextManager[StorageLockContext]:
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
                "filelock is required for FileStorageLock. "
                "Install it with: pip install filelock"
            ) from exc
        self._lock_file = Path(lock_file)
        # thread_local=False is required because run_in_executor dispatches
        # acquire/release to different worker threads; thread-local state would
        # hide the lock from the releasing thread and deadlock concurrent waiters.
        self._lock = filelock.FileLock(str(self._lock_file), thread_local=False)
        self._writer_task: asyncio.Task[Any] | None = None
        self._writer_depth: int = 0

    class _FileLockContext(StorageLockContext):
        def __init__(
            self,
            owner: FileStorageLock,
            is_write: bool,
            timeout: float | None = None,
        ) -> None:
            self._owner = owner
            self._is_write = is_write
            self._timeout = timeout
            self._acquired = False

        async def acquire(self) -> None:
            current = asyncio.current_task()
            if self._is_write:
                if self._owner._writer_task is not None and self._owner._writer_task == current:
                    self._owner._writer_depth += 1
                    return
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._owner._lock.acquire, self._timeout)
                self._owner._writer_task = current
                self._owner._writer_depth = 1
                self._acquired = True
            else:
                if self._owner._writer_task is not None and self._owner._writer_task == current:
                    return
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._owner._lock.acquire, self._timeout)
                self._acquired = True

        async def release(self) -> None:
            current = asyncio.current_task()
            if self._is_write and self._owner._writer_task is not None and self._owner._writer_task == current:
                self._owner._writer_depth -= 1
                if self._owner._writer_depth == 0:
                    self._owner._writer_task = None
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self._owner._lock.release)
                return
            if self._acquired:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._owner._lock.release)
                self._acquired = False

    def read(self, timeout: float | None = None) -> StorageLockContext:
        return self._FileLockContext(self, is_write=False, timeout=timeout)

    def write(self, timeout: float | None = None) -> StorageLockContext:
        return self._FileLockContext(self, is_write=True, timeout=timeout)
