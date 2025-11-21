from django.http import JsonResponse
from .models import Consulta, Resultado, Candidato, Fuente
from .task import procesar_consulta, reintentar_bot, procesar_consulta_por_nombres, procesar_consulta_contratista_por_nombres
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework import status
from rest_framework.decorators import api_view
from .serializers import UserSerializer, FuenteSerializer
from django.shortcuts import get_object_or_404
from rest_framework.authtoken.models import Token
from django.contrib.auth import authenticate
from django.shortcuts import get_object_or_404
from django.db.models import Avg, Count
from django.db.models import Max
from django.http import FileResponse
from .models import Resultado, Consulta, Perfil
from .utils.pdf_generator import generar_pdf_consolidado
from django.views.decorators.http import require_GET
from decimal import Decimal
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from .serializers import ConsultaDetalleSerializer, ResultadoSerializer
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from asgiref.sync import async_to_sync
from .consultar_registraduria import consultar_registraduria
from django.template.loader import render_to_string
from django.http import HttpResponse
from weasyprint import HTML
import base64
import matplotlib.pyplot as plt
import numpy as np
import io, os
from django.conf import settings
from django.http import FileResponse, Http404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from PyPDF2 import PdfMerger
from PIL import Image
import traceback
from .models import Resultado
from django.core.files.base import ContentFile
from .adres_bio import consultar_adres_bio
from .procuraduria_bio import procuraduria_bio
from .policia_bio import consultar_policia_nacional
import asyncio
from django.utils.encoding import force_bytes, force_str
from django.core.mail import send_mail
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from rest_framework.authentication import get_authorization_header

# auth/views.py
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.urls import reverse
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status


import unicodedata
import re



def _norm(s: str) -> str:
    if not s:
        return ""
    s = str(s).strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s)

def uniq_preserve(iterable):
    seen = set()
    out = []
    for x in iterable or []:
        if x and x not in seen:
            out.append(x)
            seen.add(x)
    return out

# Mapa de profesiones y bots a ejecutar
import re


def bots_por_profesion(profesion: str) -> list[str]:
    p = _norm(profesion)
    sugeridos = []
    for rx, bots in PROFESION_BOT_MAP:
        if rx.search(p):
            sugeridos.extend(bots)
    return uniq_preserve(sugeridos)


User = get_user_model()

def enviar_email_reset(user, reset_link):
    subject = "Restablecer tu contraseña"
    html = f"""
    <p>Hola {user.get_full_name() or user.username},</p>
    <p>Para restablecer tu contraseña haz clic aquí:</p>
    <p><a href="{reset_link}">{reset_link}</a></p>
    <p>Si no fuiste tú, ignora este correo.</p>
    """
    msg = EmailMultiAlternatives(subject, html, settings.DEFAULT_FROM_EMAIL, [user.email])
    msg.attach_alternative(html, "text/html")
    msg.send()

@api_view(["POST"])
@permission_classes([AllowAny])
def password_reset_request(request):
    email = (request.data.get("email") or "").strip().lower()
    try:
        user = User.objects.get(email__iexact=email, is_active=True)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)

        # URL del FRONT que mostrará el form de nueva contraseña
        front_url = getattr(settings, "FRONTEND_RESET_URL", "http://localhost:3000/reset")
        reset_link = f"{front_url}?uid={uid}&token={token}"

        enviar_email_reset(user, reset_link)
    except User.DoesNotExist:
        pass  # Siempre 200, no reveles si existe
    return Response({"detail": "Si el correo existe, enviamos instrucciones."}, status=status.HTTP_200_OK)

@api_view(["POST"])
@permission_classes([AllowAny])
def password_reset_confirm(request):
    uid = request.data.get("uid")
    token = request.data.get("token")
    new_password = request.data.get("new_password")

    if not (uid and token and new_password):
        return Response({"detail": "Datos incompletos."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        uid_int = force_str(urlsafe_base64_decode(uid))
        user = User.objects.get(pk=uid_int, is_active=True)
    except Exception:
        return Response({"detail": "Token inválido."}, status=status.HTTP_400_BAD_REQUEST)

    if not default_token_generator.check_token(user, token):
        return Response({"detail": "Token inválido o expirado."}, status=status.HTTP_400_BAD_REQUEST)

    user.set_password(new_password)
    if hasattr(user, "token_version"):
        user.token_version = (user.token_version or 0) + 1  # invalida JWT antiguos si usas esto
    user.save()

    return Response({"detail": "Contraseña actualizada correctamente."}, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([AllowAny])
def login(request):
    username = request.data.get('username')
    password = request.data.get('password')

    user = authenticate(username=username, password=password)

    if user is None:
        return Response(
            {"error": "Invalid username or password"},
            status=status.HTTP_400_BAD_REQUEST
        )

    token, created = Token.objects.get_or_create(user=user)
    return Response(
        {"token": token.key, "user": UserSerializer(user).data},
        status=status.HTTP_200_OK
    )

from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags


def enviar_email_activacion(user, activation_link):
    subject = "Activa tu cuenta en Econfia"
    from_email = settings.DEFAULT_FROM_EMAIL
    to = [user.email]

    # Renderizar plantilla HTML
    html_content = render_to_string("emails/activation_email.html", {
        "user": user,
        "activation_link": activation_link,
    })
    text_content = strip_tags(html_content)

    email = EmailMultiAlternatives(subject, text_content, from_email, to)
    email.attach_alternative(html_content, "text/html")
    email.send()


@api_view(["POST"])
@permission_classes([AllowAny])
def register(request):
    serializer = UserSerializer(data=request.data)
    if serializer.is_valid():
        # Crear usuario inactivo
        user = User(
            username=serializer.validated_data["username"],
            email=serializer.validated_data["email"],
            first_name=serializer.validated_data["first_name"],
            last_name=serializer.validated_data["last_name"],
            is_active=False,
        )
        user.set_password(serializer.validated_data["password"])
        user.save()

        # Generar token de activación
        token = default_token_generator.make_token(user)
        uid = urlsafe_base64_encode(force_bytes(user.pk))

        activation_link = f"{request.build_absolute_uri('/')}api/activar/{uid}/{token}/"

        # Enviar correo con diseño HTML
        enviar_email_activacion(user, activation_link)

        return Response(
            {"message": "Usuario creado. Revisa tu correo para activar la cuenta."},
            status=status.HTTP_201_CREATED,
        )

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

@api_view(["GET"])
@permission_classes([AllowAny])
def activate(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return Response({"error": "Token inválido"}, status=status.HTTP_400_BAD_REQUEST)

    if default_token_generator.check_token(user, token):
        if not user.is_active:
            user.is_active = True
            user.save()

            # Crear perfil asociado
            Perfil.objects.create(usuario=user)

            # Crear token de autenticación (DRF Token)
            drf_token, _ = Token.objects.get_or_create(user=user)

            return Response({
                "message": "Cuenta activada correctamente",
                "token": drf_token.key,
                "user": {
                    "username": user.username,
                    "email": user.email,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                }
            }, status=status.HTTP_200_OK)
        else:
            return Response({"message": "La cuenta ya estaba activa"}, status=status.HTTP_200_OK)

    return Response({"error": "Token inválido o expirado"}, status=status.HTTP_400_BAD_REQUEST)


from asgiref.sync import async_to_sync
import asyncio


def _extraer_token_strict(request):
    """
    Lee el header Authorization y retorna la clave del token.
    Formato esperado: "Token <key>"
    """
    auth = get_authorization_header(request).decode("utf-8").strip()
    if not auth:
        return None, "Falta header Authorization"
    parts = auth.split()
    if len(parts) != 2 or parts[0] != "Token":
        return None, "Formato de Authorization inválido. Usa: 'Token <clave>'"
    return parts[1], None

def _resolver_usuario_por_token(token_key: str):
    """
    Retorna (user, None) si el token es válido; en caso de error, (None, razon).
    """
    try:
        t = Token.objects.select_related("user").get(key=token_key)
        return t.user, None
    except Token.DoesNotExist:
        return None, "Token inválido o no encontrado"


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_mi_candidato(request):

    token_key, err = _extraer_token_strict(request)
    if err:
        return Response({"error": err}, status=status.HTTP_401_UNAUTHORIZED)

    token_user, err = _resolver_usuario_por_token(token_key)
    if err:
        return Response({"error": err}, status=status.HTTP_401_UNAUTHORIZED)

    if request.user.id != token_user.id:
        return Response(
            {"error": "Token no corresponde al usuario autenticado"},
            status=status.HTTP_401_UNAUTHORIZED
        )

    perfil = getattr(token_user, "perfil", None)
    if not perfil:
        return Response({"error": "Perfil de usuario no encontrado"}, status=status.HTTP_400_BAD_REQUEST)

    candidato = getattr(perfil, "candidato", None)
    if not candidato:
        return Response({
            "token_de": token_user.username,
            "plan": perfil.plan,
            "tiene_candidato": False,
            "candidato": None,
        }, status=status.HTTP_200_OK)

    datos_candidato = {
        "cedula": getattr(candidato, "cedula", ""),
        "tipo_doc": getattr(candidato, "tipo_doc", ""),
        "nombre": getattr(candidato, "nombre", ""),
        "apellido": getattr(candidato, "apellido", ""),
        "fecha_nacimiento": getattr(candidato, "fecha_nacimiento", None),
        "fecha_expedicion": getattr(candidato, "fecha_expedicion", None),
        "tipo_persona": getattr(candidato, "tipo_persona", ""),
        "sexo": getattr(candidato, "sexo", ""),
        "email":getattr(candidato, "email", ""),
        "profesion":getattr(candidato, "profesion", ""),
    }

    return Response({
        "token_de": token_user.username,
        "plan": perfil.plan,
        "tiene_candidato": True,
        "candidato": datos_candidato,
    }, status=status.HTTP_200_OK)

BOTS_PREMIUM_FIJOS = ['policia_nacional',
                    'rnmc',
                    'inhabilidades',
                    'libreta_militar',
                    'personeria',
                    'contraloria',
                    'procuraduria_certificado',
                    "ruaf",
                    'secop_consulta_aacs',
                    'colpensiones_rpm',
                    'porvenir_cert_afiliacion',
                    'banco_proveedores_consulta_estados']
PROFESION_BOT_MAP = [
    # ABOGACÍA (SIRNA: Registro Nacional de Abogados)  — valor sugerido: "abogado(a)"
    (re.compile(r"\babogad[oa]s?\b", re.I),
     ["sirna_inscritos_png", "rama_abogado_certificado"]),

    # ECONOMÍA (CONALPE)  — valor sugerido: "economista(s)"
    (re.compile(r"\beconomistas?\b", re.I),
     ["conalpe_consulta_inscritos", "conalpe_certificado"]),

    # PSICOLOGÍA (COLPSIC)  — valor sugerido: "psicólogo(a)"
    # (re.compile(r"\bpsic(?:ó|o)log[oa]s?\b", re.I),
    #  ["colpsic_verificacion_tarjetas", "colpsic_validar_documento"]),

    # BACTERIOLOGÍA (CNB)  — valor sugerido: "bacteriólogo(a)"
    # (re.compile(r"\bbacteriol(?:ó|o)g[oa]s?\b", re.I),
    #  ["cnb_carnet_afiliacion", "cnb_consulta_matriculados"]),

    # BIOLOGÍA (Consejo Profesional de Biología)  — valor sugerido: "biólogo(a)"
    # (re.compile(r"\bbiol(?:ó|o)g[oa]s?\b", re.I),
    #  ["biologia_consulta", "biologia_validacion_certificados"]),

    # QUÍMICA (CPQCOL)  — valor sugerido: "químico(a)"
    # (re.compile(r"\bqu(?:í|i)mic[oa]s?\b", re.I),
    #  ["cpqcol_verificar", "cpqcol_antecedentes"]),

    # INGENIERÍA QUÍMICA (CPIQ)  — valor sugerido: "ingeniero(a) químico(a)"
    # (re.compile(r"\bingenier[oa]s?\s+qu(?:í|i)mic[oa]s?\b", re.I),
    #  ["cpiq_validacion_matricula", "cpiq_validacion_tarjeta",
    #   "cpiq_certificado_vigencia", "cpiq_validacion_certificado_vigencia"]),

    # INGENIERÍA DE PETRÓLEOS (CPIP)  — valor sugerido: "ingeniero(a) de petróleos"
    # (re.compile(r"\bingenier[oa]s?\s+de\s+petr(?:ó|o)leos?\b", re.I),
    #  ["cpip_verif_matricula"]),

    # TOPOGRAFÍA (CPNT)  — valor sugerido: "topógrafo(a)"
    # (re.compile(r"\btop(?:ó|o)graf[oa]s?\b", re.I),
    #  ["cpnt_vigenciapdf", "cpnt_vigencia_externa_form", "cpnt_consulta_licencia"]),

    # ARQUITECTURA (CPNAA)  — valor sugerido: "arquitecto(a)"
    # (re.compile(r"\barquitect[oa]s?\b", re.I),
    #  ["cpnaa_matricula_arquitecto", "cpnaa_certificado_vigencia"]),

    # TECNÓLOGOS (CONALTEL)  — valor sugerido: "tecnólogo(a)"
    # (re.compile(r"\btecn(?:ó|o)log[oa]s?\b", re.I),
    #  ["conaltel_consulta_matriculados"]),

    # TÉCNICOS ELECTRICISTAS (CONTE)  — valor sugerido: "técnico(a) electricista"
    # (re.compile(r"\bt[ée]cnic[oa]s?\s+electricist[ae]s?\b", re.I),
    #  ["conte_consulta_matricula", "conte_consulta_vigencia"]),

    # INGENIERÍAS VARIAS (Consejo Profesional Nacional) — valor sugerido: "ingeniero(a)"
    # Nota: comparte patrón con el fallback; deja este antes para que tenga prioridad.
    # (re.compile(r"\bingenier[oa]s?\b", re.I),
    #  ["cp_validar_matricula", "cp_validar_certificado", "cp_certificado_busqueda", "colelectro_directorio"]),

    # INGENIERÍA (genérico/fallback) → COPNIA  — valor sugerido: "ingeniero(a)"
    (re.compile(r"\bingenier[oa]s?\b", re.I),
     ["copnia_certificado"]),

    # ADMINISTRACIÓN DE EMPRESAS / NEGOCIOS (CPAE)  — valor sugerido: "administrador(a) de empresas"
    (re.compile(r"\badministrador[ae]s?\s+de\s+empresas?\b", re.I),
     ["cpae_certificado"]),
     
    # ADMINISTRACIÓN PUBLICO (CPAA)  — valor sugerido: "administrador(a) publico"
    (re.compile(r"\badministrador[ae]s?\s+publico(?:es)?\b", re.I),
     ["ccap_validate_identity"]),

    # ADMINISTRACIÓN AMBIENTAL (CPAA)  — valor sugerido: "administrador(a) ambiental"
    (re.compile(r"\badministrador[ae]s?\s+ambiental(?:es)?\b", re.I),
     ["cpaa_generar_certificado"]),

    # CONTADURÍA (CONPUCOL)  — valor sugerido: "contador(a)"
    # (re.compile(r"\bcontador[ae]s?\b", re.I),
    #  ["conpucol_verificacion_colegiados", "conpucol_certificados"]),
]

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_consultar(request):
    token_key, err = _extraer_token_strict(request)
    if err:
        return Response({"error": err}, status=status.HTTP_401_UNAUTHORIZED)

    token_user, err = _resolver_usuario_por_token(token_key)
    if err:
        return Response({"error": err}, status=status.HTTP_401_UNAUTHORIZED)

    if request.user.id != token_user.id:
        return Response(
            {"error": "Token no corresponde al usuario autenticado"},
            status=status.HTTP_401_UNAUTHORIZED
        )

    duenio_token = token_user.username

    # ---------------------------------------------
    # Parámetros requeridos / opcionales (NUEVO)
    # ---------------------------------------------
    cedula_raw = request.data.get("cedula")
    if cedula_raw is None:
        return Response({"error": "Falta el campo cédula"}, status=status.HTTP_400_BAD_REQUEST)

    cedula = str(cedula_raw).strip()
    tipo_doc_req = request.data.get("tipo_doc")
    fecha_expedicion_req = request.data.get("fecha_expedicion")
    lista_nombres = request.data.get("lista_nombres")

    # parámetros opcionales que activan lógica de contratista (NUEVO)
    email_param = (request.data.get("email") or "").strip()
    profesion_param = (request.data.get("profesion") or "").strip()
    activar_contratista_por_param = bool(email_param and profesion_param)

    perfil = getattr(token_user, "perfil", None)
    if not perfil:
        return Response({"error": "Perfil de usuario no encontrado"}, status=status.HTTP_400_BAD_REQUEST)

    if (perfil.consultas_disponibles or 0) <= 0:
        return Response({"error": "No tienes consultas disponibles"}, status=status.HTTP_403_FORBIDDEN)

    # ---------------------------------------------
    # Ya NO depende del plan. Ahora depende de los parámetros (NUEVO)
    # es_contratista = (perfil.plan or "").lower() == "contratista"
    es_contratista = activar_contratista_por_param
    # ---------------------------------------------

    try:
        candidato = Candidato.objects.filter(cedula=cedula).first()
        if candidato:
            # Si llegaron email/profesion y el candidato no los tiene, los actualizamos (NUEVO)
            campos_a_guardar = []
            if email_param and not (candidato.email or "").strip():
                candidato.email = email_param
                campos_a_guardar.append("email")
            if profesion_param and not (candidato.profesion or "").strip():
                candidato.profesion = profesion_param
                campos_a_guardar.append("profesion")
            if campos_a_guardar:
                candidato.save(update_fields=campos_a_guardar)

            datos = {
                "cedula": candidato.cedula,
                "tipo_doc": candidato.tipo_doc,
                "nombre": candidato.nombre,
                "apellido": candidato.apellido,
                "fecha_nacimiento": candidato.fecha_nacimiento,
                "fecha_expedicion": candidato.fecha_expedicion,
                "tipo_persona": candidato.tipo_persona,
                "sexo": candidato.sexo,
                "email": candidato.email,    
                "profesion": candidato.profesion,
            }
            if fecha_expedicion_req:
                datos["fecha_expedicion"] = fecha_expedicion_req
            estado = "en_proceso"
        else:
            # Construcción inicial de datos para crear al candidato
            async def obtener_datos():
                print("🚀 Lanzando tareas para bots base (solo para construir Candidato)...")
                coros = []

                async def with_timeout(coro, t=50):
                    try:
                        return await asyncio.wait_for(coro, timeout=t)
                    except asyncio.TimeoutError:
                        print("Timeout individual de bot")
                        return {}

                if tipo_doc_req:
                    coros += [
                        with_timeout(procuraduria_bio(cedula, tipo_doc_req), 50),
                        with_timeout(consultar_policia_nacional(cedula, tipo_doc_req), 50),
                        with_timeout(consultar_adres_bio(cedula, tipo_doc_req), 50),
                    ]
                coros.append(with_timeout(consultar_registraduria(cedula), 50))

                tareas = [asyncio.create_task(c) for c in coros]
                GLOBAL_TIMEOUT = 60
                loop = asyncio.get_running_loop()
                inicio = loop.time()

                try:
                    while tareas:
                        restante = GLOBAL_TIMEOUT - (loop.time() - inicio)
                        if restante <= 0:
                            for t in tareas:
                                t.cancel()
                            await asyncio.gather(*tareas, return_exceptions=True)
                            return {}

                        espera = min(10, max(1, int(restante)))
                        done, pending = await asyncio.wait(
                            tareas,
                            return_when=asyncio.FIRST_COMPLETED,
                            timeout=espera
                        )

                        if not done:
                            tareas = list(pending)
                            continue

                        for t in done:
                            try:
                                r = t.result()
                            except Exception:
                                continue

                            datos = r.get("datos", r) if isinstance(r, dict) else {}
                            nombre = (datos.get("nombre") or "").strip()
                            apellido = (datos.get("apellido") or "").strip()
                            if nombre and apellido:
                                for p in pending:
                                    p.cancel()
                                if pending:
                                    await asyncio.gather(*pending, return_exceptions=True)
                                return datos

                        tareas = list(pending)
                    return {}
                finally:
                    restos = [t for t in tareas if not t.done()]
                    for t in restos:
                        t.cancel()
                    if restos:
                        await asyncio.gather(*restos, return_exceptions=True)

            datos = async_to_sync(obtener_datos)() or {}

            if fecha_expedicion_req:
                datos["fecha_expedicion"] = fecha_expedicion_req
            if "sexo" in datos and isinstance(datos["sexo"], str):
                datos["sexo"] = (datos["sexo"].strip().splitlines() or [""])[0]

            if not datos:
                # Crear candidato mínimo, incluyendo email/profesion si llegaron (NUEVO)
                candidato = Candidato.objects.create(
                    cedula=cedula,
                    tipo_doc=tipo_doc_req or "",
                    email=email_param or None,            # (NUEVO)
                    profesion=profesion_param or "",      # (NUEVO)
                )
                estado = "no_encontrado"
            else:
                # Sobrescribir email/profesion si llegaron por parámetro (tienen prioridad) (NUEVO)
                if email_param:
                    datos["email"] = email_param
                if profesion_param:
                    datos["profesion"] = profesion_param

                candidato = Candidato.objects.create(
                    cedula=cedula,
                    tipo_doc=datos.get("tipo_doc", tipo_doc_req or ""),
                    nombre=datos.get("nombre", ""),
                    apellido=datos.get("apellido", ""),
                    fecha_nacimiento=datos.get("fecha_nacimiento") or None,
                    fecha_expedicion=datos.get("fecha_expedicion") or None,
                    tipo_persona=datos.get("tipo_persona", ""),
                    sexo=datos.get("sexo", ""),
                    email=datos.get("email") or None,           # (NUEVO)
                    profesion=datos.get("profesion", ""),       # (NUEVO)
                )
                estado = "en_proceso"

        consulta = Consulta.objects.create(
            candidato=candidato,
            estado=estado,
            usuario=token_user
        )

        # Descontar siempre una consulta
        perfil.consultas_disponibles = max(0, (perfil.consultas_disponibles or 0) - 1)
        perfil.save(update_fields=["consultas_disponibles"])

        # Enriquecer payload para las tasks
        if isinstance(datos, dict):
            datos = {
                **datos,
                "duenio_token": duenio_token,
                "plan": perfil.plan,  # informativo, ya no decide la ruta
            }
        else:
            datos = {"duenio_token": duenio_token, "plan": perfil.plan}

        # Backfill de BOTS_CONTRATISTA_FIJOS si no existe
        try:
            _ = BOTS_CONTRATISTA_FIJOS
        except NameError:
            try:
                BOTS_CONTRATISTA_FIJOS = BOTS_PREMIUM_FIJOS  # noqa: F401
            except NameError:
                BOTS_CONTRATISTA_FIJOS = []

        # Resolver profesión para la selección de bots
        profesion = None
        if isinstance(datos, dict):
            profesion = datos.get("profesion")
        elif hasattr(candidato, "profesion"):
            profesion = candidato.profesion

        bots_profesion = bots_por_profesion(profesion)

        # ---------------------------------------------
        # Enrutamiento por "contratista" basado en parámetros (NUEVO)
        # ---------------------------------------------
        if es_contratista:
            lista_final = uniq_preserve(
                (BOTS_CONTRATISTA_FIJOS or []) +
                (bots_profesion or [])
            )
            if not lista_final:
                lista_final = BOTS_CONTRATISTA_FIJOS
            procesar_consulta_contratista_por_nombres.delay(consulta.id, datos, lista_final)
        else:
            if lista_nombres:
                if not isinstance(lista_nombres, list):
                    return Response({"error": "lista_nombres debe ser una lista"}, status=status.HTTP_400_BAD_REQUEST)
                procesar_consulta_por_nombres.delay(consulta.id, datos, lista_nombres)
            else:
                procesar_consulta.delay(consulta.id, datos)

        return Response({
            "token_de": duenio_token,
            "plan": perfil.plan,            # solo informativo
            "contratista": es_contratista,  # ahora según email+profesion
            "datos": datos,
        }, status=status.HTTP_201_CREATED)

    except Exception as e:
        return Response({
            "error": str(e),
            "token_de": duenio_token,
            "plan": getattr(perfil, "plan", None),
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def listar_consultas(request):
    consultas = Consulta.objects.filter(usuario=request.user).order_by("-fecha")

    data = [
        {
            "id": c.id,
            "cedula": c.candidato.cedula,
            "nombre": f"{c.candidato.nombre} {c.candidato.apellido}".strip(),
            "estado": c.estado,
            "fecha": c.fecha.isoformat(),
        }
        for c in consultas
    ]
    return Response(data)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def detalle_consulta(request, consulta_id):
    consulta = get_object_or_404(Consulta, id=consulta_id)
    serializer = ConsultaDetalleSerializer(consulta)
    return Response(serializer.data, status=status.HTTP_200_OK)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def listar_resultados(request, consulta_id):
    data = listar_resultados_interno(consulta_id)
    return Response(data, status=status.HTTP_200_OK)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def listar_fuentes(request, consulta_id=None):
    fuentes = Fuente.objects.all().order_by("nombre")
    serializer = FuenteSerializer(fuentes, many=True)
    return Response(serializer.data, status=status.HTTP_200_OK)

@require_GET
def resumen(request):
    total = Consulta.objects.count()
    pendientes = Consulta.objects.filter(estado="pendiente").count()
    finalizadas = Consulta.objects.filter(estado="finalizada").count()

    # Fuente más consultada
    fuente_top = (
        Resultado.objects.values("fuente__nombre")
        .annotate(total=Count("id"))
        .order_by("-total")
        .first()
    )

    data = {
        "total_consultas": total,
        "consultas_pendientes": pendientes,
        "consultas_finalizadas": finalizadas,
        "fuente_mas_consultada": fuente_top["fuente__nombre"] if fuente_top else None,
    }
    return JsonResponse(data)

@api_view(["GET"])
@permission_classes([AllowAny])
def descargar_pdf(request, consulta_id):
    consulta = get_object_or_404(Consulta, id=consulta_id)

    resultados_qs = Resultado.objects.select_related("fuente", "fuente__tipo").filter(consulta=consulta)
    resultados = [
        {
            "fuente": r.fuente.nombre if r.fuente else "Sin fuente",
            "tipo_fuente": r.fuente.tipo.nombre if r.fuente and r.fuente.tipo else "",
            "estado": r.estado,
            "score": float(r.score),
            "mensaje": r.mensaje,
            "archivo": r.archivo,
        }
        for r in resultados_qs
    ]

    # Generar PDF en memoria (BytesIO)
    pdf_buffer = generar_pdf_consolidado(resultados, consulta_id)

    return FileResponse(
        pdf_buffer,
        as_attachment=True,
        filename=f"reporte_consolidado_{consulta_id}.pdf"
    )

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def perfil(request):
    """
    Devuelve la información del usuario actualmente autenticado.
    """
    user = request.user
    serializer = UserSerializer(user)
    return Response(serializer.data, status=200)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def unificar_resultados(request):

    ids = request.GET.get("ids")
    if not ids:
        raise Http404("No se pasaron ids en la consulta (?ids=1,2,3)")

    ids = ids.split(",")
    resultados = Resultado.objects.filter(id__in=ids)

    if not resultados.exists():
        raise Http404("No se encontraron resultados con esos ids")

    output_pdf = PdfMerger()
    temp_pdfs = []

    for r in resultados:
        if not r.archivo:
            continue

        # 👉 ruta completa en el filesystem
        ruta = os.path.join(settings.MEDIA_ROOT, r.archivo)

        if not os.path.exists(ruta):
            continue

        if ruta.lower().endswith(".pdf"):
            output_pdf.append(ruta)
        else:
            # Convertir imagen a PDF temporal
            img = Image.open(ruta).convert("RGB")
            temp_buffer = io.BytesIO()
            img.save(temp_buffer, format="PDF")
            temp_buffer.seek(0)
            output_pdf.append(temp_buffer)
            temp_pdfs.append(temp_buffer)

    if len(output_pdf.pages) == 0:
        raise Http404("Ninguno de los resultados tenía archivo válido")

    # Guardar PDF final en memoria
    final_buffer = io.BytesIO()
    output_pdf.write(final_buffer)
    output_pdf.close()
    final_buffer.seek(0)

    # Cerrar temporales
    for t in temp_pdfs:
        t.close()

    return FileResponse(
        final_buffer,
        as_attachment=True,
        filename="resultados_unificados.pdf",
        content_type="application/pdf",
    )


from decimal import Decimal
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from core.models import Consulta, Resultado

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def calcular_riesgo(request, consulta_id):
    riesgo_calculado = calcular_riesgo_interno(consulta_id)
    return Response(riesgo_calculado)

import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from django.http import HttpResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

import io
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle
from django.http import HttpResponse

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def generar_mapa_calor(request, consulta_id):
    try:
        # Genera la imagen en base64 con tu función interna
        img_base64 = generar_mapa_calor_interno(consulta_id)

        # Decodifica a bytes
        img_bytes = base64.b64decode(img_base64)

        # Retorna como imagen PNG
        return HttpResponse(img_bytes, content_type="image/png")

    except Exception as e:
        return Response({"error": str(e)}, status=500)


import requests
import base64
from django.template.loader import render_to_string
from django.http import HttpResponse
from weasyprint import HTML


from decimal import Decimal
from django.db.models import Avg
from django.shortcuts import get_object_or_404
from .models import Consulta, Resultado


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def generar_bubble_chart(request, consulta_id):
    try:
        # Genera la imagen en base64 con tu función interna
        img_base64 = generar_bubble_chart_interno(consulta_id)

        # Decodifica a bytes
        img_bytes = base64.b64decode(img_base64)

        # Retorna como imagen PNG
        return HttpResponse(img_bytes, content_type="image/png")

    except Exception as e:
        return Response({"error": str(e)}, status=500)


# Funciones internas

def ajustar_score(score):
    """Convierte cualquier score a 10, 8, 6 o 2 según el valor más cercano."""
    score = round(score)
    if score >= 9:
        return 10
    elif score >= 7:
        return 8
    elif score >= 5:
        return 6
    else:
        return 2
from django.db.models import Prefetch
from decimal import Decimal
import matplotlib.patches as mpatches
def calcular_riesgo_interno(consulta_id):
    consulta = Consulta.objects.prefetch_related(
        Prefetch("resultado_set", queryset=Resultado.objects.select_related("fuente__tipo"))
    ).get(id=consulta_id)

    resultados = consulta.resultado_set.all()
    if not resultados:
        return {
            "probabilidad": 0,
            "consecuencia": 0,
            "riesgo": 0,
            "categoria": "Sin datos"
        }

    total_pesos = 0
    suma_prob = 0
    suma_cons = 0

    # Recolectar datos para el promedio ponderado
    for resultado in resultados:
        if not resultado.fuente or not resultado.fuente.tipo:
            continue

        peso = resultado.fuente.tipo.peso or 1
        prob_fuente = resultado.fuente.tipo.probabilidad or 1
        score = resultado.score or 0

        suma_prob += peso * prob_fuente
        suma_cons += peso * score
        total_pesos += peso

    if total_pesos == 0:
        return {
            "probabilidad": 0,
            "consecuencia": 0,
            "riesgo": 0,
            "categoria": "Sin datos"
        }

    # Promedios ponderados → enteros (1–5) y clamp
    def clamp01_5(x):
        return min(5, max(1, int(round(x))))

    prob_global = clamp01_5(suma_prob / total_pesos)
    cons_global = clamp01_5(suma_cons / total_pesos)

    # Matriz definida (con colores/valores)
    matriz = {
        (1,1): ("Bajo", 1), (2,1): ("Bajo", 2), (3,1): ("Bajo", 3), (4,1): ("Bajo", 4), (5,1): ("Medio", 5),
        (1,2): ("Bajo", 2), (2,2): ("Bajo", 4), (3,2): ("Medio", 6), (4,2): ("Medio", 8), (5,2): ("Medio", 10),
        (1,3): ("Bajo", 3), (2,3): ("Medio", 6), (3,3): ("Medio", 9), (4,3): ("Medio", 12), (5,3): ("Alto", 15),
        (1,4): ("Bajo", 4), (2,4): ("Medio", 8), (3,4): ("Medio", 12), (4,4): ("Alto", 16), (5,4): ("Alto", 20),
        (1,5): ("Medio", 5), (2,5): ("Medio", 10), (3,5): ("Alto", 15), (4,5): ("Alto", 20), (5,5): ("Alto", 25),
    }

    base_categoria, base_riesgo = matriz.get((prob_global, cons_global), ("Sin datos", 0))

    # Evaluar candidatos de escalamiento (no forzar sin más)
    final_prob = prob_global
    final_cons = cons_global
    final_categoria = base_categoria
    final_riesgo = base_riesgo

    for resultado in resultados:
        if not resultado.fuente or not resultado.fuente.tipo:
            continue

        peso = resultado.fuente.tipo.peso or 1
        prob_fuente = resultado.fuente.tipo.probabilidad or 1
        score = resultado.score or 0

        # ignorar resultados sin hallazgo
        if score <= 0:
            continue

        # Sólo consideramos escalamiento para pesos significativos (>=3)
        if peso < 3:
            continue

        # Candidate: combinar info global con la info de la fuente
        # Usamos max para asegurarnos de no reducir el global por un valor puntual,
        # pero podrías cambiar por un promedio ponderado si prefieres.
        cand_prob = clamp01_5(max(prob_global, prob_fuente))
        cand_cons = clamp01_5(max(cons_global, score))

        cand_categoria, cand_riesgo = matriz.get((cand_prob, cand_cons), ("Sin datos", 0))

        # Reglas de aceptación según peso (ajustables)
        accept = False
        if peso >= 5:
            # Fuente crítica: aceptar si la fuente tiene probabilidad relevante o score alto
            if score >= 4 or prob_fuente >= 3:
                accept = True
        elif peso == 4:
            # Fuente muy importante: aceptar si hallazgo fuerte Y probabilidad razonable
            if score >= 4 and prob_fuente >= 3:
                accept = True
        elif peso == 3:
            # Fuente moderada: aceptar sólo hallazgo muy alto y probabilidad moderada
            if score >= 5 and prob_fuente >= 3:
                accept = True

        # Alternativa adicional: si el candidato produce un riesgo significativamente mayor
        # y el peso es grande, podemos aceptarlo (por ejemplo, cand_riesgo >= final_riesgo + 8)
        if not accept:
            if peso >= 4 and cand_riesgo >= final_riesgo + 8 and prob_fuente >= 2:
                accept = True

        if accept and cand_riesgo > final_riesgo:
            final_prob = cand_prob
            final_cons = cand_cons
            final_categoria = cand_categoria
            final_riesgo = cand_riesgo

    return {
        "probabilidad": final_prob,
        "consecuencia": final_cons,
        "riesgo": final_riesgo,
        "categoria": final_categoria
    }
def calcular_riesgo_interno_b(consulta_id):
    consulta = get_object_or_404(Consulta, id=consulta_id)
    candidato = consulta.candidato  # Obtenemos el candidato asociado

    qs = (
        Resultado.objects.filter(consulta=consulta, fuente__isnull=False)
        .values(
            "fuente__tipo__id",
            "fuente__tipo__nombre",
            "fuente__tipo__nivel_exposicion",
            "fuente__tipo__nivel_consecuencia",
        )
        .annotate(promedio=Avg("score"))
    )

    riesgo_total = Decimal("0.0")
    detalle = []

    for r in qs:
        ND = Decimal(r["promedio"] or 0)
        NE = Decimal(r["fuente__tipo__nivel_exposicion"])
        NC = Decimal(r["fuente__tipo__nivel_consecuencia"])

        NP = ND * NE
        NR = NP * NC
        riesgo_total += NR

        if NR >= 600:
            nivel = "I"
        elif NR >= 150:
            nivel = "II"
        elif NR >= 40:
            nivel = "III"
        else:
            nivel = "IV"

        detalle.append({
            "tipo": r["fuente__tipo__nombre"],
            "nivel_deficiencia": float(ND),
            "nivel_exposicion": float(NE),
            "nivel_consecuencia": float(NC),
            "nivel_probabilidad": float(NP),
            "puntaje_riesgo": float(NR),
            "nivel_riesgo": nivel,
        })

    if riesgo_total >= 600:
        nivel_global = "I"
    elif riesgo_total >= 150:
        nivel_global = "II"
    elif riesgo_total >= 40:
        nivel_global = "III"
    else:
        nivel_global = "IV"

    # Retornamos también la info biográfica del candidato
    return {
        "consulta_id": consulta.id,
        "riesgo_total": float(riesgo_total),
        "nivel_global": nivel_global,
        "detalle": detalle,
        "candidato": {
            "cedula": candidato.cedula,
            "tipo_doc": candidato.tipo_doc,
            "nombre": candidato.nombre,
            "apellido": candidato.apellido,
            "fecha_nacimiento": candidato.fecha_nacimiento,
            "fecha_expedicion": candidato.fecha_expedicion,
            "tipo_persona": candidato.tipo_persona,
            "sexo": candidato.sexo,
        }
    }

from django.db.models import Case, When, Value, IntegerField

def listar_resultados_interno(consulta_id):
    # Definimos el orden de prioridad de tipos de fuente
    prioridad = Case(
        When(fuente__tipo__nombre="Plena identidad", then=Value(1)),
        When(fuente__tipo__nombre="Antecedentes Judiciales y Penales Nacionales", then=Value(2)),
        When(fuente__tipo__nombre="Listas Restrictivas Nacionales", then=Value(3)),
        When(fuente__tipo__nombre="Antecedentes de distintas índoles", then=Value(4)),
        When(fuente__tipo__nombre="Antecedentes Financieros y Comerciales", then=Value(5)),
        When(fuente__tipo__nombre="Seguridad Social", then=Value(6)),
        default=Value(99),
        output_field=IntegerField(),
    )

    qs = (
        Resultado.objects
        .select_related("fuente", "fuente__tipo", "consulta")
        .filter(consulta_id=consulta_id)
        .annotate(prioridad_tipo=prioridad)
        .order_by("prioridad_tipo", "fuente__tipo__nombre", "fuente__nombre")
    )

    serializer = ResultadoSerializer(qs, many=True)
    return serializer.data


import io, base64
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Patch
from matplotlib import patheffects as pe
from matplotlib import colors as mcolors

def _lerp_color(c1_hex, c2_hex, t):
    """Interpola entre 2 colores hex en [0,1]."""
    c1 = np.array(mcolors.to_rgb(c1_hex))
    c2 = np.array(mcolors.to_rgb(c2_hex))
    return mcolors.to_hex((1 - t) * c1 + t * c2)

def generar_mapa_calor_interno(consulta_id):
    # ——— Riesgo ———
    riesgo_data = calcular_riesgo_interno(consulta_id)
    prob = riesgo_data["probabilidad"]      # 1..5
    cons = riesgo_data["consecuencia"]      # 1..5

    # ——— Matriz (consecuencia x probabilidad) ———
    riesgo_matrix = np.array([
        [1,  2,  3,  4,  5],
        [2,  4,  6,  8, 10],
        [3,  6,  9, 12, 15],
        [4,  8, 12, 16, 20],
        [5, 10, 15, 20, 25]
    ], dtype=float)

    # Rangos por bucket (para degradé interno)
    RANGO_VERDE    = (1.0, 4.0)   # ≤4
    RANGO_AMARILLO = (5.0, 12.0)  # 5..12
    RANGO_ROJO     = (13.0, 25.0) # 13..25

    # Paletas por bucket (inicio → fin del degradé)
    # Puedes ajustar tonos a tu gusto.
    VERDE_INI, VERDE_FIN       = "#0D4D3A", "#10FF90"   # verde oscuro → verde neón
    AMARILLO_INI, AMARILLO_FIN = "#7A7200", "#FFF10A"   # mostaza → amarillo brillante
    ROJO_INI, ROJO_FIN         = "#5A0A0A", "#FF1A1A"   # vino → rojo vivo

    # ——— Figura ———
    fig, ax = plt.subplots(figsize=(8.8, 6.6), facecolor="none")

    # Dibujar celdas con degradé según bucket
    for i in range(5):
        for j in range(5):
            val = riesgo_matrix[i, j]
            if val <= RANGO_VERDE[1]:
                a, b = RANGO_VERDE
                t = 0 if b == a else (val - a) / (b - a)
                color = _lerp_color(VERDE_INI, VERDE_FIN, np.clip(t, 0, 1))
            elif val <= RANGO_AMARILLO[1]:
                a, b = RANGO_AMARILLO
                t = 0 if b == a else (val - a) / (b - a)
                color = _lerp_color(AMARILLO_INI, AMARILLO_FIN, np.clip(t, 0, 1))
            else:
                a, b = RANGO_ROJO
                t = 0 if b == a else (val - a) / (b - a)
                color = _lerp_color(ROJO_INI, ROJO_FIN, np.clip(t, 0, 1))

            rect = plt.Rectangle((j, i), 1, 1, facecolor=color, edgecolor="#0ff", linewidth=0.6, alpha=1.0)
            ax.add_patch(rect)

            # número con glow sutil
            txt = ax.text(j + 0.5, i + 0.5, str(int(val)),
                          ha="center", va="center", fontsize=10, color="white")
            txt.set_path_effects([
                pe.withStroke(linewidth=3.2, foreground="black", alpha=0.45),
                pe.withStroke(linewidth=1.8, foreground="#00E5FF", alpha=0.35),
            ])

    # Marco con glow neón cian
    bbox = FancyBboxPatch((0, 0), 5, 5,
                          boxstyle="round,pad=0.02,rounding_size=0.15",
                          linewidth=1.6, edgecolor="#00E5FF", facecolor="none")
    bbox.set_path_effects([
        pe.withStroke(linewidth=14, foreground=(0, 1, 1, 0.08)),
        pe.withStroke(linewidth=10, foreground=(0, 1, 1, 0.10)),
        pe.withStroke(linewidth=6,  foreground=(0, 1, 1, 0.18)),
        pe.withStroke(linewidth=3,  foreground=(0, 1, 1, 0.40)),
    ])
    ax.add_patch(bbox)

    # Marcar celda actual
    x_c, y_c = prob - 0.5, cons - 0.5
    ax.scatter([x_c], [y_c], s=180,
               facecolor="white", edgecolor="#00E5FF", linewidth=2.2, zorder=5)
    for lw, alpha in [(16, 0.08), (10, 0.10), (6, 0.18), (3, 0.40)]:
        ax.scatter([x_c], [y_c], s=180, facecolor="none",
                   edgecolor=(0, 1, 1, alpha), linewidth=lw, zorder=4)

    # Ticks y etiquetas (como en tu versión)
    ax.set_xticks(np.arange(5) + 0.5)
    ax.set_xticklabels(
        ["Improbable", "Raro", "Posible",
         "Probable", "Frecuente"],
        rotation=28, ha="right", color="white", fontsize=9
    )
    ax.set_yticks(np.arange(5) + 0.5)
    ax.set_yticklabels(
        ["Insignificante", "Menor", "Moderado", "Crítico", "Catastrófico"],
        color="white", fontsize=9
    )

    ax.set_xlim(0, 5)
    ax.set_ylim(0, 5)
    ax.invert_yaxis()  # ← mantiene la misma orientación que tu tabla
    ax.set_xlabel("Probabilidad", color="white", labelpad=8)
    ax.set_ylabel("Consecuencia", color="white", labelpad=8)

    title = ax.set_title("Mapa de Calor de Riesgos", color="white", pad=12)
    title.set_path_effects([pe.withStroke(linewidth=4, foreground="#00E5FF", alpha=0.35)])

    # Leyenda (muestras representativas por bucket)
    legend_elements = [
        Patch(facecolor=_lerp_color(VERDE_INI, VERDE_FIN, 0.7),  edgecolor="#0ff", label="Bajo (≤4)"),
        Patch(facecolor=_lerp_color(AMARILLO_INI, AMARILLO_FIN, 0.7), edgecolor="#0ff", label="Medio (5–12)"),
        Patch(facecolor=_lerp_color(ROJO_INI, ROJO_FIN, 0.7), edgecolor="#0ff", label="Alto (≥13)"),
    ]
    leg = ax.legend(handles=legend_elements, title="Nivel de Riesgo",
                    loc="upper left", bbox_to_anchor=(1.05, 1))
    plt.setp(leg.get_texts(), color="white")
    plt.setp(leg.get_title(), color="white")

    # Grid tenue cian
    ax.set_xticks(np.arange(0, 5, 1), minor=True)
    ax.set_yticks(np.arange(0, 5, 1), minor=True)
    ax.grid(which="minor", linewidth=0.6, alpha=0.35, color="#00E5FF")
    for s in ax.spines.values():
        s.set_visible(False)

    # Export transparente
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=160, transparent=True)
    buf.seek(0)
    base64_img = base64.b64encode(buf.read()).decode("utf-8")
    plt.close(fig)
    return base64_img

import matplotlib.colors as mcolors

def generar_bubble_chart_interno(consulta_id):
    # Llamar función de riesgo
    riesgo_data = calcular_riesgo_interno(consulta_id)
    prob = riesgo_data["probabilidad"]
    cons = riesgo_data["consecuencia"]
    riesgo = riesgo_data["riesgo"]
    categoria = riesgo_data["categoria"]

    # Definir color según categoría
    colores = {
        "Bajo": "green",
        "Medio": "yellow",
        "Alto": "red"
    }
    color = colores.get(categoria, "gray")

    # Convertir el color base a RGBA con menos transparencia
    rgba_color = mcolors.to_rgba(color, alpha=0.25)

    # Crear figura con fondo
    fig, ax = plt.subplots(figsize=(6, 6), facecolor=rgba_color)
    ax.set_facecolor(rgba_color)

    # Dibujar burbuja
    ax.scatter(
        prob, cons,
        s=riesgo * 100,     # tamaño proporcional al riesgo
        c=color, alpha=0.6, edgecolors="white"
    )

    # Etiquetas del punto
    ax.text(prob + 0.1, cons + 0.1, f"Riesgo: {riesgo}\n{categoria}",
            fontsize=10, ha="left", va="bottom", color="white")

    # Configuración de ejes
    ax.set_xlim(0.5, 5.5)
    ax.set_ylim(0.5, 5.5)
    ax.set_xticks(range(1, 6))
    ax.set_yticks(range(1, 6))
    ax.set_xlabel("Probabilidad", color="white")
    ax.set_ylabel("Consecuencia", color="white")
    ax.set_title("Gráfico de Burbuja - Riesgo", color="white")

    # Ejes y ticks en blanco
    ax.tick_params(colors="white")
    for spine in ax.spines.values():
        spine.set_edgecolor("white")

    # Cuadrícula en blanco tenue
    ax.grid(True, linestyle="--", alpha=0.5, color="white")

    # Convertir a base64
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", transparent=True)  # mantiene transparencia fuera del gráfico
    buf.seek(0)
    base64_img = base64.b64encode(buf.read()).decode("utf-8")
    plt.close(fig)

    return base64_img

def reporte(request, consulta_id):
    calcular_riesgo = calcular_riesgo_interno_b(consulta_id)
    resultados = listar_resultados_interno(consulta_id)
    mapa_riesgo_data = generar_mapa_calor_interno(consulta_id)

    nivel_color = {"I": "red", "II": "orange", "III": "yellow", "IV": "green"}
    color_riesgo = nivel_color.get(calcular_riesgo.get("nivel_global"), "gray")

    # Convertir ruta a URL absoluta
    for r in resultados:
        if r.get("archivo"):
            relative_path = r["archivo"].replace("\\", "/")
            r["archivo_url"] = request.build_absolute_uri(settings.MEDIA_URL + relative_path)


    context = {
        "mapa_riesgo": mapa_riesgo_data,
        "resultados": resultados,
        "riesgo": calcular_riesgo,
        "color_riesgo": color_riesgo,
    }

    html_string = render_to_string("reportes/consolidado.html", context)
    pdf = HTML(string=html_string, base_url=request.build_absolute_uri()).write_pdf()

    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = "inline; filename=reporte.pdf"
    return response

def descargar_reporte(request, consulta_id):
    # Datos internos ya calculados
    calcular_riesgo = calcular_riesgo_interno_b(consulta_id)
    resultados = listar_resultados_interno(consulta_id)
    # mapa_riesgo_data = generar_mapa_calor_interno(consulta_id)

    # Diccionario de colores según nivel de riesgo
    nivel_color = {"I": "red", "II": "orange", "III": "yellow", "IV": "green"}
    color_riesgo = nivel_color.get(calcular_riesgo.get("nivel_global"), "gray")

    context = {
        # "mapa_riesgo": mapa_riesgo_data,
        "resultados": resultados,
        "riesgo": calcular_riesgo,
        "color_riesgo": color_riesgo,
    }

    # Renderizar HTML y generar PDF
    html_string = render_to_string("reportes/consolidado.html", context)
    pdf = HTML(string=html_string, base_url=request.build_absolute_uri()).write_pdf()

    # Crear respuesta con descarga
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="reporte_{consulta_id}.pdf"'
    return response

from datetime import timedelta

import qrcode
from io import BytesIO
from django.core.files.base import ContentFile
from django.urls import reverse

import qrcode
from io import BytesIO
from django.core.files.base import ContentFile
from django.urls import reverse
from weasyprint import HTML
from django.utils.timezone import now
from .models import Consolidado
from django.utils.text import slugify 

def generar_consolidado_interno(consulta_id, tipo_id, usuario, request=None):
    from django.db import transaction
    from django.utils.text import slugify
    from .models import Consolidado, Consulta, TipoConsolidado
    from django.utils.timezone import now
    import qrcode
    from io import BytesIO
    from django.core.files.base import ContentFile
    from django.urls import reverse
    from django.conf import settings
    from django.template.loader import render_to_string
    from weasyprint import HTML
    from django.utils import timezone
    from qrcode.image.pil import PilImage
    import os

    # ---------- Helpers ----------
    def safe_filename(*parts, ext="pdf"):
        base = "-".join(filter(None, (slugify(str(p)) for p in parts)))
        return f"{base}.{ext}"

    def _build_qr_url():
        # URL que irá codificada en el QR
        if request:
            return request.build_absolute_uri(
                reverse("vista_resumen_consulta", args=[consulta_id])
            )
        return f"/econfia/resumen-consulta/{consulta_id}/"

    def _ensure_qr(consolidado):
        """
        Genera y guarda el QR SOLO si el campo consolidado.qr está vacío.
        Devuelve True si lo creó, False si ya existía.
        """
        if getattr(consolidado, "qr", None) and getattr(consolidado.qr, "name", ""):
            return False  # ya hay archivo asociado

        qr_url = _build_qr_url()

        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=4,  # quiet zone
        )
        qr.add_data(qr_url)
        qr.make(fit=True)

        # Azul oscuro (alto contraste sobre blanco)
        DARK_BLUE = "#091120"

        img: PilImage = qr.make_image(
            fill_color=DARK_BLUE,
            back_color="white"
        ).convert("RGB")  # sin alpha para evitar aplanado raro en PDF

        qr_io = BytesIO()
        img.save(qr_io, format="PNG", optimize=True)

        qr_content = ContentFile(qr_io.getvalue(), name=f"qr_{consulta_id}.png")
        # guarda el archivo en storage pero no hace save() del modelo aún
        consolidado.qr.save(f"qr_{consulta_id}.png", qr_content, save=False)
        return True

    # ---------- Carga de objetos base ----------
    consulta = Consulta.objects.get(id=consulta_id)
    tipo, _ = TipoConsolidado.objects.get_or_create(
        id=tipo_id,
        defaults={"nombre": f"Tipo {tipo_id}"}
    )
    candidato = consulta.candidato

    # ---------- Busca/crea consolidado y asegura QR ----------
    with transaction.atomic():
        consolidado = (
            Consolidado.objects.select_for_update()
            .filter(consulta=consulta, tipo=tipo)
            .order_by("-fecha_creacion")
            .first()
        )

        if consolidado is None:
            consolidado = Consolidado.objects.create(
                consulta=consulta,
                tipo=tipo,
                usuario=usuario,
            )

        # Asegurar que exista un QR si no hay archivo asociado
        created_qr = _ensure_qr(consolidado)

        # Si se generó por primera vez el QR, persiste el campo
        if created_qr:
            consolidado.save(update_fields=["qr"])

    # ---------- Cálculos / gráficas / datos del reporte ----------
    mapa_riesgo_path = generar_mapa_calor_interno(consulta_id)
    bubble_chart_path = generar_bubble_chart_interno(consulta_id)
    calcular_riesgo = calcular_riesgo_interno(consulta_id)
    resultados = listar_resultados_interno(consulta_id)
    cilindros = generar_cilindros_scores_interno(consulta_id)
    barras = generar_grafico_3d_interno(consulta_id)

    if request:
        for r in resultados:
            rel = r.get("archivo")
            if rel:
                rel = rel.replace("\\", "/")
                r["archivo_url"] = request.build_absolute_uri(settings.MEDIA_URL + rel)

    nivel_color = {
        "Extremo": "red",
        "Alto": "red",
        "Medio": "yellow",
        "Bajo": "green",
    }
    color_riesgo = nivel_color.get(calcular_riesgo.get("categoria"), "gray")

    # Obtener URL del QR de forma segura (puede no existir)
    try:
        qr_url_absoluta = (
            request.build_absolute_uri(consolidado.qr.url) if (request and consolidado.qr and consolidado.qr.name)
            else (consolidado.qr.url if (consolidado.qr and consolidado.qr.name) else None)
        )
    except Exception:
        qr_url_absoluta = None

    context = {
        "mapa_riesgo": mapa_riesgo_path,
        "bubble_chart": bubble_chart_path,
        "cilindros": cilindros,
        "barras": barras,
        "resultados": resultados,
        "riesgo": calcular_riesgo,
        "color_riesgo": color_riesgo,
        "consulta_id": consulta_id,
        "consolidado_id": consolidado.id,
        "fecha_generacion": timezone.localtime(getattr(consolidado, "fecha_creacion", timezone.now())),
        "fecha_actualizacion": timezone.localtime(consolidado.fecha_actualizacion) if getattr(consolidado, "fecha_actualizacion", None) else None,
        "usuario": usuario.username if usuario else None,
        "tipo_reporte": tipo.nombre,
        "ip_generacion": request.META.get("REMOTE_ADDR") if request else None,
        "qr_url": qr_url_absoluta,
        "candidato": {
            "cedula": candidato.cedula,
            "tipo_doc": getattr(candidato, "tipo_doc", None),
            "nombre": candidato.nombre,
            "apellido": candidato.apellido,
            "fecha_nacimiento": getattr(candidato, "fecha_nacimiento", None),
            "fecha_expedicion": getattr(candidato, "fecha_expedicion", None),
            "tipo_persona": getattr(candidato, "tipo_persona", None),
            "sexo": getattr(candidato, "sexo", None),
        },
    }

    templates_por_tipo = {
        1: "reportes/consolidado.html",
        2: "reportes/consolidado_pdf.html",
        3: "reportes/consolidado_resumen.html",
    }
    template_path = templates_por_tipo.get(tipo_id, "reportes/consolidado.html")

    html_string = render_to_string(template_path, context)
    pdf_bytes = HTML(
        string=html_string,
        base_url=(request.build_absolute_uri() if request else None)
    ).write_pdf()

    filename = safe_filename(candidato.nombre, candidato.apellido, candidato.cedula, ext="pdf")

    # Reemplazar archivo PDF previo si existe
    if consolidado.archivo and getattr(consolidado.archivo, "name", ""):
        try:
            consolidado.archivo.delete(save=False)
        except Exception:
            pass

    consolidado.archivo.save(filename, ContentFile(pdf_bytes), save=False)

    # Actualiza metadatos
    consolidado.fecha_actualizacion = now()
    if usuario:
        consolidado.usuario = usuario
    consolidado.save(update_fields=["archivo", "fecha_actualizacion", "usuario"])

    return consolidado


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def generar_consolidado(request, consulta_id, tipo_id):
    try:
        consolidado = generar_consolidado_interno(
            consulta_id, tipo_id, request.user, request=request
        )
        return Response(
            {
                "status": "success",
                "message": "Consolidado generado y guardado correctamente.",
                "consolidado_id": consolidado.id,
                "archivo_url": consolidado.archivo.url,
                "qr_url": consolidado.qr.url,
            },
            status=201,
        )
    except Exception as e:
        traceback.print_exc()
        return Response({"status": "error", "message": str(e)}, status=500)

from django.db.models import Avg, Count, Q

def resumen_consulta_interno(consulta_id):
    try:
        consulta = Consulta.objects.get(pk=consulta_id)
    except Consulta.DoesNotExist:
        return None, {"error": "Consulta no encontrada"}

    # --- Resumen por estado (igual que antes) ---
    resumen_estados = (
        Resultado.objects.filter(consulta=consulta)
        .values("estado")
        .annotate(total=Count("id"))
    )
    estados_dict = {item["estado"]: item["total"] for item in resumen_estados}

    total = sum(estados_dict.values())
    offline = estados_dict.get("offline", 0)
    validados = estados_dict.get("validado", 0)
    pendientes = estados_dict.get("pendiente", 0)

    # --- Promedio global del score 1–5 (opcional, lo dejo) ---
    promedio_score = (
        Resultado.objects.filter(consulta=consulta)
        .aggregate(promedio=Avg("score"))["promedio"]
    )
    if promedio_score is not None:
        promedio_score = round(float(promedio_score), 2)

    # --- Distribución de scores por categoría (TipoFuente) ---
    dist_qs = (
        Resultado.objects
        .filter(consulta=consulta, fuente__isnull=False, fuente__tipo__isnull=False)
        .values("fuente__tipo", "fuente__tipo__nombre")
        .annotate(
            total=Count("id"),
            s1=Count("id", filter=Q(score=1)),
            s2=Count("id", filter=Q(score=2)),
            s3=Count("id", filter=Q(score=3)),
            s4=Count("id", filter=Q(score=4)),
            s5=Count("id", filter=Q(score=5)),
        )
        .order_by("fuente__tipo__nombre")
    )

    por_categoria = []
    for row in dist_qs:
        total_cat = row["total"] or 0
        dist = {
            1: row["s1"] or 0,
            2: row["s2"] or 0,
            3: row["s3"] or 0,
            4: row["s4"] or 0,
            5: row["s5"] or 0,
        }
        # (Opcional) porcentajes, por si los quieres mostrar
        porcentajes = {k: (v / total_cat * 100 if total_cat else 0.0) for k, v in dist.items()}

        por_categoria.append({
            "categoria_id": row["fuente__tipo"],
            "categoria_nombre": row["fuente__tipo__nombre"],
            "total_bots": total_cat,
            "scores": dist,              # conteo por score (1..5)
            "porcentajes": {k: round(p, 2) for k, p in porcentajes.items()},  # opcional
        })

    data = {
        "consulta_id": consulta.id,
        "candidato": consulta.candidato.cedula,
        "total_resultados": total,
        "estados": estados_dict,
        "offline": offline,
        "validados": validados,
        "pendientes": pendientes,
        "promedio_score": promedio_score,
        "usuario": consulta.usuario.username,
        "fecha": consulta.fecha,
        "por_categoria": por_categoria,   # << AQUÍ VIENE LO QUE PEDISTE
    }

    return consulta, data

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def resumen_consulta(request, consulta_id):
    consulta, data = resumen_consulta_interno(consulta_id)
    if consulta is None:
        return Response(data, status=404)
    return Response(data, status=200)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def resumen_usuario(request):
    usuario = request.user
    perfil = getattr(usuario, "perfil", None)

    from datetime import timedelta
    from django.utils.timezone import now
    from django.db.models import Count, Avg

    hoy = now()
    hace_una_semana = hoy - timedelta(days=7)
    hace_un_mes = hoy - timedelta(days=30)

    # Consultas del usuario
    consultas = Consulta.objects.filter(usuario=usuario)

    consultas_semana = consultas.filter(fecha__gte=hace_una_semana).count()
    consultas_mes = consultas.filter(fecha__gte=hace_un_mes).count()
    total_consultas = consultas.count()

    # Consultas agrupadas por estado
    consultas_por_estado = consultas.values("estado").annotate(total=Count("id"))

    # Promedio de scores de resultados del usuario
    promedio_score = (
        Resultado.objects.filter(consulta__usuario=usuario)
        .aggregate(avg_score=Avg("score"))
        .get("avg_score")
    )

    # Total de consolidados
    total_consolidados = Consolidado.objects.filter(usuario=usuario).count()

    data = {
        "usuario": usuario.username,
        "perfil": {
            "plan": perfil.plan if perfil else None,
            "consultas_disponibles": perfil.consultas_disponibles if perfil else 0,
        },
        "estadisticas": {
            "consultas": {
                "total": total_consultas,
                "ultima_semana": consultas_semana,
                "ultimo_mes": consultas_mes,
                "por_estado": list(consultas_por_estado),
            },
            "resultados": {
                "promedio_score": float(promedio_score) if promedio_score is not None else None,
            },
            "consolidados": {
                "total": total_consolidados,
            }
        }
    }

    return Response(data)

def generar_consolidado_api(request, consulta_id, tipo_id):
    from .models import Consulta, TipoConsolidado

    consulta = Consulta.objects.get(id=consulta_id)
    tipo = TipoConsolidado.objects.get(id=tipo_id)

    # --- QR ---
    qr_url = (
        request.build_absolute_uri(
            reverse("resumen_consulta_pdf", args=[consulta_id])
        )
        if request else f"/econfia/resumen-consulta/{consulta_id}/"
    )
    qr_img = qrcode.make(qr_url)
    qr_io = BytesIO()
    qr_img.save(qr_io, format="PNG")
    qr_base64 = qr_io.getvalue()

    # --- Datos reporte ---
    mapa_riesgo_data = generar_mapa_calor_interno(consulta_id)
    calcular_riesgo = calcular_riesgo_interno_b(consulta_id)
    resultados = listar_resultados_interno(consulta_id)

    for r in resultados:
        if r.get("archivo"):
            relative_path = r["archivo"].replace("\\", "/")
            r["archivo_url"] = request.build_absolute_uri(
                settings.MEDIA_URL + relative_path
            )

    nivel_color = {"I": "red", "II": "orange", "III": "yellow", "IV": "green"}
    color_riesgo = nivel_color.get(calcular_riesgo.get("nivel_global"), "gray")

    context = {
        "mapa_riesgo": mapa_riesgo_data,
        "resultados": resultados,
        "riesgo": calcular_riesgo,
        "color_riesgo": color_riesgo,
        "consulta_id": consulta_id,
        "fecha_generacion": now(),
        "usuario": request.user.username if request.user.is_authenticated else None,
        "tipo_reporte": tipo.nombre,
        "ip_generacion": request.META.get("REMOTE_ADDR"),
        "qr_url": qr_url,
    }

    # --- Render PDF ---
    html_string = render_to_string("reportes/consolidado_pdf.html", context)
    pdf_bytes = HTML(string=html_string, base_url=request.build_absolute_uri()).write_pdf()

    # --- Respuesta HTTP (mostrar inline en navegador) ---
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="reporte_{consulta_id}.pdf"'

    return response


@api_view(["POST"])
@permission_classes([AllowAny])
def generar_consolidado_descarga(request, consulta_id, tipo_id):
    try:
        # Genera y guarda el consolidado en la base de datos
        consolidado = generar_consolidado_interno(
            consulta_id, tipo_id, request.user, request=request
        )

        # Obtén la ruta del archivo PDF generado
        archivo_path = consolidado.archivo.path

        # Retorna el PDF directamente para descargar
        response = FileResponse(open(archivo_path, 'rb'), as_attachment=True)
        response['Content-Disposition'] = f'attachment; filename="{os.path.basename(archivo_path)}"'
        return response

    except Exception as e:
        traceback.print_exc()
        return Response({"status": "error", "message": str(e)}, status=500)
    

@api_view(["GET"])
@permission_classes([AllowAny])
def descargar_consolidado_categoria(request, consulta_id, tipo_id):
    try:
        consolidado = Consolidado.objects.filter(
            consulta_id=consulta_id,
            tipo_id=tipo_id
        ).order_by("-fecha_creacion").first()

        # Si no existe consolidado o no tiene archivo, intentar generarlo on-demand
        if not consolidado or not getattr(consolidado, "archivo", None) or not getattr(consolidado.archivo, "name", ""):
            try:
                usuario = request.user if hasattr(request, "user") and request.user and request.user.is_authenticated else None
                from .views import generar_consolidado_interno
                consolidado = generar_consolidado_interno(consulta_id, tipo_id, usuario, request=request)
            except Exception as e:
                import traceback
                traceback.print_exc()
                return Response({"error": f"Error al generar consolidado: {e}"}, status=500)

        if not consolidado or not consolidado.archivo:
            return Response({"error": "No se encontró archivo PDF"}, status=404)

        file_path = consolidado.archivo.path
        if not os.path.exists(file_path):
            return Response({"error": "El archivo PDF no está disponible en el servidor"}, status=404)

        original_name = os.path.basename(consolidado.archivo.name)

        with open(file_path, "rb") as pdf:
            response = HttpResponse(pdf.read(), content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="{original_name}"'
            return response

    except Consolidado.DoesNotExist:
        return Response({"error": "Consolidado no encontrado"}, status=404)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_reintentar_bot(request, resultado_id):
    try:
        resultado = Resultado.objects.get(id=resultado_id, consulta__usuario=request.user)

        if resultado.estado == "validado":
            return Response({"error": "El bot ya fue exitoso, no necesita reintento"}, status=400)

        resultado.estado = "revalidando"
        resultado.save(update_fields=["estado"])

        # Enviar a Celery
        reintentar_bot.delay(resultado.id)

        return Response({"message": f"Reejecutando bot {resultado.fuente.nombre}", "estado": resultado.estado})

    except Resultado.DoesNotExist:
        return Response({"error": "Resultado no encontrado"}, status=404)


def vista_resumen_consulta_pdf(request, consulta_id):
    consulta = get_object_or_404(Consulta, id=consulta_id)

    mapa_riesgo_data = generar_mapa_calor_interno(consulta_id)
    calcular_riesgo = calcular_riesgo_interno_b(consulta_id)
    resultados = listar_resultados_interno(consulta_id)

    for r in resultados:
        if r.get("archivo") and request:
            relative_path = r["archivo"].replace("\\", "/")
            r["archivo_url"] = request.build_absolute_uri(
                settings.MEDIA_URL + relative_path
            )

    nivel_color = {"I": "red", "II": "orange", "III": "yellow", "IV": "green"}
    color_riesgo = nivel_color.get(calcular_riesgo.get("nivel_global"), "gray")

    context = {
        "mapa_riesgo": mapa_riesgo_data,
        "resultados": resultados,
        "riesgo": calcular_riesgo,
        "color_riesgo": color_riesgo,
        "consulta_id": consulta_id,
        "fecha_generacion": now(),
        "usuario": request.user.username if request.user.is_authenticated else None,
        "ip_generacion": request.META.get("REMOTE_ADDR"),
    }

    # 🔹 Renderizamos el template a HTML
    html_string = render_to_string("reportes/resumen_consulta.html", context)

    # 🔹 Generamos el PDF
    pdf_file = HTML(
        string=html_string,
        base_url=request.build_absolute_uri()
    ).write_pdf()

    # 🔹 Respondemos el PDF en navegador
    response = HttpResponse(pdf_file, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="resumen_consulta_{consulta_id}.pdf"'
    return response
    # Traemos la consulta
    consulta = get_object_or_404(Consulta, id=consulta_id)

    # 🔹 Aquí puedes preparar los datos adicionales que uses en el template
    # Por ejemplo: mapa_riesgo, resultados, etc. (puedes reusar de tu otra función si quieres)
    mapa_riesgo_data = generar_mapa_calor_interno(consulta_id)
    calcular_riesgo = calcular_riesgo_interno_b(consulta_id)
    resultados = listar_resultados_interno(consulta_id)

    for r in resultados:
        if r.get("archivo") and request:
            relative_path = r["archivo"].replace("\\", "/")
            r["archivo_url"] = request.build_absolute_uri(
                settings.MEDIA_URL + relative_path
            )

    context = {
        "consulta": consulta,
        "mapa_riesgo": mapa_riesgo_data,
        "resultados": resultados,
        "riesgo": calcular_riesgo,
    }

    # Renderizamos el template a HTML
    html_string = render_to_string("reportes/resumen_consulta.html", context)

    # Generamos el PDF en memoria
    pdf_file = HTML(string=html_string, base_url=request.build_absolute_uri()).write_pdf()

    # Respondemos el PDF al navegador
    response = HttpResponse(pdf_file, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="resumen_consulta_{consulta_id}.pdf"'
    return response



# views.py
from typing import Dict, Any, List
from django.http import HttpResponse, JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

# --- Matplotlib (render sin GUI) ---
import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
from matplotlib.patches import Ellipse, Rectangle

ESTADO_COLORS = {"offline": "#7f8c8d", "validado": "#2ecc71", "pendiente": "#f1c40f"}
SCORE_COLORS = {1: "#2ecc71", 2: "#f1c40f", 3: "#e67e22", 4: "#e74c3c", 5: "#8e44ad"}


def generar_grafico_3d_interno(consulta_id) -> bytes:
    """
    Usa resumen_consulta_interno(consulta_id) y genera PNG 3D de:
    offline / validado / pendiente.
    """
    # Llama tu función existente (debes tenerla definida en este mismo archivo)
    _, data = resumen_consulta_interno(consulta_id)

    # Si hubo error en el resumen, devuelvo un PNG con el mensaje
    if not data or "error" in data:
        fig, ax = plt.subplots(figsize=(5, 3), dpi=160)
        ax.text(0.5, 0.5, (data or {}).get("error", "Sin datos"), ha="center", va="center")
        ax.axis("off")
        buf = io.BytesIO(); fig.savefig(buf, format="png", bbox_inches="tight", dpi=160); plt.close(fig)
        buf.seek(0); return buf.read()

    estados = {
        "offline": int(data.get("offline", 0)),
        "validado": int(data.get("validados", 0)),
        "pendiente": int(data.get("pendientes", 0)),
    }

    etiquetas = ["offline", "validado", "pendiente"]
    valores = [estados["offline"], estados["validado"], estados["pendiente"]]

    fig = plt.figure(figsize=(7.5, 5.5), dpi=160)
    ax = fig.add_subplot(111, projection="3d")
    ax.view_init(elev=20, azim=-60)

    xs = range(len(etiquetas))
    ys = [0] * len(etiquetas)
    zs = [0] * len(etiquetas)
    dx = [0.6] * len(etiquetas)
    dy = [0.6] * len(etiquetas)
    dz = valores
    colors = [ESTADO_COLORS[e] for e in etiquetas]

    ax.bar3d(xs, ys, zs, dx, dy, dz, color=colors, shade=True, edgecolor="black", linewidth=0.5)
    ax.set_xticks(list(xs), etiquetas)
    ax.set_yticks([])
    ax.set_zlabel("Cantidad")
    ax.set_title("Estados: offline / validado / pendiente")

    for i, v in enumerate(valores):
        ax.text(i+0.3, 0.3, (v or 0) + (max(valores) * 0.04 if max(valores) else 0.2), str(v))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=160)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def generar_cilindros_scores_interno(consulta_id):
    import io, base64
    import matplotlib
    matplotlib.use("Agg")
    import numpy as np
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as pe
    from matplotlib.patches import FancyBboxPatch, Patch, Circle
    from django.db.models import Count
    from .models import Resultado

    raw = (
        Resultado.objects
        .filter(consulta_id=consulta_id)
        .values("estado")
        .annotate(n=Count("id"))
    )

    counts = {"validado": 0, "offline": 0}
    total = 0

    def _norm_estado(e):
        e = (e or "").lower().strip()
        if e == "ok":
            e = "validado"
        if e in ("error", "sin validar"):
            e = "offline"
        if e == "validada":
            e = "validado"
        return e

    for row in raw:
        e = _norm_estado(row["estado"])
        n = int(row["n"] or 0)
        total += n
        if e in counts:
            counts[e] += n

    vals = [counts["validado"], counts["offline"]]
    has_data = (sum(vals) > 0)
    if not has_data:
        vals = [1, 0]
    labels = ["Validado", "Offline"]

    CIAN_GLOW = "#00E5FF"
    COL_VALID = "#10FF90"
    COL_OFF   = "#FF4D4D"
    COLORS = [COL_VALID, COL_OFF]

    fig, ax = plt.subplots(figsize=(8, 8), facecolor="none")
    ax.set_facecolor("none")

    wedgeprops = dict(width=0.36, edgecolor=CIAN_GLOW, linewidth=1.2)
    wedges, _texts = ax.pie(
        vals,
        colors=COLORS,
        startangle=90,
        counterclock=False,
        labels=None,
        pctdistance=0.82,
        wedgeprops=wedgeprops,
        normalize=True,
    )

    for w in wedges:
        w.set_path_effects([
            pe.withStroke(linewidth=10, foreground=(0, 1, 1, 0.12)),
            pe.withStroke(linewidth=6,  foreground=(0, 1, 1, 0.18)),
            pe.withStroke(linewidth=3,  foreground=(0, 1, 1, 0.35)),
        ])

    outer = Circle((0, 0), 1.02, transform=ax.transData, fill=False, linewidth=2.0, edgecolor=CIAN_GLOW)
    outer.set_path_effects([
        pe.withStroke(linewidth=18, foreground=(0, 1, 1, 0.06)),
        pe.withStroke(linewidth=12, foreground=(0, 1, 1, 0.10)),
        pe.withStroke(linewidth=8,  foreground=(0, 1, 1, 0.16)),
    ])
    ax.add_patch(outer)

    center = Circle((0, 0), 1.0 - wedgeprops["width"], facecolor="#0B0F12", edgecolor="none", alpha=0.95)
    ax.add_patch(center)

    if has_data:
        pct_validado = 100.0 * counts["validado"] / (counts["validado"] + counts["offline"])
        txt_main = f"{pct_validado:.1f}%"
        txt_sub  = f"{counts['validado']} / {counts['offline']}"
    else:
        txt_main = "Sin datos"
        txt_sub  = "0 / 0"

    t1 = ax.text(0, 0.03, txt_main, ha="center", va="center", color="white", fontsize=28, weight="bold")
    t1.set_path_effects([pe.withStroke(linewidth=4, foreground=CIAN_GLOW, alpha=0.35)])
    t2 = ax.text(0, -0.18, txt_sub, ha="center", va="center", color="#B6F9FF", fontsize=12)
    t2.set_path_effects([pe.withStroke(linewidth=3, foreground="black", alpha=0.50)])

    if has_data:
        ang = 90
        total_vals = sum(vals)
        for i, v in enumerate(vals):
            if v <= 0:
                continue
            theta = ang - (v / total_vals) * 180
            ang -= (v / total_vals) * 360
            r = 1.0
            x = r * np.cos(np.deg2rad(theta))
            y = r * np.sin(np.deg2rad(theta))

            label = f"{labels[i]}: {v} ({(100*v/total_vals):.1f}%)"
            txt = ax.text(x * 1.18, y * 1.18, label, ha="center", va="center",
                          color="white", fontsize=11)
            txt.set_path_effects([
                pe.withStroke(linewidth=3, foreground="black", alpha=0.50),
                pe.withStroke(linewidth=1.8, foreground=CIAN_GLOW, alpha=0.30),
            ])
            ax.plot([x*0.98, x*1.10], [y*0.98, y*1.10],
                    linewidth=1.2, color=CIAN_GLOW, alpha=0.65)

    bbox = FancyBboxPatch((-1.35, -1.35), 2.7, 2.7,
                          boxstyle="round,pad=0.02,rounding_size=0.12",
                          linewidth=1.6, edgecolor=CIAN_GLOW, facecolor="none",
                          transform=ax.transData)
    bbox.set_path_effects([
        pe.withStroke(linewidth=14, foreground=(0, 1, 1, 0.08)),
        pe.withStroke(linewidth=10, foreground=(0, 1, 1, 0.10)),
        pe.withStroke(linewidth=6,  foreground=(0, 1, 1, 0.18)),
        pe.withStroke(linewidth=3,  foreground=(0, 1, 1, 0.40)),
    ])
    ax.add_patch(bbox)

    title = ax.set_title("Estados de Resultados: Validado vs Offline", color="white", pad=18, fontsize=14)
    title.set_path_effects([pe.withStroke(linewidth=4, foreground=CIAN_GLOW, alpha=0.35)])

    legend_elements = [
        Patch(facecolor=COL_VALID, edgecolor=CIAN_GLOW, label=f"Validado ({counts['validado']})"),
        Patch(facecolor=COL_OFF,   edgecolor=CIAN_GLOW, label=f"Offline ({counts['offline']})"),
    ]
    leg = ax.legend(handles=legend_elements, title="Estados", loc="upper left", bbox_to_anchor=(1.02, 1.02))
    plt.setp(leg.get_texts(), color="white")
    plt.setp(leg.get_title(), color="white")

    ax.axis("equal")
    ax.set_xlim(-1.35, 1.35)
    ax.set_ylim(-1.35, 1.35)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])

    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=170, transparent=True)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("utf-8")
    plt.close(fig)
    return b64

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def generar_grafico_3d(request, consulta_id=None):

    cid = consulta_id or request.data.get("consulta_id")
    if not cid:
        return JsonResponse({"detail": "consulta_id requerido."}, status=400)

    try:
        png_bytes = generar_grafico_3d_interno(cid)
        return HttpResponse(png_bytes, content_type="image/png")
    except Exception as e:
        return JsonResponse({"detail": f"Error generando gráfico 3D: {e}"}, status=500)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def generar_grafico_cilindros(request, consulta_id=None):

    cid = consulta_id or request.data.get("consulta_id")
    if not cid:
        return JsonResponse({"detail": "consulta_id requerido."}, status=400)

    try:
        png_bytes = generar_cilindros_scores_interno(cid)
        return HttpResponse(png_bytes, content_type="image/png")
    except Exception as e:
        return JsonResponse({"detail": f"Error generando gráfico de cilindros: {e}"}, status=500)




from django.core.mail import send_mail, BadHeaderError
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

@api_view(["POST"])
def test_email(request):

    to = request.data.get("to")
    subject = request.data.get("subject", "Prueba de correo")
    message = request.data.get("message", "Hola! Este es un correo de prueba.")

    if not to:
        return Response({"error": "Debes enviar un destinatario 'to'."},
                        status=status.HTTP_400_BAD_REQUEST)

    try:
        send_mail(
            subject,
            message,
            None,  # Usa DEFAULT_FROM_EMAIL
            [to],
            fail_silently=False,
        )
        return Response({"success": f"Correo enviado a {to}"})
    except BadHeaderError:
        return Response({"error": "Cabecera inválida en el correo"},
                        status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
