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
| `produce` | `POST /{event}/{opens}/encoder`, `GET or POST /{event}/{opens}/preview`, `GET or PUT /{event}/{opens}/program`, `DELETE /{event}/{opens}/delivery` | Prepare the encoder, inspect or release a private preview, configure a prerecorded program or withdraw delivery. Encoder credentials are sensitive. |
| `registrations` | `GET or PUT /{event}/registration`, `GET /{event}/registration/export`, `POST /{event}/{opens}/registrants`, `DELETE /{event}/{opens}/registrants/{id}/join` | Manage the form, retrieve attendee data, enroll for an occurrence and revoke its personal link. Only give this scope to an application authorized to handle those records. |

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

The API responds with HTTP 401 for missing, expired or revoked tokens, HTTP 403 for insufficient scope, and HTTP 429 with `Retry-After` after 120 authorized requests in a UTC minute. This limit is per credential. Use the workspace Owner's authenticated panel or `/api/operator/live-api/credentials` to list, issue or revoke tokens. A full private instance backup includes credential records. Portable collection exports exclude them.

## Source-only validation

Use the versions in requirements.txt for the product. Browser cases additionally need Playwright and a headless Chromium browser. The Windows rehearsal used Edge. The station integration tests use CivicCast's Python package and, for captions, its built portal-public bundle at revision 0b8ab5accac4fc6ebbc6886d5d3d9006bdc0c65f. Run the exact commands in the acceptance record. Run separate copies for different institutions and migrations. A customer Vimeo account is optional.

For a grouped development rehearsal, build the browser UI with `npm ci --prefix ui` and `npm run build --prefix ui`, then run `python acceptance/installed_package.py desktop_service.py . cutover.json --source --only fresh --cutover-only`. Install the packages in `requirements-acceptance.txt` and have FFmpeg on `PATH`. This starts the local Python service and checks Vimeo migration, workspace permissions, adaptive and progressive playback, embed cutover and recovery. The manually triggered Native packages workflow runs these checks on fresh native installations for Windows, macOS and Linux.

For a portable exit, pause institutional writes and run `python export_institution.py COLLECTION DATABASE NEW_DIRECTORY`. Store this export privately. It includes media, hashes, video details and embed placements, screening agreements, case history, routes, saved slide presentations, and the team roster, grants, groups and membership activity. Team credential hashes, active sessions and invitation tokens are excluded. Use the full offline snapshot to preserve credentials and resume service.

For a local desktop library, shut down CuePorch, then copy its entire data directory to an unused private location. Keep any separate video host's snapshot with it. Start CuePorch with the restored copy as its data directory and preserve the service address when restoring old links. Sign in again and verify known public and password-protected links, group access, and a sample of stored files before restoring traffic.

Configure named editorial contacts with CUEPORCH_OPERATOR_KEYS and publish CUEPORCH_SUPPORT_CONTACT, CUEPORCH_SUPPORT_HOURS, CUEPORCH_ESCALATION_CONTACT and CUEPORCH_RECOVERY_CONTACT. The institution owns these contacts and monitors the case inbox.
