"""Alembic environment.  ChromaTix runs migrations programmatically only
(:mod:`chromatix_store.migrate`), passing an open connection in
``config.attributes["connection"]``."""

from alembic import context

from chromatix_store.models import Base

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError(
        "ChromaTix migrations run through chromatix_store.migrate "
        "(chromatix-admin init-postgres, or opening a personal store)"
    )

context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    render_as_batch=connection.dialect.name == "sqlite",
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
