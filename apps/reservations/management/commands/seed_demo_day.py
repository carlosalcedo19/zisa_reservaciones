"""
Llena el servicio de hoy con reservas de ejemplo para ver el panel con datos.

    python manage.py seed_demo_day
    python manage.py seed_demo_day --clear   # borra las de ejemplo y sale

Solo toca reservas cuyas notas internas llevan la marca DEMO_TAG, asi que
nunca se lleva por delante datos reales.
"""

from datetime import datetime, time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.guests.models import Guest, Tag
from apps.reservations import services
from apps.reservations.models import Reservation
from apps.venues.models import Venue

DEMO_TAG = "[demo]"

# (nombre, telefono, hora, comensales, origen, ocasion, estado final)
PEOPLE = [
    ("Ana Rojas", "+51987000001", time(19, 0), 2, "web", "", "finished"),
    ("Carlos Vega", "+51987000002", time(19, 30), 2, "phone", "", "confirmed"),
    ("Familia Aguirre", "+51987000003", time(19, 30), 6, "web", "Cumpleanos", "seated"),
    ("Marta Quispe", "+51987000004", time(20, 0), 4, "phone", "", "pending"),
    ("Luis Salas", "+51987000005", time(20, 0), 2, "whatsapp", "", "confirmed"),
    ("Rita Ibanez", "+51987000006", time(20, 30), 2, "web", "Aniversario", "confirmed"),
    ("Kenji Nakamura", "+51987000007", time(21, 0), 3, "web", "", "confirmed"),
    ("Sofia Delgado", "+51987000008", time(21, 0), 4, "staff", "Negocios", "confirmed"),
]


class Command(BaseCommand):
    help = "Crea reservas de ejemplo en el servicio de hoy."

    def add_arguments(self, parser):
        parser.add_argument("--venue", help="Nombre de la sede.")
        parser.add_argument("--clear", action="store_true",
                            help="Borra las reservas de ejemplo y termina.")

    @transaction.atomic
    def handle(self, *args, **options):
        venue = (
            Venue.objects.filter(name=options["venue"]).first()
            if options["venue"]
            else Venue.objects.filter(is_active=True).order_by("name").first()
        )
        if venue is None:
            raise CommandError("No hay sede. Corre primero: manage.py seed_venue")

        existing = Reservation.objects.filter(
            venue=venue, internal_notes__startswith=DEMO_TAG
        )
        removed = existing.count()
        existing.delete()
        if options["clear"]:
            self.stdout.write(self.style.SUCCESS(f"{removed} reservas de ejemplo borradas."))
            return

        tz = services.venue_tz(venue)
        today = services.service_date_for(venue, timezone.now())
        allergy = Tag.objects.filter(name="Alergia a mariscos").first()

        created, skipped = 0, []
        for name, phone, at, party, source, occasion, final in PEOPLE:
            guest = services.get_or_create_guest(phone, name)
            starts_at = datetime.combine(today, at, tzinfo=tz)
            try:
                # Se crean como alta de personal para no chocar con la ventana
                # de antelacion de la web, y despues se marca el origen real:
                # son datos de ejemplo, no reservas que entraron por ese canal.
                reservation = services.create_reservation(
                    venue=venue, guest=guest, starts_at=starts_at, party_size=party,
                    source=Reservation.Source.STAFF, occasion=occasion,
                    internal_notes=f"{DEMO_TAG} generada por seed_demo_day",
                )
            except services.ReservationError as exc:
                skipped.append(f"{name} {at:%H:%M}: {exc}")
                continue

            reservation.source = source
            reservation.save(update_fields=["source"])

            if final == "seated":
                services.seat(reservation)
            elif final == "finished":
                services.seat(reservation)
                services.finish(reservation)
            elif final == "pending":
                reservation.status = Reservation.Status.PENDING
                reservation.confirmed_at = None
                reservation.save(update_fields=["status", "confirmed_at"])

            if allergy and name == "Marta Quispe":
                reservation.tags.add(allergy)
            created += 1

        if removed:
            self.stdout.write(f"{removed} reservas de ejemplo anteriores borradas.")
        for line in skipped:
            self.stdout.write(self.style.WARNING(f"omitida - {line}"))
        self.stdout.write(
            self.style.SUCCESS(f"{created} reservas de ejemplo creadas para el {today}.")
        )
