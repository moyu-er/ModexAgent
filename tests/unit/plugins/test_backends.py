"""W2a — BackendRegistry + the broker/control-channel registration faces.

Covers the service-level backend registry semantics (the generic
counterpart of ``ChannelAdapterRegistry``), the plugin registration
plumbing through ``ComponentRegistryLoader.load`` (attached registry →
lands; absent registry → dropped with a WARNING naming the plugin), the
bundled ``in-memory`` defaults, and the app-service construction
resolution road ``RunnableAppService`` resolves its broker and control
channel through.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

import modex_agent.plugins.loader as loader_module
from modex_agent.app.config import AppConfig
from modex_agent.app.runnable import RunnableAppService
from modex_agent.app.service import AppService
from modex_agent.control.channel import ControlChannel, InMemoryControlChannel
from modex_agent.messaging.broker import MessageBroker
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.plugins.backends import BackendRegistry, BrokerFactory, ControlChannelFactory
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.backends import (
    IN_MEMORY_BACKEND_NAME,
    register_default_backends,
)
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    Plugin,
    PluginDiscoveryConfig,
    PluginRegistrationContext,
)
from modex_agent.scope.component_registry import ComponentRegistry


class _EmptyConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}


def _broker_factory(seen: list[str]) -> BrokerFactory:
    def factory() -> MessageBroker:
        seen.append("broker")
        return InMemoryMessageBroker()

    return factory


def _channel_factory(seen: list[str]) -> ControlChannelFactory:
    def factory() -> ControlChannel:
        seen.append("channel")
        return InMemoryControlChannel()

    return factory


class _BackendPlugin(Plugin):
    """Registers one named backend per family (the third-party shape)."""

    config_model = _EmptyConfig

    def __init__(self, broker: BrokerFactory, channel: ControlChannelFactory) -> None:
        self._broker = broker
        self._channel = channel

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_broker("stub-broker", self._broker)
        ctx.register_control_channel("stub-channel", self._channel)


# ── BackendRegistry semantics ──────────────────────────────────────────────


class TestBackendRegistry:
    def test_register_resolve_round_trip(self) -> None:
        registry = BackendRegistry[MessageBroker](family="message broker")
        registry.register(IN_MEMORY_BACKEND_NAME, InMemoryMessageBroker)

        backend = registry.resolve(IN_MEMORY_BACKEND_NAME)

        assert isinstance(backend, InMemoryMessageBroker)
        assert isinstance(backend, MessageBroker)

    def test_resolve_constructs_a_fresh_instance_per_call(self) -> None:
        registry = BackendRegistry[ControlChannel](family="control channel")
        registry.register(IN_MEMORY_BACKEND_NAME, InMemoryControlChannel)

        assert registry.resolve(IN_MEMORY_BACKEND_NAME) is not registry.resolve(
            IN_MEMORY_BACKEND_NAME
        )

    def test_names_follow_registration_order(self) -> None:
        registry = BackendRegistry[MessageBroker](family="message broker")
        registry.register("b", InMemoryMessageBroker)
        registry.register("a", InMemoryMessageBroker)

        assert registry.names() == ("b", "a")

    def test_same_name_same_factory_is_idempotent(self) -> None:
        registry = BackendRegistry[MessageBroker](family="message broker")
        registry.register(IN_MEMORY_BACKEND_NAME, InMemoryMessageBroker)
        registry.register(IN_MEMORY_BACKEND_NAME, InMemoryMessageBroker)

        assert registry.names() == (IN_MEMORY_BACKEND_NAME,)

    def test_same_name_different_factory_is_loud(self) -> None:
        registry = BackendRegistry[MessageBroker](family="message broker")
        registry.register(IN_MEMORY_BACKEND_NAME, InMemoryMessageBroker)

        def other() -> MessageBroker:
            return InMemoryMessageBroker()

        try:
            registry.register(IN_MEMORY_BACKEND_NAME, other)
        except ValueError as e:
            assert IN_MEMORY_BACKEND_NAME in str(e)
            assert "message broker" in str(e)
        else:
            raise AssertionError("different factory under the same name must raise")

    def test_unknown_name_error_lists_registered_options(self) -> None:
        registry = BackendRegistry[ControlChannel](family="control channel")
        registry.register(IN_MEMORY_BACKEND_NAME, InMemoryControlChannel)

        try:
            registry.resolve("redis")
        except ValueError as e:
            assert "redis" in str(e)
            assert IN_MEMORY_BACKEND_NAME in str(e)
        else:
            raise AssertionError("unknown backend name must raise")


# ── Registration plumbing through the loader ───────────────────────────────


async def test_plugin_backend_registrations_land_in_attached_registries(
    tmp_path: Path,
) -> None:
    seen: list[str] = []
    brokers = BackendRegistry[MessageBroker](family="message broker")
    control_channels = BackendRegistry[ControlChannel](family="control channel")
    registry = ComponentRegistry()

    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(_BackendPlugin(_broker_factory(seen), _channel_factory(seen)),),
            project_plugin_paths=(),
            user_plugin_path=tmp_path / "absent",
            brokers=brokers,
            control_channels=control_channels,
        ),
    )

    assert brokers.names() == ("stub-broker",)
    assert control_channels.names() == ("stub-channel",)
    assert isinstance(brokers.resolve("stub-broker"), InMemoryMessageBroker)
    assert isinstance(control_channels.resolve("stub-channel"), InMemoryControlChannel)


async def test_plugin_backend_registrations_dropped_with_warning_without_registry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[str] = []

    with caplog.at_level(logging.WARNING, logger="modex_agent.plugins.loader"):
        await ComponentRegistryLoader.load(
            ComponentRegistry(),
            PluginDiscoveryConfig(
                bundled_factories=(
                    _BackendPlugin(_broker_factory(seen), _channel_factory(seen)),
                ),
                project_plugin_paths=(),
                user_plugin_path=tmp_path / "absent",
            ),
        )

    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("stub-broker" in m and "BackendRegistry" in m for m in warnings), warnings
    assert any("stub-channel" in m and "BackendRegistry" in m for m in warnings), warnings


async def test_bundled_defaults_register_in_memory_for_both_families(
    tmp_path: Path,
) -> None:
    brokers = BackendRegistry[MessageBroker](family="message broker")
    control_channels = BackendRegistry[ControlChannel](family="control channel")

    await ComponentRegistryLoader.load(
        ComponentRegistry(),
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(),
            user_plugin_path=tmp_path / "absent",
            brokers=brokers,
            control_channels=control_channels,
        ),
    )

    assert brokers.names() == (IN_MEMORY_BACKEND_NAME,)
    assert control_channels.names() == (IN_MEMORY_BACKEND_NAME,)
    assert isinstance(brokers.resolve(IN_MEMORY_BACKEND_NAME), InMemoryMessageBroker)
    assert isinstance(
        control_channels.resolve(IN_MEMORY_BACKEND_NAME), InMemoryControlChannel
    )


def test_register_default_backends_buffers_both_families() -> None:
    ctx = PluginRegistrationContext(registry=None)

    register_default_backends(ctx)

    assert ctx.pending_brokers() == (IN_MEMORY_BACKEND_NAME,)
    assert ctx.pending_control_channels() == (IN_MEMORY_BACKEND_NAME,)


# ── App-service registry load + construction resolution ────────────────────


class _SkeletonService(AppService):
    """The abstract lifecycle steps are irrelevant to the registry block."""

    async def initialize(self) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


def _skeleton(tmp_path: Path) -> _SkeletonService:
    return _SkeletonService(
        config_dir=tmp_path / "config",
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        emitter_factory=MagicMock(),
        resource_root=tmp_path,
    )


async def test_app_service_registry_load_populates_default_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loader_module, "DEFAULT_USER_PLUGIN_DIR", tmp_path / "absent")
    service = _skeleton(tmp_path)

    await service._load_component_registry()  # noqa: SLF001 — test seam

    assert service._broker_registry.names() == (IN_MEMORY_BACKEND_NAME,)  # noqa: SLF001
    assert service._control_channel_registry.names() == (IN_MEMORY_BACKEND_NAME,)  # noqa: SLF001
    assert isinstance(
        service._broker_registry.resolve(AppConfig().broker_backend),  # noqa: SLF001
        InMemoryMessageBroker,
    )
    assert isinstance(
        service._control_channel_registry.resolve(  # noqa: SLF001
            AppConfig().control_channel_backend
        ),
        InMemoryControlChannel,
    )


async def test_runnable_resolves_configured_backend_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The construction-resolution road: the service's own registries (the
    ones ``initialize`` resolves through) carry the plugin-registered
    names, and resolving the configured knobs yields those backends."""
    monkeypatch.setattr(loader_module, "DEFAULT_USER_PLUGIN_DIR", tmp_path / "absent")
    seen: list[str] = []
    app_config = AppConfig(
        broker_backend="stub-broker", control_channel_backend="stub-channel"
    )

    service = RunnableAppService(
        config_dir=tmp_path / "config",
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        app_config=app_config,
        resource_root=tmp_path,
    )
    await ComponentRegistryLoader.load(
        ComponentRegistry(),
        PluginDiscoveryConfig(
            bundled_factories=(
                _BackendPlugin(_broker_factory(seen), _channel_factory(seen)),
            ),
            project_plugin_paths=(),
            user_plugin_path=tmp_path / "absent",
            brokers=service._broker_registry,  # noqa: SLF001 — the registry initialize resolves through
            control_channels=service._control_channel_registry,  # noqa: SLF001
        ),
    )

    # The resolution expressions initialize() runs:
    broker = service._broker_registry.resolve(app_config.broker_backend)  # noqa: SLF001
    channel = service._control_channel_registry.resolve(  # noqa: SLF001
        app_config.control_channel_backend
    )

    assert isinstance(broker, InMemoryMessageBroker)
    assert isinstance(channel, InMemoryControlChannel)
    assert seen == ["broker", "channel"]


def test_app_config_backend_knobs_default_to_in_memory() -> None:
    config = AppConfig()

    assert config.broker_backend == IN_MEMORY_BACKEND_NAME
    assert config.control_channel_backend == IN_MEMORY_BACKEND_NAME
