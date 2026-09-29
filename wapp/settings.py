"""Configuracion de Django. Los valores por entorno salen de .env (plantilla: .env.example)."""

import os
from pathlib import Path

from django.templatetags.static import static
from django.urls import reverse_lazy
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env(name, default=None):
    return os.getenv(name, default)


def env_bool(name, default=False):
    return str(env(name, str(default))).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in str(env(name, default)).split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Seguridad
# ---------------------------------------------------------------------------

SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("Falta DJANGO_SECRET_KEY. Copia .env.example a .env y completalo.")

DEBUG = env_bool("DJANGO_DEBUG", False)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

RENDER_EXTERNAL_HOSTNAME = env("RENDER_EXTERNAL_HOSTNAME")
if RENDER_EXTERNAL_HOSTNAME:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)
    CSRF_TRUSTED_ORIGINS.append(f"https://{RENDER_EXTERNAL_HOSTNAME}")

if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# ---------------------------------------------------------------------------
# Aplicaciones
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    # Unfold va antes de django.contrib.admin para sobrescribir sus plantillas.
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "unfold.contrib.inlines",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "wapp",
    "apps.venues",
    "apps.guests",
    "apps.reservations",
    "apps.notifications",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "wapp.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "wapp.wsgi.application"


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", "zisa_reservaciones"),
        "USER": env("DB_USER", "postgres"),
        "PASSWORD": env("DB_PASSWORD", ""),
        "HOST": env("DB_HOST", "localhost"),
        "PORT": env("DB_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}

if env("DATABASE_URL"):
    import dj_database_url

    DATABASES["default"] = dj_database_url.parse(
        env("DATABASE_URL"), conn_max_age=60, conn_health_checks=True,
    )

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# Negocio
# ---------------------------------------------------------------------------

DEFAULT_VENUE_NAME = env("DEFAULT_VENUE_NAME", "Zisa")

# Vacio = entrada web desactivada.
WEB_RESERVATION_TOKEN = env("WEB_RESERVATION_TOKEN", "")

# Vacio = /api/tareas/no-shows/ desactivada.
CRON_TOKEN = env("CRON_TOKEN", "")


# ---------------------------------------------------------------------------
# Correo: Brevo si hay BREVO_API_KEY, si no SMTP; sin EMAIL_HOST, a la consola
# ---------------------------------------------------------------------------

BREVO_API_KEY = env("BREVO_API_KEY", "")

EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "digital@zisa.pe")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
EMAIL_TIMEOUT = 15
EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    "anymail.backends.brevo.EmailBackend" if BREVO_API_KEY
    else "django.core.mail.backends.smtp.EmailBackend" if EMAIL_HOST
    else "django.core.mail.backends.console.EmailBackend",
)
ANYMAIL = {"BREVO_API_KEY": BREVO_API_KEY}
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "Zisa <digital@zisa.pe>")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# Brevo no admite imagenes en linea: el logo va por URL publica.
EMAIL_LOGO_URL = env(
    "EMAIL_LOGO_URL",
    f"https://{RENDER_EXTERNAL_HOSTNAME}/static/admin/img/zisa-logo.png"
    if RENDER_EXTERNAL_HOSTNAME else "",
)


# ---------------------------------------------------------------------------
# Autenticacion
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ---------------------------------------------------------------------------
# Internacionalizacion
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "es-pe"
# Sin esto, LocaleMiddleware usa el idioma del navegador (ingles a algunos).
LANGUAGES = [("es", "Español")]
TIME_ZONE = env("TIME_ZONE", "America/Lima")
USE_I18N = True
USE_TZ = True

FIRST_DAY_OF_WEEK = 1


# ---------------------------------------------------------------------------
# Archivos estaticos y de medios
# ---------------------------------------------------------------------------

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        # Sin Manifest: exigiria collectstatic antes de los tests.
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "apps.reservations": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}


# ---------------------------------------------------------------------------
# Unfold (tema del panel de administracion)
# ---------------------------------------------------------------------------


def _admin_link(title, icon, model):
    """Enlace a la lista de `model` ("app_modelo"), visible solo con permiso de verlo."""
    app, name = model.split("_", 1)
    return {
        "title": title,
        "icon": icon,
        "link": reverse_lazy(f"admin:{model}_changelist"),
        "permission": lambda request: request.user.has_perm(f"{app}.view_{name}"),
    }


UNFOLD = {
    "SITE_TITLE": "Zisa Reservaciones",
    "SITE_HEADER": "Zisa",
    "SITE_SUBHEADER": "Panel de reservaciones",
    "SITE_SYMBOL": "restaurant",
    "SITE_URL": None,
    "SITE_LOGO": lambda request: static("admin/img/zisa-logo.png"),
    "SITE_FAVICONS": [
        {
            "rel": "icon",
            "type": "image/png",
            "href": lambda request: static("admin/img/logo_blanco.png"),
        },
    ],
    "LOGIN": {
        "image": lambda request: static("admin/img/login-salon.jpg"),
    },
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": False,
    "DASHBOARD_CALLBACK": "wapp.dashboard.dashboard_callback",
    "BORDER_RADIUS": "8px",
    "STYLES": [lambda request: static("admin/zisa.css")],
    # Blanco y negro; los unicos colores son los de estado, en zisa.css.
    "COLORS": {
        "base": {
            "50": "oklch(98.4% 0 0)",
            "100": "oklch(96.2% 0 0)",
            "200": "oklch(92.2% 0 0)",
            "300": "oklch(86.4% 0 0)",
            "400": "oklch(70.8% 0 0)",
            "500": "oklch(55.6% 0 0)",
            "600": "oklch(43.9% 0 0)",
            "700": "oklch(35.1% 0 0)",
            "800": "oklch(25.5% 0 0)",
            "900": "oklch(17.6% 0 0)",
            "950": "oklch(10.6% 0 0)",
        },
        # En modo oscuro zisa.css invierte el 600 para que los botones se vean.
        "primary": {
            "50": "oklch(98.5% 0 0)",
            "100": "oklch(96.7% 0 0)",
            "200": "oklch(92% 0 0)",
            "300": "oklch(87% 0 0)",
            "400": "oklch(70.5% 0 0)",
            "500": "oklch(55.2% 0 0)",
            "600": "oklch(21% 0 0)",
            "700": "oklch(17% 0 0)",
            "800": "oklch(14% 0 0)",
            "900": "oklch(12% 0 0)",
            "950": "oklch(9% 0 0)",
        },
        "font": {
            "subtle-light": "var(--color-base-500)",
            "subtle-dark": "var(--color-base-400)",
            "default-light": "var(--color-base-600)",
            "default-dark": "var(--color-base-300)",
            "important-light": "var(--color-base-950)",
            "important-dark": "var(--color-base-50)",
        },
    },
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": [
            {
                "title": "Hoy",
                "separator": False,
                "items": [
                    {
                        "title": "Panel de inicio",
                        "icon": "space_dashboard",
                        "link": reverse_lazy("admin:index"),
                    },
                    {
                        "title": "Plano de sala",
                        "icon": "table_restaurant",
                        "link": reverse_lazy("floor"),
                    },
                    _admin_link("Reservas", "event_seat", "reservations_reservation"),
                    _admin_link("Lista de espera", "hourglass_top",
                                "reservations_waitlistentry"),
                ],
            },
            {
                "title": "Clientes",
                "separator": True,
                "items": [
                    _admin_link("Clientes", "person", "guests_guest"),
                    _admin_link("Etiquetas", "label", "guests_tag"),
                ],
            },
            {
                "title": "Sala",
                "separator": True,
                "collapsible": True,
                "items": [
                    _admin_link("Mesas", "table_bar", "venues_table"),
                    _admin_link("Zonas", "grid_view", "venues_area"),
                    _admin_link("Mesas combinadas", "join_full", "venues_tablecombination"),
                    _admin_link("Bloqueos y ocupación", "block", "reservations_tableoccupancy"),
                    _admin_link("Restaurantes", "storefront", "venues_venue"),
                ],
            },
            {
                "title": "Horarios",
                "separator": True,
                "collapsible": True,
                "items": [
                    _admin_link("Turnos", "schedule", "venues_shift"),
                    _admin_link("Días especiales", "event_busy", "venues_calendarexception"),
                    _admin_link("Duración por grupo", "timer", "venues_durationrule"),
                ],
            },
            {
                "title": "Ajustes",
                "separator": True,
                "collapsible": True,
                "items": [
                    _admin_link("Reglas de reserva", "rule",
                                "reservations_reservationpolicy"),
                    _admin_link("Notificaciones", "send", "notifications_notification"),
                    _admin_link("Historial de cambios", "history",
                                "reservations_reservationevent"),
                    _admin_link("Usuarios", "manage_accounts", "auth_user"),
                    _admin_link("Grupos y permisos", "groups", "auth_group"),
                ],
            },
        ],
    },
}
