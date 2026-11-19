"""Inventory the public Vimeo API reference and English Help Center for release comparison."""

import csv
import json
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

OUT = Path(__file__).resolve().parents[1] / "evidence"
OUT.mkdir(exist_ok=True)
BASE = "https://developer.vimeo.com/api/reference/"


def fetch(url):
    for attempt in range(3):
        try:
            with urlopen(url, timeout=30) as r:
                return r.read()
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2)


def reference(group):
    s = fetch(BASE + group).decode()
    return json.JSONDecoder().raw_decode(s.split("var devsite=", 1)[1])[0]["reference"]


def write(name, rows):
    with (OUT / name).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    api = []
    for group in reference("videos")["groups"]:
        ref = reference(group)
        for family, spec in ref["openapi"]["paths"].items():
            for path, methods in spec["paths"].items():
                for method, op in methods.items():
                    if not isinstance(op, dict) or "operationId" not in op:
                        continue
                    api.append(
                        dict(
                            group=group,
                            family=family,
                            method=method.upper(),
                            endpoint=path,
                            operation=op["operationId"],
                            outcome=op.get("summary"),
                            version=ref["version"],
                            source=BASE + group + "#" + op["operationId"],
                            coverage="unknown",
                            transfer="unassessed",
                            evidence="",
                            date=date.today().isoformat(),
                        )
                    )
    write("vimeo-api.csv", api)
    articles = []
    url = "https://help.vimeo.com/api/v2/help_center/en-us/articles.json?page[size]=100"
    seen = set()
    while url:
        if url in seen or urlsplit(url).netloc != "help.vimeo.com":
            raise ValueError("Unexpected paging")
        seen.add(url)
        page = json.loads(fetch(url.replace("/articles?", "/articles.json?")))
        for a in page["articles"]:
            articles.append(
                dict(
                    id=a["id"],
                    section=a["section_id"],
                    title=a["title"],
                    source=a["html_url"],
                    updated=a["updated_at"],
                    coverage="unknown",
                    transfer="unassessed",
                    evidence="",
                )
            )
        url = page["links"]["next"] if page["meta"]["has_more"] else None
    write("vimeo-help.csv", articles)
    print("API operations", len(api), "Help articles", len(articles), flush=True)


if __name__ == "__main__":
    main()
