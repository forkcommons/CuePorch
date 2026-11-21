"""Persist LinkedIn scheduling and announcement outcomes across retries."""

from flask import abort
from werkzeug.exceptions import HTTPException

from linkedin_live import LinkedInWriteUnconfirmed


SCHEMA = """
CREATE TABLE IF NOT EXISTS linkedin_live_creation(
 event TEXT NOT NULL, opens INTEGER NOT NULL, company TEXT NOT NULL,
 event_revision TEXT NOT NULL, revision TEXT NOT NULL,
 state TEXT NOT NULL, video TEXT, post TEXT, PRIMARY KEY(event,opens,company));
"""
READ = "SELECT * FROM linkedin_live_creation WHERE event=? AND opens=? AND company=?"
UPDATE = """UPDATE linkedin_live_creation SET state=?,video=?,post=?
            WHERE event=? AND opens=? AND company=?"""


def provision(tx, check, create, announce, event, opens, company, event_revision, revision):
    key = (event, opens, company)
    with tx() as con:
        check(con)
        row = con.execute(READ, key).fetchone()
        if row:
            if (row["event_revision"], row["revision"]) != (event_revision, revision):
                abort(409, "The event or LinkedIn connection has changed. Check the existing schedule on LinkedIn.")
            if row["state"] == "scheduled":
                return {"state": "scheduled", "post_urn": row["post"]}, 200
            if row["state"] != "created":
                return {"state": "unconfirmed"}, 202
            video = row["video"]
        else:
            con.execute("INSERT INTO linkedin_live_creation VALUES(?,?,?,?,?,'creating',NULL,NULL)",
                        (*key, event_revision, revision))
            video = None

    if video is None:
        try:
            video = create()
        except LinkedInWriteUnconfirmed:
            return {"state": "unconfirmed"}, 202
        except HTTPException as error:
            if error.code is not None and 400 <= error.code < 500:
                with tx() as con:
                    con.execute("DELETE FROM linkedin_live_creation WHERE event=? AND opens=? AND company=?", key)
            raise
        with tx() as con:
            con.execute(UPDATE, ("created", video, None, *key))

    with tx() as con:
        check(con)
        con.execute(UPDATE, ("announcing", video, None, *key))
    try:
        post = announce(video)
    except LinkedInWriteUnconfirmed:
        return {"state": "unconfirmed"}, 202
    except HTTPException as error:
        if error.code is not None and 400 <= error.code < 500:
            with tx() as con:
                con.execute(UPDATE, ("created", video, None, *key))
        raise
    with tx() as con:
        con.execute(UPDATE, ("scheduled", video, post, *key))
    return {"state": "scheduled", "post_urn": post}, 201
