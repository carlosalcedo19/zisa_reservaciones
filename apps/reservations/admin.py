"""Admin de reservas. Toda escritura pasa por apps.reservations.services, nunca por el ModelForm."""

import uuid
from datetime import timedelta
from functools import lru_cache

from django import forms
from django.apps import apps as django_apps
from django.conf import settings
from django.contrib import admin, messages
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from unfold.admin import ModelAdmin, TabularInline
from unfold.contrib.filters.admin import RangeDateFilter

from apps.reservations import services
from apps.guests.models import Guest
from apps.venues.models import Venue
from apps.reservations.models import (
    Reservation,
    ReservationEvent,
    ReservationPolicy,
    TableOccupancy,
    WaitlistEntry,
    active_occupancies_prefetch,
)

STATUS_TONES = {
    Reservation.Status.PENDING: "warn",
    Reservation.Status.CONFIRMED: "info",
    Reservation.Status.SEATED: "ok",
    Reservation.Status.FINISHED: "muted",
    Reservation.Status.WAITLISTED: "info",
    Reservation.Status.CANCELLED: "muted",
    Reservation.Status.NO_SHOW: "bad",
}

# Unico boton rapido en lista y panel; el resto de transiciones quedan en la ficha.
NEXT_STEP = {
    Reservation.Status.WAITLISTED: ("confirm", "Confirmar", "check"),
    Reservation.Status.PENDING: ("confirm", "Confirmar", "check"),
    Reservation.Status.CONFIRMED: ("seat", "Sentar", "chair"),
    Reservation.Status.SEATED: ("finish", "Liberar", "task_alt"),
}


def chip(text, tone="muted"):
    return format_html('<span class="zs-chip" data-tone="{}">{}</span>', tone, text)


_PK_MUESTRA = str(uuid.UUID(int=0))


@lru_cache(maxsize=None)
def _url_molde(nombre):
    # reverse() es lento cientos de veces por pagina: se resuelve una vez y se sustituye el id.
    return reverse(f"admin:reservations_reservation_{nombre}", args=[_PK_MUESTRA])


def reservation_url(nombre, pk):
    return _url_molde(nombre).replace(_PK_MUESTRA, str(pk))


def next_step_button(reservation, back_url):
    paso = NEXT_STEP.get(reservation.status)
    if paso is None:
        return ""
    nombre, etiqueta, icono = paso
    return format_html(
        '<a class="zs-step" href="{}?{}">'
        '<span class="material-symbols-outlined">{}</span>{}</a>',
        reservation_url(nombre, reservation.pk), urlencode({"next": back_url}),
        icono, etiqueta,
    )


def default_venue():
    activos = Venue.objects.filter(is_active=True).order_by("name")
    return (activos.filter(name__iexact=settings.DEFAULT_VENUE_NAME).first()
            or activos.first())


class ReservationForm(forms.ModelForm):
    """Valida antes de guardar para que un choque salga como error de campo y no como IntegrityError."""

    class Meta:
        model = Reservation
        # Explicitos: un campo nuevo del modelo no entra en el formulario sin decidirlo.
        fields = (
            "venue", "guest", "shift",
            "starts_at", "ends_at", "service_date", "duration_min",
            "party_size", "children", "high_chairs",
            "status", "source", "occasion",
            "guest_notes", "internal_notes", "tags",
            "deposit_amount", "deposit_status",
            "confirmed_at", "seated_at", "released_at", "cancelled_at",
            "cancellation_reason", "created_by",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # self.initial y no field.initial: los valores de la instancia vacia ganan a field.initial.
        pedido = kwargs.get("initial") or {}
        # El pk UUID tiene default, asi que el alta se detecta con _state, no con pk.
        if self.instance._state.adding:
            if "source" in self.fields and "source" not in pedido:
                self.initial["source"] = Reservation.Source.STAFF
            if "venue" in self.fields and "venue" not in pedido:
                venue = default_venue()
                if venue is not None:
                    self.initial["venue"] = venue.pk

    def clean(self):
        cleaned = super().clean()
        venue = cleaned.get("venue")
        starts_at = cleaned.get("starts_at")
        party_size = cleaned.get("party_size")

        if not (venue and starts_at and party_size):
            return cleaned

        es_alta = self.instance._state.adding
        if not es_alta and "starts_at" not in self.changed_data:
            return cleaned

        duracion = services.duration_for(venue, party_size)
        ends_at = starts_at + timedelta(minutes=duracion)

        try:
            if es_alta:
                services.validate_lead_time(
                    venue, starts_at, cleaned.get("source") or Reservation.Source.STAFF
                )
            services.validate_calendar(venue, starts_at)

            excluir = None if es_alta else self.instance
            if not services.find_availability(venue, starts_at, ends_at, party_size,
                                              exclude_reservation=excluir):
                raise services.NoAvailability(
                    f"No queda mesa libre para {party_size} "
                    f"{'persona' if party_size == 1 else 'personas'} a esa hora."
                )
        except services.ReservationError as exc:
            raise forms.ValidationError({"starts_at": str(exc)}) from exc

        return cleaned


class OccupancyInline(TabularInline):
    model = TableOccupancy
    extra = 0
    tab = True
    fields = ("table", "starts_at", "ends_at", "is_primary", "is_active")
    readonly_fields = ("starts_at", "ends_at", "assigned_at")
    verbose_name = "mesa asignada"
    verbose_name_plural = "mesas asignadas"

    def has_add_permission(self, request, obj=None):
        # Asignar mesas pasa por services.assign_tables, no por el inline.
        return False


class EventInline(TabularInline):
    model = ReservationEvent
    extra = 0
    tab = True
    fields = ("created_at", "user", "from_status", "to_status", "comment")
    readonly_fields = fields
    verbose_name = "movimiento"
    verbose_name_plural = "movimientos"
    ordering = ("-created_at",)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Reservation)
class ReservationAdmin(ModelAdmin):
    form = ReservationForm
    # get_list_display anade la columna del siguiente paso tras "status_chip".
    list_display = ("when", "guest_display", "party_size", "tables_display",
                    "status_chip", "source", "code")
    list_display_links = ("when", "guest_display")
    warn_unsaved_form = True
    list_filter = (
        "status",
        "source",
        "venue",
        "shift",
        ("service_date", RangeDateFilter),
    )
    search_fields = ("code", "guest__name", "guest__phone")
    date_hierarchy = "service_date"
    autocomplete_fields = ("guest",)
    filter_horizontal = ("tags",)
    list_select_related = ("guest", "venue", "shift")

    def get_queryset(self, request):
        # Evita una consulta por fila en la columna "mesas".
        return super().get_queryset(request).prefetch_related(active_occupancies_prefetch())
    actions = ["action_confirm", "action_seat", "action_finish", "action_no_show",
               "action_cancel"]

    # Mesa, hora de fin, dia de servicio y duracion los calcula la capa de servicio.
    add_fieldsets = (
        (None, {
            "fields": (("venue", "source"), "guest"),
            "description": "Busca al cliente por nombre o teléfono. Si es nuevo, "
                           "créalo primero desde Clientes.",
        }),
        ("Fecha y comensales", {
            "fields": ("starts_at", ("party_size", "children", "high_chairs")),
            "description": "Con la hora y el tamaño del grupo basta: el sistema "
                           "elige la mesa que mejor encaja y calcula hasta cuándo "
                           "la ocupa. Si no hay hueco, te avisa aquí mismo.",
        }),
        ("Información adicional", {
            "fields": ("occasion", "tags", "guest_notes", "internal_notes")}),
    )

    # Primer bloque sin "tab": Unfold lo fija como cabecera y el resto van en pestanas.
    fieldsets = (
        (None, {"fields": (("code", "status"), ("venue", "guest"), "source")}),
        ("Fecha y horario", {
            "classes": ("tab",),
            "fields": (("starts_at", "ends_at"),
                       ("duration_min", "service_date"),
                       "shift"),
            "description": "Si cambias la hora de inicio, se revisa que la mesa "
                           "siga libre y la reserva se mueve con su mesa. El resto "
                           "se recalcula solo.",
        }),
        ("Comensales", {
            "classes": ("tab",),
            "fields": (("party_size", "children", "high_chairs"), "occasion", "tags"),
        }),
        ("Observaciones", {
            "classes": ("tab",),
            "fields": ("guest_notes", "internal_notes"),
            "description": "Lo que pidió el cliente y lo que anota el equipo van "
                           "por separado. Las notas internas no salen nunca de aquí.",
        }),
        ("Depósito", {
            "classes": ("tab",),
            "fields": (("deposit_amount", "deposit_status"),),
        }),
    )

    def get_fieldsets(self, request, obj=None):
        return self.add_fieldsets if obj is None else self.fieldsets

    def get_inlines(self, request, obj):
        return [] if obj is None else [OccupancyInline, EventInline]

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return []
        # El estado se cambia con las acciones, que registran el movimiento.
        return ["code", "status", "ends_at", "service_date", "shift", "duration_min"]

    # Partes de la reserva: se borran con ella sin pedir su permiso propio
    # (ReservationEvent no lo tiene a proposito).
    PARTES_DE_LA_RESERVA = ("reservations.TableOccupancy", "reservations.ReservationEvent",
                            "notifications.Notification")

    def get_deleted_objects(self, objs, request):
        borrados, cuenta, permisos, protegidos = super().get_deleted_objects(objs, request)
        partes = {django_apps.get_model(m)._meta.verbose_name for m in self.PARTES_DE_LA_RESERVA}
        return borrados, cuenta, set(permisos) - partes, protegidos

    def _refresh_guests(self, guest_ids):
        # Los contadores salen del historial: sin recalcular quedan inflados.
        for guest in Guest.objects.filter(pk__in=guest_ids):
            guest.refresh_counters()

    def delete_model(self, request, obj):
        guest_id = obj.guest_id
        super().delete_model(request, obj)
        self._refresh_guests([guest_id])

    def delete_queryset(self, request, queryset):
        guest_ids = list(queryset.values_list("guest_id", flat=True).distinct())
        super().delete_queryset(request, queryset)
        self._refresh_guests(guest_ids)

    def save_model(self, request, obj, form, change):
        if not change:
            reserva = services.create_reservation(
                venue=obj.venue,
                guest=obj.guest,
                starts_at=obj.starts_at,
                party_size=obj.party_size,
                source=obj.source,
                details=services.ReservationDetails(
                    children=obj.children or 0,
                    high_chairs=obj.high_chairs or 0,
                    occasion=obj.occasion or "",
                    guest_notes=obj.guest_notes or "",
                    internal_notes=obj.internal_notes or "",
                ),
                user=request.user,
            )
            # El admin sigue usando `obj` (mensaje, redireccion, etiquetas).
            obj.pk = reserva.pk
            for campo in ("code", "ends_at", "service_date", "duration_min",
                          "status", "shift_id", "confirmed_at", "created_by_id",
                          "created_at", "updated_at"):
                setattr(obj, campo, getattr(reserva, campo))
            self.message_user(
                request,
                f"Mesa {reserva.table_codes} asignada, "
                f"hasta las {timezone.localtime(reserva.ends_at, services.venue_tz(reserva.venue)):%H:%M}.",
                messages.INFO,
            )
            return

        anterior = Reservation.objects.get(pk=obj.pk)
        nueva_hora = obj.starts_at

        # El cambio de hora lo aplica services.move, que revalida la mesa.
        obj.starts_at = anterior.starts_at
        obj.ends_at = anterior.ends_at
        obj.service_date = anterior.service_date
        obj.save()

        if nueva_hora != anterior.starts_at:
            try:
                services.move(obj, nueva_hora, user=request.user)
                obj.refresh_from_db()
            except services.ReservationError as exc:
                self.message_user(request, f"No se pudo mover: {exc}", messages.ERROR)


    # URLs propias en vez de actions_detail de Unfold, para pintarlas al pie
    # y solo las transiciones validas.
    MOTIVOS_CANCELACION = [
        "El cliente llamó para cancelar",
        "El cliente no contesta",
        "Cambio de fecha u hora",
        "Cerramos por imprevisto",
    ]

    ACCIONES = (
        # (ruta, nombre de url, etiqueta, icono, tono, estado al que lleva)
        ("confirmar", "confirm", "Confirmar", "check", "ok", Reservation.Status.CONFIRMED),
        ("sentar", "seat", "Sentar", "chair", "primary", Reservation.Status.SEATED),
        ("finalizar", "finish", "Finalizar", "task_alt", "", Reservation.Status.FINISHED),
        ("no-show", "noshow", "No-show", "person_off", "warn", Reservation.Status.NO_SHOW),
        ("cancelar", "cancel", "Cancelar", "cancel", "danger", Reservation.Status.CANCELLED),
    )

    def get_urls(self):
        propias = [
            path("<uuid:object_id>/confirmar/",
                 self.admin_site.admin_view(self.view_confirm),
                 name="reservations_reservation_confirm"),
            path("<uuid:object_id>/sentar/",
                 self.admin_site.admin_view(self.view_seat),
                 name="reservations_reservation_seat"),
            path("<uuid:object_id>/finalizar/",
                 self.admin_site.admin_view(self.view_finish),
                 name="reservations_reservation_finish"),
            path("<uuid:object_id>/no-show/",
                 self.admin_site.admin_view(self.view_no_show),
                 name="reservations_reservation_noshow"),
            path("<uuid:object_id>/cancelar/",
                 self.admin_site.admin_view(self.view_cancel),
                 name="reservations_reservation_cancel"),
        ]
        return propias + super().get_urls()

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        extra_context = extra_context or {}
        if object_id:
            reserva = self.get_object(request, object_id)
            if reserva is not None:
                extra_context["reservation_obj"] = reserva
                extra_context["reservation_tone"] = STATUS_TONES.get(reserva.status, "muted")
                extra_context["reservation_actions"] = [
                    {
                        "url": reverse(f"admin:reservations_reservation_{nombre}",
                                       args=[reserva.pk]),
                        "label": etiqueta,
                        "icon": icono,
                        "tone": tono,
                    }
                    for _, nombre, etiqueta, icono, tono, destino in self.ACCIONES
                    if reserva.can_transition_to(destino)
                ]
        return super().changeform_view(request, object_id, form_url, extra_context)

    def get_list_display(self, request):
        # Por peticion: el ModelAdmin es compartido entre hilos.
        volver = request.get_full_path()

        @admin.display(description="siguiente paso")
        def next_step(obj):
            return next_step_button(obj, volver)

        columnas = []
        for columna in super().get_list_display(request):
            columnas.append(columna)
            if columna == "status_chip":
                columnas.append(next_step)
        return tuple(columnas)

    def _back(self, object_id, request=None):
        # ?next= lo ponen los botones rapidos para volver a la lista o al panel.
        siguiente = request.GET.get("next") if request else None
        if siguiente and url_has_allowed_host_and_scheme(
            siguiente, allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(siguiente)
        return redirect(
            reverse("admin:reservations_reservation_change", args=[object_id])
        )

    def _apply(self, request, object_id, funcion, hecho):
        reserva = self.get_object(request, object_id)
        if reserva is None:
            self.message_user(request, "La reserva ya no existe.", messages.ERROR)
            return redirect(reverse("admin:reservations_reservation_changelist"))
        try:
            funcion(reserva, user=request.user)
            self.message_user(request, f"{reserva.code}: {hecho}.", messages.SUCCESS)
        except services.ReservationError as exc:
            self.message_user(request, str(exc), messages.ERROR)
        return self._back(object_id, request)

    def view_confirm(self, request, object_id):
        return self._apply(request, object_id, services.confirm, "confirmada")

    def view_seat(self, request, object_id):
        return self._apply(request, object_id, services.seat, "sentada")

    def view_finish(self, request, object_id):
        return self._apply(request, object_id, services.finish,
                           "finalizada, mesa liberada")

    def view_no_show(self, request, object_id):
        return self._apply(request, object_id, services.mark_no_show,
                           "marcada como no-show")

    def view_cancel(self, request, object_id):
        reserva = self.get_object(request, object_id)
        if reserva is None:
            self.message_user(request, "La reserva ya no existe.", messages.ERROR)
            return redirect(reverse("admin:reservations_reservation_changelist"))

        if request.method == "POST":
            preset = request.POST.get("preset", "").strip()
            detalle = request.POST.get("reason", "").strip()
            motivo = " - ".join(x for x in (preset, detalle) if x) or "Sin motivo indicado"
            mesas = reserva.table_codes
            try:
                services.cancel(reserva, reason=motivo, user=request.user)
                self.message_user(
                    request,
                    f"{reserva.code} cancelada. Mesa {mesas} disponible de nuevo.",
                    messages.SUCCESS,
                )
            except services.ReservationError as exc:
                self.message_user(request, str(exc), messages.ERROR)
            return self._back(object_id)

        contexto = {
            **self.admin_site.each_context(request),
            "title": f"Cancelar {reserva.code}",
            "reservation": reserva,
            "local_start": timezone.localtime(
                reserva.starts_at, services.venue_tz(reserva.venue)
            ),
            "reasons": self.MOTIVOS_CANCELACION,
            "back_url": reverse("admin:reservations_reservation_change",
                                args=[object_id]),
        }
        return render(request, "admin/reservations/cancel.html", contexto)

    @admin.display(description="hora", ordering="starts_at")
    def when(self, obj):
        # Hora del local: astimezone() sin argumento usa la zona del sistema.
        local = timezone.localtime(obj.starts_at, services.venue_tz(obj.venue))
        return format_html(
            '<span class="zs-cell"><b class="zs-cell-time">{}</b><small>{}</small></span>',
            f"{local:%H:%M}", date_format(obj.service_date, "D d/m").capitalize(),
        )

    @admin.display(description="cliente", ordering="guest__name")
    def guest_display(self, obj):
        riesgo = ""
        if obj.guest.is_risky:
            riesgo = chip(f"{obj.guest.no_shows} no-shows", "bad")
        return format_html(
            '<span class="zs-cell"><b>{} {}</b><small>{}</small></span>',
            obj.guest.name, riesgo, obj.guest.phone,
        )

    @admin.display(description="mesas")
    def tables_display(self, obj):
        return obj.table_codes

    @admin.display(description="estado", ordering="status")
    def status_chip(self, obj):
        return chip(obj.get_status_display(), STATUS_TONES.get(obj.status, "muted"))

    def _run(self, request, queryset, func, verb):
        done, failures = 0, []
        for reservation in queryset:
            try:
                func(reservation, user=request.user)
                done += 1
            except services.ReservationError as exc:
                failures.append(f"{reservation.code}: {exc}")
        if done:
            self.message_user(request, f"{done} reservas: {verb}.", messages.SUCCESS)
        for failure in failures:
            self.message_user(request, failure, messages.WARNING)

    @admin.action(description="Confirmar")
    def action_confirm(self, request, queryset):
        self._run(request, queryset, services.confirm, "confirmadas")

    @admin.action(description="Sentar")
    def action_seat(self, request, queryset):
        self._run(request, queryset, services.seat, "sentadas")

    @admin.action(description="Finalizar (liberar mesa)")
    def action_finish(self, request, queryset):
        self._run(request, queryset, services.finish, "finalizadas")

    @admin.action(description="Marcar no-show")
    def action_no_show(self, request, queryset):
        self._run(request, queryset, services.mark_no_show, "marcadas como no-show")

    @admin.action(description="Cancelar")
    def action_cancel(self, request, queryset):
        def _cancel(reservation, user=None):
            return services.cancel(reservation, reason="Cancelada desde el panel",
                                   user=user)

        self._run(request, queryset, _cancel, "canceladas")


@admin.register(TableOccupancy)
class TableOccupancyAdmin(ModelAdmin):
    list_display = ("table", "slot", "kind", "reservation", "is_primary", "is_active")
    list_filter = ("is_active", "table__area__venue", "table__area", "is_primary")
    search_fields = ("table__code", "reservation__code", "block_reason")
    autocomplete_fields = ("table", "reservation")
    list_select_related = ("table", "reservation")
    date_hierarchy = "starts_at"

    @admin.display(description="franja", ordering="starts_at")
    def slot(self, obj):
        start = obj.starts_at.astimezone()
        end = obj.ends_at.astimezone()
        return f"{start:%d/%m %H:%M} - {end:%H:%M}"

    @admin.display(description="tipo")
    def kind(self, obj):
        if obj.is_block:
            return format_html("{} {}", chip("Bloqueo", "warn"), obj.block_reason)
        return chip("Reserva", "info")


@admin.register(ReservationEvent)
class ReservationEventAdmin(ModelAdmin):
    list_display = ("created_at", "reservation", "from_status", "to_status", "user",
                    "comment")
    list_filter = ("to_status",)
    search_fields = ("reservation__code", "comment")
    date_hierarchy = "created_at"
    list_select_related = ("reservation", "user")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WaitlistEntry)
class WaitlistEntryAdmin(ModelAdmin):
    list_display = ("guest", "date", "desired_time", "party_size", "flexibility_min",
                    "priority", "state")
    list_filter = ("venue", "date")
    search_fields = ("guest__name", "guest__phone")
    autocomplete_fields = ("guest", "resolved_with")
    date_hierarchy = "date"
    list_select_related = ("guest", "resolved_with")

    @admin.display(description="estado")
    def state(self, obj):
        if obj.is_resolved:
            return chip("Resuelta", "ok")
        if obj.notified_at:
            return chip("Avisada", "info")
        return chip("En espera", "warn")


@admin.register(ReservationPolicy)
class ReservationPolicyAdmin(ModelAdmin):
    list_display = ("venue", "min_lead_hours", "max_lead_days", "max_party_web",
                    "no_show_grace_min", "web_auto_confirm")
    fieldsets = (
        (None, {"fields": ("venue",)}),
        ("Reservas por la web", {
            "fields": (("min_lead_hours", "max_lead_days"),
                       ("max_party_web", "web_auto_confirm")),
        }),
        ("Operación del servicio", {
            "fields": (("no_show_grace_min", "overbooking_pct"),
                       "default_duration_min"),
            "description": "La cortesía es lo que esperas a alguien que no llega "
                           "antes de marcar su reserva como no-show.",
        }),
    )

    def has_add_permission(self, request):
        # Una por restaurante: solo mientras quede alguno sin reglas.
        return (request.user.is_superuser
                and Venue.objects.filter(policy__isnull=True).exists())

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "venue":
            kwargs["queryset"] = Venue.objects.filter(policy__isnull=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_readonly_fields(self, request, obj=None):
        return ("venue",) if obj else ()
