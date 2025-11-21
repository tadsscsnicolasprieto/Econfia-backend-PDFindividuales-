from django.contrib import admin

# Register your models here.


from core import models

admin.site.register(models.Consulta)
admin.site.register(models.TipoFuente)

@admin.register(models.Fuente)
class FuenteAdmin(admin.ModelAdmin):
    search_fields = ("nombre", "nombre_pila", "tipo__nombre")
    list_display = ("id", "nombre", "nombre_pila", "tipo")  
    
from django.contrib import admin
from .models import Resultado

# Filtro personalizado para incluir también valores nulos en 'fuente'
class FuenteListFilter(admin.SimpleListFilter):
    title = "Fuente (incluye nulos)"
    parameter_name = "fuente"

    def lookups(self, request, model_admin):
        return [
            ("con_fuente", "Con fuente"),
            ("sin_fuente", "Sin fuente (NULL)"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "con_fuente":
            return queryset.filter(fuente__isnull=False)
        if self.value() == "sin_fuente":
            return queryset.filter(fuente__isnull=True)
        return queryset


@admin.register(Resultado)
class ResultadoAdmin(admin.ModelAdmin):
    list_display = ("id", "consulta", "fuente")
    list_filter = (FuenteListFilter,)
    search_fields = ("fuente__nombre",)  # 👈 barra de búsqueda por nombre de fuente


@admin.register(models.TipoConsolidado)
class TipoConsolidadoAdmin(admin.ModelAdmin):
    list_display = ("id", "nombre", "descripcion")
admin.site.register(models.Consolidado)
admin.site.register(models.Candidato)
admin.site.register(models.Perfil)