"""add marketplace block checkpoints

Revision ID: 4d7e9a1b2c3f
Revises: 3c91b2e7f604
Create Date: 2026-07-25 10:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '4d7e9a1b2c3f'
down_revision = '3c91b2e7f604'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'marketplace_block_checkpoints',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('network', sa.String(length=20), nullable=False),
        sa.Column('block_height', sa.Integer(), nullable=False),
        sa.Column('block_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'network',
            'block_height',
            name='uq_market_block_checkpoint_network_height',
        ),
    )
    with op.batch_alter_table('marketplace_block_checkpoints', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_marketplace_block_checkpoints_block_hash'),
            ['block_hash'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_marketplace_block_checkpoints_block_height'),
            ['block_height'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_marketplace_block_checkpoints_network'),
            ['network'],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table('marketplace_block_checkpoints', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_marketplace_block_checkpoints_network'))
        batch_op.drop_index(batch_op.f('ix_marketplace_block_checkpoints_block_height'))
        batch_op.drop_index(batch_op.f('ix_marketplace_block_checkpoints_block_hash'))
    op.drop_table('marketplace_block_checkpoints')
