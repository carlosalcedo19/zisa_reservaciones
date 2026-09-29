from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group, User
from django.contrib.auth.forms import ReadOnlyPasswordHashWidget
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

# Sin re-registrar con los formularios de Unfold, crear usuarios y cambiar
# claves sale con el formulario de Django sin estilos.
admin.site.unregister(User)
admin.site.unregister(Group)


class HiddenPasswordWidget(ReadOnlyPasswordHashWidget):
    """Oculta algoritmo, sal y hash: no sirven de nada en el panel."""

    def render(self, name, value, attrs=None, renderer=None):
        usable = bool(value) and not value.startswith("!")
        return format_html(
            '<div class="readonly bg-base-50 border border-base-200 font-medium '
            'max-w-2xl px-3 py-2 rounded-default shadow-xs dark:bg-white/[.02] '
            'dark:border-base-700">{}</div>',
            "••••••••••" if usable else "Sin contraseña",
        )


PERM_VERBS = {"add": "Puede añadir", "change": "Puede modificar",
              "delete": "Puede borrar", "view": "Puede ver"}


def permission_label(perm):
    """Los nombres de permiso se guardan en ingles al migrar ("Can add ..."): se rehacen aqui."""
    model = perm.content_type.model_class()
    accion = perm.codename.split("_", 1)[0]
    if model is None or accion not in PERM_VERBS:
        return str(perm)
    meta = model._meta
    return f"{meta.app_config.verbose_name} | {PERM_VERBS[accion]} {meta.verbose_name}"


class PermisosEnEspanol:
    def formfield_for_manytomany(self, db_field, request, **kwargs):
        field = super().formfield_for_manytomany(db_field, request, **kwargs)
        if db_field.name in ("permissions", "user_permissions"):
            field.label_from_instance = permission_label
        return field


class ZisaUserChangeForm(UserChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password"].widget = HiddenPasswordWidget()
        self.fields["password"].help_text = mark_safe(
            '<a href="../password/" class="text-primary-600 dark:text-primary-500">'
            "Cambiar contraseña</a>"
        )


@admin.register(User)
class UserAdmin(PermisosEnEspanol, BaseUserAdmin, ModelAdmin):
    form = ZisaUserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm
    list_display = ("username", "first_name", "last_name", "email", "is_active",
                    "is_superuser", "last_login")
    list_filter = ("is_active", "is_superuser", "groups")


@admin.register(Group)
class GroupAdmin(PermisosEnEspanol, BaseGroupAdmin, ModelAdmin):
    pass
