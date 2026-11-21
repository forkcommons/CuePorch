<?php
add_action('init', 'cueporch_woo_register_post_video_block');

function cueporch_woo_register_post_video_block(): void {
    if (!wp_script_is('cueporch-editor-picker', 'registered')) {
        wp_register_script('cueporch-editor-picker', plugins_url('editor-picker.js', __FILE__), [], filemtime(__DIR__ . '/editor-picker.js'), true);
    }
    wp_register_script('cueporch-post-video-block', plugins_url('post-video-block.js', __FILE__), ['cueporch-editor-picker', 'wp-blocks', 'wp-element', 'wp-block-editor', 'wp-i18n'], filemtime(__DIR__ . '/post-video-block.js'), true);
    wp_add_inline_script('cueporch-post-video-block', 'window.cueporchPostSettings=' . wp_json_encode(['origin_wp' => get_option('cueporch_woo_origin', '')], JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT) . ';', 'before');
    register_block_type('cueporch/video', [
        'api_version' => 3,
        'editor_script' => 'cueporch-post-video-block',
        'attributes' => ['url' => ['type' => 'string', 'default' => '']],
        'render_callback' => 'cueporch_woo_render_post_video_block',
    ]);
}

function cueporch_woo_render_post_video_block(array $attributes): string {
    $url = isset($attributes['url']) && is_string($attributes['url']) ? $attributes['url'] : '';
    $canonical = cueporch_woo_embed_url($url, (string) get_option('cueporch_woo_origin', ''));
    if (!is_string($canonical) || $canonical === '') {
        return '';
    }
    return '<div ' . get_block_wrapper_attributes() . '>' . cueporch_woo_render_player($canonical, 1) . '</div>';
}
