"""Serialize shared-store calibration writes for a sample set."""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # Replace the existing function in place so existing client grants still apply.
    op.execute("""
CREATE OR REPLACE FUNCTION chromatix_save_calibration(
    p_sample_set integer, p_method_version integer, p_component text, p_model text,
    p_weighting text, p_through_origin boolean, p_coefficients jsonb,
    p_r_squared double precision, p_points jsonb
) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE uid integer := chromatix_current_user(); new_id integer;
BEGIN
    -- The first call locks the sample set until the processing transaction ends.
    -- This prevents runs for the same sample set from interleaving their calibrations.
    PERFORM 1 FROM sample_set WHERE id = p_sample_set FOR UPDATE;
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
END $$""")


def downgrade() -> None:
    raise NotImplementedError("ChromaTix does not downgrade stores (append-only history)")
