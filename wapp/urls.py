"""
Rutas del proyecto.

Por ahora todo el trabajo ocurre en el admin de Unfold. La raiz redirige alli
para que nadie tenga que recordar /admin/.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from apps.reservations import api
from wapp import floor

# Titulo de /admin/ en la cabecera; por defecto Django pone "Sitio administrativo".
admin.site.index_title = "Panel de inicio"

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="admin:index", permanent=False)),
    # Antes de admin.site.urls, que si no se queda con cualquier /admin/...
    path("admin/plano/", admin.site.admin_view(floor.floor_view), name="floor"),
    path("admin/plano/posiciones/", admin.site.admin_view(floor.floor_positions),
         name="floor_positions"),
    path("admin/plano/sin-reserva/", admin.site.admin_view(floor.floor_walk_in),
         name="floor_walk_in"),
    # Formulario "Reserva una mesa" de zisa.pe (lo llama WordPress).
    path("api/reservas/web/", api.web_reservation, name="api_web_reservation"),
    # Tarea programada: la llama cron-job.org cada cinco minutos.
    path("api/tareas/no-shows/", api.sweep_no_shows_task, name="api_sweep_no_shows"),
    path("admin/", admin.site.urls),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
