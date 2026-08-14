"""Integration test configuration for E2E graph workflow tests.

Loads LLM provider credentials from a local .env file (gitignored).
Tests are skipped when the .env is absent or incomplete.

The .env file is NOT the bot_project's production config — it is a
test-specific credential source. See .env.example for the template.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from dotenv import load_dotenv

_INTEGRATION_DIR = Path(__file__).parent
_ENV_FILE = _INTEGRATION_DIR / ".env"


@dataclass(frozen=True)
class E2EModelConfig:
    provider_key: str
    provider_name: str
    base_url: str
    api_key: str
    model: str
    reasoning_effort: str
    temperature: float
    max_output_tokens: int


def _load_e2e_config() -> E2EModelConfig | None:
    if not _ENV_FILE.exists():
        return None
    load_dotenv(_ENV_FILE)
    api_key = os.environ.get("TEST_LLM_API_KEY", "")
    if not api_key or api_key == "your-api-key-here":
        return None
    return E2EModelConfig(
        provider_key=os.environ.get("TEST_LLM_PROVIDER_KEY", ""),
        provider_name=os.environ.get("TEST_LLM_PROVIDER_NAME", ""),
        base_url=os.environ.get("TEST_LLM_BASE_URL", ""),
        api_key=api_key,
        model=os.environ.get("TEST_LLM_MODEL", ""),
        reasoning_effort=os.environ.get("TEST_LLM_REASONING_EFFORT", "medium"),
        temperature=float(os.environ.get("TEST_LLM_TEMPERATURE", "0.7")),
        max_output_tokens=int(os.environ.get("TEST_LLM_MAX_OUTPUT_TOKENS", "2000")),
    )


@pytest.fixture(scope="session")
def e2e_model_config() -> E2EModelConfig:
    cfg = _load_e2e_config()
    if cfg is None:
        pytest.skip("TEST_LLM_* secrets not configured")
    return cfg
