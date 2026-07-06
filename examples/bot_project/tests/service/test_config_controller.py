from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from bot.config.domain import DomainFlavor, SecretMask
from bot.config.domains import im as im_module
from bot.service.config_controller import ConfigController, FieldValidationError


def _write_im(path: Path, qq_secret: str = "s") -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "qq": {
                    "enabled": True,
                    "app_id": "A",
                    "secret": qq_secret,
                    "sandbox": False,
                    "allow_from": ["*"],
                },
                "telegram": {
                    "enabled": False,
                    "token": "",
                    "proxy": None,
                    "allow_from": ["*"],
                },
            }
        ),
        encoding="utf-8",
    )


def test_read_returns_masked_registry_payload(tmp_path: Path) -> None:
    _write_im(tmp_path / "im.yml", "supersecret")
    im_module.im_domain.yaml_path = tmp_path / "im.yml"
    payload = ConfigController().read("im")
    assert payload.domain == "im"
    assert payload.flavor is DomainFlavor.REGISTRY
    assert isinstance(payload.sections["qq"].values["secret"], SecretMask)
    assert payload.sections["qq"].values["secret"].has_value is True
    assert "restart_required" in payload.model_dump()


def test_write_persists_and_marks_restart_required(tmp_path: Path) -> None:
    im_module.im_domain.yaml_path = tmp_path / "im.yml"
    _write_im(tmp_path / "im.yml", "old")
    ctrl = ConfigController()
    ctrl.write("im", {"telegram": {"token": {"value": "newtoken"}}})
    data = yaml.safe_load((tmp_path / "im.yml").read_text(encoding="utf-8"))
    assert data["telegram"]["token"] == "newtoken"
    assert ctrl.read("im").restart_required is True


def test_write_validation_error_surfaces_field_errors(tmp_path: Path) -> None:
    im_module.im_domain.yaml_path = tmp_path / "im.yml"
    _write_im(tmp_path / "im.yml")
    ctrl = ConfigController()
    with pytest.raises(FieldValidationError) as ei:
        ctrl.write("im", {"qq": {"sandbox": "not-a-bool"}})
    assert isinstance(ei.value.errors, dict)


def test_restart_invokes_injected_restarter() -> None:
    called = {"n": 0}

    def _r() -> None:
        called["n"] += 1

    ConfigController(restarter=_r).restart()
    assert called["n"] == 1
