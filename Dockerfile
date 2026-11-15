# API、显式迁移入口与一次性 Worker 入口共用的最小镜像。
#
# 镜像只含运行所需的代码与依赖，不安装开发依赖，也不包含测试、文档、任务包、
# 模型密钥、隐藏评测材料或任何 .env 文件；任务包与工作目录由调用时挂载。
# 一次性 Worker 入口是 `python -m devflow_ai.worker_run`（compose 的 worker 服务），
# 它需要 git 来克隆 Task 的工作区，因此这里装 git；它是执行入口，不是常驻守护进程。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

# Workspace initialization clones the Task's repository, so the worker entry needs git.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

WORKDIR /app

# 先装依赖，改源码不会导致重新解析。
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# 代码与迁移脚本必须一起在镜像里，Alembic 走 alembic.ini 的 %(here)s/migrations。
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
CMD ["uvicorn", "devflow_ai.api:app", "--host", "0.0.0.0", "--port", "8000"]
