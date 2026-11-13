#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ -x .venv/bin/python3 ]; then
  exec .venv/bin/python3 -m meetingbot web --open
fi
exec python3 -m meetingbot web --open
