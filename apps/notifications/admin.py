from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin

from apps.notifications.models import Notification

STATUS_TONES = {
    Notification.SendStatus.PENDING: "warn",
    Notification.SendStatus.SENT: "info",
    Notification.SendStatus.DELIVERED: "ok",
    Notification.SendStatus.FAILED: "bad",
}


@admin.register(Notification)
class NotificationAdmin(ModelAdmin):
    list_display = ("created_at", "reservation", "template", "channel", "recipient",
                    "status_chip", "attempts")
    list_filter = ("channel", "template", "send_status")
    search_fields = ("reservation__code", "recipient", "provider_id")
    date_hierarchy = "created_at"
    list_select_related = ("reservation",)
    readonly_fields = ("created_at", "sent_at")

    @admin.display(description="estado", ordering="send_status")
    def status_chip(self, obj):
        return format_html(
            '<span class="zs-chip" data-tone="{}">{}</span>',
            STATUS_TONES.get(obj.send_status, "muted"), obj.get_send_status_display(),
        )
