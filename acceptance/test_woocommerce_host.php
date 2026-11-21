<?php
if (PHP_SAPI !== 'cli') { throw new RuntimeException('CLI only'); }
$wpLoad = getenv('CUEPORCH_WP_LOAD');
if (!$wpLoad || !is_file($wpLoad)) { throw new RuntimeException('CUEPORCH_WP_LOAD must point to existing wp-load.php'); }
if (getenv('CUEPORCH_WOO_DISPOSABLE') !== '1') { throw new RuntimeException('CUEPORCH_WOO_DISPOSABLE=1 required'); }
require_once $wpLoad;
$host = wp_parse_url(home_url('/'), PHP_URL_HOST);
if (!in_array($host, ['127.0.0.1', 'localhost', '[::1]'], true)) { throw new RuntimeException('Disposable host check failed'); }

function cueporch_test_assert($condition, $message) {
    if (!$condition) {
        throw new Exception('FAIL: ' . $message);
    }
}

function cueporch_test_urls($product): array {
    $urls = $product->get_meta('_cueporch_video_urls');
    if (is_array($urls)) {
        return $urls;
    }
    if ($urls === '') {
        return [];
    }
    throw new RuntimeException('Invalid _cueporch_video_urls meta');
}

function cueporch_test_save($product_id, $urls, $placement, $with_nonce = true) {
    if (is_array($urls)) {
        $urls = implode("\n", $urls);
    }

    $product = wc_get_product($product_id);
    $_POST = [
        '_cueporch_video_urls'      => (string) $urls,
        '_cueporch_video_placement' => (string) $placement,
    ];

    if ($with_nonce) {
        $_POST['cueporch_woo_product_video_nonce'] = wp_create_nonce('cueporch_woo_save_product_video');
    }

    do_action('woocommerce_admin_process_product_object', $product);
    $product->save();
    $_POST = [];
}

cueporch_test_assert(defined('WC_VERSION'), 'WooCommerce not active: WC_VERSION undefined');
cueporch_test_assert(function_exists('wc_get_product'), 'wc_get_product function missing');
cueporch_test_assert(function_exists('cueporch_woo_register_settings'), 'cueporch_woo_register_settings function missing');

$test_admin_id = 0;
if (function_exists('wp_set_current_user')) {
    $admins = get_users(['role' => 'administrator', 'number' => 1]);
    if (!empty($admins)) {
        $test_admin_id = (int) $admins[0]->ID;
        wp_set_current_user($test_admin_id);
    }
}

$trusted_origin = 'https://video.example.test';
$wrong_origin   = 'https://evil.example.test/not-an-origin';

require_once ABSPATH . 'wp-admin/includes/template.php';
cueporch_woo_register_settings();
update_option('cueporch_woo_origin', $trusted_origin);
cueporch_test_assert(get_option('cueporch_woo_origin') === $trusted_origin, 'trusted origin setting not saved');

update_option('cueporch_woo_origin', $wrong_origin);
cueporch_test_assert(get_option('cueporch_woo_origin') === $trusted_origin, 'invalid origin setting should retain trusted origin');

$u1 = '123e4567-e89b-12d3-a456-426614174000';
$u2 = '223e4567-e89b-12d3-a456-426614174001';

$good             = 'https://video.example.test/embed/work/' . $u1;
$watch            = 'https://video.example.test/watch/work/' . $u2;
$normalized_watch = 'https://video.example.test/embed/work/' . $u2;
$bad_origin       = 'https://evil.example.test/embed/work/' . $u1;

$product = new WC_Product_Simple();
$product->set_name('Cueporch Fixture Product');
$product->set_status('publish');
$product_id = $product->save();
cueporch_test_assert($product_id > 0, 'Failed to create WC product');

$fixture_post = get_post($product_id);

$wp_query = new WP_Query();
$wp_query->is_singular        = true;
$wp_query->queried_object     = $fixture_post;
$wp_query->queried_object_id  = $product_id;
$wp_query->post               = $fixture_post;
$GLOBALS['wp_query']          = $wp_query;
$GLOBALS['post']              = $fixture_post;

// 1) Dedup: two identical valid inputs are posted as a newline-delimited string.
cueporch_test_save($product_id, $good . "\n" . $good, 'tab');
$product = wc_get_product($product_id);
$urls    = cueporch_test_urls($product);
cueporch_test_assert($urls === [$good], 'dedup failed: ' . json_encode($urls));
cueporch_test_assert($product->get_meta('_cueporch_video_placement') === 'tab', 'placement not persisted');

// 2) Wrong-origin product input must preserve prior values.
cueporch_test_save($product_id, $bad_origin, 'tab');
$urls = cueporch_test_urls(wc_get_product($product_id));
cueporch_test_assert($urls === [$good], 'wrong-origin product input should preserve prior: ' . json_encode($urls));

// 3) Missing nonce must preserve prior values.
cueporch_test_save($product_id, $watch, 'none', false);
$urls = cueporch_test_urls(wc_get_product($product_id));
cueporch_test_assert($urls === [$good], 'missing nonce should preserve prior: ' . json_encode($urls));

// 4) Invalid list must preserve prior values.
$invalid_list = "not-a-url\nhttps://evil.example.test/embed/work/$u1";
cueporch_test_save($product_id, $invalid_list, 'tab');
$urls = cueporch_test_urls(wc_get_product($product_id));
cueporch_test_assert($urls === [$good], 'invalid list should preserve prior: ' . json_encode($urls));

// 5) Valid save/readback and watch URL normalization.
cueporch_test_save($product_id, $watch, 'tab');
$product = wc_get_product($product_id);
$urls    = cueporch_test_urls($product);
cueporch_test_assert($urls === [$normalized_watch], 'watch URL not normalized: ' . json_encode($urls));

// 6) Acceptance: guest can render the tab markup
wp_set_current_user(0);
$GLOBALS['product'] = wc_get_product($product_id);
$tabs = apply_filters('woocommerce_product_tabs', []);
cueporch_test_assert(isset($tabs['cueporch_videos']), 'missing cueporch tab');
ob_start();
call_user_func($tabs['cueporch_videos']['callback']);
$tab_html = ob_get_clean();
cueporch_test_assert(strpos($tab_html, '<iframe') !== false, 'iframe missing');
cueporch_test_assert(strpos($tab_html, $normalized_watch) !== false, 'watch url mismatch');
wp_set_current_user($test_admin_id);
wp_set_current_user($test_admin_id);

require_once ABSPATH . 'wp-admin/includes/image.php';
$cueporch_png = base64_decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nWQAAAAASUVORK5CYII=');
$cueporch_upload = wp_upload_bits('cueporch-fixture.png', null, $cueporch_png);
cueporch_test_assert(!empty($cueporch_upload['file']) && empty($cueporch_upload['error']), 'fixture PNG upload failed');

$cueporch_attachment_id = wp_insert_attachment([
    'post_mime_type' => 'image/png',
    'post_title'     => 'Cueporch fixture image',
    'post_status'    => 'inherit',
], $cueporch_upload['file'], $product_id);
cueporch_test_assert(!is_wp_error($cueporch_attachment_id) && $cueporch_attachment_id > 0, 'fixture attachment insert failed');

$cueporch_meta = wp_generate_attachment_metadata($cueporch_attachment_id, $cueporch_upload['file']);
wp_update_attachment_metadata($cueporch_attachment_id, $cueporch_meta);
$cueporch_product = wc_get_product($product_id);
$cueporch_product->set_image_id($cueporch_attachment_id);
$cueporch_product->save();
$GLOBALS['product'] = wc_get_product($product_id);

foreach (['before', 'after'] as $cueporch_placement) {
    cueporch_test_save($product_id, $normalized_watch, $cueporch_placement, true);
    $cueporch_fresh = wc_get_product($product_id);
    cueporch_test_assert($cueporch_fresh && $cueporch_fresh->get_id() === $product_id, 'fresh product read failed');
    $GLOBALS['product'] = $cueporch_fresh;
    $GLOBALS['post'] = get_post($product_id);
    setup_postdata($GLOBALS['post']);
    ob_start();
    wc_get_template('single-product/product-image.php');
    $cueporch_html = ob_get_clean();
    $cueporch_slide_pos = strpos($cueporch_html, 'cueporch-video-slide');
    $cueporch_image_pos = strpos($cueporch_html, 'wp-post-image');
    cueporch_test_assert($cueporch_slide_pos !== false, "$cueporch_placement: video slide missing in Woo image template");
    cueporch_test_assert($cueporch_image_pos !== false, "$cueporch_placement: native product image missing");
    if ($cueporch_placement === 'before') {
        cueporch_test_assert($cueporch_slide_pos < $cueporch_image_pos, 'before: video slide is not before native image');
    } else {
        cueporch_test_assert($cueporch_slide_pos > $cueporch_image_pos, 'after: video slide is not after native image');
    }
    cueporch_test_assert(substr_count($cueporch_html, $normalized_watch) === 1, "$cueporch_placement: normalized embed URL count is not 1");
    cueporch_test_assert(!array_key_exists('cueporch_videos', apply_filters('woocommerce_product_tabs', [])), "cueporch_videos tab unexpectedly present for placement: {$cueporch_placement}");
}
wp_reset_postdata();

cueporch_test_save($product_id, '', 'tab');
clean_post_cache($product_id);
$fresh = wc_get_product($product_id);
cueporch_test_assert(cueporch_test_urls($fresh) === [], 'urls not cleared');
$GLOBALS['product'] = wc_get_product($product_id);
$tabs2 = apply_filters('woocommerce_product_tabs', []);
cueporch_test_assert(!isset($tabs2['cueporch_videos']), 'cueporch tab still present');
echo 'CuePorch WooCommerce tab and gallery fixture passed';
