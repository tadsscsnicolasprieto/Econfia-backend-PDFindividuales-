from decimal import Decimal
from django.db import models
from django.conf import settings

class Consulta(models.Model):
    candidato = models.ForeignKey(
        "Candidato",
        to_field="cedula",
        db_column="cedula",
        on_delete=models.CASCADE,
        related_name="consultas"
    )
    estado = models.CharField(max_length=20, default="pendiente")
    fecha = models.DateTimeField(auto_now_add=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="consultas")
    fuente = models.ForeignKey("Fuente", on_delete=models.SET_NULL, null=True, blank=True, related_name="consultas")

    def __str__(self):
        return f"Consulta {self.candidato.cedula} - {self.estado}"


class TipoFuente(models.Model):
    nombre = models.CharField(max_length=100, unique=True)
    peso = models.PositiveSmallIntegerField(default=1)  # importancia de la fuente (1-5)
    probabilidad = models.PositiveSmallIntegerField(default=1)  # probabilidad intrínseca (1-5)

    def __str__(self):
        return self.nombre



class Fuente(models.Model):
    tipo = models.ForeignKey(TipoFuente, on_delete=models.CASCADE, related_name="fuentes")
    nombre = models.CharField(max_length=100)
    nombre_pila = models.CharField(max_length=100)
    def __str__(self):
        return f"{self.nombre} ({self.tipo.nombre})"

class Resultado(models.Model):
    consulta = models.ForeignKey("Consulta", on_delete=models.CASCADE)
    fuente = models.ForeignKey(
        "Fuente",
        on_delete=models.CASCADE,
        related_name="resultados",
        null=True,
        blank=True
    )

    score = models.IntegerField(default=0)
    estado = models.CharField(max_length=20, default="pendiente")
    mensaje = models.TextField(blank=True)
    archivo = models.CharField(max_length=255, blank=True)

    def save(self, *args, **kwargs):
        # estado siempre en minúscula
        if self.estado:
            self.estado = self.estado.lower()
            if self.estado == "ok":
                self.estado = "validado"
            if self.estado == "error":
                self.estado = "offline"
            # convertir "validado" en "validada"
            if self.estado == "validada":
                self.estado = "validado"
        if self.estado == "sin validar":
            self.estado = "offline"
        # normalizar score
        if self.score is not None:
            self.score = int(self.score)

            conversion = {
                10: 5,
                8: 4,
                6: 3,
                2: 2,
                0: 1,
            }
            # si existe en el mapa lo reemplaza
            self.score = conversion.get(self.score, self.score)

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.consulta.candidato.cedula} - {self.fuente.nombre if self.fuente else 'Sin fuente'} ({self.estado})"


from django.contrib.auth.models import User
from django.db import models

class Perfil(models.Model):
    usuario = models.OneToOneField(User, on_delete=models.CASCADE, related_name='perfil')
    foto = models.ImageField(upload_to='perfiles/', null=True, blank=True)
    consultas_disponibles = models.PositiveIntegerField(default=0)
    plan = models.CharField(
        max_length=50,
        choices=[('sin_plan','Sin Plan'), ('premium','Premium'), ('contratista','Contratista')],
        default='sin_plan'
    )
    candidato = models.OneToOneField(
        'Candidato',
        to_field='cedula',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='perfil_vinculado',
        db_column='cedula_candidato'
    )

    def __str__(self):
        return f"Perfil de {self.usuario.username}"

    @property
    def es_candidato(self) -> bool:
        return self.candidato_id is not None


class TipoConsolidado(models.Model):
    nombre = models.CharField(max_length=100, unique=True)  # Ej: "Informe completo", "Por categoría", "Personalizado"
    descripcion = models.TextField(blank=True)

    def __str__(self):
        return self.nombre

class Consolidado(models.Model):
    consulta = models.ForeignKey(Consulta, on_delete=models.CASCADE, related_name="consolidados")
    tipo = models.ForeignKey(TipoConsolidado, on_delete=models.SET_NULL, null=True, related_name="consolidados")
    archivo = models.FileField(upload_to="consolidados/", blank=True, null=True)    
    qr = models.ImageField(upload_to="qrs/", blank=True, null=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"Consolidado {self.tipo.nombre if self.tipo else 'Desconocido'} - {self.consulta.candidato.cedula}"


class Candidato(models.Model):
    cedula = models.CharField(max_length=20, primary_key=True)
    tipo_doc = models.CharField(max_length=10, null=True, blank=True)
    nombre = models.CharField(max_length=100, null=True, blank=True)
    apellido = models.CharField(max_length=100, null=True, blank=True)
    fecha_nacimiento = models.DateField(null=True, blank=True)
    fecha_expedicion = models.DateField(null=True, blank=True)
    tipo_persona = models.CharField(max_length=50, null=True, blank=True)
    sexo = models.CharField(max_length=10, null=True, blank=True)

    # Nuevos campos
    email = models.EmailField(max_length=150, null=True, blank=True)
    profesion = models.CharField(max_length=100, null=True, blank=True)

    def __str__(self):
        return f"{self.nombre} {self.apellido} ({self.cedula})"
