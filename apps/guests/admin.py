from django.contrib import admin
from django.utils.html import format_html, format_html_join
from unfold.admin import ModelAdmin

from apps.guests.models import Guest, Tag

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


@admin.register(Guest)
class GuestAdmin(ModelAdmin):
    list_display = ("name", "phone", "tags_display", "visits", "no_shows_display",
                    "last_visit")
    list_filter = ("marketing_opt_in", "tags", "language")
    search_fields = ("name", "phone", "email")
    filter_horizontal = ("tags",)
    readonly_fields = ("visits", "no_shows", "cancellations", "last_visit",
                       "created_at", "updated_at")
    actions = ["action_refresh_counters"]
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
