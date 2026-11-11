# Freqtrade (GPLv3) image with the platform package installed next to it, so the
# adapter strategy can import qts.strategies / qts.risk. Used for dry-run only.
ARG FREQTRADE_VERSION=2026.10
FROM freqtradeorg/freqtrade:${FREQTRADE_VERSION}

USER root
COPY pyproject.toml README.md /qts/
COPY src /qts/src
RUN pip install --no-cache-dir /qts && rm -rf /qts
COPY --chown=ftuser:ftuser freqtrade/user_data/strategies /freqtrade/user_data/strategies
COPY --chown=ftuser:ftuser configs /freqtrade/configs
RUN mkdir -p /freqtrade/state && chown -R ftuser:ftuser /freqtrade/state /freqtrade/user_data
USER ftuser

ENV QTS_CONFIG_FILE=/freqtrade/configs/paper.yaml \
    QTS_STATE_DIR=/freqtrade/state
