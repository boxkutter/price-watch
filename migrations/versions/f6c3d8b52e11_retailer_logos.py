"""retailer_logos

Revision ID: f6c3d8b52e11
Revises: e5b2c7a41d90
"""
import sqlalchemy as sa
from alembic import op

revision = 'f6c3d8b52e11'
down_revision = 'e5b2c7a41d90'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'retailer_logos',
        sa.Column('retailer_id', sa.String(length=40), nullable=False),
        sa.Column('content_type', sa.String(length=30), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('width', sa.Integer(), nullable=False),
        sa.Column('height', sa.Integer(), nullable=False),
        sa.Column('updated_by', sa.String(length=36), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id']),
        sa.PrimaryKeyConstraint('retailer_id'),
    )


def downgrade():
    op.drop_table('retailer_logos')
