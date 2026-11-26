#!/usr/bin/env bash
set -euo pipefail
release=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
export RENTRADAR_EXPECTED_SHA=${release##*/}
export PYTHONPATH="$release/src"
exec /opt/rentradar/.venv/bin/python -m rentradar "$@"
