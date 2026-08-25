from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

from pydantic import BaseModel, ConfigDict

PUBLIC_KEY_ENV: Final = "LANGFUSE_PUBLIC_KEY"
SECRET_KEY_ENV: Final = "LANGFUSE_SECRET_KEY"
_LANGFUSE_HOST: Final = "LANGFUSE_HOST"


class LangfuseCredentials(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str | None
    public_key: str
    secret_key: str

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> LangfuseCredentials | None:
        """Return credentials, or ``None`` when either key is absent or empty.

        Partial credentials use the same lenient ``None`` result as a wholly
        absent pair.  A supplied mapping is authoritative; only an explicit
        ``None`` falls back to the process environment.
        """
        source = os.environ if environ is None else environ
        public_key = source.get(PUBLIC_KEY_ENV)
        secret_key = source.get(SECRET_KEY_ENV)
        if not public_key or not secret_key:
            return None
        return cls(
            host=source.get(_LANGFUSE_HOST),
            public_key=public_key,
            secret_key=secret_key,
        )


__all__ = [
    "LangfuseCredentials",
    "PUBLIC_KEY_ENV",
    "SECRET_KEY_ENV",
]
