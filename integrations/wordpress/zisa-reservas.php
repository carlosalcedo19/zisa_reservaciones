<?php
/**
 * Plugin Name: Zisa – Reservas conectadas
 * Description: Envía el formulario «Reserva una mesa» (Contact Form 7) al sistema de reservas de Zisa. Si no hay mesa, el cliente ve el motivo en el formulario.
 * Version:     1.0.0
 * Author:      Zisa
 *
 * Instalación:
 *   1. Copia este archivo a wp-content/mu-plugins/zisa-reservas.php
 *      (la carpeta mu-plugins se crea si no existe; ahí se activa solo).
 *   2. Añade a wp-config.php, encima de «That's all, stop editing!»:
 *
 *        define('ZISA_RESERVAS_URL',   'https://TU-DOMINIO-DJANGO/api/reservas/web/');
 *        define('ZISA_RESERVAS_TOKEN', 'la misma clave que WEB_RESERVATION_TOKEN en el .env de Django');
 *
 *   Sin esas dos constantes el plugin no hace nada y el formulario funciona
 *   como siempre (correo + WhatsApp).
 *
 * Cómo se comporta:
 *   - Reserva creada           -> el formulario sigue su curso (correo y
 *                                 WhatsApp) y el mensaje de éxito muestra el
 *                                 código de la reserva.
 *   - Rechazada (sin mesa, día cerrado, grupo grande, datos mal)
 *                              -> se detiene el envío y el cliente ve el
 *                                 motivo. No se abre WhatsApp.
 *   - El sistema no responde   -> no se pierde el cliente: el formulario sigue
 *                                 como antes (correo + WhatsApp) y queda
 *                                 anotado en el log de PHP.
 */

if (!defined('ABSPATH')) {
    exit;
}

/** ID del formulario «Reserva una mesa» en Contact Form 7. */
if (!defined('ZISA_RESERVAS_FORM_ID')) {
    define('ZISA_RESERVAS_FORM_ID', 24585);
}

/** Mensaje de éxito devuelto por el sistema, para mostrarlo en el formulario. */
$GLOBALS['zisa_reservas_ok'] = null;

add_action('wpcf7_before_send_mail', 'zisa_reservas_enviar', 10, 3);

function zisa_reservas_enviar($contact_form, &$abort, $submission = null)
{
    if ((int) $contact_form->id() !== (int) ZISA_RESERVAS_FORM_ID) {
        return;
    }
    if (!defined('ZISA_RESERVAS_URL') || !defined('ZISA_RESERVAS_TOKEN')) {
        return; // Sin configurar: el formulario funciona como siempre.
    }
    $submission = $submission ?: WPCF7_Submission::get_instance();
    if (!$submission) {
        return;
    }

    $datos = $submission->get_posted_data();
    $campo = function ($nombre) use ($datos) {
        $valor = isset($datos[$nombre]) ? $datos[$nombre] : '';
        if (is_array($valor)) {          // los <select> llegan como lista
            $valor = reset($valor);
        }
        return trim((string) $valor);
    };

    $campos = array(
        'full-name'        => $campo('full-name'),
        'your-phone'       => $campo('your-phone'),
        'num-person'       => $campo('num-person'),
        'date-reservation' => $campo('date-reservation'),
        'time-field'       => $campo('time-field'),
        'indications'      => $campo('indications'),
    );
    // Se envía como formulario (application/x-www-form-urlencoded) y no como
    // JSON: no depende de wp_json_encode y Django lo lee igual.
    $cuerpo_envio = http_build_query($campos, '', '&');

    // Los campos van también en la URL. Desde este hosting algo en el camino
    // hacia Cloudflare vacía el cuerpo y las cabeceras (salvo Authorization);
    // la URL siempre llega entera. Django usa la URL si el cuerpo llega vacío.
    $url = ZISA_RESERVAS_URL . (strpos(ZISA_RESERVAS_URL, '?') === false ? '?' : '&') . $cuerpo_envio;

    $respuesta = wp_remote_post($url, array(
        'timeout' => 12,
        'headers' => array(
            'Authorization' => 'Bearer ' . ZISA_RESERVAS_TOKEN,
            'Content-Type'  => 'application/x-www-form-urlencoded; charset=utf-8',
        ),
        'body' => $cuerpo_envio,
    ));

    if (is_wp_error($respuesta)) {
        error_log('[zisa-reservas] Sin respuesta del sistema: ' . $respuesta->get_error_message());
        return; // Mejor recibirla por correo/WhatsApp que perderla.
    }

    $codigo = (int) wp_remote_retrieve_response_code($respuesta);
    $cuerpo = json_decode(wp_remote_retrieve_body($respuesta), true);
    $mensaje = is_array($cuerpo) && !empty($cuerpo['message']) ? $cuerpo['message'] : '';

    if ($codigo >= 200 && $codigo < 300) {
        $GLOBALS['zisa_reservas_ok'] = $mensaje;
        return;
    }

    if ($codigo === 400 || $codigo === 409) {
        // Regla de negocio o dato mal: se para el envío y se explica.
        $abort = true;
        $submission->set_response($mensaje ?: 'No pudimos registrar tu reserva. Revisa los datos.');
        return;
    }

    // 401, 5xx...: fallo nuestro, no del cliente. Sigue como siempre.
    error_log('[zisa-reservas] Respuesta ' . $codigo . ': ' . wp_remote_retrieve_body($respuesta));
}

/**
 * Solo para las llamadas al sistema: salir por IPv4. Desde el hosting, por
 * IPv6 y con su cURL 7.61, el cuerpo de la petición llegaba vacío al pasar
 * por Cloudflare (delante de Render); la misma petición por IPv4, desde otra
 * red, llegaba completa.
 */
add_action('http_api_curl', function ($handle, $args, $url) {
    if (defined('ZISA_RESERVAS_URL') && strpos($url, ZISA_RESERVAS_URL) === 0) {
        curl_setopt($handle, CURLOPT_IPRESOLVE, CURL_IPRESOLVE_V4);
    }
}, 10, 3);

/** En el éxito, el mensaje del formulario muestra el código de la reserva. */
add_filter('wpcf7_feedback_response', function ($respuesta, $resultado) {
    if (!empty($GLOBALS['zisa_reservas_ok'])
        && isset($respuesta['status']) && $respuesta['status'] === 'mail_sent') {
        $respuesta['message'] = $GLOBALS['zisa_reservas_ok'];
    }
    return $respuesta;
}, 10, 2);

