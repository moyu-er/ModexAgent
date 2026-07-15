"""Tests for bot.webui.model_fetch — URL candidate construction, auth headers,
response parsing, and the 404/405 fallback loop."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))

from bot.webui.model_fetch import (  # noqa: E402
    ModelFetchError,
    _build_headers,
    _ends_with_version_segment,
    _parse_models_response,
    _strip_compat_suffix,
    build_models_url_candidates,
    fetch_provider_models,
)

from modex_agent.core.constants import InterfaceFormat  # noqa: E402

# ── URL candidate construction ──────────────────────────────────────────────


class TestBuildModelsUrlCandidates:
    def test_override_is_exclusive(self) -> None:
        assert build_models_url_candidates(
            "https://api.openai.com", "https://custom.example/models"
        ) == ["https://custom.example/models"]

    def test_empty_base_returns_empty(self) -> None:
        assert build_models_url_candidates("") == []

    def test_plain_base_url_appends_v1_models(self) -> None:
        assert build_models_url_candidates("https://api.deepseek.com") == [
            "https://api.deepseek.com/v1/models"
        ]

    def test_trailing_slash_stripped(self) -> None:
        assert build_models_url_candidates("https://api.deepseek.com/") == [
            "https://api.deepseek.com/v1/models"
        ]

    def test_v1_version_segment(self) -> None:
        assert build_models_url_candidates("https://api.x.com/v1") == [
            "https://api.x.com/v1/models"
        ]

    def test_non_v1_version_segment_adds_fallback(self) -> None:
        result = build_models_url_candidates("https://api.zhipu.com/v4")
        assert "https://api.zhipu.com/v4/models" in result
        assert "https://api.zhipu.com/v4/v1/models" in result

    def test_compat_suffix_primary_preserved_stripped_appended(self) -> None:
        # cc-switch contract: primary candidate preserves full base_url;
        # stripped-root variants are appended as fallbacks, never replacing.
        result = build_models_url_candidates("https://api.deepseek.com/anthropic")
        assert result[0] == "https://api.deepseek.com/anthropic/v1/models"
        assert "https://api.deepseek.com/v1/models" in result
        assert "https://api.deepseek.com/models" in result

    def test_longer_compat_suffix_takes_precedence(self) -> None:
        # /api/anthropic should match before /anthropic
        result = build_models_url_candidates("https://api.x.com/api/anthropic")
        assert result[0] == "https://api.x.com/api/anthropic/v1/models"
        assert "https://api.x.com/v1/models" in result

    def test_dedup_preserves_order(self) -> None:
        result = build_models_url_candidates("https://api.x.com/v1")
        # v1 → [{base}/models], no fallback, no compat suffix
        assert len(result) == 1

    def test_kimi_coding_preserved_as_primary(self) -> None:
        # Kimi mounts Anthropic-compatible API at /coding — the models endpoint
        # is /coding/v1/models. The primary candidate MUST preserve /coding;
        # only the stripped root is a fallback.
        result = build_models_url_candidates("https://api.kimi.com/coding")
        assert result[0] == "https://api.kimi.com/coding/v1/models"
        assert "https://api.kimi.com/v1/models" in result
        assert "https://api.kimi.com/models" in result

    def test_anthropic_native_base_url(self) -> None:
        result = build_models_url_candidates("https://api.anthropic.com")
        assert result == ["https://api.anthropic.com/v1/models"]

    def test_anthropic_with_v1_version_segment(self) -> None:
        result = build_models_url_candidates("https://api.anthropic.com/v1")
        assert result == ["https://api.anthropic.com/v1/models"]

    def test_doubao_api_coding_preserved(self) -> None:
        # Mirrors cc-switch test_candidates_doubao_strip_api_coding
        result = build_models_url_candidates("https://ark.cn-beijing.volces.com/api/coding")
        assert result[0] == "https://ark.cn-beijing.volces.com/api/coding/v1/models"
        assert "https://ark.cn-beijing.volces.com/v1/models" in result
        assert "https://ark.cn-beijing.volces.com/models" in result


class TestEndsWithVersionSegment:
    @pytest.mark.parametrize("url", ["/v1", "/v4", "https://x.com/v2"])
    def test_true_for_version_segments(self, url: str) -> None:
        assert _ends_with_version_segment(url) is True

    @pytest.mark.parametrize("url", ["", "https://x.com", "https://x.com/models", "/va1"])
    def test_false_for_non_version(self, url: str) -> None:
        assert _ends_with_version_segment(url) is False


class TestStripCompatSuffix:
    def test_strips_anthropic(self) -> None:
        assert _strip_compat_suffix("https://api.x.com/anthropic") == "https://api.x.com"

    def test_strips_api_anthropic(self) -> None:
        assert _strip_compat_suffix("https://api.x.com/api/anthropic") == "https://api.x.com"

    def test_returns_none_for_no_match(self) -> None:
        assert _strip_compat_suffix("https://api.openai.com") is None


# ── Auth headers ─────────────────────────────────────────────────────────────


class TestBuildHeaders:
    def test_openai_uses_bearer(self) -> None:
        headers = _build_headers("sk-test", InterfaceFormat.OPENAI_COMPATIBLE)
        assert headers == {"Authorization": "Bearer sk-test"}

    def test_anthropic_uses_x_api_key_and_version(self) -> None:
        headers = _build_headers("sk-ant", InterfaceFormat.ANTHROPIC)
        assert headers == {
            "x-api-key": "sk-ant",
            "anthropic-version": "2023-06-01",
        }


# ── Response parsing ─────────────────────────────────────────────────────────


class TestParseModelsResponse:
    def test_openai_shape(self) -> None:
        data = {"data": [{"id": "gpt-4", "owned_by": "openai"}]}
        models = _parse_models_response(data)
        assert len(models) == 1
        assert models[0].id == "gpt-4"
        assert models[0].owned_by == "openai"

    def test_anthropic_shape_with_display_name(self) -> None:
        data = {"data": [{"id": "claude-sonnet-4", "display_name": "Claude Sonnet 4"}]}
        models = _parse_models_response(data)
        assert models[0].id == "claude-sonnet-4"
        assert models[0].display_name == "Claude Sonnet 4"
        assert models[0].owned_by is None

    def test_sorted_by_id(self) -> None:
        data = {"data": [{"id": "z-model"}, {"id": "a-model"}]}
        models = _parse_models_response(data)
        assert [m.id for m in models] == ["a-model", "z-model"]

    def test_empty_data_raises(self) -> None:
        with pytest.raises(ModelFetchError, match="0 models"):
            _parse_models_response({"data": []})

    def test_missing_data_key_raises(self) -> None:
        with pytest.raises(ModelFetchError, match="missing 'data'"):
            _parse_models_response({})

    def test_non_dict_raises(self) -> None:
        with pytest.raises(ModelFetchError, match="expected JSON object"):
            _parse_models_response([])

    def test_skips_non_dict_items(self) -> None:
        data = {"data": ["not-a-dict", {"id": "ok"}]}
        models = _parse_models_response(data)
        assert len(models) == 1
        assert models[0].id == "ok"


# ── fetch_provider_models integration ────────────────────────────────────────


def _mock_response(status: int, json_data: object | None = None) -> MagicMock:
    mock = MagicMock()
    mock.status = status
    mock.ok = 200 <= status < 300
    mock.json = AsyncMock(return_value=json_data) if json_data is not None else AsyncMock()
    mock.text = AsyncMock(return_value="error body")
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    return mock


class TestFetchProviderModels:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        session = MagicMock()
        session.get = MagicMock(return_value=_mock_response(200, {"data": [{"id": "m1"}]}))
        models = await fetch_provider_models(
            session, "https://api.x.com", "key", InterfaceFormat.OPENAI_COMPATIBLE
        )
        assert len(models) == 1
        assert models[0].id == "m1"

    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self) -> None:
        session = MagicMock()
        with pytest.raises(ModelFetchError, match="API key is required"):
            await fetch_provider_models(
                session, "https://api.x.com", "", InterfaceFormat.OPENAI_COMPATIBLE
            )

    @pytest.mark.asyncio
    async def test_missing_base_url_raises(self) -> None:
        session = MagicMock()
        with pytest.raises(ModelFetchError, match="Base URL is required"):
            await fetch_provider_models(session, "", "key", InterfaceFormat.OPENAI_COMPATIBLE)

    @pytest.mark.asyncio
    async def test_404_falls_back_to_next_candidate(self) -> None:
        session = MagicMock()
        session.get = MagicMock(
            side_effect=[_mock_response(404), _mock_response(200, {"data": [{"id": "m1"}]})]
        )
        models = await fetch_provider_models(
            session, "https://api.x.com/v4", "key", InterfaceFormat.OPENAI_COMPATIBLE
        )
        assert len(models) == 1
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_all_candidates_404_raises(self) -> None:
        session = MagicMock()
        session.get = MagicMock(return_value=_mock_response(404))
        with pytest.raises(ModelFetchError, match="All candidates failed"):
            await fetch_provider_models(
                session, "https://api.x.com/v4", "key", InterfaceFormat.OPENAI_COMPATIBLE
            )

    @pytest.mark.asyncio
    async def test_401_raises_immediately(self) -> None:
        session = MagicMock()
        session.get = MagicMock(return_value=_mock_response(401))
        with pytest.raises(ModelFetchError, match="authentication failed"):
            await fetch_provider_models(
                session, "https://api.x.com/v4", "key", InterfaceFormat.OPENAI_COMPATIBLE
            )

    @pytest.mark.asyncio
    async def test_override_url_used_exclusively(self) -> None:
        session = MagicMock()
        session.get = MagicMock(return_value=_mock_response(200, {"data": [{"id": "m1"}]}))
        await fetch_provider_models(
            session,
            "https://api.x.com",
            "key",
            InterfaceFormat.OPENAI_COMPATIBLE,
            models_url_override="https://custom/models",
        )
        session.get.assert_called_once_with(
            "https://custom/models",
            headers={"Authorization": "Bearer key"},
        )

    @pytest.mark.asyncio
    async def test_anthropic_uses_x_api_key_header(self) -> None:
        session = MagicMock()
        session.get = MagicMock(return_value=_mock_response(200, {"data": [{"id": "claude"}]}))
        await fetch_provider_models(
            session, "https://api.anthropic.com", "sk-ant", InterfaceFormat.ANTHROPIC
        )
        session.get.assert_called_once_with(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": "sk-ant", "anthropic-version": "2023-06-01"},
        )
