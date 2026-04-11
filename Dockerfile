FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
        util-linux \
        bash \
        curl \
        ca-certificates \
        dnsutils \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_SYSTEM_PYTHON=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN uv pip install --system --no-cache .

RUN mkdir -p /manifest /state
VOLUME ["/manifest", "/state"]

ENV TERM=xterm-256color
ENTRYPOINT ["wizinstall"]
