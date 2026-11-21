"""Accountless local viewer registration and operator-managed form data."""

import csv
from datetime import datetime, timezone
from io import StringIO
import json
import re
import time
import uuid

from flask import Response, abort, current_app, make_response, render_template, request

from library_appearance import brand_image
from library_interactivity import image_id, text_field, web_link
import registration_locale


SCHEMA = """
CREATE TABLE IF NOT EXISTS viewer_form(
 kind TEXT NOT NULL, subject TEXT NOT NULL, revision TEXT NOT NULL, settings TEXT NOT NULL,
 PRIMARY KEY(kind,subject));
CREATE TABLE IF NOT EXISTS viewer_registrant(
 kind TEXT NOT NULL, subject TEXT NOT NULL, email_key TEXT NOT NULL, email TEXT NOT NULL,
 registered REAL NOT NULL, answers TEXT NOT NULL, PRIMARY KEY(kind,subject,email_key));
CREATE TABLE IF NOT EXISTS viewer_form_preset(
 name TEXT PRIMARY KEY COLLATE NOCASE, settings TEXT NOT NULL);
"""
UTM_FIELDS = ("utm_source", "utm_campaign", "utm_term", "utm_content", "utm_medium")
DEFAULT = dict(enabled=False, heading="Register to watch", description="", privacy_url="",
               first_name="optional", last_name="optional", fields=[], collect_utm=False, layout="single",
               attendance_analytics=False, translations={})
DEFAULT_APPEARANCE = dict(alignment="center", logo=None, background_image=None,
                          background="#e7eee5", overlay="#ffffff", opacity=25)
FORM_TYPES = ("text", "select", "checkbox", "description", "hidden")
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def configured(settings):
    return DEFAULT | settings | {"appearance": DEFAULT_APPEARANCE | settings.get("appearance", {})}


def read_form(con, kind, subject):
    row = con.execute("SELECT revision,settings FROM viewer_form WHERE kind=? AND subject=?",
                      (kind, subject)).fetchone()
    return (row["revision"], configured(json.loads(row["settings"]))) if row else (None, configured({}))


def active(con, kind, subject):
    revision, settings = read_form(con, kind, subject)
    return dict(settings, revision=revision) if settings["enabled"] else None


def validation(body, kind="video"):
    if not isinstance(body, dict) or any(type(body.get(key)) is not bool for key in ("enabled", "collect_utm")):
        abort(422, "Choose valid registration settings.")
    result = {key: body[key] for key in ("enabled", "collect_utm")}
    analytics = body.get("attendance_analytics", False)
    if type(analytics) is not bool:
        abort(422, "Choose whether to offer attendee watch-time sharing.")
    result["attendance_analytics"] = kind == "event" and analytics
    for field in ("first_name", "last_name"):
        if body.get(field) not in ("off", "optional", "required"):
            abort(422, "Choose which name fields to ask for.")
        result[field] = body[field]
    result["heading"] = text_field(body.get("heading"), "a form heading", 80, True)
    result["description"] = text_field(body.get("description", ""), "a form description", 1000)
    result["privacy_url"] = web_link(body.get("privacy_url", ""))
    layout = body.get("layout", "single")
    if layout not in ("single", "steps"):
        abort(422, "Choose a registration layout.")
    result["layout"] = layout
    raw = body.get("appearance", {})
    if not isinstance(raw, dict):
        abort(422, "Choose a registration appearance.")
    appearance = {name: raw.get(name, value) for name, value in DEFAULT_APPEARANCE.items()}
    if appearance["alignment"] not in ("start", "center", "end"):
        abort(422, "Choose a registration form alignment.")
    if type(appearance["opacity"]) is not int or not 0 <= appearance["opacity"] <= 100:
        abort(422, "Choose an overlay opacity from 0 to 100.")
    for name in ("background", "overlay"):
        color = appearance[name]
        if not isinstance(color, str) or not re.fullmatch(r"#[a-fA-F0-9]{6}", color):
            abort(422, "Use six-digit hexadecimal colors.")
    result["appearance"] = appearance
    custom = body.get("fields")
    if not isinstance(custom, list) or len(custom) > 20:
        abort(422, "A registration form can have up to 20 custom fields.")
    fields = []
    keys = set()
    parameters = set()
    for entry in custom:
        if not isinstance(entry, dict) or entry.get("type") not in FORM_TYPES:
            abort(422, "Choose a supported field type.")
        key, kind = entry.get("id"), entry["type"]
        if not isinstance(key, str) or not re.fullmatch(r"field_[a-z0-9_]{1,25}", key) or key in keys:
            abort(422, "Each custom field needs a unique identifier.")
        keys.add(key)
        label = text_field(entry.get("label"), "a field label", 240, True)
        required = entry.get("required", False) if kind not in ("description", "hidden") else False
        if type(required) is not bool:
            abort(422, "Choose whether the field is required.")
        choices = []
        if kind == "select":
            options = entry.get("options")
            if not isinstance(options, list) or not 1 <= len(options) <= 25:
                abort(422, "A dropdown needs one to 25 choices.")
            choices = [text_field(option, "a dropdown choice", 80, True) for option in options]
            if len(set(choices)) != len(choices):
                abort(422, "Use distinct dropdown choices.")
        field = dict(id=key, type=kind, label=label, required=required, options=choices)
        if kind == "checkbox":
            field["link"] = web_link(entry.get("link", ""))
        if kind == "hidden":
            parameter = entry.get("parameter")
            if (not isinstance(parameter, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", parameter)
                    or parameter in parameters):
                abort(422, "Enter a unique hidden field parameter using lowercase letters, digits and underscores.")
            parameters.add(parameter)
            field["parameter"] = parameter
            field["default_value"] = text_field(entry.get("default_value", ""), "a hidden field default", 250)
        fields.append(field)
    return dict(result, fields=fields, translations=registration_locale.validate(body.get("translations", {}), fields))


def check_images(con, settings):
    appearance = settings["appearance"]
    for name in ("logo", "background_image"):
        appearance[name] = image_id(con, appearance[name])


def presentation(con, form, sign_on=None, language=None, *, mailchimp_target=None):
    appearance = form["appearance"]
    selected, catalogue = registration_locale.choices(form, language)
    offers = {}
    if mailchimp_target:
        kind, uid, scope = mailchimp_target
        for name in ("mailchimp_form_consent", "constant_contact_form_consent", "campaign_monitor_form_consent"):
            provider = current_app.extensions.get(name)
            if provider:
                offers[name] = provider.offer(con, kind, uid, form, scope)
    return dict(form, logo_src=brand_image(con, appearance["logo"]),
                background_src=brand_image(con, appearance["background_image"]),
                language=selected, text=catalogue[selected], catalogue=catalogue,
                can_sign_on=bool(current_app.extensions["registration_sign_on"].relay), sign_on=sign_on,
                **offers)


def admit(con, kind, uid, form, scope, occurrence=None, on_record=None):
    if request.form.get("registration_action"):
        return current_app.extensions["registration_sign_on"].attempt(con, kind, uid, form, scope, occurrence)
    if on_record is None and kind in ("video", "showcase"):
        captures = []
        for name in ("mailchimp_form_consent", "constant_contact_form_consent", "campaign_monitor_form_consent"):
            provider = current_app.extensions.get(name)
            if provider:
                capture = provider.capture(con, kind, uid, form, scope, request.form.get(name, ""))
                if capture is not None:
                    captures.append(capture)
        if captures:
            def on_record(*args, **kwargs):
                for capture in captures:
                    capture(*args, **kwargs)
    error = enroll(con, kind, uid, form, request.form, request.args, on_record=on_record)
    return error is None, None, error


def answers_from(definition, fields):
    values = {}
    for field in ("first_name", "last_name"):
        mode = definition[field]
        if mode == "off":
            continue
        name = fields.get(field, "").strip()
        if len(name) > 80 or (mode == "required" and not name):
            return "Enter the required name fields.", {}
        values[field] = name
    for field in definition["fields"]:
        key, form_type = field["id"], field["type"]
        if form_type in ("description", "hidden"):
            continue
        answer = fields.get(key, "").strip()
        if form_type == "checkbox":
            value = answer == "yes"
            if field["required"] and not value:
                return "Check the required boxes.", {}
        else:
            value = answer
            if field["required"] and not value:
                return "Complete all required fields.", {}
            if form_type == "select" and value and value not in field["options"]:
                return "Choose an option from the form.", {}
            if form_type == "text" and len(value) > 50:
                return "Use up to 50 characters in each text response.", {}
        values[key] = value
    return None, values


def meets_current_requirements(form, saved):
    required = dict(form, fields=[field for field in form["fields"] if field["required"]])
    for name in ("first_name", "last_name"):
        if required[name] != "required":
            required[name] = "off"
    values = {key: "yes" if value is True else value for key, value in saved.items()
              if isinstance(value, str) or value is True}
    error, _ = answers_from(required, values)
    return error is None


def enroll(con, kind, subject, definition, fields, query, *, replace=False, on_record=None):
    address = fields.get("email", "").strip()
    if len(address) > 254 or not EMAIL.fullmatch(address):
        return "Enter a valid email address."
    error, values = answers_from(definition, fields)
    if error:
        return error
    if definition["collect_utm"]:
        for key in UTM_FIELDS:
            value = query.get(key, "")[:250]
            if value:
                values[key] = value
    for field in definition["fields"]:
        if field["type"] == "hidden":
            values[field["id"]] = query.get("reg_" + field["parameter"], field["default_value"])[:250]
    address_key = address.casefold()
    conflict = ("DO UPDATE SET email=excluded.email,answers=excluded.answers" if replace else "DO NOTHING")
    con.execute("INSERT INTO viewer_registrant VALUES(?,?,?,?,?,?) ON CONFLICT(kind,subject,email_key) " + conflict,
                (kind, subject, address_key, address, time.time(), json.dumps(values)))
    if on_record:
        on_record(address, values)
    return None


def registrant_rows(con, kind, subject, limit=-1):
    return con.execute(
        """SELECT email,registered,answers FROM viewer_registrant WHERE kind=? AND subject=?
           ORDER BY registered DESC,email_key LIMIT ?""", (kind, subject, limit),
    )


def csv_cell(value):
    if type(value) is bool:
        return "Yes" if value else "No"
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text[:1] in ("\t", "\r") else text


def operator_form(con, kind, uid, protected_public=False):
    revision, config = read_form(con, kind, uid)
    if request.method == "PUT":
        body = request.get_json()
        if not isinstance(body, dict) or body.get("revision") != revision:
            abort(409, "The registration form changed. Reopen the editor.")
        supplied = body.get("settings")
        if isinstance(supplied, dict) and "appearance" not in supplied:
            supplied = dict(supplied, appearance=config["appearance"])
        updated = validation(supplied, kind)
        check_images(con, updated)
        if updated["enabled"] and protected_public:
            abort(409, "Registration cannot gate a designated public record.")
        name = text_field(body.get("preset_name", ""), "a registration preset name", 80)
        if name:
            con.execute("""INSERT INTO viewer_form_preset VALUES(?,?) ON CONFLICT(name) DO UPDATE SET
                           settings=excluded.settings""", (name, json.dumps(updated)))
        if config != updated:
            if kind in ("video","showcase") and current_app.extensions.get("mailchimp_form_consent"):
                import library_mailchimp_forms
                library_mailchimp_forms.stop(con,kind,uid)
            if kind in ("video","showcase") and current_app.extensions.get("constant_contact_form_consent"):
                import library_list_registration
                library_list_registration.stop_all(con,kind,uid)
            revision = uuid.uuid4().hex
            config = updated
            con.execute("""INSERT INTO viewer_form VALUES(?,?,?,?) ON CONFLICT(kind,subject) DO UPDATE SET
                           revision=excluded.revision,settings=excluded.settings""", (kind, uid, revision, json.dumps(config)))
    return dict(revision=revision, settings=config, protected_public=protected_public,
                presets={row["name"]: configured(json.loads(row["settings"])) for row in con.execute(
                    "SELECT name,settings FROM viewer_form_preset ORDER BY name"
                )},
                registrants=[dict(email=row["email"], registered=row["registered"], answers=json.loads(row["answers"]))
                             for row in registrant_rows(con, kind, uid, limit=100)])


def export(con, kind, uid):
    entries = [(row["email"], row["registered"], json.loads(row["answers"]))
               for row in registrant_rows(con, kind, uid)]
    keys = ["first_name", "last_name", *UTM_FIELDS]
    keys += sorted({key for _, _, values in entries for key in values if key.startswith("field_")})
    output = StringIO()
    csv_data = csv.writer(output)
    csv_data.writerow(["email", "registration_date_utc", *keys])
    for email, registered, answers in entries:
        row = [email, datetime.fromtimestamp(registered, timezone.utc).isoformat()]
        row += [answers.get(key, "") for key in keys]
        csv_data.writerow(csv_cell(value) for value in row)
    return Response(output.getvalue(), mimetype="text/csv", headers={
        "Cache-Control": "no-store",
        "Content-Disposition": "attachment; filename=cueporch-registrants.csv",
    })


def register(app, tx, staff, get_work, protected_public):
    @app.context_processor
    def registration_language_names():
        return dict(registration_languages=registration_locale.LANGUAGES)

    @app.post("/api/registration/preview")
    def registration_preview():
        staff()
        request.max_content_length = 4 * 1024 * 1024
        body = request.get_json()
        if not isinstance(body, dict):
            abort(422, "Choose a registration preview.")
        screen = body.get("screen", "form")
        if screen not in ("form", "sign-on"):
            abort(422, "Choose a registration preview screen.")
        language = body.get("language", "en")
        if not isinstance(language, str) or language not in registration_locale.LANGUAGES:
            abort(422, "Choose a registration preview language.")
        settings = validation(body.get("settings"), "event" if body.get("event") is True else "video")
        with tx() as con:
            check_images(con, settings)
            form = presentation(con, settings, {"step": "email"} if screen == "sign-on" else None, language)
        response = make_response(dict(html=render_template(
            "registration_preview.html", registration=form, enrollment="",
            registration_preview=True, offer_attendee_sharing=form["attendance_analytics"],
        )))
        response.cache_control.private = True
        response.cache_control.no_store = True
        return response

    @app.route("/api/library/work/<uid>/registration", methods=["GET", "PUT"])
    def video_registration(uid):
        staff()
        with tx() as con:
            work = get_work(con, uid)
            if work.get("asset_type", "video") != "video":
                abort(404)
            return operator_form(con, "video", uid, protected_public(con, uid))

    @app.get("/api/library/work/<uid>/registration/export")
    def export_registration(uid):
        staff()
        with tx() as con:
            get_work(con, uid)
            return export(con, "video", uid)
