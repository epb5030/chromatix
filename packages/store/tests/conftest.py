"""Run every store test against SQLite and PostgreSQL.

PostgreSQL tests need an admin URL in ``CHROMATIX_TEST_POSTGRES_URL``, for example
``postgresql://chromatix:chromatix@127.0.0.1:55432/postgres``.  Each test gets a
throwaway database that is dropped afterwards.  Without the URL the PostgreSQL
cases are skipped, unless ``CHROMATIX_REQUIRE_POSTGRES=1`` (set in CI), in which
case they fail.
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, make_url, text

from chromatix_store import Store

PG_URL = os.environ.get("CHROMATIX_TEST_POSTGRES_URL")
REQUIRE_PG = os.environ.get("CHROMATIX_REQUIRE_POSTGRES") == "1"


@pytest.fixture(params=["sqlite", "postgresql"])
def backend(request):
    return request.param


@pytest.fixture
def make_store(backend, tmp_path, request):
    """Factory: ``make_store(username)`` -> a fresh, empty Store on ``backend``."""
    if backend == "postgresql" and not PG_URL:
        if REQUIRE_PG:
            pytest.fail("CHROMATIX_REQUIRE_POSTGRES=1 but CHROMATIX_TEST_POSTGRES_URL is unset")
        pytest.skip("set CHROMATIX_TEST_POSTGRES_URL to run PostgreSQL tests")

    def factory(username: str) -> Store:
        if backend == "sqlite":
            return Store.open_sqlite(tmp_path / "lab", username=username)
        admin_url = make_url(PG_URL).set(drivername="postgresql+psycopg")
        name = f"chromatix_test_{uuid.uuid4().hex[:12]}"
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        store = Store.open_postgres(
            admin_url.set(database=name), tmp_path / "rawdata", username, initialize=True
        )

        def drop() -> None:
            store.engine.dispose()
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            admin.dispose()

        request.addfinalizer(drop)
        return store

    return factory


@pytest.fixture
def store(make_store):
    s = make_store("analyst")
    Store.add_user(s.engine, "analyst", "A. Analyst")
    return s
