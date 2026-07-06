"""GET /api/models -- lists (provider_name, model_name, default) choices."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).parents[2]))

from bot.service.model_config import BotModelConfig, ModelCfg, ProviderCfg
from bot.webui import server as srv

_CFG = BotModelConfig(
    default_provider="A",
    default_model="M1",
    providers=[
        ProviderCfg(
            key="a",
            name="A",
            url="u",
            api_key="k",
            models=[ModelCfg(name="M1", model="m1"), ModelCfg(name="M2", model="m2")],
        ),
    ],
)


def _make_server() -> srv.WebUIServer:
    # Bypass __init__ wiring -- the handler only reads the model-config loader.
    inst = srv.WebUIServer.__new__(srv.WebUIServer)
    inst.set_model_config_loader(lambda: _CFG)
    return inst


@pytest.mark.asyncio
async def test_models_endpoint_lists_choices() -> None:
    app = web.Application()
    inst = _make_server()
    app.router.add_get("/api/models", inst._handle_models)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/api/models")
        body = await resp.json()
        assert resp.status == 200
        assert {"provider_name": "A", "model_name": "M1", "default": True} in body["choices"]
        assert {"provider_name": "A", "model_name": "M2", "default": False} in body["choices"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_models_endpoint_empty_when_config_missing() -> None:
    app = web.Application()
    inst = srv.WebUIServer.__new__(srv.WebUIServer)
    inst.set_model_config_loader(lambda: None)
    app.router.add_get("/api/models", inst._handle_models)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/api/models")
        body = await resp.json()
        assert resp.status == 200
        assert body == {"choices": []}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_models_endpoint_live_refreshes_and_leaks_no_secret(tmp_path: Path) -> None:
    """/api/models must reflect the CURRENT model.yml (CLI edits land without restart)
    and must NEVER expose api_key/url — only provider_name + model_name + default."""
    model_yml = tmp_path / "model.yml"
    model_yml.write_text(
        'models:\n  default_provider: "A"\n  default_model: "M1"\n  providers:\n'
        '    - {key: a, name: "A", url: https://secret/v, api_key: SK-SECRET, models: [{name: M1, model: m1}]}\n',
        encoding="utf-8",
    )

    app = web.Application()
    inst = srv.WebUIServer.__new__(srv.WebUIServer)
    # Production wires a loader that re-reads model.yml per request so CLI edits
    # to the model list appear without a server restart.
    inst.set_model_config_loader(lambda: BotModelConfig.from_yaml(model_yml))
    app.router.add_get("/api/models", inst._handle_models)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp1 = await client.get("/api/models")
        body1 = await resp1.json()
        assert resp1.status == 200
        assert {c["model_name"] for c in body1["choices"]} == {"M1"}
        # No secret material may cross the wire.
        assert all(set(c) <= {"provider_name", "model_name", "default"} for c in body1["choices"])

        # User runs `modexbot model` and adds M2 to model.yml on disk.
        model_yml.write_text(
            'models:\n  default_provider: "A"\n  default_model: "M1"\n  providers:\n'
            '    - {key: a, name: "A", url: https://secret/v, api_key: SK-SECRET,\n'
            '       models: [{name: M1, model: m1}, {name: M2, model: m2}]}\n',
            encoding="utf-8",
        )
        resp2 = await client.get("/api/models")
        body2 = await resp2.json()
        # Live refresh — M2 appears without a restart.
        assert {c["model_name"] for c in body2["choices"]} == {"M1", "M2"}
        assert all(set(c) <= {"provider_name", "model_name", "default"} for c in body2["choices"])
    finally:
        await client.close()
