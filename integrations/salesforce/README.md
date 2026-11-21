# CuePorch metadata for Salesforce Sales Cloud

The metadata defines Salesforce records for CuePorch media and registrations. A CuePorch Media record links to a Campaign. Each CuePorch Registrant links to that Media record and either a Lead or a Contact. Both custom objects default to private sharing. An administrator can grant record access to the people who manage or report on the campaign.

## Deploy to a Salesforce org

Open this directory in a terminal with the Salesforce CLI and an authorized org alias. Check the package in a sandbox before deploying to your production org. Replace `cueporch-sandbox` with the alias for your sandbox. [Salesforce CLI deploy reference](https://developer.salesforce.com/docs/platform/salesforce-cli-reference/guide/cli_reference_project_deploy_start.html)

```powershell
sf project deploy start --metadata-dir metadata --single-package --target-org cueporch-sandbox --dry-run
sf project deploy start --metadata-dir metadata --single-package --target-org cueporch-sandbox
```

The Salesforce account used by CuePorch needs API access and permission to read Campaigns, Contacts, and Leads, including their email and email opt-out fields. Outbound registration also needs permission to create Leads, read and write the CuePorch custom fields on Lead, and read and create the two CuePorch custom objects and their fields. Add the **CuePorch Media** and **Registrants** related lists to the relevant Salesforce record pages if your team needs to browse them there.

## Delivery behavior

CuePorch reads a Contact or unconverted Lead by exact email. It leaves existing person records intact, skips opted-out records, and stops a delivery when an email matches multiple people. If it needs to create a Lead, the unique CuePorch Email Key prevents duplicate writes from a repeated delivery. The Media and Registrant records also have unique external keys. CuePorch reads the saved record by key after an uncertain response and preserves a status that has already progressed beyond registration. [Salesforce external ID readback](https://developer.salesforce.com/docs/platform/api-rest/guide/resources-sobject-upsert-get.html)

In the webinar's attendee panel or the video's registration editor, load Salesforce campaigns, select the destination campaign, and enable Salesforce delivery. Save the video's registration form first. The company value applies when CuePorch needs to create a Lead. CuePorch checks that the required metadata and permissions are present before enabling delivery. Viewers can register without sharing their email address, name, and registration time with Salesforce.

Delivery runs automatically by default. Operators can require approval under the contact delivery policy. Refresh Salesforce delivery status in the attendee panel to see waiting and sent counts. Stopping delivery or disconnecting Salesforce cancels pending deliveries. Records already sent remain subject to the Salesforce organization's retention settings.

The source tests use simulated Salesforce responses. Verification in a Salesforce organization and on the shipping packages is pending. Engagement reporting remains in development.

## Engagement reporting

CuePorch's Salesforce metadata defines whole-number watch percentages and video view counts on the Registrant record. The reporting adapter verifies the campaign, media, and Lead or Contact before updating an existing Registrant. It checks the saved values after an update and preserves higher viewing totals, confirmed live attendance, and custom Salesforce statuses. [Salesforce record operations](https://developer.salesforce.com/docs/platform/api-rest/guide/resources-list.html)

For webinars, turn on attendance analytics in the registration form, then enable attendance and watch-time reporting in the Salesforce delivery panel. The Salesforce connection needs permission to update the reporting fields on the Registrant object. Each attendee can separately choose whether to share attendance and watch time. Withdrawing that consent cancels pending reports.

CuePorch schedules the first report one hour after it confirms the webinar has ended. Operators can start reporting sooner from the Salesforce delivery panel once the end is confirmed. Reports run in five-minute cycles for up to 25 minutes. The event engagement policy can require operator approval. It defaults to automatic delivery.

Daily video reporting and related records for Q&A, poll answers, registration answers, and hidden campaign parameters remain in development. [Vimeo Salesforce workflow](https://help.vimeo.com/hc/en-us/articles/32200480938001-About-the-Salesforce-Sales-Cloud-integration-with-Vimeo)
