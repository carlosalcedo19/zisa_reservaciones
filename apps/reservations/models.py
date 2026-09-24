"""
El centro del sistema.

Reservation guarda el *que* y el *cuando*; TableOccupancy guarda el *donde*, y
es la tabla sobre la que Postgres impide fisicamente el solapamiento.

Las claves primarias son UUID: el identificador de una reserva o de un
cliente acaba en un enlace que se manda por WhatsApp, y un correlativo
delata cuantas reservas hay y deja probar la de al lado.
"""

import secrets
import uuid

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import F, Func, Q

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # sin I, L, O, 0, 1


def generate_code(prefix="ZS", length=6):
    """Codigo publico corto, legible por telefono y sin caracteres ambiguos."""
    body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))
    return f"{prefix}-{body}"


class TsTzRange(Func):
    """tstzrange(starts_at, ends_at) para la restriccion de exclusion."""

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class ReservationQuerySet(models.QuerySet):
    def live(self):
        return self.filter(status__in=Reservation.LIVE_STATUSES)

    def for_service_date(self, day, venue=None):
        qs = self.filter(service_date=day)
        return qs.filter(venue=venue) if venue else qs

    def overlapping(self, starts_at, ends_at):
        return self.filter(starts_at__lt=ends_at, ends_at__gt=starts_at)


class Reservation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente de confirmar"
        CONFIRMED = "confirmed", "Confirmada"
        SEATED = "seated", "Sentada"
        FINISHED = "finished", "Finalizada"
        WAITLISTED = "waitlisted", "En lista de espera"
        CANCELLED = "cancelled", "Cancelada"
        NO_SHOW = "no_show", "No-show"

    class Source(models.TextChoices):
        WEB = "web", "Web"
        PHONE = "phone", "Teléfono"
        WHATSAPP = "whatsapp", "WhatsApp"
        WALK_IN = "walk_in", "Mostrador / walk-in"
        STAFF = "staff", "Personal"

    class DepositStatus(models.TextChoices):
        NOT_APPLICABLE = "not_applicable", "No aplica"
        PENDING = "pending", "Pendiente"
        PAID = "paid", "Pagado"
        REFUNDED = "refunded", "Devuelto"
        KEPT = "kept", "Retenido"

    #: Solo estos estados ocupan mesa. El resto libera el rango de inmediato.
    LIVE_STATUSES = (Status.PENDING, Status.CONFIRMED, Status.SEATED)

    #: Transiciones permitidas. La capa de servicio las hace cumplir.
    TRANSITIONS = {
        Status.WAITLISTED: {Status.PENDING, Status.CONFIRMED, Status.CANCELLED},
        Status.PENDING: {Status.CONFIRMED, Status.SEATED, Status.CANCELLED,
                         Status.NO_SHOW},
        Status.CONFIRMED: {Status.SEATED, Status.CANCELLED, Status.NO_SHOW},
        Status.SEATED: {Status.FINISHED},
        Status.FINISHED: set(),
        Status.CANCELLED: set(),
        Status.NO_SHOW: set(),
    }

    id = models.UUIDField("id", primary_key=True, default=uuid.uuid4,
                          editable=False)

    code = models.CharField("código", max_length=12, unique=True, editable=False)
    venue = models.ForeignKey("venues.Venue", on_delete=models.PROTECT,
                              related_name="reservations", verbose_name="restaurante")
    guest = models.ForeignKey("guests.Guest", on_delete=models.PROTECT,
                              related_name="reservations", verbose_name="cliente")
    shift = models.ForeignKey("venues.Shift", on_delete=models.SET_NULL, null=True,
                              blank=True, related_name="reservations",
                              verbose_name="turno")

    starts_at = models.DateTimeField("inicio")
    ends_at = models.DateTimeField(
        "fin",
        help_text="Hora a la que se libera la mesa. Se calcula sola.",
    )
    service_date = models.DateField(
        "fecha de servicio", db_index=True,
        help_text="Día al que pertenece el servicio. Una cena que acaba pasada la medianoche sigue contando como la noche anterior.",
    )
    duration_min = models.PositiveSmallIntegerField(
        "duración (min)",
        help_text="Cuánto ocupa la mesa esta reserva. Si más adelante cambias la duración estándar, esta reserva no se mueve.",
    )

    party_size = models.PositiveSmallIntegerField("comensales")
    children = models.PositiveSmallIntegerField("niños", default=0)
    high_chairs = models.PositiveSmallIntegerField("sillas de bebé", default=0)

    status = models.CharField("estado", max_length=12, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    source = models.CharField("origen", max_length=12, choices=Source.choices,
                              default=Source.WEB)
    occasion = models.CharField("ocasión", max_length=40, blank=True,
                                help_text="Cumpleaños, aniversario, cena de negocios... Es lo que permite el detalle que hace volver al cliente.")

    guest_notes = models.TextField("notas del cliente", blank=True)
    internal_notes = models.TextField(
        "notas internas", blank=True,
        help_text="Solo lo ve el equipo. Nunca se envía en el correo de confirmación.",
    )
    tags = models.ManyToManyField("guests.Tag", blank=True, related_name="reservations",
                                  verbose_name="etiquetas")

    deposit_amount = models.DecimalField("monto del depósito", max_digits=8,
                                         decimal_places=2, null=True, blank=True)
    deposit_status = models.CharField("estado del depósito", max_length=15,
                                      choices=DepositStatus.choices,
                                      default=DepositStatus.NOT_APPLICABLE)

    # Sellos por transicion. Con un unico campo de estado, la puntualidad, la
    # rotacion real y el no-show por franja no se pueden reconstruir despues.
    confirmed_at = models.DateTimeField("confirmada en", null=True, blank=True)
    seated_at = models.DateTimeField("sentada en", null=True, blank=True)
    released_at = models.DateTimeField("liberada en", null=True, blank=True)
    cancelled_at = models.DateTimeField("cancelada en", null=True, blank=True)
    cancellation_reason = models.CharField("motivo de cancelación", max_length=120,
                                           blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL,
                                   related_name="reservations_created",
                                   verbose_name="creada por")
    created_at = models.DateTimeField("creada en", auto_now_add=True)
    updated_at = models.DateTimeField("actualizada en", auto_now=True)

    objects = ReservationQuerySet.as_manager()

    class Meta:
        verbose_name = "reserva"
        verbose_name_plural = "reservas"
        ordering = ["starts_at"]
        indexes = [
            models.Index(fields=["service_date", "status"],
                         name="reservation_day_status_idx"),
            models.Index(fields=["starts_at"], name="reservation_start_idx"),
            models.Index(fields=["guest", "-starts_at"], name="reservation_guest_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=F("starts_at")),
                                   name="reservation_range_valid"),
            models.CheckConstraint(condition=Q(party_size__gt=0),
                                   name="reservation_party_size_positive"),
            models.CheckConstraint(condition=Q(children__lte=F("party_size")),
                                   name="reservation_children_within_party"),
        ]

    def __str__(self):
        return f"{self.code} - {self.guest.name} - {self.party_size}p"

    def save(self, *args, **kwargs):
        if not self.code:
            # Colision practicamente imposible (31^6), pero es barato reintentar.
            for _ in range(5):
                candidate = generate_code()
                if not Reservation.objects.filter(code=candidate).exists():
                    self.code = candidate
                    break
            else:
                raise RuntimeError("No se pudo generar un codigo de reserva unico.")
        super().save(*args, **kwargs)

    @property
    def is_live(self):
        return self.status in self.LIVE_STATUSES

    @property
    def tables(self):
        from apps.venues.models import Table

        return Table.objects.filter(occupancies__reservation=self,
                                    occupancies__is_active=True)

    @property
    def table_codes(self):
        # Si la consulta trajo las mesas con active_occupancies_prefetch()
        # se usan esas; si no, una consulta. En listas, siempre lo primero.
        precargadas = getattr(self, "active_occupancies", None)
        if precargadas is not None:
            return ", ".join(o.table.code for o in precargadas) or "sin asignar"
        return ", ".join(
            self.occupancies.filter(is_active=True)
            .select_related("table")
            .values_list("table__code", flat=True)
        ) or "sin asignar"

    def can_transition_to(self, new_status):
        return new_status in self.TRANSITIONS.get(self.status, set())


class TableOccupancy(models.Model):
    """
    Una mesa ocupada en un rango de tiempo.

    Cubre tanto las reservas como los bloqueos (mantenimiento, evento
    privado): si fueran dos tablas distintas, una sola restriccion de
    exclusion no podria cubrir ambas y un bloqueo no impediria una reserva
    encima. Con reservation = NULL la fila es un bloqueo.

    starts_at y ends_at estan duplicados desde Reservation a proposito: una
    restriccion de exclusion no puede leer columnas de otra tabla, asi que el
    rango tiene que vivir aqui. La capa de servicio los mantiene sincronizados
    dentro de la misma transaccion.
    """

    id = models.UUIDField("id", primary_key=True, default=uuid.uuid4,
                          editable=False)

    table = models.ForeignKey("venues.Table", on_delete=models.PROTECT,
                              related_name="occupancies", verbose_name="mesa")
    reservation = models.ForeignKey(Reservation, on_delete=models.CASCADE, null=True,
                                    blank=True, related_name="occupancies",
                                    verbose_name="reserva",
                                    help_text="Déjalo vacío si es un bloqueo de mesa (mantenimiento, evento privado) y no una reserva.")
    starts_at = models.DateTimeField("inicio")
    ends_at = models.DateTimeField("fin")
    is_primary = models.BooleanField(
        "principal", default=True,
        help_text="Cuando se juntan varias mesas, la principal es la que representa a la reserva en el plano.",
    )
    is_active = models.BooleanField(
        "activa", default=True, db_index=True,
        help_text="Se desmarca sola al cancelar, marcar no-show o cerrar la reserva. Solo las activas ocupan la mesa.",
    )
    block_reason = models.CharField("motivo del bloqueo", max_length=120, blank=True)
    assigned_at = models.DateTimeField("asignada en", auto_now_add=True)

    class Meta:
        verbose_name = "ocupación de mesa"
        verbose_name_plural = "ocupaciones de mesa"
        ordering = ["starts_at", "table__code"]
        indexes = [
            models.Index(fields=["table", "starts_at", "ends_at"],
                         name="occupancy_table_range_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=F("starts_at")),
                                   name="occupancy_range_valid"),
            models.CheckConstraint(
                condition=Q(reservation__isnull=False) | ~Q(block_reason=""),
                name="block_requires_reason",
            ),
          
            ExclusionConstraint(
                name="occupancy_no_overlap",
                expressions=[
                    ("table", RangeOperators.EQUAL),
                    (TsTzRange("starts_at", "ends_at"), RangeOperators.OVERLAPS),
                ],
                condition=Q(is_active=True),
            ),
        ]

    def __str__(self):
        label = (self.reservation.code if self.reservation_id
                 else f"bloqueo: {self.block_reason}")
        return f"{self.table.code} {self.starts_at:%d/%m %H:%M} - {label}"

    @property
    def is_block(self):
        return self.reservation_id is None


class ReservationEvent(models.Model):
    """
    Bitacora inmutable: solo se inserta, nunca se edita.

    Cuando un cliente reclama "yo si reserve" o dos personas del equipo se
    contradicen, esto es la respuesta.
    """

    id = models.UUIDField("id", primary_key=True, default=uuid.uuid4,
                          editable=False)

    reservation = models.ForeignKey(Reservation, on_delete=models.CASCADE,
                                    related_name="events", verbose_name="reserva")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL, verbose_name="usuario")
    from_status = models.CharField("estado anterior", max_length=12, blank=True)
    to_status = models.CharField("estado nuevo", max_length=12)
    comment = models.CharField("comentario", max_length=200, blank=True)
    created_at = models.DateTimeField("creado en", auto_now_add=True)

    class Meta:
        verbose_name = "movimiento"
        verbose_name_plural = "historial de movimientos"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["reservation", "-created_at"])]

    def __str__(self):
        origin = self.from_status or "nueva"
        return f"{self.reservation.code}: {origin} -> {self.to_status}"


class WaitlistEntry(models.Model):
    """
    Convierte un "no hay mesa" en una venta cuando alguien cancela a las seis
    de la tarde. Es la funcionalidad con mejor retorno del sistema y casi
    siempre queda fuera del alcance inicial.
    """

    id = models.UUIDField("id", primary_key=True, default=uuid.uuid4,
                          editable=False)

    venue = models.ForeignKey("venues.Venue", on_delete=models.CASCADE,
                              related_name="waitlist", verbose_name="restaurante")
    guest = models.ForeignKey("guests.Guest", on_delete=models.PROTECT,
                              related_name="waitlist_entries", verbose_name="cliente")
    date = models.DateField("fecha", db_index=True)
    desired_time = models.TimeField("hora deseada")
    flexibility_min = models.PositiveSmallIntegerField(
        "flexibilidad (min)", default=60,
        help_text="Cuántos minutos antes o después de esa hora le sirven al cliente.",
    )
    party_size = models.PositiveSmallIntegerField("comensales")
    priority = models.PositiveSmallIntegerField("prioridad", default=0)
    notes = models.CharField("notas", max_length=200, blank=True)
    notified_at = models.DateTimeField("avisado en", null=True, blank=True)
    resolved_with = models.ForeignKey(Reservation, null=True, blank=True,
                                      on_delete=models.SET_NULL,
                                      related_name="from_waitlist",
                                      verbose_name="resuelta con")
    created_at = models.DateTimeField("creada en", auto_now_add=True)

    class Meta:
        verbose_name = "cliente en lista de espera"
        verbose_name_plural = "lista de espera"
        ordering = ["date", "-priority", "created_at"]

    def __str__(self):
        return f"{self.guest.name} {self.date} {self.desired_time:%H:%M} ({self.party_size}p)"

    @property
    def is_resolved(self):
        return self.resolved_with_id is not None


class ReservationPolicy(models.Model):
    """
    Reglas que cambian por temporada. Si viven en el codigo, cada ajuste es un
    despliegue; si viven en una fila, el gerente las mueve solo.
    """

    id = models.UUIDField("id", primary_key=True, default=uuid.uuid4,
                          editable=False)

    venue = models.OneToOneField("venues.Venue", on_delete=models.CASCADE,
                                 related_name="policy", verbose_name="restaurante")
    min_lead_hours = models.PositiveSmallIntegerField(
        "antelación mínima (horas)", default=2,
        help_text="Con cuánta anticipación puede reservar alguien desde la web. Por teléfono o en mostrador no se aplica.",
    )
    max_lead_days = models.PositiveSmallIntegerField("antelación máxima (días)",
                                                     default=60)
    max_party_web = models.PositiveSmallIntegerField(
        "máximo de comensales por web", default=8,
        help_text="A partir de aquí, el grupo tiene que llamar. Así los eventos grandes los revisa alguien.",
    )
    no_show_grace_min = models.PositiveSmallIntegerField(
        "cuánto esperas antes del no-show (min)", default=15,
        help_text="Minutos de cortesía antes de marcar la reserva como no-show.",
    )
    overbooking_pct = models.PositiveSmallIntegerField("sobreventa permitida (%)",
                                                       default=0)
    web_auto_confirm = models.BooleanField(
        "confirmar sola la reserva web", default=True,
        help_text="Si lo desmarcas, cada reserva de la web queda pendiente hasta que alguien la revise.",
    )
    default_duration_min = models.PositiveSmallIntegerField(
        "duración por defecto (min)", default=90,
        help_text="Se usa cuando ningún tramo de la tabla de duraciones encaja con el grupo.",
    )

    class Meta:
        verbose_name = "reglas de reserva"
        verbose_name_plural = "reglas de reserva"

    def __str__(self):
        return f"Politica de {self.venue.name}"


def active_occupancies_prefetch():
    """
    Prefetch de las mesas activas de cada reserva, en reservation.active_occupancies.

    Reservation.table_codes lo aprovecha: usalo en cualquier lista que muestre
    las mesas, o cada fila hara su propia consulta.
    """
    return models.Prefetch(
        "occupancies",
        queryset=TableOccupancy.objects.filter(is_active=True)
        .select_related("table").order_by("table__code"),
        to_attr="active_occupancies",
    )
