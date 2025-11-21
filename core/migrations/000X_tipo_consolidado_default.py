from django.db import migrations

def crear_tipo_consolidado(apps, schema_editor):
    TipoConsolidado = apps.get_model('core', 'TipoConsolidado')
    if not TipoConsolidado.objects.filter(id=1).exists():
        TipoConsolidado.objects.create(id=1, nombre='Consolidado General')

class Migration(migrations.Migration):
    dependencies = [
        ('core', '0001_initial'), # Cambia esto por el nombre de tu última migración
    ]
    operations = [
        migrations.RunPython(crear_tipo_consolidado),
    ]
