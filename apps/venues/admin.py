from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from apps.venues.models import (
    Area,
    CalendarException,
    DurationRule,
    Shift,
    Table,
    TableCombination,
    Venue,
)


class AreaInline(TabularInline):
    model = Area
    extra = 0
    fields = ("name", "is_bookable", "sort_order")


class ShiftInline(TabularInline):
    model = Shift
    extra = 0
    fields = ("name", "weekdays", "start_time", "last_seating", "end_time",
              "default_duration_min", "is_active")


class DurationRuleInline(TabularInline):
    model = DurationRule
    extra = 0
    fields = ("min_party", "max_party", "minutes")


@admin.register(Venue)
class VenueAdmin(ModelAdmin):
    list_display = ("name", "timezone", "capacity_display", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "address")
    inlines = [AreaInline, ShiftInline, DurationRuleInline]
    fieldsets = (
        (None, {"fields": (("name", "is_active"),)}),
        ("Contacto", {"fields": ("address", ("phone", "email"))}),
        ("Horario", {
            "fields": ("timezone",),
            "description": "Todas las horas del panel se muestran en la hora de aquí.",
        }),
    )

    @admin.display(description="aforo")
    def capacity_display(self, obj):
        return f"{obj.capacity} plazas"


@admin.register(Area)
class AreaAdmin(ModelAdmin):
    list_display = ("name", "venue", "is_bookable", "table_count", "sort_order")
    list_filter = ("venue", "is_bookable")
    search_fields = ("name",)
    ordering = ("venue", "sort_order")

    @admin.display(description="mesas")
    def table_count(self, obj):
        return obj.tables.count()


@admin.register(Table)
class TableAdmin(ModelAdmin):
    list_display = ("code", "area", "seats", "is_combinable", "position", "is_active")
    list_filter = ("area__venue", "area", "is_active", "is_combinable")
    search_fields = ("code",)
    list_editable = ("is_active",)
    ordering = ("area", "code")
    fieldsets = (
        (None, {"fields": (("area", "code"), "is_active")}),
        ("Capacidad", {
            "fields": (("min_seats", "max_seats"), "is_combinable"),
            "description": "Pon también el mínimo, no solo el máximo: así evitas "
                           "que una pareja se quede con la mesa de ocho.",
        }),
        ("Plano de sala", {
            "fields": (("pos_x", "pos_y"),),
            "classes": ("collapse",),
            "description": "Opcional. Solo hace falta si algún día quieres ver la "
                           "sala dibujada.",
        }),
    )

    @admin.display(description="capacidad", ordering="max_seats")
    def seats(self, obj):
        return f"{obj.min_seats}-{obj.max_seats}"

    @admin.display(description="plano")
    def position(self, obj):
        return f"{obj.pos_x:.0f}, {obj.pos_y:.0f}"


@admin.register(TableCombination)
class TableCombinationAdmin(ModelAdmin):
    list_display = ("name", "venue", "tables_display", "min_seats", "max_seats",
                    "is_active")
    list_filter = ("venue", "is_active")
    filter_horizontal = ("tables",)
    search_fields = ("name",)

    @admin.display(description="mesas")
    def tables_display(self, obj):
        return " + ".join(obj.tables.values_list("code", flat=True))


@admin.register(Shift)
class ShiftAdmin(ModelAdmin):
    list_display = ("name", "venue", "hours", "last_seating", "weekdays_display",
                    "is_active")
    list_filter = ("venue", "is_active")
    ordering = ("venue", "sort_order")

    @admin.display(description="horario")
    def hours(self, obj):
        return f"{obj.start_time:%H:%M} - {obj.end_time:%H:%M}"

    @admin.display(description="días")
    def weekdays_display(self, obj):
        names = ["Lu", "Ma", "Mi", "Ju", "Vi", "Sa", "Do"]
        return " ".join(names[d] for d in sorted(obj.weekday_set))


@admin.register(DurationRule)
class DurationRuleAdmin(ModelAdmin):
    list_display = ("party_range", "minutes", "venue")
    list_filter = ("venue",)
    ordering = ("venue", "min_party")

    @admin.display(description="grupo")
    def party_range(self, obj):
        return f"{obj.min_party} - {obj.max_party} pax"


@admin.register(CalendarException)
class CalendarExceptionAdmin(ModelAdmin):
    list_display = ("reason", "venue", "kind", "start_date", "end_date")
    list_filter = ("venue", "kind")
    search_fields = ("reason",)
    date_hierarchy = "start_date"
    fieldsets = (
        (None, {"fields": (("venue", "kind"), "reason")}),
        ("Vigencia", {"fields": (("start_date", "end_date"),)}),
        ("Horario y aforo especiales", {
            "fields": (("start_time", "end_time"), "max_capacity"),
            "description": "Déjalo en blanco si el local simplemente cierra.",
        }),
    )
