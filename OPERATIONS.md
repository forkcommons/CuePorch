# Operating CuePorch

**CuePorch by ForkCommons · Development version 0.3.0**

For public institutional hosting, the commands below use the source deployment. The desktop installer runs a loopback operator service.

## Set up a host

Use one private data directory and isolated PeerTube destination per institution. Protect operator.json and the host .env with the operating system's service account permissions. On Linux the launcher creates private directories and credential files. On Windows select a user-private directory or apply the intended service account ACL. A named operator credential map can be supplied in CUEPORCH_OPERATOR_KEYS as a JSON object keyed by operator name.

Run the launcher as a service under the institution's process manager with an absolute installation path and working directory. Retain authenticated administrative access to the host. The institutional service runs Waitress. Put an HTTPS reverse proxy in front of the application and PeerTube. Configure the canonical video hostname, public port and HTTPS in the PeerTube host .env before initial publication, and set CUEPORCH_PUBLIC_URL for the stable site origin. Retain that canonical hostname across moves. Configure the reverse proxy's upload size and request timeouts for the institution's masters. Set CUEPORCH_MAX_UPLOAD_BYTES to the chosen limit. Keep private agreements, logs, media and backups on operator-controlled storage. Avoid access logs containing viewing session paths.

For viewer accounts, preserve the public `Host` header at the reverse proxy and set `CUEPORCH_PUBLIC_URL` to the exact HTTPS origin. Local desktop accounts can also use the loopback HTTP address. Take the institution database along when restoring accounts and Watch Later lists. Viewer identities and sessions are separate from operator credentials. The email address is a local sign-in identifier. Email delivery and recovery require future account integration.

For team invitations, set `CUEPORCH_PUBLIC_URL` to the workspace’s public HTTPS origin and configure `CUEPORCH_SMTP_HOST`, `CUEPORCH_SMTP_FROM` and optionally `CUEPORCH_SMTP_PORT` in the service environment. The relay must accept SMTP over TLS on connect. The default port is 465. If the relay requires authentication, supply both `CUEPORCH_SMTP_USER` and `CUEPORCH_SMTP_PASSWORD` through the service’s private environment. CuePorch checks the relay certificate against the service’s CA trust. After restarting, open **Team members** and test against an address you control. If the relay rejects the invitation, the created link remains available to copy. A relay acceptance means it accepted the message for delivery.

Run each workspace with a dedicated data directory, operator credential, listener and HTTPS origin. A shared reverse proxy can route each hostname to its own listener. Use **Manage workspace links** to show approved destinations to groups. Invite members into the destination and assign their role there. For local development, run two desktop services with distinct `CUEPORCH_DESKTOP_PORT` values and use their loopback origins.

For a managed installation with separate public and local addresses use a service account against the actual canonical HTTPS PeerTube origin through --existing-peer. The default local listener configuration is for a workstation or staging host. Set support contact, coverage hours and on-call routing with the institution before offering staffed support.

For same-link replacement on an existing PeerTube server, verify that `video_file.update.enabled` is true and that the connected publishing account can edit the hosted video. CuePorch's provisioned host enables the setting. When an existing host declines replacement, the library retains both cuts and shows the host error in the delivery status. After the host configuration is corrected, retry the delivery for that work.

## Live and replay

For scheduled events, connect a live-enabled PeerTube instance and open the event's production controls. The provisioned Compose host listens on the same RTMP port inside and outside the container so the returned encoder address uses the configured port. The local default is 11935. Existing hosts must advertise a reachable ingest hostname and port. Remote encoders need operator-controlled ingest routing.

To offer direct Low-latency RTMP, run [MediaMTX](https://mediamtx.org/docs/features/authentication) as a sidecar on the CuePorch host. Use a separate listener and a high-entropy secret dedicated to this sidecar. The configurations below use the default CuePorch application port and dedicated local relay ports.

```yaml
rtsp: false
rtmp: true
rtmpAddress: 127.0.0.1:11936
hls: false
webrtc: false
srt: false
moq: false
api: true
apiAddress: 127.0.0.1:11937
authMethod: http
authHTTPAddress: http://cueporch-relay:REPLACE_WITH_RANDOM_SECRET@127.0.0.1:19001/api/internal/rtmp/authorize
authHTTPExclude:
  - action: api
paths:
  "~^cueporch/[a-f0-9]{32}$":
    source: publisher
    overridePublisher: false
```

Set `CUEPORCH_RTMP_RELAY_URL=rtmp://127.0.0.1:11936`, `CUEPORCH_RTMP_RELAY_API=http://127.0.0.1:11937` and `CUEPORCH_RTMP_RELAY_SECRET` to the matching secret in the CuePorch service environment. Keep the control API and authorization callback on loopback, and exclude the callback from public proxy routes. Use at least 24 ASCII characters that are safe in the user-info portion of a URL. For remote senders, configure MediaMTX's RTMPS listener and a trusted certificate, keep its CuePorch reader local, and set `CUEPORCH_RTMP_PUBLISH_URL` to the externally reachable RTMPS origin with its port. Never publish the control API. MediaMTX 1.21.1 is the relay version used for source commissioning.

For a Fail-safe MPEG-DASH contribution, expose CuePorch on its configured HTTPS origin. In the occurrence's production controls, choose **Prepare MPEG-DASH contribution** and copy its manifest endpoint and upload token. Set `CUEPORCH_DASH_TOKEN` in the transmitter's private environment. Run the transmitter on the encoder workstation with the manifest address shown in CuePorch.

```sh
python dash_transmitter.py --listen "https://media.example.org/event-ingest/EVENT/OCCURRENCE/RECEIVER/feed.mpd"
```

Direct the encoder to `rtmp://127.0.0.1:11938/live` with stream key `cueporch`. Use `--source` to change the listening address or to pull an existing feed when running without `--listen`. The transmitter repackages video and audio, retries transient upload errors, and sends the final manifest when the encoder stops normally. Use `--ca-file` to supply your internal certificate authority. Prefer an HTTPS reverse proxy that accepts at least 32 MiB for individual media fragments. Keep the published media contact domain and ingest credentials under the station's control.

The native launcher also accepts `--dash-transmitter` followed by the same options. It uses the service's bundled FFmpeg.

CuePorch accepts completed fragmented MP4 objects and an MPEG-DASH manifest with relative numbered templates. Each occurrence has its own credentials, delayed playback window and temporary spool. The delayed feed is also sent to the connected PeerTube host. To finish a contribution, send its remaining fragments before the final static manifest. CuePorch accepts exact transmission retries and seals the contribution against changes. Once the delayed audience has received the last fragments, the same watch link shows the event's end or waits for the saved replay. Verify the station's complete sender, receiver and playback path before an event.

For a permanent station signal, create a PeerTube live destination with live_destination.py. Supply PEERTUBE_USERNAME and PEERTUBE_PASSWORD privately. The command writes encoder settings to the specified private file and prints the embed. Send CivicCast's RTMP output to the corresponding address and stream key. Keep the local recording running independently.

Set a stable channel or meeting link with PUT /api/routes/channel/CHANNEL or /api/routes/meeting/MEETING, authorized with the operator credential. Supply revision, destination, description and public_authorized. For required civic records set public_record. The destination may be a PeerTube /videos/embed/UUID or CivicCast /api/public/embed-player/channel/CHANNEL. Choose the captioned CivicCast route until the actual hosted RTMP path carries selectable, synchronized live captions. For a station package the stable replay slug is the returned work ID. Use that same slug for the meeting's live route to preserve its URL at handoff.

For CivicCast-produced WebVTT on MPEG-TS HLS, station_caption_clock.py aligns zero-based cues to the presentation clock. Supply the captions directory and the retained first segment for that program timeline. Run it as new caption segments arrive. Existing timestamp maps are preserved. Retain a separate first segment for a new program or discontinuity. The automated rehearsal used synthetic spoken cues through the real CivicCast subtitle writer and public player.

## Recover and exit

Preserve the collection manifest and objects, CuePorch institution.sqlite, destination receipts, operator credentials, PeerTube database, data/config volumes and host .env. Backing up with `--database` includes the original and rendered files for saved Live Studio presentations. Keep backups off the serving host. Pause publishing jobs and stop the institutional service for an offline snapshot. The backup command temporarily stops PeerTube, captures it and restarts it. An existing snapshot path is protected from overwrite. Treat the snapshot as private because it contains instance secrets, rights agreements and unpublished presentations.

```sh
python deploy/recovery.py backup /private/station/host /private/station/collection /backup/station-snapshot --database /private/station/institution.sqlite
python deploy/recovery.py restore /backup/station-snapshot /private/restored --port 29000 --rtmp-port 21935
```

The destination must be a new directory and use fresh volumes. The tool verifies file hashes, restores PostgreSQL and media and preserves the original public URL. Route the institution's test hostname to the recovered listener. Test captions, known UUIDs, restricted screenings, grants and role access before switching public links. For a restored institutional application, run launch.py with its restored directory, the original institution name and --existing-peer pointed to the restored host with its service credentials. For rollback, point the institution-controlled domain to its last verified instance. External Vimeo IDs and URLs remain under Vimeo's control.

## Monitor

Check failed or waiting jobs in the operator library and approval queue. Retrying an eligible delivery reconciles its local receipt with the destination before a new upload. Use the dry run and delivery report to distinguish absent media, missing metadata and hosting failures. Monitor free disk space, database backups, transcode queue, ingress, egress and support inbox. Before an upgrade, snapshot and restore a private copy, repeat the affected acceptance cases, and retain the last working source bundle and pinned images. Price egress and archival copies using the institution's own traffic and provider rates.

## Live event API

Issue credentials in **Live events → Developer access**. A token belongs to a single CuePorch workspace and is revealed at creation. Send it in the `Authorization: Bearer` header to that workspace's HTTPS origin. Follow the normal watch and embed URLs for audience playback. Registration, privacy, regional restrictions and publication approval apply there as they do in the operator console.

Routes below are relative to `/api/v1/live-events`. The event ID and occurrence timestamp are returned when an event is created or read. Calendar queries accept `from` and `to` as ISO dates, with a range of up to one year. For a finished replay, the event detail response includes watch and embed URLs for the applicable occurrence, subject to the event's publication and embed settings.

| Scope | Routes | Behavior |
| --- | --- | --- |
| `read` | `GET` at the base path, `GET /{event}`, `GET /{event}/{opens}/health` | Read the calendar, current revision, playback URLs, occurrences and host health. |
| `schedule` | `POST` at the base path, `PUT /{event}` | Create or update the same event record as the operator console. The update body supplies the last returned `revision`. An older revision returns HTTP 409. |
| `produce` | `POST /{event}/{opens}/encoder`, `POST or DELETE /{event}/{opens}/rtmp`, `POST or DELETE /{event}/{opens}/dash`, `GET or POST /{event}/{opens}/preview`, `GET or PUT /{event}/{opens}/program`, `DELETE /{event}/{opens}/delivery` | Prepare an encoder, start or stop the RTMP relay or MPEG-DASH contribution, inspect or release a private preview, configure a prerecorded program or withdraw delivery. Encoder credentials are sensitive. |
| `registrations` | `GET or PUT /{event}/registration`, `GET /{event}/registration/export`, `POST /{event}/{opens}/registrants`, `DELETE /{event}/{opens}/registrants/{id}/join`, `GET /{event}/registrants/{id}` | Manage the form, export registrations, enroll for an occurrence, revoke its personal link or retrieve the attendee referenced by a webhook. Only give this scope to an application authorized to handle those records. |
| `registrations` | `GET /{event}/{opens}/registrants`, `GET /{event}/{opens}/registration/export`, `POST /{event}/{opens}/registration/import` | Read or export the occurrence roster, or import contacts from a UTF-8 CSV with optional column mapping. |

```sh
curl --header "Authorization: Bearer $CUEPORCH_EVENT_TOKEN" "https://media.example.org/api/v1/live-events?from=2026-11-01&to=2026-11-30"
```

Send `application/json` for writes. The event body includes a title, event type, latency, visibility and a schedule. A one-time webinar in the station's local time can use this body.

```json
{
  "title": "Community arts program",
  "slug": "community-arts-program",
  "type": "webinar",
  "latency": "standard",
  "visibility": "public",
  "save_replay": true,
  "schedule": {
    "timezone": "America/Denver",
    "start": "2026-12-10T19:00",
    "end": "2026-12-10T20:00",
    "repeat": "none"
  }
}
```

The response supplies the accepted settings, publication state, stable event links and matching occurrences. Use its event ID to manage registration and its occurrence ID to prepare the encoder. To create a registered webinar, enable its form before preparing the encoder.

To register someone through a trusted attendee application, supply `email`, applicable `first_name` and `last_name` values, and the enabled form's required answer keys in the registrant request. Supply checkbox answers as booleans or as `"yes"` and `"no"`. A retry for the same address and occurrence updates the stored answers and returns the same join link. The response includes the registrant identifier, the relative `join` URL and `expires_at` in epoch seconds. Deliver the full URL, including its fragment, privately to that attendee. The browser redeems it using a first-party cookie for the occurrence. An event password still applies. The event's ordinary form serves embedded viewers. When the occurrence ends, the personal link and its playback grants expire. Revoking the link immediately invalidates media URLs issued through it. Registration data remains available to the authorized organizer.

For bulk CSV registration, send `multipart/form-data` with a file part named `attendees`. Use `preview=1` to read the headers, form fields, suggested mapping and form revision without writing registrations. An explicit `mapping` is JSON from form field IDs to zero-based CSV column numbers. Common headers such as `Email address` and `Given name` map automatically. The optional `revision` rejects changes to the form since the preview. An invalid row aborts the batch and reports its source line. The result includes `added`, `existing`, `duplicates` and `occurrence`. Import can queue configured reminders and follow-ups. Imported attendees do not receive confirmation mail.

The occurrence roster supports an optional case-insensitive `email` substring and an `offset` with 50 entries per page. Attendee IDs, form answers and sources are scoped to the occurrence and are private to operators and clients carrying the `registrations` scope. The occurrence CSV exports the full roster regardless of the current filter. Use the series registration export for earlier records that lack a known occurrence.

The occurrence roster's `watch_time` is null until a viewer explicitly shares watch time and the player records playback in that viewing session. The JSON reports live and replay seconds and playback starts. The CSV leaves those columns blank when there is no named measurement. A viewer who stops sharing removes the association from the already collected samples for that session. Aggregate event metrics still include ordinary playback when the browser permits collection. Opt-in and withdrawal operate inside the viewing session's event and occurrence. Public watch and embed responses keep names, addresses and attendee reports private.

The API responds with HTTP 401 for missing, expired or revoked tokens, HTTP 403 for insufficient scope, and HTTP 429 with `Retry-After` after 120 authorized requests in a UTC minute. This limit is per credential. Use the workspace Owner's authenticated panel or `/api/operator/live-api/credentials` to list, issue or revoke tokens. A full private instance backup includes credential records. Portable collection exports exclude them.

When moving a Vimeo Live integration, issue a CuePorch workspace token and map Vimeo event URIs to CuePorch event IDs. Use the event calendar to select the occurrence before asking for encoder settings, health or delivery controls. Read the replay's `watch` and `embed` values from the occurrence after publication. Store those URLs separately from the series URL. Update callback verification and field names using the contract below. [Vimeo event integration reference](https://developer.vimeo.com/api/live/events)

### Live event webhooks

Set `CUEPORCH_EVENT_WEBHOOK_ORIGINS` to a space-separated list of exact HTTPS receiver origins, including a port when needed, and restart CuePorch. The receiver needs a TLS certificate trusted by CuePorch. Callback URLs may include a path. CuePorch rejects query strings, fragments and embedded credentials, and does not follow redirects.

As workspace Owner, `POST /api/operator/live-api/webhooks` with `endpoint` and a `topics` array. Copy the `signing_secret` from the creation response into your receiver's secret storage. `GET` on the same path lists subscription IDs and supported topics. `GET /api/operator/live-api/webhooks/{id}` lists recent deliveries, attempt counts and HTTP status codes. `DELETE` at that path revokes the subscription and removes its delivery queue and history. To rotate the secret, create a replacement subscription and revoke the old one after switching the receiver.

| Topic | CuePorch sends it when |
| --- | --- |
| `live-event-updated` | An operator or API client saves a new event or revision. |
| `live-event-clip-created` | CuePorch provisions a live clip on the delivery host. |
| `live-event-started`, `live-event-ended` | CuePorch observes the start and end of the occurrence on the delivery host, or an operator withdraws a live occurrence. |
| `live-event-archive-available` | The retained replay is ready and the event's publication rules allow access. |
| `registration-created`, `registration-updated` | An occurrence receives a new attendee or an API or CSV update changes that attendee's stored answers or address casing. The payload includes an opaque `data.registrant` ID. Look up the attendee with `GET /api/v1/live-events/{event}/registrants/{id}` and a token with the `registrations` scope. |

Each JSON delivery contains `id`, `type`, `event`, `occurrence`, `occurred_at` and `data`. The occurrence is null for event-wide changes. Verify the `X-CuePorch-Signature` header against `v1=` followed by the hexadecimal HMAC-SHA256 of the `X-CuePorch-Timestamp` value, a literal period and the raw request body, using the subscription secret. Compare in constant time and reject stale timestamps. Store the `X-CuePorch-Delivery` ID to make processing idempotent, then return HTTP 2xx. CuePorch retries other statuses and connection failures with the same ID and payload, using a fresh timestamp and signature. The five retry delays are 10, 30, 120, 300 and 900 seconds.

### Webinar email relay

Use the public HTTPS origin and the same SMTP-over-TLS settings as team invitations. The relay accepts or rejects each recipient address. The production panel reports `accepted` when the relay accepts a message. Delivery to the recipient's inbox depends on the relay. If the connection fails or the relay rejects a message, CuePorch retries after 15 seconds, one minute and five minutes as long as the message remains applicable. A lost acknowledgment can result in another attempt. Include the event mail queue in private instance backups by preserving `institution.sqlite`.

An operator can read or update `/api/live-events/{event}/registration/email`. Submit the current `revision` and `settings` with `confirmation`, `reminder_minutes` and `follow_up_hours`. Pass an empty reminder list and null follow-up delay to turn those messages off. Turning off a message type cancels its pending jobs. A private, withdrawn or superseded occurrence and revoked personal links prevent sending. Configured publication approval holds the mail until the event is eligible. Use the live event's own settings to control whether a follow-up can link to a released replay.

## Source-only validation

Use the versions in requirements.txt for the product. Browser cases additionally need Playwright and a headless Chromium browser. The Windows rehearsal used Edge. The station integration tests use CivicCast's Python package and, for captions, its built portal-public bundle at revision 0b8ab5accac4fc6ebbc6886d5d3d9006bdc0c65f. Run the exact commands in the acceptance record. Run separate copies for different institutions and migrations. A customer Vimeo account is optional.

For a grouped development rehearsal, build the browser UI with `npm ci --prefix ui` and `npm run build --prefix ui`, then run `python acceptance/installed_package.py desktop_service.py . cutover.json --source --only fresh --cutover-only`. Install the packages in `requirements-acceptance.txt` and have FFmpeg on `PATH`. This starts the local Python service and checks Vimeo migration, workspace permissions, adaptive and progressive playback, embed cutover and recovery. The manually triggered Native packages workflow runs these checks on fresh native installations for Windows, macOS and Linux.

For a portable exit, pause institutional writes and run `python export_institution.py COLLECTION DATABASE NEW_DIRECTORY`. Store this export privately. It includes media, hashes, video details and embed placements, screening agreements, case history, routes, saved slide presentations, and the team roster, grants, groups and membership activity. Team credential hashes, active sessions and invitation tokens are excluded. Use the full offline snapshot to preserve credentials and resume service.

For a local desktop library, shut down CuePorch, then copy its entire data directory to an unused private location. Keep any separate video host's snapshot with it. Start CuePorch with the restored copy as its data directory and preserve the service address when restoring old links. Sign in again and verify known public and password-protected links, group access, and a sample of stored files before restoring traffic.

Configure named editorial contacts with CUEPORCH_OPERATOR_KEYS and publish CUEPORCH_SUPPORT_CONTACT, CUEPORCH_SUPPORT_HOURS, CUEPORCH_ESCALATION_CONTACT and CUEPORCH_RECOVERY_CONTACT. The institution owns these contacts and monitors the case inbox.
