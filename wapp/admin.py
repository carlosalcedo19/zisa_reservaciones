from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group, User
from unfold.admin import ModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

# Sin re-registrar con los formularios de Unfold, crear usuarios y cambiar
# claves sale con el formulario de Django sin estilos.
admin.site.unregister(User)
admin.site.unregister(Group)


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm
    list_display = ("username", "first_name", "last_name", "email", "is_active",
                    "is_superuser", "last_login")
    list_filter = ("is_active", "is_superuser", "groups")


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin, ModelAdmin):
    pass
