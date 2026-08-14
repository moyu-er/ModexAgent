"""`IdGenerator` — Snowflake ID generation primitive for `modex_graph`.

Provides:

- `IdGenerator` ABC (rule 7: ABC, not Protocol) — the single seam for
  generating 64-bit Snowflake-format IDs used as primary keys by the four
  persistence tables (`graph_specs` / `graph_instances` / `node_states` /
  `  deliver_states`) and by `graph_instance_id` (the persistence
  unique key that replaces `run_id`).
- `SnowflakeIdGenerator` — default stdlib-only implementation. Standard
  Twitter Snowflake bit layout: ``timestamp_ms(41) | datacenter(5) |
  machine(5) | sequence(12)`` = 63 bits + sign bit = 64-bit signed int.
- `default_id_generator()` — process-wide lazy singleton (thread-safe).

Per the `modex_graph` dependency boundary (ADR-0033 D11), this module uses
ONLY the Python standard library (`time`, `os`, `threading`). It MUST NOT
add `snowflake-id` or any other external PyPI package. The `pyproject.toml`
dependency list stays `["pydantic>=2.0.0,<3"]` — unchanged.

Bit layout (high → low):

    | 1 sign | 41 timestamp_ms | 5 datacenter_id | 5 machine_id | 12 sequence |

- **timestamp_ms** (41 bits): milliseconds since `_EPOCH` (2024-01-01 UTC).
  Gives ~69 years of runway (until ~2093).
- **datacenter_id** (5 bits): 0..31, caller-supplied, clamped.
- **machine_id** (5 bits): 0..31, caller-supplied, clamped.
- **sequence** (12 bits): 0..4095, per-millisecond counter, lock-protected.

The 5+5 split of the 10-bit "machine" portion is the canonical Snowflake
layout and lets callers differentiate both data centers and workers within
a data center.
"""

from __future__ import annotations

import os
import threading
import time
from abc import ABC, abstractmethod

# ── Snowflake bit-width + layout constants (rule 14) ──────────────────────
# Centralized so the composition math in `generate()` and the clamping in
# `__init__` derive from a single source of truth.

_TIMESTAMP_BITS = 41
_DATACENTER_ID_BITS = 5
_MACHINE_ID_BITS = 5
_SEQUENCE_BITS = 12

# Combined width of the "machine" portion (datacenter + machine) — kept for
# readability of the shift math; the 10-bit field is the classic Snowflake
# worker id.
_WORKER_BITS = _DATACENTER_ID_BITS + _MACHINE_ID_BITS  # 10

# Maximum values each field can hold (bit-range upper bound, inclusive).
_MAX_TIMESTAMP = (1 << _TIMESTAMP_BITS) - 1
_MAX_DATACENTER_ID = (1 << _DATACENTER_ID_BITS) - 1
_MAX_MACHINE_ID = (1 << _MACHINE_ID_BITS) - 1
_MAX_SEQUENCE = (1 << _SEQUENCE_BITS) - 1

# Custom epoch: 2024-01-01 00:00:00 UTC in epoch milliseconds. IDs store
# `current_ms - _EPOCH`, so the 41-bit field lasts until ~2093.
_EPOCH = 1_704_067_200_000

# Left-shift amounts for composing the 63-bit ID.
_TIMESTAMP_SHIFT = _WORKER_BITS + _SEQUENCE_BITS  # 22
_DATACENTER_SHIFT = _MACHINE_ID_BITS + _SEQUENCE_BITS  # 17
_MACHINE_SHIFT = _SEQUENCE_BITS  # 12


class IdGenerator(ABC):
    """Abstract Snowflake-format ID generator (rule 7: ABC, not Protocol).

    A single abstract method `generate()` returns a positive 64-bit int
    suitable for use as a SQLite `BIGINT` / `INTEGER PRIMARY KEY`.

    `modex_graph` persistence uses `IdGenerator` to mint primary
    keys for `graph_specs` / `graph_instances` / `node_states` /
    `deliver_states`, and for `graph_instance_id` (the persistence unique key
    that replaces the in-memory `run_id`).

    Implementations MUST be thread-safe and monotonic within a single
    process. Cross-process uniqueness is the caller's responsibility — supply
    distinct `machine_id` / `datacenter_id` per process.
    """

    @abstractmethod
    def generate(self) -> int:
        """Generate and return a positive Snowflake-format 64-bit int ID."""
        ...


class SnowflakeIdGenerator(IdGenerator):
    """Default stdlib-only Snowflake ID generator.

    Bit layout: ``timestamp_ms(41) | datacenter_id(5) | machine_id(5) |
    sequence(12)`` → 63-bit positive int (sign bit always 0).

    Thread-safe via `threading.Lock` around the timestamp + sequence state.
    Monotonically increasing within a process: each `generate()` call returns
    a value strictly greater than the previous one.

    Clock regression handling: if the wall clock reads earlier than the last
    timestamp used, the generator advances to ``last_timestamp + 1`` to
    preserve strict monotonicity (never emits a smaller or equal ID). This is
    the behavior required by the persistence key contract.

    Sequence overflow: when 4096 IDs are requested within the same
    millisecond, the generator spin-waits for the next millisecond rather
    than overflowing the sequence field or reusing a timestamp.

    Uses only `time`, `os`, `threading` — no external PyPI dependency
    (ADR-0033 D11).
    """

    def __init__(
        self,
        machine_id: int = 0,
        datacenter_id: int = 0,
    ) -> None:
        # Clamp to valid bit range. Negative values wrap via the modulo so
        # callers passing a raw `os.getpid()`-derived value don't blow up.
        self._machine_id = self._clamp(machine_id, _MAX_MACHINE_ID)
        self._datacenter_id = self._clamp(datacenter_id, _MAX_DATACENTER_ID)
        self._last_timestamp_ms = -1
        self._sequence = 0
        self._lock = threading.Lock()

    @staticmethod
    def _clamp(value: int, max_value: int) -> int:
        """Clamp ``value`` into ``[0, max_value]`` via modulo.

        Negative inputs are folded positive first so ``-1`` does not silently
        become a huge number; callers passing `os.getpid()`-derived values
        get a deterministic in-range worker id.
        """
        if value < 0:
            value = -value
        return value % (max_value + 1)

    def generate(self) -> int:
        """Return a positive, monotonic Snowflake-format 64-bit int ID."""
        with self._lock:
            current_ms = int(time.time() * 1000)

            if current_ms < self._last_timestamp_ms:
                # Clock regressed — advance to last + 1 to keep strict
                # monotonicity (persistence key contract: IDs never go backwards).
                current_ms = self._last_timestamp_ms + 1
            elif current_ms == self._last_timestamp_ms:
                # Same millisecond — increment sequence.
                self._sequence = (self._sequence + 1) & _MAX_SEQUENCE
                if self._sequence == 0:
                    # Sequence exhausted for this ms — wait for next ms.
                    current_ms = self._wait_next_ms(current_ms)
            else:
                # New millisecond — reset sequence.
                self._sequence = 0

            self._last_timestamp_ms = current_ms

            timestamp_delta = current_ms - _EPOCH
            if timestamp_delta < 0:
                # Clock is before the epoch — clamp to 0 rather than emit a
                # negative timestamp field (which would flip the sign bit).
                timestamp_delta = 0
            elif timestamp_delta > _MAX_TIMESTAMP:
                # 41-bit overflow (~69 years past epoch) — clamp to max to
                # avoid corrupting the machine/sequence fields. This is a
                # fail-safe, not expected in any realistic timeframe.
                timestamp_delta = _MAX_TIMESTAMP

            return (
                (timestamp_delta << _TIMESTAMP_SHIFT)
                | (self._datacenter_id << _DATACENTER_SHIFT)
                | (self._machine_id << _MACHINE_SHIFT)
                | self._sequence
            )

    @staticmethod
    def _wait_next_ms(current_ms: int) -> int:
        """Spin-wait until the wall clock advances past ``current_ms``.

        Called only when the 12-bit sequence overflows within a single
        millisecond (4096 IDs in one ms). Returns the new millisecond.
        """
        next_ms = int(time.time() * 1000)
        while next_ms <= current_ms:
            next_ms = int(time.time() * 1000)
        return next_ms


# ── Process-wide singleton ────────────────────────────────────────────────
# Lazy-initialized, thread-safe via double-checked locking. The default
# derives `machine_id` from `os.getpid()` (clamped to 5 bits) so independent
# processes are less likely to collide; `datacenter_id` defaults to 0.
# Callers needing exact control construct their own `SnowflakeIdGenerator`
# and inject it — the singleton is only the "no config" path.

_default_generator: SnowflakeIdGenerator | None = None
_default_lock = threading.Lock()


def default_id_generator() -> SnowflakeIdGenerator:
    """Return the process-wide `SnowflakeIdGenerator` singleton.

    Lazily initialized on first call; thread-safe via a module-level lock.
    The singleton's `machine_id` is derived from `os.getpid() % 32` so that
    distinct processes within one host get distinct worker ids (best-effort;
    pid collision is possible but rare for concurrent agent processes).
    Subsequent calls return the same instance.
    """
    global _default_generator
    if _default_generator is None:
        with _default_lock:
            if _default_generator is None:
                _default_generator = SnowflakeIdGenerator(
                    machine_id=os.getpid(),
                    datacenter_id=0,
                )
    return _default_generator


__all__ = [
    "IdGenerator",
    "SnowflakeIdGenerator",
    "default_id_generator",
]
