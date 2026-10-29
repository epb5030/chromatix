"""Run every store test against SQLite and PostgreSQL.

PostgreSQL tests need an admin URL in ``CHROMATIX_TEST_POSTGRES_URL``, for example
``postgresql://chromatix:chromatix@127.0.0.1:55432/postgres``.  For each test the
fixture creates a database (initialised by the admin, acting as owner), a client
group role, and one personal login role per requested user, then connects as
that user exactly like a real client.  Everything is dropped afterwards.  Without
the URL the PostgreSQL cases are skipped, unless ``CHROMATIX_REQUIRE_POSTGRES=1``
(set in CI), in which case they fail.
"""

import os
import secrets
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
    """Factory: ``make_store(username, create_account=True)`` -> a Store on ``backend``.

    ``make_store.state["owner"]`` is an engine with owner rights on the same
    database (for tests that check triggers rather than privileges).
    """
    if backend == "postgresql" and not PG_URL:
        if REQUIRE_PG:
            pytest.fail("CHROMATIX_REQUIRE_POSTGRES=1 but CHROMATIX_TEST_POSTGRES_URL is unset")
        pytest.skip("set CHROMATIX_TEST_POSTGRES_URL to run PostgreSQL tests")
    state: dict = {"engines": [], "roles": []}

    def factory(username: str, create_account: bool = True) -> Store:
        if backend == "sqlite":
            store = Store.open_sqlite(tmp_path / "lab", username=username)
            if create_account:
                Store.add_user(store.engine, username, username.title())
            state["owner"] = store.engine
            return store
        from chromatix_store.admin import create_group_role, create_user, grant_group_role

        if "db" not in state:
            suffix = uuid.uuid4().hex[:10]
            server = make_url(PG_URL).set(drivername="postgresql+psycopg")
            admin = create_engine(server, isolation_level="AUTOCOMMIT")
            state.update(db=f"chromatix_test_{suffix}", suffix=suffix, admin=admin)
            with admin.connect() as conn:
                conn.execute(text(f'CREATE DATABASE "{state["db"]}"'))
            state["owner_url"] = server.set(database=state["db"])
            owner = create_engine(state["owner_url"])
            state["engines"].append(owner)
            state["owner"] = owner
            state["group"] = f"chromatix_users_{suffix}"
            create_group_role(owner, state["group"])
            state["roles"].append(state["group"])
            Store.initialize(owner)
            grant_group_role(owner, state["group"])
        role, password = f"{username}_{state['suffix']}", secrets.token_hex(12)
        if create_account:
            create_user(state["owner"], role, username.title(), state["group"], password)
        else:
            with state["owner"].begin() as conn:
                conn.execute(
                    text(
                        f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}' "
                        f'IN ROLE "{state["group"]}"'
                    )
                )
        state["roles"].insert(0, role)
        store = Store.open_postgres(
            state["owner_url"].set(username=role, password=password), tmp_path / "rawdata", role
        )
        state["engines"].append(store.engine)
        return store

    def cleanup() -> None:
        for engine in state["engines"]:
            engine.dispose()
        if "db" in state:
            with state["admin"].connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{state["db"]}" WITH (FORCE)'))
                for role in state["roles"]:
                    conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
            state["admin"].dispose()

    request.addfinalizer(cleanup)
    factory.state = state  # type: ignore[attr-defined]
    return factory


@pytest.fixture
def store(make_store):
    return make_store("analyst")


@pytest.fixture
def owner_engine(store, make_store):
    return make_store.state["owner"]
