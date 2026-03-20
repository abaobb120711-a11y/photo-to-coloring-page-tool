FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 只安装 chromium，节省镜像体积
RUN playwright install chromium

COPY . .

CMD gunicorn app:app --timeout 200 --workers 2 --bind 0.0.0.0:${PORT:-5002}
