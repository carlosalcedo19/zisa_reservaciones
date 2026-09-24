"""
Entrada de reservas desde la web (formulario "Reserva una mesa" de zisa.pe).

El formulario es un Contact Form 7. No llama aqui desde el navegador: lo hace
el servidor de WordPress al enviarse (integrations/wordpress/zisa-reservas.php),
con una clave compartida en la cabecera Authorization. Asi la clave no sale
nunca al navegador y no hace falta abrir CORS.

Recibe los campos con los mismos nombres que el formulario:

    full-name         nombre del cliente
    your-phone        telefono (cualquier formato: se normaliza)
    num-person        numero de personas
    date-reservation  AAAA-MM-DD
    time-field        "07:30 PM" (como lo manda el desplegable) o "19:30"
    indications       indicaciones especiales (opcional)

Toda la logica de negocio (antelacion, grupo maximo web, turnos, dias
cerrados, eleccion de mesa, autoconfirmacion) es la misma que en el panel:
services.create_reservation con origen WEB.

Respuestas (JSON):
    201  {"ok": true, "code", "status", "message"}   reserva creada
    200  {"ok": true, ...}                          ya existia (reenvio del formulario)
    400  {"ok": false, "message", "errors"}         datos mal formados
    401  {"ok": false, "message"}                   falta o no vale la clave
    409  {"ok": false, "message"}                   regla de negocio (sin mesa, cerrado...)
    503  {"ok": false, "message"}                   la entrada web esta desactivada
"""

import hmac
import json
import logging
from datetime import datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.reservations import services
from apps.reservations.admin import default_venue
from apps.reservations.models import Reservation

logger = logging.getLogger("apps.reservations")

MAX_PARTY_FIELD = 50  # tope de cordura del campo; el de negocio es max_party_web


def _json(ok, http_status, **extra):
    return JsonResponse({"ok": ok, **extra}, status=http_status,
                        json_dumps_params={"ensure_ascii": False})


def _authorized(request, setting="WEB_RESERVATION_TOKEN"):
    token = getattr(settings, setting, "")
    header = request.headers.get("Authorization", "")
    sent = header[7:] if header.startswith("Bearer ") else ""
    return bool(token) and hmac.compare_digest(sent.encode(), token.encode())


def _parse_time(value):
    """ "07:30 PM", "7:30 pm" o "19:30" -> time. """
    value = (value or "").strip().upper().replace(".", "")
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    return None


def _read(request):
    """Acepta JSON o formulario (asi sirve tambien para probar con curl)."""
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body or b"{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    # El plugin de WordPress repite los campos en la URL: desde el hosting de
    # zisa.pe el cuerpo llega vacio al pasar por Cloudflare, la URL no.
    if not request.POST and request.GET:
        return request.GET
    return request.POST


def _clean(data):
    """Valida y convierte los campos. Devuelve (limpios, errores)."""
    errors = {}
    get = lambda k: str(data.get(k) or "").strip()  # noqa: E731

    name = get("full-name")[:120]
    if not name:
        errors["full-name"] = "Escribe tu nombre."

    phone = get("your-phone")
    if not phone:
        errors["your-phone"] = "Escribe un teléfono de contacto."

    try:
        party = int(get("num-person"))
        if not 1 <= party <= MAX_PARTY_FIELD:
            raise ValueError
    except ValueError:
        party = None
        errors["num-person"] = "Indica cuántas personas vienen."

    try:
        day = datetime.strptime(get("date-reservation"), "%Y-%m-%d").date()
    except ValueError:
        day = None
        errors["date-reservation"] = "Elige una fecha."

    at = _parse_time(get("time-field"))
    if at is None:
        errors["time-field"] = "Elige una hora."

    notes = get("indications")[:1000]
    return {"name": name, "phone": phone, "party": party, "day": day, "time": at,
            "notes": notes}, errors


@csrf_exempt  # llamada servidor a servidor, protegida por la clave
@require_POST
def web_reservation(request):
    if not getattr(settings, "WEB_RESERVATION_TOKEN", ""):
        return _json(False, 503, message="La entrada de reservas web está desactivada.")
    if not _authorized(request):
        return _json(False, 401, message="No autorizado.")

    data = _read(request)
    if data is None:
        return _json(False, 400, message="Cuerpo no válido.", errors={})

    clean, errors = _clean(data)
    if errors:
        return _json(False, 400, message=next(iter(errors.values())), errors=errors)

    venue = default_venue()
    if venue is None:
        return _json(False, 503, message="No hay ningún restaurante activo.")

    tz = services.venue_tz(venue)
    starts_at = datetime.combine(clean["day"], clean["time"], tzinfo=tz)

    # Cliente y reserva van juntos: si la reserva se rechaza (sin mesa, dia
    # cerrado...) tampoco queda creada la ficha del cliente. Si no, cada
    # intento fallido de la web dejaria un cliente suelto.
    try:
        with transaction.atomic():
            guest = services.get_or_create_guest(clean["phone"], clean["name"])

            # Reenvio del mismo formulario (doble clic, recarga): se devuelve
            # la que ya existe en vez de crear otra.
            existing = Reservation.objects.filter(
                venue=venue, guest=guest, starts_at=starts_at,
                status__in=Reservation.LIVE_STATUSES,
            ).first()
            if existing:
                return _json(True, 200, code=existing.code, status=existing.status,
                             message=_success_message(existing, tz))

            reservation = services.create_reservation(
                venue=venue,
                guest=guest,
                starts_at=starts_at,
                party_size=clean["party"],
                source=Reservation.Source.WEB,
                guest_notes=clean["notes"],
            )
    except ValidationError as exc:  # telefono sin digitos
        msg = exc.messages[0] if exc.messages else "Revisa el teléfono."
        return _json(False, 400, message=msg, errors={"your-phone": msg})
    except services.ReservationError as exc:
        logger.info("Reserva web rechazada (%s, %s): %s", clean["phone"], starts_at, exc)
        return _json(False, 409, message=str(exc))

    logger.info("Reserva web %s creada para %s", reservation.code, guest.phone)
    return _json(True, 201, code=reservation.code, status=reservation.status,
                 message=_success_message(reservation, tz))


def _success_message(reservation, tz):
    local = timezone.localtime(reservation.starts_at, tz)
    fecha = date_format(local, "l j \\d\\e F")  # "jueves 24 de septiembre"
    cuando = f"{fecha} a las {local:%H:%M}"
    personas = (f"{reservation.party_size} "
                f"{'persona' if reservation.party_size == 1 else 'personas'}")
    if reservation.status == Reservation.Status.CONFIRMED:
        return (f"¡Listo! Tu reserva {reservation.code} está confirmada para el "
                f"{cuando}, {personas}. Te esperamos.")
    return (f"Recibimos tu solicitud {reservation.code} para el {cuando}, {personas}. "
            f"Te confirmaremos en breve.")


@csrf_exempt  # la llama un cron externo, protegida por CRON_TOKEN
@require_POST
def sweep_no_shows_task(request):
    """
    Marca los no-shows de todas las sedes activas.

    En Render no hay cron gratuito, asi que cron-job.org llama aqui cada cinco
    minutos con "Authorization: Bearer <CRON_TOKEN>". Esa misma llamada
    mantiene despierto el servicio, que en el plan gratuito se duerme tras
    quince minutos sin trafico y haria esperar al formulario de la web.
    """
    if not getattr(settings, "CRON_TOKEN", ""):
        return _json(False, 503, message="La tarea programada está desactivada.")
    if not _authorized(request, "CRON_TOKEN"):
        return _json(False, 401, message="No autorizado.")

    from apps.venues.models import Venue

    marked = {venue.name: services.sweep_no_shows(venue)
              for venue in Venue.objects.filter(is_active=True)}
    return _json(True, 200, marked=marked, total=sum(marked.values()))
