# syntax=docker/dockerfile:1

# ---- uv binary (dependency manager) ----
FROM ghcr.io/astral-sh/uv:0.5 AS uv

# ---- AWS Lambda Web Adapter ----
# Lets a normal HTTP server (uvicorn) run unmodified on Lambda: the adapter
# is a Lambda extension that translates Lambda invoke events <-> HTTP calls
# to the server below. On Docker Desktop / ECS it's simply inert.
FROM public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 AS lambda-adapter

FROM python:3.13-slim

EXPOSE 8005

# Keeps Python from generating .pyc files in the container
ENV PYTHONDONTWRITEBYTECODE=1
# Turns off buffering for easier container logging
ENV PYTHONUNBUFFERED=1
# Use the interpreter already in this image instead of letting uv download
# its own standalone Python into /root/.local (unreadable once we switch to
# a non-root user below -> "Permission denied" on libpython*.so).
ENV UV_PYTHON_PREFERENCE=only-system
ENV UV_SYSTEM_PYTHON=1
ENV UV_LINK_MODE=copy

# Lambda Web Adapter config: where our server listens, and which port
# API Gateway/Lambda traffic gets proxied to.
ENV PORT=8005
ENV AWS_LWA_PORT=8005
ENV AWS_LWA_READINESS_CHECK_PATH=/health

COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=lambda-adapter /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /app

# Install dependencies first (better layer caching)
COPY pyproject.toml uv.lock ./
# psycopg2 (not psycopg2-binary) compiles against libpq at build time.
# If you switch your pyproject.toml to psycopg2-binary instead, this
# apt block becomes unnecessary and can be removed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*
RUN uv sync --frozen --no-dev --no-install-project

COPY . /app

# Creates a non-root user with an explicit UID and adds permission to access the /app folder
RUN adduser -u 5678 --disabled-password --gecos "" appuser && chown -R appuser /app
USER appuser

ENV PATH="/app/.venv/bin:${PATH}"

# Same entrypoint for local Docker Desktop and AWS Lambda (via the adapter).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8005"]