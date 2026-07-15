# bot/webui/model_fetch.py
"""Fetch available models from a provider's model-list endpoint.

Proxies the fetch server-side (the api_key is masked in the frontend) and
constructs candidate URLs from ``base_url`` — preserving the full base URL as
the primary candidate, then appending compat-suffix-stripped fallbacks.

URL strategy (ported from the cc-switch reference project, model_fetch.rs):
  1. ``models_url`` override non-empty → only that URL (escape hatch).
  2. ``base_url`` ends with ``/v{N}`` → ``{base}/models`` primary (plus
     ``{base}/v1/models`` fallback when N ≠ 1).
  3. Otherwise → ``{base}/v1/models`` primary.
  4. If ``base_url`` ends with a known compat suffix, ALSO append
     ``{root}/v1/models`` and ``{root}/models`` as fallbacks (never replace
     the primary). This lets e.g. Kimi's ``/coding`` path be tried first,
     with the stripped root as a 404-fallback.

Auth branches by ``interface_format``:
  - ``anthropic`` → ``x-api-key`` + ``anthropic-version`` headers.
  - ``openai_compatible`` → ``Authorization: Bearer`` header.

The URL construction is identical for both formats — the ``/v1/models``
endpoint works regardless of whether the provider's chat path speaks
Anthropic or OpenAI protocol.
"""

from __future__ import annotations

import logging

import aiohttp
from pydantic import BaseModel, ConfigDict

from modex_agent.core.constants import InterfaceFormat

logger = logging.getLogger(__name__)

# Known Anthropic-protocol compat suffixes, longest-first so /api/anthropic
# does not shadow /anthropic. Ported verbatim from cc-switch KNOWN_COMPAT_SUFFIXES.
_COMPAT_SUFFIXES: tuple[str, ...] = (
    "/api/claudecode",
    "/api/anthropic",
    "/apps/anthropic",
    "/api/coding",
    "/claudecode",
    "/anthropic",
    "/step_plan",
    "/coding",
    "/claude",
)

_MAX_ERROR_BODY_CHARS: int = 512


class FetchedModel(BaseModel):
    """A model id returned by a provider's model-list endpoint.

    ``owned_by`` comes from OpenAI's response shape; ``display_name`` from
    Anthropic's. At least ``id`` is always present.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    owned_by: str | None = None
    display_name: str | None = None


class ModelFetchError(Exception):
    """Raised when fetching models from a provider fails.

    ``reason`` is a short human-readable string the REST handler maps to a
    toast; ``status`` is the upstream HTTP status when available.
    """

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _ends_with_version_segment(base_url: str) -> bool:
    """Return True if *base_url* ends with a ``/v{N}`` segment."""
    parts = base_url.rstrip("/").rsplit("/", 1)
    if len(parts) < 2:
        return False
    seg = parts[1]
    return len(seg) >= 2 and seg[0] == "v" and seg[1:].isdigit()


def _strip_compat_suffix(base_url: str) -> str | None:
    """Return the root URL with a known compat suffix stripped, or None."""
    for suffix in _COMPAT_SUFFIXES:
        if base_url.endswith(suffix):
            return base_url[: -len(suffix)].rstrip("/")
    return None


def build_models_url_candidates(
    base_url: str,
    models_url_override: str | None = None,
) -> list[str]:
    """Build an ordered, deduped list of candidate model-list URLs.

    The primary candidate is always derived from the full ``base_url``
    (preserving sub-paths like ``/coding``). Compat-suffix-stripped variants
    are appended as fallbacks, never replacing the primary.

    Returns an empty list when *base_url* is empty and no override is given.
    """
    if models_url_override:
        return [models_url_override]

    base = base_url.rstrip("/")
    if not base:
        return []

    candidates: list[str] = []

    if _ends_with_version_segment(base):
        candidates.append(f"{base}/models")
        parts = base.rsplit("/", 1)
        if parts[1] != "v1":
            candidates.append(f"{base}/v1/models")
    else:
        candidates.append(f"{base}/v1/models")

    root = _strip_compat_suffix(base)
    if root is not None and root and "://" in root:
        candidates.append(f"{root}/v1/models")
        candidates.append(f"{root}/models")

    seen: set[str] = set()
    unique: list[str] = []
    for url in candidates:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _build_headers(
    api_key: str,
    interface_format: InterfaceFormat,
) -> dict[str, str]:
    """Build auth headers based on the provider's interface format."""
    if interface_format == InterfaceFormat.ANTHROPIC:
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    return {"Authorization": f"Bearer {api_key}"}


def _parse_models_response(data: object) -> list[FetchedModel]:
    """Parse an OpenAI/Anthropic-shaped ``{data: [{id, ...}]}`` response."""
    if not isinstance(data, dict):
        raise ModelFetchError("Failed to parse response: expected JSON object")
    items = data.get("data")
    if not isinstance(items, list):
        raise ModelFetchError("Failed to parse response: missing 'data' array")
    models: list[FetchedModel] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        owned_by = item.get("owned_by")
        display_name = item.get("display_name")
        models.append(
            FetchedModel(
                id=model_id,
                owned_by=owned_by if isinstance(owned_by, str) else None,
                display_name=display_name if isinstance(display_name, str) else None,
            )
        )
    if not models:
        raise ModelFetchError("Provider returned 0 models")
    models.sort(key=lambda m: m.id)
    return models


async def fetch_provider_models(
    session: aiohttp.ClientSession,
    base_url: str,
    api_key: str,
    interface_format: InterfaceFormat,
    models_url_override: str | None = None,
) -> list[FetchedModel]:
    """Fetch the list of available models from a provider.

    Tries candidate URLs in order; 404/405 → next candidate, other non-2xx →
    fail immediately. Returns models sorted by id.

    Raises :class:`ModelFetchError` on any failure (missing key, auth error,
    all candidates exhausted, parse failure, timeout).
    """
    if not api_key:
        raise ModelFetchError("API key is required")
    if not base_url and not models_url_override:
        raise ModelFetchError("Base URL is required")

    candidates = build_models_url_candidates(base_url, models_url_override)
    if not candidates:
        raise ModelFetchError("Base URL is required")

    headers = _build_headers(api_key, interface_format)
    errors: list[str] = []

    for url in candidates:
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status in (404, 405):
                    errors.append(f"{url}: HTTP {resp.status}")
                    continue
                if not resp.ok:
                    if resp.status in (401, 403):
                        raise ModelFetchError(
                            f"HTTP {resp.status}: authentication failed",
                            status=resp.status,
                        )
                    body = await resp.text()
                    body = body[:_MAX_ERROR_BODY_CHARS]
                    raise ModelFetchError(f"HTTP {resp.status}: {body}", status=resp.status)
                data = await resp.json()
                return _parse_models_response(data)
        except ModelFetchError:
            raise
        except aiohttp.ClientError as exc:
            errors.append(f"{url}: {exc}")
        except TimeoutError as exc:
            raise ModelFetchError(f"Request timed out: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - unexpected JSON/network errors
            errors.append(f"{url}: {exc}")

    raise ModelFetchError(
        f"All candidates failed: {'; '.join(errors)}" if errors else "All candidates failed"
    )
