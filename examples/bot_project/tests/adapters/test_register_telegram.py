from __future__ import annotations

from typing import Any

import pytest
from bot.adapters.channels import AdapterBuildContext
from bot.adapters.register_telegram import _telegram_enabled, build_telegram

try:
    import telegram.ext  # noqa: F401
except ModuleNotFoundError:
    _TG_AVAILABLE = False
else:
    _TG_AVAILABLE = True

_tg_required = pytest.mark.skipif(not _TG_AVAILABLE, reason="python-telegram-bot not installed")


def _ctx(raw: dict[str, object]) -> AdapterBuildContext:
    return AdapterBuildContext(
        config_dir=None,  # type: ignore[arg-type]
        project_dir=None,  # type: ignore[arg-type]
        raw_config=raw,
        transcript_store=None,
    )


def test_telegram_disabled_when_token_missing() -> None:
    assert _telegram_enabled(_ctx({})) is False
    assert _telegram_enabled(_ctx({"telegram": {"token": ""}})) is False
    assert _telegram_enabled(_ctx({"telegram": {"enabled": True, "token": "x"}})) is True


def test_build_returns_none_when_disabled() -> None:
    assert build_telegram(_ctx({})) is None


class _FakeApp:
    """Fake PTB Application capturing lifecycle + handler registration."""

    def __init__(self) -> None:
        self.bot: Any = object()
        self.updater: Any = object()
        self.handler_added: bool = False
        self.initialize_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.shutdown_calls = 0

    def add_handler(self, _handler: Any) -> None:  # noqa: ANN401  PTB SDK-boundary fake
        self.handler_added = True

    # The register factory closes over this app and registers lifecycle hooks
    # that call these coroutines. They are NOT awaited during build().
    async def initialize(self) -> None:
        self.initialize_calls += 1

    async def start(self) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class _FakeUpdater:
    def __init__(self) -> None:
        self.polling_started = False
        self.polling_stopped = False

    async def start_polling(
        self,
        *args: Any,
        **kwargs: Any,  # noqa: ANN401  PTB SDK-boundary fake
    ) -> None:
        self.polling_started = True

    async def stop(self) -> None:
        self.polling_stopped = True


class _FakeBuilder:
    def __init__(self, app: _FakeApp) -> None:
        self._app = app

    def token(self, _t: str) -> _FakeBuilder:
        return self

    def request(
        self,
        *args: Any,
        **kwargs: Any,  # noqa: ANN401  PTB SDK-boundary fake
    ) -> _FakeBuilder:
        return self

    def get_updates_request(
        self,
        *args: Any,
        **kwargs: Any,  # noqa: ANN401  PTB SDK-boundary fake
    ) -> _FakeBuilder:
        return self

    def build(self) -> _FakeApp:
        return self._app


@_tg_required
def test_build_returns_triple_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = _FakeApp()
    fake_app.updater = _FakeUpdater()
    import telegram.ext as tg_ext

    monkeypatch.setattr(
        tg_ext,
        "Application",
        type(
            "_FakeApplicationCls",
            (),
            {"builder": staticmethod(lambda: _FakeBuilder(fake_app))},
        ),
    )
    result = build_telegram(
        _ctx(
            {
                "telegram": {
                    "enabled": True,
                    "token": "t",
                    "allow_from": ["*"],
                    "proxy": None,
                }
            }
        )
    )
    assert result is not None
    inp, out, em_factory = result
    assert inp.name == "telegram"
    assert out.name == "telegram"
    # a handler was registered
    assert fake_app.handler_added
    # lifecycle hooks were captured but NOT awaited during build
    assert fake_app.initialize_calls == 0
    # emitter factory yields a constructible emitter
    emitter = em_factory("4242.main", pool="main")
    assert emitter is not None


@_tg_required
def test_build_sets_lifecycle_hooks_on_input(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = _FakeApp()
    fake_app.updater = _FakeUpdater()
    import telegram.ext as tg_ext

    monkeypatch.setattr(
        tg_ext,
        "Application",
        type(
            "_FakeApplicationCls",
            (),
            {"builder": staticmethod(lambda: _FakeBuilder(fake_app))},
        ),
    )
    result = build_telegram(
        _ctx({"telegram": {"enabled": True, "token": "t", "allow_from": ["*"]}})
    )
    assert result is not None
    inp, _out, _em = result
    # hooks injected: start/stop delegate to them, driving the PTB app lifecycle
    assert inp._start_hook is not None  # noqa: SLF001
    assert inp._stop_hook is not None  # noqa: SLF001


def test_build_with_proxy_applies_request_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxy path exercises builder.request / get_updates_request without error."""
    fake_app = _FakeApp()
    fake_app.updater = _FakeUpdater()
    import telegram.ext as tg_ext

    monkeypatch.setattr(
        tg_ext,
        "Application",
        type(
            "_FakeApplicationCls",
            (),
            {"builder": staticmethod(lambda: _FakeBuilder(fake_app))},
        ),
    )
    result = build_telegram(
        _ctx(
            {
                "telegram": {
                    "enabled": True,
                    "token": "t",
                    "allow_from": ["*"],
                    "proxy": "http://localhost:8080",
                }
            }
        )
    )
    assert result is not None
