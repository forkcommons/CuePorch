"""Publisher grants for password-protected videos in community groups."""

import uuid

from flask import abort


SCHEMA = """
CREATE TABLE IF NOT EXISTS audience_group_playback_grant (
    group_id         TEXT NOT NULL,
    work             TEXT NOT NULL,
    privacy_revision TEXT NOT NULL,
    revision         TEXT NOT NULL,
    approval         TEXT NOT NULL,
    PRIMARY KEY (group_id, work)
);
CREATE INDEX IF NOT EXISTS audience_group_playback_grant_work
    ON audience_group_playback_grant(work);
"""

ACTIVE = (
    "FROM audience_group_playback_grant r "
    "JOIN audience_group g ON g.id=r.group_id AND g.access='members' "
    "JOIN library_privacy p ON p.work=r.work AND p.revision=r.privacy_revision "
    "    AND json_extract(p.settings,'$.visibility')='password' "
    "JOIN action a ON a.id=r.approval AND a.status='eligible'"
)


def available(con, group_id):
    sql = "SELECT r.work " + ACTIVE + " WHERE r.group_id=?"
    return con.execute(sql, (group_id,)).fetchall()


def scope(con, group_id, work, account):
    row = con.execute(
        "SELECT g.id,g.revision,r.revision,?,v.added,m.joined " + ACTIVE
        + """ JOIN audience_group_video v ON v.group_id=r.group_id AND v.kind='work' AND v.slug=r.work
              LEFT JOIN audience_group_member m ON m.group_id=g.id AND m.account=?
              WHERE r.group_id=? AND r.work=? AND (g.owner=? OR m.account IS NOT NULL)""",
        (account, account, group_id, work, account),
    ).fetchone()
    return list(row) if row else None


def group_grants_state(con, uid, privacy, can_grant):
    current_revision = privacy["revision"] if privacy else None
    rows = con.execute(
        """
        SELECT g.slug, g.access, r.revision, r.privacy_revision, a.status
        FROM audience_group_playback_grant r
        JOIN audience_group g ON g.id = r.group_id
        JOIN action a ON a.id = r.approval
        WHERE r.work = ?
        """,
        (uid,),
    ).fetchall()

    grants = []
    for slug, access, revision, privacy_revision, status in rows:
        expired = (
            privacy is None
            or privacy_revision != current_revision
            or access != "members"
            or not can_grant
        )
        grants.append(
            dict(
                slug=slug,
                revision=revision,
                state="expired" if expired else status,
            )
        )

    return dict(
        privacy_revision=current_revision,
        can_grant=can_grant,
        grants=grants,
    )


def update_grant(con, uid, row, body, action):
    cur = con.cursor()
    privacy_revision = row["revision"] if row else None
    if body.get("privacy_revision") != privacy_revision:
        abort(409)
    cur.execute("SELECT id, access FROM audience_group WHERE slug=?", (body['group'],))
    g = cur.fetchone()
    if not g:
        abort(404)
    gid, access = g
    cur.execute("SELECT privacy_revision, revision, approval FROM audience_group_playback_grant WHERE group_id=? AND work=?", (gid, uid))
    existing = cur.fetchone()
    expected = body.get('grant_revision')
    if expected is None:
        if existing is not None:
            abort(409)
    elif existing is None or existing[1] != expected:
        abort(409)
    if body['allowed'] and access != 'members':
        abort(403)
    if existing and existing[2]:
        cur.execute("UPDATE action SET status='rejected', decision='superseded' WHERE id=? AND status='waiting'", (existing[2],))
    if not body['allowed']:
        cur.execute("DELETE FROM audience_group_playback_grant WHERE group_id=? AND work=?", (gid, uid))
    else:
        rev = uuid.uuid4().hex
        action_id = 'group-playback-' + rev
        action(con, action_id, 'publication', dict(work=uid, group=gid, visibility='group_password'))
        cur.execute("INSERT INTO audience_group_playback_grant(group_id, work, privacy_revision, revision, approval) VALUES(?,?,?,?,?) ON CONFLICT(group_id, work) DO UPDATE SET privacy_revision=excluded.privacy_revision, revision=excluded.revision, approval=excluded.approval", (gid, uid, privacy_revision, rev, action_id))
