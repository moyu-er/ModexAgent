from __future__ import annotations

from collections.abc import Mapping

import pytest
from bot.eval.evalenv import LangfuseCredentials


def test_from_env_returns_credentials_from_parameterized_mapping() -> None:
    environment = {
        "LANGFUSE_HOST": "https://langfuse.example",
        "LANGFUSE_PUBLIC_KEY": "mapping-public",
        "LANGFUSE_SECRET_KEY": "mapping-secret",
    }

    credentials = LangfuseCredentials.from_env(environment)

    assert credentials == LangfuseCredentials(
        host="https://langfuse.example",
        public_key="mapping-public",
        secret_key="mapping-secret",
    )


def test_from_env_returns_none_for_absent_credentials() -> None:
    assert LangfuseCredentials.from_env({}) is None


@pytest.mark.parametrize(
    "environment",
    [
        {"LANGFUSE_PUBLIC_KEY": "public"},
        {"LANGFUSE_SECRET_KEY": "secret"},
        {"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": "secret"},
        {"LANGFUSE_PUBLIC_KEY": "public", "LANGFUSE_SECRET_KEY": ""},
    ],
)
def test_from_env_returns_none_for_incomplete_credentials(
    environment: Mapping[str, str],
) -> None:
    assert LangfuseCredentials.from_env(environment) is None


def test_from_env_defaults_to_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "process-public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "process-secret")
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)

    credentials = LangfuseCredentials.from_env()

    assert credentials == LangfuseCredentials(
        host=None,
        public_key="process-public",
        secret_key="process-secret",
    )
