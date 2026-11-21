# CuePorch modules for HubSpot

**CuePorch video** embeds the player on landing pages, site pages, and blog posts. **Watch on CuePorch** adds a text link or an optional poster to a marketing email. Choose the video from Media Bridge in both modules. For email, select the published HTTPS landing page that plays the video.

## Set up the modules

Connect HubSpot and publish eligible CuePorch videos. The app needs a registered Video object and a CuePorch oEmbed domain. [CuePorch setup](../../USER-MANUAL.md) · [HubSpot Media Bridge guide](https://developers.hubspot.com/docs/api-reference/latest/cms/media-bridge/guide)

With the HubSpot CLI connected to your CMS account, upload the two module directories from the CuePorch source checkout.

```powershell
hs cms upload integrations/hubspot/cueporch-video.module cueporch-media/cueporch-video.module
hs cms upload integrations/hubspot/cueporch-email.module cueporch-media/cueporch-email.module
```

Add the appropriate module in HubSpot's page or email editor. In its video field, select **Media integration** and choose the CuePorch content. [HubSpot module examples](https://github.com/HubSpot/cms-media-bridge) · [HubSpot CMS CLI commands](https://developers.hubspot.com/changelog/october-2025-developer-rollup)

## Share named playback with HubSpot

Set `CUEPORCH_HUBSPOT_CMS_ORIGINS` to the exact HTTPS origins of your published HubSpot pages, separated by commas. Include the scheme and host. Leave the setting empty to disable named playback. Enable HubSpot consent on the publisher page. After the viewer grants analytics consent and selects **Share my playback with this publisher in HubSpot**, CuePorch can send a play and 0%, 25%, 50%, 75%, and 100% progress events for the eligible video. CuePorch honors browser privacy signals. Turning sharing off cancels pending sends for that view.

Delivery is automatic by default. Turn on **Event engagement sync** approval in CuePorch to hold events for operator review.

## Verification

A simulated HubSpot field and template context rendered both source modules. A headless browser played the page module on a separate origin and exercised the email link and poster markup. CuePorch still checks playback permissions at its own origin. HubSpot editor import and preview await a configured tenant.
