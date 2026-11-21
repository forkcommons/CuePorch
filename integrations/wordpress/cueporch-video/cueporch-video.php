<?php
/**
 * Plugin Name:       CuePorch Video
 * Plugin URI:        https://github.com/ForkCommons/CuePorch
 * Description:       Thin WordPress adapter for CuePorch video embeds. Configures the allowed CuePorch base origin used by embed URL validation.
 * Version:           0.3.0
 * Requires at least: 6.9
 * Requires PHP:      8.3
 * Author:            ForkCommons
 * License:           AGPL-3.0-or-later
 * License URI:       https://www.gnu.org/licenses/agpl-3.0.html
 * Text Domain:       cueporch-video
 */
defined( 'ABSPATH' ) || exit;
require_once __DIR__ . '/embed-url.php';
require_once __DIR__ . '/product-video.php';
require_once __DIR__ . '/storefront.php';
function cueporch_woo_register_settings_page(): void {
	add_options_page(
		__( 'CuePorch Video', 'cueporch-video' ),
		__( 'CuePorch Video', 'cueporch-video' ),
		'manage_options',
		'cueporch-video',
		'cueporch_woo_render_settings_page'
	);
}
add_action( 'admin_menu', 'cueporch_woo_register_settings_page' );
function cueporch_woo_register_settings(): void {
	register_setting(
		'cueporch_woo_settings',
		'cueporch_woo_origin',
		array(
			'type'              => 'string',
			'sanitize_callback' => 'cueporch_woo_sanitize_origin',
			'default'           => '',
			'show_in_rest'      => false,
		)
	);
	add_settings_section(
		'cueporch_woo_section',
		__( 'CuePorch Origin', 'cueporch-video' ),
		'cueporch_woo_render_section',
		'cueporch-video'
	);
	add_settings_field(
		'cueporch_woo_origin',
		__( 'Allowed origin', 'cueporch-video' ),
		'cueporch_woo_render_origin_field',
		'cueporch-video',
		'cueporch_woo_section',
		array( 'label_for' => 'cueporch_woo_origin' )
	);
}
add_action( 'admin_init', 'cueporch_woo_register_settings' );
function cueporch_woo_render_section(): void {
	echo '<p>' . esc_html__( 'Enter the CuePorch base origin. Production origins must use HTTPS; literal loopback HTTP is accepted for local evaluation. Leave empty to disable CuePorch embeds.', 'cueporch-video' ) . '</p>';
}
function cueporch_woo_render_origin_field(): void {
	if ( ! current_user_can( 'manage_options' ) ) {
		return;
	}
	$value = (string) get_option( 'cueporch_woo_origin', '' );
	printf(
		'<input type="url" id="cueporch_woo_origin" name="cueporch_woo_origin" value="%s" class="regular-text code" autocomplete="off" spellcheck="false" maxlength="300" />',
		esc_attr( $value )
	);
	echo '<p class="description">' . esc_html__( 'Must be an origin with no credentials, query, fragment, or path other than "/". Embed URLs are accepted only when they match this origin.', 'cueporch-video' ) . '</p>';
}
function cueporch_woo_sanitize_origin( $value ) {
	$previous = (string) get_option( 'cueporch_woo_origin', '' );
	$raw      = is_string( $value ) ? trim( $value ) : null;
	if ( $raw === '' ) {
		return '';
	}
	$invalid = $raw === null || strlen( $raw ) > 300;
	$parts   = $invalid ? null : wp_parse_url( $raw );
	$invalid = $invalid || ! is_array( $parts ) || array_key_exists( 'user', $parts ) || array_key_exists( 'pass', $parts ) || array_key_exists( 'query', $parts ) || array_key_exists( 'fragment', $parts ) || ( isset( $parts['path'] ) && $parts['path'] !== '' && $parts['path'] !== '/' );
	$canonical = '';
	if ( ! $invalid ) {
		$canonical = cueporch_woo_origin( $raw );
		$invalid   = ! is_string( $canonical ) || $canonical === '';
	}
	if ( $invalid ) {
		add_settings_error(
			'cueporch_woo_origin',
			'cueporch_woo_origin_invalid',
			__( 'Enter a valid CuePorch origin. Production origins must use HTTPS; literal loopback HTTP is allowed only for local evaluation.', 'cueporch-video' )
		);
		return $previous;
	}
	return $canonical;
}
function cueporch_woo_render_settings_page(): void {
	if ( ! current_user_can( 'manage_options' ) ) {
		return;
	}
	echo '<div class="wrap"><h1>' . esc_html( get_admin_page_title() ) . '</h1>';
	settings_errors();
	echo '<form action="options.php" method="post">';
	settings_fields( 'cueporch_woo_settings' );
	do_settings_sections( 'cueporch-video' );
	submit_button();
	echo '</form></div>';
}
