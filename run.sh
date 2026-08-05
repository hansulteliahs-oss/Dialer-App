#!/usr/bin/env bash
# Dev entry point. The packaged app uses packaging/NoBrakes.app instead.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "no .env found - copy .env.example to .env and fill it in" >&2
  exit 1
fi

if [[ ! -x .venv/bin/python3 ]]; then
  echo "no venv - run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

exec .venv/bin/python3 server.py "$@"
