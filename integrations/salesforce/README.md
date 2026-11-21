# CuePorch metadata for Salesforce Sales Cloud

The metadata defines Salesforce records for CuePorch media and registrations. A CuePorch Media record links to a Campaign. Each CuePorch Registrant links to that Media record and either a Lead or a Contact. Both custom objects default to private sharing. An administrator can grant record access to the people who manage or report on the campaign.

## Deploy to a Salesforce org

Open this directory in a terminal with the Salesforce CLI and an authorized org alias. Check the package in a sandbox before deploying to your production org. Replace `cueporch-sandbox` with the alias for your sandbox. [Salesforce CLI deploy reference](https://developer.salesforce.com/docs/platform/salesforce-cli-reference/guide/cli_reference_project_deploy_start.html)

```powershell
sf project deploy start --metadata-dir metadata --single-package --target-org cueporch-sandbox --dry-run
sf project deploy start --metadata-dir metadata --single-package --target-org cueporch-sandbox
```

The Salesforce account used by CuePorch needs API access and permission to read Campaigns, Contacts, and Leads, including their email and email opt-out fields. Outbound registration also needs permission to create Leads, read and write the CuePorch custom fields on Lead, and read and create CuePorch Media and Registrant records. Enable the additional object permissions described under engagement reporting for the features you use.

## Browse a campaign in Salesforce

1. In Salesforce Setup, open **Object Manager**, select **Campaign**, and open **Page Layouts**. Edit the layout assigned to your campaign team. Add the **CuePorch Media** related list and show **Title** and **Kind**. In Lightning Experience, the Campaign record page needs a **Related Lists** or **Related List–Single** component that shows the list. [Salesforce related list configuration](https://help.salesforce.com/s/articleView?id=platform.customizing_related_lists.htm&language=en_US&type=5)
2. Assign **CuePorch Media Reporting** and **CuePorch Registrant Reporting** to the appropriate profiles on their respective custom objects. The Media layout shows **Registrants**. The Registrant layout shows the registration date, status, watch percentage, and views, plus **Registration and Hidden Fields**, **Poll Answers**, and **Questions and Answers**.
3. Assign the **CuePorch Reporting Reader** permission set to the people who need report access. It grants read access to the CuePorch custom objects. Use your organization's sharing settings to give those people access to the specific media, registrants, and related records for each campaign. Grant access to the Campaign and to any Leads or Contacts they need to open through your existing Salesforce policies.
4. Open a campaign that has received a CuePorch registration. Under **Related**, open a media title and then a registrant. Check **Details** for viewing metrics and **Related** for submitted registration and engagement records. The related lists show activity that viewers chose to share and that CuePorch delivered. The first Media record appears after the first Salesforce registration for that video or webinar occurrence.

## Delivery behavior

CuePorch reads a Contact or unconverted Lead by exact email. It leaves existing person records intact, skips opted-out records, and stops a delivery when an email matches multiple people. If it needs to create a Lead, the unique CuePorch Email Key prevents duplicate writes from a repeated delivery. The Media and Registrant records also have unique external keys. CuePorch reads the saved record by key after an uncertain response and preserves a status that has already progressed beyond registration. [Salesforce external ID readback](https://developer.salesforce.com/docs/platform/api-rest/guide/resources-sobject-upsert-get.html)

In the webinar's attendee panel or the video's registration editor, load Salesforce campaigns, select the destination campaign, and enable Salesforce delivery. Save the video's registration form first. The company value applies when CuePorch needs to create a Lead. CuePorch checks that the required metadata and permissions are present before enabling delivery. Viewers can register without sharing their email address, name, and registration time with Salesforce.

Delivery runs automatically by default. Operators can require approval under the contact delivery policy. Refresh Salesforce delivery status in the attendee panel to see waiting and sent counts. Stopping delivery or disconnecting Salesforce cancels pending deliveries. Records already sent remain subject to the Salesforce organization's retention settings.

The source tests use simulated Salesforce responses. Verification in a Salesforce organization and on the shipping packages is pending.

## Engagement reporting

CuePorch's Salesforce metadata defines whole-number watch percentages and video view counts on the Registrant record. The reporting adapter verifies the campaign, media, and Lead or Contact before updating an existing Registrant. It checks the saved values after an update and preserves higher viewing totals, confirmed live attendance, and custom Salesforce statuses. [Salesforce record operations](https://developer.salesforce.com/docs/platform/api-rest/guide/resources-list.html)

For webinars, turn on attendance analytics in the registration form, then enable attendance and watch-time reporting in the Salesforce delivery panel. The connection needs permission to update the reporting fields on the Registrant object and to read and create records in the **CuePorch Webinar Poll** and **CuePorch Webinar Q&A** objects. Save the delivery settings again after deploying the new objects to a Salesforce org with an existing webinar connection.

Each attendee can separately choose whether to share attendance, watch time, poll answers, named questions, and upvotes on named questions. Add **Poll Answers** and **Questions and Answers** to the Registrant record page to see that activity. Withdrawing consent cancels pending reports.

CuePorch schedules the first report one hour after it confirms the webinar has ended. Operators can start reporting sooner from the Salesforce delivery panel once the end is confirmed. Reports run in five-minute cycles for up to 25 minutes. The event engagement policy can require operator approval. It defaults to automatic delivery.

For registered videos, enable daily reporting in the Salesforce section of the video’s registration editor. The connection needs permission to update the watch and view fields on the Registrant object. A viewer can separately opt in to share their video activity after choosing to share registration details. The player offers a control to stop sharing future video activity with Salesforce. Previously delivered records remain subject to the Salesforce organization’s retention settings.

The first reporting window opens one hour after the original upload timestamp, or after the video was added to CuePorch when the original timestamp is unavailable. The reporting window then recurs every 24 hours. CuePorch sends watch percentage based on the portions viewed and counts playback sessions recorded with consent. The event engagement policy applies to these reports and defaults to automatic delivery.

For videos and webinars with custom questions, hidden fields, or UTM collection, deploy the **CuePorch Registration Field** object and grant the integration read and create access to it. Save Salesforce delivery for the video or webinar occurrence to enable the related fields. When a viewer opts in to Salesforce delivery, the answers they submit and the configured campaign parameters appear under **Registration and Hidden Fields** on their Registrant record. Add that related list to the Registrant record page for your team. The contact delivery approval setting applies to the registration and its related fields.
