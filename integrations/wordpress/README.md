# CuePorch Video for WooCommerce

Optional WooCommerce host adapter for editable product video URLs, with a default **Videos** tab or placement before/after classic product gallery images.

## Install and Configure

1. Copy the `cueporch-video` folder under `wp-content/plugins`.
2. In WordPress, go to **Plugins** and activate **CuePorch Video**.
3. Go to **Settings > CuePorch Video** and set the CuePorch origin. Use HTTPS. Loopback HTTP is available for local tests.
4. Edit a product: **WooCommerce > Products > Edit > Product data > General**.
5. Paste up to ten CuePorch watch or embed URLs, one per line.
6. Choose placement: **Videos** tab default, **before** classic gallery images, or **after** classic gallery images.
7. Select **Update**, then preview the product as a guest.

The storefront loads the player from CuePorch. CuePorch applies its existing domain, password and access rules. Treat any share key in a product video URL as visible to shoppers.

Existing product images are retained.

## Compatibility

Use the classic WooCommerce product editor and a theme that uses the classic product gallery. Block gallery placement and an in-admin CuePorch media picker are pending. The product-description iframe path is also available.

The real-host fixture uses WordPress 7.0, WooCommerce 11.2.0, PHP 8.3 and Storefront 4.6.0. On a disposable local WordPress site with CuePorch Video and WooCommerce active, set `CUEPORCH_WP_LOAD` to the site's `wp-load.php`, set `CUEPORCH_WOO_DISPOSABLE=1`, and run `php acceptance/test_woocommerce_host.php`.
