"""Tests for /api/config/{domain} and /api/system/restart endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.config.domains import im as im_module
from bot.service.config_controller import ConfigController
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer

_REAL_IM_PATH = Path(__file__).resolve().parents[2] / "config" / "im.yml"


def _make_client(controller: ConfigController, tmp_path: Path) -> TestClient:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        WebSocketInputAdapter(),
        store,
        static_dist=None,
        home_sessions_dir=tmp_path / ".modex",
    )
    server.set_config_controller(controller)
    return TestClient(TestServer(server.app))


@pytest.mark.asyncio
async def test_get_config_returns_masked_payload(tmp_path: Path) -> None:
    im_path = tmp_path / "im.yml"
    im_path.write_text(
        yaml.safe_dump(
            {
                "qq": {
                    "enabled": True,
                    "app_id": "A",
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
            }
        ),
        encoding="utf-8",
    )
    im_module.im_domain.yaml_path = im_path
    try:
        client = _make_client(ConfigController(), tmp_path)
        await client.start_server()
        try:
            resp = await client.get("/api/config/im")
            assert resp.status == 200
            data = await resp.json()
            assert data["flavor"] == "registry"
            assert data["sections"]["qq"]["values"]["secret"]["has_value"] is True
        finally:
            await client.close()
    finally:
        im_module.im_domain.yaml_path = _REAL_IM_PATH


@pytest.mark.asyncio
async def test_put_config_persists_and_returns_restart_required(
    tmp_path: Path,
) -> None:
    im_path = tmp_path / "im.yml"
    im_path.write_text(
        yaml.safe_dump(
            {"telegram": {"enabled": False, "token": "", "proxy": None, "allow_from": ["*"]}}
        ),
        encoding="utf-8",
    )
    im_module.im_domain.yaml_path = im_path
    try:
        client = _make_client(ConfigController(), tmp_path)
        await client.start_server()
        try:
            resp = await client.put(
                "/api/config/im", json={"telegram": {"token": {"value": "tok"}}}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["restart_required"] is True
            assert data["sections"]["telegram"]["values"]["token"]["has_value"] is True
        finally:
            await client.close()
    finally:
        im_module.im_domain.yaml_path = _REAL_IM_PATH


@pytest.mark.asyncio
async def test_get_config_unknown_domain_returns_404(tmp_path: Path) -> None:
    client = _make_client(ConfigController(), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/config/does-not-exist")
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_config_validation_error_returns_400(tmp_path: Path) -> None:
    im_path = tmp_path / "im.yml"
    im_path.write_text(
        yaml.safe_dump(
            {
                "qq": {
                    "enabled": True,
                    "app_id": "A",
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
            }
        ),
        encoding="utf-8",
    )
    im_module.im_domain.yaml_path = im_path
    try:
        client = _make_client(ConfigController(), tmp_path)
        await client.start_server()
        try:
            resp = await client.put("/api/config/im", json={"qq": {"sandbox": "not-a-bool"}})
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "validation"
            assert "fields" in data
        finally:
            await client.close()
    finally:
        im_module.im_domain.yaml_path = _REAL_IM_PATH


@pytest.mark.asyncio
async def test_post_restart_invokes_controller(tmp_path: Path) -> None:
    called = {"n": 0}

    def _r() -> None:
        called["n"] += 1

    client = _make_client(ConfigController(restarter=_r), tmp_path)
    await client.start_server()
    try:
        resp = await client.post("/api/system/restart")
        assert resp.status == 200
        assert called["n"] == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_restart_unavailable_returns_hint(tmp_path: Path) -> None:
    # ConfigController with no restarter -> restart() raises RuntimeError.
    client = _make_client(ConfigController(), tmp_path)
    await client.start_server()
    try:
        resp = await client.post("/api/system/restart")
        assert resp.status == 200
        data = await resp.json()
        assert "error" in data
        assert "hint" in data
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_endpoints_return_503_when_controller_not_set(tmp_path: Path) -> None:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        WebSocketInputAdapter(),
        store,
        static_dist=None,
        home_sessions_dir=tmp_path / ".modex",
    )
    # NOTE: no set_config_controller call.
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        assert (await client.get("/api/config/im")).status == 503
        assert (await client.put("/api/config/im", json={})).status == 503
        assert (await client.post("/api/system/restart")).status == 503
    finally:
        await client.close()
