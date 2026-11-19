# Operating CuePorch

**CuePorch by ForkCommons · Version 0.2.0**

For public institutional hosting, the commands below use the source deployment. The desktop installer runs a loopback operator service.

## Set up a host

Use one private data directory and isolated PeerTube destination per institution. Protect operator.json and the host .env with the operating system's service account permissions. On Linux the launcher creates private directories and credential files. On Windows select a user-private directory or apply the intended service account ACL. A named operator credential map can be supplied in CUEPORCH_OPERATOR_KEYS as a JSON object keyed by operator name.

Run the launcher as a service under the institution's process manager with an absolute installation path and working directory. Retain authenticated administrative access to the host. The institutional service runs Waitress. Put an HTTPS reverse proxy in front of the application and PeerTube. Configure the canonical video hostname, public port and HTTPS in the PeerTube host .env before initial publication, and set CUEPORCH_PUBLIC_URL for the stable site origin. Retain that canonical hostname across moves. Configure the reverse proxy's upload size and request timeouts for the institution's masters. Set CUEPORCH_MAX_UPLOAD_BYTES to the chosen limit. Keep private agreements, logs, media and backups on operator-controlled storage. Avoid access logs containing viewing session paths.

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

## Source-only validation

Use the versions in requirements.txt for the product. Browser cases additionally need Playwright and a headless Chromium browser. The Windows rehearsal used Edge. The station integration tests use CivicCast's Python package and, for captions, its built portal-public bundle at revision 0b8ab5accac4fc6ebbc6886d5d3d9006bdc0c65f. Run the exact commands in the acceptance record. Run separate copies for different institutions and migrations. A customer Vimeo account is optional.

For a portable exit, pause institutional writes and run `python export_institution.py COLLECTION DATABASE NEW_DIRECTORY`. Store this export privately. It includes the media, hashes, screening agreements, case history, routes and saved slide presentations. The presentation records identify the original files and PNGs for reuse. Use the full offline snapshot to preserve credentials and resume service.

Configure named editorial contacts with CUEPORCH_OPERATOR_KEYS and publish CUEPORCH_SUPPORT_CONTACT, CUEPORCH_SUPPORT_HOURS, CUEPORCH_ESCALATION_CONTACT and CUEPORCH_RECOVERY_CONTACT. The institution owns these contacts and monitors the case inbox.
