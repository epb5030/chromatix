"""PostgreSQL roles: owner, client group, personal logins, SECURITY DEFINER writes (ADR 0003)."""

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
    """A fresh database, an owner engine and unique role names; all dropped afterwards."""
    if not PG_URL:
        pytest.fail("CHROMATIX_REQUIRE_POSTGRES=1 but CHROMATIX_TEST_POSTGRES_URL is unset")
    suffix = uuid.uuid4().hex[:10]
    server = make_url(PG_URL).set(drivername="postgresql+psycopg")
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    db = f"chromatix_test_{suffix}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db}"'))
    owner_url = server.set(database=db)
    engines, roles = [], []

    def engine(url):
        e = create_engine(url)
        engines.append(e)
        return e

    def role(name):
        roles.insert(0, f"{name}_{suffix}")
        return roles[0]

    def cleanup():
        for e in engines:
            e.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
            for r in roles:
                conn.execute(text(f'DROP ROLE IF EXISTS "{r}"'))
        admin.dispose()

    request.addfinalizer(cleanup)
    return {"owner_url": owner_url, "engine": engine, "role": role}


def _provision(pg):
    """Owner sets up the schema, a client group and one user; returns (owner, user_url, user)."""
    from chromatix_store.admin import create_group_role, create_user, grant_group_role

    owner = pg["engine"](pg["owner_url"])
    group, user, password = (
        pg["role"]("chromatix_users"),
        pg["role"]("analyst"),
        secrets.token_hex(12),
    )
    pg["group"] = group
    create_group_role(owner, group)
    Store.initialize(owner)
    grant_group_role(owner, group)
    create_user(owner, user, "A. Analyst", group, password)
    return owner, pg["owner_url"].set(username=user, password=password), user


def test_user_runs_the_workflow_under_their_own_name(pg, tmp_path):
    _, user_url, user = _provision(pg)
    store = Store.open_postgres(user_url, tmp_path / "raw", user)
    path = tmp_path / "inj.cdf"
    write_aia(gaussian_chromatogram([GaussianPeak(2.0, 100, 0.05)], noise=0.3), path)
    project = store.create_project("P")
    sample_set = store.create_sample_set(project, "SS")
    injection = store.add_injection(sample_set, "Std", store.import_raw_file(path))
    method = store.save_method_version(project, "M", IntegrationParams())
    result = store.process_injection(injection, method)
    assert len(store.peaks(result)) == 1
    trail = store.audit_trail()
    assert [e.action for e in trail][-1] == "result.create"
    assert {e.username for e in trail if e.action != "user.create"} == {user}
    store.engine.dispose()


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "INSERT INTO audit_event (at, username, action, entity, entity_id, reason, details) "
            "VALUES (now(), 'someone', 'forged', 'x', '1', '', '{}')",
            "permission denied",
        ),
        (
            "INSERT INTO project (name, description, created_at, created_by) "
            "VALUES ('x', '', now(), 1)",
            "permission denied",
        ),
        ("UPDATE audit_event SET reason = 'x'", "permission denied"),
        ("DELETE FROM result_peak", "permission denied"),
        ("TRUNCATE audit_event", "permission denied"),
        ("UPDATE project SET name = 'x'", "permission denied"),
        (
            "INSERT INTO user_account (username, full_name, active, created_at) "
            "VALUES ('mallory', '', true, now())",
            "permission denied",
        ),
        (
            "SELECT chromatix_audit('forged', 'x', '1', '', '{}'::jsonb)",
            "permission denied for function",
        ),
        ("SELECT chromatix_current_user()", "permission denied for function"),
        ("DROP TRIGGER audit_event_append_only ON audit_event", "must be owner"),
        ("ALTER TABLE audit_event DISABLE TRIGGER ALL", "must be owner"),
        ("DROP FUNCTION chromatix_save_result(integer, integer, text, jsonb)", "must be owner"),
        ("CREATE TABLE sneaky (id int)", "permission denied"),
    ],
)
def test_user_cannot_tamper(pg, statement, message):
    _, user_url, _ = _provision(pg)
    user = pg["engine"](user_url)
    with pytest.raises(DBAPIError, match=message), user.begin() as conn:
        conn.execute(text(statement))


def test_login_outside_the_group_gets_nothing(pg):
    owner, _, _ = _provision(pg)
    outsider, password = pg["role"]("outsider"), secrets.token_hex(12)
    with owner.begin() as conn:
        conn.execute(text(f"CREATE ROLE \"{outsider}\" LOGIN PASSWORD '{password}'"))
    engine = pg["engine"](pg["owner_url"].set(username=outsider, password=password))
    for statement in ("SELECT chromatix_create_project('x', '')", "SELECT * FROM audit_event"):
        with pytest.raises(DBAPIError, match="permission denied"), engine.begin() as conn:
            conn.execute(text(statement))


def test_cannot_work_under_another_name(pg, tmp_path):
    _, user_url, _ = _provision(pg)
    with pytest.raises(PermissionError, match="own database role"):
        Store.open_postgres(user_url, tmp_path / "raw", "someone_else")


def test_database_enforces_reason_for_reprocessing(pg, tmp_path):
    """Even calling the functions directly, a second result needs a reason."""
    _, user_url, user = _provision(pg)
    store = Store.open_postgres(user_url, tmp_path / "raw", user)
    path = tmp_path / "inj.cdf"
    write_aia(gaussian_chromatogram([GaussianPeak(2.0, 100, 0.05)]), path)
    project = store.create_project("P")
    injection = store.add_injection(
        store.create_sample_set(project, "SS"), "Std", store.import_raw_file(path)
    )
    method = store.save_method_version(project, "M", IntegrationParams())
    raw = pg["engine"](user_url)
    call = text("SELECT chromatix_save_result(:i, :m, '', '[]'::jsonb)")
    with raw.begin() as conn:
        conn.execute(call, {"i": injection, "m": method})
    with pytest.raises(DBAPIError, match="needs a reason"), raw.begin() as conn:
        conn.execute(call, {"i": injection, "m": method})
    store.engine.dispose()


def test_refuses_uninitialised_database(pg, tmp_path):
    with pytest.raises(ProtectionMissingError, match="not initialised"):
        Store.open_postgres(pg["owner_url"], tmp_path / "raw", "analyst")


def test_refuses_disabled_trigger(pg, tmp_path):
    owner, user_url, user = _provision(pg)
    with owner.begin() as conn:
        conn.execute(text("ALTER TABLE result DISABLE TRIGGER result_append_only"))
    with pytest.raises(ProtectionMissingError, match="result_append_only"):
        Store.open_postgres(user_url, tmp_path / "raw", user)


def test_refuses_missing_write_function(pg, tmp_path):
    owner, user_url, user = _provision(pg)
    with owner.begin() as conn:
        conn.execute(text("DROP FUNCTION chromatix_save_result(integer, integer, text, jsonb)"))
    with pytest.raises(ProtectionMissingError, match="chromatix_save_result"):
        Store.open_postgres(user_url, tmp_path / "raw", user)


def test_regrant_removes_old_insert_privileges(pg):
    """Upgrading from the earlier INSERT-based setup: re-running the grant revokes INSERT."""
    from chromatix_store.admin import grant_group_role

    owner, user_url, _ = _provision(pg)
    group = pg["group"]
    with owner.begin() as conn:
        conn.execute(text(f'GRANT INSERT ON audit_event TO "{group}"'))
    grant_group_role(owner, group)
    user = pg["engine"](user_url)
    with pytest.raises(DBAPIError, match="permission denied"), user.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_event (at, username, action, entity, entity_id, reason, "
                "details) VALUES (now(), 'x', 'x', 'x', '1', '', '{}')"
            )
        )


def test_admin_cli(pg, tmp_path, monkeypatch, capsys):
    from chromatix_store.admin import main

    owner = pg["owner_url"].render_as_string(hide_password=False)
    group, user, password = pg["role"]("chromatix_users"), pg["role"]("jdoe"), secrets.token_hex(12)
    assert main(["init-postgres", "--url", owner, "--group-role", group, "--create-role"]) == 0
    monkeypatch.setenv("CHROMATIX_USER_PASSWORD", password)
    assert (
        main(
            [
                "add-user",
                "--url",
                owner,
                "--group-role",
                group,
                "--username",
                user,
                "--full-name",
                "J. Doe",
            ]
        )
        == 0
    )
    assert password not in capsys.readouterr().out
    # Running init again is harmless (idempotent DDL and grants).
    assert main(["init-postgres", "--url", owner, "--group-role", group]) == 0
    store = Store.open_postgres(
        pg["owner_url"].set(username=user, password=password), tmp_path / "raw", user
    )
    assert store.create_project("CLI project") > 0
    assert store.audit_trail("project")[0].username == user
    store.engine.dispose()


def test_rejects_bad_role_name(pg):
    from chromatix_store.admin import grant_group_role

    with pytest.raises(ValueError, match="invalid role name"):
        grant_group_role(pg["engine"](pg["owner_url"]), 'x"; DROP TABLE audit_event; --')
