"""
Marca como no-show las reservas cuya ventana de gracia ya vencio.

Pensado para una tarea periodica cada cinco minutos:

    python manage.py sweep_no_shows
"""

from django.core.management.base import BaseCommand

from apps.reservations import services
from apps.venues.models import Venue


class Command(BaseCommand):
    help = "Marca no-shows segun la gracia definida en la politica de cada sede."

    def add_arguments(self, parser):
        parser.add_argument("--venue", help="Nombre de una sede concreta.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Solo informa cuantas reservas se marcarian.",
        )

    def handle(self, *args, **options):
        venues = Venue.objects.filter(is_active=True)
        if options["venue"]:
            venues = venues.filter(name=options["venue"])

        total = 0
        for venue in venues:
            if options["dry_run"]:
                from datetime import timedelta

                from django.utils import timezone

                from apps.reservations.models import Reservation

                cutoff = timezone.now() - timedelta(
                    minutes=services.policy_for(venue).no_show_grace_min
                )
                count = Reservation.objects.filter(
                    venue=venue,
                    status__in=[Reservation.Status.PENDING,
                                Reservation.Status.CONFIRMED],
                    starts_at__lt=cutoff,
                ).count()
                self.stdout.write(f"{venue.name}: {count} reservas se marcarian.")
                total += count
                continue

            marked = services.sweep_no_shows(venue)
            self.stdout.write(f"{venue.name}: {marked} marcadas como no-show.")
            total += marked

        verb = "se marcarian" if options["dry_run"] else "marcadas"
        self.stdout.write(self.style.SUCCESS(f"Total: {total} {verb}."))
