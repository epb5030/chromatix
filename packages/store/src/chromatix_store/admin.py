"""PostgreSQL deployment: owner, client group, personal logins, and the ``chromatix-admin`` CLI.

Roles on a shared store:

* the **owner** creates and migrates the schema and owns every table, trigger
  and write function.  Its credentials are for administrators only;
* a **client group role** (NOLOGIN, e.g. ``chromatix_users``) may SELECT from
  every table and EXECUTE the SECURITY DEFINER write functions
  (:mod:`chromatix_store.pg_functions`), and nothing else: no INSERT, UPDATE,
  DELETE or TRUNCATE on any table, no ownership, no CREATE in the schema;
* every ChromaTix user has a **personal login role** of the same name, a member
  of the group.  The write functions take the user from ``session_user``, so the
  audit trail records who actually authenticated to PostgreSQL.

Usage::

    chromatix-admin init-postgres --url postgresql://owner@db/chromatix \\
        --group-role chromatix_users --create-role
    CHROMATIX_USER_PASSWORD=... chromatix-admin add-user \\
        --url postgresql://owner@db/chromatix --group-role chromatix_users \\
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
from sqlalchemy import Engine, create_engine, make_url, text

from chromatix_store import migrate, pg_functions
from chromatix_store.store import Store

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


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


def _role_exists(engine: Engine, role: str) -> bool:
    with engine.connect() as conn:
        return bool(conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}))


def create_group_role(engine: Engine, role: str) -> None:
    """Create the client group role (cannot log in; needs CREATEROLE)."""
    _execute(
        engine,
        [
            sql.SQL(
                "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                "NOBYPASSRLS"
            ).format(sql.Identifier(_check_role(role)))
        ],
    )


def grant_group_role(engine: Engine, role: str) -> None:
    """Give the group exactly what clients need: SELECT + EXECUTE on the write functions.

    Run as the owner after :meth:`Store.initialize`.  Idempotent; also removes
    any INSERT privilege left from an older setup.
    """
    ident = sql.Identifier(_check_role(role))
    statements: list[sql.Composable] = [
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(ident),
        sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(ident),
        sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC"),
        sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(ident),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident),
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(ident),
    ]
    for signature in pg_functions.INTERNAL_FUNCTIONS:
        statements.append(
            sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM {}").format(sql.SQL(signature), ident)
        )
    for signature in pg_functions.PUBLIC_FUNCTIONS:
        statements.append(
            sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(sql.SQL(signature), ident)
        )
    _execute(engine, statements)


def create_user(
    engine: Engine, username: str, full_name: str, group: str, password: str | None
) -> None:
    """Create a ChromaTix account plus a personal login role in ``group``.

    If the login role already exists (e.g. provisioned from LDAP), it is only
    added to the group and ``password`` is ignored.
    """
    user = sql.Identifier(_check_role(username))
    group_ident = sql.Identifier(_check_role(group))
    if _role_exists(engine, username):
        _execute(engine, [sql.SQL("GRANT {} TO {}").format(group_ident, user)])
    else:
        if not password:
            raise ValueError(f"a password is needed to create login role {username!r}")
        _execute(
            engine,
            [
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} INHERIT NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS IN ROLE {}"
                ).format(user, sql.Literal(password), group_ident)
            ],
        )
    Store.add_user(engine, username, full_name)


def _owner_engine(url: str) -> Engine:
    return create_engine(make_url(url).set(drivername="postgresql+psycopg"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chromatix-admin", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser(
        "init-postgres",
        help="create or upgrade the schema (migrations), then (re)apply the group's grants",
    )
    init.add_argument("--url", required=True, help="owner connection URL (no password in it)")
    init.add_argument("--group-role", "--app-role", dest="group_role", required=True)
    init.add_argument("--create-role", action="store_true", help="also create the group role")

    add = sub.add_parser("add-user", help="create a ChromaTix user with a personal login role")
    add.add_argument("--url", required=True, help="owner connection URL")
    add.add_argument("--group-role", required=True)
    add.add_argument("--username", required=True)
    add.add_argument("--full-name", default="")

    status = sub.add_parser("status", help="show the store's schema revision")
    status.add_argument("--url", required=True, help="connection URL (any role)")

    new = sub.add_parser(
        "new-migration", help="(developers) autogenerate a migration from model changes"
    )
    new.add_argument("message")

    args = parser.parse_args(argv)
    if args.command == "new-migration":
        migrate.new_revision(args.message)
        return 0
    engine = _owner_engine(args.url)
    try:
        if args.command == "init-postgres":
            before = migrate.current_revision(engine)
            if args.create_role:
                create_group_role(engine, args.group_role)
            Store.initialize(engine)
            grant_group_role(engine, args.group_role)
            Store.verify_protection(engine)
            after = migrate.current_revision(engine)
            change = "created" if before is None else ("upgraded" if before != after else "checked")
            print(
                f"schema {change} (revision {before or '-'} -> {after}); "
                f"group {args.group_role} may SELECT and call write functions only"
            )
        elif args.command == "status":
            current, head = migrate.current_revision(engine), migrate.head_revision()
            state = "up to date" if current == head else "needs `init-postgres`"
            print(f"store revision: {current or 'none'}; this ChromaTix: {head} ({state})")
        else:
            password = None
            if not _role_exists(engine, args.username):
                password = os.environ.get("CHROMATIX_USER_PASSWORD") or getpass.getpass(
                    f"Password for new login role {args.username}: "
                )
            create_user(engine, args.username, args.full_name, args.group_role, password)
            print(f"user {args.username} created (member of {args.group_role})")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
