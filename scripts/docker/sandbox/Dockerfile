# ModexAgent sandbox baseline image (Ticket 09, ADR-0007 OCI family).
# Environment only — the entrypoint/cmd is decided by the runtime layer
# (containers are started with `sleep infinity` + `exec` per command).
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        jq \
        python3 \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Non-root user; uid/gid 1000 matches the typical first host user so
# workspace bind-mount files keep sane ownership.
RUN useradd --create-home --uid 1000 --user-group modex
USER modex

WORKDIR /workspace
