import json
import secrets
import uuid
from datetime import datetime, time, timedelta

from django.contrib.auth.models import Permission, User
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.reservations import services
from apps.reservations.models import Reservation, TableOccupancy
from apps.reservations.tests import LIMA, BaseSalaTestCase
from apps.venues.models import Area, Shift, Table, Venue
from wapp import floor
from wapp.dashboard import dashboard_callback

FLOOR = "floor"
FLOOR_WALK_IN = "floor_walk_in"
FLOOR_POSITIONS = "floor_positions"
JSON = "application/json"
TABLES_JSON = "tables_json"
TIME_SELECTED = "time_selected"
TL_RANGE = "tl_range"
MOTIVO_EVENTO = "Evento privado"
HORA_CENA = "20:15"
OCHO = "20:00"
CHANGE_TABLE = "change_table"
READY = "dashboard_ready"
HOURLY = "hourly_labels"
TOMORROW = "tomorrow"
MONTH = "month"
NOW_KPIS = "now_kpis"
SUMMARY = "summary"
TABLES = "tables"
VALUE = "value"
AREAS = "areas"
VENUE = "venue"
IS_NOW = "is_now"


def _staff(username, *codenames):
    """Usuario de equipo con clave aleatoria: los tests entran con force_login."""
    user = User.objects.create_user(username, password=secrets.token_urlsafe(),
                                    is_staff=True)
    if codenames:
        user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
    return user


def _mensajes(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class PlanoBase(BaseSalaTestCase):
    """La sala de BaseSalaTestCase con mas zonas y las mesas colocadas."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        terraza = Area.objects.create(venue=cls.venue, name="Terraza", sort_order=1)
        privado = Area.objects.create(venue=cls.venue, name="Privado", sort_order=2)
        barra = Area.objects.create(venue=cls.venue, name="Barra", sort_order=3)
        cls.tt1 = Table.objects.create(area=terraza, code="T1", min_seats=1, max_seats=2)
        cls.p1 = Table.objects.create(area=privado, code="P1", min_seats=2, max_seats=4)
        cls.p2 = Table.objects.create(area=privado, code="P2", min_seats=2, max_seats=4)
        cls.b1 = Table.objects.create(area=barra, code="B1", min_seats=1, max_seats=1)
        # S1 y S2 en la misma columna; Barra debajo del salon.
        posiciones = {cls.t1: (10, 10), cls.t2: (10, 30), cls.t3: (20, 20),
                      cls.tt1: (80, 20), cls.p1: (50, 15), cls.p2: (50, 25),
                      cls.b1: (10, 90)}
        for mesa, (x, y) in posiciones.items():
            mesa.pos_x, mesa.pos_y = x, y
            mesa.save(update_fields=["pos_x", "pos_y"])
        cls.staff = _staff("host", CHANGE_TABLE, "add_reservation")

    def setUp(self):
        self.client.force_login(self.staff)

    def plano(self, **params):
        return self.client.get(reverse(FLOOR), params)

    def dia(self):
        return self.cena().date()

    def plano_de_la_cena(self):
        return self.plano(fecha=self.dia().isoformat(), hora=HORA_CENA)


class PlanoEstadosTests(PlanoBase):
    """Una cena de manana a las 20:15 con cada estado posible de mesa."""

    def setUp(self):
        super().setUp()
        c = self.cena
        # T1: ya se fue una mesa a las 17:00 y hay otra sentada que se libera pronto.
        services.finish(services.seat_walk_in(self.venue, self.tt1, 2, now=c(hora=17)))
        services.seat_walk_in(self.venue, self.tt1, 2, now=c(hora=19))
        # S1: sentada con un bloqueo despues.
        services.seat_walk_in(self.venue, self.t1, 3, now=c(hora=20))
        services.block_table(self.t1, c(hora=22), c(hora=23), reason=MOTIVO_EVENTO)
        # S2: esperando (dentro de la cortesia). S3: sin llegar.
        self.reservar(self.t2, 2, c(hora=20))
        self.reservar(self.t3, 6, c(hora=19, minuto=30))
        # P1: llega pronto, y a media tarde hubo una mesa de 17:05 a 18:20.
        self.reservar(self.p1, 2, c(hora=20, minuto=45))
        hecha = services.finish(services.seat_walk_in(self.venue, self.p1, 2,
                                                      now=c(hora=17)))
        Reservation.objects.filter(pk=hecha.pk).update(
            seated_at=c(hora=17, minuto=5), released_at=c(hora=18, minuto=20))
        # P2: libre; solo tiene bloqueos (uno por la manana, fuera del servicio).
        services.block_table(self.p2, c(hora=10), c(hora=11), reason=MOTIVO_EVENTO)
        services.block_table(self.p2, c(hora=22), c(hora=23), reason=MOTIVO_EVENTO)
        # B1: bloqueada ahora.
        services.block_table(self.b1, c(hora=20), c(hora=22), reason="Mantenimiento")

    def reservar(self, mesa, personas, inicio):
        return services.create_reservation(
            venue=self.venue, guest=self.guest, starts_at=inicio, party_size=personas,
            source=Reservation.Source.STAFF, tables=[mesa],
        )

    def test_cada_mesa_tiene_su_estado(self):
        r = self.plano_de_la_cena()
        self.assertEqual(r.status_code, 200)
        fichas = r.context[TABLES_JSON]
        estados = {f["code"]: f["state"] for f in fichas.values()}
        self.assertEqual(estados, {
            "S1": "seated", "S2": "waiting", "S3": "late", "T1": "seated",
            "P1": "soon", "P2": "free", "B1": "blocked",
        })
        self.assertEqual(r.context["stats"], {
            "free": 2, "total": 7, "people": 5, "arriving": 1, "late": 1,
        })
        self.assertFalse(r.context[IS_NOW])
        self.assertEqual(r.context[TIME_SELECTED], HORA_CENA)
        self.assertEqual(r.context[TL_RANGE], "19:00 – 23:00")
        self.assertTrue(r.context["tl_has_data"])
        self.assertIsNotNone(r.context["tl_at"])

    def test_fichas_del_panel_lateral(self):
        r = self.plano_de_la_cena()
        fichas = {f["code"]: f for f in r.context[TABLES_JSON].values()}

        self.assertEqual(fichas["B1"]["now"]["kind"], "block")
        self.assertEqual(fichas["B1"]["now"]["range"], "20:00 - 22:00")
        self.assertEqual(fichas["S3"]["now"]["late_by"], "45 min")
        self.assertEqual(fichas["S1"]["now"]["remaining"], "1 h 15")
        self.assertIsNone(fichas["S1"]["next"])  # lo siguiente es un bloqueo
        self.assertEqual(fichas["P1"]["next"]["time"], "20:45")
        self.assertEqual(fichas["P1"]["next"]["in"], "30 min")

        # P1 muestra su hora real; T1 (cerrada al instante), la reservada.
        dia_p1 = [d for d in fichas["P1"]["day"] if d["tone"] == "done"]
        self.assertEqual([d["time"] for d in dia_p1], ["17:05–18:20"])
        dia_t1 = [d["time"] for d in fichas["T1"]["day"] if d["tone"] == "done"]
        self.assertEqual(dia_t1, ["17:00–18:30"])

    def test_dibujo_de_las_mesas(self):
        r = self.plano_de_la_cena()
        mesas = {t.code: t for t in r.context[TABLES]}
        self.assertEqual(mesas["S3"].shape, "long")
        self.assertEqual(mesas["S1"].shape, "square")
        self.assertEqual(mesas["T1"].shape, "round")
        self.assertEqual(mesas["S3"].hint, "+45 min")
        self.assertEqual(mesas["S2"].hint, "2 pax")
        self.assertEqual(mesas["P1"].hint, "a las 20:45")
        self.assertEqual(mesas["B1"].hint, "bloqueada")
        self.assertEqual(mesas["P2"].hint, "2-4 pax")
        self.assertTrue(mesas["S1"].hint.startswith("hasta "))
        self.assertIn("--w:", mesas["S3"].style)
        self.assertEqual(sum(c["taken"] for c in mesas["S1"].chairs), 3)
        self.assertEqual(len(mesas["S3"].chairs), 8)
        # El bloqueo de la manana queda fuera de la ventana del servicio.
        self.assertEqual([b["tone"] for b in mesas["P2"].bars], ["blocked"])

    def test_zonas_con_su_icono(self):
        r = self.plano_de_la_cena()
        iconos = {a["name"]: a["icon"] for a in r.context[AREAS]}
        self.assertEqual(iconos, {"Salon": "restaurant", "Terraza": "deck",
                                  "Privado": "meeting_room", "Barra": "local_bar"})
        self.assertTrue(all("left:calc(" in a["style"] for a in r.context[AREAS]))
        self.assertEqual(r.context["unplaced"], 0)

    def test_quien_llega_y_que_mesa_se_libera(self):
        r = self.plano_de_la_cena()
        llegadas = r.context["arrivals"]
        self.assertEqual([a["when"] for a in llegadas], ["+45 min", "ahora", "en 30 min"])
        self.assertEqual([a[TABLES] for a in llegadas], ["S3", "S2", "P1"])
        self.assertTrue(llegadas[0]["late"])
        libera = r.context["freeing"]
        self.assertEqual([(f[TABLES], f["in"]) for f in libera], [("T1", "15 min")])

    def test_ocupacion_por_franjas(self):
        r = self.plano_de_la_cena()
        franjas = {s["label"]: s for s in r.context["tl_slots"]}
        self.assertEqual(len(franjas), 8)
        self.assertTrue(franjas[OCHO]["is_at"])
        self.assertEqual(franjas[OCHO][TABLES], 4)  # S1, S2, S3 y T1
        self.assertEqual(r.context["tl_peak"][TABLES], 4)
        self.assertEqual(franjas[OCHO]["bar_px"], floor.HEAT_PX)
        self.assertEqual([h["label"] for h in r.context["tl_hours"]],
                         ["19", "20", "21", "22", "23"])

    def test_hora_fuera_del_servicio_se_anade_al_desplegable(self):
        r = self.plano(fecha=self.dia().isoformat(), hora="17:07")
        self.assertEqual(r.context[TIME_SELECTED], "17:00")
        self.assertIn("17:00", r.context["time_options"])
        self.assertIsNone(r.context["tl_at"])
        self.assertIn("hora=16%3A37", r.context["prev_url"])
        self.assertIn("hora=17%3A37", r.context["next_url"])

    def test_mesas_sin_colocar(self):
        Table.objects.update(pos_x=0, pos_y=0)
        r = self.plano_de_la_cena()
        self.assertEqual(r.context["unplaced"], 7)
        alto = 210 + floor.PLAN_INSET["top"] + floor.PLAN_INSET["bottom"]
        self.assertEqual(r.context["plan_height"], alto)


class PlanoAhoraTests(PlanoBase):
    def test_fecha_mal_escrita_muestra_ahora(self):
        r = self.plano(fecha="ayer", hora=HORA_CENA)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context[IS_NOW])

    def test_la_barra_atrasada_se_mide_con_la_hora_real(self):
        reserva = services.create_reservation(
            venue=self.venue, guest=self.guest, starts_at=self.cena(), party_size=2,
            source=Reservation.Source.STAFF, tables=[self.t1],
        )
        inicio = timezone.now() - timedelta(minutes=30)
        fin = inicio + timedelta(minutes=90)
        Reservation.objects.filter(pk=reserva.pk).update(
            starts_at=inicio, ends_at=fin,
            service_date=services.service_date_for(self.venue, inicio))
        TableOccupancy.objects.filter(reservation=reserva).update(
            starts_at=inicio, ends_at=fin)

        r = self.plano()
        ficha = r.context[TABLES_JSON][str(self.t1.pk)]
        self.assertEqual(ficha["state"], "late")
        self.assertEqual([d["tone"] for d in ficha["day"]], ["late"])
        self.assertTrue(r.context[IS_NOW])


class PlanoLocalesTests(BaseSalaTestCase):
    """Eleccion de local y ventana del servicio cuando faltan turnos o mesas."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.staff = _staff("host")

    def setUp(self):
        self.client.force_login(self.staff)

    def pedir(self, venue, hora=HORA_CENA):
        return self.client.get(reverse(FLOOR), {
            "local": venue.pk, "fecha": self.cena().date().isoformat(), "hora": hora})

    def test_sin_turnos_la_ventana_la_marcan_las_ocupaciones(self):
        anexo = Venue.objects.create(name="Zisa Anexo", timezone="America/Lima")
        patio = Area.objects.create(venue=anexo, name="Patio")
        mesa = Table.objects.create(area=patio, code="A1", min_seats=1, max_seats=2)
        services.block_table(mesa, self.cena(hora=20), self.cena(hora=21, minuto=30),
                             reason=MOTIVO_EVENTO)
        r = self.pedir(anexo)
        self.assertEqual(r.context[VENUE], anexo)
        self.assertEqual(r.context[TL_RANGE], "20:00 – 22:00")
        self.assertEqual(r.context[AREAS][0]["icon"], "deck")
        self.assertEqual(r.context[TABLES_JSON][str(mesa.pk)]["state"], "blocked")

    def test_local_sin_mesas_ni_turnos(self):
        vacio = Venue.objects.create(name="Zisa Vacio", timezone="America/Lima")
        r = self.pedir(vacio)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context[TL_RANGE], "12:00 – 00:00")
        self.assertEqual(r.context["plan_height"], 480)
        self.assertEqual(r.context[AREAS], [])
        self.assertFalse(r.context["tl_has_data"])

    def test_sin_locales_activos(self):
        Venue.objects.update(is_active=False)
        r = self.client.get(reverse(FLOOR))
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context[VENUE])
        self.assertNotIn(TABLES, r.context)

    def test_turno_que_pasa_la_medianoche(self):
        Shift.objects.filter(venue=self.venue).update(end_time=time(1, 30))
        dia = self.cena().date()
        inicio, cierre = floor._service_window(self.venue, dia, LIMA, [])
        self.assertEqual(inicio, datetime.combine(dia, time(19), tzinfo=LIMA))
        self.assertEqual(cierre, datetime.combine(dia + timedelta(days=1), time(2),
                                                  tzinfo=LIMA))


class PlanoPosicionesTests(BaseSalaTestCase):
    def enviar(self, user, cuerpo):
        self.client.force_login(user)
        return self.client.post(reverse(FLOOR_POSITIONS), cuerpo, content_type=JSON)

    def test_guarda_y_recorta_al_lienzo(self):
        editor = _staff("editor", CHANGE_TABLE)
        r = self.enviar(editor, json.dumps({str(self.t1.pk): [150, -5],
                                            str(self.t2.pk): [12.345, 40]}))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "saved": 2})
        self.t1.refresh_from_db()
        self.t2.refresh_from_db()
        self.assertEqual((float(self.t1.pos_x), float(self.t1.pos_y)), (100.0, 0.0))
        self.assertEqual((float(self.t2.pos_x), float(self.t2.pos_y)), (12.35, 40.0))

    def test_datos_no_validos(self):
        editor = _staff("editor", CHANGE_TABLE)
        for cuerpo in ("esto no es json", json.dumps({str(self.t1.pk): 5})):
            with self.subTest(cuerpo=cuerpo):
                r = self.enviar(editor, cuerpo)
                self.assertEqual(r.status_code, 400)
                self.assertFalse(r.json()["ok"])

    def test_sin_permiso_no_guarda(self):
        r = self.enviar(_staff("mozo"), json.dumps({str(self.t1.pk): [5, 5]}))
        self.assertEqual(r.status_code, 403)


class PlanoSinReservaTests(BaseSalaTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.host = _staff("host", "add_reservation")

    def setUp(self):
        self.client.force_login(self.host)

    def sentar(self, **campos):
        datos = {"table": self.t1.pk, "party": "2"}
        datos.update(campos)
        return self.client.post(reverse(FLOOR_WALK_IN), datos)

    def test_sienta_y_avisa_de_la_siguiente_ocupacion(self):
        ahora = timezone.now()
        services.block_table(self.t1, ahora + timedelta(minutes=60),
                             ahora + timedelta(minutes=120), reason=MOTIVO_EVENTO)
        volver = f"{reverse(FLOOR)}?local={self.venue.pk}#antes"
        r = self.sentar(party="1", name="Pedro Soto", next=volver)

        self.assertRedirects(r, f"{volver.split('#')[0]}#{self.t1.pk}",
                             fetch_redirect_response=False)
        reserva = Reservation.objects.get(source=Reservation.Source.WALK_IN)
        self.assertEqual(reserva.status, Reservation.Status.SEATED)
        self.assertEqual(reserva.internal_notes, "Pedro Soto")
        [aviso] = _mensajes(r)
        self.assertIn("Mesa S1 ocupada: 1 persona sin reserva", aviso)
        self.assertIn("Ojo", aviso)

    def test_varias_personas_con_telefono(self):
        r = self.sentar(party="3", name="Luis", phone="987 555 444")
        self.assertRedirects(r, f"{reverse(FLOOR)}#{self.t1.pk}",
                             fetch_redirect_response=False)
        [aviso] = _mensajes(r)
        self.assertIn("3 personas sin reserva", aviso)
        self.assertNotIn("Ojo", aviso)

    def test_vuelta_a_otro_sitio_se_ignora(self):
        r = self.sentar(next="https://otro-sitio.example/")
        self.assertRedirects(r, f"{reverse(FLOOR)}#{self.t1.pk}",
                             fetch_redirect_response=False)

    def test_errores_vuelven_al_plano_con_el_motivo(self):
        casos = {
            "sin comensales": ({"party": "muchas"}, "Indica cuántas personas son."),
            "telefono": ({"phone": "sin numero"}, "El teléfono no contiene dígitos."),
            "mesa": ({"table": str(uuid.uuid4())}, "Esa mesa ya no existe."),
        }
        for nombre, (campos, esperado) in casos.items():
            with self.subTest(nombre):
                r = self.sentar(**campos)
                self.assertRedirects(r, reverse(FLOOR), fetch_redirect_response=False)
                self.assertEqual(_mensajes(r)[-1], esperado)
        self.assertFalse(Reservation.objects.exists())

    def test_sin_permiso_no_sienta(self):
        self.client.force_login(_staff("mozo"))
        self.assertEqual(self.sentar().status_code, 403)
        self.assertFalse(Reservation.objects.exists())


class PanelInicioTests(BaseSalaTestCase):
    def setUp(self):
        ahora = timezone.now()
        self.hoy = services.service_date_for(self.venue, ahora)
        services.seat_walk_in(self.venue, self.t1, 3, now=ahora - timedelta(minutes=20))
        self.atrasada = self.reserva_de_hoy(self.t2, 2, ahora - timedelta(minutes=25))
        planton = self.reserva_de_hoy(self.t3, 6, ahora - timedelta(hours=3))
        services.mark_no_show(planton)
        # Hace una semana vino gente: da con que comparar.
        services.finish(services.seat_walk_in(self.venue, self.t2, 2,
                                              now=ahora - timedelta(days=7)))

    def reserva_de_hoy(self, mesa, personas, inicio):
        reserva = services.create_reservation(
            venue=self.venue, guest=self.guest, starts_at=self.cena(), party_size=personas,
            source=Reservation.Source.STAFF, tables=[mesa],
        )
        fin = inicio + timedelta(minutes=90)
        Reservation.objects.filter(pk=reserva.pk).update(
            starts_at=inicio, ends_at=fin, service_date=self.hoy)
        TableOccupancy.objects.filter(reservation=reserva).update(
            starts_at=inicio, ends_at=fin)
        reserva.refresh_from_db()
        return reserva

    def test_cifras_del_momento_y_del_dia(self):
        ctx = dashboard_callback(None, {})
        self.assertTrue(ctx[READY])
        self.assertEqual(ctx[VENUE], self.venue)
        self.assertEqual(ctx["service_date"], self.hoy)

        en_sala, libres, sin_llegar = ctx[NOW_KPIS]
        self.assertEqual(en_sala[VALUE], 3)
        self.assertEqual(en_sala["hint"], "1 mesa sentada")
        self.assertNotIn("S1", libres["hint"])
        self.assertNotIn("S2", libres["hint"])
        self.assertEqual(sin_llegar[VALUE], 1)
        self.assertEqual(sin_llegar["tone"], "bad")

        cubiertos, ocupacion, no_shows = ctx["day_kpis"]
        self.assertEqual(cubiertos[VALUE], 5)
        self.assertEqual(cubiertos["hint"], "de 16 plazas")
        self.assertEqual(ocupacion[VALUE], f"{round(5 / 16 * 100)}%")
        self.assertEqual(no_shows[VALUE], 1)

    def test_lista_de_proximas_y_atrasadas(self):
        ctx = dashboard_callback(None, {})
        self.assertEqual(ctx["overdue_count"], 1)
        self.assertEqual(ctx["upcoming_total"], 2)
        atrasada = next(r for r in ctx["upcoming"] if r.pk == self.atrasada.pk)
        self.assertTrue(atrasada.is_overdue)
        self.assertIn("Sentar", atrasada.step)
        self.assertEqual(atrasada.tone, "info")
        self.assertIn("status__in=pending%2Cconfirmed", ctx["overdue_url"])
        ocupadas = {t["code"] for a in ctx[AREAS] for t in a[TABLES] if t["busy"]}
        self.assertEqual(ocupadas, {"S1", "S2"})

    def test_graficos_del_dia_y_de_la_semana(self):
        ctx = dashboard_callback(None, {})
        self.assertEqual(ctx[HOURLY][0], "19:00")
        self.assertEqual(len(ctx[HOURLY]), 8)
        self.assertEqual(len(ctx["history"]), 7)
        self.assertTrue(ctx["history"][-1]["is_today"])
        self.assertGreaterEqual(ctx["history_total"], 5)
        self.assertIn("Mostrador / walk-in", ctx["source_labels"])
        self.assertEqual([s["name"] for s in ctx["shifts"]], ["Cena"])

    def test_resumen_de_manana_semana_y_mes(self):
        manana = self.hoy + timedelta(days=1)
        services.create_reservation(
            venue=self.venue, guest=self.guest, party_size=2, tables=[self.t1],
            starts_at=datetime.combine(manana, time(21), tzinfo=LIMA),
            source=Reservation.Source.STAFF,
        )
        pendiente = services.create_reservation(
            venue=self.venue, guest=self.guest, party_size=2, tables=[self.t2],
            starts_at=datetime.combine(manana, time(21), tzinfo=LIMA),
            source=Reservation.Source.STAFF, status=Reservation.Status.PENDING,
        )
        services.cancel(services.create_reservation(
            venue=self.venue, guest=self.guest, party_size=6, tables=[self.t3],
            starts_at=datetime.combine(manana, time(21), tzinfo=LIMA),
            source=Reservation.Source.STAFF,
        ), reason="Cambio de planes")
        resumen = dashboard_callback(None, {})[SUMMARY]

        self.assertEqual(resumen[TOMORROW]["date"], manana)
        self.assertEqual(len(resumen[TOMORROW]["rows"]), 2)
        self.assertEqual(resumen[TOMORROW]["pending"], 1)
        self.assertEqual(resumen[TOMORROW]["cancelled"], 1)
        self.assertIn(pendiente, resumen[TOMORROW]["rows"])
        self.assertEqual(resumen[TOMORROW]["rows"][0].local_time.hour, 21)
        self.assertEqual(len(resumen["week"]["days"]), 7)
        self.assertIsNotNone(resumen["week"]["change"])  # hace 7 dias vino gente
        self.assertEqual(resumen[MONTH]["no_shows"], 1)
        self.assertGreater(resumen[MONTH]["no_show_rate"], 0)
        self.assertIsNotNone(resumen[MONTH]["best_day"])

    def test_turno_que_cruza_la_medianoche(self):
        Shift.objects.filter(venue=self.venue).update(end_time=time(1, 0))
        etiquetas = dashboard_callback(None, {})[HOURLY]
        self.assertEqual(etiquetas[-1], "00:30")
        self.assertEqual(len(etiquetas), 12)

    def test_sin_turnos_no_hay_grafico_por_horas(self):
        Shift.objects.filter(venue=self.venue).update(is_active=False)
        ctx = dashboard_callback(None, {})
        self.assertEqual(ctx[HOURLY], [])
        self.assertEqual(ctx["hourly_peak"], 0)
        self.assertEqual(ctx["shifts"], [])

    def test_sin_local_el_panel_queda_vacio(self):
        Venue.objects.update(is_active=False)
        self.assertEqual(dashboard_callback(None, {}), {READY: False})

    def test_la_portada_del_admin_se_dibuja(self):
        self.client.force_login(_staff("host", "view_reservation"))
        r = self.client.get(reverse("admin:index"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context[READY])
        self.assertContains(r, "Cena")


class PanelVacioTests(TestCase):
    def test_sin_mesas_ni_reservas(self):
        Venue.objects.create(name="Zisa", timezone="America/Lima")
        ctx = dashboard_callback(None, {})
        self.assertEqual(ctx["day_kpis"][0]["hint"], "sin mesas cargadas")
        self.assertEqual(ctx[NOW_KPIS][1]["hint"], "sala completa")
        self.assertEqual(ctx[NOW_KPIS][2]["hint"], "nadie con retraso")
        self.assertIsNone(ctx[SUMMARY]["week"]["change"])
        self.assertEqual(ctx[SUMMARY][MONTH]["avg_party"], 0)


class UsuariosTests(TestCase):
    def test_el_admin_crea_un_usuario_desde_el_panel(self):
        admin_user = User.objects.create_superuser("jefe", password=secrets.token_urlsafe())
        self.client.force_login(admin_user)
        clave = secrets.token_urlsafe()
        response = self.client.post(reverse("admin:auth_user_add"), {
            "username": "anfitriona", "usable_password": "true",
            "password1": clave, "password2": clave,
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.get(username="anfitriona").check_password(clave))
