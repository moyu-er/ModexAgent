#!/usr/bin/env bash
# Build the ModexAgent sandbox baseline image (Ticket 09).
# Usage: build.sh [--tag <image:tag>]
set -euo pipefail

tag="modex-sandbox:latest"
if [[ "${1:-}" == "--tag" ]]; then
    if [[ $# -ne 2 ]]; then
        echo "usage: $0 [--tag <image:tag>]" >&2
        exit 2
    fi
    tag="$2"
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

docker build -t "${tag}" "${script_dir}/"
