"""
Registro de envios.

Sin esta tabla no se puede responder "le llego la confirmacion?", ni
reintentar un envio fallido, ni evitar mandar el recordatorio dos veces.

Las claves primarias son UUID: el identificador de una reserva o de un
cliente acaba en un enlace que se manda por WhatsApp, y un correlativo
delata cuantas reservas hay y deja probar la de al lado.
"""

import uuid

from django.db import models


class Notification(models.Model):
    class Channel(models.TextChoices):
        EMAIL = "email", "Correo"
        WHATSAPP = "whatsapp", "WhatsApp"
        SMS = "sms", "SMS"

    class Template(models.TextChoices):
        CONFIRMATION = "confirmation", "Confirmación de reserva"
        REMINDER = "reminder_24h", "Recordatorio 24 h antes"
        CANCELLATION = "cancellation", "Cancelación"
        TABLE_RELEASED = "table_released", "Aviso de lista de espera"
        DEPOSIT_REQUEST = "deposit_request", "Solicitud de depósito"

    class SendStatus(models.TextChoices):
        PENDING = "pending", "Pendiente"
        SENT = "sent", "Enviada"
        DELIVERED = "delivered", "Entregada"
        FAILED = "failed", "Fallida"

    id = models.UUIDField("id", primary_key=True, default=uuid.uuid4,
                          editable=False)

    reservation = models.ForeignKey("reservations.Reservation", on_delete=models.CASCADE,
                                    related_name="notifications", verbose_name="reserva")
    channel = models.CharField("canal", max_length=10, choices=Channel.choices)
    template = models.CharField("plantilla", max_length=30, choices=Template.choices)
    recipient = models.CharField("destino", max_length=120,
                                 help_text="Teléfono o correo al que se envió.")
    send_status = models.CharField("estado del envío", max_length=10,
                                   choices=SendStatus.choices,
                                   default=SendStatus.PENDING)
    provider_id = models.CharField("id del proveedor", max_length=80, blank=True)
    error = models.TextField("error", blank=True)
    attempts = models.PositiveSmallIntegerField("intentos", default=0)
    created_at = models.DateTimeField("creada en", auto_now_add=True)
    sent_at = models.DateTimeField("enviada en", null=True, blank=True)

    class Meta:
        verbose_name = "notificación"
        verbose_name_plural = "notificaciones"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["reservation", "template"])]
        constraints = [
            # Evita mandar el recordatorio dos veces cuando la tarea se reintenta.
            models.UniqueConstraint(
                fields=["reservation", "template"],
                name="one_notification_per_template",
            ),
        ]

    def __str__(self):
        return f"{self.get_template_display()} -> {self.recipient} ({self.send_status})"
