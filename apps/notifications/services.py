"""Correos al cliente de confirmacion y cancelacion de la reserva."""

import logging
from email.mime.image import MIMEImage
from pathlib import Path

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.formats import date_format

from apps.notifications.models import Notification

logger = logging.getLogger("apps.reservations")

LOGO = Path(settings.BASE_DIR) / "static" / "admin" / "img" / "zisa-logo-negro.png"

SUBJECTS = {
    Notification.Template.CONFIRMATION: "Tu reserva en {venue} está confirmada ({code})",
    Notification.Template.CANCELLATION: "Tu reserva en {venue} fue cancelada ({code})",
}

TEMPLATE_FILES = {
    Notification.Template.CONFIRMATION: "emails/reservation_confirmed",
    Notification.Template.CANCELLATION: "emails/reservation_cancelled",
}


def notify_confirmed(reservation):
    _schedule(reservation, Notification.Template.CONFIRMATION)


def notify_cancelled(reservation):
    _schedule(reservation, Notification.Template.CANCELLATION)


def _schedule(reservation, template):
    pk = reservation.pk
    transaction.on_commit(lambda: send(pk, template))


def _context(reservation):
    from apps.reservations.services import venue_tz

    venue = reservation.venue
    local = timezone.localtime(reservation.starts_at, venue_tz(venue))
    return {
        "reservation": reservation,
        "guest": reservation.guest,
        "venue": venue,
        "logo_src": settings.EMAIL_LOGO_URL or "cid:logo",
        "day": date_format(local, "l j \\d\\e F \\d\\e Y"), 
        "time": f"{local:%H:%M}",
        "people": (f"{reservation.party_size} "
                   f"{'persona' if reservation.party_size == 1 else 'personas'}"),
    }


def _attach_logo(message):
    message.mixed_subtype = "related"
    logo = MIMEImage(LOGO.read_bytes(), "png")
    logo.add_header("Content-ID", "<logo>")
    logo.add_header("Content-Disposition", "inline", filename="zisa.png")
    message.attach(logo)


def send(reservation_pk, template):
    """Envia el correo una sola vez; si fallo antes, reintenta sobre el mismo registro."""
    from apps.reservations.models import Reservation

    reservation = (Reservation.objects.select_related("guest", "venue")
                   .get(pk=reservation_pk))
    recipient = (reservation.guest.email or "").strip()
    if not recipient:
        return None

    notification, _ = Notification.objects.get_or_create(
        reservation=reservation, template=template,
        defaults={"channel": Notification.Channel.EMAIL, "recipient": recipient},
    )
    if notification.send_status in (Notification.SendStatus.SENT,
                                    Notification.SendStatus.DELIVERED):
        return notification

    context = _context(reservation)
    base = TEMPLATE_FILES[template]
    message = EmailMultiAlternatives(
        subject=SUBJECTS[template].format(venue=reservation.venue.name,
                                          code=reservation.code),
        body=render_to_string(f"{base}.txt", context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient],
        reply_to=[settings.DEFAULT_FROM_EMAIL],
    )
    message.attach_alternative(render_to_string(f"{base}.html", context), "text/html")
    if not settings.EMAIL_LOGO_URL:
        _attach_logo(message)

    notification.recipient = recipient
    notification.attempts += 1
    try:
        message.send()
    except Exception as exc:  # SMTP, DNS, credenciales... nada debe tumbar la reserva
        logger.exception("No se pudo enviar %s de %s a %s", template,
                         reservation.code, recipient)
        notification.send_status = Notification.SendStatus.FAILED
        notification.error = str(exc)[:2000]
    else:
        logger.info("Correo %s de %s enviado a %s", template, reservation.code, recipient)
        notification.send_status = Notification.SendStatus.SENT
        notification.sent_at = timezone.now()
        notification.error = ""
    notification.save()
    return notification
