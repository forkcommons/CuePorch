"""Record sourced plan qualifiers and assign the owning Vimeo Help Center section."""

import csv
import html
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1] / "evidence"


def pages(family):
    url = (
        f"https://help.vimeo.com/api/v2/help_center/en-us/{family}.json?page[size]=100"
    )
    while url:
        if urlsplit(url).netloc != "help.vimeo.com":
            raise ValueError("Unexpected source")
        with urlopen(url, timeout=30) as response:
            data = json.load(response)
        yield from data[family]
        url = (
            data["links"]["next"].replace("/" + family + "?", "/" + family + ".json?")
            if data["meta"]["has_more"]
            else None
        )


def requirements(body):
    clean = html.unescape(re.sub("<[^>]*>", " ", body))
    words = " ".join(clean.split())
    sentences = re.split(r"(?<=[.!?])\s+", words[:900])
    qualifying = next(
        (
            s
            for s in sentences[:5]
            if re.search(
                r"(requires? |available (?:to|for|on)|included (?:in|with)|must have).{0,120}(plan|member|account|enterprise|credit|subscription)",
                s,
                re.I,
            )
        ),
        None,
    )
    if not qualifying:
        return "No explicit entitlement identified in the introductory text"
    names = re.findall(
        r"\b(Basic|Free|Starter|Standard|Advanced|Enterprise|Plus|Pro|Business|Premium)\b",
        qualifying,
        flags=re.I,
    )
    if re.search(r"all (?:Vimeo )?(?:plans|accounts|users)", qualifying, re.I):
        return "All accounts; see source for feature-specific qualifications"
    if names:
        return (
            "Referenced tiers: "
            + ", ".join(dict.fromkeys(x.title() for x in names))
            + "; see source for alternatives and conditions"
        )
    if re.search(r"paying|paid", qualifying, re.I):
        return "Paid entitlement; verify credits or supplementary conditions at source"
    return "Source specifies an eligibility condition; see linked article"


def main():
    sections = {s["id"]: s["name"] for s in pages("sections")}
    arts = {
        str(a["id"]): (
            sections.get(a["section_id"], str(a["section_id"])),
            requirements(a["body"]),
        )
        for a in pages("articles")
    }
    file = ROOT / "vimeo-help.csv"
    with file.open(encoding="utf8", newline="") as f:
        rows = list(csv.DictReader(f))
    missing = set(r["id"] for r in rows) - arts.keys()
    if missing:
        raise ValueError("Help catalog changed; refresh the source inventory")
    for r in rows:
        r["section_name"], r["plan_limits"] = arts[r["id"]]
    with file.open("w", encoding="utf8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(len(rows), "articles;", len(sections), "sections")


if __name__ == "__main__":
    main()
