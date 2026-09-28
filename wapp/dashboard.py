"""Datos del panel de inicio (Unfold -> dashboard_callback -> templates/admin/index.html)."""

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


#: Reservas "en firme": lo cancelado y la lista de espera no ocupan sala.
BOOKED = (
    Reservation.Status.PENDING, Reservation.Status.CONFIRMED, Reservation.Status.SEATED,
    Reservation.Status.FINISHED, Reservation.Status.NO_SHOW,
)
CAME = (
    Reservation.Status.PENDING, Reservation.Status.CONFIRMED, Reservation.Status.SEATED,
    Reservation.Status.FINISHED,
)
MONTH_NAMES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
               "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
DAY_LETTERS = ["L", "M", "X", "J", "V", "S", "D"]


def _pct_change(now, before):
    if not before:
        return None
    return round((now - before) / before * 100)


def _counts_by_day(venue, start, end):
    """dia -> estado -> (reservas, personas)."""
    rows = (
        Reservation.objects.filter(
            venue=venue,
            service_date__gte=start,
            service_date__lte=end,
        )
        .values("service_date", "status")
        .annotate(n=Count("id"), people=Sum("party_size"))
    )
    by_day = {}
    for r in rows:
        by_day.setdefault(r["service_date"], {})[r["status"]] = (r["n"], r["people"] or 0)
    return by_day


def _add_to_tally(t, status, n, people):
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


def _tally(by_day, start, end):
    t = {"reservations": 0, "people": 0, "no_shows": 0, "cancelled": 0, "pending": 0}
    day = start
    while day <= end:
        for status, (n, people) in by_day.get(day, {}).items():
            _add_to_tally(t, status, n, people)
        day += timedelta(days=1)
    return t


def _people_on(by_day, day):
    return sum(p for s, (n, p) in by_day.get(day, {}).items() if s in CAME)


def _tomorrow_summary(venue, by_day, tomorrow, list_url):
    tm = _tally(by_day, tomorrow, tomorrow)
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
    return {
        **tm,
        "date": tomorrow,
        "rows": tomorrow_rows,
        "more": max(0, tm["reservations"] - tm["no_shows"] - len(tomorrow_rows)),
        "url": f"{list_url}?{tomorrow_qs}",
    }


def _week_days(by_day, week_start, today):
    days = []
    for i in range(7):
        day = week_start + timedelta(days=i)
        days.append({"letter": DAY_LETTERS[i], "number": day.day,
                     "people": _people_on(by_day, day),
                     "is_today": day == today, "is_future": day > today})
    top = max((d["people"] for d in days), default=0) or 1
    for d in days:
        d["pct"] = round(d["people"] / top * 100)
    return days


def _week_summary(by_day, today, week_start, week_end):
    """Se compara con lo que llevaba la semana anterior a esta altura."""
    wk = _tally(by_day, week_start, week_end)
    wk_so_far = _tally(by_day, week_start, today)
    wk_prev = _tally(by_day, week_start - timedelta(days=7), today - timedelta(days=7))
    return {
        **wk,
        "range": f"{week_start.day} – {week_end.day} {MONTH_NAMES[week_end.month - 1][:3]}",
        "days": _week_days(by_day, week_start, today),
        "change": _pct_change(wk_so_far["people"], wk_prev["people"]),
    }


def _best_day(by_day, start, end):
    best_day, best_people = None, 0
    day = start
    while day <= end:
        people = _people_on(by_day, day)
        if people > best_people:
            best_day, best_people = day, people
        day += timedelta(days=1)
    return best_day, best_people


def _month_summary(by_day, today, month_start, month_end, prev_month_start, list_url):
    mo = _tally(by_day, month_start, month_end)
    mo_so_far = _tally(by_day, month_start, today)
    prev_same_day = min(today.day, (month_start - timedelta(days=1)).day)
    mo_prev = _tally(by_day, prev_month_start, prev_month_start.replace(day=prev_same_day))
    best_day, best_people = _best_day(by_day, month_start, today)
    closed = mo_so_far["reservations"]
    month_qs = urlencode({"service_date__year": today.year,
                          "service_date__month": today.month})
    return {
        **mo,
        "name": MONTH_NAMES[today.month - 1],
        "change": _pct_change(mo_so_far["people"], mo_prev["people"]),
        "no_show_rate": round(mo_so_far["no_shows"] / closed * 100) if closed else 0,
        "avg_party": round(mo_so_far["people"] / closed, 1) if closed else 0,
        "best_day": best_day,
        "best_people": best_people,
        "url": f"{list_url}?{month_qs}",
    }


def _summary(venue, today, list_url):
    """Manana, semana (lun-dom) y mes, todo de una sola consulta agrupada."""
    tomorrow = today + timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    month_start = today.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    month_end = next_month - timedelta(days=1)
    prev_month_start = (month_start - timedelta(days=1)).replace(day=1)

    by_day = _counts_by_day(
        venue,
        min(prev_month_start, week_start - timedelta(days=7)),
        max(month_end, week_end, tomorrow),
    )
    return {
        "tomorrow": _tomorrow_summary(venue, by_day, tomorrow, list_url),
        "week": _week_summary(by_day, today, week_start, week_end),
        "month": _month_summary(by_day, today, month_start, month_end,
                                prev_month_start, list_url),
    }


def _empty(context):
    context.update({"dashboard_ready": False})
    return context


def _plural(n):
    return "s" if n != 1 else ""


def _busy_table_ids(venue, now):
    return set(
        TableOccupancy.objects.filter(
            table__area__venue=venue,
            is_active=True,
            starts_at__lte=now,
            ends_at__gt=now,
        ).values_list("table_id", flat=True)
    )


def _now_kpis(seated_now, free_tables, overdue, grace):
    return [
        {
            "icon": "groups",
            "label": "En sala",
            "value": sum(r.party_size for r in seated_now),
            "hint": f"{len(seated_now)} mesa{_plural(len(seated_now))} sentada"
                    f"{_plural(len(seated_now))}",
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


def _day_kpis(live, no_shows, covers, capacity, occupancy):
    return [
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
            "hint": f"{len(live)} reserva{_plural(len(live))} en pie",
        },
        {
            "icon": "person_off",
            "label": "No-shows",
            "value": len(no_shows),
            "hint": "en este servicio",
            "tone": "bad" if no_shows else "muted",
        },
    ]


def _shift_rows(venue, today, live, capacity):
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
    return shifts


def _history(venue, today):
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
        row = per_day.get(day) or {}
        history.append({
            "day": day,
            "label": DAY_NAMES[day.weekday()],
            "number": day.day,
            "covers": row.get("covers") or 0,
            "reservations": row.get("reservations") or 0,
            "is_today": day == today,
        })
    peak = max((h["covers"] for h in history), default=0) or 1
    for row in history:
        row["pct"] = round(row["covers"] / peak * 100)
    return history


def _areas(tables, busy_ids):
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
    return list(areas.values())


def _decorate_upcoming(upcoming, overdue, tz, index_url):
    for reservation in upcoming:
        reservation.step = next_step_button(reservation, index_url)
        reservation.tone = STATUS_TONE.get(reservation.status, "muted")
        reservation.local_time = timezone.localtime(reservation.starts_at, tz)
        reservation.is_overdue = reservation in overdue


def _hourly(venue, today, tz, live):
    """Gente sentada por media hora (no reservas que empiezan): eso satura la cocina."""
    hourly_labels, hourly_values = [], []
    abiertos = [s for s in venue.shifts.filter(is_active=True) if s.applies_on(today)]
    if not abiertos:
        return hourly_labels, hourly_values
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
    return hourly_labels, hourly_values


def _sources(venue, today):
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
    return source_labels, source_values


def dashboard_callback(_request, context):
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

    busy_ids = _busy_table_ids(venue, now)
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

    grace = services.policy_for(venue).no_show_grace_min
    overdue = [
        r for r in live
        if r.status in (Reservation.Status.PENDING, Reservation.Status.CONFIRMED)
        and r.starts_at < now - timedelta(minutes=grace)
    ]

    history = _history(venue, today)

    hoy_qs = urlencode({
        "service_date__year": today.year,
        "service_date__month": today.month,
        "service_date__day": today.day,
    })
    list_url = reverse("admin:reservations_reservation_changelist")
    today_url = f"{list_url}?{hoy_qs}"

    _decorate_upcoming(upcoming, overdue, tz, reverse("admin:index"))

    hourly_labels, hourly_values = _hourly(venue, today, tz, live)
    source_labels, source_values = _sources(venue, today)

    context.update({
        "dashboard_ready": True,
        "venue": venue,
        "service_date": today,
        "now_kpis": _now_kpis(seated_now, free_tables, overdue, grace),
        "day_kpis": _day_kpis(live, no_shows, covers, capacity, occupancy),
        "shifts": _shift_rows(venue, today, live, capacity),
        "history": history,
        "history_labels": [f"{h['label']} {h['number']}" for h in history],
        "history_values": [h["covers"] for h in history],
        "history_total": sum(h["covers"] for h in history),
        "hourly_labels": hourly_labels,
        "hourly_values": hourly_values,
        "hourly_peak": max(hourly_values) if hourly_values else 0,
        "source_labels": source_labels,
        "source_values": source_values,
        "areas": _areas(tables, busy_ids),
        "upcoming": upcoming[:8],
        "upcoming_total": len(upcoming),
        "overdue_count": len(overdue),
        "today_url": today_url,
        "summary": _summary(venue, today, list_url),
        "overdue_url": f"{today_url}&{urlencode({'status__in': 'pending,confirmed'})}",
    })
    return context
