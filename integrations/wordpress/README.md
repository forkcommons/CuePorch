# CuePorch Video for WooCommerce

Optional WooCommerce host adapter for editable product video URLs, with a default **Videos** tab or placement before/after classic product gallery images.

## Install and Configure

1. Copy the `cueporch-video` folder under `wp-content/plugins`.
2. In WordPress, go to **Plugins** and activate **CuePorch Video**.
3. Go to **Settings > CuePorch Video** and set the CuePorch origin. Use HTTPS. Loopback HTTP is available for local tests.
4. Edit a product: **WooCommerce > Products > Edit > Product data > General**.
5. Select **Choose from CuePorch** and search your Library in the CuePorch popup. Choose a video, then return to the product editor. You can also paste up to ten CuePorch watch or embed URLs, one per line.
6. Choose placement: **Videos** tab default, **before** classic gallery images, or **after** classic gallery images.
7. Select **Update**, then preview the product as a guest.

The storefront loads the player from CuePorch. CuePorch applies its existing domain, password and access rules. Treat any share key in a product video URL as visible to shoppers.

Existing product images are retained.

The picker shows which WordPress site will receive the selected player URL. If CuePorch asks for an operator credential, enter it in the CuePorch popup. Private Library items are excluded, and CuePorch checks the current sharing settings for the selected video. The picker adds the URL to the form. Select **Update** to save it.

Select **Upload** in the CuePorch popup to add files to your Library. It uses the same upload defaults, folder settings, and approval policy as the CuePorch Library. A private or pending upload stays in CuePorch until its settings allow sharing. Uploading a file does not add it to the product.

For a saved product with WordPress-hosted images, select **Create Product Video**, then **Create from product photos** in the CuePorch popup. CuePorch uses the saved title and up to eight featured and gallery images to make a short video. **Open draft in CuePorch editor** opens the private result. After its sharing settings permit an embed, choose it from the Library and update the product.

## Compatibility

Use the classic WooCommerce product editor for product settings. On classic storefronts, gallery placement adds a video slide. On storefronts using the WooCommerce Product Gallery block, the hosted player appears in its own section immediately before or after the native gallery. The product-description iframe path is also available.

The real-host fixture uses WordPress 7.0, WooCommerce 11.2.0, PHP 8.3 and Storefront 4.6.0. On a disposable local WordPress site with CuePorch Video and WooCommerce active, set `CUEPORCH_WP_LOAD` to the site's `wp-load.php`, set `CUEPORCH_WOO_DISPOSABLE=1`, and run `php acceptance/test_woocommerce_host.php`.

For a source browser rehearsal, use a disposable loopback store with a published test product that has a gallery image. Set WooCommerce site visibility to **Live** so a logged-out shopper can see the product. Set `CUEPORCH_WOO_DISPOSABLE=1` and supply `CUEPORCH_WOO_STOREFRONT` (the loopback origin), `CUEPORCH_WOO_PRODUCT_ID`, `CUEPORCH_WOO_USERNAME` and `CUEPORCH_WOO_PASSWORD` as test environment variables. With FFmpeg, the Python acceptance dependencies and a headless Chromium or Edge available, run `python -m unittest discover -s acceptance -p test_woocommerce_playback.py -v` from the repository root. The test changes the fixture's saved player origin and product settings and tests decoded playback and revocation.

For the block-gallery rehearsal, use a disposable WordPress block theme with the WooCommerce Product Gallery block in its Single Product template. A minimal template is provided at `acceptance/woocommerce_block_single_product.html`. Set `CUEPORCH_WOO_BLOCK_GALLERY=1` when running the browser rehearsal against that theme.
