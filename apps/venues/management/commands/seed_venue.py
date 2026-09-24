"""
Carga una sala de ejemplo para poder trabajar desde el primer minuto.

    python manage.py seed_venue
    python manage.py seed_venue --reset   # borra la sede y la vuelve a crear

Las mesas, capacidades y posiciones son un punto de partida: reemplazalas por
las reales de Zisa desde el admin.
"""

from datetime import time

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.guests.models import Tag
from apps.reservations.models import ReservationPolicy
from apps.venues.models import (
    Area,
    DurationRule,
    Shift,
    Table,
    TableCombination,
    Venue,
)

AREAS = [
    # (nombre, reservable, orden)
    ("Salon", True, 0),
    ("Terraza", True, 1),
    ("Barra", False, 2),
]

TABLES = {
    "Salon": [
        # (codigo, min, max, pos_x, pos_y)
        ("S1", 2, 4, 14, 30),
        ("S2", 2, 4, 14, 70),
        ("S3", 4, 6, 33, 30),
        ("S4", 6, 8, 33, 70),
        ("S5", 2, 4, 50, 50),
    ],
    "Terraza": [
        ("T1", 2, 4, 66, 30),
        ("T2", 4, 6, 66, 70),
        ("T3", 2, 4, 81, 50),
    ],
    "Barra": [
        ("B1", 1, 2, 94, 40),
        ("B2", 1, 2, 94, 60),
    ],
}

COMBINATIONS = [
    # (nombre, codigos, min, max)
    ("S3+S4", ["S3", "S4"], 9, 14),
    ("T1+T2", ["T1", "T2"], 7, 10),
]

SHIFTS = [
    # (nombre, dias, inicio, ultima entrada, cierre, duracion, orden)
    ("Almuerzo", "0,1,2,3,4,5,6", time(12, 0), time(15, 0), time(16, 0), 75, 0),
    ("Cena", "0,1,2,3,4,5,6", time(19, 0), time(21, 45), time(23, 0), 90, 1),
]

DURATION_RULES = [
    # (min pax, max pax, minutos)
    (1, 2, 75),
    (3, 4, 90),
    (5, 8, 120),
    (9, 20, 150),
]

TAGS = [
    ("VIP", Tag.Kind.PROFILE, "#1D4ED8", False),
    ("Prensa", Tag.Kind.PROFILE, "#7C3AED", False),
    ("Alergia a mariscos", Tag.Kind.ALLERGY, "#B91C1C", True),
    ("Celiaco", Tag.Kind.ALLERGY, "#B91C1C", True),
    ("Vegetariano", Tag.Kind.ALLERGY, "#047857", True),
    ("Prefiere terraza", Tag.Kind.PREFERENCE, "#B45309", False),
    ("Prefiere mesa tranquila", Tag.Kind.PREFERENCE, "#B45309", False),
]


class Command(BaseCommand):
    help = "Crea una sede de ejemplo con zonas, mesas, turnos, reglas y etiquetas."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="Zisa", help="Nombre de la sede.")
        parser.add_argument(
            "--reset", action="store_true",
            help="Borra la sede existente con ese nombre antes de crearla.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        name = options["name"]

        if options["reset"]:
            deleted, _ = Venue.objects.filter(name=name).delete()
            if deleted:
                self.stdout.write(self.style.WARNING(f"Sede '{name}' eliminada."))

        venue, created = Venue.objects.get_or_create(
            name=name,
            defaults={
                "timezone": "America/Lima",
                "address": "Av. Ejemplo 123, Lima",
                "phone": "+5115550123",
            },
        )
        if not created:
            self.stdout.write(
                self.style.WARNING(
                    f"La sede '{name}' ya existe. Usa --reset para recrearla."
                )
            )
            return

        areas = {}
        for area_name, bookable, order in AREAS:
            areas[area_name] = Area.objects.create(
                venue=venue, name=area_name, is_bookable=bookable, sort_order=order
            )

        tables = {}
        for area_name, rows in TABLES.items():
            for code, min_seats, max_seats, pos_x, pos_y in rows:
                tables[code] = Table.objects.create(
                    area=areas[area_name], code=code, min_seats=min_seats,
                    max_seats=max_seats, pos_x=pos_x, pos_y=pos_y,
                )

        for combo_name, codes, min_seats, max_seats in COMBINATIONS:
            combo = TableCombination.objects.create(
                venue=venue, name=combo_name, min_seats=min_seats, max_seats=max_seats
            )
            combo.tables.set([tables[c] for c in codes])

        for shift_name, weekdays, start, last, end, duration, order in SHIFTS:
            Shift.objects.create(
                venue=venue, name=shift_name, weekdays=weekdays, start_time=start,
                last_seating=last, end_time=end, default_duration_min=duration,
                sort_order=order,
            )

        for min_party, max_party, minutes in DURATION_RULES:
            DurationRule.objects.create(
                venue=venue, min_party=min_party, max_party=max_party, minutes=minutes
            )

        for tag_name, kind, color, kitchen in TAGS:
            Tag.objects.get_or_create(
                name=tag_name,
                defaults={"kind": kind, "color": color, "show_in_kitchen": kitchen},
            )

        ReservationPolicy.objects.get_or_create(venue=venue)

        self.stdout.write(
            self.style.SUCCESS(
                f"Sede '{venue.name}' creada: {len(areas)} zonas, {len(tables)} mesas "
                f"({venue.capacity} plazas), {len(SHIFTS)} turnos, "
                f"{len(COMBINATIONS)} combinaciones, {len(TAGS)} etiquetas."
            )
        )
