"""product_aliases_and_glossary

Revision ID: e5b2c7a41d90
Revises: d3a1f0c9b7e2
"""
import sqlalchemy as sa
from alembic import op

revision = 'e5b2c7a41d90'
down_revision = 'd3a1f0c9b7e2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'product_aliases',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('product_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('folded', sa.String(length=200), nullable=False),
        sa.Column('created_by', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.ForeignKeyConstraint(['product_id'], ['products.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('product_id', 'folded'),
    )
    op.create_index(op.f('ix_product_aliases_product_id'), 'product_aliases', ['product_id'],
                    unique=False)
    op.create_table(
        'glossary_entries',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('term', sa.String(length=80), nullable=False),
        sa.Column('canonical', sa.String(length=80), nullable=False),
        sa.Column('created_by', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('term'),
    )


def downgrade():
    op.drop_table('glossary_entries')
    op.drop_index(op.f('ix_product_aliases_product_id'), table_name='product_aliases')
    op.drop_table('product_aliases')
