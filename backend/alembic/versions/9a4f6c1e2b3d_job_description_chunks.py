"""job description chunks

Revision ID: 9a4f6c1e2b3d
Revises: 7c3d2a9b5e10
Create Date: 2026-11-10
"""

from typing import Sequence, Union

from alembic import op

revision: str = "9a4f6c1e2b3d"
down_revision: Union[str, Sequence[str], None] = "7c3d2a9b5e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Needs a role allowed to create extensions: the image's superuser with the
    # compose files, the master user on RDS.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        CREATE TABLE job_description_chunks (
            id UUID NOT NULL,
            session_id UUID NOT NULL,
            ordinal INTEGER NOT NULL,
            content TEXT NOT NULL,
            embedding vector NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_job_description_chunks_session_ordinal UNIQUE (session_id, ordinal),
            FOREIGN KEY (session_id) REFERENCES interview_sessions (id) ON DELETE CASCADE
        )
        """
    )


def downgrade() -> None:
    op.drop_table("job_description_chunks")
    # The extension stays: dropping it would break anything else that uses it.
