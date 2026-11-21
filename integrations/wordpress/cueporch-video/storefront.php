<?php
if (!defined('ABSPATH')) {
    exit;
}

function cueporch_woo_product_video_urls($product): array {
    if (!($product instanceof WC_Product)) return [];
    $raw = $product->get_meta('_cueporch_video_urls');
    if (!is_array($raw) || !function_exists('cueporch_woo_embed_url')) return [];
    $origin = (string) get_option('cueporch_woo_origin', '');
    $urls = [];
    foreach ($raw as $entry) {
        if (!is_string($entry) || $entry === '') continue;
        $valid = cueporch_woo_embed_url($entry, $origin);
        if (!is_string($valid) || $valid === '') continue;
        if (in_array($valid, $urls, true)) continue;
        $urls[] = $valid;
        if (count($urls) >= 10) break;
    }
    return $urls;
}

function cueporch_woo_render_player(string $url, int $position): string {
    $src = esc_url($url);
    if ($src === '') return '';
    $label = sprintf(
        /* translators: %d: numbered position of the video player */
        __('Video %d', 'cueporch-video'),
        $position
    );
    if (trim($label) === '') $label = 'Video ' . $position;
    return '<div class="cueporch-video" style="position:relative;width:100%;padding-top:56.25%;">'
        . '<iframe src="' . $src . '" title="' . esc_attr($label) . '" loading="lazy"'
        . ' referrerpolicy="origin" allow="autoplay; fullscreen; picture-in-picture"'
        . ' allowfullscreen style="position:absolute;top:0;left:0;width:100%;height:100%;border:0;">'
        . '</iframe></div>';
}

add_filter('woocommerce_product_tabs', function ($tabs) {
    if (!is_product()) return $tabs;
    global $product;
    if (!($product instanceof WC_Product)) return $tabs;
    $placement = (string) $product->get_meta('_cueporch_video_placement');
    if ($placement === '') $placement = 'tab';
    if ($placement !== 'tab') return $tabs;
    $urls = cueporch_woo_product_video_urls($product);
    if (empty($urls)) return $tabs;
    $tabs['cueporch_videos'] = [
        'title' => __('Videos', 'cueporch-video'),
        'priority' => 50,
        'callback' => function () use ($urls) {
            echo '<h2>' . esc_html__('Videos', 'cueporch-video') . '</h2>';
            $i = 0;
            foreach ($urls as $url) {
                $i++;
                echo cueporch_woo_render_player($url, $i);
            }
        },
    ];
    return $tabs;
}, 20);

function cueporch_woo_gallery_slides($product) {
    $urls = cueporch_woo_product_video_urls($product);
    if (empty($urls)) {
        return '';
    }

    $thumb = esc_url(wc_placeholder_img_src('gallery_thumbnail'));
    $out   = '';
    $n     = 0;

    foreach ($urls as $url) {
        $n++;
        $alt = sprintf(__('Video slide %d', 'cueporch-video'), $n);

        $out .= '<div class="woocommerce-product-gallery__image cueporch-video-slide" data-thumb="' . $thumb . '" data-thumb-alt="' . esc_attr($alt) . '">';
        $out .= '<img src="' . $thumb . '" data-large_image="' . $thumb . '" data-large_image_width="100" data-large_image_height="100" style="display:none" alt="" />';
        $out .= cueporch_woo_render_player($url, $n);
        $out .= '</div>';
    }

    return $out;
}

function cueporch_woo_video_before_thumbnail($html, $attachment_id) {
    if (!is_product() || doing_action('woocommerce_product_thumbnails')) {
        return $html;
    }

    global $product;
    if (!($product instanceof WC_Product)) {
        return $html;
    }

    if ('before' !== $product->get_meta('_cueporch_video_placement')) {
        return $html;
    }

    $featured = (int) $product->get_image_id();
    $attachment_id = (int) $attachment_id;

    if ($attachment_id !== $featured && 0 !== $attachment_id) {
        return $html;
    }

    return cueporch_woo_gallery_slides($product) . $html;
}
add_filter('woocommerce_single_product_image_thumbnail_html', 'cueporch_woo_video_before_thumbnail', 12, 2);

function cueporch_woo_video_after_thumbnails() {
    if (!is_product()) {
        return;
    }

    global $product;
    if (!($product instanceof WC_Product)) {
        return;
    }

    if ('after' !== $product->get_meta('_cueporch_video_placement')) {
        return;
    }

    echo cueporch_woo_gallery_slides($product);
}
add_action('woocommerce_product_thumbnails', 'cueporch_woo_video_after_thumbnails', 99);
