"""
Clientes y etiquetas.

El telefono normalizado a E.164 es la llave real, no el correo: la gente
reserva por telefono, y sin normalizar se acumulan tres fichas del mismo
cliente (987654321, +51 987 654 321, 051987654321).

Las claves primarias son UUID: el identificador de una reserva o de un
cliente acaba en un enlace que se manda por WhatsApp, y un correlativo
delata cuantas reservas hay y deja probar la de al lado.
"""

import re
import uuid

from django.core.exceptions import ValidationError
from django.db import models

# Prefijo por defecto para numeros locales sin codigo de pais.
DEFAULT_COUNTRY_CODE = "+51"


def normalize_phone(value: str, country_code: str = DEFAULT_COUNTRY_CODE) -> str:
    """
    Devuelve el numero en formato E.164.

    Acepta las formas que escribe la gente al telefono: con espacios, guiones,
    parentesis, 00 o + delante, y con o sin codigo de pais.
    """
    if not value:
        return ""

    raw = value.strip()
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)

    if not digits:
        raise ValidationError("El teléfono no contiene dígitos.")

    if raw.startswith("00"):
        digits = digits[2:]
        has_plus = True

    if has_plus:
        number = f"+{digits}"
    elif digits.startswith(country_code.lstrip("+")) and len(digits) > 9:
        number = f"+{digits}"
    else:
        number = f"{country_code}{digits.lstrip('0')}"

    if not 8 <= len(number) - 1 <= 15:
        raise ValidationError(f"Telefono con longitud invalida: {value}")

    return number


class Tag(models.Model):
    """
    Alergias, preferencias y perfil.

    Se enlaza tanto al cliente como a la reserva: una alergia puede ser del
    acompanante de esta noche y no del titular.
    """

    class Kind(models.TextChoices):
        ALLERGY = "allergy", "Alergia o dieta"
        PREFERENCE = "preference", "Preferencia"
        PROFILE = "profile", "Perfil"

    id = models.UUIDField("id", primary_key=True, default=uuid.uuid4,
                          editable=False)

    name = models.CharField("nombre", max_length=40, unique=True)
    kind = models.CharField("tipo", max_length=12, choices=Kind.choices)
    color = models.CharField("color", max_length=7, default="#6B7280",
                             help_text="Color de la etiqueta en el panel, en hexadecimal.")
    show_in_kitchen = models.BooleanField(
        "visible en cocina",
        default=False,
        help_text="Márcalo en alergias y dietas: así aparece en la comanda que ve la cocina.",
    )

    class Meta:
        verbose_name = "etiqueta"
        verbose_name_plural = "etiquetas"
        ordering = ["kind", "name"]

    def __str__(self):
        return self.name


class Guest(models.Model):
    id = models.UUIDField("id", primary_key=True, default=uuid.uuid4,
                          editable=False)

    phone = models.CharField(
        "teléfono", max_length=20, unique=True, db_index=True,
        help_text="Escríbelo como quieras: con espacios, guiones o sin el código de país. Se guarda siempre igual (+51987654321).",
    )
    name = models.CharField("nombre", max_length=120)
    email = models.EmailField("correo", blank=True)
    language = models.CharField("idioma", max_length=5, default="es")
    marketing_opt_in = models.BooleanField("acepta marketing", default=False)
    notes = models.TextField(
        "notas internas", blank=True,
        help_text="Solo lo ve el equipo. Nunca se envía al cliente.",
    )
    tags = models.ManyToManyField(Tag, blank=True, related_name="guests",
                                  verbose_name="etiquetas")

    # Contadores desnormalizados a proposito: la lista del dia los muestra en
    # cada fila y no puede permitirse un agregado por reserva.
    visits = models.PositiveIntegerField("visitas", default=0, editable=False)
    no_shows = models.PositiveIntegerField("no-shows", default=0, editable=False)
    cancellations = models.PositiveIntegerField("cancelaciones", default=0, editable=False)
    last_visit = models.DateField("última visita", null=True, blank=True, editable=False)

    created_at = models.DateTimeField("creado en", auto_now_add=True)
    updated_at = models.DateTimeField("actualizado en", auto_now=True)

    class Meta:
        verbose_name = "cliente"
        verbose_name_plural = "clientes"
        ordering = ["name"]
        indexes = [models.Index(fields=["name"])]

    def __str__(self):
        return f"{self.name} - {self.phone}"

    def clean(self):
        if self.phone:
            self.phone = normalize_phone(self.phone)

    def save(self, *args, **kwargs):
        # La normalizacion va en save() y no solo en clean(): tambien tiene que
        # aplicarse a los objetos creados desde codigo o desde una importacion.
        if self.phone:
            self.phone = normalize_phone(self.phone)
        super().save(*args, **kwargs)

    @property
    def is_risky(self):
        """Dos o mas plantones. Sirve para exigir deposito o confirmar por telefono."""
        return self.no_shows >= 2

    def refresh_counters(self):
        """
        Recalcula los contadores desde el historial real de reservas.

        Se llama desde la capa de servicio al cerrar o cancelar una reserva, y
        se puede ejecutar en lote si los contadores se desincronizan.
        """
        from apps.reservations.models import Reservation

        totals = self.reservations.aggregate(
            visits=models.Count("pk", filter=models.Q(status=Reservation.Status.FINISHED)),
            no_shows=models.Count("pk", filter=models.Q(status=Reservation.Status.NO_SHOW)),
            cancellations=models.Count(
                "pk", filter=models.Q(status=Reservation.Status.CANCELLED)
            ),
            last=models.Max(
                "service_date", filter=models.Q(status=Reservation.Status.FINISHED)
            ),
        )
        self.visits = totals["visits"] or 0
        self.no_shows = totals["no_shows"] or 0
        self.cancellations = totals["cancellations"] or 0
        self.last_visit = totals["last"]
        self.save(update_fields=["visits", "no_shows", "cancellations", "last_visit"])
