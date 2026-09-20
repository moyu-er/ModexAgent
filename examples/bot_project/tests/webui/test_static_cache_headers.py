"""Static-asset cache headers.

``public/`` files (mascot frames, favicon) keep stable URLs across releases;
without an explicit Cache-Control the browser heuristically caches them from
Last-Modified and stale artwork lingers after an upgrade. The static
middleware must stamp ``Cache-Control: no-cache`` on every ``/webui/``
response (cheap Last-Modified/304 revalidation, always fresh) and leave API
routes untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer

_DATA_DIR = ".modex"


@pytest.mark.asyncio
async def test_static_assets_get_no_cache_header(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "mascot").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    (dist / "mascot" / "f1.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    server = WebUIServer(
        WebSocketInputAdapter(),
        WorkspaceScopedTranscriptStore(data_dir_name=_DATA_DIR),
        static_dist=dist,
    )

    async with TestClient(TestServer(server.app)) as client:
        for path in ("/webui/", "/webui/index.html", "/webui/mascot/f1.png"):
            resp = await client.get(path)
            assert resp.status == 200, path
            assert resp.headers.get("Cache-Control") == "no-cache", path

        api_resp = await client.get("/api/sessions")
        assert api_resp.headers.get("Cache-Control") != "no-cache"
