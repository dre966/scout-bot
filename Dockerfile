FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg2 \
    unzip \
    curl \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    supervisor \
    ca-certificates \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && CHROME_VERSION=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+') \
    && wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}.0/linux64/chromedriver-linux64.zip" -O /tmp/chromedriver.zip \
    && unzip /tmp/chromedriver.zip -d /tmp/ \
    && mv /tmp/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf /tmp/chromedriver* /var/lib/apt/lists/*

RUN mkdir -p /app /app/data /app/logs

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x /usr/local/bin/chromedriver

ENV DISPLAY=:99
ENV CHROME_FLAGS="--remote-debugging-port=9222 --no-sandbox --disable-gpu --window-size=1920,1080"

VOLUME ["/app/data", "/app/logs"]

EXPOSE 9222 5900 6080

ENTRYPOINT ["/bin/bash", "-c", "\
    Xvfb :99 -screen 0 1920x1080x24 & \
    sleep 2 && \
    x11vnc -display :99 -forever -nopw -rfbport 5900 -auth guess & \
    websockify --web /usr/share/novnc/ 6080 localhost:5900 & \
    sleep 1 && \
    google-chrome --remote-debugging-port=9222 --no-sandbox --disable-dev-shm-usage --disable-gpu --window-size=1920,1080 --disable-software-rasterizer --remote-allow-origins=* & \
    sleep 3 && \
    exec python bot.py"]
