from rest_framework import serializers
from django.contrib.auth.models import User
from .models import Consulta, Resultado, Perfil, Candidato, Fuente

class FuenteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Fuente
        fields = ["id", "nombre", "nombre_pila", "tipo"]

class PerfilSerializer(serializers.ModelSerializer):
    class Meta:
        model = Perfil
        fields = ['foto', 'consultas_disponibles', 'plan']
        
class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)
    full_name = serializers.SerializerMethodField()
    is_staff = serializers.BooleanField(read_only=True)
    is_superuser = serializers.BooleanField(read_only=True)
    last_login = serializers.DateTimeField(read_only=True)
    date_joined = serializers.DateTimeField(read_only=True)
    groups = serializers.StringRelatedField(many=True, read_only=True)

    perfil = PerfilSerializer(read_only=True)
    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'password',
            'first_name', 'last_name',
            'full_name', 'is_staff', 'is_superuser',
            'last_login', 'date_joined', 'groups',
            'perfil'
        ]


    def get_full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip()


class ConsultaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Consulta
        fields = ['id', 'cedula', 'estado', 'fecha']
        read_only_fields = ['id', 'estado', 'fecha']


# serializers.py
class ResultadoSerializer(serializers.ModelSerializer):
    fuente = serializers.CharField(source="fuente.nombre_pila", default=None)
    tipo_fuente = serializers.CharField(source="fuente.tipo.nombre", default=None)

    class Meta:
        model = Resultado
        fields = ["id", "consulta_id", "fuente", "tipo_fuente", "estado", "score", "mensaje", "archivo"]


class CandidatoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Candidato
        fields = [
            "cedula",
            "tipo_doc",
            "nombre",
            "apellido",
            "fecha_nacimiento",
            "fecha_expedicion",
            "tipo_persona",
            "sexo"
        ]

# Serializer de la consulta, incluyendo el candidato
class ConsultaDetalleSerializer(serializers.ModelSerializer):
    candidato = CandidatoSerializer(read_only=True)  # 👈 aquí se anida

    class Meta:
        model = Consulta
        fields = ["id", "estado", "fecha", "candidato"]