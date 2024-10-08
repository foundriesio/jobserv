"""empty message

Revision ID: bfe4b68df66e
Revises: 8c2f916d3b24
Create Date: 2024-09-20 10:00:10.309091

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'bfe4b68df66e'
down_revision = '8c2f916d3b24'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(sa.Column('allowed_host_tags_str', sa.Text(), nullable=True))

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('allowed_host_tags_str')

    # ### end Alembic commands ###
