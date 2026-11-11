# syntax=docker/dockerfile:1
# Core platform image (no Freqtrade / GPL code): CLI, backtests, paper trader.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-root user: the process never needs more than its own files.
# Note: the optional AI extra (LightGBM) additionally needs the OpenMP runtime:
# build a derived image with `apt-get install -y libgomp1` and QTS_EXTRAS="reporting,ai".
RUN useradd --create-home --uid 1000 qts

WORKDIR /app
COPY pyproject.toml README.md requirements-lock.txt ./
COPY src ./src
ARG QTS_EXTRAS=reporting
# Exact, audited versions: the image is the same whenever it is built.
RUN pip install -c requirements-lock.txt ".[${QTS_EXTRAS}]"

COPY configs ./configs
RUN mkdir -p /app/data /app/state /app/reports && chown -R qts:qts /app
USER qts

ENV QTS_CONFIG_FILE=/app/configs/default.yaml \
    QTS_STATE_DIR=/app/state
ENTRYPOINT ["qts"]
CMD ["--help"]
