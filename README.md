# Zisa Reservaciones

Sistema de reservas para restaurante sobre Django 5.2 + PostgreSQL, con el
panel de administración temado con [django-unfold](https://unfoldadmin.com/).

El código va en inglés (nombres de modelos, campos y funciones); las etiquetas
que ve el usuario en el panel van en español a través de `verbose_name`.

## Puesta en marcha

```bash
# 1. Dependencias
venv/Scripts/pip install -r requirements.txt

# 2. Variables de entorno
cp .env.example .env        # y completa DB_PASSWORD

# 3. Base de datos (PostgreSQL 17)
psql -U postgres -c "CREATE DATABASE zisa_reservaciones;"

# 4. Migraciones y datos de ejemplo
python manage.py migrate
python manage.py seed_venue
python manage.py createsuperuser

# 5. Arrancar
python manage.py runserver
```

La raíz redirige a `/admin/`.

## Estructura

```
apps/
  venues/          Venue, Area, Table, TableCombination, Shift,
                   DurationRule, CalendarException
  guests/          Guest, Tag
  reservations/    Reservation, TableOccupancy, ReservationEvent,
                   WaitlistEntry, ReservationPolicy
                   services.py  <- toda la logica de escritura
  notifications/   Notification
wapp/              settings, urls, wsgi, asgi
```

## Las cuatro decisiones que sostienen el diseño

**1. PostgreSQL, no SQLite.** El corazón del sistema es "¿esta mesa está libre
en este rango?". `TableOccupancy` lleva una `ExclusionConstraint` sobre
`(table, tstzrange(starts_at, ends_at))` filtrada a `is_active=True`: la base
rechaza el solapamiento venga del admin, de la web pública o de un script.
Requiere la extensión `btree_gist`, que habilita la migración
`reservations/0001_enable_btree_gist.py`.

Reservas y bloqueos de mesa comparten esa tabla — con `reservation = NULL` la
fila es un bloqueo — para que ambos compitan por el mismo espacio bajo la misma
restricción.

**2. La hora local es el dominio.** `USE_TZ = True`, se guarda en UTC y se
muestra en `America/Lima`. Cada reserva guarda además su `service_date`
calculada en la zona del local: una cena que termina a las 00:30 pertenece al
servicio de la noche anterior.

**3. Toda escritura pasa por `reservations/services.py`.** Nunca por un
`ModelForm` ni por el admin en crudo. Es el único lugar donde caben, en la
misma transacción, el `select_for_update`, el cálculo de `ends_at` y
`service_date`, la sincronización de `TableOccupancy` y el registro en
`ReservationEvent`. El admin expone las transiciones como acciones que llaman
a estas funciones.

```python
from apps.reservations import services

guest = services.get_or_create_guest("987 654 321", "Ana Rojas")
reserva = services.create_reservation(
    venue=venue, guest=guest, starts_at=inicio, party_size=4,
    source=Reservation.Source.PHONE,
)
services.seat(reserva)
services.finish(reserva)
```

**4. Las claves primarias son UUID.** Todos los modelos declaran
`id = UUIDField(primary_key=True, default=uuid4, editable=False)`. El
identificador de una reserva acaba en un enlace que se manda por WhatsApp, y
un correlativo delata cuántas reservas hay y deja probar la de al lado.

Eso no sustituye a `Reservation.code` (`ZS-3YGSJR`): el UUID es para las URLs,
el código es lo que se dicta por teléfono — seis caracteres, sin I/L/O/0/1.

## Máquina de estados

```
pending ──▶ confirmed ──▶ seated ──▶ finished
   │             │
   │             └──▶ no_show      (tarea automatica, al vencer no_show_grace_min)
   └─────────────┴──▶ cancelled

waitlisted ──▶ confirmed
```

Solo `pending`, `confirmed` y `seated` ocupan mesa. El resto libera el rango de
inmediato apagando `TableOccupancy.is_active`.

## Comandos

```bash
python manage.py seed_venue [--name Zisa] [--reset]
python manage.py sweep_no_shows [--venue Zisa] [--dry-run]
python manage.py test apps.reservations
```

`sweep_no_shows` está pensado para una tarea periódica cada cinco minutos.

## Despliegue (Render + Neon, plan gratuito)

- **Neon**: PostgreSQL. Su cadena de conexión va en `DATABASE_URL`, que si
  existe reemplaza a las variables `DB_*`.
- **Render**: el servicio web, descrito en `render.yaml` (New → Blueprint).
  `build.sh` instala, ejecuta `collectstatic` y migra en cada despliegue.
- **cron-job.org**: cada cinco minutos, `POST /api/tareas/no-shows/` con
  `Authorization: Bearer <CRON_TOKEN>`. Marca los no-shows y, de paso, evita
  que Render duerma el servicio (en el plan gratuito se duerme a los 15 min
  sin tráfico, y el formulario de la web esperaría casi un minuto).

## Formulario de reservas de zisa.pe

El formulario «Reserva una mesa» de la web (Contact Form 7) crea las reservas
directamente aquí, con las mismas reglas que el panel: antelación, grupo
máximo web, turnos, días cerrados y elección de mesa.

```
Cliente ──> WordPress (CF7) ──servidor a servidor──> POST /api/reservas/web/
                                  Authorization: Bearer <clave>
```

1. Django debe estar publicado en una URL HTTPS que el servidor de WordPress
   pueda alcanzar (por ejemplo `https://reservas.zisa.pe`).
2. En el `.env` de Django: `WEB_RESERVATION_TOKEN=<clave larga>`
   (vacía = entrada web apagada).
3. Copiar `integrations/wordpress/zisa-reservas.php` a
   `wp-content/mu-plugins/` y añadir a `wp-config.php`:

   ```php
   define('ZISA_RESERVAS_URL',   'https://reservas.zisa.pe/api/reservas/web/');
   define('ZISA_RESERVAS_TOKEN', '<la misma clave>');
   ```

Si no hay mesa (o el día está cerrado, o el grupo es grande), el cliente ve
el motivo en el formulario y no se abre WhatsApp. Si Django no responde, el
formulario sigue como antes (correo + WhatsApp) para no perder la reserva.

Quien llega sin reserva se registra desde **Plano de sala → mesa libre →
Sentar sin reserva**: la mesa queda ocupada y la web deja de ofrecerla.

## Lo que falta

- Vistas de servicio propias: línea de tiempo (mesas × horas), plano de sala y
  agenda para tablet. Hoy todo se opera desde el admin.
- Cara pública: enlace para que el cliente cancele o cambie su reserva.
- Envío real de notificaciones — el modelo `Notification` registra los envíos,
  pero todavía no hay proveedor conectado.
- Aforo por franja: hoy el límite es solo por mesas. Si la cocina tope a *N*
  comensales cada media hora, hace falta una entidad más.
