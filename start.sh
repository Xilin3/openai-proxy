#!/bin/sh
cd "$(dirname "$0")" || exit 1
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python -m bps_proxy "$@"
fi
exec python3 -m bps_proxy "$@"
