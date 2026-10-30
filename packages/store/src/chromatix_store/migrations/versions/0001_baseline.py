"""Baseline schema: Empower-style object model, append-only triggers, PostgreSQL write functions.

Revision ID: 0001
Revises:
Create Date: 2026-10-29

Matches the schema created by ChromaTix up to and including PR #5
(result_peak.baseline_start/end, SECURITY DEFINER write functions).  Stores
created before migrations existed are stamped at this revision if they have
that layout (see chromatix_store.migrate).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("raw_file", "processing_method_version", "result", "result_peak", "audit_event")

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _ts() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def _create_tables() -> None:
    op.create_table(
        "user_account",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
    )
    op.create_table(
        "project",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("user_account.id"), nullable=False),
    )
    op.create_table(
        "raw_file",
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("original_name", sa.String(500), nullable=False),
        sa.Column("file_format", sa.String(32), nullable=False),
        sa.Column("imported_at", _ts(), nullable=False),
        sa.Column("imported_by", sa.Integer(), sa.ForeignKey("user_account.id"), nullable=False),
    )
    op.create_table(
        "sample_set",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("user_account.id"), nullable=False),
    )
    op.create_table(
        "injection",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sample_set_id", sa.Integer(), sa.ForeignKey("sample_set.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("sample_name", sa.String(200), nullable=False),
        sa.Column("sample_type", sa.String(32), nullable=False),
        sa.Column("vial", sa.String(32), nullable=True),
        sa.Column("amount", sa.Double(), nullable=True),
        sa.Column(
            "raw_file_sha256", sa.String(64), sa.ForeignKey("raw_file.sha256"), nullable=False
        ),
        sa.Column("created_at", _ts(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("user_account.id"), nullable=False),
        sa.UniqueConstraint("sample_set_id", "position"),
    )
    op.create_table(
        "processing_method",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.UniqueConstraint("project_id", "name"),
    )
    op.create_table(
        "processing_method_version",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("method_id", sa.Integer(), sa.ForeignKey("processing_method.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("params", _JSON, nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("user_account.id"), nullable=False),
        sa.UniqueConstraint("method_id", "version"),
    )
    op.create_table(
        "result",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("injection_id", sa.Integer(), sa.ForeignKey("injection.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "method_version_id",
            sa.Integer(),
            sa.ForeignKey("processing_method_version.id"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("user_account.id"), nullable=False),
        sa.UniqueConstraint("injection_id", "version"),
    )
    op.create_table(
        "result_peak",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("result_id", sa.Integer(), sa.ForeignKey("result.id"), nullable=False),
        sa.Column("peak_number", sa.Integer(), nullable=False),
        sa.Column("retention_time", sa.Double(), nullable=False),
        sa.Column("start_time", sa.Double(), nullable=False),
        sa.Column("end_time", sa.Double(), nullable=False),
        sa.Column("height", sa.Double(), nullable=False),
        sa.Column("area", sa.Double(), nullable=False),
        sa.Column("width_50", sa.Double(), nullable=True),
        sa.Column("boundary", sa.String(2), nullable=False),
        sa.Column("baseline_start", sa.Double(), nullable=False),
        sa.Column("baseline_end", sa.Double(), nullable=False),
    )
    op.create_table(
        "audit_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", _ts(), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("details", _JSON, nullable=False),
    )


def _sqlite_triggers() -> None:
    for table in APPEND_ONLY:
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                f"BEFORE {action} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} is append-only'); END"
            )


_HEADER = "LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$"

# Frozen copy of the PostgreSQL DDL as of this revision.
PG_DDL = [
    "CREATE OR REPLACE FUNCTION chromatix_append_only() RETURNS trigger "
    "LANGUAGE plpgsql AS $$ BEGIN "
    "RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; "
    "END $$",
    *[
        statement
        for table in APPEND_ONLY
        for statement in (
            f"CREATE OR REPLACE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION chromatix_append_only()",
            f"CREATE OR REPLACE TRIGGER {table}_no_truncate "
            f"BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION chromatix_append_only()",
        )
    ],
    f"""
CREATE OR REPLACE FUNCTION chromatix_current_user() RETURNS integer {_HEADER}
DECLARE uid integer;
BEGIN
    SELECT id INTO uid FROM user_account WHERE username = session_user AND active;
    IF uid IS NULL THEN
        RAISE EXCEPTION 'unknown or inactive user %', session_user USING ERRCODE = 'CXU01';
    END IF;
    RETURN uid;
END $$""",
    f"""
CREATE OR REPLACE FUNCTION chromatix_audit(
    p_action text, p_entity text, p_entity_id text, p_reason text, p_details jsonb
) RETURNS void {_HEADER}
BEGIN
    INSERT INTO audit_event (at, username, action, entity, entity_id, reason, details)
    VALUES (now(), session_user, p_action, p_entity, p_entity_id, coalesce(p_reason, ''),
            coalesce(p_details, '{{}}'::jsonb));
END $$""",
    f"""
CREATE OR REPLACE FUNCTION chromatix_create_project(p_name text, p_description text)
RETURNS integer {_HEADER}
DECLARE uid integer := chromatix_current_user(); new_id integer;
BEGIN
    INSERT INTO project (name, description, created_at, created_by)
    VALUES (p_name, coalesce(p_description, ''), now(), uid) RETURNING id INTO new_id;
    PERFORM chromatix_audit('project.create', 'project', new_id::text, '',
                            jsonb_build_object('name', p_name));
    RETURN new_id;
END $$""",
    f"""
CREATE OR REPLACE FUNCTION chromatix_create_sample_set(p_project integer, p_name text)
RETURNS integer {_HEADER}
DECLARE uid integer := chromatix_current_user(); new_id integer;
BEGIN
    INSERT INTO sample_set (project_id, name, created_at, created_by)
    VALUES (p_project, p_name, now(), uid) RETURNING id INTO new_id;
    PERFORM chromatix_audit('sample_set.create', 'sample_set', new_id::text, '',
                            jsonb_build_object('name', p_name));
    RETURN new_id;
END $$""",
    f"""
CREATE OR REPLACE FUNCTION chromatix_register_raw_file(
    p_sha256 text, p_size bigint, p_original_name text, p_format text
) RETURNS void {_HEADER}
DECLARE uid integer := chromatix_current_user();
BEGIN
    IF p_sha256 !~ '^[0-9a-f]{{64}}$' THEN
        RAISE EXCEPTION 'invalid sha256 %', p_sha256;
    END IF;
    INSERT INTO raw_file (sha256, size, original_name, file_format, imported_at, imported_by)
    VALUES (p_sha256, p_size, p_original_name, p_format, now(), uid)
    ON CONFLICT (sha256) DO NOTHING;
    IF FOUND THEN
        PERFORM chromatix_audit('raw_file.import', 'raw_file', p_sha256, '',
                                jsonb_build_object('original_name', p_original_name));
    END IF;
END $$""",
    f"""
CREATE OR REPLACE FUNCTION chromatix_add_injection(
    p_sample_set integer, p_sample_name text, p_sha256 text, p_sample_type text,
    p_vial text, p_amount double precision
) RETURNS integer {_HEADER}
DECLARE uid integer := chromatix_current_user(); next_pos integer; new_id integer;
BEGIN
    PERFORM 1 FROM sample_set WHERE id = p_sample_set FOR UPDATE;
    SELECT coalesce(max(position), 0) + 1 INTO next_pos
    FROM injection WHERE sample_set_id = p_sample_set;
    INSERT INTO injection (sample_set_id, position, sample_name, sample_type, vial, amount,
                           raw_file_sha256, created_at, created_by)
    VALUES (p_sample_set, next_pos, p_sample_name, coalesce(p_sample_type, 'unknown'), p_vial,
            p_amount, p_sha256, now(), uid)
    RETURNING id INTO new_id;
    PERFORM chromatix_audit('injection.create', 'injection', new_id::text, '',
                            jsonb_build_object('sample_name', p_sample_name,
                                               'raw_file', p_sha256));
    RETURN new_id;
END $$""",
    f"""
CREATE OR REPLACE FUNCTION chromatix_save_method_version(
    p_project integer, p_name text, p_params jsonb, p_reason text
) RETURNS integer {_HEADER}
DECLARE uid integer := chromatix_current_user(); mid integer; latest integer; new_id integer;
BEGIN
    PERFORM 1 FROM project WHERE id = p_project FOR UPDATE;
    SELECT id INTO mid FROM processing_method WHERE project_id = p_project AND name = p_name;
    IF mid IS NULL THEN
        INSERT INTO processing_method (project_id, name) VALUES (p_project, p_name)
        RETURNING id INTO mid;
    END IF;
    SELECT max(version) INTO latest FROM processing_method_version WHERE method_id = mid;
    IF latest IS NOT NULL AND coalesce(btrim(p_reason), '') = '' THEN
        RAISE EXCEPTION 'changing a processing method needs a reason' USING ERRCODE = 'CXR01';
    END IF;
    INSERT INTO processing_method_version (method_id, version, params, reason, created_at,
                                           created_by)
    VALUES (mid, coalesce(latest, 0) + 1, p_params, coalesce(p_reason, ''), now(), uid)
    RETURNING id INTO new_id;
    PERFORM chromatix_audit('method.save', 'processing_method_version', new_id::text,
                            coalesce(p_reason, ''),
                            jsonb_build_object('method', p_name, 'version', coalesce(latest, 0) + 1,
                                               'params', p_params));
    RETURN new_id;
END $$""",
    f"""
CREATE OR REPLACE FUNCTION chromatix_save_result(
    p_injection integer, p_method_version integer, p_reason text, p_peaks jsonb
) RETURNS integer {_HEADER}
DECLARE uid integer := chromatix_current_user(); latest integer; new_id integer; n integer;
BEGIN
    PERFORM 1 FROM injection WHERE id = p_injection FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown injection %', p_injection;
    END IF;
    SELECT max(version) INTO latest FROM result WHERE injection_id = p_injection;
    IF latest IS NOT NULL AND coalesce(btrim(p_reason), '') = '' THEN
        RAISE EXCEPTION 'reprocessing an injection needs a reason' USING ERRCODE = 'CXR01';
    END IF;
    INSERT INTO result (injection_id, version, method_version_id, reason, created_at,
                        created_by)
    VALUES (p_injection, coalesce(latest, 0) + 1, p_method_version, coalesce(p_reason, ''),
            now(), uid)
    RETURNING id INTO new_id;
    INSERT INTO result_peak (result_id, peak_number, retention_time, start_time, end_time,
                             height, area, width_50, boundary, baseline_start, baseline_end)
    SELECT new_id, x.peak_number, x.retention_time, x.start_time, x.end_time, x.height, x.area,
           x.width_50, x.boundary, x.baseline_start, x.baseline_end
    FROM jsonb_to_recordset(coalesce(p_peaks, '[]'::jsonb)) AS x(
        peak_number integer, retention_time double precision, start_time double precision,
        end_time double precision, height double precision, area double precision,
        width_50 double precision, boundary text, baseline_start double precision,
        baseline_end double precision);
    GET DIAGNOSTICS n = ROW_COUNT;
    PERFORM chromatix_audit('result.create', 'result', new_id::text, coalesce(p_reason, ''),
                            jsonb_build_object('injection_id', p_injection,
                                               'version', coalesce(latest, 0) + 1,
                                               'method_version_id', p_method_version,
                                               'peaks', n));
    RETURN new_id;
END $$""",
    *[
        f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC"
        for signature in (
            "chromatix_current_user()",
            "chromatix_audit(text, text, text, text, jsonb)",
            "chromatix_create_project(text, text)",
            "chromatix_create_sample_set(integer, text)",
            "chromatix_register_raw_file(text, bigint, text, text)",
            "chromatix_add_injection(integer, text, text, text, text, double precision)",
            "chromatix_save_method_version(integer, text, jsonb, text)",
            "chromatix_save_result(integer, integer, text, jsonb)",
        )
    ],
]


def run_postgres_ddl(statements: Sequence[str]) -> None:
    """Execute DDL through a raw psycopg cursor with no parameters, so the % in
    PL/pgSQL RAISE messages is not taken for a query placeholder."""
    raw = op.get_bind().connection.dbapi_connection
    assert raw is not None
    cursor = raw.cursor()
    try:
        for statement in statements:
            cursor.execute(statement)
    finally:
        cursor.close()


def upgrade() -> None:
    _create_tables()
    if op.get_bind().dialect.name == "sqlite":
        _sqlite_triggers()
    else:
        run_postgres_ddl(PG_DDL)


def downgrade() -> None:
    raise NotImplementedError("ChromaTix does not downgrade stores (append-only history)")
