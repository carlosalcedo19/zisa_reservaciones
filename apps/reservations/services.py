"""
Capa de servicio de reservas.

Ninguna escritura debe pasar por un ModelForm ni por el admin en crudo. Este
modulo es el unico lugar donde caben, en la misma transaccion: el
select_for_update, el calculo de ends_at y service_date, la sincronizacion de
TableOccupancy y el registro en ReservationEvent.
"""

import logging
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.guests.models import Guest, normalize_phone
from apps.reservations.models import (
    Reservation,
    ReservationEvent,
    ReservationPolicy,
    TableOccupancy,
)
from apps.venues.models import CalendarException, DurationRule, Table, TableCombination

logger = logging.getLogger(__name__)


class ReservationError(Exception):
    """Error de negocio: el mensaje es apto para mostrarse al usuario."""


class NoAvailability(ReservationError):
    pass


class InvalidTransition(ReservationError):
    pass


# ---------------------------------------------------------------------------
# Utilidades de tiempo
# ---------------------------------------------------------------------------


def venue_tz(venue):
    return zoneinfo.ZoneInfo(venue.timezone)


def service_date_for(venue, starts_at):
    """
    Dia del servicio al que pertenece un inicio.

    Una cena que empieza a las 23:30 y termina a la 01:00 sigue perteneciendo
    al servicio de la noche anterior, asi que las horas antes de las 06:00
    locales cuentan como el dia previo.
    """
    local = timezone.localtime(starts_at, venue_tz(venue))
    if local.hour < 6:
        return (local - timedelta(days=1)).date()
    return local.date()


def policy_for(venue):
    policy, _ = ReservationPolicy.objects.get_or_create(venue=venue)
    return policy


def duration_for(venue, party_size):
    """Minutos que ocupa un grupo, segun las reglas de duracion de la sede."""
    rule = (
        DurationRule.objects.filter(
            venue=venue, min_party__lte=party_size, max_party__gte=party_size
        )
        .order_by("min_party")
        .first()
    )
    if rule:
        return rule.minutes
    return policy_for(venue).default_duration_min


def shift_for(venue, starts_at):
    """Turno que cubre un inicio concreto, o None si el local esta cerrado."""
    local = timezone.localtime(starts_at, venue_tz(venue))
    day = service_date_for(venue, starts_at)
    for shift in venue.shifts.filter(is_active=True).order_by("sort_order", "start_time"):
        if shift.applies_on(day) and shift.contains(local):
            return shift
    return None


# ---------------------------------------------------------------------------
# Validaciones previas
# ---------------------------------------------------------------------------


def validate_calendar(venue, starts_at, ends_at):
    day = service_date_for(venue, starts_at)
    local_start = timezone.localtime(starts_at, venue_tz(venue))

    closure = venue.calendar_exceptions.filter(
        kind=CalendarException.Kind.CLOSED,
        start_date__lte=day,
        end_date__gte=day,
    ).first()
    if closure:
        raise NoAvailability(f"El local está cerrado ese día: {closure.reason}.")

    shift = shift_for(venue, starts_at)
    if shift is None:
        raise NoAvailability(
            f"No hay turno abierto a las {local_start:%H:%M} del {day:%d/%m/%Y}."
        )
    if local_start.time() > shift.last_seating:
        raise NoAvailability(
            f"La última entrada del turno {shift.name} es a las "
            f"{shift.last_seating:%H:%M}."
        )
    return shift


def validate_lead_time(venue, starts_at, source):
    if source != Reservation.Source.WEB:
        return  # El personal puede saltarse la ventana; la web no.

    policy = policy_for(venue)
    now = timezone.now()
    if starts_at < now + timedelta(hours=policy.min_lead_hours):
        raise ReservationError(
            f"Las reservas web necesitan al menos {policy.min_lead_hours} h "
            f"de antelación."
        )
    if starts_at > now + timedelta(days=policy.max_lead_days):
        raise ReservationError(
            f"Todavía no se aceptan reservas con más de {policy.max_lead_days} "
            f"días de antelación."
        )


# ---------------------------------------------------------------------------
# Disponibilidad
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Offer:
    """Una combinacion de mesas que sirve para un grupo en un rango."""

    tables: tuple
    capacity: int
    slack: int  # capacidad sobrante; menos es mejor
    combination: object = None

    @property
    def codes(self):
        return "+".join(t.code for t in self.tables)


def busy_table_ids(venue, starts_at, ends_at, exclude_reservation=None):
    """Ids de mesas con una ocupacion activa que se pisa con el rango."""
    qs = TableOccupancy.objects.filter(
        table__area__venue=venue,
        is_active=True,
        starts_at__lt=ends_at,
        ends_at__gt=starts_at,
    )
    if exclude_reservation is not None:
        qs = qs.exclude(reservation=exclude_reservation)
    return set(qs.values_list("table_id", flat=True))


def find_availability(venue, starts_at, ends_at, party_size, exclude_reservation=None):
    """
    Devuelve las ofertas viables ordenadas de menor a mayor desperdicio.

    Primero mesas sueltas que encajan por si solas; despues combinaciones
    declaradas. No inventa combinaciones: solo usa las que existen en
    TableCombination.
    """
    busy = busy_table_ids(venue, starts_at, ends_at, exclude_reservation)

    free = list(
        Table.objects.filter(area__venue=venue, is_active=True, area__is_bookable=True)
        .exclude(id__in=busy)
        .select_related("area")
    )

    offers = [
        Offer(tables=(table,), capacity=table.max_seats,
              slack=table.max_seats - party_size)
        for table in free
        if table.fits(party_size)
    ]

    free_ids = {t.id for t in free}
    for combo in (
        TableCombination.objects.filter(
            venue=venue, is_active=True, min_seats__lte=party_size,
            max_seats__gte=party_size,
        ).prefetch_related("tables")
    ):
        combo_tables = list(combo.tables.all())
        if combo_tables and all(t.id in free_ids for t in combo_tables):
            offers.append(
                Offer(
                    tables=tuple(combo_tables),
                    capacity=combo.max_seats,
                    slack=combo.max_seats - party_size,
                    combination=combo,
                )
            )

    # Menos desperdicio primero; a igualdad, menos mesas involucradas.
    return sorted(offers, key=lambda o: (o.slack, len(o.tables)))


def open_slots(venue, day, party_size, step_min=15):
    """
    Franjas en las que cabe un grupo ese dia. Alimenta el buscador del panel y
    el formulario publico.
    """
    tz = venue_tz(venue)
    duration = duration_for(venue, party_size)
    slots = []

    for shift in venue.shifts.filter(is_active=True).order_by("sort_order", "start_time"):
        if not shift.applies_on(day):
            continue

        cursor = datetime.combine(day, shift.start_time, tzinfo=tz)
        limit = datetime.combine(day, shift.last_seating, tzinfo=tz)
        if shift.crosses_midnight and shift.last_seating < shift.start_time:
            limit += timedelta(days=1)

        while cursor <= limit:
            ends_at = cursor + timedelta(minutes=duration)
            if find_availability(venue, cursor, ends_at, party_size):
                slots.append(cursor)
            cursor += timedelta(minutes=step_min)

    return slots


# ---------------------------------------------------------------------------
# Escrituras
# ---------------------------------------------------------------------------


def _log_event(reservation, user, from_status, to_status, comment=""):
    ReservationEvent.objects.create(
        reservation=reservation,
        user=user,
        from_status=from_status or "",
        to_status=to_status,
        comment=comment,
    )


def get_or_create_guest(phone, name, email="", user=None):
    """El telefono normalizado es la llave: evita fichas duplicadas."""
    number = normalize_phone(phone)
    guest, created = Guest.objects.get_or_create(
        phone=number, defaults={"name": name, "email": email}
    )
    if not created and name and guest.name != name:
        # El nombre mas reciente gana; el historial queda en las reservas.
        guest.name = name
        if email and not guest.email:
            guest.email = email
        guest.save(update_fields=["name", "email"])
    return guest


@transaction.atomic
def create_reservation(
    *,
    venue,
    guest,
    starts_at,
    party_size,
    source=Reservation.Source.WEB,
    tables=None,
    duration_min=None,
    children=0,
    high_chairs=0,
    occasion="",
    guest_notes="",
    internal_notes="",
    user=None,
    status=None,
):
    """
    Crea la reserva y sus ocupaciones en una sola transaccion.

    Si no se pasan mesas, elige la mejor oferta disponible. Si Postgres
    rechaza el solapamiento (dos hosts reservando a la vez), se traduce a un
    error de negocio legible en lugar de un 500.
    """
    policy = policy_for(venue)

    if source == Reservation.Source.WEB and party_size > policy.max_party_web:
        raise ReservationError(
            f"Los grupos de más de {policy.max_party_web} personas se reservan "
            f"por teléfono."
        )

    duration = duration_min or duration_for(venue, party_size)
    ends_at = starts_at + timedelta(minutes=duration)

    validate_lead_time(venue, starts_at, source)
    if source == Reservation.Source.WALK_IN:
        # Quien ya esta en la puerta no pasa por turnos ni ultima entrada:
        # si el anfitrion lo sienta, es su decision.
        shift = shift_for(venue, starts_at)
    else:
        shift = validate_calendar(venue, starts_at, ends_at)

    if tables is None:
        offers = find_availability(venue, starts_at, ends_at, party_size)
        if not offers:
            raise NoAvailability(f"No hay mesa para {party_size} personas a esa hora.")
        tables = list(offers[0].tables)
    else:
        tables = list(tables)
        capacity = sum(t.max_seats for t in tables)
        if capacity < party_size:
            raise ReservationError(
                f"Las mesas elegidas suman {capacity} plazas y el grupo es de "
                f"{party_size}."
            )

    if status is None:
        status = (
            Reservation.Status.CONFIRMED
            if policy.web_auto_confirm or source != Reservation.Source.WEB
            else Reservation.Status.PENDING
        )

    reservation = Reservation(
        venue=venue,
        guest=guest,
        shift=shift,
        starts_at=starts_at,
        ends_at=ends_at,
        service_date=service_date_for(venue, starts_at),
        duration_min=duration,
        party_size=party_size,
        children=children,
        high_chairs=high_chairs,
        status=status,
        source=source,
        occasion=occasion,
        guest_notes=guest_notes,
        internal_notes=internal_notes,
        created_by=user,
    )
    if status == Reservation.Status.CONFIRMED:
        reservation.confirmed_at = timezone.now()
    reservation.save()

    _assign(reservation, tables)
    _log_event(reservation, user, "", status, "Reserva creada")
    logger.info("Reserva %s creada en %s", reservation.code, reservation.table_codes)
    return reservation


def _assign(reservation, tables):
    """Crea las ocupaciones. Traduce el choque de Postgres a un error legible."""
    try:
        with transaction.atomic():
            TableOccupancy.objects.bulk_create(
                [
                    TableOccupancy(
                        table=table,
                        reservation=reservation,
                        starts_at=reservation.starts_at,
                        ends_at=reservation.ends_at,
                        is_primary=(index == 0),
                        is_active=reservation.is_live,
                    )
                    for index, table in enumerate(tables)
                ]
            )
    except IntegrityError as exc:
        if "occupancy_no_overlap" in str(exc):
            raise NoAvailability(
                "Alguien acaba de tomar esa mesa. Elige otra hora u otra mesa."
            ) from exc
        raise


@transaction.atomic
def assign_tables(reservation, tables, user=None):
    """Reasigna la reserva a otras mesas sin perder el rastro."""
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    reservation.occupancies.update(is_active=False)
    _assign(reservation, list(tables))
    _log_event(reservation, user, reservation.status, reservation.status,
               f"Reasignada a {reservation.table_codes}")
    return reservation


@transaction.atomic
def move(reservation, new_start, user=None, tables=None):
    """Cambia la hora (y opcionalmente las mesas) revalidando todo."""
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if not reservation.is_live:
        raise InvalidTransition("Solo se puede mover una reserva viva.")

    new_end = new_start + timedelta(minutes=reservation.duration_min)
    shift = validate_calendar(reservation.venue, new_start, new_end)

    if tables is None:
        current = list(reservation.tables)
        busy = busy_table_ids(reservation.venue, new_start, new_end, reservation)
        if all(t.id not in busy for t in current):
            tables = current
        else:
            offers = find_availability(
                reservation.venue, new_start, new_end, reservation.party_size, reservation
            )
            if not offers:
                raise NoAvailability("No hay mesa libre a esa hora.")
            tables = list(offers[0].tables)

    tz = venue_tz(reservation.venue)
    previous = timezone.localtime(reservation.starts_at, tz)

    reservation.occupancies.update(is_active=False)
    reservation.starts_at = new_start
    reservation.ends_at = new_end
    reservation.shift = shift
    reservation.service_date = service_date_for(reservation.venue, new_start)
    reservation.save(update_fields=["starts_at", "ends_at", "shift", "service_date",
                                    "updated_at"])
    _assign(reservation, tables)

    current_local = timezone.localtime(new_start, tz)
    _log_event(reservation, user, reservation.status, reservation.status,
               f"Movida de {previous:%d/%m %H:%M} a {current_local:%d/%m %H:%M}")
    return reservation


def _transition(reservation, new_status, user, comment="", extra_fields=None):
    if not reservation.can_transition_to(new_status):
        raise InvalidTransition(
            f"No se puede pasar de {reservation.get_status_display()} a "
            f"{Reservation.Status(new_status).label}."
        )

    previous = reservation.status
    reservation.status = new_status
    fields = ["status", "updated_at"]
    for field, value in (extra_fields or {}).items():
        setattr(reservation, field, value)
        fields.append(field)
    reservation.save(update_fields=fields)

    # Los estados muertos liberan el rango de inmediato.
    if new_status not in Reservation.LIVE_STATUSES:
        reservation.occupancies.update(is_active=False)

    _log_event(reservation, user, previous, new_status, comment)
    return reservation


@transaction.atomic
def confirm(reservation, user=None):
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    return _transition(reservation, Reservation.Status.CONFIRMED, user,
                       extra_fields={"confirmed_at": timezone.now()})


@transaction.atomic
def seat(reservation, user=None):
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    return _transition(reservation, Reservation.Status.SEATED, user,
                       extra_fields={"seated_at": timezone.now()})


@transaction.atomic
def finish(reservation, user=None):
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    reservation = _transition(reservation, Reservation.Status.FINISHED, user,
                              extra_fields={"released_at": timezone.now()})
    reservation.guest.refresh_counters()
    return reservation


@transaction.atomic
def cancel(reservation, reason="", user=None):
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    reservation = _transition(
        reservation, Reservation.Status.CANCELLED, user, comment=reason,
        extra_fields={"cancelled_at": timezone.now(),
                      "cancellation_reason": reason[:120]},
    )
    reservation.guest.refresh_counters()
    return reservation


@transaction.atomic
def mark_no_show(reservation, user=None):
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    reservation = _transition(reservation, Reservation.Status.NO_SHOW, user,
                              extra_fields={"released_at": timezone.now()})
    reservation.guest.refresh_counters()
    return reservation


@transaction.atomic
def block_table(table, starts_at, ends_at, reason, user=None):
    """Saca una mesa de servicio. Compite por el mismo espacio que una reserva."""
    try:
        return TableOccupancy.objects.create(
            table=table, reservation=None, starts_at=starts_at, ends_at=ends_at,
            block_reason=reason or "Fuera de servicio",
        )
    except IntegrityError as exc:
        if "occupancy_no_overlap" in str(exc):
            raise NoAvailability(
                f"La mesa {table.code} ya tiene una reserva en ese rango."
            ) from exc
        raise


def sweep_no_shows(venue, now=None):
    """
    Marca como no-show las reservas cuya gracia ya vencio.

    Pensada para una tarea periodica (cron, Celery beat o el management
    command sweep_no_shows llamado cada cinco minutos).
    """
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=policy_for(venue).no_show_grace_min)

    candidates = Reservation.objects.filter(
        venue=venue,
        status__in=[Reservation.Status.PENDING, Reservation.Status.CONFIRMED],
        starts_at__lt=cutoff,
    )
    marked = 0
    for reservation in candidates:
        try:
            mark_no_show(reservation)
            marked += 1
        except InvalidTransition:
            continue
    if marked:
        logger.info("%s reservas marcadas como no-show en %s", marked, venue)
    return marked


# ---------------------------------------------------------------------------
# Clientes sin reserva (walk-in)
# ---------------------------------------------------------------------------

#: Ficha compartida para quien llega sin reserva y no deja sus datos.
WALK_IN_PHONE = "+000000000"
WALK_IN_NAME = "Cliente de paso"

#: Por debajo de esto no merece la pena sentar a nadie antes de la siguiente
#: reserva de la mesa.
MIN_WALK_IN_MIN = 30


def walk_in_guest():
    guest, _ = Guest.objects.get_or_create(
        phone=WALK_IN_PHONE, defaults={"name": WALK_IN_NAME}
    )
    return guest


@transaction.atomic
def seat_walk_in(venue, table, party_size, name="", phone="", user=None, now=None):
    """
    Sienta en el acto a quien llega sin reserva.

    Crea una reserva de origen "walk-in" ya sentada en esa mesa desde ahora.
    Asi la mesa queda ocupada para todos: el plano, la lista del dia y, sobre
    todo, la web, que deja de ofrecerla.

    Si la mesa tiene una reserva mas tarde, la ocupacion se corta a esa hora
    (y se avisa en el mensaje de vuelta); si falta menos de MIN_WALK_IN_MIN,
    no se sienta.
    """
    now = (now or timezone.now()).replace(second=0, microsecond=0)
    if party_size < 1:
        raise ReservationError("Indica cuántas personas son.")
    if party_size > table.max_seats:
        raise ReservationError(
            f"La mesa {table.code} es para {table.max_seats} como máximo."
        )

    ocupada = TableOccupancy.objects.filter(
        table=table, is_active=True, starts_at__lte=now, ends_at__gt=now,
    ).select_related("reservation").first()
    if ocupada:
        quien = (ocupada.reservation.guest.name if ocupada.reservation_id
                 else f"bloqueo: {ocupada.block_reason}")
        raise NoAvailability(f"La mesa {table.code} ya está ocupada ({quien}).")

    duration = duration_for(venue, party_size)
    siguiente = (
        TableOccupancy.objects.filter(table=table, is_active=True, starts_at__gt=now)
        .order_by("starts_at").first()
    )
    corta_a = None
    if siguiente:
        hueco = int((siguiente.starts_at - now).total_seconds() // 60)
        if hueco < MIN_WALK_IN_MIN:
            hora = timezone.localtime(siguiente.starts_at, venue_tz(venue))
            raise NoAvailability(
                f"La mesa {table.code} tiene una reserva a las {hora:%H:%M}."
            )
        if hueco < duration:
            duration, corta_a = hueco, siguiente.starts_at

    guest = get_or_create_guest(phone, name or WALK_IN_NAME) if phone else walk_in_guest()
    reservation = create_reservation(
        venue=venue, guest=guest, starts_at=now, party_size=party_size,
        source=Reservation.Source.WALK_IN, tables=[table], duration_min=duration,
        user=user, status=Reservation.Status.SEATED,
        internal_notes=(name if name and not phone else ""),
    )
    reservation.seated_at = now
    reservation.save(update_fields=["seated_at"])
    reservation.cut_at = corta_a  # para el mensaje de vuelta, no se guarda
    return reservation

