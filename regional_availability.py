"""Evaluate optional publication territories using a trusted deployment signal."""

import csv
from io import StringIO
import re

from flask import request

from library_analytics import REGION_CODE, observed_region


DEFAULT = dict(mode="anywhere", areas=[])
ZIP = re.compile(r"[0-9]{5}")
HEADERS = ("MODE", "COUNTRY", "REGION", "CITY", "ZIP CODE")
MAX_AREAS = 1000
MAX_BYTES = 262144


def validated(body):
    if not isinstance(body, dict) or body.get("mode") not in ("anywhere", "allow", "deny"):
        raise ValueError("Choose where the event is available.")
    mode = body["mode"]
    if mode == "anywhere":
        return DEFAULT.copy()
    areas = body.get("areas")
    if not isinstance(areas, list) or not 1 <= len(areas) <= MAX_AREAS:
        raise ValueError(f"Enter one to {MAX_AREAS} territories.")
    rows = []
    for area in areas:
        if not isinstance(area, str) or not 1 <= len(area) <= 120 or not area.strip().isprintable():
            raise ValueError("Enter a country, subdivision or United States ZIP code.")
        code, delimiter, city = area.strip().partition("|")
        code, city = code.strip().upper(), city.strip()
        if ZIP.fullmatch(code) and not delimiter:
            rows.append(code)
        elif REGION_CODE.fullmatch(code) and (not delimiter or (city and code.split("-")[0] == "US")):
            rows.append(f"{code} | {city}" if city else code)
        else:
            raise ValueError("Use an ISO country or subdivision, a US ZIP code, or US-CO | City.")
    return dict(mode=mode, areas=list(dict.fromkeys(rows)))


def from_csv(text):
    rows = csv.reader(StringIO(text, newline=""), strict=True)
    header = tuple(value.strip().upper() for value in next(rows, []))
    if len(header) != len(HEADERS) or set(header) != set(HEADERS):
        raise ValueError("Use the MODE, COUNTRY, REGION, CITY and ZIP CODE columns.")
    mode = None
    areas = []
    for row in rows:
        if not any(value.strip() for value in row):
            continue
        if len(row) != len(HEADERS):
            raise ValueError("Enter one territory in each CSV row.")
        values = dict(zip(header, (value.strip() for value in row)))
        row_mode = {"allowed": "allow", "blocked": "deny"}.get(values["MODE"].lower())
        if row_mode is None or mode not in (None, row_mode):
            raise ValueError("Use all allowed records or all blocked records in a file.")
        mode = row_mode
        country, region = values["COUNTRY"].upper(), values["REGION"].upper()
        city, postcode = values["CITY"], values["ZIP CODE"]
        if postcode:
            if country not in ("", "US") or region or city or not ZIP.fullmatch(postcode):
                raise ValueError("Zip codes apply to the United States. Leave the region and city blank.")
            areas.append(postcode)
        else:
            if country != (region.split("-")[0] if region else country) or not re.fullmatch(r"[A-Z]{2}", country):
                raise ValueError("Use a two-letter country code and its full subdivision code.")
            code = region or country
            areas.append(f"{code} | {city}" if city else code)
        if len(areas) > MAX_AREAS:
            raise ValueError(f"Use no more than {MAX_AREAS} territories per file.")
    return validated(dict(mode=mode, areas=areas))


def csv_text(policy):
    out = StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(HEADERS)
    mode = "allowed" if policy["mode"] == "allow" else "blocked"
    for area in policy["areas"]:
        code, _, city = area.partition(" | ")
        if ZIP.fullmatch(code):
            writer.writerow((mode, "US", "", "", code))
        else:
            # Preserve the territory while preventing an exported city from becoming a spreadsheet formula.
            if city.lstrip().startswith(("=", "+", "-", "@")):
                city = "'" + city
            writer.writerow((mode, code.split("-")[0], code if "-" in code else "", city, ""))
    return out.getvalue()


def match(area, region, city, postcode):
    country = region.split("-")[0]
    if ZIP.fullmatch(area):
        return False if country != "US" else None if postcode is None else area == postcode
    code, _, name = area.partition(" | ")
    if code.split("-")[0] != country:
        return False
    if "-" in code and region != code:
        return None if "-" not in region else False
    if name:
        return name.casefold() == city.casefold() if city else None
    return True


def permits(policy, trusted_proxies):
    if policy["mode"] == "anywhere":
        return True
    region = observed_region(trusted_proxies)
    if not region:
        return False
    city = request.headers.get("X-CuePorch-City", "").strip()[:100]
    postcode = request.headers.get("X-CuePorch-Postal-Code", "").strip()
    zipcode = postcode if ZIP.fullmatch(postcode) else None
    matches = [match(area, region, city, zipcode) for area in policy["areas"]]
    return any(result is True for result in matches) if policy["mode"] == "allow" else all(result is False for result in matches)
