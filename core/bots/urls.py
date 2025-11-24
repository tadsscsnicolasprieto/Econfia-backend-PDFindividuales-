# core/urls.py
from django.urls import path
from core.bots import views

urlpatterns = [
    path('api/consultar/', views.api_consultar),
    path('api/resultados/<int:consulta_id>/', views.api_resultados),
    path('api/resultados/no_ok/<int:consulta_id>/', views.api_resultados_no_ok),
    path('api/resultados/ok/<int:consulta_id>/', views.api_resultados_ok),
]
