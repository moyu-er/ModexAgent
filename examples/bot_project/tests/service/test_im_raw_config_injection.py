from __future__ import annotations

from pathlib import Path

import yaml
from bot.service import web_ui_service


def test_load_im_sections_returns_sections(tmp_path: Path) -> None:
    im_yml = tmp_path / "im.yml"
    im_yml.write_text(
        yaml.safe_dump(
            {
                "qq": {
                    "enabled": True,
                    "app_id": "A",
                    "secret": "S",
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
    merged = web_ui_service.load_im_sections(im_yml)
    assert merged["qq"]["app_id"] == "A"
    assert merged["telegram"]["token"] == ""


def test_load_im_sections_returns_empty_when_missing(tmp_path: Path) -> None:
    assert web_ui_service.load_im_sections(tmp_path / "missing.yml") == {}
