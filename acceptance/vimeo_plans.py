"""Refresh the four-tier feature table from Vimeo's public plan comparison."""

import csv
import html
import re
from pathlib import Path
import requests

URL = "https://vimeo.com/upgrade-plan"
PARTIAL = {
    "Users|Custom permissions": "Institution operator, station, creator and exhibitor credentials; existing Vimeo accounts and groups do not migrate.",
    "Storage|Bandwidth|Data residency": "The operator chooses host, storage and region. Provider quotas, guarantees and compliance do not transfer.",
    "Embed videos anywhere|No ads before, after, or on your video|Custom video link|Custom Subdomain": "Portable institution-controlled embeds and hostname. Rewrite external references using the exported mapping.",
    "Upload or download transcripts|Automated Closed Captioning|Transcription|Transcript editing": "Retain available timed captions. Generate recorded captions with TextFlowKit; attach corrected files without replacing the video URL.",
    "Chapters|Chapter sharing": "Preserve supplied chapter titles and times; Vimeo-specific links need rewriting.",
    "Event broadcast tools|Live API|Live manual closed captions|Live automated closed captioning": "CivicCast and PeerTube software live paths; synchronized supplied cues on CivicCast tested. Event accounts and event settings do not migrate.",
    "Video library|File transfer|Replace version in URL|Version history": "Retain stable work identity, files and local revisions; historic Vimeo cuts, accounts and all folder organization require separate reconciliation.",
    "Video review|Configurable review links": "Operator review queue and restricted screening sessions; Vimeo review links and time-coded team discussions are not imported.",
    "Embed domain level privacy|Restrict video access|Password protection|Unlisted links for sharing": "Record supplied source restrictions and stage privately. Issue new work-specific grants. Source passwords and private URLs do not transfer.",
    "Audit logs (CSV/API)": "Export local decision and route history. No import of historic Vimeo audit events.",
    "Video showcases": "Public catalog and related trailers. Vimeo showcase layout, membership and links are not reconstructed.",
    "Self-serve knowledge base": "Local operator guide, migration instructions and recovery and acceptance records.",
}
MISSING = {
    "Video maker and editing tools",
    "Vimeo AI",
    "Analytics",
    "Priority support",
    "Enterprise services",
}
EXPLICIT_MISSING = {
    "Interactive video",
    "Screen recording",
    "Virtual events & webinars",
    "Simulcasting",
    "Q&A and polls",
    "Audience chat",
    "Live Teleprompter",
    "Guest speakers",
    "Attendee registration",
    "Branded invite and reminder emails",
    "Marketing integrations",
    "Moderated Q&A",
    "eCDN",
    "Webinar API",
    "Zoom and Webex uploads",
    "Slack integration",
    "Adobe Premiere Pro integration",
    "Download review comments",
    "Federated Search API",
    "Learning Platform integration",
    "Embed videos as GIFs in email",
    "Sell videos as a subscription service",
    "Lead capture with custom forms",
    "Marketing Integrations",
    "Digital rights management (DRM)",
    "Geo-blocking",
    "Single sign-on (SSO)",
    "Directory & Provisioning (SCIM)",
    "HIPAA-compliant video playback",
    "SOC 2 Type II Report",
    "China delivery for VOD",
    "Data Processing Addendum (DPA)",
    "Custom cards and calls-to-action in the player",
}


def coverage(section, name):
    for names, detail in PARTIAL.items():
        if name in names.split("|"):
            return "partial", detail
    if section in MISSING or name in EXPLICIT_MISSING:
        return (
            "missing",
            "No delivered equivalent or automatic migration for this feature.",
        )
    return (
        "unknown",
        "Equivalent behavior unverified. Existing configuration requires operator assessment.",
    )


def main():
    response = requests.get(URL, timeout=30)
    response.raise_for_status()
    tables = re.findall(
        r"<h3[^>]*>([^<]*)</h3>\s*<table\b[^>]*>(.*?)</table>", response.text, re.S
    )
    if not tables:
        raise ValueError("Vimeo plan table changed")
    rows = []
    for section, table in tables:
        for row in re.findall(r"<tr\b.*?</tr>", table, re.S):
            cells = [
                html.unescape(re.sub(r"<[^>]*>", "", cell)).strip()
                for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, re.S)
            ]
            if len(cells) == 5 and cells[0]:
                rows.append([section, *cells, *coverage(section, cells[0]), URL])
    with (Path(__file__).resolve().parents[1] / "evidence/vimeo-plans.csv").open(
        "w", encoding="utf8", newline=""
    ) as stream:
        w = csv.writer(stream)
        w.writerow(
            [
                "section",
                "feature",
                "Starter",
                "Standard",
                "Advanced",
                "Enterprise",
                "coverage",
                "CuePorch outcome and transfer",
                "source",
            ]
        )
        w.writerows(rows)
    print(len(rows), "plan comparison rows")


def current_plans():
    article = requests.get(
        "https://help.vimeo.com/api/v2/help_center/en-us/articles/12425432033937.json",
        timeout=30,
    )
    article.raise_for_status()
    data = article.json()["article"]
    aliases = {
        "Video storage": "Storage",
        "Seats": "Users",
        "Ad-free": "No ads before, after, or on your video",
        "Player embedding": "Embed videos anywhere",
        "Embed domain-level privacy": "Embed domain level privacy",
        "Showcases": "Video showcases",
        "Custom URL for your video": "Custom video link",
        "Video privacy settings": "Restrict video access",
        "Automated closed captioning": "Automated Closed Captioning",
        "Upload/download & edit transcripts": "Transcript editing",
        "Video chaptering & SEO enhancements": "Chapters",
        "Screen recordings": "Screen recording",
        "Attendee Registration": "Attendee registration",
        "Access to Vimeo AI features": "Transcription",
        "Live, Webinar, Analytics, & Search API": "Live API",
    }
    excluded = (
        "analytics",
        "templates",
        "stock photos",
        "video editing",
        "timeline editing",
        "teleprompter",
        "training",
        "account manager",
        "invite",
        "q&a",
        "marketing",
        "support during live",
        "highlight videos",
        "sso",
        "custom ctas",
        "gif maker",
        "phone support",
        "production services",
        "email support",
    )
    path = Path(__file__).resolve().parents[1] / "evidence/vimeo-current-plans.csv"
    with path.open("w", encoding="utf8", newline="") as out:
        writer = csv.writer(out)
        for i, row in enumerate(
            re.findall(r"<tr\b.*?</tr>", data["body"], re.S | re.I)
        ):
            cells = [
                html.unescape(re.sub(r"<[^>]*>", "", c)).strip()
                for c in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, re.S | re.I)
            ]
            if i == 0:
                if cells != [
                    "Feature",
                    "Free",
                    "Creator",
                    "Core",
                    "Professional",
                    "Studio",
                    "Enterprise",
                ]:
                    raise ValueError("New plan table needs review")
                writer.writerow(
                    [
                        *cells,
                        "coverage",
                        "CuePorch outcome and transfer",
                        "source",
                        "updated",
                    ]
                )
                continue
            name = cells[0]
            status, detail = coverage("", aliases.get(name, name))
            if status == "unknown" and any(t in name.lower() for t in excluded):
                status, detail = (
                    "missing",
                    "No delivered feature-specific service or account-data transfer.",
                )
            writer.writerow(
                [*cells, status, detail, data["html_url"], data["updated_at"]]
            )
    print(i, "current plan comparison rows")


if __name__ == "__main__":
    main()
    current_plans()
