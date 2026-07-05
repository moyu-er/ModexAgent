from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from bot.config.domain import DomainFlavor, SecretMask, get_domain
from bot.config.domains import im as im_module  # noqa: F401 - import registers the im domain
from pydantic import ValidationError


def test_im_domain_is_registry_with_qq_and_telegram() -> None:
    dom = get_domain("im")
    assert dom is not None
    assert dom.flavor is DomainFlavor.REGISTRY
    assert set(dom.kinds()) >= {"qq", "telegram"}


def _write(path: Path, data: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_im_domain_read_masks_and_write_roundtrip(tmp_path: Path) -> None:
    dom = get_domain("im")
    assert dom is not None
    yml = tmp_path / "im.yml"
    dom.yaml_path = yml
    _write(
        yml,
        {
            "qq": {
                "enabled": True,
                "app_id": "111",
                "secret": "s",
                "sandbox": False,
                "allow_from": ["*"],
            },
            "telegram": {
                "enabled": False,
                "token": "",
                "proxy": None,
                "allow_from": ["*"],
            },
        },
    )

    sections, _restart = dom.read_registry()
    assert isinstance(sections["qq"].values["secret"], SecretMask)
    assert sections["qq"].values["secret"].has_value is True
    assert sections["telegram"].values["token"].has_value is False

    dom.write_registry({"telegram": {"token": {"value": "newtoken"}, "proxy": "http://proxy:8080"}})
    data = yaml.safe_load(yml.read_text(encoding="utf-8"))
    assert data["telegram"]["token"] == "newtoken"
    assert data["telegram"]["proxy"] == "http://proxy:8080"
    # qq untouched by the telegram-only write
    assert data["qq"]["secret"] == "s"


def test_im_domain_unknown_field_raises_without_touching_disk(tmp_path: Path) -> None:
    dom = get_domain("im")
    assert dom is not None
    yml = tmp_path / "im.yml"
    dom.yaml_path = yml
    _write(
        yml,
        {
            "qq": {
                "enabled": False,
                "app_id": "111",
                "secret": "s",
                "sandbox": False,
                "allow_from": ["*"],
            },
        },
    )
    original = yml.read_text(encoding="utf-8")

    with pytest.raises(ValidationError):
        dom.write_registry({"qq": {"bogus_field": "rejected"}})

    assert yml.read_text(encoding="utf-8") == original
