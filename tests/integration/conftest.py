from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest

from qts.core.config import AppConfig
from qts.integrations.db.storage import Storage
from qts.market_data.store import ParquetStore
from tests.conftest import random_walk

SYMBOL = "TEST/USDT"


@pytest.fixture(params=["sqlite", "postgres"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Storage]:
    """Every persistence test runs on SQLite and, when available, on PostgreSQL."""
    if request.param == "sqlite":
        store = Storage(f"sqlite:///{tmp_path / 'qts.sqlite'}")
        store.init_schema()
        yield store
        return
    url = os.environ.get("QTS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("QTS_TEST_DATABASE_URL not set")
    store = Storage(url)
    _wipe(store)
    store.init_schema()
    yield store
    _wipe(store)
    store.engine.dispose()


def _wipe(store: Storage) -> None:
    from sqlalchemy import text

    from qts.integrations.db.models import Base

    Base.metadata.drop_all(store.engine)
    with store.engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture
def paper_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "mode": "paper",
            "market": {"symbols": [SYMBOL]},
            "data": {"directory": str(tmp_path / "data"), "start": "2018-01-01"},
            "risk": {"allowed_symbols": [SYMBOL]},
        }
    )


@pytest.fixture
def candles() -> pd.DataFrame:
    return random_walk(1200, seed=7)


@pytest.fixture
def parquet(paper_config: AppConfig) -> ParquetStore:
    return ParquetStore(paper_config.data.directory)
