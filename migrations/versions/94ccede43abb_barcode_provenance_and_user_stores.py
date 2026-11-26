"""barcode provenance and user stores

Revision ID: 94ccede43abb
Revises: 30e96313779f
"""
import sqlalchemy as sa
from alembic import op

revision = '94ccede43abb'
down_revision = '30e96313779f'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_stores",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("store_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id", "store_id"),
    )
    with op.batch_alter_table("products") as batch:  # batch mode: also works on SQLite
        batch.add_column(sa.Column("gtin_source", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("gtin_set_by", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("gtin_set_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key("fk_products_gtin_set_by_users", "users", ["gtin_set_by"], ["id"])


def downgrade():
    with op.batch_alter_table("products") as batch:
        batch.drop_constraint("fk_products_gtin_set_by_users", type_="foreignkey")
        batch.drop_column("gtin_set_at")
        batch.drop_column("gtin_set_by")
        batch.drop_column("gtin_source")
    op.drop_table("user_stores")
