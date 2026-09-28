"""Plano de sala. Las vistas van envueltas en admin_view (wapp/urls.py)."""

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from decimal import Decimal

from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.http import require_POST

from apps.reservations import services
from apps.reservations.admin import next_step_button, reservation_url
from apps.reservations.models import Reservation, TableOccupancy
from apps.venues.models import Table, Venue

#: Con cuanta antelacion una mesa libre pasa a "llega pronto".
SOON_MIN = 45

SLOT_MIN = 30

#: Alto maximo, en px, de las barras de esa tira (sala llena).
HEAT_PX = 40

STATES = {
    # clave: (etiqueta, tono de chip)
    "free": ("Libre", "ok"),
    "soon": ("Llega pronto", "info"),
    "waiting": ("Esperando cliente", "warn"),
    "late": ("Sin llegar", "bad"),
    "seated": ("Ocupada", "primary"),
    "blocked": ("Bloqueada", "muted"),
}

BAR_TONES = {
    Reservation.Status.PENDING: "pending",
    Reservation.Status.CONFIRMED: "booked",
    Reservation.Status.SEATED: "seated",
    Reservation.Status.FINISHED: "done",
}

BAR_LEGEND = [
    ("done", "Ya se fue"),
    ("seated", "Sentada"),
    ("booked", "Confirmada"),
    ("pending", "Por confirmar"),
    ("late", "Sin llegar"),
    ("blocked", "Bloqueo"),
]


def _parse_moment(request, tz):
    """Instante pedido en ?fecha=YYYY-MM-DD&hora=HH:MM, o ahora."""
    fecha, hora = request.GET.get("fecha"), request.GET.get("hora")
    if fecha and hora:
        try:
            local = datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M")
            return local.replace(tzinfo=tz), False
        except ValueError:
            pass
    return timezone.now(), True


def _minutes(delta):
    return max(0, round(delta.total_seconds() / 60))


def _human(minutos):
    if minutos < 60:
        return f"{minutos} min"
    h, m = divmod(minutos, 60)
    return f"{h} h {m:02d}" if m else f"{h} h"


def _area_icon(name):
    nombre = name.lower()
    if "terr" in nombre or "patio" in nombre or "jard" in nombre:
        return "deck"
    if "barra" in nombre or "bar" == nombre:
        return "local_bar"
    if "priv" in nombre or "vip" in nombre:
        return "meeting_room"
    return "restaurant"


def _chairs(table, taken):
    """Sillas como estilos CSS relativos a la mesa; las primeras `taken` salen ocupadas."""
    n = table.max_seats
    sitios = []
    if table.shape == "round":
        for i in range(n):
            ang = math.radians(180 + 360 * i / n) if n <= 2 else math.radians(-90 + 360 * i / n)
            cx, cy = math.cos(ang), math.sin(ang)
            sitios.append(f"left:calc(50% + {cx:.3f} * (50% + 9px));"
                          f"top:calc(50% + {cy:.3f} * (50% + 9px))")
    elif table.shape == "square":
        lados = ["left:50%;top:-9px", "left:50%;top:calc(100% + 9px)",
                 "left:-9px;top:50%", "left:calc(100% + 9px);top:50%"]
        sitios = lados[:n]
    else:
        largos = n - 2
        arriba = math.ceil(largos / 2)
        abajo = largos - arriba
        sitios.append("left:-9px;top:50%")
        sitios.append("left:calc(100% + 9px);top:50%")
        for i in range(arriba):
            sitios.append(f"left:{(i + 0.5) / arriba * 100:.1f}%;top:-9px")
        for i in range(abajo):
            sitios.append(f"left:{(i + 0.5) / abajo * 100:.1f}%;top:calc(100% + 9px)")
    return [{"style": s, "taken": i < taken} for i, s in enumerate(sitios)]


def _service_window(venue, service_date, tz, spans):
    """Del primer turno al ultimo; sin turnos, lo que cubran las reservas o 12:00-24:00."""
    turnos = [s for s in venue.shifts.filter(is_active=True) if s.applies_on(service_date)]
    if turnos:
        inicio = datetime.combine(service_date, min(s.start_time for s in turnos), tzinfo=tz)
        cierre = datetime.combine(service_date, max(s.end_time for s in turnos), tzinfo=tz)
        if cierre <= inicio:
            cierre += timedelta(days=1)
    elif spans:
        inicio = min(s["start"] for s in spans).astimezone(tz).replace(minute=0, second=0)
        cierre = max(s["end"] for s in spans).astimezone(tz)
    else:
        inicio = datetime.combine(service_date, time(12), tzinfo=tz)
        cierre = inicio + timedelta(hours=12)
    inicio = inicio.replace(minute=0, second=0, microsecond=0)
    if cierre.minute or cierre.second:
        cierre = cierre.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return inicio, cierre


#: Tiene que coincidir con .zs-plan-inner en static/admin/zisa.css.
PLAN_INSET = {"top": 104, "right": 112, "bottom": 76, "left": 112}

#: Ancho de referencia; el real lo pone la tarjeta, el alto es fijo.
PLAN_NOMINAL_W = 1000


def _half(table):
    """Medio ancho y medio alto del tablero en px (ver .zs-ft en zisa.css)."""
    if table.shape == "round":
        return 30, 30
    if table.shape == "long":
        return table.width / 2, 37
    return 37, 37


#: Margenes de zona en px: silla + aire, etiqueta y hueco entre zonas.
ZONE_CHAIR = 26
ZONE_LABEL = 34
ZONE_GAP = 10


def _inner_height(inner_w, sx, sy):
    """Alto que conserva la proporcion del lienzo 16:9, acotado para caber en pantalla."""
    if sx >= 1 and sy >= 1:
        inner_h = inner_w * (sy * 9) / (sx * 16)
    else:
        inner_h = 0
    return round(min(440, max(210, inner_h)))


def _grow_box(b, t):
    hw, hh = _half(t)
    if t.dx < b["x0"]:
        b["x0"], b["pl"] = t.dx, hw + ZONE_CHAIR
    elif t.dx == b["x0"]:
        b["pl"] = max(b["pl"], hw + ZONE_CHAIR)
    if t.dx > b["x1"]:
        b["x1"], b["pr"] = t.dx, hw + ZONE_CHAIR
    elif t.dx == b["x1"]:
        b["pr"] = max(b["pr"], hw + ZONE_CHAIR)
    b["y0"], b["y1"] = min(b["y0"], t.dy), max(b["y1"], t.dy)
    b["hh"] = max(b["hh"], hh)


def _zone_boxes(tables):
    boxes = {}
    for t in tables:
        b = boxes.setdefault(t.area_id, {
            "name": t.area.name, "icon": _area_icon(t.area.name),
            "idx": len(boxes) % 4,
            "x0": 101.0, "x1": -1.0, "y0": 101.0, "y1": -1.0,
            "pl": 0, "pr": 0, "hh": 0,
        })
        _grow_box(b, t)

    boxes = list(boxes.values())
    # Cada borde es [% del area interior, px]: left = calc(x% - px).
    for b in boxes:
        b["l"] = [b["x0"], -b["pl"]]
        b["r"] = [b["x1"], b["pr"]]
        b["t"] = [b["y0"], -(b["hh"] + ZONE_CHAIR + ZONE_LABEL)]
        b["b"] = [b["y1"], b["hh"] + ZONE_CHAIR]
    return boxes


def _cruza(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1


def _ay(e, inner_h):
    return e[0] / 100 * inner_h + e[1]


def _side_by_side(a, b, inner_h):
    return (_cruza(_ay(a["t"], inner_h), _ay(a["b"], inner_h),
                   _ay(b["t"], inner_h), _ay(b["b"], inner_h))
            and not _cruza(a["x0"], a["x1"], b["x0"], b["x1"]))


def _match_heights(boxes, inner_h):
    def ay(e):
        return _ay(e, inner_h)

    for a in boxes:
        for b in boxes:
            if a is b or not _side_by_side(a, b, inner_h):
                continue
            arriba = min(a["t"], b["t"], key=ay)
            abajo = max(a["b"], b["b"], key=ay)
            a["t"] = b["t"] = list(arriba)
            a["b"] = b["b"] = list(abajo)


def _junta(a1, b0, pa, pb):
    return [(a1 + b0) / 2, (pa - pb) / 2]


def _join_side(a, b, boxes):
    izq, der = (a, b) if a["x1"] < b["x0"] else (b, a)
    en_medio = any(c is not a and c is not b and izq["x1"] < c["x0"] and c["x1"] < der["x0"]
                   and _cruza(c["y0"], c["y1"], izq["y0"], izq["y1"]) for c in boxes)
    if not en_medio:
        m = _junta(izq["x1"], der["x0"], izq["pr"], der["pl"])
        izq["r"], der["l"] = [m[0], m[1] - ZONE_GAP / 2], [m[0], m[1] + ZONE_GAP / 2]


def _join_stacked(a, b):
    arr, aba = (a, b) if a["y1"] < b["y0"] else (b, a)
    m = [(arr["y1"] + aba["y0"]) / 2, 0]
    arr["b"], aba["t"] = [m[0], -ZONE_GAP / 2], [m[0], ZONE_GAP / 2]


def _share_borders(boxes):
    """Zonas vecinas se reparten el hueco: asi nunca se pisan, sea cual sea el ancho."""
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            misma_franja = _cruza(a["y0"] - 1, a["y1"] + 1, b["y0"] - 1, b["y1"] + 1)
            misma_columna = _cruza(a["x0"] - 1, a["x1"] + 1, b["x0"] - 1, b["x1"] + 1)
            if misma_franja and not misma_columna:
                _join_side(a, b, boxes)
            elif misma_columna and not misma_franja:
                _join_stacked(a, b)


def _css(e):
    signo = "+" if e[1] >= 0 else "-"
    return f"calc({e[0]:.3f}% {signo} {abs(e[1]):.0f}px)"


def _tramo(a, z):
    return f"calc({z[0] - a[0]:.3f}% + {z[1] - a[1]:.0f}px)"


def _layout(tables):
    """Estira el rectangulo que ocupan las mesas (no el lienzo entero); deja dx/dy en cada mesa."""
    if not tables:
        return 480, []

    xs = [t.x for t in tables]
    ys = [t.y for t in tables]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    sx, sy = x1 - x0, y1 - y0

    for t in tables:
        t.dx = (t.x - x0) / sx * 100 if sx >= 1 else 50.0
        t.dy = (t.y - y0) / sy * 100 if sy >= 1 else 50.0

    ins = PLAN_INSET
    inner_w = PLAN_NOMINAL_W - ins["left"] - ins["right"]
    inner_h = _inner_height(inner_w, sx, sy)
    height = inner_h + ins["top"] + ins["bottom"]

    boxes = _zone_boxes(tables)
    _match_heights(boxes, inner_h)
    _share_borders(boxes)

    for b in boxes:
        b["style"] = (f"left:{_css(b['l'])};top:{_css(b['t'])};"
                      f"width:{_tramo(b['l'], b['r'])};height:{_tramo(b['t'], b['b'])}")
    return height, boxes


@dataclass(frozen=True)
class _Moment:
    at: datetime
    real_now: datetime      # hora real: lo atrasado se mide contra ella
    tz: tzinfo
    grace: int              # minutos de cortesia antes de dar a alguien por tarde
    service_date: date
    here: str               # vuelta de los botones de siguiente paso

    def hhmm(self, dt):
        return f"{timezone.localtime(dt, self.tz):%H:%M}"


def _change_url(r):
    return reservation_url("change", r.pk)


def _pick_venue(request):
    venues = list(Venue.objects.filter(is_active=True).order_by("name"))
    venue = next((v for v in venues if str(v.pk) == request.GET.get("local")), None)
    return venues, venue or (venues[0] if venues else None)


def _occupancies(venue, service_date, at):
    return (
        TableOccupancy.objects.filter(table__area__venue=venue, is_active=True)
        .filter(
            Q(reservation__service_date=service_date)
            | Q(reservation__isnull=True,
                starts_at__lt=at + timedelta(hours=12),
                ends_at__gt=at - timedelta(hours=12))
        )
        .select_related("reservation__guest", "reservation__venue")
        .order_by("starts_at")
    )


def _finished_occupancies(venue, service_date):
    """Las terminadas ya no estan activas, pero cuentan para la vista por horas."""
    return (
        TableOccupancy.objects.filter(
            table__area__venue=venue, is_active=False,
            reservation__status=Reservation.Status.FINISHED,
            reservation__service_date=service_date,
        )
        .select_related("reservation__guest")
    )


def _group_by_table(occupancies):
    by_table = {}
    for occ in occupancies:
        by_table.setdefault(occ.table_id, []).append(occ)
    return by_table


def _idle_state(upcoming, at):
    if (upcoming and not upcoming.is_block
            and upcoming.starts_at - at <= timedelta(minutes=SOON_MIN)):
        return "soon"
    return "free"


def _block_info(block, m):
    return {
        "kind": "block",
        "reason": block.block_reason,
        "range": f"{m.hhmm(block.starts_at)} - {m.hhmm(block.ends_at)}",
        "remaining": _human(_minutes(block.ends_at - m.at)),
    }


def _reservation_state(r, m):
    progress = 0
    if r.status == Reservation.Status.SEATED:
        state = "seated"
        total = (r.ends_at - r.starts_at).total_seconds() or 1
        progress = min(100, max(0, round((m.at - r.starts_at).total_seconds() / total * 100)))
    elif m.at > r.starts_at + timedelta(minutes=m.grace):
        state = "late"
    else:
        state = "waiting"
    now_info = {
        "kind": "reservation",
        "guest": r.guest.name,
        "phone": r.guest.phone,
        "party": r.party_size,
        "range": f"{m.hhmm(r.starts_at)} - {m.hhmm(r.ends_at)}",
        "until": m.hhmm(r.ends_at),
        "status": r.get_status_display(),
        "remaining": _human(_minutes(r.ends_at - m.at)),
        "late_by": _human(_minutes(m.at - r.starts_at)) if state == "late" else "",
        "occasion": r.occasion,
        "notes": r.guest_notes,
        "risky": r.guest.no_shows if r.guest.is_risky else 0,
        "progress": progress,
        "url": _change_url(r),
        "step": str(next_step_button(r, m.here)),
    }
    return state, now_info, progress


def _now_state(current, upcoming, m):
    if current is None:
        return _idle_state(upcoming, m.at), None, 0
    if current.is_block:
        return "blocked", _block_info(current, m), 0
    return _reservation_state(current.reservation, m)


def _next_info(upcoming, m):
    if not upcoming or upcoming.is_block:
        return None, False
    r = upcoming.reservation
    next_info = {
        "time": m.hhmm(r.starts_at),
        "guest": r.guest.name,
        "party": r.party_size,
        "in": _human(_minutes(r.starts_at - m.at)),
        "url": _change_url(r),
        "step": str(next_step_button(r, m.here)),
    }
    return next_info, r.starts_at - m.at <= timedelta(hours=1)


def _span(o, current, m):
    if o.is_block:
        return {"start": o.starts_at, "end": o.ends_at, "tone": "blocked",
                "label": o.block_reason, "status": "Bloqueo", "url": "",
                "party": 0, "is_current": o is current}
    r = o.reservation
    if r.service_date != m.service_date:
        return None
    tone = BAR_TONES.get(r.status, "booked")
    # Contra la hora real, no la del plano: rojo significa "esta pasando ahora".
    if (r.status in (Reservation.Status.PENDING, Reservation.Status.CONFIRMED)
            and m.real_now > r.starts_at + timedelta(minutes=m.grace)):
        tone = "late"
    return {"start": o.starts_at, "end": o.ends_at, "tone": tone,
            "label": r.guest.name, "party": r.party_size,
            "status": r.get_status_display(), "url": _change_url(r),
            "is_current": o is current}


def _done_span(o):
    r = o.reservation
    inicio = r.seated_at or o.starts_at
    fin = r.released_at or o.ends_at
    # Sentada y cerrada casi a la vez: se marco tarde, se usa el horario reservado.
    if fin - inicio < timedelta(minutes=10):
        inicio, fin = o.starts_at, o.ends_at
    return {"start": inicio, "end": fin, "tone": "done",
            "label": r.guest.name, "party": r.party_size,
            "status": "Finalizada", "url": _change_url(r),
            "is_current": False}


def _table_spans(rows, done_rows, current, m):
    spans = [s for s in (_span(o, current, m) for o in rows) if s is not None]
    spans.extend(_done_span(o) for o in done_rows)
    spans.sort(key=lambda s: s["start"])
    for s in spans:
        s["time"] = f"{m.hhmm(s['start'])}–{m.hhmm(s['end'])}"
    return spans


def _table_payload(table, state, now_info, next_info, spans):
    return {
        "code": table.code,
        "area": table.area.name,
        "seats": f"{table.min_seats}-{table.max_seats}",
        "min": table.min_seats,
        "max": table.max_seats,
        "state": state,
        "state_label": STATES[state][0],
        "tone": STATES[state][1],
        "now": now_info,
        "next": next_info,
        "day": [
            {"time": s["time"],
             "label": s["label"] + (f" · {s['party']}p" if s["party"] else ""),
             "status": s["status"], "url": s["url"], "tone": s["tone"],
             "is_current": s["is_current"]}
            for s in spans
        ],
    }


def _table_shape(max_seats):
    if max_seats <= 2:
        return "round"
    if max_seats <= 4:
        return "square"
    return "long"


def _table_hint(table, state, now_info, next_info, party):
    if state == "seated":
        return f"hasta {now_info['until']}"
    if state == "late":
        return f"+{now_info['late_by']}"
    if state == "waiting":
        return f"{party} pax"
    if state == "soon":
        return f"a las {next_info['time']}"
    if state == "blocked":
        return "bloqueada"
    return f"{table.min_seats}-{table.max_seats} pax"


def _draw_table(table, state, progress, now_info, next_info):
    table.state = state
    table.x = float(table.pos_x)
    table.y = float(table.pos_y)
    table.shape = _table_shape(table.max_seats)
    if table.shape == "long":
        table.width = 64 + 34 * math.ceil((table.max_seats - 2) / 2)
    table.progress = progress

    party = now_info["party"] if now_info and now_info["kind"] == "reservation" else 0
    table.chairs = _chairs(table, min(party, table.max_seats))
    table.chair_tone = state
    table.hint = _table_hint(table, state, now_info, next_info, party)


def _fill_tables(tables, by_table, done_by_table, m):
    counts = dict.fromkeys(STATES, 0)
    people_in = 0
    arriving = 0
    payload = {}
    all_spans = []

    for table in tables:
        rows = by_table.get(table.id, [])
        current = next((o for o in rows if o.starts_at <= m.at < o.ends_at), None)
        upcoming = next((o for o in rows if o.starts_at > m.at), None)

        state, now_info, progress = _now_state(current, upcoming, m)
        counts[state] += 1
        if state == "seated":
            people_in += now_info["party"]

        next_info, llega_pronto = _next_info(upcoming, m)
        if llega_pronto:
            arriving += 1

        spans = _table_spans(rows, done_by_table.get(table.id, []), current, m)
        table.spans = spans
        all_spans.extend(spans)

        payload[str(table.pk)] = _table_payload(table, state, now_info, next_info, spans)
        _draw_table(table, state, progress, now_info, next_info)

    return counts, people_in, arriving, payload, all_spans


def _reservations_on_tables(occupancies, tables, service_date):
    codigo = {t.id: t.code for t in tables}
    por_reserva = {}
    for occ in occupancies:
        r = occ.reservation
        if r is None or r.service_date != service_date:
            continue
        item = por_reserva.setdefault(r.pk, {"r": r, "tables": [], "table_id": occ.table_id})
        item["tables"].append(codigo.get(occ.table_id, "?"))
    return por_reserva


def _arrival_when(r, at, late):
    if late:
        return f"+{_human(_minutes(at - r.starts_at))}"
    if r.starts_at > at:
        return f"en {_human(_minutes(r.starts_at - at))}"
    return "ahora"


def _arrivals_and_freeing(por_reserva, m):
    at = m.at
    arrivals, freeing = [], []
    for item in por_reserva.values():
        r = item["r"]
        base = {
            "time": m.hhmm(r.starts_at),
            "guest": r.guest.name,
            "party": r.party_size,
            "tables": ", ".join(sorted(item["tables"])),
            "table_id": str(item["table_id"]),
        }
        if r.status in (Reservation.Status.PENDING, Reservation.Status.CONFIRMED) \
                and r.starts_at > at - timedelta(hours=2):
            late = at > r.starts_at + timedelta(minutes=m.grace)
            arrivals.append({**base, "start": r.starts_at, "late": late,
                             "when": _arrival_when(r, at, late)})
        elif r.status == Reservation.Status.SEATED and r.starts_at <= at < r.ends_at \
                and r.ends_at - at <= timedelta(minutes=45):
            freeing.append({**base, "end": r.ends_at, "until": m.hhmm(r.ends_at),
                            "in": _human(_minutes(r.ends_at - at))})
    arrivals.sort(key=lambda a: a["start"])
    freeing.sort(key=lambda f: f["end"])
    return arrivals, freeing


def _style_tables(tables):
    """Estilos precalculados: con cientos de mesas, los filtros de plantilla se notan."""
    for t in tables:
        t.style = (f"left:{t.dx:.2f}%;top:{t.dy:.2f}%;"
                   + (f"--w:{t.width}px;" if getattr(t, "width", None) else "")
                   + f"--p:{t.progress}%")
        # Como texto con punto: {{ t.x }} se localizaria como "14,5".
        t.rx, t.ry = f"{t.x:.2f}", f"{t.y:.2f}"


def _timeline_bars(tables, pct):
    for table in tables:
        bars = []
        for s in table.spans:
            left, right = max(0.0, pct(s["start"])), min(100.0, pct(s["end"]))
            if right <= 0 or left >= 100 or right <= left:
                continue
            bars.append({**s, "style": f"left:{left:.3f}%;width:{right - left:.3f}%"})
        table.bars = bars


def _hour_ticks(win_start, win_end, tz, pct):
    hours = []
    tick = win_start
    while tick <= win_end:
        hours.append({"label": f"{timezone.localtime(tick, tz):%H}", "pct": pct(tick)})
        tick += timedelta(hours=1)
    return hours


def _slot_load(tables, start, end):
    mesas = 0
    personas = 0
    for table in tables:
        dentro = [s for s in table.spans
                  if s["tone"] != "blocked" and s["start"] < end and s["end"] > start]
        if dentro:
            mesas += 1
            personas += max(s["party"] for s in dentro)
    return mesas, personas


def _occupancy_slots(tables, venue, win_start, win_end, m):
    slots = []
    cursor = win_start
    total_tables = len(tables) or 1
    while cursor < win_end:
        fin = cursor + timedelta(minutes=SLOT_MIN)
        mesas, personas = _slot_load(tables, cursor, fin)
        local = timezone.localtime(cursor, m.tz)
        slots.append({
            "label": f"{local:%H:%M}",
            "tables": mesas,
            "people": personas,
            "pct": round(mesas / total_tables * 100),
            "bar_px": 0,
            "url": "?" + urlencode({"local": venue.pk, "fecha": f"{local:%Y-%m-%d}",
                                    "hora": f"{local:%H:%M}"}),
            "is_at": cursor <= m.at < fin,
        })
        cursor = fin
    return slots


def _slot_peak(slots):
    """Franja mas llena; las barras se escalan a ella para no quedar planas."""
    peak = max(slots, key=lambda s: s["tables"]) if slots else None
    tope = (peak["tables"] if peak else 0) or 1
    for s in slots:
        s["bar_px"] = max(3, round(s["tables"] / tope * HEAT_PX))
    return peak


def _timeline_areas(tables):
    tl_areas = []
    for table in tables:
        if not tl_areas or tl_areas[-1]["name"] != table.area.name:
            tl_areas.append({"name": table.area.name,
                             "icon": _area_icon(table.area.name), "tables": []})
        tl_areas[-1]["tables"].append(table)
    return tl_areas


def _time_options(local_at, win_start, win_end, tz):
    """Hora como lista en 24 h: los campos nativos siguen el idioma del navegador."""
    elegida = local_at.replace(minute=local_at.minute - local_at.minute % 15,
                               second=0, microsecond=0)
    time_options = []
    cursor = timezone.localtime(win_start, tz)
    fin_local = timezone.localtime(win_end, tz)
    while cursor <= fin_local:
        time_options.append(f"{cursor:%H:%M}")
        cursor += timedelta(minutes=15)
    hora_elegida = f"{elegida:%H:%M}"
    if hora_elegida not in time_options:
        time_options.append(hora_elegida)
        time_options.sort()
    return time_options, hora_elegida


def _shifted_url(venue, local_at, minutes):
    t = local_at + timedelta(minutes=minutes)
    return "?" + urlencode({"local": venue.pk, "fecha": f"{t:%Y-%m-%d}",
                            "hora": f"{t:%H:%M}"})


def floor_view(request):
    venues, venue = _pick_venue(request)

    context = {
        **admin.site.each_context(request),
        "title": "Plano de sala",
        "venue": venue,
        "venues": venues,
    }
    if venue is None:
        return render(request, "admin/floor.html", context)

    tz = services.venue_tz(venue)
    real_now = timezone.now()
    at, is_now = _parse_moment(request, tz)
    local_at = timezone.localtime(at, tz)
    service_date = services.service_date_for(venue, at)
    m = _Moment(at=at, real_now=real_now, tz=tz,
                grace=services.policy_for(venue).no_show_grace_min,
                service_date=service_date, here=request.get_full_path())

    tables = list(
        Table.objects.filter(area__venue=venue, is_active=True)
        .select_related("area")
        .order_by("area__sort_order", "code")
    )

    occupancies = _occupancies(venue, service_date, at)
    counts, people_in, arriving, payload, all_spans = _fill_tables(
        tables,
        _group_by_table(occupancies),
        _group_by_table(_finished_occupancies(venue, service_date)),
        m,
    )
    arrivals, freeing = _arrivals_and_freeing(
        _reservations_on_tables(occupancies, tables, service_date), m,
    )

    plan_height, areas = _layout(tables)
    _style_tables(tables)

    win_start, win_end = _service_window(venue, service_date, tz, all_spans)
    win_total = (win_end - win_start).total_seconds()
    pct = lambda dt: (dt - win_start).total_seconds() / win_total * 100

    _timeline_bars(tables, pct)
    hours = _hour_ticks(win_start, win_end, tz, pct)
    slots = _occupancy_slots(tables, venue, win_start, win_end, m)
    peak = _slot_peak(slots)
    time_options, hora_elegida = _time_options(local_at, win_start, win_end, tz)

    at_pct = pct(at)
    now_pct = pct(real_now)

    # Sin coordenadas cargadas todas caen en (0, 0) y el plano no sirve.
    unplaced = sum(1 for t in tables if t.x == 0 and t.y == 0)
    ws_local = timezone.localtime(win_start, tz)

    context.update({
        "tables": tables,
        "areas": areas,
        "tables_json": payload,
        "plan_height": plan_height,
        "legend": [
            {"key": key, "label": label, "tone": tone, "count": counts[key]}
            for key, (label, tone) in STATES.items()
        ],
        "stats": {
            "free": counts["free"] + counts["soon"],
            "total": len(tables),
            "people": people_in,
            "arriving": arriving,
            "late": counts["late"],
        },
        "local_at": local_at,
        "date_label": date_format(local_at, "D j \\d\\e M").capitalize(),
        "time_options": time_options,
        "time_selected": hora_elegida,
        "arrivals": arrivals[:6],
        "arrivals_more": max(0, len(arrivals) - 6),
        "freeing": freeing[:4],
        "tl_has_data": bool(all_spans),
        "is_now": is_now,
        "service_date": service_date,
        "prev_url": _shifted_url(venue, local_at, -30),
        "next_url": _shifted_url(venue, local_at, 30),
        "now_url": "?" + urlencode({"local": venue.pk}),
        "can_edit": request.user.has_perm("venues.change_table"),
        "unplaced": unplaced if unplaced > 1 else 0,
        "positions_url": reverse("floor_positions"),
        "walk_in_url": reverse("floor_walk_in"),
        "can_walk_in": request.user.has_perm("reservations.add_reservation"),
        "tl_areas": _timeline_areas(tables),
        "tl_hours": hours,
        "tl_slots": slots,
        "tl_peak": peak,
        "tl_legend": BAR_LEGEND,
        "tl_at": at_pct if 0 <= at_pct <= 100 else None,
        "tl_now": now_pct if (not is_now and 0 <= now_pct <= 100) else None,
        "tl_window": {
            "y": ws_local.year, "m": ws_local.month, "d": ws_local.day,
            "h": ws_local.hour, "mi": ws_local.minute,
            "total": round(win_total / 60),
            "local": str(venue.pk),
        },
        "tl_hour_width": 100 / max(1, (len(hours) - 1)),
        "tl_range": f"{ws_local:%H:%M} – {timezone.localtime(win_end, tz):%H:%M}",
    })
    return render(request, "admin/floor.html", context)


@require_POST
def floor_positions(request):
    """Guarda las posiciones que llegan del modo edicion: {id: [x, y]}."""
    if not request.user.has_perm("venues.change_table"):
        raise PermissionDenied
    try:
        data = json.loads(request.body)
        cambios = {
            str(pk): (Decimal(str(round(float(x), 2))), Decimal(str(round(float(y), 2))))
            for pk, (x, y) in data.items()
        }
    except (ValueError, TypeError, AttributeError):
        return JsonResponse({"ok": False, "error": "Datos no válidos."}, status=400)

    tables = Table.objects.filter(pk__in=cambios.keys())
    for table in tables:
        x, y = cambios[str(table.pk)]
        table.pos_x = min(max(x, Decimal(0)), Decimal(100))
        table.pos_y = min(max(y, Decimal(0)), Decimal(100))
    Table.objects.bulk_update(tables, ["pos_x", "pos_y"])
    return JsonResponse({"ok": True, "saved": len(tables)})


@require_POST
def floor_walk_in(request):
    if not request.user.has_perm("reservations.add_reservation"):
        raise PermissionDenied

    volver = request.POST.get("next") or reverse("floor")
    if not url_has_allowed_host_and_scheme(volver, allowed_hosts={request.get_host()},
                                           require_https=request.is_secure()):
        volver = reverse("floor")

    table = Table.objects.filter(pk=request.POST.get("table"), is_active=True) \
        .select_related("area__venue").first()
    try:
        party = int(request.POST.get("party") or 0)
    except ValueError:
        party = 0
    if table is None:
        messages.error(request, "Esa mesa ya no existe.")
        return redirect(volver)

    try:
        r = services.seat_walk_in(
            table.area.venue, table, party,
            name=(request.POST.get("name") or "").strip()[:120],
            phone=(request.POST.get("phone") or "").strip(),
            user=request.user,
        )
    except services.ReservationError as exc:
        messages.error(request, str(exc))
        return redirect(volver)
    except ValidationError as exc:  # telefono sin digitos
        messages.error(request, exc.messages[0])
        return redirect(volver)

    tz = services.venue_tz(table.area.venue)
    aviso = ""
    if r.cut_at:
        aviso = (f" Ojo: la mesa tiene otra reserva a las "
                 f"{timezone.localtime(r.cut_at, tz):%H:%M}.")
    messages.success(
        request,
        f"Mesa {table.code} ocupada: {party} "
        f"{'persona' if party == 1 else 'personas'} sin reserva, hasta las "
        f"{timezone.localtime(r.ends_at, tz):%H:%M}.{aviso}",
    )
    return redirect(f"{volver.split('#')[0]}#{table.pk}")

