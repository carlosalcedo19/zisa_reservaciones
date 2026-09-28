<?php
/**
 * Plugin Name: Zisa – Reservas conectadas
 * Description: Envía el formulario «Reserva una mesa» (Contact Form 7) al sistema de reservas de Zisa. Si no hay mesa, el cliente ve el motivo en el formulario.
 * Version:     1.0.0
 * Author:      Zisa
 *
 * Requiere ZISA_RESERVAS_URL y ZISA_RESERVAS_TOKEN en wp-config.php; sin ellas no hace nada.
 */

if (!defined('ABSPATH')) {
    exit;
}

if (!defined('ZISA_RESERVAS_FORM_ID')) {
    define('ZISA_RESERVAS_FORM_ID', 24585);
}

$GLOBALS['zisa_reservas_ok'] = null;

add_action('wpcf7_before_send_mail', 'zisaReservasEnviar', 10, 3);

function zisaReservasEnviar($contact_form, &$abort, $submission = null)
{
    $submission = zisaReservasSubmission($contact_form, $submission);
    if (!$submission) {
        return;
    }

    $respuesta = zisaReservasPost(zisaReservasCampos($submission));

    if (is_wp_error($respuesta)) {
        error_log('[zisa-reservas] Sin respuesta del sistema: ' . $respuesta->get_error_message());
        return; // Mejor recibirla por correo/WhatsApp que perderla.
    }

    zisaReservasProcesarRespuesta($respuesta, $submission, $abort);
}

function zisaReservasSubmission($contact_form, $submission)
{
    if ((int) $contact_form->id() !== (int) ZISA_RESERVAS_FORM_ID
        || !defined('ZISA_RESERVAS_URL') || !defined('ZISA_RESERVAS_TOKEN')) {
        return null;
    }
    return $submission ?: WPCF7_Submission::get_instance();
}

function zisaReservasCampos($submission)
{
    $datos = $submission->get_posted_data();
    $campo = function ($nombre) use ($datos) {
        $valor = isset($datos[$nombre]) ? $datos[$nombre] : '';
        if (is_array($valor)) {          // los <select> llegan como lista
            $valor = reset($valor);
        }
        return trim((string) $valor);
    };

    return array(
        'full-name'        => $campo('full-name'),
        'your-phone'       => $campo('your-phone'),
        'your-email'       => $campo('your-email'),
        'num-person'       => $campo('num-person'),
        'date-reservation' => $campo('date-reservation'),
        'time-field'       => $campo('time-field'),
        'indications'      => $campo('indications'),
    );
}

function zisaReservasPost($campos)
{
    $cuerpo_envio = http_build_query($campos, '', '&');

    // También en la URL: desde este hosting el cuerpo llega vacío a Cloudflare.
    $url = ZISA_RESERVAS_URL . (strpos(ZISA_RESERVAS_URL, '?') === false ? '?' : '&') . $cuerpo_envio;

    return wp_remote_post($url, array(
        'timeout' => 12,
        'headers' => array(
            'Authorization' => 'Bearer ' . ZISA_RESERVAS_TOKEN,
            'Content-Type'  => 'application/x-www-form-urlencoded; charset=utf-8',
        ),
        'body' => $cuerpo_envio,
    ));
}

function zisaReservasProcesarRespuesta($respuesta, $submission, &$abort)
{
    $codigo = (int) wp_remote_retrieve_response_code($respuesta);
    $cuerpo = json_decode(wp_remote_retrieve_body($respuesta), true);
    $mensaje = is_array($cuerpo) && !empty($cuerpo['message']) ? $cuerpo['message'] : '';

    if ($codigo >= 200 && $codigo < 300) {
        $GLOBALS['zisa_reservas_ok'] = $mensaje;
    } elseif ($codigo === 400 || $codigo === 409) {
        $abort = true;
        $submission->set_response($mensaje ?: 'No pudimos registrar tu reserva. Revisa los datos.');
    } else {
        // Fallo nuestro, no del cliente: no se bloquea el formulario.
        error_log('[zisa-reservas] Respuesta ' . $codigo . ': ' . wp_remote_retrieve_body($respuesta));
    }
}

// IPv4: por IPv6 (cURL 7.61 del hosting) el cuerpo llegaba vacío a Cloudflare.
add_action('http_api_curl', function ($handle) {
    $url = func_get_arg(2);
    if (defined('ZISA_RESERVAS_URL') && strpos($url, ZISA_RESERVAS_URL) === 0) {
        curl_setopt($handle, CURLOPT_IPRESOLVE, CURL_IPRESOLVE_V4);
    }
}, 10, 3);

add_filter('wpcf7_feedback_response', function ($respuesta) {
    if (!empty($GLOBALS['zisa_reservas_ok'])
        && isset($respuesta['status']) && $respuesta['status'] === 'mail_sent') {
        $respuesta['message'] = $GLOBALS['zisa_reservas_ok'];
    }
    return $respuesta;
}, 10, 1);

