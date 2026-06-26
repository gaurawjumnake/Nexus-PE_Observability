# syntax=docker/dockerfile:1

FROM public.ecr.aws/lambda/python:3.13

# Pin uv to a specific version so the layer hash is stable across builds
COPY --from=ghcr.io/astral-sh/uv:0.7.12 /uv /uvx /bin/

# Copy dependency files
COPY pyproject.toml uv.lock ${LAMBDA_TASK_ROOT}/

# Install dependencies — cache mount keeps downloaded wheels across builds
# even when pyproject.toml/uv.lock change, avoiding re-downloads from PyPI
RUN --mount=type=cache,target=/root/.cache/uv \
    cd ${LAMBDA_TASK_ROOT} && uv pip install --system .

# Copy application code
COPY . ${LAMBDA_TASK_ROOT}

# Lambda invokes main.handler via Mangum
CMD ["main.handler"]
