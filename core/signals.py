from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User
from .models import Perfil, Candidato

# @receiver(post_save, sender=Perfil)
# def crear_o_actualizar_candidato(sender, instance, created, **kwargs):
#     """
#     Cuando un perfil tenga el plan 'contratista',
#     se crea o actualiza un Candidato con la info del User y Perfil.
#     """
#     if instance.plan == "contratista":
#         user = instance.usuario

#         # Crear o actualizar el candidato
#         Candidato.objects.update_or_create(
#             cedula=user.username,  # 👈 aquí asumo que el username = cedula, cámbialo si manejas otro campo
#             defaults={
#                 "nombre": user.first_name,
#                 "apellido": user.last_name,
#                 "email": user.email,
#                 "profesion": "",  # podrías llenarlo desde otro campo si lo tienes
#                 "tipo_doc": None,
#                 "fecha_nacimiento": None,
#                 "fecha_expedicion": None,
#                 "tipo_persona": "natural",
#                 "sexo": None,
#             },
#         )
