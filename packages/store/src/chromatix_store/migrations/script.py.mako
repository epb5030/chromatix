"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Rules (docs/adr/0003):
* Migrations are frozen.  Never edit one that has been merged; add a new one.
* Write SQL for triggers and PostgreSQL functions out in full here, rather than
  importing it from application code that may change later.
* No downgrades: history tables are append-only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    raise NotImplementedError("ChromaTix does not downgrade stores (append-only history)")
