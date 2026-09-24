"""
Datos del panel de inicio.

Unfold llama a `dashboard_callback` al renderizar /admin/ y le pasa el
contexto de la plantilla. Todo lo que devuelve aqui lo dibuja
templates/admin/index.html.

Las consultas estan acotadas al servicio del dia, la semana y el mes (con
el mes anterior para comparar): la portada del panel tiene que abrir rapido
aunque la tabla de reservas crezca.
"""

from datetime import datetime, timedelta

from django.db.models import Count, Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from apps.reservations import services
from apps.reservations.admin import next_step_button
from apps.reservations.models import (
    Reservation, TableOccupancy, active_occupancies_prefetch,
)
from apps.venues.models import Table, Venue

STATUS_TONE = {
    Reservation.Status.PENDING: "warn",
    Reservation.Status.CONFIRMED: "info",
    Reservation.Status.SEATED: "ok",
    Reservation.Status.FINISHED: "muted",
    Reservation.Status.WAITLISTED: "info",
    Reservation.Status.CANCELLED: "muted",
    Reservation.Status.NO_SHOW: "bad",
}

DAY_NAMES = ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"]


# ---------------------------------------------------------------------------
# Resumen: manana, esta semana y este mes
# ---------------------------------------------------------------------------

#: Estados que cuentan como reserva "en firme" (lo cancelado y la lista de
#: espera no ocupan sala).
BOOKED = (
    Reservation.Status.PENDING, Reservation.Status.CONFIRMED, Reservation.Status.SEATED,
    Reservation.Status.FINISHED, Reservation.Status.NO_SHOW,
)
#: De esas, las que traen gente (el no-show reservo pero no vino).
CAME = (
    Reservation.Status.PENDING, Reservation.Status.CONFIRMED, Reservation.Status.SEATED,
    Reservation.Status.FINISHED,
)
MONTH_NAMES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
               "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
DAY_LETTERS = ["L", "M", "X", "J", "V", "S", "D"]


def _pct_change(now, before):
    """Variacion en % frente al periodo anterior, o None si no hay con que comparar."""
    if not before:
        return None
    return round((now - before) / before * 100)


def _summary(venue, today, list_url):
    """
    Cifras de manana, la semana (lun-dom) y el mes en curso.

    Todo sale de una sola consulta agrupada por dia y estado, desde el
    primer dia del mes anterior (para comparar) hasta el final de la semana
    o de manana, lo que llegue mas lejos.
    """
    tomorrow = today + timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    month_start = today.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    month_end = next_month - timedelta(days=1)
    prev_month_start = (month_start - timedelta(days=1)).replace(day=1)

    rows = (
        Reservation.objects.filter(
            venue=venue,
            service_date__gte=min(prev_month_start, week_start - timedelta(days=7)),
            service_date__lte=max(month_end, week_end, tomorrow),
        )
        .values("service_date", "status")
        .annotate(n=Count("id"), people=Sum("party_size"))
    )
    # dia -> estado -> (reservas, personas)
    by_day = {}
    for r in rows:
        by_day.setdefault(r["service_date"], {})[r["status"]] = (r["n"], r["people"] or 0)

    def tally(start, end):
        t = {"reservations": 0, "people": 0, "no_shows": 0, "cancelled": 0, "pending": 0}
        day = start
        while day <= end:
            for status, (n, people) in by_day.get(day, {}).items():
                if status in BOOKED:
                    t["reservations"] += n
                if status in CAME:
                    t["people"] += people
                if status == Reservation.Status.NO_SHOW:
                    t["no_shows"] += n
                if status == Reservation.Status.CANCELLED:
                    t["cancelled"] += n
                if status == Reservation.Status.PENDING:
                    t["pending"] += n
            day += timedelta(days=1)
        return t

    # Manana, con su lista.
    tm = tally(tomorrow, tomorrow)
    tomorrow_rows = list(
        Reservation.objects.filter(venue=venue, service_date=tomorrow, status__in=CAME)
        .select_related("guest")
        .prefetch_related(active_occupancies_prefetch())
        .order_by("starts_at")[:6]
    )
    tz = services.venue_tz(venue)
    for r in tomorrow_rows:
        r.local_time = timezone.localtime(r.starts_at, tz)
        r.tone = STATUS_TONE.get(r.status, "muted")
    tomorrow_qs = urlencode({"service_date__year": tomorrow.year,
                             "service_date__month": tomorrow.month,
                             "service_date__day": tomorrow.day})

    # Semana: toda la semana (incluye lo ya reservado para los dias que
    # faltan) y, para comparar, lo que llevaba la anterior a esta altura.
    wk = tally(week_start, week_end)
    wk_so_far = tally(week_start, today)
    wk_prev = tally(week_start - timedelta(days=7), today - timedelta(days=7))
    days = []
    for i in range(7):
        day = week_start + timedelta(days=i)
        people = sum(p for s, (n, p) in by_day.get(day, {}).items() if s in CAME)
        days.append({"letter": DAY_LETTERS[i], "number": day.day, "people": people,
                     "is_today": day == today, "is_future": day > today})
    top = max((d["people"] for d in days), default=0) or 1
    for d in days:
        d["pct"] = round(d["people"] / top * 100)

    # Mes: igual, comparando con los mismos dias del mes anterior.
    mo = tally(month_start, month_end)
    mo_so_far = tally(month_start, today)
    prev_same_day = min(today.day, (month_start - timedelta(days=1)).day)
    mo_prev = tally(prev_month_start, prev_month_start.replace(day=prev_same_day))
    best_day, best_people = None, 0
    day = month_start
    while day <= today:
        people = sum(p for s, (n, p) in by_day.get(day, {}).items() if s in CAME)
        if people > best_people:
            best_day, best_people = day, people
        day += timedelta(days=1)
    closed = mo_so_far["reservations"]  # reservas del mes hasta hoy
    month_qs = urlencode({"service_date__year": today.year,
                          "service_date__month": today.month})

    return {
        "tomorrow": {
            **tm,
            "date": tomorrow,
            "rows": tomorrow_rows,
            "more": max(0, tm["reservations"] - tm["no_shows"] - len(tomorrow_rows)),
            "url": f"{list_url}?{tomorrow_qs}",
        },
        "week": {
            **wk,
            "range": f"{week_start.day} – {week_end.day} {MONTH_NAMES[week_end.month - 1][:3]}",
            "days": days,
            "change": _pct_change(wk_so_far["people"], wk_prev["people"]),
        },
        "month": {
            **mo,
            "name": MONTH_NAMES[today.month - 1],
            "change": _pct_change(mo_so_far["people"], mo_prev["people"]),
            "no_show_rate": round(mo_so_far["no_shows"] / closed * 100) if closed else 0,
            "avg_party": round(mo_so_far["people"] / closed, 1) if closed else 0,
            "best_day": best_day,
            "best_people": best_people,
            "url": f"{list_url}?{month_qs}",
        },
    }



def _empty(context):
    context.update({"dashboard_ready": False})
    return context


def dashboard_callback(request, context):
    venue = Venue.objects.filter(is_active=True).order_by("name").first()
    if venue is None:
        return _empty(context)

    tz = services.venue_tz(venue)
    now = timezone.now()
    today = services.service_date_for(venue, now)

    todays = (
        Reservation.objects.filter(venue=venue, service_date=today)
        .select_related("guest")
        .prefetch_related(active_occupancies_prefetch())
    )
    live = [r for r in todays if r.is_live]

    covers = sum(r.party_size for r in live)
    capacity = venue.capacity or 0
    occupancy = round(covers / capacity * 100) if capacity else 0

    seated_now = [r for r in live if r.status == Reservation.Status.SEATED]
    no_shows = [r for r in todays if r.status == Reservation.Status.NO_SHOW]

    # Mesas libres en este instante: las que no tienen ocupacion activa viva.
    busy_ids = set(
        TableOccupancy.objects.filter(
            table__area__venue=venue,
            is_active=True,
            starts_at__lte=now,
            ends_at__gt=now,
        ).values_list("table_id", flat=True)
    )
    tables = list(
        Table.objects.filter(area__venue=venue, is_active=True)
        .select_related("area")
        .order_by("area__sort_order", "code")
    )
    free_tables = [t for t in tables if t.id not in busy_ids]

    upcoming = sorted(
        (r for r in live if r.starts_at >= now - timedelta(minutes=30)),
        key=lambda r: r.starts_at,
    )

    # Reservas cuya gracia ya vencio y siguen sin sentarse: es la fila que el
    # anfitrion tiene que mirar primero.
    grace = services.policy_for(venue).no_show_grace_min
    overdue = [
        r for r in live
        if r.status in (Reservation.Status.PENDING, Reservation.Status.CONFIRMED)
        and r.starts_at < now - timedelta(minutes=grace)
    ]

    # Lo que pasa en la sala en este instante.
    now_kpis = [
        {
            "icon": "groups",
            "label": "En sala",
            "value": sum(r.party_size for r in seated_now),
            "hint": f"{len(seated_now)} mesa{'s' if len(seated_now) != 1 else ''} sentada"
                    f"{'s' if len(seated_now) != 1 else ''}",
        },
        {
            "icon": "table_restaurant",
            "label": "Mesas libres",
            "value": len(free_tables),
            "hint": ", ".join(t.code for t in free_tables[:4]) or "sala completa",
        },
        {
            "icon": "schedule",
            "label": "Sin llegar",
            "value": len(overdue),
            "hint": f"pasados los {grace} min de cortesia" if overdue else "nadie con retraso",
            "tone": "bad" if overdue else "muted",
        },
    ]

    # Como viene el dia entero.
    day_kpis = [
        {
            "icon": "restaurant",
            "label": "Cubiertos",
            "value": covers,
            "hint": f"de {capacity} plazas" if capacity else "sin mesas cargadas",
        },
        {
            "icon": "donut_large",
            "label": "Ocupación",
            "value": f"{occupancy}%",
            "hint": f"{len(live)} reserva{'s' if len(live) != 1 else ''} en pie",
        },
        {
            "icon": "person_off",
            "label": "No-shows",
            "value": len(no_shows),
            "hint": "en este servicio",
            "tone": "bad" if no_shows else "muted",
        },
    ]

    # Ocupacion por turno del dia.
    shifts = []
    for shift in venue.shifts.filter(is_active=True).order_by("sort_order", "start_time"):
        if not shift.applies_on(today):
            continue
        rows = [r for r in live if r.shift_id == shift.id]
        shift_covers = sum(r.party_size for r in rows)
        shifts.append({
            "name": shift.name,
            "hours": f"{shift.start_time:%H:%M} - {shift.end_time:%H:%M}",
            "last_seating": shift.last_seating,
            "reservations": len(rows),
            "covers": shift_covers,
            "pct": round(shift_covers / capacity * 100) if capacity else 0,
        })

    # Cubiertos de los ultimos siete dias de servicio.
    since = today - timedelta(days=6)
    per_day = {
        row["service_date"]: row
        for row in Reservation.objects.filter(
            venue=venue,
            service_date__gte=since,
            service_date__lte=today,
            status__in=[*Reservation.LIVE_STATUSES, Reservation.Status.FINISHED],
        )
        .values("service_date")
        .annotate(covers=Sum("party_size"), reservations=Count("id"))
    }
    history = []
    for offset in range(7):
        day = since + timedelta(days=offset)
        row = per_day.get(day)
        history.append({
            "day": day,
            "label": DAY_NAMES[day.weekday()],
            "number": day.day,
            "covers": (row or {}).get("covers") or 0,
            "reservations": (row or {}).get("reservations") or 0,
            "is_today": day == today,
        })
    peak = max((h["covers"] for h in history), default=0) or 1
    for row in history:
        row["pct"] = round(row["covers"] / peak * 100)

    # Estado de la sala agrupado por zona.
    areas = {}
    for table in tables:
        bucket = areas.setdefault(
            table.area.name, {"name": table.area.name, "tables": []}
        )
        bucket["tables"].append({
            "code": table.code,
            "seats": f"{table.min_seats}-{table.max_seats}",
            "busy": table.id in busy_ids,
        })

    # Lista de reservas filtrada al servicio de hoy (usa date_hierarchy).
    hoy_qs = urlencode({
        "service_date__year": today.year,
        "service_date__month": today.month,
        "service_date__day": today.day,
    })
    list_url = reverse("admin:reservations_reservation_changelist")
    today_url = f"{list_url}?{hoy_qs}"
    index_url = reverse("admin:index")

    for reservation in upcoming:
        reservation.step = next_step_button(reservation, index_url)
        reservation.tone = STATUS_TONE.get(reservation.status, "muted")
        reservation.local_time = timezone.localtime(reservation.starts_at, tz)
        reservation.is_overdue = reservation in overdue

    # Ocupacion franja a franja del servicio de hoy. Se cuenta cuanta gente
    # hay sentada en cada media hora, no cuantas reservas empiezan: es lo que
    # dice de verdad cuando se satura la cocina.
    hourly_labels, hourly_values = [], []
    abiertos = [s for s in venue.shifts.filter(is_active=True) if s.applies_on(today)]
    if abiertos:
        inicio = min(s.start_time for s in abiertos)
        cierre = max(s.end_time for s in abiertos)
        cursor = datetime.combine(today, inicio, tzinfo=tz)
        limite = datetime.combine(today, cierre, tzinfo=tz)
        if cierre <= inicio:
            limite += timedelta(days=1)
        while cursor < limite:
            fin_franja = cursor + timedelta(minutes=30)
            hourly_labels.append(cursor.strftime("%H:%M"))
            hourly_values.append(
                sum(r.party_size for r in live
                    if r.starts_at < fin_franja and r.ends_at > cursor)
            )
            cursor = fin_franja

    # De donde llegan las reservas, ultimos 30 dias.
    etiquetas_origen = dict(Reservation.Source.choices)
    origenes = (
        Reservation.objects.filter(venue=venue, service_date__gte=today - timedelta(days=29),
                                   service_date__lte=today)
        .values("source")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    source_labels = [etiquetas_origen.get(o["source"], o["source"]) for o in origenes]
    source_values = [o["total"] for o in origenes]

    context.update({
        "dashboard_ready": True,
        "venue": venue,
        "service_date": today,
        "now_kpis": now_kpis,
        "day_kpis": day_kpis,
        "shifts": shifts,
        "history": history,
        "history_labels": [f"{h['label']} {h['number']}" for h in history],
        "history_values": [h["covers"] for h in history],
        "history_total": sum(h["covers"] for h in history),
        "hourly_labels": hourly_labels,
        "hourly_values": hourly_values,
        "hourly_peak": max(hourly_values) if hourly_values else 0,
        "source_labels": source_labels,
        "source_values": source_values,
        "areas": list(areas.values()),
        "upcoming": upcoming[:8],
        "upcoming_total": len(upcoming),
        "overdue_count": len(overdue),
        "today_url": today_url,
        "summary": _summary(venue, today, list_url),
        "overdue_url": f"{today_url}&{urlencode({'status__in': 'pending,confirmed'})}",
    })
    return context
