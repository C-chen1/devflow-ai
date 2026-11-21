"""Add the trusted-side accounting tables for one controlled RSI experiment.

These tables record limits and results; they do not run anything. One experiment freezes the
V0 execution manifest digest, the declared task set, the candidate count, the total model-token
budget, the wall-clock budget, the promotion thresholds and who opened it. Candidates carry
their own execution digest and lineage, reservations make token accounting reserve-then-settle,
and task results make each attempt's verdict idempotent.

Two properties are enforced by constraints rather than by the service's own checks: one Run is
recorded for at most one candidate of an experiment (``uq_experiment_task_results_run``), and a
reservation that ends without a settled amount must state who ended it and why (released, or
usage unknown). ``evidence_source`` keeps a caller's declaration apart from a verdict the shared
classifier derived, and only the classified kind may carry a ``verification_id``. A classified
result also has to state the attempt context it was classified under — ``deadline_context``
(fired / not_fired / unknown) and where that statement came from — because the classifier checks
the deadline before it looks at the formal chain, so an omitted context would silently change the
verdict.

The limits are not editable through the service after the experiment is opened: changing them
mid-experiment would silently rewrite what the promotion decision was measured against. A
database-level UPDATE could still change a row, which is reported as a known limit rather than
treated as impossible.
"""
from alembic import op
import sqlalchemy as sa

revision = "d9b3c7e1a250"
down_revision = "b41e9d2c8370"
branch_labels = None
depends_on = None

SHA256 = "^[0-9a-f]{64}$"


def upgrade() -> None:
    op.create_table(
        "experiments",
        sa.Column("id", sa.Uuid(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("identity_key", sa.Text(), nullable=False),
        sa.Column("base_execution_sha256", sa.Text(), nullable=False),
        sa.Column("max_candidates", sa.Integer(), nullable=False),
        sa.Column("max_total_model_tokens", sa.BigInteger(), nullable=False),
        sa.Column("max_total_seconds", sa.Integer(), nullable=False),
        sa.Column("promotion_min_formal_passes", sa.Integer(), nullable=False),
        sa.Column("promotion_max_failures", sa.Integer(), nullable=False),
        sa.Column("reserved_tokens", sa.BigInteger(), server_default=sa.text("0"),
                  nullable=False),
        sa.Column("settled_tokens", sa.BigInteger(), server_default=sa.text("0"),
                  nullable=False),
        sa.Column("over_budget", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_experiments_identity_key"),
        sa.CheckConstraint("char_length(btrim(identity_key)) > 0",
                           name="ck_experiments_identity_key"),
        sa.CheckConstraint(f"base_execution_sha256 ~ '{SHA256}'",
                           name="ck_experiments_base_execution_sha256"),
        sa.CheckConstraint("max_candidates BETWEEN 1 AND 100",
                           name="ck_experiments_max_candidates"),
        sa.CheckConstraint("max_total_model_tokens > 0",
                           name="ck_experiments_max_total_model_tokens"),
        sa.CheckConstraint("max_total_seconds BETWEEN 1 AND 2592000",
                           name="ck_experiments_max_total_seconds"),
        sa.CheckConstraint("promotion_min_formal_passes >= 1",
                           name="ck_experiments_promotion_min_formal_passes"),
        sa.CheckConstraint("promotion_max_failures >= 0",
                           name="ck_experiments_promotion_max_failures"),
        sa.CheckConstraint("reserved_tokens >= 0", name="ck_experiments_reserved_tokens"),
        sa.CheckConstraint("settled_tokens >= 0", name="ck_experiments_settled_tokens"),
        sa.CheckConstraint(
            "settled_tokens <= max_total_model_tokens OR over_budget",
            name="ck_experiments_settled_within_budget"),
        sa.CheckConstraint("char_length(btrim(created_by)) > 0",
                           name="ck_experiments_created_by"),
        sa.CheckConstraint("deadline_at > created_at", name="ck_experiments_deadline"),
        sa.CheckConstraint("(closed_at IS NULL) = (closed_reason IS NULL)",
                           name="ck_experiments_closure"),
    )

    op.create_table(
        "experiment_tasks",
        sa.Column("id", sa.Uuid(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("experiment_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("task_id", sa.Uuid(as_uuid=True), nullable=False),
        # The target-repository baseline the Task pinned when the experiment opened. Frozen
        # here so "all candidates were measured on the same baseline" is a fact the ledger can
        # check, not something to look up in a row that might have changed since.
        sa.Column("base_commit", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"],
                                name="fk_experiment_tasks_experiment_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "task_id",
                            name="uq_experiment_tasks_task"),
        sa.CheckConstraint(
            "base_commit ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'",
            name="ck_experiment_tasks_base_commit"),
    )

    op.create_table(
        "experiment_candidates",
        sa.Column("id", sa.Uuid(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("experiment_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("execution_sha256", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("state", sa.Text(), server_default=sa.text("'prepared'"), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("formal_passes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("non_formal_results", sa.Integer(), server_default=sa.text("0"),
                  nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("concluded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"],
                                name="fk_experiment_candidates_experiment_id"),
        sa.ForeignKeyConstraint(["parent_id"], ["experiment_candidates.id"],
                                name="fk_experiment_candidates_parent_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "label", name="uq_experiment_candidates_label"),
        sa.CheckConstraint("char_length(btrim(label)) BETWEEN 1 AND 200",
                           name="ck_experiment_candidates_label"),
        sa.CheckConstraint(f"execution_sha256 ~ '{SHA256}'",
                           name="ck_experiment_candidates_execution_sha256"),
        sa.CheckConstraint("state IN ('prepared', 'concluded')",
                           name="ck_experiment_candidates_state"),
        sa.CheckConstraint("outcome IS NULL OR outcome IN ('accepted', 'rejected', 'abandoned')",
                           name="ck_experiment_candidates_outcome"),
        sa.CheckConstraint("formal_passes >= 0", name="ck_experiment_candidates_formal_passes"),
        sa.CheckConstraint("non_formal_results >= 0",
                           name="ck_experiment_candidates_non_formal_results"),
        sa.CheckConstraint("(state = 'concluded') = (outcome IS NOT NULL)",
                           name="ck_experiment_candidates_concluded_outcome"),
        sa.CheckConstraint("(outcome IS NULL) = (reason IS NULL)",
                           name="ck_experiment_candidates_reason"),
        sa.CheckConstraint("(outcome IS NULL) = (decided_by IS NULL)",
                           name="ck_experiment_candidates_decided_by"),
        sa.CheckConstraint("(state = 'concluded') = (concluded_at IS NOT NULL)",
                           name="ck_experiment_candidates_concluded_at"),
        sa.CheckConstraint("parent_id IS NULL OR parent_id <> id",
                           name="ck_experiment_candidates_parent"),
    )
    op.create_index("ix_experiment_candidates_experiment_id", "experiment_candidates",
                    ["experiment_id"])

    op.create_table(
        "experiment_records",
        sa.Column("id", sa.Uuid(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("experiment_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("candidate_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("task_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("record_sha256", sa.Text(), nullable=False),
        sa.Column("registered_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"],
                                name="fk_experiment_records_experiment_id"),
        sa.ForeignKeyConstraint(["candidate_id"], ["experiment_candidates.id"],
                                name="fk_experiment_records_candidate_id"),
        sa.PrimaryKeyConstraint("id"),
        # One fixed record per attempt, and one attempt per digest: a Run cannot have two
        # different records, and the same bytes cannot be fixed for two Runs.
        sa.UniqueConstraint("experiment_id", "run_id",
                            name="uq_experiment_records_run"),
        sa.UniqueConstraint("experiment_id", "record_sha256",
                            name="uq_experiment_records_digest"),
        sa.CheckConstraint("record_sha256 ~ '^[0-9a-f]{64}$'",
                           name="ck_experiment_records_sha256"),
        sa.CheckConstraint("char_length(btrim(registered_by)) > 0",
                           name="ck_experiment_records_registered_by"),
    )
    op.create_index("ix_experiment_records_candidate", "experiment_records",
                    ["experiment_id", "candidate_id", "task_id"])


    op.create_table(
        "experiment_reservations",
        sa.Column("id", sa.Uuid(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("experiment_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("candidate_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("request_key", sa.Uuid(as_uuid=True), nullable=False),
        # Which attempt the request belonged to, when it was a model call made by an Agent.
        # Nullable: a candidate-generation call has no Run.
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("agent_execution_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'reserved'"), nullable=False),
        # How far this request got: "reserved" means it provably never left this process,
        # "dispatched" means it may have reached the provider. Only the first may be released.
        sa.Column("stage", sa.Text(), server_default=sa.text("'reserved'"), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_tokens", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_by", sa.Text(), nullable=True),
        sa.Column("ended_reason", sa.Text(), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"],
                                name="fk_experiment_reservations_experiment_id"),
        sa.ForeignKeyConstraint(["candidate_id"], ["experiment_candidates.id"],
                                name="fk_experiment_reservations_candidate_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "request_key",
                            name="uq_experiment_reservations_request"),
        sa.CheckConstraint("tokens > 0", name="ck_experiment_reservations_tokens"),
        sa.CheckConstraint("state IN ('reserved', 'settled', 'released', 'unknown')",
                           name="ck_experiment_reservations_state"),
        sa.CheckConstraint("actual_tokens IS NULL OR actual_tokens >= 0",
                           name="ck_experiment_reservations_actual_tokens"),
        sa.CheckConstraint("(state = 'settled') = (actual_tokens IS NOT NULL)",
                           name="ck_experiment_reservations_settled_tokens"),
        sa.CheckConstraint("(state = 'settled') = (settled_at IS NOT NULL)",
                           name="ck_experiment_reservations_settled_at"),
        sa.CheckConstraint("stage IN ('reserved', 'dispatched')",
                           name="ck_experiment_reservations_stage"),
        sa.CheckConstraint("(stage = 'dispatched') = (dispatched_at IS NOT NULL)",
                           name="ck_experiment_reservations_dispatched_at"),
        sa.CheckConstraint("(state IN ('released', 'unknown')) = (ended_at IS NOT NULL)",
                           name="ck_experiment_reservations_ended_at"),
        sa.CheckConstraint("(ended_at IS NULL) = (ended_by IS NULL)",
                           name="ck_experiment_reservations_ended_by"),
        sa.CheckConstraint("(ended_at IS NULL) = (ended_reason IS NULL)",
                           name="ck_experiment_reservations_ended_reason"),
        sa.CheckConstraint(
            "ended_by IS NULL OR char_length(btrim(ended_by)) > 0",
            name="ck_experiment_reservations_ended_by_text"),
        sa.CheckConstraint(
            "ended_reason IS NULL OR char_length(btrim(ended_reason)) BETWEEN 1 AND 1000",
            name="ck_experiment_reservations_ended_reason_text"),
    )
    op.create_index("ix_experiment_reservations_experiment_id", "experiment_reservations",
                    ["experiment_id"])

    op.create_table(
        "experiment_task_results",
        sa.Column("id", sa.Uuid(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("experiment_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("candidate_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("task_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("recorded_by", sa.Text(), nullable=False),
        sa.Column("evidence_source", sa.Text(), server_default=sa.text("'declared'"),
                  nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("verification_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("deadline_context", sa.Text(), nullable=True),
        sa.Column("context_source", sa.Text(), nullable=True),
        sa.Column("context_evidence", sa.Text(), server_default=sa.text("'declared'"),
                  nullable=False),
        sa.Column("record_sha256", sa.Text(), nullable=True),
        sa.Column("record_provenance", sa.Text(), nullable=True),
        sa.Column("attempt_kind", sa.Text(), nullable=True),
        sa.Column("run_execution_sha256", sa.Text(), nullable=True),
        sa.Column("workspace_base_commit", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"],
                                name="fk_experiment_task_results_experiment_id"),
        sa.ForeignKeyConstraint(["candidate_id"], ["experiment_candidates.id"],
                                name="fk_experiment_task_results_candidate_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id", "task_id", "run_id",
                            name="uq_experiment_task_results_attempt"),
        # One Run belongs to at most one candidate: the same attempt may not be counted in
        # two arms of the same experiment.
        sa.UniqueConstraint("experiment_id", "run_id",
                            name="uq_experiment_task_results_run"),
        sa.CheckConstraint(
            "outcome IN ('formal_pass', 'failed', 'public_only_pass', 'incomplete', 'timeout')",
            name="ck_experiment_task_results_outcome"),
        sa.CheckConstraint("char_length(btrim(recorded_by)) > 0",
                           name="ck_experiment_task_results_recorded_by"),
        sa.CheckConstraint("evidence_source IN ('declared', 'classified')",
                           name="ck_experiment_task_results_evidence_source"),
        sa.CheckConstraint(
            "decision_reason IS NULL OR char_length(decision_reason) <= 1000",
            name="ck_experiment_task_results_decision_reason"),
        sa.CheckConstraint(
            "verification_id IS NULL OR evidence_source = 'classified'",
            name="ck_experiment_task_results_verification_source"),
        # A formal pass is only a formal pass when the classifier named the Verification it
        # loaded; a declared formal pass keeps this column NULL on purpose.
        sa.CheckConstraint(
            "(evidence_source = 'classified' AND outcome = 'formal_pass') "
            "= (verification_id IS NOT NULL)",
            name="ck_experiment_task_results_formal_verification"),
        # The classifier needs an attempt context. Only a classified result carries one, and
        # "unknown" is a recorded value rather than an omitted one: a caller may not leave the
        # deadline state implied, because the classifier checks it before the formal chain.
        sa.CheckConstraint(
            "(evidence_source = 'classified') = (deadline_context IS NOT NULL)",
            name="ck_experiment_task_results_deadline_context"),
        sa.CheckConstraint(
            "deadline_context IS NULL OR deadline_context IN ('fired', 'not_fired', 'unknown')",
            name="ck_experiment_task_results_deadline_context_value"),
        sa.CheckConstraint(
            "(evidence_source = 'classified') = (context_source IS NOT NULL)",
            name="ck_experiment_task_results_context_source"),
        sa.CheckConstraint(
            "context_source IS NULL "
            "OR char_length(btrim(context_source)) BETWEEN 1 AND 200",
            name="ck_experiment_task_results_context_source_text"),
        # Where the context came from: a persisted run record the classifier was checked
        # against, or the caller's statement. Only the record kind can be promotable, and it
        # must name the exact bytes it read.
        sa.CheckConstraint("context_evidence IN ('record', 'declared')",
                           name="ck_experiment_task_results_context_evidence"),
        sa.CheckConstraint(
            "record_sha256 IS NULL OR record_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_experiment_task_results_record_sha256"),
        sa.CheckConstraint(
            "(context_evidence = 'record') = (record_sha256 IS NOT NULL)",
            name="ck_experiment_task_results_record_binding"),
        # The kind of attempt the record describes. A rehearsal is evidence, but never a
        # promotion sample, so the row keeps the kind rather than re-deriving it later.
        sa.CheckConstraint(
            "attempt_kind IS NULL OR attempt_kind IN ('formal', 'rehearsal', 'unknown')",
            name="ck_experiment_task_results_attempt_kind"),
        sa.CheckConstraint(
            "(context_evidence = 'record') = (attempt_kind IS NOT NULL)",
            name="ck_experiment_task_results_attempt_kind_binding"),
        # The digest fixes the bytes; it does not attest who wrote them. The only value today
        # is a file the caller supplied, and saying so keeps it from reading as proof of a
        # trusted execution process.
        sa.CheckConstraint(
            "record_provenance IS NULL "
            "OR record_provenance IN ('caller_supplied', 'control_plane_registered')",
            name="ck_experiment_task_results_record_provenance"),
        sa.CheckConstraint(
            "(context_evidence = 'record') = (record_provenance IS NOT NULL)",
            name="ck_experiment_task_results_record_provenance_binding"),
        # The execution digest the Run recorded when this result was written. Frozen here so a
        # later change to the Run row cannot move an attempt into another candidate's record.
        sa.CheckConstraint(
            "run_execution_sha256 IS NULL OR run_execution_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_experiment_task_results_run_execution_sha256"),
        # The target-repository baseline the attempt's checkout actually verified, frozen when
        # the result is written. NULL means no verified checkout was on record.
        sa.CheckConstraint(
            "workspace_base_commit IS NULL "
            "OR workspace_base_commit ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'",
            name="ck_experiment_task_results_workspace_base_commit"),
    )
    op.create_index("ix_experiment_task_results_candidate_id", "experiment_task_results",
                    ["candidate_id"])


def downgrade() -> None:
    op.drop_index("ix_experiment_task_results_candidate_id",
                  table_name="experiment_task_results")
    op.drop_table("experiment_task_results")
    op.drop_index("ix_experiment_reservations_experiment_id",
                  table_name="experiment_reservations")
    op.drop_table("experiment_reservations")
    op.drop_index("ix_experiment_records_candidate", table_name="experiment_records")
    op.drop_table("experiment_records")
    op.drop_index("ix_experiment_candidates_experiment_id", table_name="experiment_candidates")
    op.drop_table("experiment_candidates")
    op.drop_table("experiment_tasks")
    op.drop_table("experiments")
