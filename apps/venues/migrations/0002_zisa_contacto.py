from django.db import migrations

ADDRESS = "Avenida El Golf 591, Trujillo, La Libertad 13009"
PHONE = "+51 970 183 691"


def forwards(apps, schema_editor):
    apps.get_model("venues", "Venue").objects.filter(name="Zisa").update(
        address=ADDRESS, phone=PHONE,
    )


class Migration(migrations.Migration):

    dependencies = [("venues", "0001_initial")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
