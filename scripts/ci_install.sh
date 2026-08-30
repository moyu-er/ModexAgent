#!/usr/bin/env bash
# `uv pip install` with retry against transient PyPI CDN failures.
#
# Rationale: a job can fail with "HTTP status client error (403 Forbidden)"
# while downloading a wheel (e.g. hatchling) even though every sibling job
# in the same workflow run downloads the same artifacts successfully — a
# Fastly/PyPI transient, not a deterministic error. uv exposes no
# user-facing retry control for such status codes, so retry here.
set -u

for attempt in 1 2 3; do
  if uv pip install "$@"; then
    exit 0
  fi
  echo "[ci-install] attempt $attempt failed; retrying in 15s" >&2
  sleep 15
done
echo "[ci-install] all 3 attempts failed" >&2
exit 1
