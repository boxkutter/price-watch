"""job_progress

Revision ID: g7d4e9c63f22
Revises: f6c3d8b52e11
"""
import sqlalchemy as sa
from alembic import op

revision = 'g7d4e9c63f22'
down_revision = 'f6c3d8b52e11'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'job_progress',
        sa.Column('job_id', sa.String(length=36), nullable=False),
        sa.Column('text', sa.String(length=200), nullable=False),
        sa.Column('done', sa.Integer(), nullable=False),
        sa.Column('total', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('job_id'),
    )
    op.create_index(op.f('ix_job_progress_updated_at'), 'job_progress', ['updated_at'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_job_progress_updated_at'), table_name='job_progress')
    op.drop_table('job_progress')
