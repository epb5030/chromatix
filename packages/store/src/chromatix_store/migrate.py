"""Schema migrations (Alembic), run programmatically.

* Personal (SQLite) stores are upgraded when opened (after a backup copy).
* Shared (PostgreSQL) stores are upgraded by the owner with
  ``chromatix-admin init-postgres``; clients refuse a store whose revision
  differs from the one this version of ChromaTix expects.

Stores created before migrations existed have no ``alembic_version`` table.
If their layout matches the baseline revision they are stamped as ``0001``;
older layouts are refused.

Developers create a new revision with::

    uv run chromatix-admin new-migration "add calibration tables"

which upgrades a scratch SQLite database to head and autogenerates the
difference to the current models.  Review the file, write out trigger and
PostgreSQL function SQL in full, and keep it frozen once merged.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import Connection, Engine, create_engine, inspect

BASELINE = "0001"
_LOCATION = Path(__file__).parent / "migrations"


class StoreVersionError(RuntimeError):
    """The store's schema revision cannot be used by this version of ChromaTix."""


def _config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_LOCATION))
    config.set_main_option("file_template", "%%(rev)s_%%(slug)s")
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def head_revision() -> str:
    head = ScriptDirectory.from_config(_config()).get_current_head()
    assert head is not None
    return head


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _legacy_layout(conn: Connection) -> str:
    """'empty', 'baseline' (pre-migration store at 0001) or 'old'."""
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    if "user_account" not in tables:
        return "empty"
    if "result_peak" in tables:
        columns = {c["name"] for c in inspector.get_columns("result_peak")}
        if {"baseline_start", "baseline_end"} <= columns:
            return "baseline"
    return "old"


def _check_known(revision: str) -> None:
    try:
        known = ScriptDirectory.from_config(_config()).get_revision(revision)
    except CommandError:
        known = None
    if known is None:
        raise StoreVersionError(
            f"store schema revision {revision!r} is newer than this ChromaTix "
            f"(expects {head_revision()}); update ChromaTix"
        )


def upgrade(engine: Engine) -> None:
    """Bring the store to the head revision (idempotent)."""
    with engine.begin() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
        if current is not None:
            _check_known(current)
        else:
            layout = _legacy_layout(conn)
            if layout == "old":
                raise StoreVersionError(
                    "this store was created by an early ChromaTix build before migrations "
                    "and cannot be upgraded; export what you need and create a new store"
                )
            if layout == "baseline":
                command.stamp(_config(conn), BASELINE)
        command.upgrade(_config(conn), "head")


def needs_upgrade(engine: Engine) -> bool:
    return current_revision(engine) != head_revision()


def new_revision(message: str) -> None:
    """Developer helper: autogenerate the next numbered revision from the model changes."""
    count = sum(1 for _ in ScriptDirectory.from_config(_config()).walk_revisions())
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite:///{Path(tmp) / 'scratch.sqlite'}")
        upgrade(engine)
        with engine.begin() as conn:
            command.revision(
                _config(conn), message=message, autogenerate=True, rev_id=f"{count + 1:04d}"
            )
        engine.dispose()
