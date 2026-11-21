<?php
if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

add_action( 'woocommerce_product_options_general_product_data', 'cueporch_woo_render_product_video_fields' );

function cueporch_woo_render_product_video_fields() {
    global $post;

    $product_id = ( $post instanceof WP_Post ) ? absint( $post->ID ) : 0;
    $urls       = $product_id ? get_post_meta( $product_id, '_cueporch_video_urls', true ) : array();
    $urls       = is_array( $urls ) ? array_values( array_filter( array_map( 'strval', $urls ), 'strlen' ) ) : array();
    $placement  = $product_id ? get_post_meta( $product_id, '_cueporch_video_placement', true ) : 'tab';

    if ( ! in_array( $placement, array( 'tab', 'before', 'after' ), true ) ) {
        $placement = 'tab';
    }

    woocommerce_wp_textarea_input(
        array(
            'id'                => '_cueporch_video_urls',
            'label'             => esc_html__( 'CuePorch video URLs', 'cueporch-video' ),
            'description'       => esc_html__( 'Up to 10 CuePorch video URLs, one per line.', 'cueporch-video' ),
            'desc_tip'          => true,
            'value'             => implode( "\n", $urls ),
            'rows'              => 6,
            'custom_attributes' => array( 'maxlength' => '10240' ),
        )
    );

    if ( get_option( 'cueporch_woo_origin' ) ) {
        echo '<p class="form-field">';
        echo '<button type="button" class="button" id="cueporchChooseVideo" data-origin="' . esc_attr( get_option( 'cueporch_woo_origin' ) ) . '">' . esc_html__( 'Choose from CuePorch', 'cueporch-video' ) . '</button>';
        if ( current_user_can( 'edit_post', $product_id ) ) {
            $source = wc_get_product( $product_id );
            if ( $source instanceof WC_Product ) {
                $photos = cueporch_woo_product_photo_candidates( $source );
                if ( is_array( $photos ) && ! empty( $photos ) ) {
                    $photos_json = wp_json_encode( $photos );
                    if ( is_string( $photos_json ) && '' !== $photos_json ) {
                        echo '<button'
                            . ' type="button"'
                            . ' id="cueporchCreateProductVideo"'
                            . ' class="button"'
                            . ' data-title="' . esc_attr( $source->get_name() ) . '"'
                            . ' data-photos="' . esc_attr( $photos_json ) . '"'
                            . '>'
                            . esc_html__( 'Create Product Video', 'cueporch-video' )
                            . '</button>';
                    }
                }
            }
        }
        echo ' <span id="cueporchPickerStatus" aria-live="polite"></span>';
        echo '</p>';
    }

    woocommerce_wp_select(
        array(
            'id'      => '_cueporch_video_placement',
            'label'   => esc_html__( 'CuePorch video placement', 'cueporch-video' ),
            'options' => array(
                'tab'    => esc_html__( 'Product tab', 'cueporch-video' ),
                'before' => esc_html__( 'Before gallery images', 'cueporch-video' ),
                'after'  => esc_html__( 'After gallery images', 'cueporch-video' ),
            ),
            'value'   => $placement,
        )
    );

    wp_nonce_field( 'cueporch_woo_save_product_video', 'cueporch_woo_product_video_nonce' );
}

add_action( 'woocommerce_admin_process_product_object', 'cueporch_woo_save_product_video_fields' );

function cueporch_woo_save_product_video_fields( $product ) {
    if ( ! $product instanceof WC_Product ) {
        return;
    }

    if ( defined( 'DOING_AUTOSAVE' ) && DOING_AUTOSAVE ) {
        return;
    }

    $product_id = $product->get_id();

    if ( wp_is_post_revision( $product_id ) || wp_is_post_autosave( $product_id ) ) {
        return;
    }

    if ( ! current_user_can( 'edit_post', $product_id ) ) {
        return;
    }

    if ( ! isset( $_POST['cueporch_woo_product_video_nonce'] ) || ! is_string( $_POST['cueporch_woo_product_video_nonce'] ) ) {
        return;
    }

    $nonce = sanitize_text_field( wp_unslash( $_POST['cueporch_woo_product_video_nonce'] ) );

    if ( ! wp_verify_nonce( $nonce, 'cueporch_woo_save_product_video' ) ) {
        return;
    }

    if ( ! isset( $_POST['_cueporch_video_urls'] ) || ! is_string( $_POST['_cueporch_video_urls'] ) ) {
        return;
    }

    $raw_urls = wp_unslash( $_POST['_cueporch_video_urls'] );
    $length   = function_exists( 'mb_strlen' ) ? mb_strlen( $raw_urls, 'UTF-8' ) : strlen( $raw_urls );

    if ( $length > 10240 ) {
        if ( class_exists( 'WC_Admin_Meta_Boxes' ) ) {
            WC_Admin_Meta_Boxes::add_error( esc_html__( 'The CuePorch video URL list is too long. The previous video configuration was kept.', 'cueporch-video' ) );
        }
        return;
    }

    $allowed_origin = (string) get_option( 'cueporch_woo_origin', '' );
    $lines          = preg_split( '/\r\n|\r|\n/', $raw_urls );
    $urls           = array();
    $seen           = array();

    foreach ( $lines as $line ) {
        $line = trim( $line );

        if ( '' === $line ) {
            continue;
        }

        $safe_url = cueporch_woo_embed_url( $line, $allowed_origin );

        if ( '' === $safe_url ) {
            if ( class_exists( 'WC_Admin_Meta_Boxes' ) ) {
                WC_Admin_Meta_Boxes::add_error( esc_html__( 'One or more CuePorch video URLs are invalid. The previous video configuration was kept.', 'cueporch-video' ) );
            }
            return;
        }

        if ( ! isset( $seen[ $safe_url ] ) ) {
            $seen[ $safe_url ] = true;
            $urls[]            = $safe_url;
        }
    }

    if ( count( $urls ) > 10 ) {
        if ( class_exists( 'WC_Admin_Meta_Boxes' ) ) {
            WC_Admin_Meta_Boxes::add_error( esc_html__( 'Up to 10 unique CuePorch video URLs are allowed. The previous video configuration was kept.', 'cueporch-video' ) );
        }
        return;
    }

    $placement_raw = isset( $_POST['_cueporch_video_placement'] ) && is_string( $_POST['_cueporch_video_placement'] )
        ? wp_unslash( $_POST['_cueporch_video_placement'] )
        : '';
    $placement     = in_array( $placement_raw, array( 'tab', 'before', 'after' ), true ) ? $placement_raw : 'tab';

    if ( empty( $urls ) ) {
        $product->delete_meta_data( '_cueporch_video_urls' );
    } else {
        $product->update_meta_data( '_cueporch_video_urls', array_values( $urls ) );
    }

    $product->update_meta_data( '_cueporch_video_placement', $placement );
}

add_action( 'admin_enqueue_scripts', 'cueporch_woo_enqueue_editor_picker' );
function cueporch_woo_enqueue_editor_picker( $hook ) {
    if ( 'post.php' !== $hook && 'post-new.php' !== $hook ) {
        return;
    }
    $screen = get_current_screen();
    if ( ! $screen || 'product' !== $screen->post_type ) {
        return;
    }
    if ( ! get_option( 'cueporch_woo_origin' ) ) {
        return;
    }
    wp_enqueue_script( 'cueporch-editor-picker' );
}

function cueporch_woo_product_photo_candidates(WC_Product $product): array {
    if (!is_admin()) return [];
    $ids = array_filter(array_merge([(int) $product->get_image_id()], array_map('intval', $product->get_gallery_image_ids())), fn($i) => $i > 0);
    $ids = array_slice(array_values(array_unique($ids)), 0, 8);
    $ok = ['image/jpeg', 'image/png', 'image/webp'];
    $out = [];
    foreach ($ids as $id) {
        $mime = get_post_mime_type($id);
        $src  = wp_get_attachment_image_src($id, 'large');
        if (!$mime || !in_array($mime, $ok, true) || !$src || empty($src[0])) continue;
        $path = wp_parse_url($src[0], PHP_URL_PATH);
        $name = $path ? sanitize_file_name(basename($path)) : '';
        if ($name === '') continue;
        $out[] = ['name' => $name, 'mime' => $mime, 'url' => esc_url_raw($src[0])];
    }
    return $out;
}
