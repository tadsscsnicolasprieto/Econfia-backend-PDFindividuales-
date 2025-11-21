from core.models import Fuente, TipoFuente
from django.db import transaction

def registrar_fuentes_si_faltan(lista_nombres_fuentes, tipo_nombre_default="General"):
    """
    Crea las fuentes que no existan en la base de datos, usando un TipoFuente genérico si es necesario.
    Imprime en consola si la fuente fue creada o ya existía.
    """
    with transaction.atomic():
        tipo, _ = TipoFuente.objects.get_or_create(
            nombre=tipo_nombre_default,
            defaults={"peso": 1, "probabilidad": 1}
        )
        for nombre in lista_nombres_fuentes:
            fuente, creada = Fuente.objects.get_or_create(
                nombre=nombre,
                defaults={"tipo": tipo, "nombre_pila": nombre}
            )
            if creada:
                print(f"Fuente creada: {nombre}")
            else:
                print(f"Fuente ya existe: {nombre}")
