FROM --platform=linux/arm64 python:3.10-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV WORKERS=3
ENV PORT=${PORT:-8080}

WORKDIR /app

# Injected by Makefile: requirements.txt (amd64) or requirements.arm64.txt (arm64)
ARG LOCKFILE=requirements.arm64.txt

# ── System deps ────────────────────────────────────────────────────────────────
# No changes needed here — all packages have arm64 variants in Debian repos
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git curl redis-server npm build-essential pkg-config \
       libssl-dev cmake libicu-dev zlib1g-dev \
       libcurl4-openssl-dev ruby-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── github-linguist (gem) ──────────────────────────────────────────────────────
# Unchanged — gem install resolves native extensions for the host arch
RUN gem install github-linguist

# ── ripgrep ────────────────────────────────────────────────────────────────────
# FIXED: The original used an amd64 .deb — that binary will SIGILL on M2.
# Strategy: install Rust, then compile ripgrep from source for arm64.
# (Compiling from source also removes the redundant git-clone block that
#  existed alongside the .deb install in the original.)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --default-toolchain stable --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"

RUN cargo install ripgrep --locked \
    && rg --version

# ── uv (Python package manager) ────────────────────────────────────────────────
# The official install script detects the arch automatically — no change needed.
RUN curl -sSL https://astral.sh/uv/install.sh -o /install.sh \
    && chmod 755 /install.sh \
    && /install.sh \
    && rm /install.sh

# ── Python dependencies ────────────────────────────────────────────────────────
COPY requirements.txt requirements.arm64.txt ./
RUN pip install --no-cache-dir -r ${LOCKFILE}

# Sanity-check: verify aarch64 wheels resolved correctly (fails fast if not)
RUN python - <<'EOF'
import tree_sitter_python, tree_sitter_javascript
from tree_sitter import Language, Parser
lang = Language(tree_sitter_python.language())
p = Parser()
p.language = lang
tree = p.parse(b"x = 1")
assert tree.root_node.type == "module", "tree-sitter self-test failed"
print("tree-sitter aarch64 OK")
EOF

# ── Node / JS tooling ──────────────────────────────────────────────────────────
# FIXED: npm on arm64 Debian ships an old Node. Pin a modern LTS via NodeSource
# so that native addons (e.g. those pulled by eslint plugins) compile correctly.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN npm install -g prettier@2.0.4 typescript eslint@8.57.0 \
    && npm install react react-dom @types/react @types/react-dom \
    && npm install --save-dev \
       @typescript-eslint/parser \
       @typescript-eslint/eslint-plugin \
       eslint-plugin-import \
       eslint-plugin-react

# ── Application code ───────────────────────────────────────────────────────────
COPY sweepai  /app/sweepai
COPY tests    /app/tests
COPY redis.conf /app/redis.conf
COPY bin      /app/bin

ENV PYTHONPATH=.
RUN chmod a+x /app/bin/startup.sh

EXPOSE 8080
CMD ["bash", "-c", "/app/bin/startup.sh"]

LABEL org.opencontainers.image.description="Backend for Sweep, an AI-powered junior developer"
LABEL org.opencontainers.image.source="https://github.com/sweepai/sweep"
