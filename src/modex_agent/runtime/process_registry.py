from __future__ import annotations

from abc import ABC, abstractmethod

from modex_agent.runtime.process_identity import ProcessIdentity


class ProcessRegistry(ABC):
    """Query and maintain the process IDs currently known to be alive.

    This documented single-implementation seam lets multi-instance deployments
    replace the zero-infrastructure registry with distributed discovery.
    """

    @abstractmethod
    def alive_process_ids(self) -> set[int]:
        """Return the process IDs currently known to be alive."""
        ...

    @abstractmethod
    def register(self, process_id: int) -> None:
        """Register a process ID as alive."""
        ...

    @abstractmethod
    def unregister(self, process_id: int) -> None:
        """Unregister a process ID that is no longer alive."""
        ...


class SingletonProcessRegistry(ProcessRegistry):
    """In-memory registry initialized with the current process identity."""

    def __init__(self, identity: ProcessIdentity) -> None:
        self._alive: set[int] = {identity.process_id}

    def alive_process_ids(self) -> set[int]:
        return set(self._alive)

    def register(self, process_id: int) -> None:
        self._alive.add(process_id)

    def unregister(self, process_id: int) -> None:
        self._alive.discard(process_id)


__all__ = ["ProcessRegistry", "SingletonProcessRegistry"]
