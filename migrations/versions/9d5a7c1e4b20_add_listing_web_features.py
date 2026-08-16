"""add listing web feature controls

Revision ID: 9d5a7c1e4b20
Revises: 4d7e9a1b2c3f
Create Date: 2026-08-16 16:15:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '9d5a7c1e4b20'
down_revision = '4d7e9a1b2c3f'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('listings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('featured_web', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('featured_rank', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('featured_label', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('featured_starts_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('featured_ends_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('featured_admin_note', sa.Text(), nullable=True))
        batch_op.create_index(batch_op.f('ix_listings_featured_web'), ['featured_web'], unique=False)

    with op.batch_alter_table('listings', schema=None) as batch_op:
        batch_op.alter_column('featured_web', server_default=None)
        batch_op.alter_column('featured_rank', server_default=None)


def downgrade():
    with op.batch_alter_table('listings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_listings_featured_web'))
        batch_op.drop_column('featured_admin_note')
        batch_op.drop_column('featured_ends_at')
        batch_op.drop_column('featured_starts_at')
        batch_op.drop_column('featured_label')
        batch_op.drop_column('featured_rank')
        batch_op.drop_column('featured_web')
