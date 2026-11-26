"""search_jobs

Revision ID: 4f8ecdecdd71
Revises: a8532f7105f8
"""
import sqlalchemy as sa
from alembic import op

revision = '4f8ecdecdd71'
down_revision = 'a8532f7105f8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('owner_id', sa.String(length=36), nullable=True))
        batch.add_column(sa.Column('payload', sa.JSON(), nullable=True))
        batch.create_foreign_key('fk_jobs_owner_id_users', 'users', ['owner_id'], ['id'])
    op.create_index(op.f('ix_jobs_owner_id'), 'jobs', ['owner_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_jobs_owner_id'), table_name='jobs')
    with op.batch_alter_table('jobs') as batch:
        batch.drop_constraint('fk_jobs_owner_id_users', type_='foreignkey')
        batch.drop_column('payload')
        batch.drop_column('owner_id')
