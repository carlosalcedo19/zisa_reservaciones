from unittest import mock

from django.core import mail
from django.test import override_settings

from apps.guests.models import Guest
from apps.notifications.models import Notification
from apps.reservations import services
from apps.reservations.models import Reservation
from apps.reservations.tests import BaseSalaTestCase

FROM = "Zisa <digital@zisa.pe>"


@override_settings(DEFAULT_FROM_EMAIL=FROM)
class CorreoTests(BaseSalaTestCase):
    def setUp(self):
        self.cliente = Guest.objects.create(phone="+51911111111", name="Luis Paz",
                                            email="luis@example.com")

    def _reserva(self, status=None, guest=None):
        with self.captureOnCommitCallbacks(execute=True):
            return services.create_reservation(
                venue=self.venue, guest=guest or self.cliente,
                starts_at=self.cena(), party_size=2,
                source=Reservation.Source.PHONE, status=status,
            )

    def test_confirmar_envia_el_correo_desde_digital(self):
        reserva = self._reserva()
        self.assertEqual(len(mail.outbox), 1)
        correo = mail.outbox[0]
        self.assertEqual(correo.from_email, FROM)
        self.assertEqual(correo.to, ["luis@example.com"])
        self.assertIn(reserva.code, correo.subject)
        self.assertIn("confirmada", correo.subject)
        self.assertIn("20:00", correo.body)
        self.assertIn("2 personas", correo.body)
        aviso = Notification.objects.get(reservation=reserva)
        self.assertEqual(aviso.send_status, Notification.SendStatus.SENT)

    def test_la_pendiente_avisa_al_confirmarse_y_solo_una_vez(self):
        reserva = self._reserva(status=Reservation.Status.PENDING)
        self.assertEqual(len(mail.outbox), 0)
        with self.captureOnCommitCallbacks(execute=True):
            services.confirm(reserva)
        self.assertEqual(len(mail.outbox), 1)
        # Un reenvio no duplica el correo.
        from apps.notifications.services import send
        send(reserva.pk, Notification.Template.CONFIRMATION)
        self.assertEqual(len(mail.outbox), 1)

    def test_cancelar_envia_el_motivo(self):
        reserva = self._reserva()
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            services.cancel(reserva, reason="Cerramos por imprevisto")
        self.assertEqual(len(mail.outbox), 1)
        correo = mail.outbox[0]
        self.assertIn("cancelada", correo.subject)
        self.assertIn("Motivo: Cerramos por imprevisto", correo.body)
        self.assertIn("Cerramos por imprevisto", correo.alternatives[0][0])

    def test_sin_correo_no_se_envia_nada(self):
        self._reserva(guest=self.guest)  # Ana no tiene correo
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(Notification.objects.exists())

    def test_si_falla_el_servidor_la_reserva_sigue_y_queda_anotado(self):
        with mock.patch("django.core.mail.EmailMultiAlternatives.send",
                        side_effect=OSError("SMTP caido")):
            reserva = self._reserva()
        reserva.refresh_from_db()
        self.assertEqual(reserva.status, Reservation.Status.CONFIRMED)
        aviso = Notification.objects.get(reservation=reserva)
        self.assertEqual(aviso.send_status, Notification.SendStatus.FAILED)
        self.assertIn("SMTP caido", aviso.error)
