# Vimeo feature accounting

**CuePorch by ForkCommons · Version 0.1.0**

Source snapshot taken November 18, 2026. The public API reference exposed 404 method/path entries across 25 groups at version 3.4.9. The English Help Center feed exposed 1,268 articles, including account support and billing. An article, endpoint and end-user feature are different units. The attached CSVs preserve these denominators separately. Coverage applies to this candidate and the particular workflow tested. Unknowns receive no credit.

| Vimeo workflow | CuePorch equivalent and transfer fidelity | Candidate evidence and limit |
|---|---|---|
| Inventory an owned library | Partial. Paginated inventory preserves distinct IDs even for identically named cuts. Reports sources lacking media. | Migration rehearsal, three source IDs. Authenticated Vimeo behavior is simulated. |
| Retrieve owner downloads and masters | Partial. Recover the supplied original when identified, otherwise the best advertised downloadable file. Report owner file needed if neither exists. | FFmpeg decode, independent SHA-256, refreshed link and checkpoint replay. Adaptive-only extraction is unverified. |
| Captions and transcripts | Partial. Preserve and attach timed VTT and language to the hosted work. Keep generated provenance. | Source fixture and real PeerTube caption endpoint. Translation, transcript editing and multilingual audio equivalence remain unknown. |
| Chapters and credits | Partial. Capture documented chapter and credit fields, and attach supplied chapters to the PeerTube video. | Independent fixture comparison, real chapter update. Thumbnail and third-party credit identity transfer are unverified. |
| Resumable video upload and hosting | Partial. Real private-first PeerTube upload, remote processing, retry identity, optional public visibility and player. | Hosted VOD test. Large-account quota, enterprise storage and global edge capacity remain unmeasured. |
| Player and embeds | Partial. Actual PeerTube player with institution-controlled watch and embed routes. Export source-to-destination mappings. | Headless Edge VOD and live playback. Existing embeds on sites outside the institution still require edits by those site owners. |
| Live streaming and replay | Partial. RTMP insert, real web playback, independent local recording and separately publishable replay. | Software encoder, five-second VOD and 60-second local capture. Separate CivicCast web-path caption synchronization measured 0.08 seconds on a speech fixture. Field receiver and full hardware chain remain unverified. |
| Privacy and exhibition | Partial. Imported items stay private. Domain restrictions are inventoried. Work/version grants have audience, agreement, dates and independent revocation. | Two real browser embeds and media requests; revocation, decision and restoration exercised. Password transfer and enterprise DRM are unsupported. |
| Report, appeal and restore | Partial. Work-specific case history and scoped suspension or restoration for restricted screenings. | Two simulated roles, real database and HTTP outcomes. Human response time remains unobserved. |
| Creator submissions | Partial. Browser intake captures master, supplied VTT, poster, credits and terms; a private work can produce a separate screening derivative. | Browser upload, retained private master, separate viewing derivative and remote delivery exercised in the station-and-artist rehearsal. |
| Versions | Partial. Separate source IDs and retained media revisions; inventory reported version metadata and permission gaps. | Same-title cut and metadata tests. Migration of each prior Vimeo version and associated audio track is unverified. |
| Team review and collaboration | Partial. Institution operator roles, configurable review queue and case messages. | Approval and exhibition tests. Team memberships, time-coded review-link comments and historic discussions lack transfer evidence. |
| Showcases, folders, portfolios and library search | Partial. Institution catalog and reusable PeerTube channels/playlists. | No verified automatic reproduction of a customer's full folder or showcase organization. |
| Webinars, registration, interactivity and shoppable video | Missing from the delivered CuePorch workflows. | No implementation or migration evidence for registration, polls, interactive hotspots or sales links. |
| Screen recorder, editor, creator and teleprompter | Missing from the bounded product. | Existing files can be ingested. No delivered recording/editing studio or teleprompter. |
| AI generation, translation, dubbing and highlights | Missing except for recorded caption generation via TextFlowKit. | Prior LPM promo transcription produced a VTT draft. Caption accuracy and the other AI functions remain unverified. |
| Analytics and historical engagement | Missing for migration. | No import of historical plays, audiences or enterprise analytics. |
| OTT, purchases, subscriptions and payments | Missing. | No migration of customers, payment details, transactions, apps or entitlements. |
| Enterprise identity, governance and residency | Unknown for full equivalence. Operators choose hosting location. | Managed identity, SSO, enterprise compliance certification and enterprise service guarantees are unverified. |
| Mobile apps, integrations, SEO and marketing | Unknown for full equivalence. | Web playback alone provides insufficient evidence for native apps, integration APIs or SEO parity. |

The release must also reconcile any feature-level unknowns material to the two target workflows. The broader reference tables preserve gaps instead of counting unexamined entries as supported.

Sources: [Vimeo feature catalog](https://vimeo.com/features), [API reference](https://developer.vimeo.com/api/reference/), [Help Center](https://help.vimeo.com/), [download links](https://help.vimeo.com/hc/en-us/articles/12427806914577), [text tracks](https://help.vimeo.com/hc/en-us/articles/17480150130833).

## Reference coverage

The API inventory classifies 10 entries as partial, 85 as missing and 309 as unknown. The Help Center inventory classifies 6 as partial, 424 as missing, 73 as provider guidance and 765 as unknown. Unknown means the candidate has insufficient evidence for equivalent behavior. Each table has its own denominator. Neither measures the percentage of customers who can migrate. The [player SDK inventory](vimeo-player.csv) lists 99 published JavaScript methods and events. CuePorch supplies replacement embeds. Sites using the Vimeo.Player JavaScript contract require integration changes.

The [legacy-tier pricing table](vimeo-plans.csv) covers all 124 feature rows in the published Starter, Standard, Advanced and Enterprise comparison table. CuePorch has partial equivalents for 31 rows, lacks 66 and leaves 27 unverified. No row received full-equivalence credit. Rows include repeated features across sections. The four paid columns preserve the published plan limits and inclusion values. Account-specific entitlements depend on the customer’s plan and transition history. [Vimeo plan source](https://vimeo.com/upgrade-plan)

The [current feature matrix](vimeo-current-plans.csv) uses the Help Center table updated November 18, spanning Free, Creator, Core, Professional, Studio and Enterprise. Of its 50 rows, 15 have partial CuePorch equivalents, 28 are missing and 7 are unverified. Zero received full-equivalence credit. Several items group multiple functions, so a partial match can be as narrow as transcription or supplied chapters. Vimeo identifies Starter, Standard and Advanced as transitioning legacy tiers. Inventory the assets actually returned by the source account before estimating recoverable masters. [Current plans](https://help.vimeo.com/hc/en-us/articles/12425432033937) · [2026 transition](https://help.vimeo.com/hc/en-us/articles/52596250640785)
