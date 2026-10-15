FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_CACHE_DIR=/opt/uv-cache \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:/root/.local/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    jq \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install uv and create a shared warm virtual environment outside /workspace
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python pytest shiny playwright

WORKDIR /workspace
