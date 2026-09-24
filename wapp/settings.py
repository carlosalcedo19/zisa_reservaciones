"""
Configuracion de Django para el proyecto Zisa Reservaciones.

Los valores sensibles y los que cambian entre entornos se leen de un archivo
.env que no se versiona. Usa .env.example como plantilla.
"""

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

# Render publica el dominio del servicio en esta variable; asi el despliegue
# funciona sin tener que copiarlo a mano en DJANGO_ALLOWED_HOSTS.
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
    # Dominio
    "apps.venues",
    "apps.guests",
    "apps.reservations",
    "apps.notifications",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Sirve /static/ en produccion sin Nginx. Va justo despues de Security.
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

# En produccion (Neon) la conexion llega como una sola URL:
# postgresql://usuario:clave@host/base?sslmode=require
if env("DATABASE_URL"):
    import dj_database_url

    DATABASES["default"] = dj_database_url.parse(
        env("DATABASE_URL"), conn_max_age=60, conn_health_checks=True,
    )

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# Negocio
# ---------------------------------------------------------------------------

# Restaurante que viene elegido al crear una reserva desde el panel. Si no
# existe (o esta inactivo), se usa el primer restaurante activo.
DEFAULT_VENUE_NAME = env("DEFAULT_VENUE_NAME", "Zisa")

# Clave que comparte WordPress para enviar el formulario "Reserva una mesa"
# (ver apps/reservations/api.py). Vacia = entrada web desactivada.
WEB_RESERVATION_TOKEN = env("WEB_RESERVATION_TOKEN", "")

# Clave del servicio de cron externo (cron-job.org) que llama cada cinco
# minutos a /api/tareas/no-shows/. Vacia = esa ruta desactivada.
CRON_TOKEN = env("CRON_TOKEN", "")


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
#
# En un sistema de reservaciones la hora local es el dominio entero: se guarda
# en UTC (USE_TZ) y se muestra siempre en la zona del restaurante.
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "es-pe"
# El panel es solo en espanol. Sin esto, LocaleMiddleware toma el idioma del
# navegador y a quien lo tenga en ingles le sale "Username" y "Log in".
LANGUAGES = [("es", "Español")]
TIME_ZONE = env("TIME_ZONE", "America/Lima")
USE_I18N = True
USE_TZ = True

# El local abre la semana en lunes.
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
        # Solo comprime. La variante Manifest (con hash en el nombre) exige
        # correr collectstatic antes de cada test que pinte una plantilla.
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
    """
    Entrada del menu que apunta a la lista de un modelo ("app_modelo").

    Solo se muestra a quien puede ver ese modelo: sin esto, Unfold ensena
    todos los enlaces a cualquier usuario del panel y al pulsar sale un 403.
    """
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
    # Sin enlace "Volver al sitio" en el login: la raiz solo redirige al panel.
    "SITE_URL": None,
    # Logo negro sobre transparente; en modo oscuro zisa.css lo invierte.
    "SITE_LOGO": lambda request: static("admin/img/zisa-logo.png"),
    "LOGIN": {
        "image": lambda request: static("admin/img/login-salon.jpg"),
    },
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": False,
    "DASHBOARD_CALLBACK": "wapp.dashboard.dashboard_callback",
    "BORDER_RADIUS": "8px",
    "STYLES": [lambda request: static("admin/zisa.css")],
    # Paleta en blanco y negro: croma 0 en toda la escala, asi que no hay
    # tinte en ningun tono. El unico color del panel es el de los estados
    # (llega, atrasada, sentada...), definidos en static/admin/zisa.css.
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
        # Grafito: el acento es casi negro y sin tinte, como el resto de la
        # paleta. En tema claro Unfold usa 600 (botones, enlaces); en oscuro
        # usa 500 para enlaces, y zisa.css invierte 600 a casi blanco para
        # que los botones no desaparezcan sobre el fondo negro.
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
        # Ordenado por lo que se usa: arriba lo del servicio de cada dia,
        # despues los clientes y al final lo que se configura una vez. Los
        # tres grupos de configuracion van plegados (se abren solos si la
        # pagina actual esta dentro) para que el menu quepa sin scroll.
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
                ],
            },
        ],
    },
}
