"""PostgreSQL deployment: owner vs application roles, and the ``chromatix-admin`` CLI.

Two roles keep the audit trail tamper-resistant:

* the **owner** role creates and migrates the schema and owns every table and
  trigger.  Only it (or a superuser) could drop a trigger, so its credentials
  are for administrators only;
* the **application** role is what ChromaTix clients connect as.  It can read
  everything and INSERT into the data tables, nothing more: no UPDATE, DELETE,
  TRUNCATE, no ownership (so no ALTER/DROP), and no CREATE in the schema.
  User accounts are managed by the owner, so the app role can only read them.

Usage::

    CHROMATIX_APP_PASSWORD=... chromatix-admin init-postgres \\
        --url postgresql://owner@db/chromatix --app-role chromatix_app --create-role
    chromatix-admin add-user --url postgresql://owner@db/chromatix \\
        --username jdoe --full-name "J. Doe"
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from collections.abc import Sequence

from psycopg import sql
from sqlalchemy import Engine, create_engine, make_url

from chromatix_store.models import Base, User
from chromatix_store.store import Store

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
READ_ONLY_TABLES = (User.__tablename__,)


def _check_role(role: str) -> str:
    if not _ROLE_NAME.match(role):
        raise ValueError(f"invalid role name {role!r} (lowercase letters, digits, _)")
    return role


def _execute(engine: Engine, statements: Sequence[sql.Composable]) -> None:
    """Run psycopg-composed statements (identifiers/literals quoted by psycopg)."""
    with engine.begin() as conn:
        raw = conn.connection.dbapi_connection
        assert raw is not None
        cur = raw.cursor()
        try:
            for statement in statements:
                cur.execute(statement)
        finally:
            cur.close()


def create_app_role(engine: Engine, role: str, password: str) -> None:
    """Create a login role with no special privileges (needs CREATEROLE)."""
    if not password:
        raise ValueError("the application role needs a password")
    _execute(
        engine,
        [
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
            ).format(sql.Identifier(_check_role(role)), sql.Literal(password))
        ],
    )


def grant_app_role(engine: Engine, role: str) -> None:
    """Give ``role`` exactly the privileges a ChromaTix client needs.

    Run as the owner after :meth:`Store.initialize`.  Idempotent.
    """
    ident = sql.Identifier(_check_role(role))
    statements: list[sql.Composable] = [
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(ident),
        sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(ident),
        sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC"),
        sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(ident),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident),
        sql.SQL("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {}").format(ident),
    ]
    for table in Base.metadata.tables:
        privileges = "SELECT" if table in READ_ONLY_TABLES else "SELECT, INSERT"
        statements.append(
            sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                sql.SQL(privileges), sql.Identifier(table), ident
            )
        )
    _execute(engine, statements)


def _owner_engine(url: str) -> Engine:
    return create_engine(make_url(url).set(drivername="postgresql+psycopg"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chromatix-admin", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-postgres", help="create schema + triggers and grant the app role")
    init.add_argument("--url", required=True, help="owner connection URL (no password in it)")
    init.add_argument("--app-role", required=True)
    init.add_argument(
        "--create-role",
        action="store_true",
        help="also create the app role; password from $CHROMATIX_APP_PASSWORD or a prompt",
    )

    add = sub.add_parser("add-user", help="create a ChromaTix user account")
    add.add_argument("--url", required=True, help="owner connection URL")
    add.add_argument("--username", required=True)
    add.add_argument("--full-name", default="")

    args = parser.parse_args(argv)
    engine = _owner_engine(args.url)
    try:
        if args.command == "init-postgres":
            if args.create_role:
                password = os.environ.get("CHROMATIX_APP_PASSWORD") or getpass.getpass(
                    f"Password for new role {args.app_role}: "
                )
                create_app_role(engine, args.app_role, password)
            Store.initialize(engine)
            grant_app_role(engine, args.app_role)
            Store.verify_protection(engine)
            print(f"initialised; role {args.app_role} granted SELECT/INSERT only")
        else:
            Store.add_user(engine, args.username, args.full_name)
            print(f"user {args.username} created")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
