"""Store the presentation settings of the event player and its pre-live view."""

from calendar import day_name
import json

from flask import abort, request

from library_appearance import COLORS, PALETTE, brand_image


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_player_settings(event TEXT PRIMARY KEY, settings TEXT NOT NULL);
"""
SWITCHES = ("show_schedule", "show_recurring", "show_latest", "show_live_badge", "show_viewers")
PLAYER_SWITCHES = dict(customize=False, progress=True, volume=True, pip=True, fullscreen=True, show_title=True)
PLAYER_COLORS = {key: value for key, value in COLORS if key in ("primary", "accent", "icon")}
APPEARANCE = PLAYER_SWITCHES | PLAYER_COLORS | {"play_position": "bottom"}
DEFAULTS = dict.fromkeys(SWITCHES, False) | dict(poster=None, logo=None, byline="") | APPEARANCE


def settings(con, event):
    row = con.execute("SELECT settings FROM live_player_settings WHERE event=?", (event,)).fetchone()
    return DEFAULTS | (json.loads(row["settings"]) if row else {})


def appearance(values, event):
    return {key: values[key] for key in APPEARANCE} | {"live_seek": event.get("dvr_seconds") != 0}


def recurring(schedule):
    units = {"days": "day", "weeks": "week", "months": "month"}
    unit = units.get(schedule["repeat"])
    if not unit:
        return ""
    step = schedule["interval"]
    cadence = f"Every {unit}" if step == 1 else f"Every {step} {unit}s"
    if schedule["repeat"] == "weeks":
        cadence += " on " + ", ".join(day_name[day] for day in schedule["weekdays"])
    elif schedule["repeat"] == "months":
        cadence += f" on day {int(schedule['start'][8:10])}"
    return f"{cadence} at {schedule['start'][11:]} ({schedule['timezone']})"


def lobby(values, event, occurrence, artwork):
    schedule = event["schedule"]
    interval = recurring(schedule) if values["show_recurring"] else ""
    shown = occurrence if values["show_schedule"] else None
    if not (interval or artwork or values["show_schedule"]):
        return None
    return dict(poster=artwork, date=shown["start"] if shown else "", recurring=interval,
                message=(f"{shown['local'].replace('T', ' ')} ({schedule['timezone']})" if shown
                         else "No upcoming broadcasts" if values["show_schedule"] else ""))


def register(app, tx, staff, read):
    @app.route("/api/live-events/<uid>/player", methods=("GET", "PUT"))
    def live_player_settings(uid):
        staff()
        with tx() as con:
            _, event = read(con, uid)
            values = settings(con, uid)
            if request.method == "PUT":
                body = request.get_json()
                if not isinstance(body, dict):
                    abort(422, "Choose event player settings.")
                values = {key: body.get(key, values[key]) for key in DEFAULTS}
                if any(type(values[key]) is not bool for key in (*SWITCHES, *PLAYER_SWITCHES)):
                    abort(422, "Choose event player settings.")
                if any(not isinstance(values[key], str) or not PALETTE.fullmatch(values[key]) for key in PLAYER_COLORS):
                    abort(422, "Use six-digit hexadecimal player colors.")
                if values["play_position"] not in ("bottom", "center", "auto"):
                    abort(422, "Choose a play button position.")
                byline = values["byline"]
                if not isinstance(byline, str) or len(byline) > 100 or (byline and not byline.isprintable()):
                    abort(422, "Enter a byline of up to 100 characters.")
                values["byline"] = byline.strip()
                for name in ("poster", "logo"):
                    digest = values[name]
                    if digest is not None and (not isinstance(digest, str) or not con.execute(
                            "SELECT 1 FROM review_logo WHERE digest=?", (digest,)).fetchone()):
                        abort(422, f"Choose an uploaded {name} image.")
                con.execute("INSERT OR REPLACE INTO live_player_settings VALUES(?,?)", (uid, json.dumps(values)))
            return dict(values, image=brand_image(con, values["poster"]), logo_image=brand_image(con, values["logo"]),
                        recurring=recurring(event["schedule"]))
