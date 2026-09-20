#!/bin/bash
set -e
export DISPLAY=:99
Xvfb :99 -screen 0 1920x1080x24 &
sleep 2
x11vnc -display :99 -forever -nopw -rfbport 5900 &
websockify --web /usr/share/novnc/ 6080 localhost:5900 &
sleep 2
echo "[entrypoint] Xvfb/VNC ready, starting bot (chrome will be launched by selenium)..."
exec python -u bot.py
