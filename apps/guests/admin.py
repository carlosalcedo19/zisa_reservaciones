from django import forms
from django.contrib import admin
from django.db import transaction
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from unfold.admin import ModelAdmin

from apps.guests.models import Guest, Tag
from apps.reservations.models import Reservation, WaitlistEntry
from wapp.excel import ExcelExportMixin, RangeForm, add_sheet, new_workbook

# Las etiquetas llevan el color que elige el equipo, asi que no pueden usar los
# tonos fijos de .zs-chip: solo toman su forma y le pasan el color por variable.
CHIP = '<span class="zs-chip zs-chip--tag" style="--tag:{}">{}</span>'


@admin.register(Tag)
class TagAdmin(ModelAdmin):
    list_display = ("chip", "kind", "show_in_kitchen")
    list_filter = ("kind", "show_in_kitchen")
    search_fields = ("name",)

    @admin.display(description="etiqueta", ordering="name")
    def chip(self, obj):
        return format_html(CHIP, obj.color, obj.name)


class GuestRangeForm(RangeForm):
    modo = forms.ChoiceField(
        label="Qué clientes", initial="reservaron", widget=forms.RadioSelect,
        choices=[("reservaron", "Los que tienen reservas en esas fechas"),
                 ("nuevos", "Los que se registraron en esas fechas")],
    )


@admin.register(Guest)
class GuestAdmin(ExcelExportMixin, ModelAdmin):
    list_display = ("name", "phone", "tags_display", "visits", "no_shows_display",
                    "last_visit")
    list_filter = ("marketing_opt_in", "tags", "language")
    search_fields = ("name", "phone", "email")
    filter_horizontal = ("tags",)
    readonly_fields = ("visits", "no_shows", "cancellations", "last_visit",
                       "created_at", "updated_at")
    actions = ["action_refresh_counters"]
    actions_list = ["export_excel"]
    excel_name = "clientes"
    excel_form = GuestRangeForm
    warn_unsaved_form = True
    fieldsets = (
        (None, {
            "fields": (("name", "phone"), ("email", "language")),
            "description": "Escribe el teléfono como te lo digan. Se guarda siempre "
                           "en el mismo formato, así no se duplican fichas.",
        }),
        ("Perfil", {
            "classes": ("tab",),
            "fields": ("tags", "marketing_opt_in", "notes"),
        }),
        ("Actividad del cliente", {
            "classes": ("tab",),
            "fields": (("visits", "no_shows"), ("cancellations", "last_visit")),
            "description": "Se actualizan solos al cerrar o cancelar una reserva. "
                           "Si algo no cuadra, usa la acción «Recalcular contadores».",
        }),
        ("Registro", {
            "classes": ("tab",),
            "fields": (("created_at", "updated_at"),),
        }),
    )

    @admin.display(description="etiquetas")
    def tags_display(self, obj):
        tags = obj.tags.all()[:4]
        if not tags:
            return "—"
        return format_html_join(
            " ", CHIP, ((t.color, t.name) for t in tags)
        )

    @admin.display(description="no-shows", ordering="no_shows")
    def no_shows_display(self, obj):
        if obj.no_shows >= 2:
            return format_html('<span class="zs-chip" data-tone="bad">{} · riesgo</span>',
                               obj.no_shows)
        return obj.no_shows

    @admin.action(description="Recalcular contadores desde el historial")
    def action_refresh_counters(self, request, queryset):
        for guest in queryset:
            guest.refresh_counters()
        self.message_user(request, f"{queryset.count()} clientes recalculados.")

    # Reserva y lista de espera protegen al cliente para que nadie lo borre sin
    # querer; el superadmin si puede, y se lleva su historial con el.
    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def _historial(self, guests):
        return (Reservation.objects.filter(guest__in=guests),
                WaitlistEntry.objects.filter(guest__in=guests))

    def get_deleted_objects(self, objs, request):
        objs = list(objs)
        borrados, cuenta, permisos, _ = super().get_deleted_objects(objs, request)
        cuenta, permisos, protegidos = dict(cuenta), set(permisos), []
        for qs in self._historial(objs):
            if not qs.exists():
                continue
            b, c, p, pr = self.admin_site._registry[qs.model].get_deleted_objects(qs, request)
            borrados += b
            permisos |= set(p)
            protegidos += pr
            for nombre, n in c.items():
                cuenta[nombre] = cuenta.get(nombre, 0) + n
        return borrados, cuenta, permisos, protegidos

    @transaction.atomic
    def delete_model(self, request, obj):
        for qs in self._historial([obj]):
            qs.delete()
        super().delete_model(request, obj)

    @transaction.atomic
    def delete_queryset(self, request, queryset):
        for qs in self._historial(queryset):
            qs.delete()
        super().delete_queryset(request, queryset)

    def excel_filter(self, queryset, data):
        if data["modo"] == "nuevos":
            return queryset.filter(created_at__date__range=(data["desde"], data["hasta"]))
        return queryset.filter(
            reservations__service_date__range=(data["desde"], data["hasta"])).distinct()

    def excel_workbook(self, request, queryset):
        wb = new_workbook()
        add_sheet(wb, "Clientes", [
            ("Nombre", 28, None), ("Teléfono", 16, None), ("Correo", 28, None),
            ("Idioma", 8, None), ("Etiquetas", 30, None), ("Visitas", 9, None),
            ("No-shows", 10, None), ("Cancelaciones", 13, None),
            ("Última visita", 13, "date"), ("Acepta marketing", 16, None),
            ("Notas internas", 40, None), ("Cliente desde", 13, "date"),
        ], (
            [g.name, g.phone, g.email, g.language, ", ".join(t.name for t in g.tags.all()),
             g.visits, g.no_shows, g.cancellations, g.last_visit,
             "Sí" if g.marketing_opt_in else "No", g.notes,
             timezone.localtime(g.created_at).date()]
            for g in queryset.prefetch_related("tags")
        ))
        return wb
