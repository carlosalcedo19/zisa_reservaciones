"""
La sala: sedes, zonas, mesas, combinaciones, turnos y calendario.

Todo lo que describe *donde* y *cuando* se puede sentar a alguien vive aqui.
Las reservas propiamente dichas estan en apps.reservations.

Los nombres del codigo van en ingles; las etiquetas que ve el usuario en el
panel van en espanol a traves de verbose_name.

Las claves primarias son UUID: el identificador de una reserva o de un
cliente acaba en un enlace que se manda por WhatsApp, y un correlativo
delata cuantas reservas hay y deja probar la de al lado.
"""

import uuid
from datetime import date, datetime

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Venue(models.Model):
    """
    Zisa opera un local, pero el modelo admite varios sin rehacer nada:
    todas las demas entidades cuelgan de una sede.
    """

    id = models.UUIDField("id", primary_key=True, default=uuid.uuid4,
                          editable=False)

    name = models.CharField("nombre", max_length=80)
    timezone = models.CharField("zona horaria", max_length=50, default="America/Lima")
    address = models.CharField("dirección", max_length=200, blank=True)
    phone = models.CharField("teléfono", max_length=25, blank=True)
    email = models.EmailField("correo", blank=True)
    is_active = models.BooleanField("activa", default=True)

    class Meta:
        verbose_name = "restaurante"
        verbose_name_plural = "restaurantes"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def capacity(self):
        """Suma de la capacidad maxima de las mesas activas."""
        return (
            Table.objects.filter(area__venue=self, is_active=True).aggregate(
                total=models.Sum("max_seats")
            )["total"]
            or 0
        )


class Area(models.Model):
    """Terraza, Salon, Barra, Privado."""

    id = models.UUIDField("id", primary_key=True, default=uuid.uuid4,
                          editable=False)

    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name="areas",
                              verbose_name="restaurante")
    name = models.CharField("nombre", max_length=50)
    is_bookable = models.BooleanField(
        "reservable",
        default=True,
        help_text="Desmárcalo en la barra o en zonas que se llenan solo con gente que llega sin reservar.",
    )
    sort_order = models.PositiveSmallIntegerField("orden", default=0)

    class Meta:
        verbose_name = "zona"
        verbose_name_plural = "zonas"
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["venue", "name"], name="unique_area_per_venue"),
        ]

    def __str__(self):
        return self.name


class Table(models.Model):
    """
    La unidad que se vende.

    min_seats importa tanto como max_seats: sentar a dos personas en la mesa
    de ocho es perder la mesa de ocho.
    """

    id = models.UUIDField("id", primary_key=True, default=uuid.uuid4,
                          editable=False)

    area = models.ForeignKey(Area, on_delete=models.PROTECT, related_name="tables",
                             verbose_name="zona")
    code = models.CharField("código", max_length=10, help_text="Como la llama el equipo: S1, T4, B2...")
    min_seats = models.PositiveSmallIntegerField("capacidad minima", default=1)
    max_seats = models.PositiveSmallIntegerField("capacidad maxima")
    is_combinable = models.BooleanField(
        "combinable",
        default=True,
        help_text="Se puede juntar con otras mesas para atender grupos grandes.",
    )
    pos_x = models.DecimalField(
        "posición X", max_digits=5, decimal_places=2, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Dónde está la mesa de izquierda (0) a derecha (100). Solo hace falta si quieres ver el plano dibujado.",
    )
    pos_y = models.DecimalField(
        "posición Y", max_digits=5, decimal_places=2, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Dónde está la mesa de arriba (0) a abajo (100).",
    )
    is_active = models.BooleanField("activa", default=True)

    class Meta:
        verbose_name = "mesa"
        verbose_name_plural = "mesas"
        ordering = ["area__sort_order", "code"]
        constraints = [
            models.UniqueConstraint(fields=["area", "code"], name="unique_table_per_area"),
            models.CheckConstraint(
                condition=models.Q(max_seats__gte=models.F("min_seats")),
                name="table_seat_range_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(min_seats__gt=0),
                name="table_min_seats_positive",
            ),
        ]

    def __str__(self):
        return f"{self.code} ({self.min_seats}-{self.max_seats})"

    def fits(self, party_size):
        return self.min_seats <= party_size <= self.max_seats


class TableCombination(models.Model):
    """
    Combinacion de mesas que puede venderse como una sola.

    Sin esto un grupo de diez nunca entra aunque haya dos mesas de seis
    pegadas. Declarar las combinaciones validas de antemano es mas simple y
    mas fiable que deducirlas por cercania en el plano.
    """

    id = models.UUIDField("id", primary_key=True, default=uuid.uuid4,
                          editable=False)

    venue = models.ForeignKey(Venue, on_delete=models.CASCADE,
                              related_name="combinations", verbose_name="restaurante")
    name = models.CharField("nombre", max_length=40, help_text="Cómo la llamas: «S3+S4».")
    tables = models.ManyToManyField(Table, related_name="combinations",
                                    verbose_name="mesas")
    min_seats = models.PositiveSmallIntegerField("capacidad minima", default=1)
    max_seats = models.PositiveSmallIntegerField("capacidad maxima")
    is_active = models.BooleanField("activa", default=True)

    class Meta:
        verbose_name = "combinación de mesas"
        verbose_name_plural = "combinaciones de mesas"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["venue", "name"],
                                    name="unique_combination_per_venue"),
        ]

    def __str__(self):
        return self.name


class Shift(models.Model):
    """
    Almuerzo, Cena.

    last_seating es el campo que casi siempre se olvida: el local cierra a las
    23:00 pero deja de aceptar entradas a las 21:45.
    """

    class Weekday(models.IntegerChoices):
        MONDAY = 0, "Lunes"
        TUESDAY = 1, "Martes"
        WEDNESDAY = 2, "Miércoles"
        THURSDAY = 3, "Jueves"
        FRIDAY = 4, "Viernes"
        SATURDAY = 5, "Sábado"
        SUNDAY = 6, "Domingo"

    id = models.UUIDField("id", primary_key=True, default=uuid.uuid4,
                          editable=False)

    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name="shifts",
                              verbose_name="restaurante")
    name = models.CharField("nombre", max_length=40)
    weekdays = models.CharField(
        "dias de la semana",
        max_length=13,
        default="0,1,2,3,4,5,6",
        help_text="Números separados por coma. 0 es lunes y 6 es domingo. Ejemplo: 4,5,6 para viernes, sábado y domingo.",
    )
    start_time = models.TimeField("hora de inicio")
    end_time = models.TimeField("hora de cierre")
    last_seating = models.TimeField("ultima entrada")
    default_duration_min = models.PositiveSmallIntegerField(
        "duración por defecto (min)",
        default=90,
        help_text="Cuánto dura una reserva de este turno si ningún tramo de la tabla de duraciones encaja con el grupo.",
    )
    sort_order = models.PositiveSmallIntegerField("orden", default=0)
    is_active = models.BooleanField("activo", default=True)

    class Meta:
        verbose_name = "turno"
        verbose_name_plural = "turnos"
        ordering = ["sort_order", "start_time"]
        constraints = [
            models.UniqueConstraint(fields=["venue", "name"], name="unique_shift_per_venue"),
        ]

    def __str__(self):
        return f"{self.name} {self.start_time:%H:%M}-{self.end_time:%H:%M}"

    @property
    def weekday_set(self):
        return {int(d) for d in self.weekdays.split(",") if d.strip().isdigit()}

    @property
    def crosses_midnight(self):
        return self.end_time <= self.start_time

    def applies_on(self, day: date) -> bool:
        return self.is_active and day.weekday() in self.weekday_set

    def contains(self, local_dt: datetime) -> bool:
        """True si una hora local cae dentro del turno, incluida la madrugada."""
        moment = local_dt.time()
        if self.crosses_midnight:
            return moment >= self.start_time or moment < self.end_time
        return self.start_time <= moment < self.end_time


class DurationRule(models.Model):
    """
    Cuanto ocupa una mesa segun el tamano del grupo.

    Una mesa de dos rota en 75 minutos y una de ocho ocupa dos horas y media.
    Con un unico numero global se subvende la sala o se encima a la gente.
    """

    id = models.UUIDField("id", primary_key=True, default=uuid.uuid4,
                          editable=False)

    venue = models.ForeignKey(Venue, on_delete=models.CASCADE,
                              related_name="duration_rules", verbose_name="restaurante")
    min_party = models.PositiveSmallIntegerField("comensales desde")
    max_party = models.PositiveSmallIntegerField("comensales hasta")
    minutes = models.PositiveSmallIntegerField("cuánto ocupan la mesa (min)")

    class Meta:
        verbose_name = "duración por tamaño de grupo"
        verbose_name_plural = "duración por tamaño de grupo"
        ordering = ["min_party"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(max_party__gte=models.F("min_party")),
                name="duration_rule_range_valid",
            ),
        ]

    def __str__(self):
        return f"{self.min_party}-{self.max_party} pax: {self.minutes} min"


class CalendarException(models.Model):
    """
    Feriados, horarios especiales, aforo reducido y eventos privados.

    Sin esta tabla cada excepcion termina siendo una edicion manual y apurada
    del turno, que despues nadie se acuerda de revertir.
    """

    class Kind(models.TextChoices):
        CLOSED = "closed", "Cerrado"
        SPECIAL_HOURS = "special_hours", "Horario especial"
        REDUCED_CAPACITY = "reduced_capacity", "Aforo reducido"

    id = models.UUIDField("id", primary_key=True, default=uuid.uuid4,
                          editable=False)

    venue = models.ForeignKey(Venue, on_delete=models.CASCADE,
                              related_name="calendar_exceptions", verbose_name="restaurante")
    start_date = models.DateField("desde")
    end_date = models.DateField("hasta")
    kind = models.CharField("tipo", max_length=20, choices=Kind.choices)
    start_time = models.TimeField("hora de inicio", null=True, blank=True)
    end_time = models.TimeField("hora de cierre", null=True, blank=True)
    max_capacity = models.PositiveSmallIntegerField("aforo maximo", null=True, blank=True)
    reason = models.CharField("motivo", max_length=120)

    class Meta:
        verbose_name = "día especial"
        verbose_name_plural = "días especiales"
        ordering = ["-start_date"]
        indexes = [models.Index(fields=["venue", "start_date", "end_date"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="calendar_exception_range_valid",
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.start_date} - {self.reason}"

    def covers(self, day: date) -> bool:
        return self.start_date <= day <= self.end_date
