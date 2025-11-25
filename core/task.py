import os
import asyncio
import itertools
from django.conf import settings
from celery import shared_task
from .models import Consulta, Resultado
from .bots.bot_configs import get_bot_configs
from .bots.bot_configs_contratista import get_bot_configs_contratista
from asgiref.sync import async_to_sync
import requests
import httpx
from time import perf_counter

async def run_bot(bot):
    try:
        # El bot ya guarda sus propios resultados en la BD
        await bot['func'](**bot['kwargs'])
    except Exception as e:
        # Guardar error o hacer log
        print(f"Error en bot {bot['func'].__name__}: {e}")
  
def chunked(iterable, size):
    """Divide un iterable en listas de tamaño 'size'."""
    it = iter(iterable)
    while True:
        chunk = list(itertools.islice(it, size))
        if not chunk:
            break
        yield chunk

@shared_task
def procesar_consulta(consulta_id, datos):

    async def run_bot(bot):
        try:
            await bot['func'](**bot['kwargs'])
        except Exception as e:
            print(f"Error en bot {bot['func'].__name__}: {e}")

    def chunked(iterable, size):
        it = iter(iterable)
        while True:
            chunk = list(itertools.islice(it, size))
            if not chunk:
                break
            yield chunk

    consulta = Consulta.objects.get(id=consulta_id)

    if not datos:
        # fallback por si algo falla
        from .consultar_registraduria import consultar_registraduria
        datos = async_to_sync(consultar_registraduria)(consulta.candidato.cedula)

    if not datos:
        consulta.estado = 'no_encontrado'
        consulta.save()
        return

    folder = os.path.join(settings.MEDIA_ROOT, 'resultados', str(consulta_id))
    os.makedirs(folder, exist_ok=True)

    datos.setdefault('rutas', {})

    bot_configs = get_bot_configs(consulta_id, datos)

    async def main_bots():
        # Corre en paralelo por lotes. Tamaño configurable vía env `BOT_BATCH_SIZE`.
        try:
            batch_size = int(os.environ.get('BOT_BATCH_SIZE', '10'))
        except Exception:
            batch_size = 10
        print(f"[task] Ejecutando bots en lotes de tamaño={batch_size}")
        for batch in chunked(bot_configs, batch_size):
            await asyncio.gather(*(run_bot(bot) for bot in batch))

    # Ejecutar bots (paralelo por lotes)
    async_to_sync(main_bots)()

    consulta.estado = 'completado'
    consulta.save()

    async def llamar_consolidado():
        headers = {
            "Authorization": "Token e48c48a21bbe510fadf2073ddc5e70c0a2db2827"
        }
        urls = [
            f"https://econfia.co/api/generar_consolidado/{consulta_id}/1/",
            f"https://econfia.co/api/generar_consolidado/{consulta_id}/3/",
        ]
        async with httpx.AsyncClient(timeout=9999) as client:
            results = await asyncio.gather(
                *(client.post(url, headers=headers) for url in urls),
                return_exceptions=True
            )
            for url, r in zip(urls, results):
                if isinstance(r, Exception):
                    print(f"❌ Error llamando a {url}: {r}")
                else:
                    try:
                        print(f"Consolidado generado en {url}: {r.json()}")
                    except Exception:
                        print(f"Consolidado generado en {url}: status={r.status_code}")

    try:
        async_to_sync(llamar_consolidado)()
    except Exception as e:
        print(f"Error general llamando a las APIs: {e}")
        
@shared_task
def procesar_consulta_por_nombres(consulta_id, datos, lista_nombres):
    async def run_bot(bot):
        try:
            await bot['func'](**bot['kwargs'])
        except Exception as e:
            print(f"Error en bot {bot['func'].__name__}: {e}")

    def chunked(iterable, size):
        it = iter(iterable)
        while True:
            chunk = list(itertools.islice(it, size))
            if not chunk:
                break
            yield chunk

    consulta = Consulta.objects.get(id=consulta_id)

    if not datos:
        # fallback por si algo falla
        from .consultar_registraduria import consultar_registraduria
        datos = async_to_sync(consultar_registraduria)(consulta.candidato.cedula)

    if not datos:
        consulta.estado = 'no_encontrado'
        consulta.save()
        return

    folder = os.path.join(settings.MEDIA_ROOT, 'resultados', str(consulta_id))
    os.makedirs(folder, exist_ok=True)

    datos.setdefault('rutas', {})

    # Traemos todos los bots
    bot_configs = get_bot_configs(consulta_id, datos)

    # Filtramos por lista de nombres
    bot_configs = [bot for bot in bot_configs if bot["name"] in lista_nombres]

    async def main_bots():
        for batch in chunked(bot_configs, 50):
            await asyncio.gather(*(run_bot(bot) for bot in batch))

    # Ejecutar solo los bots filtrados
    async_to_sync(main_bots)()

    consulta.estado = 'completado'
    consulta.save()

    # async def llamar_consolidado():
    #     headers = {
    #         "Authorization": "Token e48c48a21bbe510fadf2073ddc5e70c0a2db2827"
    #     }
    #     urls = [
    #         f"https://econfia.co/api/generar_consolidado/{consulta_id}/1/",
    #         f"https://econfia.co/api/generar_consolidado/{consulta_id}/3/",
    #     ]
    #     async with httpx.AsyncClient(timeout=9999) as client:
    #         results = await asyncio.gather(
    #             *(client.post(url, headers=headers) for url in urls),
    #             return_exceptions=True
    #         )
    #         for url, r in zip(urls, results):
    #             if isinstance(r, Exception):
    #                 print(f"Error llamando a {url}: {r}")
    #             else:
    #                 print(f"Consolidado generado en {url}: {r.json()}")

    # try:
    #     async_to_sync(llamar_consolidado)()
    # except Exception as e:
    #     print(f"Error general llamando a las APIs: {e}")

        
from celery import shared_task
import asyncio
import httpx
from asgiref.sync import async_to_sync
from django.db import transaction

from core.models import Resultado


@shared_task
def reintentar_bot(resultado_id):
    original = Resultado.objects.get(id=resultado_id)
    consulta = original.consulta
    candidato = consulta.candidato

    # ============================
    # 🔥 FIX 1 — Validar fuente antes de usarla
    # ============================
    if not original.fuente:
        original.estado = "fallido"
        original.mensaje = "El resultado no tiene fuente asignada. No se puede reintentar el bot."
        original.save()
        return original.mensaje

    nombre_fuente = (original.fuente.nombre or "").strip().lower()
    # ============================

    datos = {
        "cedula": candidato.cedula or "",
        "tipo_doc": candidato.tipo_doc or "",
        "nombre": candidato.nombre or "",
        "apellido": candidato.apellido or "",
        "fecha_nacimiento": (
            candidato.fecha_nacimiento.strftime("%Y-%m-%d")
            if getattr(candidato, "fecha_nacimiento", None) else ""
        ),
        "fecha_expedicion": (
            candidato.fecha_expedicion.strftime("%Y-%m-%d")
            if getattr(candidato, "fecha_expedicion", None) else ""
        ),
        "tipo_persona": getattr(candidato, "tipo_persona", "") or "",
        "sexo": getattr(candidato, "sexo", "") or "",
        "email": getattr(candidato, "email", "") or "",
        "error": ""
    }

    bot_configs = get_bot_configs(consulta.id, datos)

    # ============================
    # 🔥 FIX 2 — Comparación segura del nombre del bot
    # ============================
    bot = next((b for b in bot_configs
                if (b.get("name") or "").strip().lower() == nombre_fuente), None)
    # ============================

    # ============================
    # 🔥 FIX 3 — Soporte a bots contratistas (seguro si no existen)
    # ============================
    if not bot:
        try:
            alt_configs = get_bot_configs_contratista(consulta.id, datos)
        except NameError:
            alt_configs = []
        bot = next((b for b in alt_configs
                    if (b.get("name") or "").strip().lower() == nombre_fuente), None)
    # ============================

    # ============================
    # 🔥 FIX 4 — Manejo si no se encuentra config para este bot
    # ============================
    if not bot:
        original.estado = "fallido"
        original.mensaje = (
            f"No se encontró configuración para la fuente '{nombre_fuente}' "
            f"ni en get_bot_configs ni en get_bot_configs_contratista."
        )
        original.save()
        return original.mensaje
    # ============================

    existentes = set(
        Resultado.objects
        .filter(consulta=consulta, fuente=original.fuente)
        .values_list("id", flat=True)
    )

    mensaje_final = ""
    try:
        async_to_sync(bot["func"])(**(bot.get("kwargs") or {}))

        nuevos_qs = Resultado.objects.filter(
            consulta=consulta,
            fuente=original.fuente
        ).exclude(id__in=existentes)

        if nuevos_qs.exists():
            nuevo = nuevos_qs.last()
            with transaction.atomic():
                original.delete()
            mensaje_final = f"Se reintentó el bot {nuevo.fuente.nombre}. ID nuevo: {nuevo.id}"
        else:
            original.estado = "fallido"
            original.mensaje = "No se generó un nuevo resultado en el reintento."
            original.save()
            mensaje_final = f"No se creó un nuevo resultado en reintento para {nombre_fuente}"

    except Exception as e:
        original.estado = "fallido"
        original.mensaje = str(e)
        original.save()
        mensaje_final = f"Error al reintentar bot {nombre_fuente}: {e}"

    return mensaje_final


def _llamar_consolidado_sincrono(consulta_id: int):
    """Wrapper síncrono para lanzar las llamadas async a los consolidados."""
    try:
        async_to_sync(_llamar_consolidado_async)(consulta_id)
    except Exception as e:
        # No levantamos excepción para no romper la tarea; sólo logueamos.
        print(f"Error general llamando a las APIs de consolidado para consulta {consulta_id}: {e}")


async def _llamar_consolidado_async(consulta_id: int):
    headers = {
        # ⚠️ Considera mover este token a settings/variables de entorno
        "Authorization": "Token e48c48a21bbe510fadf2073ddc5e70c0a2db2827"
    }
    urls = [
        f"https://econfia.co/api/generar_consolidado/{consulta_id}/1/",
        f"https://econfia.co/api/generar_consolidado/{consulta_id}/3/",
    ]
    async with httpx.AsyncClient(timeout=9999) as client:
        results = await asyncio.gather(
            *(client.post(url, headers=headers) for url in urls),
            return_exceptions=True
        )
        for url, r in zip(urls, results):
            if isinstance(r, Exception):
                print(f"[Consolidado] Error llamando a {url}: {r}")
            else:
                # Evita reventar si no es JSON
                try:
                    print(f"[Consolidado] OK {url}: {r.json()}")
                except Exception:
                    print(f"[Consolidado] OK {url}: {r.status_code}")

@shared_task
def procesar_consulta_contratista_por_nombres(consulta_id, datos, lista_nombres):
    async def run_bot(bot):
        try:
            await bot["func"](**bot["kwargs"])
        except Exception as e:
            print(f"Error en bot {bot['func'].__name__}: {e}")

    def chunked(iterable, size):
        it = iter(iterable)
        while True:
            chunk = list(itertools.islice(it, size))
            if not chunk:
                break
            yield chunk

    consulta = Consulta.objects.get(id=consulta_id)

    # Fallback si no recibimos datos
    if not datos:
        from .consultar_registraduria import consultar_registraduria
        datos = async_to_sync(consultar_registraduria)(consulta.candidato.cedula)

    if not datos:
        consulta.estado = "no_encontrado"
        consulta.save()
        return

    # Asegurar carpeta de salida
    folder = os.path.join(settings.MEDIA_ROOT, "resultados", str(consulta_id))
    os.makedirs(folder, exist_ok=True)

    datos.setdefault("rutas", {})

    # 1) Traer bots desde la factory de CONTRATISTA
    bot_configs = get_bot_configs_contratista(consulta_id, datos)

    # 2) Filtrar por nombres solicitados
    if lista_nombres:
        bot_configs = [b for b in bot_configs if b["name"] in lista_nombres]

    # 3) Ejecutar en lotes (concurrency control)
    async def main_bots():
        for batch in chunked(bot_configs, 50):
            await asyncio.gather(*(run_bot(b) for b in batch))

    async_to_sync(main_bots)()

    # 4) Marcar consulta como completada
    consulta.estado = "completado"
    consulta.save()
