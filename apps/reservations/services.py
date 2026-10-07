"""Unico punto de escritura de reservas: ocupaciones y eventos van en la misma transaccion."""

import logging
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.guests.models import Guest, normalize_phone
from apps.notifications import services as notifications
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


def venue_tz(venue):
    return zoneinfo.ZoneInfo(venue.timezone)


def service_date_for(venue, starts_at):
    # Antes de las 06:00 locales cuenta como el servicio de la noche anterior.
    local = timezone.localtime(starts_at, venue_tz(venue))
    if local.hour < 6:
        return (local - timedelta(days=1)).date()
    return local.date()


def policy_for(venue):
    policy, _ = ReservationPolicy.objects.get_or_create(venue=venue)
    return policy


def duration_for(venue, party_size):
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
    local = timezone.localtime(starts_at, venue_tz(venue))
    day = service_date_for(venue, starts_at)
    for shift in venue.shifts.filter(is_active=True).order_by("sort_order", "start_time"):
        if shift.applies_on(day) and shift.contains(local):
            return shift
    return None


def validate_calendar(venue, starts_at):
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


def validate_not_past(venue, starts_at):
    """Nadie reserva para un dia o una hora que ya paso, tampoco el personal."""
    now = timezone.now()
    if starts_at >= now:
        return
    ahora = timezone.localtime(now, venue_tz(venue))
    if service_date_for(venue, starts_at) < service_date_for(venue, now):
        raise ReservationError(
            f"No se puede reservar en un día que ya pasó. Hoy es {ahora:%d/%m/%Y}."
        )
    raise ReservationError(
        f"Esa hora ya pasó: son las {ahora:%H:%M}. Elige una hora posterior."
    )


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


@dataclass(frozen=True)
class Offer:
    tables: tuple
    capacity: int
    slack: int  # capacidad sobrante; menos es mejor
    combination: object = None

    @property
    def codes(self):
        return "+".join(t.code for t in self.tables)


def busy_table_ids(venue, starts_at, ends_at, exclude_reservation=None):
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
    # Solo combina mesas declaradas en TableCombination; nunca inventa juntas.
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

    return sorted(offers, key=lambda o: (o.slack, len(o.tables)))


def open_slots(venue, day, party_size, step_min=15):
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


def _log_event(reservation, user, from_status, to_status, comment=""):
    ReservationEvent.objects.create(
        reservation=reservation,
        user=user,
        from_status=from_status or "",
        to_status=to_status,
        comment=comment,
    )


def get_or_create_guest(phone, name, email=""):
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


@dataclass(frozen=True)
class ReservationDetails:
    children: int = 0
    high_chairs: int = 0
    occasion: str = ""
    guest_notes: str = ""
    internal_notes: str = ""


def validate_tables(venue, starts_at, ends_at, party_size, tables,
                    exclude_reservation=None, combination=None):
    """Mesas elegidas a mano: varias se juntan si son una combinacion declarada o todas combinables."""
    tables = list(tables)
    if not tables:
        raise ReservationError("Elige al menos una mesa.")

    ajenas = [t.code for t in tables if t.area.venue_id != venue.pk]
    if ajenas:
        raise ReservationError(f"La mesa {', '.join(ajenas)} es de otro restaurante.")

    inactivas = [t.code for t in tables if not t.is_active]
    if inactivas:
        raise ReservationError(f"La mesa {', '.join(inactivas)} está fuera de servicio.")

    if combination is not None:
        if party_size > combination.max_seats:
            raise ReservationError(
                f"{combination.name} es para {combination.max_seats} personas como "
                f"máximo y el grupo es de {party_size}."
            )
    elif len(tables) > 1:
        sueltas = [t.code for t in tables if not t.is_combinable]
        if sueltas:
            raise ReservationError(
                f"La mesa {', '.join(sueltas)} no se puede juntar con otras."
            )

    capacity = sum(t.max_seats for t in tables)
    if combination is None and capacity < party_size:
        raise ReservationError(
            f"Las mesas elegidas suman {capacity} plazas y el grupo es de "
            f"{party_size}."
        )

    busy = busy_table_ids(venue, starts_at, ends_at, exclude_reservation)
    ocupadas = [t.code for t in tables if t.id in busy]
    if ocupadas:
        raise NoAvailability(
            f"La mesa {', '.join(ocupadas)} ya está ocupada a esa hora."
        )
    return tables


def _pick_tables(venue, starts_at, ends_at, party_size, tables):
    if tables is None:
        offers = find_availability(venue, starts_at, ends_at, party_size)
        if not offers:
            raise NoAvailability(f"No hay mesa para {party_size} personas a esa hora.")
        return list(offers[0].tables)

    return validate_tables(venue, starts_at, ends_at, party_size, tables)


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
    details=None,
    user=None,
    status=None,
    skip_calendar=False,
):
    details = details or ReservationDetails()
    policy = policy_for(venue)

    if source == Reservation.Source.WEB and party_size > policy.max_party_web:
        raise ReservationError(
            f"Los grupos de más de {policy.max_party_web} personas se reservan "
            f"por teléfono."
        )

    duration = duration_min or duration_for(venue, party_size)
    ends_at = starts_at + timedelta(minutes=duration)

    validate_lead_time(venue, starts_at, source)
    if skip_calendar:
        # Quien llega sin reserva se salta turnos y ultima entrada: decide el anfitrion.
        shift = shift_for(venue, starts_at)
    else:
        shift = validate_calendar(venue, starts_at)

    tables = _pick_tables(venue, starts_at, ends_at, party_size, tables)

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
        children=details.children,
        high_chairs=details.high_chairs,
        status=status,
        source=source,
        occasion=details.occasion,
        guest_notes=details.guest_notes,
        internal_notes=details.internal_notes,
        created_by=user,
    )
    if status == Reservation.Status.CONFIRMED:
        reservation.confirmed_at = timezone.now()
    reservation.save()

    _assign(reservation, tables)
    _log_event(reservation, user, "", status, "Reserva creada")
    if status == Reservation.Status.CONFIRMED:
        notifications.notify_confirmed(reservation)
    logger.info("Reserva %s creada en %s", reservation.code, reservation.table_codes)
    return reservation


def _assign(reservation, tables):
    # El constraint de Postgres atrapa la carrera entre dos hosts; se traduce a error legible.
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
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    reservation.occupancies.update(is_active=False)
    _assign(reservation, list(tables))
    _log_event(reservation, user, reservation.status, reservation.status,
               f"Reasignada a {reservation.table_codes}")
    return reservation


@transaction.atomic
def move(reservation, new_start, user=None, tables=None):
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if not reservation.is_live:
        raise InvalidTransition("Solo se puede mover una reserva viva.")

    new_end = new_start + timedelta(minutes=reservation.duration_min)
    shift = validate_calendar(reservation.venue, new_start)

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

    if new_status not in Reservation.LIVE_STATUSES:
        reservation.occupancies.update(is_active=False)

    _log_event(reservation, user, previous, new_status, comment)
    return reservation


@transaction.atomic
def confirm(reservation, user=None):
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    reservation = _transition(reservation, Reservation.Status.CONFIRMED, user,
                              extra_fields={"confirmed_at": timezone.now()})
    notifications.notify_confirmed(reservation)
    return reservation


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
    notifications.notify_cancelled(reservation)
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


# Ficha compartida para walk-ins que no dejan sus datos.
WALK_IN_PHONE = "+000000000"
WALK_IN_NAME = "Cliente de paso"

MIN_WALK_IN_MIN = 30


def walk_in_guest():
    guest, _ = Guest.objects.get_or_create(
        phone=WALK_IN_PHONE, defaults={"name": WALK_IN_NAME}
    )
    return guest


@transaction.atomic
def seat_walk_in(venue, table, party_size, name="", phone="", user=None, now=None):
    """Crea una reserva ya sentada para que la web deje de ofrecer la mesa; se corta antes de la siguiente reserva."""
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
        source=Reservation.Source.RECEPCION, tables=[table], duration_min=duration,
        user=user, status=Reservation.Status.SEATED, skip_calendar=True,
        details=ReservationDetails(internal_notes=(name if name and not phone else "")),
    )
    reservation.seated_at = now
    reservation.save(update_fields=["seated_at"])
    reservation.cut_at = corta_a  # para el mensaje de vuelta, no se guarda
    return reservation

