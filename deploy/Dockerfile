# vulnhound app 镜像（Linux 服务器 host 网络模式）
FROM python:3.13-slim

# 沙箱通过 docker CLI 在宿主 docker 里起 vh-agent 容器
RUN apt-get update && apt-get install -y --no-install-recommends docker-cli curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY agent/ agent/
COPY proxy/ proxy/
COPY server/ server/
COPY web/ web/
COPY eval/ eval/
RUN pip install --no-cache-dir -e .

EXPOSE 8900
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8900}"]
