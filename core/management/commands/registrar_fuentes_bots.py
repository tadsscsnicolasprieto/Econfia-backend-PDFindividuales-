from django.core.management.base import BaseCommand
from core.scripts.fuentes_bots import FUENTES_BOTS
from core.scripts.registrar_fuentes import registrar_fuentes_si_faltan

class Command(BaseCommand):
    help = 'Registra automáticamente todas las fuentes usadas por los bots.'

    def handle(self, *args, **options):
        registrar_fuentes_si_faltan(FUENTES_BOTS)
        self.stdout.write(self.style.SUCCESS('Fuentes registradas correctamente.'))
