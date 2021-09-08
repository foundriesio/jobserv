"""empty message

Revision ID: 8c2f916d3b24
Revises: 3de1dc6abf74
Create Date: 2021-09-07 16:51:57.866563

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = '8c2f916d3b24'
down_revision = '3de1dc6abf74'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('builds', 'annotation', existing_type=mysql.TEXT, type_=mysql.MEDIUMTEXT)


def downgrade():
    op.alter_column('builds', 'annotation', existing_type=mysql.MEDIUMTEXT, type_=mysql.TEXT)
