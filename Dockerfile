# Multi-stage build. Uses uv for fast, reproducible installs.
# Build:  docker build -t k8s-incident-agent:dev .
FROM python:3.11-slim AS builder

# uv for dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Install kubectl (the tools shell out to it).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && ARCH=$(dpkg --print-architecture) \
    && curl -fsSLo /usr/local/bin/kubectl \
       "https://dl.k8s.io/release/v1.31.0/bin/linux/${ARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN uv pip install --system --no-cache .

# --- runtime ---
FROM python:3.11-slim AS runtime
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/kubectl /usr/local/bin/kubectl

WORKDIR /app
COPY agent/ ./agent/
COPY runbooks/ ./runbooks/

# Pre-download the embedding model into the image (avoids cold-start fetch).
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" || true

ENTRYPOINT ["python", "-m", "agent"]
CMD ["once"]
