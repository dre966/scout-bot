#!/bin/bash
set -e
echo "[entrypoint] headless mode - starting bot (no Xvfb/VNC)"
exec python -u bot.py
