"""chain_membership

Revision ID: h8e5f0d74a33
Revises: g7d4e9c63f22
"""
import sqlalchemy as sa
from alembic import op

revision = 'h8e5f0d74a33'
down_revision = 'g7d4e9c63f22'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user_memberships',
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('retailer_id', sa.String(length=40), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('user_id', 'retailer_id'),
    )


def downgrade():
    op.drop_table('user_memberships')
