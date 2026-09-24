"""
Tests de la parte con mas aristas: disponibilidad, solapamiento y estados.

    python manage.py test apps.reservations
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.guests.models import Guest, normalize_phone
from apps.reservations import services
from apps.reservations.models import Reservation, TableOccupancy
from apps.venues.models import Area, DurationRule, Shift, Table, TableCombination, Venue

LIMA = ZoneInfo("America/Lima")


class BaseSalaTestCase(TestCase):
    """Sala minima: un salon con tres mesas y un turno de cena."""

    @classmethod
    def setUpTestData(cls):
        cls.venue = Venue.objects.create(name="Zisa", timezone="America/Lima")
        cls.area = Area.objects.create(venue=cls.venue, name="Salon")
        cls.t1 = Table.objects.create(area=cls.area, code="S1", min_seats=2, max_seats=4)
        cls.t2 = Table.objects.create(area=cls.area, code="S2", min_seats=2, max_seats=4)
        cls.t3 = Table.objects.create(area=cls.area, code="S3", min_seats=6, max_seats=8)

        cls.combo = TableCombination.objects.create(
            venue=cls.venue, name="S1+S2", min_seats=5, max_seats=8
        )
        cls.combo.tables.set([cls.t1, cls.t2])

        Shift.objects.create(
            venue=cls.venue, name="Cena", weekdays="0,1,2,3,4,5,6",
            start_time=time(19, 0), last_seating=time(21, 45), end_time=time(23, 0),
        )
        DurationRule.objects.create(venue=cls.venue, min_party=1, max_party=4, minutes=90)
        DurationRule.objects.create(venue=cls.venue, min_party=5, max_party=8, minutes=120)

        cls.guest = Guest.objects.create(phone="+51987654321", name="Ana Rojas")

    def cena(self, dias_desde_hoy=1, hora=20, minuto=0):
        """Un inicio valido dentro del turno de cena, en hora de Lima."""
        dia = timezone.localtime(timezone.now(), LIMA).date() + timedelta(
            days=dias_desde_hoy
        )
        return datetime.combine(dia, time(hora, minuto), tzinfo=LIMA)


class TelefonoTests(TestCase):
    def test_normaliza_las_formas_que_escribe_la_gente(self):
        esperado = "+51987654321"
        for entrada in ["987654321", "987 654 321", "+51 987-654-321",
                        "0051987654321", "(51) 987654321"]:
            with self.subTest(entrada=entrada):
                self.assertEqual(normalize_phone(entrada), esperado)

    def test_el_cliente_se_guarda_normalizado(self):
        guest = Guest.objects.create(phone="987 654 999", name="Luis Salas")
        self.assertEqual(guest.phone, "+51987654999")


class DisponibilidadTests(BaseSalaTestCase):
    def test_elige_la_mesa_con_menos_desperdicio(self):
        inicio = self.cena()
        ofertas = services.find_availability(self.venue, inicio,
                                             inicio + timedelta(minutes=90), 2)
        # S3 es de 6-8, no acepta un grupo de 2; S1 y S2 si.
        self.assertTrue(ofertas)
        self.assertEqual(ofertas[0].slack, 2)
        self.assertIn(ofertas[0].codes, {"S1", "S2"})

    def test_usa_una_combinacion_cuando_ninguna_mesa_alcanza_sola(self):
        inicio = self.cena()
        # Ocupamos S3, la unica mesa que sola aguanta 6.
        services.create_reservation(
            venue=self.venue, guest=self.guest, starts_at=inicio, party_size=6,
            source=Reservation.Source.PHONE, tables=[self.t3],
        )
        otro = Guest.objects.create(phone="+51900000001", name="Bruno Vega")
        reserva = services.create_reservation(
            venue=self.venue, guest=otro, starts_at=inicio, party_size=6,
            source=Reservation.Source.PHONE,
        )
        self.assertEqual(reserva.table_codes, "S1, S2")

    def test_no_hay_mesa_cuando_todo_esta_ocupado(self):
        inicio = self.cena()
        for indice, mesa in enumerate([self.t1, self.t2, self.t3]):
            guest = Guest.objects.create(phone=f"+5190000010{indice}", name=f"G{indice}")
            services.create_reservation(
                venue=self.venue, guest=guest, starts_at=inicio, party_size=2,
                source=Reservation.Source.PHONE, tables=[mesa],
            )
        otro = Guest.objects.create(phone="+51900000199", name="Sin suerte")
        with self.assertRaises(services.NoAvailability):
            services.create_reservation(
                venue=self.venue, guest=otro, starts_at=inicio, party_size=2,
                source=Reservation.Source.PHONE,
            )


class SolapamientoTests(BaseSalaTestCase):
    def test_la_base_rechaza_dos_ocupaciones_activas_que_se_pisan(self):
        """
        La defensa central del sistema. No depende de la capa de servicio: es
        Postgres el que rechaza la fila.
        """
        inicio = self.cena()
        reserva = services.create_reservation(
            venue=self.venue, guest=self.guest, starts_at=inicio, party_size=2,
            source=Reservation.Source.PHONE, tables=[self.t1],
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TableOccupancy.objects.create(
                    table=self.t1, reservation=reserva,
                    starts_at=inicio + timedelta(minutes=30),
                    ends_at=inicio + timedelta(minutes=120),
                )

    def test_cancelar_libera_la_mesa_de_inmediato(self):
        inicio = self.cena()
        reserva = services.create_reservation(
            venue=self.venue, guest=self.guest, starts_at=inicio, party_size=2,
            source=Reservation.Source.PHONE, tables=[self.t1],
        )
        services.cancel(reserva, reason="El cliente llamo", user=None)

        otro = Guest.objects.create(phone="+51900000002", name="Marta Quispe")
        nueva = services.create_reservation(
            venue=self.venue, guest=otro, starts_at=inicio, party_size=2,
            source=Reservation.Source.PHONE, tables=[self.t1],
        )
        self.assertEqual(nueva.table_codes, "S1")

    def test_un_bloqueo_impide_reservar_encima(self):
        inicio = self.cena()
        services.block_table(self.t1, inicio, inicio + timedelta(hours=3),
                             reason="Toldo roto")
        ofertas = services.find_availability(self.venue, inicio,
                                             inicio + timedelta(minutes=90), 2)
        self.assertNotIn("S1", [o.codes for o in ofertas])

    def test_reservas_consecutivas_no_se_pisan(self):
        """Un rango termina donde empieza el siguiente: tstzrange es [inicio, fin)."""
        inicio = self.cena(hora=19)
        services.create_reservation(
            venue=self.venue, guest=self.guest, starts_at=inicio, party_size=2,
            source=Reservation.Source.PHONE, tables=[self.t1],
        )
        otro = Guest.objects.create(phone="+51900000003", name="Rita Ibanez")
        segunda = services.create_reservation(
            venue=self.venue, guest=otro, starts_at=inicio + timedelta(minutes=90),
            party_size=2, source=Reservation.Source.PHONE, tables=[self.t1],
        )
        self.assertEqual(segunda.table_codes, "S1")


class EstadoTests(BaseSalaTestCase):
    def _reserva(self):
        return services.create_reservation(
            venue=self.venue, guest=self.guest, starts_at=self.cena(), party_size=2,
            source=Reservation.Source.PHONE, tables=[self.t1],
        )

    def test_recorrido_completo_deja_rastro(self):
        reserva = self._reserva()
        services.seat(reserva)
        reserva = services.finish(reserva)

        self.assertEqual(reserva.status, Reservation.Status.FINISHED)
        self.assertIsNotNone(reserva.seated_at)
        self.assertIsNotNone(reserva.released_at)
        self.assertEqual(reserva.events.count(), 3)  # creada, sentada, finalizada
        self.assertFalse(reserva.occupancies.filter(is_active=True).exists())

    def test_no_se_puede_sentar_una_reserva_cancelada(self):
        reserva = self._reserva()
        services.cancel(reserva, reason="prueba")
        with self.assertRaises(services.InvalidTransition):
            services.seat(reserva)

    def test_el_no_show_suma_al_contador_del_cliente(self):
        reserva = self._reserva()
        services.mark_no_show(reserva)
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.no_shows, 1)
        self.assertTrue(self.guest.is_risky is False)  # hace falta un segundo planton


class CalendarioTests(BaseSalaTestCase):
    def test_rechaza_una_hora_fuera_de_turno(self):
        fuera = self.cena(hora=17)
        with self.assertRaises(services.NoAvailability):
            services.create_reservation(
                venue=self.venue, guest=self.guest, starts_at=fuera, party_size=2,
                source=Reservation.Source.PHONE,
            )

    def test_rechaza_despues_de_la_ultima_entrada(self):
        tarde = self.cena(hora=22, minuto=30)
        with self.assertRaises(services.NoAvailability):
            services.create_reservation(
                venue=self.venue, guest=self.guest, starts_at=tarde, party_size=2,
                source=Reservation.Source.PHONE,
            )

    def test_la_madrugada_pertenece_al_servicio_de_la_noche_anterior(self):
        medianoche = datetime.combine(date(2026, 9, 12), time(0, 30), tzinfo=LIMA)
        self.assertEqual(
            services.service_date_for(self.venue, medianoche), date(2026, 9, 11)
        )

    def test_el_grupo_grande_no_pasa_por_web(self):
        with self.assertRaises(services.ReservationError):
            services.create_reservation(
                venue=self.venue, guest=self.guest, starts_at=self.cena(),
                party_size=20, source=Reservation.Source.WEB,
            )


class BorradoTests(BaseSalaTestCase):
    """
    Borrar una reserva desde el admin arrastra sus movimientos, ocupaciones y
    notificaciones. Antes, un usuario con permiso para borrar reservas no
    podia: Django le pedia tambien permiso sobre "movimiento", que no se da
    a nadie para que la bitacora no se toque.
    """

    def setUp(self):
        from django.contrib.auth.models import Permission, User

        self.staff = User.objects.create_user("host", password="x", is_staff=True)
        self.staff.user_permissions.add(
            Permission.objects.get(codename="view_reservation"),
            Permission.objects.get(codename="delete_reservation"),
        )
        self.reserva = services.create_reservation(
            venue=self.venue, guest=self.guest, starts_at=self.cena(), party_size=2,
            source=Reservation.Source.STAFF,
        )
        services.seat(self.reserva)  # nace confirmada; sentar deja otro movimiento

    def test_staff_con_permiso_puede_borrar_la_reserva_y_su_historial(self):
        from django.urls import reverse

        from apps.reservations.models import ReservationEvent

        self.client.force_login(self.staff)
        url = reverse("admin:reservations_reservation_delete", args=[self.reserva.pk])

        pagina = self.client.get(url)
        self.assertEqual(pagina.status_code, 200)
        self.assertFalse(pagina.context["perms_lacking"])

        self.client.post(url, {"post": "yes"})
        self.assertFalse(Reservation.objects.filter(pk=self.reserva.pk).exists())
        self.assertFalse(ReservationEvent.objects.filter(reservation_id=self.reserva.pk).exists())
        self.assertFalse(TableOccupancy.objects.filter(reservation_id=self.reserva.pk).exists())

    def test_los_movimientos_siguen_sin_poder_borrarse_sueltos(self):
        from django.contrib import admin

        from apps.reservations.models import ReservationEvent

        evento = ReservationEvent.objects.filter(reservation=self.reserva).first()
        ma = admin.site._registry[ReservationEvent]
        self.assertFalse(ma.has_delete_permission(None, evento))


class ReservaWebTests(BaseSalaTestCase):
    """
    Entrada del formulario "Reserva una mesa" de zisa.pe (apps/reservations/api.py).
    Los campos llevan los mismos nombres que en el Contact Form 7.
    """

    TOKEN = "clave-de-prueba"

    def post(self, token=TOKEN, **campos):
        from django.test import override_settings

        datos = {
            "full-name": "Lucia Paredes",
            "your-phone": "987 111 222",
            "num-person": "2",
            "date-reservation": self.cena().date().isoformat(),
            "time-field": "08:00 PM",
            "indications": "Mesa junto a la ventana",
        }
        datos.update({k.replace("_", "-"): v for k, v in campos.items()})
        cabeceras = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        with override_settings(WEB_RESERVATION_TOKEN=self.TOKEN):
            return self.client.post("/api/reservas/web/", datos, **cabeceras)

    def test_crea_la_reserva_web_con_mesa_y_cliente(self):
        r = self.post()
        self.assertEqual(r.status_code, 201, r.content)
        cuerpo = r.json()
        self.assertTrue(cuerpo["ok"])

        reserva = Reservation.objects.get(code=cuerpo["code"])
        self.assertEqual(reserva.source, Reservation.Source.WEB)
        self.assertEqual(reserva.party_size, 2)
        self.assertEqual(reserva.guest.phone, "+51987111222")
        self.assertEqual(reserva.guest_notes, "Mesa junto a la ventana")
        # "08:00 PM" del desplegable son las 20:00 de Lima.
        self.assertEqual(timezone.localtime(reserva.starts_at, LIMA).hour, 20)
        self.assertNotEqual(reserva.table_codes, "sin asignar")
        self.assertIn(cuerpo["code"], cuerpo["message"])

    def test_sin_clave_o_con_otra_no_entra(self):
        self.assertEqual(self.post(token=None).status_code, 401)
        self.assertEqual(self.post(token="otra").status_code, 401)
        self.assertFalse(Reservation.objects.exists())

    def test_sin_clave_configurada_la_entrada_esta_apagada(self):
        from django.test import override_settings

        with override_settings(WEB_RESERVATION_TOKEN=""):
            r = self.client.post("/api/reservas/web/", {},
                                 HTTP_AUTHORIZATION="Bearer ")
        self.assertEqual(r.status_code, 503)

    def test_datos_incompletos_dicen_que_campo_falla(self):
        r = self.post(time_field="Hora de reserva", full_name="")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(set(r.json()["errors"]), {"time-field", "full-name"})

    def test_acepta_hora_en_24h(self):
        r = self.post(time_field="20:30")
        self.assertEqual(r.status_code, 201, r.content)

    def test_sin_mesa_devuelve_el_motivo(self):
        # Para parejas solo sirven S1 y S2 (S3 pide minimo 6): la tercera
        # pareja a la misma hora ya no cabe.
        for telefono in ("987000001", "987000002"):
            self.assertEqual(self.post(your_phone=telefono).status_code, 201)
        r = self.post(your_phone="987000003")
        self.assertEqual(r.status_code, 409)
        self.assertIn("No hay mesa", r.json()["message"])

    def test_fuera_de_turno_devuelve_el_motivo(self):
        r = self.post(time_field="07:00 AM")
        self.assertEqual(r.status_code, 409)
        self.assertIn("turno", r.json()["message"])

    def test_reenviar_el_formulario_no_duplica(self):
        primera = self.post().json()
        r = self.post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], primera["code"])
        self.assertEqual(Reservation.objects.count(), 1)

    def test_un_rechazo_no_deja_cliente_creado(self):
        r = self.post(num_person="30", your_phone="987 333 111")
        self.assertEqual(r.status_code, 409)
        self.assertFalse(Guest.objects.filter(phone="+51987333111").exists())

    def test_grupo_grande_se_deriva_al_telefono(self):
        r = self.post(num_person="30")
        self.assertEqual(r.status_code, 409)
        self.assertIn("teléfono", r.json()["message"])


class TareaNoShowTests(BaseSalaTestCase):
    """Ruta que llama cron-job.org cada cinco minutos (api.sweep_no_shows_task)."""

    TOKEN = "clave-cron"

    def llamar(self, token=TOKEN):
        from django.test import override_settings

        cabeceras = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        with override_settings(CRON_TOKEN=self.TOKEN):
            return self.client.post("/api/tareas/no-shows/", **cabeceras)

    def reserva_atrasada(self, minutos):
        """Reserva confirmada que empezo hace `minutos` y nadie llego."""
        reserva = services.create_reservation(
            venue=self.venue, guest=self.guest, starts_at=self.cena(), party_size=2,
            source=Reservation.Source.STAFF,
        )
        self.assertEqual(reserva.status, Reservation.Status.CONFIRMED)
        inicio = timezone.now() - timedelta(minutes=minutos)
        Reservation.objects.filter(pk=reserva.pk).update(
            starts_at=inicio, ends_at=inicio + timedelta(minutes=90))
        return reserva

    def test_marca_solo_las_que_pasaron_la_gracia(self):
        vencida = self.reserva_atrasada(minutos=20)
        a_tiempo = self.reserva_atrasada(minutos=5)

        r = self.llamar()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["total"], 1)

        vencida.refresh_from_db()
        a_tiempo.refresh_from_db()
        self.assertEqual(vencida.status, Reservation.Status.NO_SHOW)
        self.assertEqual(a_tiempo.status, Reservation.Status.CONFIRMED)

    def test_sin_clave_o_con_otra_no_entra(self):
        self.reserva_atrasada(minutos=20)
        self.assertEqual(self.llamar(token=None).status_code, 401)
        self.assertEqual(self.llamar(token="otra").status_code, 401)
        self.assertFalse(Reservation.objects.filter(
            status=Reservation.Status.NO_SHOW).exists())

    def test_sin_clave_configurada_la_ruta_esta_apagada(self):
        from django.test import override_settings

        with override_settings(CRON_TOKEN=""):
            r = self.client.post("/api/tareas/no-shows/", HTTP_AUTHORIZATION="Bearer ")
        self.assertEqual(r.status_code, 503)


class WalkInTests(BaseSalaTestCase):
    """Quien llega sin reserva: se sienta en el acto y la mesa deja de ofrecerse."""

    def ahora(self):
        return self.cena(hora=20)  # un "ahora" fijo dentro del turno de cena

    def test_sienta_en_el_acto_y_ocupa_la_mesa(self):
        r = services.seat_walk_in(self.venue, self.t1, 3, now=self.ahora())
        self.assertEqual(r.status, Reservation.Status.SEATED)
        self.assertEqual(r.source, Reservation.Source.WALK_IN)
        self.assertEqual(r.guest.name, services.WALK_IN_NAME)
        self.assertEqual(r.table_codes, "S1")
        self.assertEqual(r.duration_min, 90)

    def test_la_web_ya_no_ofrece_esa_mesa(self):
        services.seat_walk_in(self.venue, self.t1, 2, now=self.ahora())
        inicio = self.ahora() + timedelta(minutes=30)
        ofertas = services.find_availability(
            self.venue, inicio, inicio + timedelta(minutes=90), 2)
        mesas = {t.code for o in ofertas for t in o.tables}
        self.assertNotIn("S1", mesas)

    def test_con_telefono_usa_la_ficha_del_cliente(self):
        r = services.seat_walk_in(self.venue, self.t1, 2, name="Pedro Soto",
                                  phone="987 555 444", now=self.ahora())
        self.assertEqual(r.guest.phone, "+51987555444")
        self.assertEqual(r.guest.name, "Pedro Soto")

    def test_se_corta_antes_de_la_siguiente_reserva(self):
        services.create_reservation(
            venue=self.venue, guest=self.guest, party_size=2, tables=[self.t1],
            starts_at=self.ahora() + timedelta(minutes=60),
            source=Reservation.Source.STAFF,
        )
        r = services.seat_walk_in(self.venue, self.t1, 2, now=self.ahora())
        self.assertEqual(r.duration_min, 60)
        self.assertIsNotNone(r.cut_at)

    def test_no_sienta_si_la_siguiente_reserva_esta_encima(self):
        services.create_reservation(
            venue=self.venue, guest=self.guest, party_size=2, tables=[self.t1],
            starts_at=self.ahora() + timedelta(minutes=20),
            source=Reservation.Source.STAFF,
        )
        with self.assertRaises(services.NoAvailability):
            services.seat_walk_in(self.venue, self.t1, 2, now=self.ahora())

    def test_no_sienta_en_una_mesa_ocupada(self):
        services.seat_walk_in(self.venue, self.t1, 2, now=self.ahora())
        with self.assertRaises(services.NoAvailability):
            services.seat_walk_in(self.venue, self.t1, 2,
                                  now=self.ahora() + timedelta(minutes=10))

    def test_no_pasa_de_la_capacidad_de_la_mesa(self):
        with self.assertRaises(services.ReservationError):
            services.seat_walk_in(self.venue, self.t1, 6, now=self.ahora())

