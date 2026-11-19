"""Store the presentation settings of the event player and its pre-live view."""

from calendar import day_name
import json

from flask import abort, request

from library_appearance import brand_image


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_player_settings(event TEXT PRIMARY KEY, settings TEXT NOT NULL);
"""
DEFAULTS = {"show_schedule": False, "show_recurring": False, "poster": None}


def settings(con, event):
    row = con.execute("SELECT settings FROM live_player_settings WHERE event=?", (event,)).fetchone()
    return DEFAULTS | (json.loads(row["settings"]) if row else {})


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
                if not isinstance(body, dict) or any(type(body.get(key)) is not bool for key in ("show_schedule", "show_recurring")):
                    abort(422, "Choose event player settings.")
                poster = body.get("poster")
                if poster is not None and (not isinstance(poster, str) or not con.execute(
                        "SELECT 1 FROM review_logo WHERE digest=?", (poster,)).fetchone()):
                    abort(422, "Choose an uploaded poster image.")
                values = {key: body.get(key) for key in DEFAULTS}
                con.execute("INSERT OR REPLACE INTO live_player_settings VALUES(?,?)", (uid, json.dumps(values)))
            return dict(values, image=brand_image(con, values["poster"]), recurring=recurring(event["schedule"]))
