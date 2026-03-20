FROM python:3.12-slim

# Playwright Chromium 需要的系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates fonts-liberation \
        libnss3 libatk-bridge2.0-0 libdrm2 libxcomposite1 \
        libxdamage1 libxrandr2 libgbm1 libasound2 \
        libpango-1.0-0 libcairo2 libatspi2.0-0 libxshmfence1 \
        libx11-xcb1 libxcb-dri3-0 libcups2 libxkbcommon0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Chromium 浏览器二进制（带系统依赖补全）
RUN playwright install --with-deps chromium

COPY . .

CMD gunicorn app:app --timeout 200 --workers 2 --bind 0.0.0.0:${PORT:-5002}
