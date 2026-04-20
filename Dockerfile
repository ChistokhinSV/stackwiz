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

# Expose helper scripts at a stable host path for engine staging.
RUN install -d /usr/local/share/stackwiz && \
    cp -a /app/src/stackwiz/share/. /usr/local/share/stackwiz/

# Framework-owned KB articles — shipped inside the stackwiz image
# so consumer stacks (currently 077's kb-core) can `docker cp` them
# out and rsync into the central KB at install time. One-way push;
# operators NEVER edit /framework-kb/*.md — edits go in the 079
# repo and propagate on the next stackwiz image bump.
COPY kb/articles/framework /framework-kb

RUN mkdir -p /manifest /state
VOLUME ["/manifest", "/state"]

ENV TERM=xterm-256color
ENTRYPOINT ["wizinstall"]
