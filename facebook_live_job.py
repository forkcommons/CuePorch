"""Persist Facebook scheduling outcomes without repeating uncertain writes."""

from flask import abort
from werkzeug.exceptions import HTTPException

from facebook_live import FacebookLiveUnconfirmed


SCHEMA = """
CREATE TABLE IF NOT EXISTS facebook_live_creation(
 event TEXT NOT NULL, opens INTEGER NOT NULL, page TEXT NOT NULL, revision TEXT NOT NULL,
 state TEXT NOT NULL, destination TEXT, PRIMARY KEY(event,opens,page));
"""


def provision(tx, check, create, save, event, opens, page, revision):
    key = (event, opens, page)
    with tx() as con:
        check(con)
        prior = con.execute("SELECT * FROM facebook_live_creation WHERE event=? AND opens=? AND page=?", key).fetchone()
        if prior:
            if prior["revision"] != revision:
                abort(409, "This broadcast was prepared using another Facebook connection.")
            if prior["state"] != "prepared":
                return {"state": "unconfirmed"}, 202
            destination = prior["destination"]
            if not con.execute("SELECT 1 FROM live_simulcast_destination WHERE id=?", (destination,)).fetchone():
                abort(409, "This broadcast was removed. Check its status in Facebook Live Producer.")
            return {"state": "prepared", "id": destination}, 200
        con.execute("INSERT INTO facebook_live_creation VALUES(?,?,?,?,'sending',NULL)", (*key, revision))

    try:
        target = create()
    except FacebookLiveUnconfirmed:
        with tx() as con:
            con.execute("UPDATE facebook_live_creation SET state='unconfirmed' WHERE event=? AND opens=? AND page=?", key)
        return {"state": "unconfirmed"}, 202
    except HTTPException as error:
        if error.code is not None and 400 <= error.code < 500:
            with tx() as con:
                con.execute("DELETE FROM facebook_live_creation WHERE event=? AND opens=? AND page=?", key)
        raise

    with tx() as con:
        check(con)
        destination = save(con, target)
        con.execute("UPDATE facebook_live_creation SET state='prepared',destination=? WHERE event=? AND opens=? AND page=?",
                    (destination, *key))
    return {"state": "prepared", "id": destination}, 201
