"""Create durable jobs, attempts, and scheduler leases.

Revision ID: 0006_market_morning_jobs
Revises: 0005_market_morning_editions
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0006_market_morning_jobs"
down_revision: str | None = "0005_market_morning_editions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "mm_jobs",
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("job_type", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("payload", mysql.JSON(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("available_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", mysql.DATETIME(fsp=6)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("completed_at", mysql.DATETIME(fsp=6)),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_mm_job_idempotency_key"),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_mm_job_claim",
        "mm_jobs",
        ["status", "available_at", "priority", "created_at"],
    )
    op.create_index(
        "ix_mm_job_expired_lease",
        "mm_jobs",
        ["status", "lease_expires_at"],
    )

    op.create_table(
        "mm_job_attempts",
        sa.Column("attempt_id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("finished_at", mysql.DATETIME(fsp=6)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("details", mysql.JSON()),
        sa.ForeignKeyConstraint(["job_id"], ["mm_jobs.job_id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "job_id",
            "attempt_number",
            name="uq_mm_job_attempt_number",
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_mm_job_attempt_status",
        "mm_job_attempts",
        ["job_id", "status", "started_at"],
    )

    op.create_table(
        "mm_scheduler_leases",
        sa.Column("lease_name", sa.String(128), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("lease_until", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("heartbeat_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("acquired_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        **_TABLE_OPTIONS,
    )


def downgrade() -> None:
    op.drop_table("mm_scheduler_leases")
    op.drop_table("mm_job_attempts")
    op.drop_table("mm_jobs")
