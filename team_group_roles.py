"""Workspace group defaults and individual role overrides."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_group_role(
 team_group TEXT PRIMARY KEY REFERENCES team_group(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('viewer','contributor','contributor_plus','admin')));
CREATE TABLE IF NOT EXISTS team_member_role_override(
 member TEXT PRIMARY KEY REFERENCES team_member(id) ON DELETE CASCADE);
"""
ROLES = ("viewer", "contributor", "contributor_plus", "admin")

# Individual assignments take precedence. The highest group default applies to members of several groups.
EFFECTIVE_ROLE = """CASE
 WHEN m.role='owner' OR EXISTS(SELECT 1 FROM team_member_role_override direct WHERE direct.member=m.id) THEN m.role
 ELSE coalesce((SELECT defaults.role FROM team_group_member group_member
                JOIN team_group_role defaults ON defaults.team_group=group_member.team_group
                WHERE group_member.member=m.id
                ORDER BY CASE defaults.role
                   WHEN 'admin' THEN 4 WHEN 'contributor_plus' THEN 3 WHEN 'contributor' THEN 2 ELSE 1 END DESC
                LIMIT 1), m.role)
 END"""


def set_direct_role(con, member, enabled):
    if enabled:
        con.execute("INSERT OR IGNORE INTO team_member_role_override VALUES(?)", (member,))
    else:
        con.execute("DELETE FROM team_member_role_override WHERE member=?", (member,))
