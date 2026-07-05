from __future__ import annotations

from bot.config.domain import get_domain
from bot.service import web_ui_service
from bot.service.config_controller import ConfigController


def test_im_and_model_domains_registered_on_import() -> None:
    assert get_domain("im") is not None
    assert get_domain("model") is not None


def test_controller_uses_trigger_restart_without_invoking_it() -> None:
    # _trigger_restart is referenced (not called) — building the controller must not restart.
    ctrl = ConfigController(restarter=web_ui_service._trigger_restart)
    payload = ctrl.read("im")
    assert payload.domain == "im"
