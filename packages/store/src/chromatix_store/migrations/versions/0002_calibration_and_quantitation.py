"""Calibration and quantitation.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29

* new append-only table ``calibration`` (one curve per component per processing
  of a sample set);
* ``injection.component_amounts`` (known amounts for standards);
* ``result_peak.component`` / ``amount`` / ``calibration_id``;
* PostgreSQL: ``chromatix_add_injection`` gains ``p_amounts``,
  ``chromatix_save_result`` stores identification and amounts, new
  ``chromatix_save_calibration``.

SQLite: columns are added with plain ALTER TABLE (no table rebuild), so the
append-only triggers on ``result_peak`` stay in place.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_HEADER = "LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$"

PG_DDL = [
    "CREATE OR REPLACE TRIGGER calibration_append_only BEFORE UPDATE OR DELETE ON calibration "
    "FOR EACH ROW EXECUTE FUNCTION chromatix_append_only()",
    "CREATE OR REPLACE TRIGGER calibration_no_truncate BEFORE TRUNCATE ON calibration "
    "FOR EACH STATEMENT EXECUTE FUNCTION chromatix_append_only()",
    "DROP FUNCTION chromatix_add_injection(integer, text, text, text, text, double precision)",
    f"""
CREATE FUNCTION chromatix_add_injection(
    p_sample_set integer, p_sample_name text, p_sha256 text, p_sample_type text,
    p_vial text, p_amount double precision, p_amounts jsonb
) RETURNS integer {_HEADER}
DECLARE uid integer := chromatix_current_user(); next_pos integer; new_id integer;
BEGIN
    PERFORM 1 FROM sample_set WHERE id = p_sample_set FOR UPDATE;
    SELECT coalesce(max(position), 0) + 1 INTO next_pos
    FROM injection WHERE sample_set_id = p_sample_set;
    INSERT INTO injection (sample_set_id, position, sample_name, sample_type, vial, amount,
                           component_amounts, raw_file_sha256, created_at, created_by)
    VALUES (p_sample_set, next_pos, p_sample_name, coalesce(p_sample_type, 'unknown'), p_vial,
            p_amount, p_amounts, p_sha256, now(), uid)
    RETURNING id INTO new_id;
    PERFORM chromatix_audit('injection.create', 'injection', new_id::text, '',
                            jsonb_build_object('sample_name', p_sample_name,
                                               'raw_file', p_sha256,
                                               'sample_type', coalesce(p_sample_type, 'unknown'),
                                               'component_amounts', p_amounts));
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
                             height, area, width_50, boundary, baseline_start, baseline_end,
                             component, amount, calibration_id)
    SELECT new_id, x.peak_number, x.retention_time, x.start_time, x.end_time, x.height, x.area,
           x.width_50, x.boundary, x.baseline_start, x.baseline_end, x.component, x.amount,
           x.calibration_id
    FROM jsonb_to_recordset(coalesce(p_peaks, '[]'::jsonb)) AS x(
        peak_number integer, retention_time double precision, start_time double precision,
        end_time double precision, height double precision, area double precision,
        width_50 double precision, boundary text, baseline_start double precision,
        baseline_end double precision, component text, amount double precision,
        calibration_id integer);
    GET DIAGNOSTICS n = ROW_COUNT;
    PERFORM chromatix_audit('result.create', 'result', new_id::text, coalesce(p_reason, ''),
                            jsonb_build_object('injection_id', p_injection,
                                               'version', coalesce(latest, 0) + 1,
                                               'method_version_id', p_method_version,
                                               'peaks', n));
    RETURN new_id;
END $$""",
    f"""
CREATE OR REPLACE FUNCTION chromatix_save_calibration(
    p_sample_set integer, p_method_version integer, p_component text, p_model text,
    p_weighting text, p_through_origin boolean, p_coefficients jsonb,
    p_r_squared double precision, p_points jsonb
) RETURNS integer {_HEADER}
DECLARE uid integer := chromatix_current_user(); new_id integer;
BEGIN
    INSERT INTO calibration (sample_set_id, method_version_id, component, model, weighting,
                             through_origin, coefficients, r_squared, points, created_at,
                             created_by)
    VALUES (p_sample_set, p_method_version, p_component, p_model, p_weighting,
            p_through_origin, p_coefficients, p_r_squared, p_points, now(), uid)
    RETURNING id INTO new_id;
    PERFORM chromatix_audit('calibration.create', 'calibration', new_id::text, '',
                            jsonb_build_object('sample_set_id', p_sample_set,
                                               'method_version_id', p_method_version,
                                               'component', p_component, 'model', p_model,
                                               'coefficients', p_coefficients,
                                               'r_squared', p_r_squared,
                                               'points', jsonb_array_length(p_points)));
    RETURN new_id;
END $$""",
    "REVOKE EXECUTE ON FUNCTION chromatix_add_injection(integer, text, text, text, text, "
    "double precision, jsonb) FROM PUBLIC",
    "REVOKE EXECUTE ON FUNCTION chromatix_save_result(integer, integer, text, jsonb) FROM PUBLIC",
    "REVOKE EXECUTE ON FUNCTION chromatix_save_calibration(integer, integer, text, text, text, "
    "boolean, jsonb, double precision, jsonb) FROM PUBLIC",
]


def run_postgres_ddl(statements: Sequence[str]) -> None:
    """Raw psycopg cursor, no parameters: keeps PL/pgSQL's % intact."""
    raw = op.get_bind().connection.dbapi_connection
    assert raw is not None
    cursor = raw.cursor()
    try:
        for statement in statements:
            cursor.execute(statement)
    finally:
        cursor.close()


def upgrade() -> None:
    op.create_table(
        "calibration",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sample_set_id", sa.Integer(), sa.ForeignKey("sample_set.id"), nullable=False),
        sa.Column(
            "method_version_id",
            sa.Integer(),
            sa.ForeignKey("processing_method_version.id"),
            nullable=False,
        ),
        sa.Column("component", sa.String(200), nullable=False),
        sa.Column("model", sa.String(16), nullable=False),
        sa.Column("weighting", sa.String(8), nullable=False),
        sa.Column("through_origin", sa.Boolean(), nullable=False),
        sa.Column("coefficients", _JSON, nullable=False),
        sa.Column("r_squared", sa.Double(), nullable=True),
        sa.Column("points", _JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("user_account.id"), nullable=False),
    )
    if op.get_bind().dialect.name == "sqlite":
        for statement in (
            "ALTER TABLE injection ADD COLUMN component_amounts JSON",
            "ALTER TABLE result_peak ADD COLUMN component VARCHAR(200)",
            "ALTER TABLE result_peak ADD COLUMN amount DOUBLE",
            "ALTER TABLE result_peak ADD COLUMN calibration_id INTEGER REFERENCES calibration (id)",
            *[
                f"CREATE TRIGGER IF NOT EXISTS calibration_no_{action.lower()} "
                f"BEFORE {action} ON calibration BEGIN "
                "SELECT RAISE(ABORT, 'calibration is append-only'); END"
                for action in ("UPDATE", "DELETE")
            ],
        ):
            op.execute(statement)
    else:
        op.add_column("injection", sa.Column("component_amounts", _JSON, nullable=True))
        op.add_column("result_peak", sa.Column("component", sa.String(200), nullable=True))
        op.add_column("result_peak", sa.Column("amount", sa.Double(), nullable=True))
        op.add_column("result_peak", sa.Column("calibration_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "result_peak_calibration_id_fkey",
            "result_peak",
            "calibration",
            ["calibration_id"],
            ["id"],
        )
        run_postgres_ddl(PG_DDL)


def downgrade() -> None:
    raise NotImplementedError("ChromaTix does not downgrade stores (append-only history)")
