# core/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('api/consultar/', views.api_consultar),
    path("api/consultas/", views.listar_consultas, name="listar_consultas"),
    path("api/consultas/<int:consulta_id>/", views.detalle_consulta, name="detalle_consulta"),
    path("api/resultados/<int:consulta_id>/", views.listar_resultados, name="listar_resultados"),
    path("api/calcular_riesgo/<int:consulta_id>/", views.calcular_riesgo, name="calcular_riesgo"),
    path("api/dashboard/resumen/", views.resumen, name="resumen"),
    path("api/descargar_pdf/<int:consulta_id>/", views.descargar_reporte, name="descargar_pdf"),
    path("api/login/",views.login, name="login"),
    path("api/register/", views.register, name="register"), 
    path("api/activar/<uidb64>/<token>/", views.activate, name="activate"), 
    path("api/profile/", views.perfil, name="profile"),
    path("api/profile-stats/", views.resumen_usuario, name="profile"),
    path("api/reporteria/<int:consulta_id>/", views.reporte, name="reporte"),
    path("api/unificar-resultados/", views.unificar_resultados, name="unificar_resultados"),
    path("api/mapa-riesgo/<int:consulta_id>/", views.generar_mapa_calor, name="mapa_riesgo"),
    path("api/burbuja-riesgo/<int:consulta_id>/", views.generar_bubble_chart, name="burbuja_riesgo"),
    path("api/generar_consolidado/<int:consulta_id>/<int:tipo_id>/", views.generar_consolidado, name="generar_consolidado"),
    path("api/generar_consolidado_descarga/<int:consulta_id>/<int:tipo_id>/", views.generar_consolidado_descarga, name="generar_consolidado"),
    path("api/generar_consolidado_full/<int:consulta_id>/<int:tipo_id>/", views.descargar_consolidado_categoria, name="generar_consolidado"),
    path("api/consolidado/<int:consulta_id>/<int:tipo_id>/", views.generar_consolidado_api, name="consolidado_api"),
    path("api/relanzar_bot/<int:resultado_id>/", views.api_reintentar_bot, name="reintentar_bot"),
    path("api/fuentes/", views.listar_fuentes, name="listar_fuentes"),  
    path("api/resumen-consulta/<int:consulta_id>/", views.resumen_consulta, name="vista_resumen_consulta"),
    path("prueba", views.calcular_riesgo_interno),
    path("api/auth/password-reset/", views.password_reset_request, name="password_reset_request"),
    path("api/auth/password-reset/confirm/", views.password_reset_confirm, name="password_reset_confirm"),
    path("api/contratista", views.api_mi_candidato),
    # Aquí irán los graficos endpoint

    path("api/estado-3d/<int:consulta_id>/", views.generar_grafico_3d),
    path("api/cilindros-3d/<int:consulta_id>/", views.generar_grafico_cilindros),
    
    
    path("api/test-email/", views.test_email)
]
