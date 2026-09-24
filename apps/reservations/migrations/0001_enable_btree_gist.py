"""
Habilita btree_gist antes de crear la restriccion de exclusion.

La ExclusionConstraint de TableOccupancy combina un operador de igualdad
(sobre table_id, un uuid) con uno de solapamiento (sobre un tstzrange).
Un indice GiST solo sabe indexar el rango; btree_gist es lo que le permite
incluir tambien la columna del uuid.
"""

from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        BtreeGistExtension(),
    ]
