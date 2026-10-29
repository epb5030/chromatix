"""PostgreSQL owner/application role separation (ADR 0003)."""

import os
import secrets
import uuid

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import DBAPIError

from chromatix_core import IntegrationParams
from chromatix_core.io import write_aia
from chromatix_core.synthetic import GaussianPeak, gaussian_chromatogram
from chromatix_store import ProtectionMissingError, Store

PG_URL = os.environ.get("CHROMATIX_TEST_POSTGRES_URL")
REQUIRE_PG = os.environ.get("CHROMATIX_REQUIRE_POSTGRES") == "1"

pytestmark = pytest.mark.skipif(
    not PG_URL and not REQUIRE_PG, reason="set CHROMATIX_TEST_POSTGRES_URL"
)


@pytest.fixture
def pg(request):
    """A fresh database and an unused role name; both are dropped afterwards."""
    if not PG_URL:
        pytest.fail("CHROMATIX_REQUIRE_POSTGRES=1 but CHROMATIX_TEST_POSTGRES_URL is unset")
    suffix = uuid.uuid4().hex[:12]
    db, role, password = (
        f"chromatix_test_{suffix}",
        f"chromatix_app_{suffix}",
        secrets.token_hex(16),
    )
    server = make_url(PG_URL).set(drivername="postgresql+psycopg")
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db}"'))
    owner_url = server.set(database=db)
    app_url = owner_url.set(username=role, password=password)
    engines = []

    def engine(url):
        e = create_engine(url)
        engines.append(e)
        return e

    def cleanup():
        for e in engines:
            e.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
            conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        admin.dispose()

    request.addfinalizer(cleanup)
    return {
        "owner_url": owner_url,
        "app_url": app_url,
        "role": role,
        "password": password,
        "engine": engine,
    }


def _provision(pg):
    from chromatix_store.admin import create_app_role, grant_app_role

    owner = pg["engine"](pg["owner_url"])
    create_app_role(owner, pg["role"], pg["password"])
    Store.initialize(owner)
    grant_app_role(owner, pg["role"])
    Store.add_user(owner, "analyst", "A. Analyst")
    return owner


def test_app_role_runs_the_workflow(pg, tmp_path):
    _provision(pg)
    store = Store.open_postgres(pg["app_url"], tmp_path / "raw", "analyst")
    path = tmp_path / "inj.cdf"
    write_aia(gaussian_chromatogram([GaussianPeak(2.0, 100, 0.05)], noise=0.3), path)
    project = store.create_project("P")
    sample_set = store.create_sample_set(project, "SS")
    injection = store.add_injection(sample_set, "Std", store.import_raw_file(path))
    method = store.save_method_version(project, "M", IntegrationParams())
    result = store.process_injection(injection, method)
    assert len(store.peaks(result)) == 1
    assert store.audit_trail()[-1].action == "result.create"
    store.engine.dispose()


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("UPDATE audit_event SET reason = 'x'", "permission denied"),
        ("DELETE FROM result_peak", "permission denied"),
        ("TRUNCATE audit_event", "permission denied"),
        ("UPDATE project SET name = 'x'", "permission denied"),
        (
            "INSERT INTO user_account (username, full_name, active, created_at) "
            "VALUES ('mallory', '', true, now())",
            "permission denied",
        ),
        ("DROP TRIGGER audit_event_append_only ON audit_event", "must be owner"),
        ("ALTER TABLE audit_event DISABLE TRIGGER ALL", "must be owner"),
        ("DROP TABLE result_peak", "must be owner"),
        ("CREATE TABLE sneaky (id int)", "permission denied"),
    ],
)
def test_app_role_cannot_tamper(pg, statement, message):
    _provision(pg)
    app = pg["engine"](pg["app_url"])
    with pytest.raises(DBAPIError, match=message), app.begin() as conn:
        conn.execute(text(statement))


def test_refuses_uninitialised_database(pg, tmp_path):
    with pytest.raises(ProtectionMissingError, match="not initialised"):
        Store.open_postgres(pg["owner_url"], tmp_path / "raw", "analyst")


def test_refuses_disabled_trigger(pg, tmp_path):
    owner = _provision(pg)
    with owner.begin() as conn:
        conn.execute(text("ALTER TABLE result DISABLE TRIGGER result_append_only"))
    with pytest.raises(ProtectionMissingError, match="result_append_only"):
        Store.open_postgres(pg["app_url"], tmp_path / "raw", "analyst")


def test_admin_cli(pg, tmp_path, monkeypatch, capsys):
    from chromatix_store.admin import main

    owner = pg["owner_url"].render_as_string(hide_password=False)
    monkeypatch.setenv("CHROMATIX_APP_PASSWORD", pg["password"])
    assert main(["init-postgres", "--url", owner, "--app-role", pg["role"], "--create-role"]) == 0
    assert main(["add-user", "--url", owner, "--username", "jdoe", "--full-name", "J. Doe"]) == 0
    assert pg["password"] not in capsys.readouterr().out
    # Running init again is harmless (idempotent grants and triggers).
    assert main(["init-postgres", "--url", owner, "--app-role", pg["role"]]) == 0
    store = Store.open_postgres(pg["app_url"], tmp_path / "raw", "jdoe")
    assert store.create_project("CLI project") > 0
    store.engine.dispose()


def test_rejects_bad_role_name(pg):
    from chromatix_store.admin import grant_app_role

    with pytest.raises(ValueError, match="invalid role name"):
        grant_app_role(pg["engine"](pg["owner_url"]), 'x"; DROP TABLE audit_event; --')
