"""
Plano de sala.

Dibuja las mesas en su posicion (Table.pos_x / pos_y, en % del local) con el
estado que tienen en un instante: ahora, o la hora que se elija para ver como
estara la sala mas tarde. Debajo va la ocupacion por horas del servicio: una
fila por mesa con sus reservas como barras, incluidas las ya terminadas.

Tambien guarda las posiciones cuando se reordenan arrastrando en el modo de
edicion. Las vistas se montan en wapp/urls.py envueltas en admin_view, asi que
heredan el login y la comprobacion de staff del panel.
"""

import json
import math
from datetime import datetime, time, timedelta
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

#: Ancho de cada franja de la tira de ocupacion por horas.
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

#: Tono de cada barra de la linea de tiempo segun el estado de la reserva.
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
    """
    Sillas alrededor de la mesa, como estilos CSS relativos a la propia mesa.

    Redonda: repartidas en circulo. Cuadrada: una por lado. Alargada: una en
    cada cabecera y el resto a lo largo. Las primeras `taken` salen ocupadas.
    """
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
    """
    Desde la apertura del primer turno hasta el cierre del ultimo. Si ese dia
    no hay turnos, lo que cubran las reservas, y si tampoco, 12:00 a 24:00.
    """
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
    # Siempre a horas en punto, para que la regla quede limpia.
    inicio = inicio.replace(minute=0, second=0, microsecond=0)
    if cierre.minute or cierre.second:
        cierre = cierre.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return inicio, cierre


#: Margen fijo, en px, entre el borde del plano y el centro de las mesas mas
#: exteriores. Arriba hay mas: ahi va la etiqueta de cada zona. Tiene que
#: coincidir con .zs-plan-inner en static/admin/zisa.css.
PLAN_INSET = {"top": 104, "right": 112, "bottom": 76, "left": 112}

#: Ancho de referencia del plano para calcular su alto y detectar zonas que
#: se pisan. El ancho real se estira con la tarjeta; el alto es fijo.
PLAN_NOMINAL_W = 1000


def _half(table):
    """Medio ancho y medio alto del tablero en px (ver .zs-ft en zisa.css)."""
    if table.shape == "round":
        return 30, 30
    if table.shape == "long":
        return table.width / 2, 37
    return 37, 37


def _layout(tables):
    """
    Encuadra el plano y dibuja las zonas.

    Las posiciones guardadas son % de un lienzo 16:9 que casi nunca se usa
    entero: si las mesas ocupan la franja del 30 al 70 %, dibujar el lienzo
    completo deja media tarjeta vacia. Aqui se estira el rectangulo que
    ocupan las mesas a un area interior (.zs-plan-inner) con margenes fijos
    en px, y se elige el alto del plano para que las distancias entre mesas
    se parezcan a las del lienzo original. El ancho lo pone la tarjeta.

    Los margenes van en px y no en %: las mesas y las sillas miden lo mismo
    en cualquier pantalla, asi que su hueco tambien.

    Deja en cada mesa dx / dy (% del area interior) y devuelve la proporcion
    del plano en px y las zonas con su posicion ya como CSS.
    """
    if not tables:
        return 480, []

    xs = [t.x for t in tables]
    ys = [t.y for t in tables]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    sx, sy = x1 - x0, y1 - y0

    for t in tables:
        t.dx = (t.x - x0) / sx * 100 if sx >= 1 else 50.0
        t.dy = (t.y - y0) / sy * 100 if sy >= 1 else 50.0

    # Alto: el area interior conserva la forma que tenian las mesas en el
    # lienzo 16:9 (con un minimo para que dos filas de mesas no se toquen y
    # un maximo para que el plano quepa en pantalla).
    ins = PLAN_INSET
    inner_w = PLAN_NOMINAL_W - ins["left"] - ins["right"]
    if sx >= 1 and sy >= 1:
        inner_h = inner_w * (sy * 9) / (sx * 16)
    else:
        inner_h = 0
    inner_h = round(min(440, max(210, inner_h)))
    height = inner_h + ins["top"] + ins["bottom"]

    # Zonas: rectangulo que envuelve los centros de sus mesas, mas un margen
    # en px que sale del tamano real de la mesa de cada borde y sus sillas.
    SILLA = 26        # silla + aire
    ETIQUETA = 34     # hueco para la etiqueta de la zona
    boxes = {}
    for t in tables:
        hw, hh = _half(t)
        b = boxes.setdefault(t.area_id, {
            "name": t.area.name, "icon": _area_icon(t.area.name),
            "idx": len(boxes) % 4,
            "x0": 101.0, "x1": -1.0, "y0": 101.0, "y1": -1.0,
            "pl": 0, "pr": 0, "hh": 0,
        })
        if t.dx < b["x0"]:
            b["x0"], b["pl"] = t.dx, hw + SILLA
        elif t.dx == b["x0"]:
            b["pl"] = max(b["pl"], hw + SILLA)
        if t.dx > b["x1"]:
            b["x1"], b["pr"] = t.dx, hw + SILLA
        elif t.dx == b["x1"]:
            b["pr"] = max(b["pr"], hw + SILLA)
        b["y0"], b["y1"] = min(b["y0"], t.dy), max(b["y1"], t.dy)
        b["hh"] = max(b["hh"], hh)

    boxes = list(boxes.values())
    # Cada borde es [% del area interior, px]: left = calc(x% - px).
    for b in boxes:
        b["l"] = [b["x0"], -b["pl"]]
        b["r"] = [b["x1"], b["pr"]]
        b["t"] = [b["y0"], -(b["hh"] + SILLA + ETIQUETA)]
        b["b"] = [b["y1"], b["hh"] + SILLA]

    # Para comparar bordes hace falta una sola unidad: px del plano nominal.
    def ax(e):
        return e[0] / 100 * inner_w + e[1]

    def ay(e):
        return e[0] / 100 * inner_h + e[1]

    def cruza(a0, a1, b0, b1):
        return a0 < b1 and b0 < a1

    # Zonas una al lado de otra (comparten franja vertical): misma altura,
    # para que se lean como salas contiguas y no como cajas sueltas.
    for a in boxes:
        for b in boxes:
            if a is b or not cruza(ay(a["t"]), ay(a["b"]), ay(b["t"]), ay(b["b"])):
                continue
            if cruza(a["x0"], a["x1"], b["x0"], b["x1"]):
                continue
            arriba = min(a["t"], b["t"], key=ay)
            abajo = max(a["b"], b["b"], key=ay)
            a["t"] = b["t"] = list(arriba)
            a["b"] = b["b"] = list(abajo)

    # Zonas vecinas (una junto a otra, sin nada en medio) comparten borde:
    # el hueco entre las mesas de una y otra se reparte por la mitad. Asi se
    # leen como salas contiguas y nunca se pisan, sea cual sea el ancho real
    # de la pantalla. El borde exterior conserva su margen propio.
    GAP = 10

    def junta(a1, b0, pa, pb):
        # Punto medio entre el borde de la mesa de un lado (a1% + pa px) y
        # el de la del otro (b0% - pb px), como [%, px].
        return [(a1 + b0) / 2, (pa - pb) / 2]

    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            misma_franja = cruza(a["y0"] - 1, a["y1"] + 1, b["y0"] - 1, b["y1"] + 1)
            misma_columna = cruza(a["x0"] - 1, a["x1"] + 1, b["x0"] - 1, b["x1"] + 1)
            if misma_franja and not misma_columna:
                izq, der = (a, b) if a["x1"] < b["x0"] else (b, a)
                en_medio = any(c is not a and c is not b and izq["x1"] < c["x0"] and c["x1"] < der["x0"]
                               and cruza(c["y0"], c["y1"], izq["y0"], izq["y1"]) for c in boxes)
                if not en_medio:
                    m = junta(izq["x1"], der["x0"], izq["pr"], der["pl"])
                    izq["r"], der["l"] = [m[0], m[1] - GAP / 2], [m[0], m[1] + GAP / 2]
            elif misma_columna and not misma_franja:
                arr, aba = (a, b) if a["y1"] < b["y0"] else (b, a)
                m = [(arr["y1"] + aba["y0"]) / 2, 0]
                arr["b"], aba["t"] = [m[0], -GAP / 2], [m[0], GAP / 2]

    def css(e):
        signo = "+" if e[1] >= 0 else "-"
        return f"calc({e[0]:.3f}% {signo} {abs(e[1]):.0f}px)"

    def tramo(a, z):
        return f"calc({z[0] - a[0]:.3f}% + {z[1] - a[1]:.0f}px)"

    for b in boxes:
        b["style"] = (f"left:{css(b['l'])};top:{css(b['t'])};"
                      f"width:{tramo(b['l'], b['r'])};height:{tramo(b['t'], b['b'])}")
    return height, boxes


def floor_view(request):
    venues = list(Venue.objects.filter(is_active=True).order_by("name"))
    venue = next((v for v in venues if str(v.pk) == request.GET.get("local")), None)
    venue = venue or (venues[0] if venues else None)

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
    grace = services.policy_for(venue).no_show_grace_min

    tables = list(
        Table.objects.filter(area__venue=venue, is_active=True)
        .select_related("area")
        .order_by("area__sort_order", "code")
    )

    # Lo que ocupa mesa ese dia de servicio, mas los bloqueos que tocan las
    # 12 horas alrededor del instante pedido.
    occupancies = (
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
    by_table = {}
    for occ in occupancies:
        by_table.setdefault(occ.table_id, []).append(occ)

    # Las ya terminadas liberan su ocupacion (is_active=False), pero para la
    # vista por horas cuentan: se dibujan con su hora real de entrada y salida.
    finished = (
        TableOccupancy.objects.filter(
            table__area__venue=venue, is_active=False,
            reservation__status=Reservation.Status.FINISHED,
            reservation__service_date=service_date,
        )
        .select_related("reservation__guest")
    )
    done_by_table = {}
    for occ in finished:
        done_by_table.setdefault(occ.table_id, []).append(occ)

    here = request.get_full_path()
    change_url = lambda r: reservation_url("change", r.pk)
    hhmm = lambda dt: f"{timezone.localtime(dt, tz):%H:%M}"

    counts = {key: 0 for key in STATES}
    people_in = 0
    arriving = 0
    payload = {}
    all_spans = []

    for table in tables:
        rows = by_table.get(table.id, [])
        current = next((o for o in rows if o.starts_at <= at < o.ends_at), None)
        upcoming = next((o for o in rows if o.starts_at > at), None)

        now_info = None
        progress = 0
        if current is None:
            if (upcoming and not upcoming.is_block
                    and upcoming.starts_at - at <= timedelta(minutes=SOON_MIN)):
                state = "soon"
            else:
                state = "free"
        elif current.is_block:
            state = "blocked"
            now_info = {
                "kind": "block",
                "reason": current.block_reason,
                "range": f"{hhmm(current.starts_at)} - {hhmm(current.ends_at)}",
                "remaining": _human(_minutes(current.ends_at - at)),
            }
        else:
            r = current.reservation
            if r.status == Reservation.Status.SEATED:
                state = "seated"
                people_in += r.party_size
                total = (r.ends_at - r.starts_at).total_seconds() or 1
                progress = min(100, max(0, round((at - r.starts_at).total_seconds() / total * 100)))
            elif at > r.starts_at + timedelta(minutes=grace):
                state = "late"
            else:
                state = "waiting"
            now_info = {
                "kind": "reservation",
                "guest": r.guest.name,
                "phone": r.guest.phone,
                "party": r.party_size,
                "range": f"{hhmm(r.starts_at)} - {hhmm(r.ends_at)}",
                "until": hhmm(r.ends_at),
                "status": r.get_status_display(),
                "remaining": _human(_minutes(r.ends_at - at)),
                "late_by": _human(_minutes(at - r.starts_at)) if state == "late" else "",
                "occasion": r.occasion,
                "notes": r.guest_notes,
                "risky": r.guest.no_shows if r.guest.is_risky else 0,
                "progress": progress,
                "url": change_url(r),
                "step": str(next_step_button(r, here)),
            }
        counts[state] += 1

        next_info = None
        if upcoming and not upcoming.is_block:
            r = upcoming.reservation
            if r.starts_at - at <= timedelta(hours=1):
                arriving += 1
            next_info = {
                "time": hhmm(r.starts_at),
                "guest": r.guest.name,
                "party": r.party_size,
                "in": _human(_minutes(r.starts_at - at)),
                "url": change_url(r),
                "step": str(next_step_button(r, here)),
            }

        # --- Tramos del dia para la linea de tiempo ---------------------
        spans = []
        for o in rows:
            if o.is_block:
                spans.append({"start": o.starts_at, "end": o.ends_at, "tone": "blocked",
                              "label": o.block_reason, "status": "Bloqueo", "url": "",
                              "party": 0, "is_current": o is current})
                continue
            r = o.reservation
            if r.service_date != service_date:
                continue
            tone = BAR_TONES.get(r.status, "booked")
            # Atrasada respecto a la hora real, no a la del plano: una barra
            # roja tiene que significar "esto esta pasando ahora".
            if (r.status in (Reservation.Status.PENDING, Reservation.Status.CONFIRMED)
                    and real_now > r.starts_at + timedelta(minutes=grace)):
                tone = "late"
            spans.append({"start": o.starts_at, "end": o.ends_at, "tone": tone,
                          "label": r.guest.name, "party": r.party_size,
                          "status": r.get_status_display(), "url": change_url(r),
                          "is_current": o is current})
        for o in done_by_table.get(table.id, []):
            r = o.reservation
            inicio = r.seated_at or o.starts_at
            fin = r.released_at or o.ends_at
            # Sentada y cerrada casi a la vez: se marco tarde en el panel, no
            # es lo que duro la mesa. Se dibuja el horario reservado.
            if fin - inicio < timedelta(minutes=10):
                inicio, fin = o.starts_at, o.ends_at
            spans.append({"start": inicio, "end": fin, "tone": "done",
                          "label": r.guest.name, "party": r.party_size,
                          "status": "Finalizada", "url": change_url(r),
                          "is_current": False})
        spans.sort(key=lambda s: s["start"])
        for s in spans:
            s["time"] = f"{hhmm(s['start'])}–{hhmm(s['end'])}"
        table.spans = spans
        all_spans.extend(spans)

        payload[str(table.pk)] = {
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

        # --- Dibujo de la mesa ------------------------------------------
        table.state = state
        table.x = float(table.pos_x)
        table.y = float(table.pos_y)
        table.shape = ("round" if table.max_seats <= 2
                       else "square" if table.max_seats <= 4 else "long")
        if table.shape == "long":
            table.width = 64 + 34 * math.ceil((table.max_seats - 2) / 2)
        table.progress = progress

        party = now_info["party"] if now_info and now_info["kind"] == "reservation" else 0
        table.chairs = _chairs(table, min(party, table.max_seats))
        table.chair_tone = state
        # Lo que se lee dentro de la mesa sin abrir nada.
        if state == "seated":
            table.hint = f"hasta {now_info['until']}"
        elif state == "late":
            table.hint = f"+{now_info['late_by']}"
        elif state == "waiting":
            table.hint = f"{party} pax"
        elif state == "soon":
            table.hint = f"a las {next_info['time']}"
        elif state == "blocked":
            table.hint = "bloqueada"
        else:
            table.hint = f"{table.min_seats}-{table.max_seats} pax"

    # --- Resumen del panel cuando no hay mesa elegida ------------------------
    # Lo que el anfitrion mira sin tocar nada: quien llega y que mesa se libera.
    codigo = {t.id: t.code for t in tables}
    por_reserva = {}
    for occ in occupancies:
        r = occ.reservation
        if r is None or r.service_date != service_date:
            continue
        item = por_reserva.setdefault(r.pk, {"r": r, "tables": [], "table_id": occ.table_id})
        item["tables"].append(codigo.get(occ.table_id, "?"))

    arrivals, freeing = [], []
    for item in por_reserva.values():
        r = item["r"]
        base = {
            "time": hhmm(r.starts_at),
            "guest": r.guest.name,
            "party": r.party_size,
            "tables": ", ".join(sorted(item["tables"])),
            "table_id": str(item["table_id"]),
        }
        if r.status in (Reservation.Status.PENDING, Reservation.Status.CONFIRMED) \
                and r.starts_at > at - timedelta(hours=2):
            late = at > r.starts_at + timedelta(minutes=grace)
            arrivals.append({**base, "start": r.starts_at, "late": late,
                             "when": (f"+{_human(_minutes(at - r.starts_at))}" if late
                                      else f"en {_human(_minutes(r.starts_at - at))}"
                                      if r.starts_at > at else "ahora")})
        elif r.status == Reservation.Status.SEATED and r.starts_at <= at < r.ends_at \
                and r.ends_at - at <= timedelta(minutes=45):
            freeing.append({**base, "end": r.ends_at, "until": hhmm(r.ends_at),
                            "in": _human(_minutes(r.ends_at - at))})
    arrivals.sort(key=lambda a: a["start"])
    freeing.sort(key=lambda f: f["end"])

    # --- Encuadre y zonas ----------------------------------------------------
    plan_height, areas = _layout(tables)
    # Estilo de cada mesa ya armado (necesita dx/dy de _layout): con cientos
    # de mesas, cada filtro dentro del bucle de la plantilla se nota.
    for t in tables:
        t.style = (f"left:{t.dx:.2f}%;top:{t.dy:.2f}%;"
                   + (f"--w:{t.width}px;" if getattr(t, "width", None) else "")
                   + f"--p:{t.progress}%")
        # Como texto con punto: {{ t.x }} se localizaria como "14,5".
        t.rx, t.ry = f"{t.x:.2f}", f"{t.y:.2f}"

    # --- Linea de tiempo -----------------------------------------------------
    win_start, win_end = _service_window(venue, service_date, tz, all_spans)
    win_total = (win_end - win_start).total_seconds()
    pct = lambda dt: (dt - win_start).total_seconds() / win_total * 100

    for table in tables:
        bars = []
        for s in table.spans:
            left, right = max(0.0, pct(s["start"])), min(100.0, pct(s["end"]))
            if right <= 0 or left >= 100 or right <= left:
                continue
            bars.append({**s, "style": f"left:{left:.3f}%;width:{right - left:.3f}%"})
        table.bars = bars

    hours = []
    tick = win_start
    while tick <= win_end:
        hours.append({"label": f"{timezone.localtime(tick, tz):%H}", "pct": pct(tick)})
        tick += timedelta(hours=1)

    # Ocupacion franja a franja: mesas con alguien (o reservadas) y personas.
    slots = []
    cursor = win_start
    total_tables = len(tables) or 1
    while cursor < win_end:
        fin = cursor + timedelta(minutes=SLOT_MIN)
        mesas = 0
        personas = 0
        for table in tables:
            dentro = [s for s in table.spans
                      if s["tone"] != "blocked" and s["start"] < fin and s["end"] > cursor]
            if dentro:
                mesas += 1
                personas += max(s["party"] for s in dentro)
        local = timezone.localtime(cursor, tz)
        slots.append({
            "label": f"{local:%H:%M}",
            "tables": mesas,
            "people": personas,
            "pct": round(mesas / total_tables * 100),
            "bar_px": 0,
            "url": "?" + urlencode({"local": venue.pk, "fecha": f"{local:%Y-%m-%d}",
                                    "hora": f"{local:%H:%M}"}),
            "is_at": cursor <= at < fin,
        })
        cursor = fin
    peak = max(slots, key=lambda s: s["tables"]) if slots else None
    # Las barras se escalan al pico del dia: con la sala a medias seguirian
    # siendo casi planas. El numero exacto va encima de cada una.
    tope = (peak["tables"] if peak else 0) or 1
    for s in slots:
        s["bar_px"] = max(3, round(s["tables"] / tope * HEAT_PX))

    # Tramos por zona para agrupar las filas.
    tl_areas = []
    for table in tables:
        if not tl_areas or tl_areas[-1]["name"] != table.area.name:
            tl_areas.append({"name": table.area.name,
                             "icon": _area_icon(table.area.name), "tables": []})
        tl_areas[-1]["tables"].append(table)

    # Desplegable de hora: cada 15 min del servicio, en 24 h. Los campos
    # nativos de fecha/hora siguen el idioma del navegador (09/23, 08:13 PM),
    # asi que la hora va como lista y la fecha como texto en espanol.
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

    at_pct = pct(at)
    now_pct = pct(real_now)

    # Enlaces para moverse en el tiempo sin tocar el formulario.
    def shifted(minutes):
        t = local_at + timedelta(minutes=minutes)
        return "?" + urlencode({"local": venue.pk, "fecha": f"{t:%Y-%m-%d}",
                                "hora": f"{t:%H:%M}"})

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
        "prev_url": shifted(-30),
        "next_url": shifted(30),
        "now_url": "?" + urlencode({"local": venue.pk}),
        "can_edit": request.user.has_perm("venues.change_table"),
        "unplaced": unplaced if unplaced > 1 else 0,
        "positions_url": reverse("floor_positions"),
        "walk_in_url": reverse("floor_walk_in"),
        "can_walk_in": request.user.has_perm("reservations.add_reservation"),
        # Linea de tiempo
        "tl_areas": tl_areas,
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
    """Sienta a quien llega sin reserva en la mesa elegida del plano."""
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

