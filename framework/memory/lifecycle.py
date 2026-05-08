"""Lifecycle and maintenance policy ABCs and default implementations.

Phase 6 — turn/session lifecycle hooks and background maintenance tasks.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from framework.core.types import MessageRole
from framework.memory.compression.tool_chain_sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationMode,
)
from framework.memory.core.layers import MemoryLayerSet
from framework.memory.core.scope import (
    MemoryAgentRole,
    MemoryContext,
    MemoryLayerName,
    SessionScope,
)
from framework.memory.registry.base import MemoryStoreRegistry

logger = logging.getLogger(__name__)


def _normalize_role(role: str | MemoryAgentRole | None) -> MemoryAgentRole:
    """Normalize agent role string to MemoryAgentRole enum."""
    if role is None:
        return MemoryAgentRole.MAIN
    if isinstance(role, MemoryAgentRole):
        return role
    try:
        return MemoryAgentRole(role.lower())
    except ValueError:
        return MemoryAgentRole.MAIN


# ── Lifecycle ───────────────────────────────────────────────────────────────


class MemoryLifecyclePolicy(ABC):
    """Turn and session lifecycle hooks called during normal request flow."""

    @abstractmethod
    async def on_turn_start(self, context: MemoryContext, layers: MemoryLayerSet) -> None: ...

    @abstractmethod
    async def on_messages_added(
        self, context: MemoryContext, layers: MemoryLayerSet, revision: Any = None,
    ) -> None: ...

    @abstractmethod
    async def on_turn_end(self, context: MemoryContext, layers: MemoryLayerSet) -> None: ...

    @abstractmethod
    async def on_session_end(self, context: MemoryContext, layers: MemoryLayerSet) -> None: ...


class DefaultMemoryLifecyclePolicy(MemoryLifecyclePolicy):
    """Default lifecycle: trigger compression after messages, flush providers on turn end."""

    def __init__(
        self,
        compression_coordinator: Any | None = None,
    ) -> None:
        self._coordinator = compression_coordinator

    async def on_turn_start(self, context: MemoryContext, layers: MemoryLayerSet) -> None:
        pass

    async def on_messages_added(
        self, context: MemoryContext, layers: MemoryLayerSet, revision: Any = None,
    ) -> None:
        """Trigger compression check after messages are added."""
        _ = revision
        await self._clear_pending_on_completed_assistant(context, layers)
        if self._coordinator is not None:
            try:
                # ReAct writes one logical tool interaction in multiple session
                # appends: assistant(tool_calls) first, then tool(result), then
                # a final assistant answer. Compression must not run while that
                # chain is incomplete or waiting for the model continuation,
                # otherwise it can keep a later tool result while pruning the
                # assistant message that declared its tool_call_id.
                if await self._has_open_react_process(context, layers):
                    logger.debug(
                        "Post-append compression skipped: ReAct process message is pending for %s",
                        context.session_id,
                    )
                    return
                await self._coordinator.maybe_compress(
                    session=layers.session,
                    archive=layers.archive,
                    pending=layers.pending,
                    context=context,
                )
            except Exception:
                logger.warning("Post-append compression check failed", exc_info=True)

    async def _has_open_react_process(
        self,
        context: MemoryContext,
        layers: MemoryLayerSet,
    ) -> bool:
        """Return True when compression could split the active tool-call tail.

        Only protects the *last* assistant with tool_calls. Older stale
        incomplete groups are cleaned by the sanitizer during compression
        and must not block compression forever.
        """
        try:
            raw_messages = await layers.session.get_all_messages(context)
            messages = [
                msg.to_dict() if hasattr(msg, "to_dict") else dict(msg)
                for msg in raw_messages
            ]
        except Exception:
            logger.debug("Unable to inspect session before compression", exc_info=True)
            return False
        if not messages:
            return False

        last = messages[-1]
        last_role = last.get("role")
        # Fast-path: the physical last message is a tool result or an
        # assistant+tools_calls. The current ReAct turn is writing its
        # results; compression must wait for a plain assistant to close
        # the chain.
        if last_role == str(MessageRole.TOOL):
            return True
        if last_role == str(MessageRole.ASSISTANT) and last.get("tool_calls"):
            return True

        # The session ends with a plain assistant or user. Run the full
        # sanitizer pass to check whether the last assistant with tool_calls
        # is still incomplete.
        result = DefaultSessionToolChainSanitizer().sanitize(
            messages,
            mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
        )
        return result.has_open_tail

    async def _clear_pending_on_completed_assistant(
        self,
        context: MemoryContext,
        layers: MemoryLayerSet,
    ) -> None:
        pending = layers.pending
        if pending is None:
            return
        try:
            raw_messages = await layers.session.get_all_messages(context)
            messages = [
                msg.to_dict() if hasattr(msg, "to_dict") else dict(msg)
                for msg in raw_messages
            ]
        except Exception:
            logger.debug("Unable to inspect session for pending clear", exc_info=True)
            return
        if any(
            message.get("role") == MessageRole.ASSISTANT.value and not message.get("tool_calls")
            for message in messages
        ):
            try:
                await pending.clear(context)
            except Exception:
                logger.warning("Failed to clear pending pruned inputs", exc_info=True)

    async def on_turn_end(self, context: MemoryContext, layers: MemoryLayerSet) -> None:
        """Flush provider recorder, save checkpoint if configured."""
        _ = context, layers

    async def on_session_end(self, context: MemoryContext, layers: MemoryLayerSet) -> None:
        """Role-aware session cleanup.

        - Subagent: clear short-term memory on task completion.
        - Main / Peer: persist by default; delayed cleanup is owned by
          retention and maintenance policies.
        """
        role = _normalize_role(context.agent_role)
        if role == MemoryAgentRole.SUBAGENT:
            try:
                await layers.session.clear(context)
                if layers.pending is not None:
                    await layers.pending.clear(context)
                logger.debug("Cleared subagent session for %s", context.session_id)
            except Exception:
                logger.warning("Failed to clear subagent session", exc_info=True)


# ── Maintenance ─────────────────────────────────────────────────────────────


@dataclass
class MaintenanceResult:
    scope_key: str
    task: str
    success: bool
    detail: str | None = None


class MemoryMaintenancePolicy(ABC):
    """Background maintenance: idle compact, retention, orphan cleanup."""

    @abstractmethod
    async def scan_once(
        self,
        *,
        registry: MemoryStoreRegistry,
        layers: MemoryLayerSet,
    ) -> list[MaintenanceResult]: ...


class DefaultMemoryMaintenancePolicy(MemoryMaintenancePolicy):
    """Default maintenance: idle auto-compact, archive/knowledge retention enforcement."""

    def __init__(
        self,
        idle_threshold_seconds: float = 1800.0,
        keep_recent_messages: int = 8,
        compression_coordinator: Any | None = None,
        archive_retention_policy: ArchiveRetentionPolicy | None = None,
        knowledge_retention_policy: KnowledgeRetentionPolicy | None = None,
    ) -> None:
        self._idle_threshold = idle_threshold_seconds
        self._keep_recent = keep_recent_messages
        self._coordinator = compression_coordinator
        self._archive_retention = archive_retention_policy
        self._knowledge_retention = knowledge_retention_policy

    async def scan_once(
        self,
        *,
        registry: MemoryStoreRegistry,
        layers: MemoryLayerSet,
    ) -> list[MaintenanceResult]:
        import time
        results: list[MaintenanceResult] = []
        has_work = (
            self._coordinator is not None and layers.archive is not None
        ) or self._archive_retention is not None or self._knowledge_retention is not None
        if not has_work:
            return results

        # ── Idle compact (session layer) ──────────────────────────────────────
        if self._coordinator is not None and layers.archive is not None:
            try:
                records = await registry.list_records(layer=MemoryLayerName.SESSION)
            except Exception:
                logger.warning("Maintenance scan failed to list records", exc_info=True)
                records = []

            for record in records:
                ctx = record.context
                if ctx is None:
                    continue
                try:
                    storage = await registry.resolve(
                        layer=MemoryLayerName(record.layer),
                        scope=SessionScope(),
                        context=ctx,
                    )
                    last = await storage.get(".last_activity")
                    if isinstance(last, int | float):
                        if time.time() - last <= self._idle_threshold:
                            continue
                    else:
                        if record.updated_at and time.time() - record.updated_at <= self._idle_threshold:
                            continue

                    await self._coordinator.maybe_compress(
                        session=layers.session,
                        archive=layers.archive,
                        pending=layers.pending,
                        context=ctx,
                    )
                    await storage.set(".last_activity", time.time())
                    results.append(MaintenanceResult(
                        scope_key=record.scope_key, task="idle_compact", success=True,
                    ))
                except Exception as exc:
                    logger.warning("Maintenance failed for %s: %s", record.scope_key, exc)
                    results.append(MaintenanceResult(
                        scope_key=record.scope_key, task="idle_compact", success=False, detail=str(exc),
                    ))

        # ── Archive retention enforcement ─────────────────────────────────────
        if self._archive_retention is not None and layers.archive is not None:
            try:
                archive_records = await registry.list_records(layer=MemoryLayerName.ARCHIVE)
            except Exception:
                logger.warning("Maintenance scan failed to list archive records", exc_info=True)
                archive_records = []

            for record in archive_records:
                ctx = record.context
                if ctx is None:
                    continue
                try:
                    archive_storage = await registry.resolve(
                        layer=MemoryLayerName.ARCHIVE,
                        scope=layers.archive.get_scope(),
                        context=ctx,
                    )
                    entries = await archive_storage.read_logs(since_cursor=0)
                    if not entries:
                        continue

                    max_entries = await self._archive_retention.get_max_entries(ctx)
                    max_age_days = await self._archive_retention.get_max_age_days(ctx)
                    pruned = False

                    if max_entries is not None and len(entries) > max_entries:
                        await archive_storage.save_logs(entries[-max_entries:])
                        entries = entries[-max_entries:]
                        pruned = True

                    if max_age_days is not None:
                        cutoff = time.time() - (max_age_days * 86400)
                        kept = []
                        for entry in entries:
                            created_at = entry.get("created_at")
                            entry_time: float | None = None
                            if isinstance(created_at, str):
                                from datetime import datetime
                                entry_time = datetime.fromisoformat(created_at).timestamp()
                            elif isinstance(created_at, int | float):
                                entry_time = float(created_at)
                            if entry_time is not None and entry_time < cutoff:
                                pruned = True
                                continue
                            kept.append(entry)
                        if pruned and len(kept) != len(entries):
                            await archive_storage.save_logs(kept)

                    if pruned:
                        results.append(MaintenanceResult(
                            scope_key=record.scope_key, task="archive_retention", success=True,
                        ))
                except Exception as exc:
                    logger.warning("Archive retention failed for %s: %s", record.scope_key, exc)
                    results.append(MaintenanceResult(
                        scope_key=record.scope_key, task="archive_retention", success=False, detail=str(exc),
                    ))

        # ── Knowledge eviction ────────────────────────────────────────────────
        if self._knowledge_retention is not None and layers.knowledge is not None:
            try:
                knowledge_records = await registry.list_records(layer=MemoryLayerName.KNOWLEDGE)
            except Exception:
                logger.warning("Maintenance scan failed to list knowledge records", exc_info=True)
                knowledge_records = []

            for record in knowledge_records:
                ctx = record.context
                if ctx is None:
                    continue
                try:
                    knowledge_storage = await registry.resolve(
                        layer=MemoryLayerName.KNOWLEDGE,
                        scope=layers.knowledge.get_scope(),
                        context=ctx,
                    )
                    keys = await knowledge_storage.list_keys()
                    keys = [k for k in keys if not k.endswith("._meta")]
                    if not keys:
                        continue

                    # Build file -> last-update map from changelog
                    changelog = await knowledge_storage.read_logs(since_cursor=0)
                    file_last_update: dict[str, float] = {}
                    for entry in changelog:
                        file_name = entry.get("file")
                        created_at = entry.get("created_at")
                        if not file_name or not created_at:
                            continue
                        knowledge_entry_time: float | None = None
                        if isinstance(created_at, str):
                            from datetime import datetime
                            knowledge_entry_time = datetime.fromisoformat(created_at).timestamp()
                        elif isinstance(created_at, int | float):
                            knowledge_entry_time = float(created_at)
                        if knowledge_entry_time is not None:
                            prev = file_last_update.get(file_name, 0.0)
                            file_last_update[file_name] = max(prev, knowledge_entry_time)

                    pruned = False
                    for key in keys:
                        if self._knowledge_retention.is_permanent_file(key):
                            continue
                        stale_days = self._knowledge_retention.get_stale_threshold_days(key)
                        if stale_days is None:
                            continue
                        last_update = file_last_update.get(key, record.updated_at or 0.0)
                        if time.time() - last_update > stale_days * 86400:
                            await knowledge_storage.delete(key)
                            pruned = True

                    if pruned:
                        results.append(MaintenanceResult(
                            scope_key=record.scope_key, task="knowledge_eviction", success=True,
                        ))
                except Exception as exc:
                    logger.warning("Knowledge eviction failed for %s: %s", record.scope_key, exc)
                    results.append(MaintenanceResult(
                        scope_key=record.scope_key, task="knowledge_eviction", success=False, detail=str(exc),
                    ))

        return results


# ── Retention ───────────────────────────────────────────────────────────────


class SessionRetentionPolicy(ABC):
    """Session layer aging: compression trigger, checkpoint expiry."""

    @abstractmethod
    async def should_compact(
        self, *, storage: Any, context: MemoryContext,
    ) -> bool: ...

    @abstractmethod
    async def should_evict_checkpoint(
        self, *, storage: Any, context: MemoryContext,
    ) -> bool: ...


class DefaultSessionRetentionPolicy(SessionRetentionPolicy):
    async def should_compact(self, *, storage: Any, context: MemoryContext) -> bool:
        _ = storage, context
        return False

    async def should_evict_checkpoint(self, *, storage: Any, context: MemoryContext) -> bool:
        _ = storage, context
        return False


class ArchiveRetentionPolicy(ABC):
    """Archive layer aging: max entries, max age."""

    @abstractmethod
    async def get_max_entries(self, context: MemoryContext) -> int | None: ...

    @abstractmethod
    async def get_max_age_days(self, context: MemoryContext) -> int | None: ...


class DefaultArchiveRetentionPolicy(ArchiveRetentionPolicy):
    def __init__(self, max_entries: int | None = 1000, max_age_days: int | None = None) -> None:
        self._max_entries = max_entries
        self._max_age_days = max_age_days

    async def get_max_entries(self, context: MemoryContext) -> int | None:
        _ = context
        return self._max_entries

    async def get_max_age_days(self, context: MemoryContext) -> int | None:
        _ = context
        return self._max_age_days


class KnowledgeRetentionPolicy(ABC):
    """Knowledge layer aging: which files are permanent, stale thresholds."""

    @abstractmethod
    def is_permanent_file(self, file_key: str) -> bool: ...

    @abstractmethod
    def get_stale_threshold_days(self, file_key: str) -> int | None: ...


class DefaultKnowledgeRetentionPolicy(KnowledgeRetentionPolicy):
    def __init__(
        self,
        stale_days: int = 14,
        default_files: dict[str, str] | None = None,
    ) -> None:
        self._stale_days = stale_days
        self._default_files = default_files or {
            "soul": "SOUL.md",
            "user": "USER.md",
            "memory": "MEMORY.md",
        }
        self._protected_logical = {"soul", "user"}

    def is_permanent_file(self, file_key: str) -> bool:
        # Check logical key
        if file_key in self._protected_logical:
            return True
        # Check storage key mapped from a protected logical key
        for logical, storage in self._default_files.items():
            if logical in self._protected_logical and file_key == storage:
                return True
        return False

    def get_stale_threshold_days(self, file_key: str) -> int | None:
        memory_file = self._default_files.get("memory", "MEMORY.md")
        if file_key in ("memory", memory_file):
            return self._stale_days
        return None
