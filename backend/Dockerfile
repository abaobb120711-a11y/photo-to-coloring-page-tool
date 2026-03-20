FROM python:3.12-slim

WORKDIR /app

# 安装基础工具（playwright install --with-deps 需要 wget 等）
RUN apt-get update && \
    apt-get install -y --no-install-recommends wget ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --with-deps 会自动安装当前 OS 所需的全部系统库
RUN playwright install --with-deps chromium

COPY . .

CMD gunicorn app:app --timeout 200 --workers 2 --bind 0.0.0.0:${PORT:-5002}

