FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IS_SANDBOX=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    UV_CACHE_DIR=/opt/uv-cache \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:/root/.opencode/bin:/root/.local/bin:/usr/local/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    diffutils \
    fd-find \
    findutils \
    git \
    gnupg \
    gzip \
    jq \
    less \
    lsof \
    openssh-client \
    patch \
    procps \
    ripgrep \
    sqlite3 \
    tar \
    unzip \
    wget \
    xz-utils \
    zip \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh \
    && uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python \
        httpx \
        playwright \
        pytest \
        pytest-asyncio \
        pytest-playwright \
        pyyaml \
        requests \
        ruff \
        shiny \
    && /opt/venv/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@latest @openai/codex@latest \
    && npm cache clean --force \
    && curl -fsSL https://antigravity.google/cli/install.sh | bash -s -- --dir /usr/local/bin \
    && ln -sf /usr/local/bin/agy /usr/local/bin/antigravity \
    && mkdir -p /root/.opencode/bin \
    && arch="$(uname -m)" \
    && if [ "$arch" = "aarch64" ] || [ "$arch" = "arm64" ]; then oc_target="linux-arm64"; else oc_target="linux-x64-baseline"; fi \
    && curl -fsSL "https://opencode.ai/update/api/latest/cli/opencode/opencode-${oc_target}.tar.gz" | tar -xz -C /root/.opencode/bin \
    && chmod 755 /root/.opencode/bin/opencode \
    && ln -sf /root/.opencode/bin/opencode /usr/local/bin/opencode \
    && printf 'export PATH="/opt/venv/bin:/root/.opencode/bin:/root/.local/bin:/usr/local/bin:$PATH"\n' > /etc/profile.d/skilldiff-path.sh

WORKDIR /workspace
