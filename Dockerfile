# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY f1tv_dl ./f1tv_dl
# Optional CA bundle for builds behind a trusted corporate or session proxy.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
        PIP_CERT=/run/secrets/proxy_ca pip install --no-cache-dir .; \
    else \
        pip install --no-cache-dir .; \
    fi

RUN useradd --uid 10001 --create-home f1tv \
    && mkdir -p /download /cache \
    && chown f1tv:f1tv /download /cache
ENV XDG_CACHE_HOME=/cache
USER f1tv
WORKDIR /download
ENTRYPOINT ["f1tv-dl"]
