"""reference_products_and_images

Revision ID: d3a1f0c9b7e2
Revises: 94ccede43abb
"""
import sqlalchemy as sa
from alembic import op

revision = 'd3a1f0c9b7e2'
down_revision = '94ccede43abb'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'reference_products',
        sa.Column('gtin14', sa.String(length=14), nullable=False),
        sa.Column('source', sa.String(length=30), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('name', sa.String(length=300), nullable=True),
        sa.Column('brand', sa.String(length=300), nullable=True),
        sa.Column('quantity_text', sa.String(length=100), nullable=True),
        sa.Column('page_url', sa.String(length=500), nullable=True),
        sa.Column('image_url', sa.String(length=500), nullable=True),
        sa.Column('details', sa.JSON(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('next_check_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('gtin14'),
    )
    op.create_index(op.f('ix_reference_products_next_check_at'), 'reference_products',
                    ['next_check_at'], unique=False)
    op.create_table(
        'product_images',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('product_id', sa.String(length=36), nullable=False),
        sa.Column('owner_id', sa.String(length=36), nullable=True),
        sa.Column('source', sa.String(length=12), nullable=False),
        sa.Column('content_type', sa.String(length=30), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('width', sa.Integer(), nullable=False),
        sa.Column('height', sa.Integer(), nullable=False),
        sa.Column('credit', sa.String(length=300), nullable=True),
        sa.Column('source_url', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id']),
        sa.ForeignKeyConstraint(['product_id'], ['products.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_product_images_owner_id'), 'product_images', ['owner_id'], unique=False)
    op.create_index(op.f('ix_product_images_product_id'), 'product_images', ['product_id'],
                    unique=False)


def downgrade():
    op.drop_index(op.f('ix_product_images_product_id'), table_name='product_images')
    op.drop_index(op.f('ix_product_images_owner_id'), table_name='product_images')
    op.drop_table('product_images')
    op.drop_index(op.f('ix_reference_products_next_check_at'), table_name='reference_products')
    op.drop_table('reference_products')
