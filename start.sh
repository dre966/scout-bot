#!/bin/bash
set -e
export DISPLAY=:99
Xvfb :99 -screen 0 1920x1080x24 &
sleep 2
x11vnc -display :99 -forever -nopw -rfbport 5900 &
websockify --web /usr/share/novnc/ 6080 localhost:5900 &
sleep 2
mkdir -p /tmp/chrome-debug
google-chrome --remote-debugging-port=9222 --no-sandbox --disable-dev-shm-usage --disable-gpu --window-size=1920,1080 --remote-allow-origins=* --user-data-dir=/tmp/chrome-debug > /tmp/chrome.log 2>&1 &
echo "[entrypoint] waiting for chrome on 9222..."
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
  if curl -sf http://127.0.0.1:9222/json/version; then
    echo "[entrypoint] chrome ready"
    break
  fi
  echo "[entrypoint] chrome not ready $i/30"
  sleep 1
done
cat /tmp/chrome.log
exec python bot.py
