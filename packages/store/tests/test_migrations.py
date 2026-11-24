"""Schema migrations (Alembic)."""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text

from chromatix_store import Store, StoreVersionError, migrate
from chromatix_store.models import Base


def test_migrations_match_models(store, owner_engine):
    """A store built purely from migrations has exactly the schema the models describe.

    Fails when a model changes without a matching migration.
    """
    with owner_engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diffs == []


def test_new_store_is_at_head(store, owner_engine):
    assert migrate.current_revision(owner_engine) == migrate.head_revision()


def _store_at(engine, revision):
    """Build a store at an older revision by running the migrations up to it."""
    from alembic import command

    with engine.begin() as conn:
        command.upgrade(migrate._config(conn), revision)


def _legacy_sqlite_store(path, *, baseline_layout=True):
    """A store as ChromaTix created it before migrations existed: the 0001 layout,
    no alembic_version table."""
    engine = create_engine(f"sqlite:///{path.with_suffix('.sqlite')}")
    if baseline_layout:
        _store_at(engine, migrate.BASELINE)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE alembic_version"))
            conn.execute(
                text(
                    "INSERT INTO user_account (username, full_name, active, created_at) "
                    "VALUES ('legacy', '', 1, '2026-10-29 10:00:00')"
                )
            )
    else:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE user_account (id INTEGER PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE result_peak (id INTEGER PRIMARY KEY, area FLOAT)"))
    engine.dispose()


def test_unversioned_baseline_store_is_adopted(tmp_path):
    _legacy_sqlite_store(tmp_path / "lab")
    store = Store.open_sqlite(tmp_path / "lab", "legacy")
    assert migrate.current_revision(store.engine) == migrate.head_revision()
    assert [u.username for u in store.users()] == ["legacy"]
    assert store.create_project("After upgrade") > 0
    backups = list(tmp_path.glob("lab.sqlite.bak-unversioned-*"))
    assert len(backups) == 1


def test_upgrade_from_0001_keeps_data_and_protection(tmp_path):
    """A 0001 store with a processed result upgrades to head; history is intact,
    new columns are empty, and result_peak is still append-only (SQLite)."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'lab').with_suffix('.sqlite')}")
    _store_at(engine, "0001")
    with engine.begin() as conn:
        for statement in (
            "INSERT INTO user_account VALUES (1, 'u', '', 1, '2026-10-29')",
            "INSERT INTO project VALUES (1, 'P', '', '2026-10-29', 1)",
            "INSERT INTO sample_set VALUES (1, 1, 'SS', '2026-10-29', 1)",
            "INSERT INTO raw_file VALUES ('" + "a" * 64 + "', 1, 'x.cdf', 'aia', '2026-10-29', 1)",
            "INSERT INTO injection VALUES (1, 1, 1, 'Std', 'unknown', NULL, NULL, '"
            + "a" * 64
            + "', '2026-10-29', 1)",
            "INSERT INTO processing_method VALUES (1, 1, 'Default')",
            "INSERT INTO processing_method_version VALUES (1, 1, 1, '{}', '', '2026-10-29', 1)",
            "INSERT INTO result VALUES (1, 1, 1, 1, '', '2026-10-29', 1)",
            "INSERT INTO result_peak VALUES (1, 1, 1, 2.0, 1.9, 2.1, 100, 12.5, 0.1, 'BB', 2, 2)",
        ):
            conn.execute(text(statement))
    engine.dispose()

    store = Store.open_sqlite(tmp_path / "lab", "u")
    assert migrate.current_revision(store.engine) == migrate.head_revision()
    (peak,) = store.peaks(1)
    assert (peak.area, peak.component, peak.amount, peak.calibration_id) == (12.5, None, None, None)
    with pytest.raises(Exception, match="append-only"), store.engine.begin() as conn:
        conn.execute(text("UPDATE result_peak SET area = 0"))
    assert len(list(tmp_path.glob("lab.sqlite.bak-0001-*"))) == 1


def test_old_layout_is_refused(tmp_path):
    _legacy_sqlite_store(tmp_path / "lab", baseline_layout=False)
    with pytest.raises(StoreVersionError, match="early ChromaTix build"):
        Store.open_sqlite(tmp_path / "lab", "legacy")


def test_up_to_date_store_is_not_backed_up(tmp_path):
    Store.open_sqlite(tmp_path / "lab", "u").engine.dispose()
    Store.open_sqlite(tmp_path / "lab", "u").engine.dispose()
    assert list(tmp_path.glob("lab.sqlite.bak-*")) == []


def test_newer_store_is_refused_sqlite(tmp_path):
    store = Store.open_sqlite(tmp_path / "lab", "u")
    with store.engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'ffff'"))
    store.engine.dispose()
    with pytest.raises(StoreVersionError, match="newer than this ChromaTix"):
        Store.open_sqlite(tmp_path / "lab", "u")


@pytest.fixture
def pg_only(backend):
    if backend != "postgresql":
        pytest.skip("PostgreSQL only")


def test_client_refuses_other_revision(pg_only, make_store, tmp_path):
    store = make_store("analyst")
    owner = make_store.state["owner"]
    with owner.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'ffff'"))
    url = store.engine.url
    with pytest.raises(StoreVersionError, match="expects"):
        Store.open_postgres(url, tmp_path / "raw", store.username)


def test_admin_status(pg_only, make_store, capsys):
    from chromatix_store.admin import main

    make_store("analyst")
    owner_url = make_store.state["owner_url"].render_as_string(hide_password=False)
    assert main(["status", "--url", owner_url]) == 0
    out = capsys.readouterr().out
    assert f"store revision: {migrate.head_revision()}" in out
    assert "up to date" in out


def test_revisions_form_a_single_line():
    """No branches in the history: every store follows the same upgrade path."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(migrate._config())
    assert len(script.get_heads()) == 1
    assert [r.revision for r in script.walk_revisions()][-1] == migrate.BASELINE
