"""Attach explicit, outcome-specific assessments to the dated public reference inventory."""

import csv
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "evidence"
MIGRATION = "acceptance/test_migration.py"
PLAYER = "evidence/peertube-playback.json"
EXHIBIT = "acceptance/test_exhibition.py"
PARTIAL = {
    "get_videos": (
        "Source library enumerated with stable IDs and pagination.",
        "Recorded owner metadata and supported assets.",
        MIGRATION,
    ),
    "get_text_tracks": (
        "Caption discovery and preservation.",
        "Downloaded available tracks and retained language.",
        MIGRATION,
    ),
    "get_video_privacy_domains": (
        "Domain discovery and isolated screening grants.",
        "Retained domain names. Agreements require independent reconstruction.",
        EXHIBIT,
    ),
    "get_video_credits": (
        "Portable credit fields.",
        "Recorded identifying fields available in the source response.",
        MIGRATION,
    ),
    "get_chapters": (
        "Chapter display and portable cue points.",
        "Title and timecode preserved.",
        PLAYER,
    ),
    "get_video_versions": (
        "Version inventory.",
        "Records available fields or denied access. Historical media retrieval remains unverified.",
        MIGRATION,
    ),
    "create_text_track": (
        "PeerTube caption attachment.",
        "Available timed text attached to destination video.",
        PLAYER,
    ),
    "edit_text_track": (
        "Replace a caption in place.",
        "Published work identity retained on correction.",
        PLAYER,
    ),
    "get_video": (
        "Hosted work details and stable identity.",
        "Owner metadata is only partially mapped.",
        PLAYER,
    ),
    "create_chapter": (
        "Publish chapter timecodes to hosted work.",
        "Titles and timestamps supported.",
        PLAYER,
    ),
}
OUTSIDE = {
    "payments": "payments and financial records",
    "on-demand": "commercial catalogs and buyer entitlements",
    "subscription-plans": "subscription management",
    "api-apps": "Vimeo developer application administration",
    "watch-later-queue": "personal Vimeo watch history and queues",
    "likes": "historic Vimeo reactions",
    "portfolios": "Vimeo portfolio-site administration",
    "embed-presets": "account-wide Vimeo embed presets",
}
ARTICLE = {
    "12427806914577": PARTIAL["get_videos"],
    "17480150130833": PARTIAL["get_text_tracks"],
    "12425955868817": PARTIAL["get_text_tracks"],
    "30030693052305": PARTIAL["get_video_privacy_domains"],
    "12426058338961": PARTIAL["get_video_versions"],
    "12425450200209": PARTIAL["get_videos"],
}
NO_HISTORY = "No historical account data transferred."


def assess(row):
    if "operation" in row:
        if row["operation"] in PARTIAL:
            return ("partial", *PARTIAL[row["operation"]])
        if row["group"] in OUTSIDE:
            return (
                "missing",
                "CuePorch has no delivery for " + OUTSIDE[row["group"]] + ".",
                NO_HISTORY,
                "Scope and delivered source",
            )
        if row["group"] in (
            "analytics",
            "groups",
            "teams",
            "showcases",
            "folders",
            "channels",
            "users",
        ):
            return (
                "unknown",
                "No operation-specific parity demonstrated.",
                "No verified reconstruction of this source object.",
                "",
            )
    else:
        if row["id"] in ARTICLE:
            return ("partial", *ARTICLE[row["id"]])
        section = row.get("section_name", "")
        if section in {
            "About Vimeo subscription plans",
            "Managing your Vimeo subscription",
            "Payment issues and FAQs",
            "Fair use",
            "DMCA",
            "Copyright Law and Infringement",
            "Miscellaneous Legal",
            "Trademark",
            "Creative Commons",
        }:
            return (
                "provider guidance",
                "Provider policy, subscription or legal guidance.",
                "No transferable software function established by this article.",
                "Article section and published title",
            )
        if section in {
            "Editing in a web browser",
            "Editing on mobile",
            "Creating in a web browser",
            "Interactive overlays",
            "Getting started with interactive",
            "Hotspots",
            "Interactive video quizzes",
            "Purchased videos",
            "Managing Vimeo On Demand titles",
            "Vimeo On Demand Taxes and Payout",
            "Vimeo app for Apple Vision Pro",
            "Vimeo Android app",
            "TV apps",
            "Record Studio",
            "Interactive analytics",
            "AI Credits",
            "Getting started with Vimeo On Demand",
            "Webinar email customization",
            "Streaming Analytics on Vimeo OTT",
        }:
            return (
                "missing",
                f"The delivered product has no equivalent for the Vimeo {section} workflow.",
                "No transfer of the feature-specific assets or configuration.",
                "Delivered platform scope and implementation",
            )
        title = row["title"].lower()
        for terms, description in [
            (("vimeo ott", "my ott", "my vhx"), "OTT service"),
            (
                ("sales tax", "vat", "billing", "refund", "invoice"),
                "Vimeo billing/account operations",
            ),
            (
                ("teleprompter", "shoppable", "interactive hotspot"),
                "studio or commerce tooling",
            ),
            (
                ("scim", "saml", "single sign-on", " sso"),
                "enterprise identity provisioning",
            ),
            (
                ("ai dubbing", "ai translation", "generate a script"),
                "AI creation and translation",
            ),
        ]:
            if any(t in title for t in terms):
                return (
                    "missing",
                    "CuePorch does not implement this " + description + " workflow.",
                    NO_HISTORY,
                    "Scope and delivered source",
                )
    return (
        "unknown",
        "Outcome equivalence unverified.",
        "No transfer guarantee recorded for this item.",
        "",
    )


def main():
    totals = []
    for filename in ("vimeo-api.csv", "vimeo-help.csv"):
        file = ROOT / filename
        with file.open(encoding="utf8", newline="") as f:
            rows = list(csv.DictReader(f))
        for row in rows:
            row["coverage"], row["equivalent"], row["transfer"], row["evidence"] = (
                assess(row)
            )
            row.setdefault(
                "plan_limits", "See linked Vimeo record; entitlement unverified"
            )
        with file.open("w", encoding="utf8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        totals.append(
            (filename, len(rows), dict(Counter(row["coverage"] for row in rows)))
        )
    print(totals)


if __name__ == "__main__":
    main()
