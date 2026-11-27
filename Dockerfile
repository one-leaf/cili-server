FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# git 供 /git 网关（git http-backend）与本地裸仓库初始化使用
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
# /app/data 是外部挂载点：配置、数据库、访问日志和裸仓库都在里面。
# 镜像内先放一份示例配置，便于不挂载直接启动；挂载后以宿主机文件为准。
COPY config.example.json ./data/config/config.json

RUN mkdir -p /app/data/db /app/data/logs /app/data/workspaces

EXPOSE 7681

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7681"]
